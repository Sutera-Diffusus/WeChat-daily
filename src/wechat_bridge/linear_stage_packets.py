"""K8 development-only linear stage packet storage.

This module is intentionally independent from the older compact packet
projector.  It is a small, local data structure for experiments with staged
requests.  A root owns ordered page references; the pages point at one global
message, content, candidate, and evidence table.  The page streams are
aligned by ordinal position, never multiplied as a cartesian product.

The public API is deliberately narrow::

    LinearStagePacketStore
    build_linear_stage_packets
    materialize_stage_a
    materialize_stage_b
    materialize_stage_c
    recover_linear_packet

Bodies live only in the content table and are omitted from normal exports and
stage materializers.  ``include_body=True`` is an explicit local replay
choice; no provider, external file, or production state is touched here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple, Union

from .dialogue_segments import has_context_prefix, is_context_only_text


LINEAR_STAGE_PACKET_VERSION = "linear_stage_packet_v1"
LINEAR_STORE_SCHEMA_VERSION = "linear_stage_packet_store_v1"
LINEAR_PIPELINE_VERSION = "workstream_k8_linear_stage_packets_v1"
DEFAULT_MAX_INPUT_TOKEN_PROXY = 2000
DEFAULT_MAX_USER_TOKEN_PROXY = 1600
DEFAULT_MAX_MESSAGES = 24
DEFAULT_MAX_CANDIDATE_ROWS = 64
DEFAULT_MAX_EVIDENCE_REFS = 64
DEFAULT_SYSTEM_PROMPTS = {
    "A": "Extract topic structure from scoped message handles and candidate links.",
    "B": "Inspect the requested topic message handles and evidence handles.",
    "C": "Reconcile stage A and B structures using only scoped evidence handles.",
}

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "messagebody",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_body",
        "transcript",
        "text_redacted",
    }
)
EVIDENCE_KEYS = frozenset({"evidence_refs", "evidence", "evidence_references"})
PRIMARY_KEYS = ("primary_fragments", "primary", "fragments")
ADJACENT_KEYS = ("adjacent_context", "adjacent", "context_fragments", "greeting", "greeting_context")
FACT_KEYS = ("authoritative_facts", "message_metadata", "facts")
SOURCE_KEYS = ("source_refs", "sources", "source_references")
CANDIDATE_KEYS = (
    "candidate_qa_links",
    "candidate_person_history",
    "candidate_object_history",
    "candidate_state_history",
    "continuity_candidates",
    "qa_candidates",
    "person_history",
    "object_history",
    "state_history",
    "candidate_rows",
    "candidates",
    "candidate_reasons",
    "open_thread_candidates",
    "open_threads",
)
LAYER_KEYS = frozenset(PRIMARY_KEYS + ADJACENT_KEYS + FACT_KEYS + SOURCE_KEYS + CANDIDATE_KEYS + ("evidence_refs",))
WEAK_REASON_KEYS = frozenset(
    {
        "time_proximity_weak",
        "same_segment_weak",
        "same_segment_only",
        "time_proximity",
        "time_only",
        "time_proximity_only",
        "same_segment",
        "temporal_proximity",
        "temporal_only",
        "same_dialogue_segment",
    }
)


class LinearStagePacketError(ValueError):
    """Stable fail-closed error for the local K8 contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        super().__init__(self.code if not detail else "%s: %s" % (self.code, detail))


class LinearCapacityError(LinearStagePacketError):
    """A stage envelope cannot satisfy one of the hard limits."""

    def __init__(self, stats: Mapping[str, Any], limits: "LinearCapacity") -> None:
        self.stats = deepcopy(dict(stats))
        self.limits = limits
        super().__init__("capacity_exceeded")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=str)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def estimate_token_proxy(value: Any, system_prompt: str = "") -> int:
    """Estimate request tokens from the system text and canonical user JSON."""

    return (len(str(system_prompt)) + len(canonical_json(value)) + 3) // 4


def _user_token_proxy(value: Any) -> int:
    return (len(canonical_json(value)) + 3) // 4


def _as_mapping(value: Any, *, include_body: bool = True) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return deepcopy({str(key): item for key, item in value.items()})
    method = getattr(value, "to_dict", None)
    if callable(method):
        for kwargs in (
            {"include_body": include_body},
            {"include_bodies": include_body},
            {"include_content": include_body},
            {"include_model_packet": include_body},
            {},
        ):
            try:
                result = method(**kwargs)
            except TypeError:
                continue
            if isinstance(result, Mapping):
                return deepcopy({str(key): item for key, item in result.items()})
    raise LinearStagePacketError("packet_not_mapping")


def _first(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in value and value[name] not in (None, ""):
            return value[name]
    return None


def _text(value: Any, default: str = "unknown") -> str:
    if value is None:
        return default
    result = str(value)
    return result if result else default


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _rows(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        # A named table is accepted in addition to an array of rows.
        if all(isinstance(child, Mapping) for child in value.values()):
            return [deepcopy(dict(child)) for child in value.values()]
        return []
    if not isinstance(value, (list, tuple)):
        return []
    return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _rows_from(data: Mapping[str, Any], names: Sequence[str]) -> List[Dict[str, Any]]:
    for name in names:
        values = _rows(data.get(name))
        if values:
            return values
    return []


_LINEAR_CONTEXT_FRAGMENT_TYPES = frozenset(
    {"conversation_opener", "acknowledgement", "reaction", "context", "media"}
)
_LINEAR_MEDIA_TYPES = frozenset(
    {"image", "video", "audio", "voice", "file", "sticker", "emoji", "system", "location", "media"}
)


def _linear_row_text(row: Mapping[str, Any]) -> str:
    # These are public/redacted projections only.  Never consult raw/private
    # keys while assigning a packet role.
    for key in ("text_redacted", "fragment_text_redacted", "text", "content", "message_text"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _linear_row_message_type(row: Mapping[str, Any]) -> str:
    return str(row.get("message_type") or row.get("type") or "text")


def _linear_row_role(row: Mapping[str, Any]) -> str:
    raw = row.get("dialogue_role", row.get("message_role", row.get("role", "")))
    role = str(raw or "").strip().casefold()
    if role in {"opener", "conversation opener", "conversation_opener"}:
        return "conversation_opener"
    if role in {"context", "context-only", "context_only"}:
        return "context_only"
    if role == "substantive":
        return role
    roles = row.get("roles")
    if isinstance(roles, (list, tuple, set, frozenset)):
        normalized = {str(value).strip().casefold() for value in roles}
        if normalized & {"context", "context-only", "context_only", "adjacent", "conversation_opener"}:
            return "context_only"
    return ""


def _linear_row_is_context_only(row: Mapping[str, Any]) -> bool:
    """Classify one source row for primary/context layering.

    The source may be body-free.  In that case a missing text field and an
    unknown role are deliberately retained as primary candidates; role
    inference must never become a lossy filter.  A known social phrase or
    typed media placeholder is the only lexical fallback.
    """

    text = _linear_row_text(row)
    message_type = _linear_row_message_type(row)
    if is_context_only_text(text, message_type=message_type):
        return True
    role = _linear_row_role(row)
    fragment_type = str(row.get("fragment_type") or row.get("kind") or "").strip().casefold()
    if fragment_type in _LINEAR_CONTEXT_FRAGMENT_TYPES:
        # Keep a mixed social turn substantive even if an upstream adapter
        # supplied an overly broad opener/ack label.
        if fragment_type != "media" and text and has_context_prefix(text):
            return False
        if fragment_type == "media" and text and not is_context_only_text(text, message_type=message_type):
            return str(message_type).casefold() in _LINEAR_MEDIA_TYPES
        return True
    if role in {"conversation_opener", "context_only"} or bool(row.get("is_opener")):
        if text and has_context_prefix(text):
            return False
        return True
    if bool(row.get("is_silent")) and str(message_type).casefold() in _LINEAR_MEDIA_TYPES:
        return True
    return False


def _linear_normalise_row(row: Mapping[str, Any], *, context_only: bool) -> Dict[str, Any]:
    value = deepcopy(dict(row))
    if context_only:
        value["role"] = "context_only"
        # Keep a useful typed marker while avoiding a stale opener label in
        # the canonical layer row.  Body-free rows simply gain role metadata.
        fragment_type = str(value.get("fragment_type") or value.get("kind") or "").casefold()
        if fragment_type in {"", "unknown", "statement"}:
            value["fragment_type"] = "media" if str(_linear_row_message_type(value)).casefold() in _LINEAR_MEDIA_TYPES else "acknowledgement"
        for key in ("dialogue_role", "message_role"):
            if key in value:
                value[key] = "context_only"
        if "is_opener" in value:
            value["is_opener"] = False
    else:
        role = _linear_row_role(value)
        if role in {"conversation_opener", "context_only"}:
            value["role"] = "substantive"
            for key in ("dialogue_role", "message_role"):
                if key in value:
                    value[key] = "substantive"
            if value.get("fragment_type") in {"conversation_opener", "acknowledgement", "reaction", "context"}:
                value["fragment_type"] = "question" if "?" in _linear_row_text(value) or "？" in _linear_row_text(value) else "statement"
            if "is_opener" in value:
                value["is_opener"] = False
    return value


def _linear_row_key(row: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(row.get("message_id") or row.get("message_handle") or ""),
        str(row.get("fragment_id") or ""),
        str(row.get("span") or row.get("span_start") or ""),
    )


def _scope_parts(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(value, Mapping):
        return (
            str(value.get("account_id", value.get("account"))) if value.get("account_id", value.get("account")) not in (None, "") else None,
            str(value.get("chat_id", value.get("chat"))) if value.get("chat_id", value.get("chat")) not in (None, "") else None,
        )
    if isinstance(value, str) and value:
        # Opaque handles use ``ACCOUNT/CHAT|kind|id`` as a transport key.
        # The slash is not a scope declaration in that form: treating the
        # suffix as a chat id can turn an ordinary handle into a fabricated
        # cross-chat violation.  Scope strings are deliberately limited to
        # the two-part forms emitted by this module.
        if "|" in value:
            return (None, None)
        for separator in ("/", "::"):
            if separator in value:
                left, right = value.split(separator, 1)
                if left and right and not any(marker in right for marker in ("/", "::", "|")):
                    return (left, right)
    return (None, None)


def _scope_of(data: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> Tuple[str, str, str]:
    account = _first(data, "account_id", "account")
    chat = _first(data, "chat_id", "chat")
    scope_account, scope_chat = _scope_parts(data.get("scope"))
    account = str(account or scope_account or "unknown")
    chat = str(chat or scope_chat or "unknown")
    observed = set()
    for row in rows:
        row_account = _first(row, "account_id", "account")
        row_chat = _first(row, "chat_id", "chat")
        nested_account, nested_chat = _scope_parts(row.get("scope"))
        row_account = row_account or nested_account
        row_chat = row_chat or nested_chat
        if row_account not in (None, "", "unknown") or row_chat not in (None, "", "unknown"):
            observed.add((str(row_account or account), str(row_chat or chat)))
    # A root may carry only an opaque transport handle in ``scope``.  That
    # handle is intentionally not parsed above; when the layer rows provide a
    # single explicit scope, use that scope as the safe root identity instead
    # of comparing it to the placeholder ``unknown``.  Conflicting row scopes
    # still fail closed below.
    if account == "unknown" and chat == "unknown" and observed:
        observed_accounts = {item[0] for item in observed}
        observed_chats = {item[1] for item in observed}
        if len(observed_accounts) == 1 and len(observed_chats) == 1:
            account = next(iter(observed_accounts))
            chat = next(iter(observed_chats))
    elif account == "unknown" and observed:
        observed_accounts = {item[0] for item in observed}
        if len(observed_accounts) == 1:
            account = next(iter(observed_accounts))
    elif chat == "unknown" and observed:
        observed_chats = {item[1] for item in observed}
        if len(observed_chats) == 1:
            chat = next(iter(observed_chats))
    if any(item[0] != account for item in observed):
        raise LinearStagePacketError("cross_account_scope")
    if any(item[1] != chat for item in observed):
        raise LinearStagePacketError("cross_chat_scope")
    return account, chat, "%s/%s" % (account, chat)


def _scope_guard(row: Mapping[str, Any], scope: str) -> None:
    """Reject an explicitly scoped row that would cross the root boundary."""

    expected_account, expected_chat = (scope.split("/", 1) + ["unknown"])[:2]
    row_account, row_chat = _scope_parts(row.get("scope"))
    row_account = _first(row, "account_id", "account") or row_account
    row_chat = _first(row, "chat_id", "chat") or row_chat
    if row_account not in (None, "", "unknown", expected_account):
        raise LinearStagePacketError("cross_account_scope")
    if row_chat not in (None, "", "unknown", expected_chat):
        raise LinearStagePacketError("cross_chat_scope")
    # Relation rows often carry one scope per endpoint instead of a single
    # scope object.  Those endpoint scopes are just as authoritative.
    account_keys = ("left_account_id", "right_account_id", "question_account_id", "answer_account_id", "source_account_id")
    chat_keys = ("left_chat_id", "right_chat_id", "question_chat_id", "answer_chat_id", "source_chat_id")
    for key in account_keys:
        value = row.get(key)
        if value not in (None, "", "unknown", expected_account):
            raise LinearStagePacketError("cross_account_scope")
    for key in chat_keys:
        value = row.get(key)
        if value not in (None, "", "unknown", expected_chat):
            raise LinearStagePacketError("cross_chat_scope")


def _identifier(row: Mapping[str, Any], *names: str) -> Optional[str]:
    value = _first(row, *names)
    return str(value) if value is not None else None


def _message_id(row: Mapping[str, Any]) -> Optional[str]:
    return _identifier(row, "message_id", "source_message_id", "id")


def _candidate_id(row: Mapping[str, Any]) -> Optional[str]:
    return _identifier(row, "candidate_id", "context_relation_id", "relation_id", "thread_id", "id")


def _evidence_id(row: Mapping[str, Any]) -> Optional[str]:
    return _identifier(row, "evidence_id", "id", "ref_id", "source_ref_id")


def _message_ids(row: Mapping[str, Any]) -> Tuple[str, ...]:
    values: List[str] = []
    for key in (
        "message_id",
        "source_message_id",
        "left_message_id",
        "right_message_id",
        "left_id",
        "right_id",
        "question_id",
        "answer_id",
        "quoted_message_id",
    ):
        if row.get(key) not in (None, ""):
            values.append(str(row[key]))
    for key in ("message_ids", "source_message_ids", "member_message_ids"):
        if isinstance(row.get(key), (list, tuple)):
            values.extend(str(item) for item in row[key] if item not in (None, ""))
    return _unique(values)


def _evidence_ids(row: Mapping[str, Any]) -> Tuple[str, ...]:
    values: List[str] = []
    for key in ("evidence_id", "evidence_ref_id", "evidence_handle", "ref_id"):
        if row.get(key) not in (None, ""):
            values.append(str(row[key]))
    for key in ("evidence_ids", "evidence_ref_ids", "evidence_handle_refs"):
        if isinstance(row.get(key), (list, tuple)):
            values.extend(str(item) for item in row[key] if item not in (None, ""))
    for key in EVIDENCE_KEYS:
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    nested = _first(item, "evidence_id", "evidence_ref_id", "evidence_handle", "ref_id", "id")
                    if nested not in (None, ""):
                        values.append(str(nested))
                elif item not in (None, ""):
                    values.append(str(item))
    return _unique(values)


def _reasons(row: Mapping[str, Any]) -> Tuple[str, ...]:
    values: List[str] = []
    for key in ("candidate_reason", "candidate_reasons", "supporting_slot_codes", "reason_codes", "reasons"):
        child = row.get(key)
        if isinstance(child, str):
            values.append(child)
        elif isinstance(child, (list, tuple)):
            values.extend(str(item) for item in child)
    return _unique(values)


def _is_weak_only(row: Mapping[str, Any]) -> bool:
    reasons = tuple(str(item).casefold() for item in _reasons(row))
    if reasons and set(reasons) <= WEAK_REASON_KEYS:
        return True
    # Some upstream projections carry the guard as a boolean instead of a
    # reason code.  Treat that assertion as weak unless an explicit strong
    # relation reason is also present.
    weak_flag = any(bool(row.get(key)) for key in ("time_is_weak_only", "same_segment_is_weak_only", "weak_only", "time_only", "same_segment_only"))
    strong_flag = any(bool(row.get(key)) for key in ("strong_relation", "is_strong", "materialized_relation"))
    strong_reason = set(reasons) - WEAK_REASON_KEYS
    return weak_flag and not strong_flag and not strong_reason


def _short_key(value: Any) -> str:
    return stable_hash(value)[:24]


def _chunk(values: Sequence[str], size: int) -> List[List[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)] or [[]]


def _body_free(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, child in value.items():
            if str(key).casefold() in BODY_KEYS and child not in (None, "", [], (), {}):
                continue
            output[str(key)] = _body_free(child)
        return output
    if isinstance(value, (list, tuple)):
        return [_body_free(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return [_body_free(child) for child in sorted(value, key=str)]
    return deepcopy(value)


@dataclass(frozen=True)
class LinearCapacity:
    max_input_token_proxy: int = DEFAULT_MAX_INPUT_TOKEN_PROXY
    max_user_token_proxy: int = DEFAULT_MAX_USER_TOKEN_PROXY
    max_messages: int = DEFAULT_MAX_MESSAGES
    max_candidate_rows: int = DEFAULT_MAX_CANDIDATE_ROWS
    max_evidence_refs: int = DEFAULT_MAX_EVIDENCE_REFS

    def __post_init__(self) -> None:
        for name in ("max_input_token_proxy", "max_user_token_proxy", "max_messages", "max_candidate_rows", "max_evidence_refs"):
            if int(getattr(self, name)) < 1:
                raise ValueError("%s must be positive" % name)
        if self.max_user_token_proxy > self.max_input_token_proxy:
            raise ValueError("max_user_token_proxy cannot exceed max_input_token_proxy")

    def to_dict(self) -> Dict[str, int]:
        return {name: int(getattr(self, name)) for name in ("max_input_token_proxy", "max_user_token_proxy", "max_messages", "max_candidate_rows", "max_evidence_refs")}


@dataclass(frozen=True)
class LinearMaterialStats:
    input_token_proxy: int
    user_token_proxy: int
    canonical_chars: int
    system_prompt_chars: int
    message_count: int
    candidate_count: int
    evidence_count: int
    within_limits: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_token_proxy": self.input_token_proxy,
            "user_token_proxy": self.user_token_proxy,
            "canonical_chars": self.canonical_chars,
            "system_prompt_chars": self.system_prompt_chars,
            "message_count": self.message_count,
            "candidate_count": self.candidate_count,
            "candidate_row_count": self.candidate_count,
            "evidence_count": self.evidence_count,
            "evidence_ref_count": self.evidence_count,
            "within_limits": bool(self.within_limits),
        }


def _capacity(value: Any) -> LinearCapacity:
    if value is None:
        return LinearCapacity()
    if isinstance(value, LinearCapacity):
        return value
    if isinstance(value, Mapping):
        return LinearCapacity(
            max_input_token_proxy=int(value.get("max_input_token_proxy", DEFAULT_MAX_INPUT_TOKEN_PROXY)),
            max_user_token_proxy=int(value.get("max_user_token_proxy", value.get("internal_user_token_proxy", DEFAULT_MAX_USER_TOKEN_PROXY))),
            max_messages=int(value.get("max_messages", DEFAULT_MAX_MESSAGES)),
            max_candidate_rows=int(value.get("max_candidate_rows", value.get("max_candidates", DEFAULT_MAX_CANDIDATE_ROWS))),
            max_evidence_refs=int(value.get("max_evidence_refs", value.get("max_evidence", DEFAULT_MAX_EVIDENCE_REFS))),
        )
    raise TypeError("capacity must be LinearCapacity or mapping")


class LinearStagePacketStore:
    """One process-local, content-addressed linear page store.

    ``stage_a_system_prompt`` is only used while choosing page boundaries.  A
    caller may still override the prompt at materialization time; the default
    matches :func:`materialize_stage_a` and keeps the common build path exact.
    """

    def __init__(
        self,
        *,
        capacity: Any = None,
        cache: Optional[Mapping[str, Any]] = None,
        stage_a_system_prompt: Optional[str] = None,
        system_prompt_a: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self.capacity = _capacity(capacity)
        selected_prompt = stage_a_system_prompt
        if selected_prompt is None:
            selected_prompt = system_prompt_a
        if selected_prompt is None:
            selected_prompt = system_prompt
        self.stage_a_system_prompt = str(selected_prompt) if selected_prompt is not None else DEFAULT_SYSTEM_PROMPTS["A"]
        self.packet_version = LINEAR_STAGE_PACKET_VERSION
        self.message_table: Dict[str, Dict[str, Any]] = {}
        self.content_table: Dict[str, Dict[str, Any]] = {}
        self.candidate_table: Dict[str, Dict[str, Any]] = {}
        self.evidence_table: Dict[str, Dict[str, Any]] = {}
        self.root_table: Dict[str, Dict[str, Any]] = {}
        self.page_table: Dict[str, Dict[str, Any]] = {}
        self.open_snapshot_table: Dict[str, Dict[str, Any]] = {}
        self.cache: Dict[str, Dict[str, Dict[str, Any]]] = {
            "fixed": {},
            "dynamic": {},
            "content": {},
        }
        if isinstance(cache, Mapping):
            for namespace in self.cache:
                source = cache.get(namespace)
                if namespace == "content" and not isinstance(source, Mapping):
                    source = cache.get("content_cache")
                if isinstance(source, Mapping):
                    self.cache[namespace].update(deepcopy(dict(source)))
        self._message_order: List[str] = []
        self._candidate_order: List[str] = []
        self._evidence_order: List[str] = []
        self._content_order: List[str] = []

    @property
    def store(self) -> "LinearStagePacketStore":
        return self

    @property
    def messages(self) -> Mapping[str, Dict[str, Any]]:
        return self.message_table

    @property
    def content(self) -> Mapping[str, Dict[str, Any]]:
        return self.content_table

    @property
    def candidates(self) -> Mapping[str, Dict[str, Any]]:
        return self.candidate_table

    @property
    def evidence(self) -> Mapping[str, Dict[str, Any]]:
        return self.evidence_table

    @property
    def open_snapshots(self) -> Mapping[str, Dict[str, Any]]:
        return self.open_snapshot_table

    @property
    def roots(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self.root_table.values())

    @property
    def pages(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self.page_table.values())

    @property
    def packets(self) -> Tuple[Dict[str, Any], ...]:
        return self.pages

    @classmethod
    def from_packets(cls, packets: Any, **kwargs: Any) -> "LinearStagePacketStore":
        return build_linear_stage_packets(packets, store=cls(**kwargs))

    def _put_content(self, scope: str, body: Any, *, owner: str) -> str:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        digest = stable_hash({"scope": scope, "body": text})
        key = "%s|content|%s" % (scope, digest[:32])
        current = self.content_table.get(key)
        if current is not None and current.get("body") != text:
            key = "%s|content|%s" % (scope, digest)
        if key not in self.content_table:
            self.content_table[key] = {
                "content_handle": key,
                "content_id": key,
                "scope": scope,
                "body_hash": digest,
                "char_count": len(text),
                "body": text,
                "source_refs": [],
            }
            self._content_order.append(key)
        refs = self.content_table[key].setdefault("source_refs", [])
        source_ref = {"owner": str(owner)}
        if source_ref not in refs:
            refs.append(source_ref)
        return key

    def _encode(self, value: Any, *, scope: str, owner: str) -> Any:
        if isinstance(value, Mapping):
            output: Dict[str, Any] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                if key.casefold() in BODY_KEYS and child not in (None, "", [], (), {}):
                    output["%s_ref" % key] = self._put_content(scope, child, owner=owner)
                elif key.casefold() in EVIDENCE_KEYS and isinstance(child, (list, tuple)):
                    handles = [self._register_evidence(item, scope, owner=owner) for item in child if item not in (None, "")]
                    output["evidence_handle_refs"] = [item for item in handles if item]
                else:
                    output[key] = self._encode(child, scope=scope, owner=owner)
            return output
        if isinstance(value, (list, tuple)):
            return [self._encode(child, scope=scope, owner=owner) for child in value]
        if isinstance(value, (set, frozenset)):
            return [self._encode(child, scope=scope, owner=owner) for child in sorted(value, key=str)]
        return deepcopy(value)

    def _decode(self, value: Any, *, include_body: bool, seen: Optional[set] = None) -> Any:
        seen = seen or set()
        if isinstance(value, Mapping):
            output: Dict[str, Any] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                if key.endswith("_ref") and key[:-4].casefold() in BODY_KEYS and isinstance(child, str):
                    entry = self.content_table.get(child)
                    if include_body and entry is not None:
                        output[key[:-4]] = entry.get("body", "")
                    continue
                if key == "evidence_handle_refs" and isinstance(child, (list, tuple)):
                    if include_body:
                        output["evidence_refs"] = [self._decode(self.evidence_table[item], include_body=True, seen=seen) for item in child if item in self.evidence_table]
                    continue
                output[key] = self._decode(child, include_body=include_body, seen=seen)
            return output
        if isinstance(value, (list, tuple)):
            return [self._decode(child, include_body=include_body, seen=seen) for child in value]
        return deepcopy(value)

    def _key_for(self, table: Mapping[str, Any], scope: str, kind: str, identity: str, encoded: Any, order: List[str]) -> str:
        base = "%s|%s|%s" % (scope, kind, identity)
        key = base
        if key in table and stable_hash(table[key]) != stable_hash(encoded):
            key = "%s|%s" % (base, _short_key(encoded))
        if key not in table:
            order.append(key)
        return key

    def _register_evidence(self, raw: Mapping[str, Any], scope: str, *, owner: str = "") -> str:
        if not isinstance(raw, Mapping):
            raw = {"evidence_id": str(raw)}
        _scope_guard(raw, scope)
        identity = _evidence_id(raw) or "evidence_%s" % _short_key(_body_free(raw))
        encoded = self._encode(raw, scope=scope, owner=owner or identity)
        base = "%s|evidence|%s" % (scope, identity)
        key = base
        current = self.evidence_table.get(base)
        if current is not None:
            current_payload = deepcopy(current)
            for field in ("evidence_handle", "scope"):
                current_payload.pop(field, None)
            if stable_hash(current_payload) != stable_hash(encoded):
                key = "%s|%s" % (base, _short_key(encoded))
        if key not in self.evidence_table:
            self._evidence_order.append(key)
        if key not in self.evidence_table:
            encoded = deepcopy(dict(encoded))
            encoded.update({"evidence_handle": key, "evidence_id": identity, "scope": scope})
            self.evidence_table[key] = encoded
        else:
            # The same evidence id may be encountered through a second layer;
            # retain any newly discovered content handles without creating a
            # cartesian duplicate row.
            current = self.evidence_table[key]
            for field in ("content_handles", "source_refs"):
                if field in encoded:
                    current[field] = list(_unique(list(current.get(field, ())) + list(encoded[field] if isinstance(encoded[field], (list, tuple)) else (encoded[field],))))
        return key

    def _register_candidate(self, raw: Mapping[str, Any], scope: str, *, view: str = "candidate_rows") -> Optional[str]:
        if _is_weak_only(raw):
            return None
        _scope_guard(raw, scope)
        row_account, row_chat = _scope_parts(raw.get("scope"))
        row_account = _first(raw, "account_id", "account") or row_account
        row_chat = _first(raw, "chat_id", "chat") or row_chat
        if row_account not in (None, "", "unknown", scope.split("/", 1)[0]) or row_chat not in (None, "", "unknown", scope.split("/", 1)[1]):
            return None
        identity = _candidate_id(raw) or "candidate_%s" % _short_key(_body_free(raw))
        encoded = self._encode(raw, scope=scope, owner=identity)
        nested: List[str] = []
        for evidence_key in EVIDENCE_KEYS:
            values = raw.get(evidence_key)
            if not isinstance(values, (list, tuple)):
                continue
            for item in values:
                if isinstance(item, Mapping):
                    handle = self._register_evidence(item, scope, owner=identity)
                elif item not in (None, ""):
                    text = str(item)
                    handle = next(
                        (
                            key
                            for key, record in self.evidence_table.items()
                            if record.get("scope") == scope and (key == text or str(record.get("evidence_id")) == text)
                        ),
                        None,
                    )
                    if handle is None:
                        handle = self._register_evidence({"evidence_id": text}, scope, owner=identity)
                else:
                    handle = None
                if handle:
                    nested.append(handle)
        # Keep already encoded handles (for callers that supplied an internal
        # reference row) while replacing public evidence ids with table keys.
        nested.extend(
            str(item)
            for item in encoded.get("evidence_handle_refs", ())
            if item not in (None, "")
        )
        encoded = deepcopy(dict(encoded))
        # A candidate is keyed by its relation identity, not by the view in
        # which it happened to be discovered.  Evidence and view membership
        # are merged below so the same row never multiplies across views.
        key_payload = deepcopy(encoded)
        for field in ("candidate_handle", "candidate_id", "scope", "view_names", "evidence_handle_refs"):
            key_payload.pop(field, None)
        encoded["candidate_handle"] = ""
        encoded["candidate_id"] = identity
        encoded["scope"] = scope
        encoded["view_names"] = []
        encoded["evidence_handle_refs"] = list(_unique(nested))
        key = "%s|candidate|%s" % (scope, identity)
        current = self.candidate_table.get(key)
        if current is not None:
            existing_payload = deepcopy(current)
            for field in ("candidate_handle", "candidate_id", "scope", "view_names", "evidence_handle_refs"):
                existing_payload.pop(field, None)
            if stable_hash(existing_payload) != stable_hash(key_payload):
                key = "%s|candidate|%s|%s" % (scope, identity, _short_key(key_payload))
                current = self.candidate_table.get(key)
        if key not in self.candidate_table:
            encoded["view_names"] = [view]
            encoded["candidate_handle"] = key
            self._candidate_order.append(key)
            self.candidate_table[key] = encoded
        else:
            current = self.candidate_table[key]
            names = list(current.get("view_names", ()))
            if view not in names:
                names.append(view)
                current["view_names"] = names
            refs = list(current.get("evidence_handle_refs", ()))
            current["evidence_handle_refs"] = list(_unique(refs + nested))
        return key

    def _register_message(self, raw: Mapping[str, Any], scope: str, *, role: str, order: int) -> str:
        _scope_guard(raw, scope)
        identity = _message_id(raw) or "message_%s" % _short_key(_body_free(raw))
        encoded = self._encode(raw, scope=scope, owner=identity)
        key = "%s|message|%s" % (scope, identity)
        current = self.message_table.get(key)
        if current is None:
            current = {
                "message_handle": key,
                "message_id": identity,
                "scope": scope,
                "order": int(order),
                "roles": [],
                "authority_rows": [],
                "primary_rows": [],
                "adjacent_rows": [],
                "identity_row": encoded,
                "content_handles": [],
            }
            self.message_table[key] = current
            self._message_order.append(key)
        elif not self._content_handles(current.get("identity_row", {})) and self._content_handles(encoded):
            # A placeholder may have been registered from a relation/source
            # before its authoritative message row arrives.  Upgrade the
            # identity view while retaining every occurrence in layer rows.
            current["identity_row"] = encoded
        if role not in current["roles"]:
            current["roles"].append(role)
        target = "primary_rows" if role == "primary" else "adjacent_rows" if role == "adjacent" else "authority_rows"
        # Keep occurrence order losslessly.  The root layer index identifies
        # the occurrence when the same message id appears more than once.
        current[target].append(encoded)
        handles: List[str] = []
        def collect(item: Any) -> None:
            if isinstance(item, Mapping):
                for name, child in item.items():
                    if str(name).endswith("_ref") and isinstance(child, str) and child in self.content_table:
                        handles.append(child)
                    collect(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    collect(child)
        collect(encoded)
        current["content_handles"] = list(_unique(list(current.get("content_handles", ())) + handles))
        return key

    def _register_placeholder(self, message_id: str, scope: str, order: int) -> str:
        for handle, record in self.message_table.items():
            if record.get("scope") == scope and record.get("message_id") == str(message_id):
                return handle
        return self._register_message({"message_id": message_id, "account_id": scope.split("/", 1)[0], "chat_id": scope.split("/", 1)[1]}, scope, role="authority", order=order)

    def _root_rows(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        fixed = data.get("fixed_part") if isinstance(data.get("fixed_part"), Mapping) else {}
        dynamic = data.get("dynamic_part") if isinstance(data.get("dynamic_part"), Mapping) else {}
        primary = _rows_from(data, ("primary_fragments",)) or _rows_from(fixed, ("primary_fragments",)) or _rows_from(data, PRIMARY_KEYS)
        adjacent = _rows_from(data, ("adjacent_context",)) or _rows_from(dynamic, ("adjacent_context",)) or _rows_from(data, ADJACENT_KEYS)
        # Layer assignment is authoritative at root construction.  A stale
        # upstream primary label must not promote a pure acknowledgement,
        # greeting or media placeholder.  Keep the row losslessly by moving
        # it into adjacent context; mixed turns remain primary.
        normalized_primary: List[Dict[str, Any]] = []
        normalized_adjacent: List[Dict[str, Any]] = [
            _linear_normalise_row(row, context_only=True) if _linear_row_is_context_only(row) else row
            for row in adjacent
        ]
        adjacent_keys = {_linear_row_key(row) for row in normalized_adjacent}
        for row in primary:
            context_only = _linear_row_is_context_only(row)
            normalized = _linear_normalise_row(row, context_only=context_only)
            if context_only:
                key = _linear_row_key(normalized)
                if key not in adjacent_keys:
                    normalized_adjacent.append(normalized)
                    adjacent_keys.add(key)
            else:
                normalized_primary.append(normalized)
        primary = normalized_primary
        adjacent = normalized_adjacent
        facts = _rows_from(data, ("authoritative_facts",)) or _rows_from(fixed, FACT_KEYS) or _rows_from(data, FACT_KEYS)
        evidence = _rows_from(data, ("evidence_refs",)) or _rows_from(dynamic, ("evidence_refs",))
        sources = _rows_from(data, SOURCE_KEYS) or _rows_from(fixed, SOURCE_KEYS)
        candidates: List[Tuple[str, Dict[str, Any]]] = []
        for key in CANDIDATE_KEYS:
            for row in _rows(data.get(key)):
                candidates.append((key, row))
            for row in _rows(dynamic.get(key)):
                candidates.append((key, row))
        return {"primary": primary, "adjacent": adjacent, "facts": facts, "evidence": evidence, "sources": sources, "candidates": candidates}

    def _make_root(self, source: Any, index: int) -> Dict[str, Any]:
        data = _as_mapping(source, include_body=True)
        rows = self._root_rows(data)
        all_rows = rows["primary"] + rows["adjacent"] + rows["facts"] + rows["evidence"] + rows["sources"] + [row for _, row in rows["candidates"]]
        account, chat, scope = _scope_of(data, all_rows)
        source_id = _first(data, "packet_id", "context_packet_id", "root_id") or "root_%03d" % (index + 1)
        source_id = str(source_id)
        source_fingerprint = stable_hash({"source_id": source_id, "scope": scope, "source": data})
        for existing in self.root_table.values():
            if existing.get("source_fingerprint") == source_fingerprint:
                return existing
        existing_source = self.root_table.get(source_id)
        if existing_source is not None and existing_source.get("source_fingerprint") == source_fingerprint:
            return existing_source
        root_id = source_id
        template_data = {key: value for key, value in data.items() if key not in LAYER_KEYS and key not in {"primary_fragments", "adjacent_context", "authoritative_facts", "evidence_refs", "source_refs"}}
        template = self._encode(template_data, scope=scope, owner=root_id)
        message_handles: List[str] = []
        primary_rows: List[Dict[str, Any]] = []
        adjacent_rows: List[Dict[str, Any]] = []
        authority_handles: List[str] = []
        sequence = 0
        for row in rows["primary"]:
            handle = self._register_message(row, scope, role="primary", order=sequence)
            sequence += 1
            message_handles.append(handle)
            primary_rows.append(
                {
                    "message_handle": handle,
                    "message_id": _message_id(row) or self.message_table[handle]["message_id"],
                    "row_index": len(self.message_table[handle].get("primary_rows", ())) - 1,
                }
            )
        for row in rows["adjacent"]:
            handle = self._register_message(row, scope, role="adjacent", order=sequence)
            sequence += 1
            message_handles.append(handle)
            adjacent_rows.append(
                {
                    "message_handle": handle,
                    "message_id": _message_id(row) or self.message_table[handle]["message_id"],
                    "row_index": len(self.message_table[handle].get("adjacent_rows", ())) - 1,
                }
            )
        for row in rows["facts"]:
            handle = self._register_message(row, scope, role="authority", order=sequence)
            sequence += 1
            authority_handles.append(handle)
            message_handles.append(handle)
        # A source ref can be the only mention of a message.  Keep a minimal
        # central authority row so source/primary/adjacent links recover.
        for row in rows["sources"]:
            _scope_guard(row, scope)
            message_id = _message_id(row)
            if message_id:
                handle = self._register_placeholder(message_id, scope, sequence)
                sequence += 1
                message_handles.append(handle)
        message_handles = list(_unique(message_handles))
        candidate_handles: List[str] = []
        candidate_views: Dict[str, List[str]] = {}
        for view, row in rows["candidates"]:
            handle = self._register_candidate(row, scope, view=view)
            if not handle:
                continue
            candidate_handles.append(handle)
            candidate_views.setdefault(view, []).append(handle)
            for message_id in _message_ids(row):
                matching = [key for key, value in self.message_table.items() if value.get("message_id") == message_id and value.get("scope") == scope]
                if matching:
                    message_handles.extend(matching[:1])
                else:
                    message_handles.append(self._register_placeholder(message_id, scope, sequence))
                    sequence += 1
        candidate_handles = list(_unique(candidate_handles))
        evidence_handles: List[str] = []
        evidence_rows: List[Dict[str, Any]] = []
        for row in rows["evidence"]:
            handle = self._register_evidence(row, scope, owner=root_id)
            if handle:
                evidence_handles.append(handle)
                evidence_rows.append({"evidence_handle": handle, "evidence_id": _evidence_id(row) or handle})
        for handle in candidate_handles:
            refs = self.candidate_table.get(handle, {}).get("evidence_handle_refs", ())
            evidence_handles.extend(str(item) for item in refs)
        evidence_handles = list(_unique(evidence_handles))
        layer_rows = {
            "primary_fragments": primary_rows,
            "adjacent_context": adjacent_rows,
            "authoritative_facts": [
                {
                    "message_handle": item,
                    "row_index": len(self.message_table[item].get("authority_rows", ())) - 1,
                }
                for item in authority_handles
            ],
            "evidence_refs": evidence_rows,
            "source_refs": [self._encode(row, scope=scope, owner=root_id) for row in rows["sources"]],
            "candidate_views": {key: list(_unique(value)) for key, value in candidate_views.items()},
        }
        topic_map = self._make_topics(data, scope, message_handles, candidate_handles, evidence_handles)
        content_handles = []
        for handle in message_handles:
            content_handles.extend(self.message_table.get(handle, {}).get("content_handles", ()))
        for handle in candidate_handles + evidence_handles:
            record = (self.candidate_table if handle in self.candidate_table else self.evidence_table).get(handle, {})
            content_handles.extend(self._content_handles(record))
        content_handles = list(_unique(content_handles))
        fixed_part = data.get("fixed_part") if isinstance(data.get("fixed_part"), Mapping) else {}
        dynamic_part = data.get("dynamic_part") if isinstance(data.get("dynamic_part"), Mapping) else {}
        fixed_hash = stable_hash(
            {
                "scope": scope,
                "message_handles": message_handles,
                "authority_handles": authority_handles,
                "primary_rows": primary_rows,
                "fixed_part": self._encode(fixed_part, scope=scope, owner=root_id),
            }
        )
        dynamic_hash = stable_hash(
            {
                "scope": scope,
                "candidate_handles": candidate_handles,
                "evidence_handles": evidence_handles,
                "adjacent_rows": adjacent_rows,
                "topics": topic_map,
                "dynamic_part": self._encode(dynamic_part, scope=scope, owner=root_id),
            }
        )
        content_hash = stable_hash([self.content_table[item] for item in content_handles if item in self.content_table])
        packet_hash = stable_hash({"version": LINEAR_STAGE_PACKET_VERSION, "fixed_hash": fixed_hash, "dynamic_hash": dynamic_hash, "content_hash": content_hash, "template": template})
        existing = self.root_table.get(root_id)
        if existing is not None:
            # A same-id but changed source remains addressable; preserve the
            # original root and derive a deterministic sibling id.
            root_id = "%s|%s" % (root_id, _short_key({"scope": scope, "fingerprint": source_fingerprint}))
        root = {
            "root_id": root_id,
            "packet_id": root_id,
            "source_packet_id": str(_first(data, "packet_id", "context_packet_id") or root_id),
            "source_fingerprint": source_fingerprint,
            "scope": {"account_id": account, "chat_id": chat},
            "scope_key": scope,
            "source_template": template,
            "message_handles": list(_unique(message_handles)),
            "primary_message_handles": [item["message_handle"] for item in primary_rows],
            "adjacent_message_handles": [item["message_handle"] for item in adjacent_rows],
            "authority_message_handles": authority_handles,
            "candidate_handles": candidate_handles,
            "candidate_link_refs": candidate_handles,
            "evidence_handles": evidence_handles,
            "source_rows": layer_rows["source_refs"],
            "layer_rows": layer_rows,
            "candidate_views": layer_rows["candidate_views"],
            "topics": topic_map,
            "page_refs": [],
            "fixed_hash": fixed_hash,
            "dynamic_hash": dynamic_hash,
            "content_hash": content_hash,
            "packet_hash": packet_hash,
            "cache_key": stable_hash({"fixed": fixed_hash, "dynamic": dynamic_hash, "content": content_hash}),
            "status": "open",
        }
        self.root_table[root_id] = root
        self.cache["fixed"][fixed_hash] = {"root_id": root_id, "scope": scope, "message_handles": list(message_handles), "authority_message_handles": authority_handles}
        self.cache["dynamic"][dynamic_hash] = {"root_id": root_id, "candidate_handles": candidate_handles, "evidence_handles": evidence_handles, "topics": topic_map}
        self.cache["content"][content_hash] = {"root_id": root_id, "content_handles": content_handles}
        return root

    def _content_handles(self, value: Any) -> List[str]:
        output: List[str] = []
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).endswith("_ref") and isinstance(child, str) and child in self.content_table:
                    output.append(child)
                output.extend(self._content_handles(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                output.extend(self._content_handles(child))
        return output

    def _make_topics(self, data: Mapping[str, Any], scope: str, message_handles: Sequence[str], candidate_handles: Sequence[str], evidence_handles: Sequence[str]) -> Dict[str, Dict[str, List[str]]]:
        topics: Dict[str, Dict[str, List[str]]] = {}
        raw_topics = data.get("topics", data.get("topic_map", ()))
        if isinstance(raw_topics, Mapping):
            raw_topics = [dict(value, topic_id=key) if isinstance(value, Mapping) else {"topic_id": key} for key, value in raw_topics.items()]
        for row in _rows(raw_topics):
            topic_id = _first(row, "topic_id", "id")
            if topic_id is None:
                continue
            mids = _message_ids(row)
            eids = _evidence_ids(row)
            topics[str(topic_id)] = {"message_handles": [], "candidate_handles": [], "evidence_handles": []}
            message_values: List[str] = list(mids)
            for key in ("message_handles", "message_ids", "context_ids", "context_message_ids"):
                value = row.get(key)
                if isinstance(value, (list, tuple)):
                    message_values.extend(str(item) for item in value if item not in (None, ""))
            for value in message_values:
                for handle in message_handles:
                    if value == handle or self.message_table.get(handle, {}).get("message_id") == value:
                        topics[str(topic_id)]["message_handles"].append(handle)
            candidate_values: List[str] = []
            for key in ("candidate_handles", "candidate_ids", "candidate_link_refs", "candidates"):
                value = row.get(key)
                if isinstance(value, (list, tuple)):
                    candidate_values.extend(
                        str(item.get("candidate_handle", item.get("candidate_id", item.get("id", ""))))
                        if isinstance(item, Mapping)
                        else str(item)
                        for item in value
                        if item not in (None, "")
                    )
            for value in candidate_values:
                for handle in candidate_handles:
                    record = self.candidate_table.get(handle, {})
                    if value == handle or record.get("candidate_id") == value:
                        topics[str(topic_id)]["candidate_handles"].append(handle)
            evidence_values: List[str] = list(eids)
            for key in ("evidence_handles", "evidence_ids"):
                value = row.get(key)
                if isinstance(value, (list, tuple)):
                    evidence_values.extend(str(item) for item in value if item not in (None, ""))
            for value in evidence_values:
                for handle in evidence_handles:
                    record = self.evidence_table.get(handle, {})
                    if value == handle or record.get("evidence_id") == value:
                        topics[str(topic_id)]["evidence_handles"].append(handle)
        for handle in candidate_handles:
            row = self.candidate_table.get(handle, {})
            topic_values = []
            for key in ("topic_id", "topic", "thread_id"):
                if row.get(key) not in (None, ""):
                    topic_values.append(str(row[key]))
            for key in ("topic_ids", "thread_ids"):
                if isinstance(row.get(key), (list, tuple)):
                    topic_values.extend(str(item) for item in row[key])
            for topic_id in topic_values:
                target = topics.setdefault(topic_id, {"message_handles": [], "candidate_handles": [], "evidence_handles": []})
                target["candidate_handles"].append(handle)
                for mid in _message_ids(row):
                    for message_handle in message_handles:
                        if self.message_table.get(message_handle, {}).get("message_id") == mid:
                            target["message_handles"].append(message_handle)
                target["evidence_handles"].extend(str(item) for item in row.get("evidence_handle_refs", ()))
        if not topics:
            topics["root"] = {"message_handles": list(message_handles), "candidate_handles": list(candidate_handles), "evidence_handles": list(evidence_handles)}
        for target in topics.values():
            for key in target:
                target[key] = list(_unique(target[key]))
        return topics

    def _make_pages(self, root: Dict[str, Any], *, system_prompt: Optional[str] = None) -> None:
        """Create count-bounded pages and split only when Stage A tokens require it.

        Each stream is chunked independently and then optionally subdivided in
        ordinal order.  A subdivision never joins a message to every
        candidate/evidence row: each handle appears once in its own ordered
        stream, preserving the linear (max-of-streams) page shape.
        """

        prompt = self.stage_a_system_prompt if system_prompt is None else str(system_prompt)
        # Split the three streams in one pass.  Keeping independent count
        # chunks as hard subproblems can strand the tail of one stream on an
        # extra page (for example 25 messages + 65 candidates + 65 evidence),
        # even though the tail fits on the preceding page.  A single global
        # pass still enforces each stream's count cap and remains linear.
        all_streams = (
            list(root["message_handles"]),
            list(root["candidate_handles"]),
            list(root["evidence_handles"]),
        )
        if _stage_a_segment_fits(self, root, ([], [], []), prompt):
            segments = _split_stage_a_segment(self, root, all_streams, prompt)
        else:
            # If the fixed envelope cannot fit even with zero handles, no
            # subdivision can become complete.  Keep the ordinary count page
            # so a pending snapshot contains the full retryable root and the
            # caller sees one necessary pending page rather than a misleading
            # run of tiny pending fragments.
            message_chunks = _chunk(root["message_handles"], self.capacity.max_messages)
            candidate_chunks = _chunk(root["candidate_handles"], self.capacity.max_candidate_rows)
            evidence_chunks = _chunk(root["evidence_handles"], self.capacity.max_evidence_refs)
            count_page_count = max(len(message_chunks), len(candidate_chunks), len(evidence_chunks), 1)
            segments = [
                (
                    message_chunks[index] if index < len(message_chunks) else [],
                    candidate_chunks[index] if index < len(candidate_chunks) else [],
                    evidence_chunks[index] if index < len(evidence_chunks) else [],
                )
                for index in range(count_page_count)
            ]

        for index, (messages, candidates, evidence) in enumerate(segments):
            page_id = "%s|page|%04d" % (root["root_id"], index + 1)
            page = {
                "page_id": page_id,
                "root_id": root["root_id"],
                "ordinal": index + 1,
                "scope": deepcopy(root["scope"]),
                "message_handles": list(messages),
                "candidate_handles": list(candidates),
                # The page ledger intentionally stores one canonical
                # candidate stream.  ``candidate_link_refs`` is retained as
                # the stable page-level alias used by older callers.
                "candidate_link_refs": list(candidates),
                "evidence_handles": list(evidence),
                "status": "open",
            }
            page["page_hash"] = stable_hash({key: page[key] for key in ("root_id", "ordinal", "message_handles", "candidate_handles", "evidence_handles")})
            self.page_table[page_id] = page
            root["page_refs"].append(page_id)

        # Keep an explicit, inspectable upper bound for callers that need to
        # distinguish ordinary count paging from token-driven subdivisions.
        # This is metadata only and therefore does not alter packet/cache
        # hashes or any body-bearing table.
        root["page_count_bound"] = _stage_a_page_count_bound(self, root, prompt)

    def add_packets(
        self,
        packets: Any,
        *,
        stage_a_system_prompt: Optional[str] = None,
        system_prompt_a: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> "LinearStagePacketStore":
        selected_prompt = stage_a_system_prompt
        if selected_prompt is None:
            selected_prompt = system_prompt_a
        if selected_prompt is None:
            selected_prompt = system_prompt
        if selected_prompt is not None:
            self.stage_a_system_prompt = str(selected_prompt)
        if isinstance(packets, Mapping) or hasattr(packets, "to_dict"):
            packets = [packets]
        if isinstance(packets, Iterable) and not isinstance(packets, (str, bytes, list, tuple)):
            packets = list(packets)
        if not isinstance(packets, (list, tuple)):
            raise LinearStagePacketError("packets_shape")
        for index, source in enumerate(packets):
            root = self._make_root(source, index)
            if not root.get("page_refs"):
                self._make_pages(root, system_prompt=self.stage_a_system_prompt)
        return self

    def get_root(self, ref: Any) -> Dict[str, Any]:
        if isinstance(ref, Mapping):
            value = ref.get("root_id", ref.get("packet_id", ref.get("page_id")))
            if value is not None:
                ref = value
        key = str(ref)
        if key in self.root_table:
            return self.root_table[key]
        page = self.page_table.get(key)
        if page is not None and page.get("root_id") in self.root_table:
            return self.root_table[page["root_id"]]
        raise LinearStagePacketError("root_not_found")

    def get_page(self, ref: Any) -> Dict[str, Any]:
        if isinstance(ref, Mapping):
            value = ref.get("page_id")
            if value is not None:
                ref = value
        key = str(ref)
        if key in self.page_table:
            return self.page_table[key]
        root = self.get_root(ref)
        refs = root.get("page_refs", ())
        if len(refs) == 1 and refs[0] in self.page_table:
            return self.page_table[refs[0]]
        raise LinearStagePacketError("page_required_for_multi_page_root")

    def materialize_stage_a(self, ref: Any, **kwargs: Any) -> Dict[str, Any]:
        return _materialize_a(self, ref, **kwargs)

    def materialize_stage_b(self, ref: Any, **kwargs: Any) -> Dict[str, Any]:
        return _materialize_b(self, ref, **kwargs)

    def materialize_stage_c(self, stage_a: Any, stage_b: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return _materialize_c(self, stage_a, stage_b, **kwargs)

    def recover_linear_packet(self, ref: Any, **kwargs: Any) -> Dict[str, Any]:
        return _recover(self, ref, **kwargs)

    def to_dict(self, *, include_body: bool = False, include_content: Optional[bool] = None) -> Dict[str, Any]:
        if include_content is not None:
            include_body = bool(include_content)
        content: Dict[str, Any] = {}
        for key in self._content_order:
            row = deepcopy(self.content_table[key])
            if not include_body:
                row.pop("body", None)
            content[key] = row
        return {
            "schema_version": LINEAR_STORE_SCHEMA_VERSION,
            "packet_version": LINEAR_STAGE_PACKET_VERSION,
            "pipeline_version": LINEAR_PIPELINE_VERSION,
            "capacity": self.capacity.to_dict(),
            "root_count": len(self.root_table),
            "page_count": len(self.page_table),
            "roots": [_body_free(self.root_table[key]) if not include_body else deepcopy(self.root_table[key]) for key in self.root_table],
            "pages": [_body_free(self.page_table[key]) for key in self.page_table],
            "messages": [_body_free(self.message_table[key]) for key in self._message_order],
            "candidates": [_body_free(self.candidate_table[key]) for key in self._candidate_order],
            "evidence": [_body_free(self.evidence_table[key]) for key in self._evidence_order],
            "content_table": content,
            "open_snapshots": {
                key: _body_free(value) for key, value in self.open_snapshot_table.items()
            },
            "cache": {
                "fixed": deepcopy(self.cache["fixed"]),
                "dynamic": deepcopy(self.cache["dynamic"]),
                "content_cache": deepcopy(self.cache["content"]),
            },
        }


def _record_body_free(record: Mapping[str, Any]) -> Dict[str, Any]:
    return _body_free(record)


def _message_public(store: LinearStagePacketStore, handle: str, *, include_body: bool, minimal: bool = False) -> Dict[str, Any]:
    record = store.message_table.get(handle)
    if record is None:
        raise LinearStagePacketError("message_handle_missing")
    if minimal:
        row: Dict[str, Any] = {
            "message_handle": handle,
            "message_id": record.get("message_id", ""),
        }
        if record.get("roles"):
            row["roles"] = list(record["roles"])
        if include_body:
            body_row = store._decode(record.get("identity_row", {}), include_body=True)
            for key in ("body", "content", "text", "text_redacted", "message_text", "raw_text"):
                if key in body_row:
                    row["body"] = body_row[key]
                    break
        return row
    row = {
        "message_handle": handle,
        "message_id": record.get("message_id", ""),
        "scope": record.get("scope", ""),
        "roles": list(record.get("roles", ())),
        "authority": _record_body_free(record.get("authority_rows", [record.get("identity_row", {})])[0] if record.get("authority_rows") else record.get("identity_row", {})),
        "content_handles": list(record.get("content_handles", ())),
    }
    if include_body:
        body_row = store._decode(record.get("identity_row", {}), include_body=True)
        for key in ("body", "content", "text", "text_redacted", "message_text", "raw_text"):
            if key in body_row:
                row["body"] = body_row[key]
                break
    return row


def _candidate_public(store: LinearStagePacketStore, handle: str, *, minimal: bool = False) -> Dict[str, Any]:
    record = store.candidate_table.get(handle)
    if record is None:
        raise LinearStagePacketError("candidate_handle_missing")
    if minimal:
        return {
            "candidate_handle": handle,
            "candidate_id": record.get("candidate_id", ""),
            "message_ids": list(_message_ids(record)),
            "evidence_handles": list(record.get("evidence_handle_refs", ())),
        }
    return {
        "candidate_handle": handle,
        "candidate_id": record.get("candidate_id", ""),
        "left_message_id": record.get("left_message_id"),
        "right_message_id": record.get("right_message_id"),
        "message_ids": list(_message_ids(record)),
        "relation_label": record.get("relation_label", record.get("relation")),
        "relation_subtype": record.get("relation_subtype"),
        "candidate_reason": list(_reasons(record)),
        "evidence_handles": list(record.get("evidence_handle_refs", ())),
        "view_names": list(record.get("view_names", ())),
    }


def _evidence_public(store: LinearStagePacketStore, handle: str, *, include_body: bool, minimal: bool = False) -> Dict[str, Any]:
    record = store.evidence_table.get(handle)
    if record is None:
        raise LinearStagePacketError("evidence_handle_missing")
    if minimal:
        row: Dict[str, Any] = {
            "evidence_handle": handle,
            "evidence_id": record.get("evidence_id", ""),
        }
        for key in ("message_id", "type", "kind", "span"):
            if key in record and key != "span":
                row[key] = deepcopy(record[key])
            elif key == "span" and key in record:
                row[key] = deepcopy(record[key])
        if include_body:
            decoded = store._decode(record, include_body=True)
            for key in ("body", "content", "text", "quote", "evidence_text", "raw_text"):
                if key in decoded:
                    row[key] = decoded[key]
                    break
        return row
    row = _record_body_free(record)
    row["evidence_handle"] = handle
    if include_body:
        return store._decode(record, include_body=True)
    return row


def _stats(user: Mapping[str, Any], system_prompt: str, store: LinearStagePacketStore, *, message_count: int, candidate_count: int, evidence_count: int) -> LinearMaterialStats:
    chars = len(canonical_json(user))
    user_proxy = (chars + 3) // 4
    total_proxy = estimate_token_proxy(user, system_prompt)
    limits = store.capacity
    within = total_proxy <= limits.max_input_token_proxy and user_proxy <= limits.max_user_token_proxy and message_count <= limits.max_messages and candidate_count <= limits.max_candidate_rows and evidence_count <= limits.max_evidence_refs
    return LinearMaterialStats(total_proxy, user_proxy, chars, len(str(system_prompt)), message_count, candidate_count, evidence_count, within)


def _finish(
    user: Dict[str, Any],
    system_prompt: str,
    store: LinearStagePacketStore,
    *,
    message_count: int,
    candidate_count: int,
    evidence_count: int,
    allow_over_capacity: bool = False,
    raise_on_capacity: bool = False,
    snapshot_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach final accounting and preserve an over-limit replay point.

    The accounting deliberately includes the accounting fields themselves;
    otherwise a request could pass the check and exceed the limit after
    ``material_stats`` and ``limits`` are appended.  Capacity failures are
    represented as a pending envelope by default so callers can persist and
    retry the exact page.  ``raise_on_capacity`` is available to strict local
    callers that prefer the legacy exception behaviour.
    """

    initial = _stats(user, system_prompt, store, message_count=message_count, candidate_count=candidate_count, evidence_count=evidence_count)
    if not initial.within_limits and raise_on_capacity:
        raise LinearCapacityError(initial.to_dict(), store.capacity)
    output = deepcopy(user)
    output["limits"] = store.capacity.to_dict()
    output["status"] = "complete" if initial.within_limits else ("over_capacity" if allow_over_capacity else "pending")
    if not initial.within_limits:
        output["pending_reason"] = "capacity_exceeded"
        context = dict(snapshot_context or {})
        snapshot_id = "snapshot|%s" % stable_hash(
            {
                "stage": output.get("stage"),
                "root_id": output.get("root_id"),
                "page_id": output.get("page_id"),
                "message_handles": output.get("message_handles", ()),
                "candidate_handles": output.get("candidate_handles", ()),
                "evidence_handles": output.get("evidence_handles", ()),
                "stats": initial.to_dict(),
            }
        )
        snapshot = {
            "open_snapshot_id": snapshot_id,
            "status": "open",
            "stage": output.get("stage", ""),
            "root_id": output.get("root_id", ""),
            "page_id": output.get("page_id", ""),
            "scope": deepcopy(output.get("scope", {})),
            "source_refs": deepcopy(context.get("source_refs", ())),
            "message_handles": list(output.get("message_handles", ())),
            "candidate_handles": list(output.get("candidate_handles", ())),
            "evidence_handles": list(output.get("evidence_handles", ())),
            "limits": store.capacity.to_dict(),
            "material_stats": initial.to_dict(),
            "reason_code": "capacity_exceeded",
        }
        # A pending page may be one subdivision of a larger root.  Keep the
        # continuation ledger beside the open snapshot so retrying the first
        # page never loses handles that live on later pages.
        if context.get("continuation_refs") is not None:
            snapshot["continuation_refs"] = deepcopy(context["continuation_refs"])
        if context.get("continuation_ref") is not None:
            snapshot["continuation_ref"] = deepcopy(context["continuation_ref"])
        store.open_snapshot_table[snapshot_id] = deepcopy(snapshot)
        output["open_snapshot_ref"] = snapshot_id
        output["open_snapshot"] = _body_free(snapshot)
    # ``material_stats`` describes the user payload, excluding this transport
    # bookkeeping.  This is the same payload that is sent alongside the
    # system prompt and keeps the proxy independently reproducible by callers.
    stats = _stats(user, system_prompt, store, message_count=message_count, candidate_count=candidate_count, evidence_count=evidence_count)
    output["material_stats"] = stats.to_dict()
    if not stats.within_limits and not initial.within_limits and output.get("open_snapshot_ref") in store.open_snapshot_table:
        store.open_snapshot_table[output["open_snapshot_ref"]]["material_stats"] = stats.to_dict()
    return output


def _resolve_page(store: LinearStagePacketStore, ref: Any, *, allow_root: bool = False) -> Dict[str, Any]:
    if isinstance(ref, Mapping) and ref.get("page_id") in store.page_table:
        return store.page_table[str(ref["page_id"])]
    if str(ref) in store.page_table:
        return store.page_table[str(ref)]
    root = store.get_root(ref)
    if allow_root and root.get("page_refs"):
        return store.page_table[root["page_refs"][0]]
    return store.get_page(ref)


def _stage_a_user_payload(
    store: LinearStagePacketStore,
    page: Mapping[str, Any],
    *,
    root: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the exact body-free Stage A user envelope used for accounting.

    Keeping page construction and materialization on this one path is
    important: token-aware boundaries must be based on the canonical payload
    that is actually sent, including the compact topic index.
    """

    if root is None:
        root = store.root_table.get(str(page.get("root_id")), {})
    messages = [
        _message_public(store, str(handle), include_body=False, minimal=True)
        for handle in page.get("message_handles", ())
    ]
    candidates = [str(handle) for handle in page.get("candidate_handles", ())]
    return {
        "schema_version": LINEAR_STAGE_PACKET_VERSION,
        "stage": "A",
        "root_id": str(page.get("root_id", "")),
        "page_id": str(page.get("page_id", "")),
        "scope": deepcopy(page.get("scope", {})),
        "message_handles": [str(handle) for handle in page.get("message_handles", ())],
        "messages": messages,
        # Candidate handles are the single canonical expression in Stage A.
        # Older Stage C readers accept both scalar handles and the previous
        # mapping-shaped refs, so scalar refs retain compatibility without
        # repeating every handle and candidate id.
        "candidate_link_refs": candidates,
        "topic_map": _page_topic_map(store, root, page),
    }


def _temporary_stage_a_page(
    root: Mapping[str, Any],
    ordinal: int,
    streams: Tuple[Sequence[str], Sequence[str], Sequence[str]],
) -> Dict[str, Any]:
    """Return the small page view needed while testing a candidate split."""

    messages, candidates, evidence = streams
    return {
        "page_id": "%s|page|%04d" % (root.get("root_id", ""), ordinal),
        "root_id": root.get("root_id", ""),
        "ordinal": int(ordinal),
        "scope": deepcopy(root.get("scope", {})),
        "message_handles": list(messages),
        "candidate_handles": list(candidates),
        "candidate_link_refs": list(candidates),
        "evidence_handles": list(evidence),
        "status": "open",
    }


def _stage_a_segment_fits(
    store: LinearStagePacketStore,
    root: Mapping[str, Any],
    streams: Tuple[Sequence[str], Sequence[str], Sequence[str]],
    system_prompt: str,
    *,
    ordinal: int = 1,
) -> bool:
    """Check token and per-stream limits for one prospective page."""

    page = _temporary_stage_a_page(root, ordinal, streams)
    user = _stage_a_user_payload(store, page, root=root)
    messages, candidates, evidence = streams
    stats = _stats(
        user,
        system_prompt,
        store,
        message_count=len(messages),
        candidate_count=len(candidates),
        evidence_count=0,
    )
    return bool(
        stats.within_limits
        and len(messages) <= store.capacity.max_messages
        and len(candidates) <= store.capacity.max_candidate_rows
        and len(evidence) <= store.capacity.max_evidence_refs
    )


def _split_stage_a_segment(
    store: LinearStagePacketStore,
    root: Mapping[str, Any],
    segment: Tuple[Sequence[str], Sequence[str], Sequence[str]],
    system_prompt: str,
) -> List[Tuple[List[str], List[str], List[str]]]:
    """Subdivide one count page into token-fitting contiguous stream pages."""

    streams = tuple(list(values) for values in segment)
    if _stage_a_segment_fits(store, root, streams, system_prompt):
        return [streams]  # type: ignore[list-item]

    cursors = [0, 0, 0]
    output: List[Tuple[List[str], List[str], List[str]]] = []
    limits = (
        store.capacity.max_messages,
        store.capacity.max_candidate_rows,
        store.capacity.max_evidence_refs,
    )
    # The stream order is stable and intentionally not a relation join.  A
    # page may carry only one stream when that is what the token budget allows;
    # the union of all pages is still exactly the three source streams.
    while any(cursors[index] < len(streams[index]) for index in range(3)):
        current: List[List[str]] = [[], [], []]
        forced = False
        for stream_index in range(3):
            while cursors[stream_index] < len(streams[stream_index]):
                if len(current[stream_index]) >= limits[stream_index]:
                    break
                trial = [list(values) for values in current]
                trial[stream_index].append(streams[stream_index][cursors[stream_index]])
                trial_tuple = (trial[0], trial[1], trial[2])
                if _stage_a_segment_fits(
                    store,
                    root,
                    trial_tuple,
                    system_prompt,
                    ordinal=len(output) + 1,
                ):
                    current[stream_index].append(streams[stream_index][cursors[stream_index]])
                    cursors[stream_index] += 1
                    continue
                if not any(current):
                    # One opaque handle or the fixed envelope itself can be
                    # larger than the budget.  Do not truncate it; preserve
                    # it as an explicitly pending continuation point.
                    current[stream_index].append(streams[stream_index][cursors[stream_index]])
                    cursors[stream_index] += 1
                    forced = True
                break
            if forced:
                break
        if not any(current):
            # Defensive progress guarantee for an unusual limit/shape.  The
            # capacity dataclass rejects zero limits, but this also prevents a
            # future custom stream from looping forever.
            for stream_index in range(3):
                if cursors[stream_index] < len(streams[stream_index]):
                    current[stream_index].append(streams[stream_index][cursors[stream_index]])
                    cursors[stream_index] += 1
                    break
        output.append((current[0], current[1], current[2]))
    return output or [([], [], [])]


def _stage_a_page_count_bound(
    store: LinearStagePacketStore,
    root: Mapping[str, Any],
    system_prompt: str,
) -> Dict[str, Any]:
    """Describe the linear max(count-bound, token-bound) page upper bound."""

    counts = (
        len(root.get("message_handles", ())),
        len(root.get("candidate_handles", ())),
        len(root.get("evidence_handles", ())),
    )
    count_bound = max(
        1,
        math.ceil(counts[0] / store.capacity.max_messages),
        math.ceil(counts[1] / store.capacity.max_candidate_rows),
        math.ceil(counts[2] / store.capacity.max_evidence_refs),
    )
    full_page = _temporary_stage_a_page(
        root,
        1,
        (
            list(root.get("message_handles", ())),
            list(root.get("candidate_handles", ())),
            list(root.get("evidence_handles", ())),
        ),
    )
    full_user = _stage_a_user_payload(store, full_page, root=root)
    full_user_tokens = _user_token_proxy(full_user)
    empty_page = _temporary_stage_a_page(root, 1, ([], [], []))
    fixed_tokens = max(1, _user_token_proxy(_stage_a_user_payload(store, empty_page, root=root)))
    # Greedy packing can leave at most the next indivisible handle's worth of
    # capacity at a stream boundary.  Reserve that worst singleton increment
    # in the safety budget so the advertised token term remains an upper
    # bound even when one stream (usually candidates) is much wider than the
    # others.  No body is inspected here: the singleton payload is the same
    # body-free Stage A envelope used by materialization.
    max_item_increment = 1
    for stream_index, handles in enumerate(
        (
            root.get("message_handles", ()),
            root.get("candidate_handles", ()),
            root.get("evidence_handles", ()),
        )
    ):
        for handle in handles:
            singleton_streams: Tuple[List[str], List[str], List[str]] = ([], [], [])
            singleton_streams[stream_index].append(str(handle))
            singleton_page = _temporary_stage_a_page(root, 1, singleton_streams)
            singleton_tokens = _user_token_proxy(_stage_a_user_payload(store, singleton_page, root=root))
            max_item_increment = max(max_item_increment, singleton_tokens - fixed_tokens)
    safe_user_budget = max(1, store.capacity.max_user_token_proxy - fixed_tokens - max_item_increment)
    token_bound = max(1, math.ceil(full_user_tokens / safe_user_budget))
    expected = max(count_bound, token_bound)
    return {
        "count_page_bound": count_bound,
        "token_page_bound": token_bound,
        "expected_page_count": expected,
        "actual_page_count": len(root.get("page_refs", ())),
        "linear": len(root.get("page_refs", ())) <= expected,
        "material_user_token_proxy": full_user_tokens,
        "material_input_token_proxy": estimate_token_proxy(full_user, system_prompt),
        "safe_user_token_budget": safe_user_budget,
        "max_item_token_increment": max_item_increment,
        "counts": {
            "messages": counts[0],
            "candidates": counts[1],
            "evidence": counts[2],
        },
    }


def _page_topic_map(store: LinearStagePacketStore, root: Mapping[str, Any], page: Mapping[str, Any]) -> Dict[str, Dict[str, List[int]]]:
    """Return compact per-topic indexes into the page-level handle arrays."""

    message_index = {str(handle): index for index, handle in enumerate(page.get("message_handles", ()))}
    candidate_index = {str(handle): index for index, handle in enumerate(page.get("candidate_handles", ()))}
    evidence_index = {str(handle): index for index, handle in enumerate(page.get("evidence_handles", ()))}
    raw_topics = root.get("topics", {}) if isinstance(root.get("topics"), Mapping) else {}
    output: Dict[str, Dict[str, List[int]]] = {}
    for topic_id, raw in raw_topics.items():
        if not isinstance(raw, Mapping):
            continue
        output[str(topic_id)] = {
            "message_indices": [
                message_index[str(item)]
                for item in raw.get("message_handles", ())
                if str(item) in message_index
            ],
            "candidate_indices": [
                candidate_index[str(item)]
                for item in raw.get("candidate_handles", ())
                if str(item) in candidate_index
            ],
            "evidence_indices": [
                evidence_index[str(item)]
                for item in raw.get("evidence_handles", ())
                if str(item) in evidence_index
            ],
        }
    if not output:
        output["root"] = {
            "message_indices": list(range(len(message_index))),
            "candidate_indices": list(range(len(candidate_index))),
            "evidence_indices": list(range(len(evidence_index))),
        }
    return output


def _snapshot_source_refs(store: LinearStagePacketStore, root: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Expose source identity refs in a pending snapshot without bodies."""

    rows = root.get("source_rows", ())
    if not isinstance(rows, (list, tuple)):
        return []
    return [_body_free(dict(row)) for row in rows if isinstance(row, Mapping)]


def _snapshot_continuation_refs(store: LinearStagePacketStore, page: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a body-free, lossless retry index for a pending page."""

    root = store.root_table.get(str(page.get("root_id")), {})
    page_refs = [str(item) for item in root.get("page_refs", ())]
    page_id = str(page.get("page_id", ""))
    try:
        position = page_refs.index(page_id)
    except ValueError:
        position = 0
    return {
        "root_id": str(root.get("root_id", page.get("root_id", ""))),
        "page_refs": page_refs,
        "remaining_page_refs": page_refs[position:],
        "message_handles": [str(item) for item in root.get("message_handles", ())],
        "candidate_handles": [str(item) for item in root.get("candidate_handles", ())],
        "evidence_handles": [str(item) for item in root.get("evidence_handles", ())],
    }


def _scope_identity(value: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(value, Mapping):
        return None
    account = _first(value, "account_id", "account")
    chat = _first(value, "chat_id", "chat")
    nested_account, nested_chat = _scope_parts(value.get("scope"))
    account = account or nested_account
    chat = chat or nested_chat
    if account in (None, "") or chat in (None, ""):
        return None
    return str(account), str(chat)


def _materialize_a(
    store: LinearStagePacketStore,
    ref: Any,
    *,
    system_prompt: Optional[str] = None,
    allow_over_capacity: bool = False,
    raise_on_capacity: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    page = _resolve_page(store, ref, allow_root=True)
    root = store.root_table[page["root_id"]]
    prompt = DEFAULT_SYSTEM_PROMPTS["A"] if system_prompt is None else str(system_prompt)
    # Stage A only maps identifiers.  Relation/evidence detail remains in the
    # authoritative tables and is intentionally not repeated in this request.
    user = _stage_a_user_payload(store, page, root=root)
    return _finish(
        user,
        prompt,
        store,
        message_count=len(page["message_handles"]),
        candidate_count=len(page["candidate_handles"]),
        evidence_count=0,
        allow_over_capacity=allow_over_capacity,
        raise_on_capacity=raise_on_capacity,
        snapshot_context={
            "source_refs": _snapshot_source_refs(store, root),
            "continuation_refs": _snapshot_continuation_refs(store, page),
        },
    )


def _topic_handles(store: LinearStagePacketStore, root: Mapping[str, Any], topic_ids: Optional[Sequence[str]], page: Mapping[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    topics = root.get("topics", {}) if isinstance(root.get("topics"), Mapping) else {}
    selected = [str(item) for item in topic_ids] if topic_ids is not None else list(topics) or ["root"]
    page_messages = set(str(item) for item in page.get("message_handles", ()))
    page_candidates = set(str(item) for item in page.get("candidate_handles", ()))
    page_evidence = set(str(item) for item in page.get("evidence_handles", ()))
    messages: List[str] = []
    candidates: List[str] = []
    evidence: List[str] = []
    for topic_id in selected:
        target = topics.get(topic_id, {}) if isinstance(topics, Mapping) else {}
        messages.extend(str(item) for item in target.get("message_handles", ()) if str(item) in page_messages and str(item) in store.message_table)
        candidates.extend(str(item) for item in target.get("candidate_handles", ()) if str(item) in page_candidates and str(item) in store.candidate_table)
        evidence.extend(str(item) for item in target.get("evidence_handles", ()) if str(item) in page_evidence and str(item) in store.evidence_table)
    if not messages and topic_ids is None:
        messages.extend(str(item) for item in page.get("message_handles", ()) if str(item) in store.message_table)
    if not candidates and topic_ids is None:
        candidates.extend(str(item) for item in page.get("candidate_handles", ()) if str(item) in store.candidate_table)
    for handle in candidates:
        evidence.extend(str(item) for item in store.candidate_table.get(handle, {}).get("evidence_handle_refs", ()) if str(item) in page_evidence and str(item) in store.evidence_table)
    if not evidence and topic_ids is None:
        evidence.extend(str(item) for item in page.get("evidence_handles", ()) if str(item) in store.evidence_table)
    return list(_unique(messages)), list(_unique(candidates)), list(_unique(evidence))


def _materialize_b(
    store: LinearStagePacketStore,
    ref: Any,
    *,
    topic_ids: Optional[Sequence[str]] = None,
    include_body: bool = False,
    system_prompt: Optional[str] = None,
    allow_over_capacity: bool = False,
    raise_on_capacity: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    page = _resolve_page(store, ref, allow_root=True)
    root = store.root_table[page["root_id"]]
    prompt = DEFAULT_SYSTEM_PROMPTS["B"] if system_prompt is None else str(system_prompt)
    message_handles, candidate_handles, evidence_handles = _topic_handles(store, root, topic_ids, page)
    messages = [_message_public(store, handle, include_body=include_body, minimal=True) for handle in message_handles]
    evidence = [_evidence_public(store, handle, include_body=include_body, minimal=True) for handle in evidence_handles]
    user = {
        "schema_version": LINEAR_STAGE_PACKET_VERSION,
        "stage": "B",
        "root_id": page["root_id"],
        "page_id": page["page_id"],
        "scope": deepcopy(page["scope"]),
        "topic_ids": [str(item) for item in (topic_ids if topic_ids is not None else root.get("topics", {}).keys())],
        "message_handles": message_handles,
        "messages": messages,
        "evidence_handles": evidence_handles,
        "evidence": evidence,
        "candidate_handles": candidate_handles,
    }
    return _finish(
        user,
        prompt,
        store,
        message_count=len(messages),
        candidate_count=len(candidate_handles),
        evidence_count=len(evidence),
        allow_over_capacity=allow_over_capacity,
        raise_on_capacity=raise_on_capacity,
        snapshot_context={
            "source_refs": _snapshot_source_refs(store, root),
            "continuation_refs": _snapshot_continuation_refs(store, page),
        },
    )


def _materialize_c(
    store: LinearStagePacketStore,
    stage_a: Any,
    stage_b: Any = None,
    *,
    system_prompt: Optional[str] = None,
    allow_over_capacity: bool = False,
    raise_on_capacity: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    prompt = DEFAULT_SYSTEM_PROMPTS["C"] if system_prompt is None else str(system_prompt)
    a = _as_mapping(stage_a, include_body=False) if not isinstance(stage_a, Mapping) else deepcopy(dict(stage_a))
    b = _as_mapping(stage_b, include_body=False) if stage_b is not None and not isinstance(stage_b, Mapping) else deepcopy(dict(stage_b or {}))
    # Accept the nested provider-style envelope as well as its direct user map.
    if isinstance(a.get("packet"), Mapping):
        a = dict(a["packet"])
    if isinstance(b.get("packet"), Mapping):
        b = dict(b["packet"])
    a_scope = _scope_identity(a)
    b_scope = _scope_identity(b)
    if a_scope is not None and b_scope is not None and a_scope != b_scope:
        raise LinearStagePacketError("stage_scope_mismatch")
    scope = deepcopy(a.get("scope", b.get("scope", {})))
    a_messages = list(a.get("message_handles", ()))
    b_messages = list(b.get("message_handles", ()))
    a_candidates = list(a.get("candidate_handles", ()))
    a_refs = list(a.get("candidate_link_refs", ()))
    for item in a_refs:
        if isinstance(item, Mapping):
            handle = item.get("candidate_handle", item.get("candidate_id"))
        else:
            handle = item
        if handle not in (None, ""):
            a_candidates.append(handle)
    a_candidates = list(_unique(str(item) for item in a_candidates if item not in (None, "")))
    b_evidence = list(b.get("evidence_handles", ()))
    evidence_handles = list(_unique(b_evidence + list(a.get("evidence_handles", ()))))
    user = {
        "schema_version": LINEAR_STAGE_PACKET_VERSION,
        "stage": "C",
        "root_id": a.get("root_id", b.get("root_id", "")),
        "page_id": a.get("page_id", b.get("page_id", "")),
        "scope": scope,
        "stage_a": {
            "message_handles": list(_unique(a_messages)),
            "candidate_link_refs": _body_free(a_refs),
            "candidate_handles": list(_unique(a_candidates)),
        },
        "stage_b": {
            "topic_ids": list(b.get("topic_ids", ())),
            "message_handles": list(_unique(b_messages)),
            "evidence_handles": evidence_handles,
        },
        "evidence_handles": evidence_handles,
    }
    return _finish(
        user,
        prompt,
        store,
        message_count=len(_unique(a_messages + b_messages)),
        candidate_count=len(a_candidates),
        evidence_count=len(evidence_handles),
        allow_over_capacity=allow_over_capacity,
        raise_on_capacity=raise_on_capacity,
        snapshot_context={"source_refs": []},
    )


def _recover(store: LinearStagePacketStore, ref: Any, *, include_body: bool = True, **_: Any) -> Dict[str, Any]:
    root = store.get_root(ref)
    result = store._decode(root.get("source_template", {}), include_body=include_body)
    layer_rows = root.get("layer_rows", {})
    primary: List[Dict[str, Any]] = []
    adjacent: List[Dict[str, Any]] = []
    facts: List[Dict[str, Any]] = []
    for link in layer_rows.get("primary_fragments", ()):
        handle = link.get("message_handle") if isinstance(link, Mapping) else None
        record = store.message_table.get(str(handle))
        if record:
            rows = record.get("primary_rows", ())
            index = 0
            if isinstance(link, Mapping) and link.get("row_index") is not None:
                index = int(link.get("row_index", 0))
            if rows:
                primary.append(store._decode(rows[min(index, len(rows) - 1)], include_body=include_body))
    for link in layer_rows.get("adjacent_context", ()):
        handle = link.get("message_handle") if isinstance(link, Mapping) else None
        record = store.message_table.get(str(handle))
        if record:
            rows = record.get("adjacent_rows", ())
            index = int(link.get("row_index", 0)) if isinstance(link, Mapping) else 0
            if rows:
                adjacent.append(store._decode(rows[min(index, len(rows) - 1)], include_body=include_body))
    for link in layer_rows.get("authoritative_facts", ()):
        handle = link.get("message_handle") if isinstance(link, Mapping) else None
        record = store.message_table.get(str(handle))
        if record:
            rows = record.get("authority_rows", ())
            if rows:
                index = int(link.get("row_index", 0)) if isinstance(link, Mapping) else 0
                facts.append(store._decode(rows[min(index, len(rows) - 1)], include_body=include_body))
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    views = layer_rows.get("candidate_views", {}) if isinstance(layer_rows.get("candidate_views"), Mapping) else {}
    for view, handles in views.items():
        candidates[str(view)] = [store._decode(store.candidate_table[item], include_body=include_body) for item in handles if item in store.candidate_table]
    # Evidence may be declared directly by the packet or only nested under a
    # candidate relation.  The global root ledger already records both, but
    # older roots and hand-built stores can omit the nested rows from
    # ``layer_rows.evidence_refs``.  Union direct, root, and candidate-owned
    # handles in stable order so recovery is lossless without multiplying
    # candidate x evidence rows.
    evidence_handles: List[str] = []
    seen_evidence = set()

    def add_evidence_handle(value: Any) -> None:
        handle = str(value) if value not in (None, "") else ""
        if handle and handle in store.evidence_table and handle not in seen_evidence:
            seen_evidence.add(handle)
            evidence_handles.append(handle)

    for item in layer_rows.get("evidence_refs", ()):
        if isinstance(item, Mapping):
            add_evidence_handle(item.get("evidence_handle"))
    for item in root.get("evidence_handles", ()):
        add_evidence_handle(item)
    candidate_handles = list(root.get("candidate_handles", ()))
    for handles in views.values():
        candidate_handles.extend(handles if isinstance(handles, (list, tuple)) else ())
    for candidate_handle in _unique(str(item) for item in candidate_handles):
        record = store.candidate_table.get(candidate_handle, {})
        for evidence_handle in record.get("evidence_handle_refs", ()):
            add_evidence_handle(evidence_handle)
    evidence = [
        store._decode(store.evidence_table[handle], include_body=include_body)
        for handle in evidence_handles
    ]
    result.update({
        "packet_id": root.get("source_packet_id", root.get("root_id")),
        "account_id": root.get("scope", {}).get("account_id"),
        "chat_id": root.get("scope", {}).get("chat_id"),
        "scope": deepcopy(root.get("scope", {})),
        "primary_fragments": primary,
        "adjacent_context": adjacent,
        "authoritative_facts": facts,
        "evidence_refs": evidence,
        "source_refs": [store._decode(row, include_body=include_body) for row in root.get("source_rows", ())],
    })
    for view, rows in candidates.items():
        result[view] = rows
    return result


def _resolve_store_and_ref(first: Any, second: Any = None) -> Tuple[LinearStagePacketStore, Any]:
    if isinstance(first, LinearStagePacketStore):
        if second is None:
            raise TypeError("packet/page reference is required")
        return first, second
    if isinstance(second, LinearStagePacketStore):
        return second, first
    if hasattr(first, "store") and isinstance(getattr(first, "store"), LinearStagePacketStore):
        return first.store, second
    raise TypeError("first or second argument must be LinearStagePacketStore")


def build_linear_stage_packets(packets: Any, *, capacity: Any = None, store: Optional[LinearStagePacketStore] = None, **kwargs: Any) -> LinearStagePacketStore:
    """Build one global table store and linearly paged roots."""

    stage_prompt = kwargs.get("stage_a_system_prompt")
    if stage_prompt is None:
        stage_prompt = kwargs.get("system_prompt_a")
    if stage_prompt is None:
        stage_prompt = kwargs.get("system_prompt")
    if store is None:
        overrides = {name: kwargs[name] for name in ("max_input_token_proxy", "max_user_token_proxy", "max_messages", "max_candidate_rows", "max_evidence_refs") if name in kwargs}
        if capacity is None and overrides:
            capacity = overrides
        store = LinearStagePacketStore(capacity=capacity, stage_a_system_prompt=stage_prompt)
    elif capacity is not None:
        store.capacity = _capacity(capacity)
    return store.add_packets(packets, stage_a_system_prompt=stage_prompt)


def materialize_stage_a(store_or_ref: Any, ref_or_store: Any = None, **kwargs: Any) -> Dict[str, Any]:
    store, ref = _resolve_store_and_ref(store_or_ref, ref_or_store)
    return _materialize_a(store, ref, **kwargs)


def materialize_stage_b(store_or_ref: Any, ref_or_store: Any = None, **kwargs: Any) -> Dict[str, Any]:
    store, ref = _resolve_store_and_ref(store_or_ref, ref_or_store)
    return _materialize_b(store, ref, **kwargs)


def materialize_stage_c(store_or_a: Any, stage_a_or_b: Any = None, stage_b: Any = None, **kwargs: Any) -> Dict[str, Any]:
    if isinstance(store_or_a, LinearStagePacketStore):
        store = store_or_a
        a = stage_a_or_b
        b = stage_b
    elif isinstance(stage_a_or_b, LinearStagePacketStore):
        store = stage_a_or_b
        a = store_or_a
        b = stage_b
    elif isinstance(store_or_a, Mapping) and isinstance(stage_a_or_b, Mapping) and stage_b is not None and isinstance(stage_b, LinearStagePacketStore):
        store = stage_b
        a = store_or_a
        b = stage_a_or_b
    else:
        raise TypeError("materialize_stage_c requires store and stage A/B envelopes")
    return _materialize_c(store, a, b, **kwargs)


def recover_linear_packet(store_or_ref: Any, ref_or_store: Any = None, **kwargs: Any) -> Dict[str, Any]:
    store, ref = _resolve_store_and_ref(store_or_ref, ref_or_store)
    return _recover(store, ref, **kwargs)


__all__ = [
    "LINEAR_STAGE_PACKET_VERSION",
    "LINEAR_STORE_SCHEMA_VERSION",
    "LINEAR_PIPELINE_VERSION",
    "DEFAULT_MAX_INPUT_TOKEN_PROXY",
    "DEFAULT_MAX_USER_TOKEN_PROXY",
    "DEFAULT_MAX_MESSAGES",
    "DEFAULT_MAX_CANDIDATE_ROWS",
    "DEFAULT_MAX_EVIDENCE_REFS",
    "LinearCapacity",
    "LinearMaterialStats",
    "LinearStagePacketError",
    "LinearCapacityError",
    "LinearStagePacketStore",
    "build_linear_stage_packets",
    "materialize_stage_a",
    "materialize_stage_b",
    "materialize_stage_c",
    "recover_linear_packet",
    "canonical_json",
    "stable_hash",
    "estimate_token_proxy",
]
