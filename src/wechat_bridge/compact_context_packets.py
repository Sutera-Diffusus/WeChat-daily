"""K6 compact, reversible ContextPacket material.

This module is deliberately a *local projection* of the development-only K2
``ContextPacket``.  It is not a provider adapter and it is not a semantic
decision layer.  The compact store keeps the expensive parts of overlapping
packets in content-addressed tables and leaves packets as small, body-free
indexes.  A selected leaf packet can be materialised into a request-local
view; the view uses short handles for message/fragment content and keeps
authoritative facts physically separate from candidate rows.

Important boundaries:

* no file, frozen split, provider, production table, or production API is
  touched by this module;
* source packets are never mutated;
* time, same-segment, lexical and other source candidate reasons are retained
  as candidate metadata only; this module never creates a topic, event, claim,
  state or relation verdict;
* over-capacity data is split on complete message/candidate/evidence units or
  retained as ``pending``.  Content is never silently truncated.

The public entry points are :func:`compact_context_packets`,
:class:`CompactContextPacketStore`, and :func:`materialize_stage_packet`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

from .dialogue_segments import has_context_prefix, is_context_only_text


COMPACT_CONTEXT_PACKET_VERSION = "context_packet_compact_v1"
COMPACT_STORE_SCHEMA_VERSION = "compact_context_packet_store_v1"
COMPACT_PROJECTOR_SCHEMA_VERSION = "compact_context_packet_projector_v1"
COMPACT_PIPELINE_VERSION = "workstream_k6_compact_context_packet_v1"
UNKNOWN = "unknown"

DEFAULT_MAX_INPUT_TOKEN_PROXY = 2000
DEFAULT_MAX_MESSAGES = 24
DEFAULT_MAX_CANDIDATE_ROWS = 64
DEFAULT_MAX_EVIDENCE_REFS = 64

FROZEN_MARKERS = frozenset({"frozen", "frozen_test", "frozen-test"})
_PROVIDER_CONTEXT_FRAGMENT_TYPES = frozenset(
    {"conversation_opener", "acknowledgement", "reaction", "context", "media"}
)
_PROVIDER_MEDIA_TYPES = frozenset(
    {"image", "video", "audio", "file", "sticker", "emoji", "system", "location"}
)
_PROVIDER_SOCIAL_ROLES = frozenset({"conversation_opener", "context_only"})
# Provider role projection is deliberately stricter than K2 retention.  These
# labels/types describe an authoritative placeholder or social marker.  A
# candidate/merged cue must never replace one of them.  The only exception is
# an independently carried direct human caption paired with a positive source
# role (the same narrow exception used by the compact Stage-A protocol).
_PROVIDER_POSITIVE_ROLES = frozenset({"primary", "substantive", "mixed"})
_PROVIDER_BLOCKED_ROLES = frozenset(
    {
        "media",
        "media_placeholder",
        "placeholder",
        "system",
        "event",
        "event_placeholder",
        "event_place_holder",
        "empty",
        "empty_authority",
        "reaction",
        "greeting",
        "conversation_opener",
        "ack",
        "acknowledgement",
        "context_only",
        "authority_only",
    }
)
_PROVIDER_BLOCKED_MESSAGE_TYPES = frozenset(
    {
        "image",
        "photo",
        "picture",
        "video",
        "audio",
        "voice",
        "file",
        "document",
        "card",
        "sticker",
        "emoji",
        "system",
        "location",
        "media",
        "empty",
        "empty_authority",
        "event",
        "event_message",
        "event_placeholder",
    }
)
_PROVIDER_DIRECT_CUE_FIELDS = ("caption", "text", "text_redacted", "message_text")

# These names are treated as body-bearing only when their value is a scalar
# body.  We use the same conservative vocabulary as the K5 audit.  Hashes,
# IDs and metadata keys are never removed merely because their *value* happens
# to contain a marker.
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "evidence_text",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_redacted",
    }
)

_PRIMARY_KEYS = frozenset({"primary_fragments", "primary", "fragments"})
_ADJACENT_KEYS = frozenset({"adjacent_context", "adjacent", "context_fragments"})
_FACT_KEYS = frozenset({"authoritative_facts", "message_metadata", "facts"})
_EVIDENCE_KEYS = frozenset({"evidence_refs", "evidence", "evidence_references"})
_SOURCE_KEYS = frozenset({"source_refs", "sources", "source_references"})
_CANDIDATE_KEYS = frozenset(
    {
        "candidate_qa_links",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "continuity_candidates",
        "qa_candidates",
        "person_history",
        "object_history",
        "state_history",
        "open_thread_candidates",
        "open_threads",
        "candidate_rows",
        "candidates",
        "candidate_reasons",
    }
)
_CUE_KEYS = frozenset({"activation_cues", "cues"})


class CompactContextPacketError(ValueError):
    """Stable, fail-closed K6 input or projection error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        message = self.code if not detail else "%s: %s" % (self.code, detail)
        super().__init__(message)


class CompactCapacityError(CompactContextPacketError):
    """A provider material view exceeds the K6 hard limits."""

    def __init__(self, stats: Mapping[str, Any], limits: "CompactCapacity") -> None:
        self.stats = deepcopy(dict(stats))
        self.limits = limits
        super().__init__("capacity_exceeded")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _compact_provider_labels(value: Any) -> Set[str]:
    """Collect body-free role labels from one authoritative/source row."""

    if not isinstance(value, Mapping):
        return set()
    labels: Set[str] = set()
    sources: List[Mapping[str, Any]] = [value]
    # A linearized message can keep the immutable registry row under one of
    # these names.  Only role/type metadata is read from it; body fields are
    # never copied into the projection.
    for key in ("identity_row", "authority", "authoritative", "message_metadata", "source_row", "mapping_row"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key in (
            "semantic_role",
            "provider_role",
            "primary_context_role",
            "role",
            "dialogue_role",
            "message_role",
            "layer",
            "fragment_role",
        ):
            marker = source.get(key)
            if marker not in (None, ""):
                labels.add(str(marker).strip().casefold().replace("-", "_").replace(" ", "_"))
        roles = source.get("roles")
        if isinstance(roles, (list, tuple, set, frozenset)):
            labels.update(
                str(marker).strip().casefold().replace("-", "_").replace(" ", "_")
                for marker in roles
                if marker not in (None, "")
            )
    return labels


def _compact_provider_type(value: Any) -> str:
    """Read the authoritative message type, including nested identity rows."""

    if not isinstance(value, Mapping):
        return ""
    sources: List[Mapping[str, Any]] = [value]
    for key in ("identity_row", "authority", "authoritative", "message_metadata", "source_row", "mapping_row"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key in ("message_type", "type", "media_type", "content_type"):
            marker = source.get(key)
            if marker not in (None, ""):
                return str(marker).strip().casefold().replace("-", "_").replace(" ", "_")
    return ""


def _compact_direct_human_presence(value: Any) -> bool:
    """Return whether a row carries a direct caption/text field.

    ``content``/``description`` are intentionally excluded: on media and
    event rows they are commonly XML/JSON transport metadata.  The helper
    only answers presence and never returns the body itself.
    """

    if not isinstance(value, Mapping):
        return False
    for key in _PROVIDER_DIRECT_CUE_FIELDS:
        candidate = value.get(key)
        if isinstance(candidate, str) and bool(candidate.strip()):
            return True
    refs = value.get("content_ref_fields")
    if isinstance(refs, Mapping):
        return any(str(key) in _PROVIDER_DIRECT_CUE_FIELDS and value_ref not in (None, "") for key, value_ref in refs.items())
    # Linear message rows retain the encoded identity/authority body fields
    # separately.  Presence is enough for the gate; body recovery remains in
    # the private content table and is not emitted here.
    for key in ("identity_row", "authority", "authoritative", "message_metadata"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and _compact_direct_human_presence(nested):
            return True
    return False


def _compact_positive_role(value: Any) -> bool:
    return bool(_compact_provider_labels(value) & _PROVIDER_POSITIVE_ROLES)


def _compact_valid_span(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    start = value.get("start", value.get("span_start"))
    end = value.get("end", value.get("span_end", start))
    return isinstance(start, int) and isinstance(end, int) and start >= 0 and end >= start


def _compact_authority_blocks_cue(row: Mapping[str, Any], authority: Mapping[str, Any]) -> bool:
    """Return whether authoritative metadata is a hard context barrier."""

    authority_labels = _compact_provider_labels(authority)
    authority_type = _compact_provider_type(authority)
    # ``event`` is a barrier even when a source omitted the explicit
    # placeholder flag; event payloads are not human message text.
    if authority_labels & _PROVIDER_BLOCKED_ROLES:
        return True
    if authority_type in _PROVIDER_BLOCKED_MESSAGE_TYPES:
        return True
    if authority_type in {"event", "event_message"}:
        return True
    row_labels = _compact_provider_labels(row)
    row_type = _compact_provider_type(row)
    if row_labels & _PROVIDER_BLOCKED_ROLES or row_type in _PROVIDER_BLOCKED_MESSAGE_TYPES:
        return True
    if row_type in {"event", "event_message"}:
        return True
    if any(row.get(key) is True or row.get(key) == 1 for key in ("is_placeholder", "placeholder", "media_placeholder", "event_placeholder")):
        return True
    return False


def _compact_context_only(row: Mapping[str, Any], *, message_type: Any = "text") -> bool:
    """Classify a retained fragment only for a provider-facing view.

    Compact storage keeps the source row untouched.  This narrow projection
    helper is used when materialising a model packet, where context-only rows
    must be represented as context markers rather than independent primaries.
    """

    text = ""
    for key in ("text_redacted", "text", "message_text", "content", "body"):
        value = row.get(key)
        if isinstance(value, str):
            text = value
            break
    if is_context_only_text(text, message_type=message_type):
        return True
    roles = []
    for key in ("role", "dialogue_role", "message_role"):
        value = row.get(key)
        if isinstance(value, str):
            roles.append(value.casefold())
    value = row.get("roles")
    if isinstance(value, (list, tuple)):
        roles.extend(str(item).casefold() for item in value if isinstance(item, str))
    fragment_type = str(row.get("fragment_type") or row.get("kind") or "").strip().casefold()
    if fragment_type in _PROVIDER_CONTEXT_FRAGMENT_TYPES:
        if fragment_type != "media" and text and has_context_prefix(text):
            return False
        if fragment_type == "media" and text and not is_context_only_text(text, message_type=message_type):
            return str(message_type or "").casefold() in _PROVIDER_MEDIA_TYPES
        return True
    if set(roles) & _PROVIDER_SOCIAL_ROLES or bool(row.get("is_opener")):
        if text and has_context_prefix(text):
            return False
        return True
    return bool(row.get("is_silent")) and str(message_type or "").casefold() in _PROVIDER_MEDIA_TYPES


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _deepcopy_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): deepcopy(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return {str(key): deepcopy(item) for key, item in result.items()}
    raise CompactContextPacketError("packet_not_mapping")


def _text(value: Any, default: str = UNKNOWN) -> str:
    if value is None:
        return default
    text = str(value)
    return text if text else default


def _nonempty_text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if isinstance(value, str):
            item = value
        else:
            item = str(value)
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _list_of_mappings(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise CompactContextPacketError("layer_shape")
    output: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CompactContextPacketError("layer_row_shape")
        output.append({str(key): deepcopy(child) for key, child in item.items()})
    return output


def _scope_parts(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(value, Mapping):
        account = value.get("account_id", value.get("account"))
        chat = value.get("chat_id", value.get("chat"))
        return (_nonempty_text(account), _nonempty_text(chat))
    if isinstance(value, str) and value:
        # K3 uses account/chat, while K2 commonly carries a scope mapping.
        if "/" in value:
            account, chat = value.split("/", 1)
            return (_nonempty_text(account), _nonempty_text(chat))
        if "::" in value:
            account, chat = value.split("::", 1)
            return (_nonempty_text(account), _nonempty_text(chat))
    return None, None


def _scope_key(account_id: str, chat_id: str) -> str:
    return "%s/%s" % (account_id, chat_id)


def _scoped(scope: str, kind: str, identifier: str) -> str:
    return "%s|%s|%s" % (scope, kind, identifier)


def _is_frozen_marker(value: Any) -> bool:
    return isinstance(value, str) and value.casefold() in FROZEN_MARKERS


def _check_no_frozen(value: Any, *, structural_only: bool = True) -> None:
    """Reject explicit frozen markers without reading a frozen data source.

    ``structural_only`` deliberately ignores arbitrary body values; a user
    message saying ``frozen`` is not a split marker.  The check traverses
    split/scope keys and nested rows so a foreign marker cannot be hidden in a
    candidate or evidence reference.
    """

    def visit(item: Any, key: Optional[str] = None) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                child_key = str(raw_key)
                lower = child_key.casefold()
                if structural_only and lower not in {
                    "split",
                    "dataset_split",
                    "partition",
                    "scope",
                    "data_scope",
                    "source_scope",
                    "account_id",
                    "chat_id",
                }:
                    # Nested mappings can carry a scope marker under an
                    # arbitrary wrapper, so still descend into the child.
                    visit(child, child_key)
                    continue
                if _is_frozen_marker(child):
                    raise CompactContextPacketError("frozen_scope_forbidden")
                if isinstance(child, Mapping):
                    account, chat = _scope_parts(child)
                    if _is_frozen_marker(account) or _is_frozen_marker(chat):
                        raise CompactContextPacketError("frozen_scope_forbidden")
                visit(child, child_key)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, key)

    visit(value)


def _body_values(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Return body-bearing scalar fields from one row, preserving aliases."""

    result: Dict[str, Any] = {}
    for key, child in value.items():
        if str(key).casefold() in _BODY_KEYS and isinstance(child, (str, bytes)):
            result[str(key)] = child.decode("utf-8", "replace") if isinstance(child, bytes) else str(child)
    return result


def _without_body(value: Any) -> Any:
    """Copy a mapping while replacing scalar body values with no body.

    This helper is for table rows.  Source templates use explicit content
    references instead, allowing exact recovery without duplicating text.
    """

    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, child in value.items():
            if str(key).casefold() in _BODY_KEYS and isinstance(child, (str, bytes)):
                continue
            result[str(key)] = _without_body(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_without_body(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return [_without_body(child) for child in sorted(value, key=str)]
    return deepcopy(value)


def _body_free_export(value: Any) -> Any:
    """Return the public, body-free form used by ``to_dict()``.

    Compact tables keep ``content_ref_fields`` internally with the original
    source field names so recovery can restore the K2 shape.  Those names are
    themselves body-key names (``content``, ``text_redacted`` ...), however,
    and a default ledger/export must not look like it contains body fields.
    Keep the references while making the field names unambiguously references;
    drop all other body-key mappings/scalars from the public projection.
    """

    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            lower = key.casefold()
            if lower == "content_ref_fields" and isinstance(child, Mapping):
                result[key] = {
                    "%s_ref" % str(field_name): _body_free_export(ref)
                    for field_name, ref in child.items()
                }
                continue
            if lower in _BODY_KEYS:
                continue
            result[key] = _body_free_export(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_body_free_export(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return [_body_free_export(child) for child in sorted(value, key=str)]
    return deepcopy(value)


def _first_id(value: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        child = value.get(key)
        if isinstance(child, str) and child:
            return child
    return None


def _message_id(value: Mapping[str, Any]) -> Optional[str]:
    return _first_id(value, "message_id", "source_message_id")


def _fragment_id(value: Mapping[str, Any]) -> Optional[str]:
    return _first_id(value, "fragment_id", "source_fragment_id")


def _candidate_id(value: Mapping[str, Any]) -> Optional[str]:
    return _first_id(value, "candidate_id", "context_relation_id", "relation_id", "thread_id", "discourse_thread_id")


def _evidence_source_id(value: Mapping[str, Any]) -> Optional[str]:
    return _first_id(value, "evidence_id", "id", "source_ref_id", "ref_id")


def _ref_message_ids(value: Mapping[str, Any]) -> Tuple[str, ...]:
    result: List[str] = []
    for key in (
        "message_id",
        "left_message_id",
        "right_message_id",
        "question_id",
        "answer_id",
        "reply_to_message_id",
        "quoted_message_id",
    ):
        child = value.get(key)
        if isinstance(child, str) and child:
            result.append(child)
    for key in ("message_ids", "source_message_ids", "member_message_ids", "fragment_message_ids"):
        child = value.get(key)
        if isinstance(child, (list, tuple)):
            result.extend(str(item) for item in child if isinstance(item, str) and item)
    return _unique(result)


def _ref_fragment_ids(value: Mapping[str, Any]) -> Tuple[str, ...]:
    result: List[str] = []
    for key in (
        "fragment_id",
        "left_fragment_id",
        "right_fragment_id",
        "question_fragment_id",
        "answer_fragment_id",
    ):
        child = value.get(key)
        if isinstance(child, str) and child:
            result.append(child)
    for key in ("fragment_ids", "member_fragment_ids", "anchor_fragment_ids"):
        child = value.get(key)
        if isinstance(child, (list, tuple)):
            result.extend(str(item) for item in child if isinstance(item, str) and item)
    return _unique(result)


def _span(value: Any) -> Optional[Dict[str, int]]:
    if isinstance(value, Mapping):
        nested = value.get("span")
        if nested is not None:
            return _span(nested)
        start, end = value.get("start", value.get("span_start")), value.get("end", value.get("span_end"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start, end = value[0], value[1]
    else:
        return None
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        return None
    if start_i < 0 or end_i < start_i:
        return None
    return {"start": start_i, "end": end_i}


@dataclass(frozen=True)
class CompactCapacity:
    """Hard provider-material limits for one selected compact packet."""

    max_input_token_proxy: int = DEFAULT_MAX_INPUT_TOKEN_PROXY
    max_messages: int = DEFAULT_MAX_MESSAGES
    max_candidate_rows: int = DEFAULT_MAX_CANDIDATE_ROWS
    max_evidence_refs: int = DEFAULT_MAX_EVIDENCE_REFS

    def __post_init__(self) -> None:
        for name in ("max_input_token_proxy", "max_messages", "max_candidate_rows", "max_evidence_refs"):
            if int(getattr(self, name)) < 1:
                raise ValueError("%s must be positive" % name)

    def to_dict(self) -> Dict[str, int]:
        return {
            "max_input_token_proxy": int(self.max_input_token_proxy),
            "max_messages": int(self.max_messages),
            "max_candidate_rows": int(self.max_candidate_rows),
            "max_evidence_refs": int(self.max_evidence_refs),
        }


@dataclass(frozen=True)
class CompactContentEntry:
    """One private body; all packet/row layers refer to its ``content_id``."""

    content_id: str
    scope: str
    body_hash: str
    body: str
    source_refs: Tuple[Mapping[str, Any], ...] = ()

    @property
    def char_count(self) -> int:
        return len(self.body)

    def to_dict(self, *, include_body: bool = True) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "content_id": self.content_id,
            "scope": self.scope,
            "body_hash": self.body_hash,
            "char_count": self.char_count,
            "source_refs": sorted((deepcopy(dict(item)) for item in self.source_refs), key=canonical_json),
        }
        if include_body:
            result["body"] = self.body
        return result

    def __getitem__(self, key: str) -> Any:
        return self.to_dict(include_body=True)[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict(include_body=True).get(key, default)


@dataclass(frozen=True)
class CompactPacketMaterialStats:
    input_token_proxy: int
    canonical_chars: int
    message_count: int
    candidate_row_count: int
    evidence_ref_count: int

    def to_dict(self) -> Dict[str, int]:
        return {
            "input_token_proxy": int(self.input_token_proxy),
            "canonical_chars": int(self.canonical_chars),
            "message_count": int(self.message_count),
            "candidate_row_count": int(self.candidate_row_count),
            "evidence_ref_count": int(self.evidence_ref_count),
        }


@dataclass(frozen=True)
class CompactPacketCapacity:
    status: str
    stats: CompactPacketMaterialStats
    limits: CompactCapacity
    reason_code: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "stats": self.stats.to_dict(),
            "limits": self.limits.to_dict(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class CompactContextPacket:
    """Body-free compact packet index.

    All ``*_ids`` fields point into a :class:`CompactContextPacketStore`.
    ``adjacent_refs`` intentionally contains only ordered message/fragment
    identifiers and no text/body fields.
    """

    packet_id: str
    source_packet_id: str
    account_id: str
    chat_id: str
    scope: str
    window_scale: str = UNKNOWN
    anchor_fragment_ids: Tuple[str, ...] = ()
    anchor_claim_ids: Tuple[str, ...] = ()
    primary_refs: Tuple[Mapping[str, Any], ...] = ()
    adjacent_refs: Tuple[Mapping[str, Any], ...] = ()
    source_message_ids: Tuple[str, ...] = ()
    context_message_ids: Tuple[str, ...] = ()
    authoritative_fact_ids: Tuple[str, ...] = ()
    candidate_row_ids: Tuple[str, ...] = ()
    evidence_ref_ids: Tuple[str, ...] = ()
    source_ref_ids: Tuple[str, ...] = ()
    activation_cue_ids: Tuple[str, ...] = ()
    candidate_view_ids: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    candidate_reason_codes: Tuple[str, ...] = ()
    uncertainties: Tuple[str, ...] = ()
    open_snapshot_ref: str = ""
    predecessor_refs: Tuple[str, ...] = ()
    successor_refs: Tuple[str, ...] = ()
    boundary: Mapping[str, Any] = field(default_factory=dict)
    parent_packet_id: str = ""
    subpacket_ids: Tuple[str, ...] = ()
    status: str = "open"
    pending_reason: str = ""
    reactivation: Mapping[str, Any] = field(default_factory=dict)
    source_template_id: str = ""
    fixed_hash: str = ""
    dynamic_hash: str = ""
    content_hash: str = ""
    # Hashes of the source fixed/dynamic mappings are carried through rehashes
    # so a marker/metadata-only change cannot accidentally reuse a packet hash.
    source_fixed_hash: str = ""
    source_dynamic_hash: str = ""
    packet_hash: str = ""
    cache_key: str = ""
    packet_version: str = COMPACT_CONTEXT_PACKET_VERSION
    is_container: bool = False

    @property
    def id(self) -> str:
        return self.packet_id

    @property
    def hash(self) -> str:
        return self.packet_hash

    @property
    def message_ids(self) -> Tuple[str, ...]:
        return self.source_message_ids

    @property
    def all_message_ids(self) -> Tuple[str, ...]:
        return _unique(self.source_message_ids + self.context_message_ids)

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_row_ids)

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_ref_ids)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "source_packet_id": self.source_packet_id,
            "packet_version": self.packet_version,
            "scope": {"account_id": self.account_id, "chat_id": self.chat_id},
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "window_scale": self.window_scale,
            "anchor_fragment_ids": list(self.anchor_fragment_ids),
            "anchor_claim_ids": list(self.anchor_claim_ids),
            "primary_refs": [deepcopy(dict(item)) for item in self.primary_refs],
            "adjacent_refs": [deepcopy(dict(item)) for item in self.adjacent_refs],
            "source_message_ids": list(self.source_message_ids),
            "context_message_ids": list(self.context_message_ids),
            "authoritative_fact_ids": list(self.authoritative_fact_ids),
            "candidate_row_ids": list(self.candidate_row_ids),
            "candidate_view_ids": {str(key): list(value) for key, value in self.candidate_view_ids.items()},
            "evidence_ref_ids": list(self.evidence_ref_ids),
            "source_ref_ids": list(self.source_ref_ids),
            "activation_cue_ids": list(self.activation_cue_ids),
            "candidate_reason_codes": list(self.candidate_reason_codes),
            "uncertainties": list(self.uncertainties),
            "open_snapshot_ref": self.open_snapshot_ref,
            # Aliases make the split lineage explicit to callers that use the
            # contract's plural wording.
            "open_snapshot": self.open_snapshot_ref,
            "predecessor_refs": list(self.predecessor_refs),
            "successor_refs": list(self.successor_refs),
            "boundary": deepcopy(dict(self.boundary)),
            "parent_packet_id": self.parent_packet_id,
            "subpacket_ids": list(self.subpacket_ids),
            "status": self.status,
            "pending_reason": self.pending_reason,
            "reactivation": deepcopy(dict(self.reactivation)),
            "source_template_id": self.source_template_id,
            "fixed_hash": self.fixed_hash,
            "dynamic_hash": self.dynamic_hash,
            "content_hash": self.content_hash,
            "source_fixed_hash": self.source_fixed_hash,
            "source_dynamic_hash": self.source_dynamic_hash,
            "packet_hash": self.packet_hash,
            "hash": self.packet_hash,
            "cache_key": self.cache_key,
            "is_container": self.is_container,
        }


@dataclass(frozen=True)
class CompactCompressionStats:
    source_canonical_chars: int
    compact_index_chars: int
    compact_private_chars: int
    body_free_index_chars: int
    content_chars: int
    compression_ratio: float
    private_compression_ratio: float
    space_savings_pct: float
    duplicate_body_occurrences_removed: int
    duplicate_candidate_occurrences_removed: int
    duplicate_evidence_occurrences_removed: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_canonical_chars": self.source_canonical_chars,
            "compact_index_chars": self.compact_index_chars,
            "compact_private_chars": self.compact_private_chars,
            "body_free_index_chars": self.body_free_index_chars,
            "content_chars": self.content_chars,
            "compression_ratio": self.compression_ratio,
            "private_compression_ratio": self.private_compression_ratio,
            "space_savings_pct": self.space_savings_pct,
            "duplicate_body_occurrences_removed": self.duplicate_body_occurrences_removed,
            "duplicate_candidate_occurrences_removed": self.duplicate_candidate_occurrences_removed,
            "duplicate_evidence_occurrences_removed": self.duplicate_evidence_occurrences_removed,
        }


@dataclass(frozen=True)
class CompactContextPacketResult:
    """Result returned by :func:`compact_context_packets`.

    ``packets`` contains leaf packets suitable for selection.  Container
    parents remain available through ``store.packet_index`` and can be
    recovered by ``store.recover_packet``; keeping them out of this tuple
    prevents callers from accidentally sending both a parent and its children.
    """

    store: "CompactContextPacketStore"
    packets: Tuple[CompactContextPacket, ...]
    source_packet_ids: Tuple[str, ...] = ()
    input_hash: str = ""
    compression: CompactCompressionStats = field(
        default_factory=lambda: CompactCompressionStats(0, 0, 0, 0, 0, 1.0, 1.0, 0.0, 0, 0, 0)
    )

    @property
    def compact_packets(self) -> Tuple[CompactContextPacket, ...]:
        return self.packets

    @property
    def compression_ratio(self) -> float:
        return self.compression.compression_ratio

    @property
    def packet_ids(self) -> Tuple[str, ...]:
        return tuple(item.packet_id for item in self.packets)

    def __iter__(self) -> Iterator[CompactContextPacket]:
        return iter(self.packets)

    def __len__(self) -> int:
        return len(self.packets)

    def __getitem__(self, index: int) -> CompactContextPacket:
        return self.packets[index]

    def recover_packet(self, packet_id: str, *, include_body: bool = True) -> Dict[str, Any]:
        return self.store.recover_packet(packet_id, include_body=include_body)

    def materialize_stage_packet(
        self,
        packet: Union[str, CompactContextPacket],
        *,
        capacity: Optional[CompactCapacity] = None,
        allow_over_capacity: bool = False,
    ) -> Dict[str, Any]:
        return self.store.materialize_stage_packet(packet, capacity=capacity, allow_over_capacity=allow_over_capacity)

    def to_dict(self, *, include_content: bool = False) -> Dict[str, Any]:
        return {
            "schema_version": COMPACT_STORE_SCHEMA_VERSION,
            "packet_version": COMPACT_CONTEXT_PACKET_VERSION,
            "input_hash": self.input_hash,
            "source_packet_ids": list(self.source_packet_ids),
            "packet_ids": list(self.packet_ids),
            "packet_count": len(self.packets),
            "compression": self.compression.to_dict(),
            "store": self.store.to_dict(include_content=include_content),
            "packets": [item.to_dict() for item in self.packets],
        }


class CompactPacketCache:
    """Small independent cache with fixed/dynamic/content namespaces.

    Cache values contain references and metadata only.  The content namespace
    may be exported with private bodies when explicitly requested by the local
    caller; no cache is persisted or connected to a production store here.
    """

    def __init__(self) -> None:
        self.fixed: Dict[str, Dict[str, Any]] = {}
        self.dynamic: Dict[str, Dict[str, Any]] = {}
        self.content: Dict[str, Dict[str, Any]] = {}

    @property
    def fixed_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.fixed

    @property
    def dynamic_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.dynamic

    @property
    def content_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.content

    def get(self, part: str, digest: str) -> Optional[Dict[str, Any]]:
        table = getattr(self, str(part), None)
        if not isinstance(table, MutableMapping):
            raise ValueError("unknown compact cache part")
        value = table.get(str(digest))
        return deepcopy(value) if value is not None else None

    def put(self, part: str, digest: str, value: Mapping[str, Any]) -> str:
        table = getattr(self, str(part), None)
        if not isinstance(table, MutableMapping):
            raise ValueError("unknown compact cache part")
        table[str(digest)] = deepcopy(dict(value))
        return str(digest)

    def to_dict(self, *, include_content: bool = False) -> Dict[str, Any]:
        if include_content:
            content = deepcopy(self.content)
            content_key = "content"
        else:
            # ``content`` is itself a body-key in the compact contract's
            # conservative walker.  Name the body-free cache namespace as a
            # reference namespace; the in-memory ``.content`` attribute is
            # unchanged and explicit private export still uses ``content``.
            content = {key: _body_free_export(value) for key, value in self.content.items()}
            content_key = "content_cache"
        return {
            "schema_version": "compact_packet_cache_v1",
            "fixed": deepcopy(self.fixed),
            "dynamic": deepcopy(self.dynamic),
            content_key: content,
        }


class CompactContextPacketStore:
    """A process-local compact table store and projector.

    The constructor is intentionally public for tests and replay tools, but
    callers normally use :func:`compact_context_packets` or
    :meth:`from_packets`.  Mapping values are copied on ingress and egress so
    mutating a provider request cannot alter the authoritative tables.
    """

    def __init__(
        self,
        *,
        capacity: Optional[CompactCapacity] = None,
        packet_version: str = COMPACT_CONTEXT_PACKET_VERSION,
        cache: Optional[CompactPacketCache] = None,
    ) -> None:
        self.capacity = capacity or CompactCapacity()
        self.packet_version = _text(packet_version, COMPACT_CONTEXT_PACKET_VERSION)
        self.cache = cache or CompactPacketCache()
        self.content_table: Dict[str, CompactContentEntry] = {}
        self.fragment_table: Dict[str, Dict[str, Any]] = {}
        self.message_table: Dict[str, Dict[str, Any]] = {}
        self.authoritative_fact_table: Dict[str, Dict[str, Any]] = {}
        # Public aliases use the shorter names most callers expect.
        self.authoritative_facts = self.authoritative_fact_table
        self.candidate_row_table: Dict[str, Dict[str, Any]] = {}
        self.candidate_rows = self.candidate_row_table
        self.evidence_ref_table: Dict[str, Dict[str, Any]] = {}
        self.evidence_refs = self.evidence_ref_table
        self.source_ref_table: Dict[str, Dict[str, Any]] = {}
        self.activation_cue_table: Dict[str, Dict[str, Any]] = {}
        self.open_snapshot_table: Dict[str, Dict[str, Any]] = {}
        self.packet_index: Dict[str, CompactContextPacket] = {}
        self.source_templates: Dict[str, Any] = {}
        self._source_packet_mappings: Dict[str, Dict[str, Any]] = {}
        self._source_sizes: Dict[str, int] = {}
        self._source_template_counter = 0
        self._content_occurrences = 0
        self._candidate_occurrences = 0
        self._evidence_occurrences = 0

    @classmethod
    def from_packets(
        cls,
        packets: Any,
        *,
        capacity: Optional[CompactCapacity] = None,
        max_input_token_proxy: Optional[int] = None,
        max_messages: Optional[int] = None,
        max_candidate_rows: Optional[int] = None,
        max_evidence_refs: Optional[int] = None,
        packet_version: str = COMPACT_CONTEXT_PACKET_VERSION,
        cache: Optional[CompactPacketCache] = None,
    ) -> CompactContextPacketResult:
        return compact_context_packets(
            packets,
            capacity=capacity,
            max_input_token_proxy=max_input_token_proxy,
            max_messages=max_messages,
            max_candidate_rows=max_candidate_rows,
            max_evidence_refs=max_evidence_refs,
            packet_version=packet_version,
            cache=cache,
            store=cls(capacity=capacity, packet_version=packet_version, cache=cache),
        )

    @property
    def packets(self) -> Tuple[CompactContextPacket, ...]:
        """Leaf packets in deterministic insertion order."""

        return tuple(packet for packet in self.packet_index.values() if not packet.is_container)

    @property
    def compact_packets(self) -> Tuple[CompactContextPacket, ...]:
        return self.packets

    @property
    def content(self) -> Mapping[str, CompactContentEntry]:
        return self.content_table

    @property
    def fixed_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.cache.fixed

    @property
    def dynamic_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.cache.dynamic

    @property
    def content_cache(self) -> Dict[str, Dict[str, Any]]:
        return self.cache.content

    @staticmethod
    def _rows_from_container(data: Mapping[str, Any], keys: Iterable[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            rows.extend(_list_of_mappings(value))
        return rows

    @staticmethod
    def _nested_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        value = data.get(key)
        return value if isinstance(value, Mapping) else {}

    def _source_layers(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Collect K2 arrays from direct, dynamic and K1-named projections."""

        fixed = self._nested_mapping(data, "fixed_part")
        dynamic = self._nested_mapping(data, "dynamic_part")
        conceptual = self._nested_mapping(data, "candidate_context")
        # Direct K2 layers are authoritative when present.  Duplicated K5
        # mirrors are used only to fill a missing layer, never to replace it.
        primary = self._rows_from_container(data, ("primary_fragments",))
        if not primary:
            primary = self._rows_from_container(fixed, ("primary_fragments",))
        if not primary:
            primary = self._rows_from_container(conceptual, ("primary_fragments",))
        adjacent = self._rows_from_container(data, ("adjacent_context",))
        if not adjacent:
            adjacent = self._rows_from_container(dynamic, ("adjacent_context",))
        if not adjacent:
            adjacent = self._rows_from_container(conceptual, ("adjacent_context",))
        facts = self._rows_from_container(data, ("authoritative_facts",))
        if not facts:
            facts = self._rows_from_container(fixed, ("authoritative_facts", "message_metadata"))
        authority_contract = self._nested_mapping(data, "authoritative_facts_contract")
        if not facts:
            facts = self._rows_from_container(authority_contract, ("message_metadata",))
        evidence = self._rows_from_container(data, ("evidence_refs",))
        if not evidence:
            evidence = self._rows_from_container(dynamic, ("evidence_refs",))
        sources = self._rows_from_container(data, ("source_refs",))
        if not sources:
            sources = self._rows_from_container(fixed, ("source_refs",))

        direct_candidate_keys = (
            "candidate_qa_links",
            "candidate_person_history",
            "candidate_object_history",
            "candidate_state_history",
        )
        conceptual_candidate_keys = (
            "continuity_candidates",
            "qa_candidates",
            "person_history",
            "object_history",
            "state_history",
            "candidate_rows",
            "candidate_reasons",
        )
        candidates: List[Tuple[str, Dict[str, Any]]] = []
        for key in direct_candidate_keys:
            values = self._rows_from_container(data, (key,))
            if not values:
                values = self._rows_from_container(dynamic, (key,))
            if not values:
                # K1 conceptual aliases are still assigned their original
                # view name for provider-layer preservation.
                alias = {
                    "candidate_qa_links": "qa_candidates",
                    "candidate_person_history": "person_history",
                    "candidate_object_history": "object_history",
                    "candidate_state_history": "state_history",
                }[key]
                values = self._rows_from_container(conceptual, (alias,))
            candidates.extend((key, row) for row in values)
        for key in conceptual_candidate_keys:
            values = self._rows_from_container(conceptual, (key,))
            if not values:
                values = self._rows_from_container(dynamic, (key,))
            if not values and key in {"candidate_rows", "candidate_reasons"}:
                values = self._rows_from_container(data, (key,))
            candidates.extend((key, row) for row in values)
        # A packet may expose a direct ``open_thread_candidates`` layer rather
        # than the conceptual ``open_threads`` alias.
        for key in ("open_thread_candidates", "open_threads"):
            values = self._rows_from_container(data, (key,))
            if not values:
                values = self._rows_from_container(dynamic, (key,))
            if not values:
                values = self._rows_from_container(conceptual, (key,))
            candidates.extend(("open_threads", row) for row in values)

        cues = self._rows_from_container(data, ("activation_cues",))
        if not cues:
            cues = self._rows_from_container(dynamic, ("activation_cues",))
        if not cues:
            cues = self._rows_from_container(conceptual, ("activation_cues",))

        return {
            "primary": primary,
            "adjacent": adjacent,
            "facts": facts,
            "evidence": evidence,
            "sources": sources,
            "candidates": candidates,
            "cues": cues,
        }

    def _resolve_scope(self, data: Mapping[str, Any], layers: Mapping[str, Any]) -> Tuple[str, str, str]:
        declared_account = _nonempty_text(data.get("account_id"))
        declared_chat = _nonempty_text(data.get("chat_id"))
        scope_account, scope_chat = _scope_parts(data.get("scope"))
        if declared_account is not None and scope_account is not None and declared_account != scope_account:
            raise CompactContextPacketError("account_scope_mismatch")
        if declared_chat is not None and scope_chat is not None and declared_chat != scope_chat:
            raise CompactContextPacketError("chat_scope_mismatch")
        account_id = declared_account or scope_account or UNKNOWN
        chat_id = declared_chat or scope_chat or UNKNOWN

        observed: List[Tuple[str, str]] = []

        def visit(item: Any) -> None:
            if isinstance(item, Mapping):
                account = _nonempty_text(item.get("account_id"))
                chat = _nonempty_text(item.get("chat_id"))
                if account not in (None, UNKNOWN) and chat not in (None, UNKNOWN):
                    observed.append((account, chat))
                nested_scope = item.get("scope")
                nested_account, nested_chat = _scope_parts(nested_scope)
                if nested_account not in (None, UNKNOWN) and nested_chat not in (None, UNKNOWN):
                    observed.append((nested_account, nested_chat))
                for child in item.values():
                    visit(child)
            elif isinstance(item, (list, tuple, set, frozenset)):
                for child in item:
                    visit(child)

        # Scope comes from packet material, not from arbitrary body text.
        for value in layers.values():
            visit(value)
        observed_unique = list(dict.fromkeys(observed))
        accounts = {item[0] for item in observed_unique}
        chats = {item[1] for item in observed_unique}
        if len(accounts) > 1:
            raise CompactContextPacketError("cross_account_scope_forbidden")
        if len(chats) > 1:
            raise CompactContextPacketError("cross_chat_scope_forbidden")
        if observed_unique:
            observed_account, observed_chat = observed_unique[0]
            if account_id != UNKNOWN and account_id != observed_account:
                raise CompactContextPacketError("account_scope_mismatch")
            if chat_id != UNKNOWN and chat_id != observed_chat:
                raise CompactContextPacketError("chat_scope_mismatch")
            if account_id == UNKNOWN:
                account_id = observed_account
            if chat_id == UNKNOWN:
                chat_id = observed_chat
        if account_id.casefold() in FROZEN_MARKERS or chat_id.casefold() in FROZEN_MARKERS:
            raise CompactContextPacketError("frozen_scope_forbidden")

        # Explicit source IDs in nested rows are still checked against the
        # resolved pair.  Unknown scope is allowed only for a single source
        # message; a multi-message unknown packet is not safe to compact.
        message_ids: List[str] = []
        for value in layers.values():
            def collect(item: Any) -> None:
                if isinstance(item, Mapping):
                    message = _message_id(item)
                    if message:
                        message_ids.append(message)
                    account = _nonempty_text(item.get("account_id"))
                    chat = _nonempty_text(item.get("chat_id"))
                    if account not in (None, UNKNOWN, account_id):
                        raise CompactContextPacketError("cross_account_scope_forbidden")
                    if chat not in (None, UNKNOWN, chat_id):
                        raise CompactContextPacketError("cross_chat_scope_forbidden")
                    nested = item.get("scope")
                    nested_account, nested_chat = _scope_parts(nested)
                    if nested_account not in (None, UNKNOWN, account_id):
                        raise CompactContextPacketError("cross_account_scope_forbidden")
                    if nested_chat not in (None, UNKNOWN, chat_id):
                        raise CompactContextPacketError("cross_chat_scope_forbidden")
                    for child in item.values():
                        collect(child)
                elif isinstance(item, (list, tuple, set, frozenset)):
                    for child in item:
                        collect(child)

            collect(value)
        if (account_id == UNKNOWN or chat_id == UNKNOWN) and len(set(message_ids)) > 1:
            raise CompactContextPacketError("scope_unknown_for_multi_message_packet")
        return account_id, chat_id, _scope_key(account_id, chat_id)

    def _add_content(self, scope: str, body: Any, *, source_kind: str, source_id: str) -> str:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        body_hash = stable_hash({"scope": scope, "body": text})
        content_id = _scoped(scope, "content", body_hash[:32])
        source_ref = {"kind": str(source_kind), "id": str(source_id)}
        current = self.content_table.get(content_id)
        if current is not None:
            if current.body != text:
                # Hash collision is extraordinarily unlikely, but failing
                # closed is preferable to aliasing two bodies.
                raise CompactContextPacketError("content_hash_collision")
            refs = list(current.source_refs)
            if source_ref not in refs:
                refs.append(source_ref)
                self.content_table[content_id] = CompactContentEntry(
                    content_id=current.content_id,
                    scope=current.scope,
                    body_hash=current.body_hash,
                    body=current.body,
                    source_refs=tuple(refs),
                )
            self._content_occurrences += 1
            return content_id
        self.content_table[content_id] = CompactContentEntry(
            content_id=content_id,
            scope=scope,
            body_hash=body_hash,
            body=text,
            source_refs=(source_ref,),
        )
        self._content_occurrences += 1
        return content_id

    @staticmethod
    def _replace_nested_body_refs(
        value: Any,
        *,
        scope: str,
        add_content: Any,
        source_kind: str,
        source_id: str,
    ) -> Any:
        if isinstance(value, Mapping):
            result: Dict[str, Any] = {}
            for key, child in value.items():
                if str(key).casefold() in _BODY_KEYS and isinstance(child, (str, bytes)):
                    content_id = add_content(scope, child, source_kind=source_kind, source_id=source_id)
                    result["$content_field:%s" % str(key)] = content_id
                else:
                    result[str(key)] = CompactContextPacketStore._replace_nested_body_refs(
                        child,
                        scope=scope,
                        add_content=add_content,
                        source_kind=source_kind,
                        source_id=source_id,
                    )
            return result
        if isinstance(value, (list, tuple)):
            return [
                CompactContextPacketStore._replace_nested_body_refs(
                    child,
                    scope=scope,
                    add_content=add_content,
                    source_kind=source_kind,
                    source_id=source_id,
                )
                for child in value
            ]
        return deepcopy(value)

    def _add_fragment(self, row: Mapping[str, Any], scope: str, *, role: str) -> str:
        fragment_id = _fragment_id(row)
        message_id = _message_id(row)
        if not fragment_id:
            # Hand-built packets may omit fragment IDs.  Use an input-stable
            # local ID while retaining the entire body and source row.
            fragment_id = "fragment_%s" % stable_hash(_without_body(row))[:24]
        if not message_id:
            message_id = UNKNOWN
        key = _scoped(scope, "fragment", fragment_id)
        body_fields = _body_values(row)
        body_ref_fields: Dict[str, str] = {}
        for name, body in body_fields.items():
            body_ref_fields[name] = self._add_content(scope, body, source_kind="fragment", source_id=fragment_id)
        record = self.fragment_table.get(key)
        clean = _without_body(row)
        # Evidence is represented once in the evidence table; fragment table
        # stores only IDs for nested references.
        nested_evidence = row.get("evidence_refs")
        if isinstance(nested_evidence, (list, tuple)):
            clean["evidence_ref_ids"] = list(self._register_evidence_rows(nested_evidence, scope, owner_id=fragment_id))
            clean.pop("evidence_refs", None)
        clean["fragment_id"] = fragment_id
        clean["message_id"] = message_id
        clean["scope"] = scope
        clean["content_ref_fields"] = body_ref_fields
        roles: List[str] = []
        if isinstance(record, Mapping):
            roles.extend(str(item) for item in record.get("roles", ()) if isinstance(item, str))
        # Preserve the canonical K2 semantic role as a body-free authority.
        # The storage view also records whether this fragment was selected as
        # a primary/adjacent ref, but that transport role must not overwrite
        # ``context_only``/``conversation_opener`` when K6 projects a
        # provider-facing primary subset later.
        declared_role = row.get("role")
        if isinstance(declared_role, str) and declared_role and declared_role not in roles:
            roles.append(declared_role)
        declared_roles = row.get("roles")
        if isinstance(declared_roles, (list, tuple)):
            for item in declared_roles:
                if isinstance(item, str) and item and item not in roles:
                    roles.append(item)
        if role not in roles:
            roles.append(role)
        clean["roles"] = roles
        if isinstance(record, Mapping):
            existing_body = record.get("content_ref_fields")
            if isinstance(existing_body, Mapping):
                merged = dict(existing_body)
                merged.update(body_ref_fields)
                clean["content_ref_fields"] = merged
            existing_evidence = record.get("evidence_ref_ids")
            if isinstance(existing_evidence, (list, tuple)):
                clean["evidence_ref_ids"] = list(_unique(tuple(existing_evidence) + tuple(clean.get("evidence_ref_ids", ()))))
        self.fragment_table[key] = clean
        return key

    def _add_message(self, row: Mapping[str, Any], scope: str, *, fragment_keys: Sequence[str], primary: bool) -> str:
        message_id = _message_id(row) or UNKNOWN
        key = _scoped(scope, "message", message_id)
        body_fields = _body_values(row)
        content_ref_fields = {
            name: self._add_content(scope, body, source_kind="message", source_id=message_id)
            for name, body in body_fields.items()
        }
        clean = _without_body(row)
        clean["message_id"] = message_id
        clean["scope"] = scope
        clean["content_ref_fields"] = content_ref_fields
        current = self.message_table.get(key)
        if isinstance(current, Mapping):
            merged = dict(current)
            merged.update(clean)
            old_fields = current.get("content_ref_fields") if isinstance(current.get("content_ref_fields"), Mapping) else {}
            merged["content_ref_fields"] = {**dict(old_fields), **content_ref_fields}
            old_fragments = current.get("fragment_keys") if isinstance(current.get("fragment_keys"), (list, tuple)) else []
            clean_fragments = list(_unique(tuple(old_fragments) + tuple(fragment_keys)))
            merged["fragment_keys"] = clean_fragments
            merged["primary"] = bool(current.get("primary")) or bool(primary)
            self.message_table[key] = merged
        else:
            clean["fragment_keys"] = list(_unique(fragment_keys))
            clean["primary"] = bool(primary)
            self.message_table[key] = clean
        return key

    def _register_evidence_rows(self, values: Any, scope: str, *, owner_id: str = "") -> Tuple[str, ...]:
        if values is None:
            return ()
        if not isinstance(values, (list, tuple)):
            raise CompactContextPacketError("evidence_shape")
        output: List[str] = []
        for raw in values:
            if not isinstance(raw, Mapping):
                raise CompactContextPacketError("evidence_row_shape")
            row = {str(key): deepcopy(child) for key, child in raw.items()}
            _check_no_frozen(row)
            source_id = _evidence_source_id(row)
            if not source_id:
                source_id = "evidence_%s" % stable_hash(_without_body(row))[:24]
            ref_type = _text(row.get("type"), "evidence")
            canonical_id = row.get("evidence_id") if isinstance(row.get("evidence_id"), str) and row.get("evidence_id") else "%s:%s" % (ref_type, source_id)
            key = _scoped(scope, "evidence", str(canonical_id))
            body_fields = _body_values(row)
            body_refs = {
                name: self._add_content(scope, body, source_kind="evidence", source_id=str(canonical_id))
                for name, body in body_fields.items()
            }
            clean = _without_body(row)
            clean.pop("evidence_refs", None)
            clean["evidence_id"] = str(canonical_id)
            clean["source_ref_id"] = source_id
            clean["scope"] = scope
            clean["content_ref_fields"] = body_refs
            if owner_id:
                clean.setdefault("owner_ids", []).append(str(owner_id))
            existing = self.evidence_ref_table.get(key)
            if existing is not None:
                # Identical references collapse.  A conflicting same-ID ref is
                # retained under a deterministic disambiguated key instead of
                # silently replacing authoritative evidence.
                if stable_hash(existing) != stable_hash(clean):
                    key = _scoped(scope, "evidence", "%s:%s" % (canonical_id, stable_hash(clean)[:12]))
                    existing = self.evidence_ref_table.get(key)
                if existing is not None:
                    owner_ids = list(existing.get("owner_ids", ())) if isinstance(existing, Mapping) else []
                    owner_ids.extend(str(owner_id) for _ in [0] if owner_id and str(owner_id) not in owner_ids)
                    merged = dict(existing)
                    merged["owner_ids"] = list(_unique(owner_ids))
                    self.evidence_ref_table[key] = merged
                    output.append(key)
                    self._evidence_occurrences += 1
                    continue
            self.evidence_ref_table[key] = clean
            output.append(key)
            self._evidence_occurrences += 1
        return _unique(output)

    def _add_fact(self, row: Mapping[str, Any], scope: str) -> str:
        value = {str(key): deepcopy(child) for key, child in row.items()}
        _check_no_frozen(value)
        message_id = _message_id(value) or UNKNOWN
        key = _scoped(scope, "fact", message_id)
        body_refs = {
            name: self._add_content(scope, body, source_kind="fact", source_id=message_id)
            for name, body in _body_values(value).items()
        }
        clean = _without_body(value)
        evidence_ids = self._register_evidence_rows(value.get("evidence_refs"), scope, owner_id=message_id)
        clean.pop("evidence_refs", None)
        clean["message_id"] = message_id
        clean["scope"] = scope
        clean["metadata_authoritative"] = True
        clean["content_ref_fields"] = body_refs
        clean["evidence_ref_ids"] = list(evidence_ids)
        existing = self.authoritative_fact_table.get(key)
        if existing is not None and stable_hash(existing) != stable_hash(clean):
            # Metadata revisions are append-only.  Preserve both rows when a
            # packet repeats one message ID with a changed authoritative
            # record; never let the later row overwrite the first silently.
            key = _scoped(scope, "fact", "%s:%s" % (message_id, stable_hash(clean)[:12]))
            existing = self.authoritative_fact_table.get(key)
        if existing is None:
            self.authoritative_fact_table[key] = clean
        else:
            merged = dict(existing)
            merged["evidence_ref_ids"] = list(_unique(tuple(existing.get("evidence_ref_ids", ())) + tuple(evidence_ids)))
            old_fields = existing.get("content_ref_fields") if isinstance(existing.get("content_ref_fields"), Mapping) else {}
            merged["content_ref_fields"] = {**dict(old_fields), **body_refs}
            self.authoritative_fact_table[key] = merged
        return key

    @staticmethod
    def _candidate_view_name(view: str) -> str:
        mapping = {
            "candidate_qa_links": "qa_candidates",
            "candidate_person_history": "person_history",
            "candidate_object_history": "object_history",
            "candidate_state_history": "state_history",
            "continuity_candidates": "continuity_candidates",
            "qa_candidates": "qa_candidates",
            "person_history": "person_history",
            "object_history": "object_history",
            "state_history": "state_history",
            "open_thread_candidates": "open_threads",
            "open_threads": "open_threads",
            "candidate_reasons": "candidate_reasons",
            "candidate_rows": "continuity_candidates",
            "candidates": "continuity_candidates",
        }
        return mapping.get(str(view), str(view))

    def _add_candidate(self, row: Mapping[str, Any], scope: str, *, view: str) -> str:
        value = {str(key): deepcopy(child) for key, child in row.items()}
        _check_no_frozen(value)
        candidate_id = _candidate_id(value)
        if not candidate_id:
            candidate_id = "candidate_%s" % stable_hash(_without_body(value))[:24]
        key = _scoped(scope, "candidate", candidate_id)
        body_refs = {
            name: self._add_content(scope, body, source_kind="candidate", source_id=candidate_id)
            for name, body in _body_values(value).items()
        }
        evidence_ids = self._register_evidence_rows(value.get("evidence_refs"), scope, owner_id=candidate_id)
        clean = _without_body(value)
        clean.pop("evidence_refs", None)
        clean["candidate_id"] = candidate_id
        clean["scope"] = scope
        clean["candidate_only"] = True
        clean["content_ref_fields"] = body_refs
        clean["evidence_ref_ids"] = list(evidence_ids)
        clean["view_names"] = [self._candidate_view_name(view)]
        existing = self.candidate_row_table.get(key)
        if existing is not None:
            # K5 emits the same candidate in direct arrays, continuity and
            # candidate-reason mirrors.  Merge only lossless sets/aliases;
            # preserve the first scalar authority rather than guessing.
            merged = dict(existing)
            old_views = list(existing.get("view_names", ())) if isinstance(existing.get("view_names"), (list, tuple)) else []
            merged["view_names"] = list(_unique(tuple(old_views) + (self._candidate_view_name(view),)))
            merged["evidence_ref_ids"] = list(_unique(tuple(existing.get("evidence_ref_ids", ())) + tuple(evidence_ids)))
            old_fields = existing.get("content_ref_fields") if isinstance(existing.get("content_ref_fields"), Mapping) else {}
            merged["content_ref_fields"] = {**dict(old_fields), **body_refs}
            for reason_key in ("candidate_reason", "reason_codes", "supporting_slot_codes", "semantic_support", "uncertainties"):
                old = existing.get(reason_key)
                new = value.get(reason_key)
                if isinstance(old, (list, tuple)) or isinstance(new, (list, tuple)):
                    merged[reason_key] = list(_unique(tuple(old or ()) + tuple(new or ())))
            self.candidate_row_table[key] = merged
        else:
            self.candidate_row_table[key] = clean
        self._candidate_occurrences += 1
        return key

    def _add_source(self, row: Mapping[str, Any], scope: str) -> str:
        value = {str(key): deepcopy(child) for key, child in row.items()}
        _check_no_frozen(value)
        source_id = _first_id(value, "source_ref_id", "id", "message_id", "fragment_id", "claim_id")
        if not source_id:
            source_id = "source_%s" % stable_hash(_without_body(value))[:24]
        source_type = _text(value.get("type"), "source")
        key = _scoped(scope, "source", "%s:%s" % (source_type, source_id))
        body_refs = {
            name: self._add_content(scope, body, source_kind="source", source_id=source_id)
            for name, body in _body_values(value).items()
        }
        clean = _without_body(value)
        clean["source_ref_id"] = source_id
        clean["scope"] = scope
        clean["content_ref_fields"] = body_refs
        existing = self.source_ref_table.get(key)
        if existing is None:
            self.source_ref_table[key] = clean
        elif stable_hash(existing) != stable_hash(clean):
            key = _scoped(scope, "source", "%s:%s:%s" % (source_type, source_id, stable_hash(clean)[:12]))
            self.source_ref_table.setdefault(key, clean)
        return key

    def _add_cue(self, row: Mapping[str, Any], scope: str, *, packet_id: str) -> str:
        value = {str(key): deepcopy(child) for key, child in row.items()}
        _check_no_frozen(value)
        replay_key = _first_id(value, "replay_key", "cue_id", "activation_cue_id")
        if not replay_key:
            replay_key = "cue_%s" % stable_hash(_without_body(value))[:24]
        key = _scoped(scope, "cue", replay_key)
        clean = _without_body(value)
        clean["cue_id"] = replay_key
        clean["scope"] = scope
        clean["packet_ids"] = list(_unique(tuple(clean.get("packet_ids", ())) + (packet_id,)))
        existing = self.activation_cue_table.get(key)
        if existing is None:
            self.activation_cue_table[key] = clean
        else:
            merged = dict(existing)
            merged["packet_ids"] = list(_unique(tuple(existing.get("packet_ids", ())) + (packet_id,)))
            # A replay key is the dedupe key; preserving the original cue
            # payload is safer than replacing it with a later mirror.
            self.activation_cue_table[key] = merged
        return key

    @staticmethod
    def _window_scale(data: Mapping[str, Any]) -> str:
        values: List[Any] = [data.get("window_scale"), data.get("scale")]
        for key in ("window", "dynamic_part", "candidate_context"):
            nested = data.get(key)
            if isinstance(nested, Mapping):
                values.extend((nested.get("window_scale"), nested.get("scale")))
        for value in values:
            if isinstance(value, str) and value:
                upper = value.upper()
                if upper in {"W0", "W1", "W2", "W3", "W4"}:
                    return upper
                if value in {"micro", "turn", "local", "session"}:
                    return {"micro": "W0", "turn": "W1", "local": "W1", "session": "W2"}[value]
        return UNKNOWN

    @staticmethod
    def _boundary_unknown() -> Dict[str, Any]:
        return {
            "start": {"resolution": UNKNOWN, "message_id": UNKNOWN, "evidence_ref": UNKNOWN},
            "end": {"resolution": UNKNOWN, "message_id": UNKNOWN, "evidence_ref": UNKNOWN},
        }

    @staticmethod
    def _boundary_from_source(data: Mapping[str, Any]) -> Dict[str, Any]:
        boundary = data.get("boundary")
        if not isinstance(boundary, Mapping):
            dynamic = data.get("dynamic_part")
            boundary = dynamic.get("boundary") if isinstance(dynamic, Mapping) else None
        if not isinstance(boundary, Mapping):
            return CompactContextPacketStore._boundary_unknown()
        return deepcopy(dict(boundary))

    @staticmethod
    def _normalise_ref(ref: Mapping[str, Any], *, message_id: str, fragment_id: str, role: str) -> Dict[str, Any]:
        # Adjacent refs must remain body-free, ordered and auditable.  Keep
        # relationship metadata but discard nested evidence bodies; evidence
        # IDs live in the global evidence table.
        output: Dict[str, Any] = {
            "message_id": message_id,
            "fragment_id": fragment_id,
            "role": role,
        }
        for key in (
            "relative_to_fragment_id",
            "distance_in_fragment_order",
            "same_segment",
            "time_distance_seconds",
            "candidate_id",
            "candidate_reason",
            "is_primary",
        ):
            if key in ref:
                output[key] = deepcopy(ref[key])
        return output

    @staticmethod
    def _template_hint(key: str) -> str:
        lower = str(key).casefold()
        if lower in _PRIMARY_KEYS:
            return "fragment"
        if lower in _ADJACENT_KEYS:
            return "fragment"
        if lower in _FACT_KEYS:
            return "fact"
        if lower in _EVIDENCE_KEYS:
            return "evidence"
        if lower in _SOURCE_KEYS:
            return "source"
        if lower in _CUE_KEYS:
            return "cue"
        if lower in _CANDIDATE_KEYS:
            return "candidate"
        return ""

    @staticmethod
    def _lookup_row_key(
        row: Mapping[str, Any],
        *,
        kind: str,
        scope: str,
        lookup: Mapping[Tuple[str, str], str],
    ) -> Optional[str]:
        if kind == "fragment":
            identifier = _fragment_id(row)
        elif kind == "fact":
            identifier = _message_id(row)
        elif kind == "candidate":
            identifier = _candidate_id(row)
        elif kind == "evidence":
            identifier = _evidence_source_id(row)
            if identifier:
                identifier = "%s:%s" % (_text(row.get("type"), "evidence"), identifier)
        elif kind == "source":
            identifier = _first_id(row, "source_ref_id", "id", "message_id", "fragment_id", "claim_id")
            if identifier:
                identifier = "%s:%s" % (_text(row.get("type"), "source"), identifier)
        elif kind == "cue":
            identifier = _first_id(row, "replay_key", "cue_id", "activation_cue_id")
        else:
            identifier = None
        if identifier is None:
            return lookup.get((kind, stable_hash(_without_body(row))))
        direct = lookup.get((kind, identifier))
        if direct is not None:
            return direct
        return lookup.get((kind, stable_hash(_without_body(row))))

    def _make_source_template(
        self,
        data: Mapping[str, Any],
        *,
        scope: str,
        row_lookup: Mapping[Tuple[str, str], str],
    ) -> Tuple[str, Any]:
        """Build a body-free ref template used for exact-ish local replay."""

        def templateize(value: Any, hint: str = "", owner: str = "") -> Any:
            if isinstance(value, Mapping):
                if hint in {"fragment", "fact", "candidate", "evidence", "source", "cue"}:
                    row_key = self._lookup_row_key(value, kind=hint, scope=scope, lookup=row_lookup)
                    if row_key is not None:
                        return {"$compact_ref": {"kind": hint, "key": row_key}}
                result: Dict[str, Any] = {}
                for key, child in value.items():
                    key_text = str(key)
                    child_hint = self._template_hint(key_text)
                    if key_text.casefold() in _BODY_KEYS and isinstance(child, (str, bytes)):
                        content_id = self._add_content(scope, child, source_kind="template", source_id=owner or "packet")
                        result[key_text] = {"$compact_content_ref": content_id}
                    elif isinstance(child, (list, tuple)) and child_hint:
                        result[key_text] = [templateize(item, child_hint, owner) for item in child]
                    else:
                        result[key_text] = templateize(child, child_hint, owner)
                return result
            if isinstance(value, (list, tuple)):
                return [templateize(child, hint, owner) for child in value]
            if isinstance(value, (set, frozenset)):
                return [templateize(child, hint, owner) for child in sorted(value, key=str)]
            return deepcopy(value)

        template = templateize(data, owner=str(data.get("packet_id") or data.get("context_packet_id") or "packet"))
        template_id = _scoped(scope, "template", stable_hash(template)[:32])
        self.source_templates[template_id] = template
        return template_id, template

    @staticmethod
    def _materialize_record(record: Mapping[str, Any], content: Mapping[str, CompactContentEntry], tables: Mapping[str, Mapping[str, Mapping[str, Any]]], *, include_body: bool) -> Dict[str, Any]:
        result = deepcopy(dict(record))
        body_fields = result.pop("content_ref_fields", {})
        evidence_ids = result.pop("evidence_ref_ids", [])
        result.pop("roles", None)
        result.pop("view_names", None)
        result.pop("owner_ids", None)
        if include_body and isinstance(body_fields, Mapping):
            for field_name, content_id in body_fields.items():
                entry = content.get(str(content_id))
                if entry is not None:
                    result[str(field_name)] = entry.body
        elif not include_body:
            for field_name in body_fields:
                result.pop(str(field_name), None)
        if evidence_ids:
            evidence_table = tables.get("evidence", {})
            refs: List[Dict[str, Any]] = []
            for evidence_id in evidence_ids:
                row = evidence_table.get(str(evidence_id))
                if row is not None:
                    refs.append(CompactContextPacketStore._materialize_record(row, content, tables, include_body=include_body))
            result["evidence_refs"] = refs
        # Internal scope is useful in the compact table but source rows use
        # their original account/chat columns, if any.
        return result

    def _content_ids_for_refs(
        self,
        *,
        fragment_keys: Sequence[str],
        message_keys: Sequence[str],
        fact_keys: Sequence[str],
        candidate_keys: Sequence[str],
        evidence_keys: Sequence[str],
    ) -> Tuple[str, ...]:
        values: List[str] = []
        for table, keys in (
            (self.fragment_table, fragment_keys),
            (self.message_table, message_keys),
            (self.authoritative_fact_table, fact_keys),
            (self.candidate_row_table, candidate_keys),
            (self.evidence_ref_table, evidence_keys),
        ):
            for key in keys:
                row = table.get(str(key))
                if isinstance(row, Mapping):
                    refs = row.get("content_ref_fields")
                    if isinstance(refs, Mapping):
                        values.extend(str(item) for item in refs.values() if isinstance(item, str))
        return _unique(values)

    def _content_hash_for_ids(self, content_ids: Sequence[str]) -> str:
        payload = []
        for content_id in content_ids:
            entry = self.content_table.get(str(content_id))
            if entry is not None:
                payload.append(entry.to_dict(include_body=True))
        return stable_hash(payload)

    @staticmethod
    def _candidate_refs_for_child(packet: CompactContextPacket, candidate_ids: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
        selected = set(str(item) for item in candidate_ids)
        return {
            str(name): tuple(str(item) for item in values if str(item) in selected)
            for name, values in packet.candidate_view_ids.items()
        }

    def _register_cache_parts(self, packet: CompactContextPacket, content_ids: Sequence[str]) -> None:
        self.cache.put(
            "fixed",
            packet.fixed_hash,
            {
                "packet_version": packet.packet_version,
                "scope": packet.scope,
                "anchor_fragment_ids": list(packet.anchor_fragment_ids),
                "anchor_claim_ids": list(packet.anchor_claim_ids),
                "authoritative_fact_ids": list(packet.authoritative_fact_ids),
                "primary_refs": [deepcopy(dict(item)) for item in packet.primary_refs],
            },
        )
        self.cache.put(
            "dynamic",
            packet.dynamic_hash,
            {
                "packet_version": packet.packet_version,
                "scope": packet.scope,
                "adjacent_refs": [deepcopy(dict(item)) for item in packet.adjacent_refs],
                "candidate_row_ids": list(packet.candidate_row_ids),
                "candidate_view_ids": {str(key): list(value) for key, value in packet.candidate_view_ids.items()},
                "evidence_ref_ids": list(packet.evidence_ref_ids),
                "activation_cue_ids": list(packet.activation_cue_ids),
                "candidate_reason_codes": list(packet.candidate_reason_codes),
                "uncertainties": list(packet.uncertainties),
                "open_snapshot_ref": packet.open_snapshot_ref,
            },
        )
        self.cache.put(
            "content",
            packet.content_hash,
            {
                "packet_version": packet.packet_version,
                "scope": packet.scope,
                "content_ids": list(content_ids),
                # The content cache is a private namespace.  Bodies are only
                # present here, never in fixed/dynamic entries or indexes.
                "content": [self.content_table[item].to_dict(include_body=True) for item in content_ids if item in self.content_table],
            },
        )

    def _ingest_packet(self, packet: Any) -> CompactContextPacket:
        data = _deepcopy_mapping(packet)
        _check_no_frozen(data)
        packet_id = _first_id(data, "packet_id", "context_packet_id")
        if not packet_id:
            raise CompactContextPacketError("packet_id_missing")
        if packet_id != packet_id.strip():
            raise CompactContextPacketError("packet_id_invalid")
        layers = self._source_layers(data)
        account_id, chat_id, scope = self._resolve_scope(data, layers)
        primary_rows = layers["primary"]
        if not primary_rows:
            raise CompactContextPacketError("primary_fragments_empty")
        source_packet_id = packet_id
        if packet_id in self.packet_index:
            # Packet IDs are source-local.  A duplicate source ID in a
            # different scope gets a deterministic scoped ID instead of
            # silently overwriting another chat's material.
            existing = self.packet_index[packet_id]
            if existing.scope != scope:
                packet_id = _scoped(scope, "packet", packet_id)
            elif existing.source_packet_id == source_packet_id:
                # Idempotent re-ingest returns the original leaf/container.
                return existing

        fragment_lookup: Dict[Tuple[str, str], str] = {}
        fragment_key_by_identity: Dict[Tuple[str, str], str] = {}
        primary_refs: List[Dict[str, Any]] = []
        adjacent_refs: List[Dict[str, Any]] = []
        for row in primary_rows:
            key = self._add_fragment(row, scope, role="primary")
            fragment_id = self.fragment_table[key]["fragment_id"]
            message_id = self.fragment_table[key]["message_id"]
            fragment_lookup[("fragment", fragment_id)] = key
            fragment_key_by_identity[("fragment", stable_hash(_without_body(row)))] = key
            primary_refs.append(self._normalise_ref(row, message_id=message_id, fragment_id=fragment_id, role="primary") | {"table_key": key})
        seen_adjacent: Set[str] = set()
        for row in layers["adjacent"]:
            key = self._add_fragment(row, scope, role="adjacent")
            fragment_id = self.fragment_table[key]["fragment_id"]
            message_id = self.fragment_table[key]["message_id"]
            fragment_lookup[("fragment", fragment_id)] = key
            fragment_key_by_identity[("fragment", stable_hash(_without_body(row)))] = key
            # Adjacent is ordered, but repeated K5 mirrors are not separate
            # source fragments.  Preserve the first occurrence only.
            if key in seen_adjacent:
                continue
            seen_adjacent.add(key)
            adjacent_refs.append(self._normalise_ref(row, message_id=message_id, fragment_id=fragment_id, role="adjacent") | {"table_key": key})

        fact_ids: List[str] = []
        fact_lookup: Dict[Tuple[str, str], str] = {}
        for row in layers["facts"]:
            key = self._add_fact(row, scope)
            message_id = self.authoritative_fact_table[key]["message_id"]
            fact_lookup[("fact", message_id)] = key
            fact_lookup[("fact", stable_hash(_without_body(row)))] = key
            if key not in fact_ids:
                fact_ids.append(key)

        # A message can be represented by a full message row, a fact, or a
        # fragment body.  The table always retains one body reference per
        # distinct body and never materialises duplicate packet copies.
        message_keys: List[str] = []
        explicit_messages = _list_of_mappings(data.get("messages")) if data.get("messages") is not None else []
        for row in explicit_messages:
            key = self._add_message(row, scope, fragment_keys=(), primary=False)
            if key not in message_keys:
                message_keys.append(key)
        for row in primary_rows:
            fragment_id = _fragment_id(row) or ""
            frag_key = fragment_lookup.get(("fragment", fragment_id))
            message_id = _message_id(row) or UNKNOWN
            message_row = dict(row)
            fact_key = fact_lookup.get(("fact", message_id))
            if fact_key is not None:
                # Facts carry authoritative metadata; fragment supplies the
                # body fallback when a source message row is absent.
                message_row.update({key: value for key, value in self.authoritative_fact_table[fact_key].items() if key not in {"scope", "content_ref_fields", "evidence_ref_ids"}})
            key = self._add_message(message_row, scope, fragment_keys=(frag_key,) if frag_key else (), primary=True)
            if key not in message_keys:
                message_keys.append(key)
        for row in layers["adjacent"]:
            fragment_id = _fragment_id(row) or ""
            frag_key = fragment_lookup.get(("fragment", fragment_id))
            message_id = _message_id(row) or UNKNOWN
            # Do not let an adjacent projection overwrite a primary message;
            # it can only add a body fallback or fragment reference.
            existing_fact = fact_lookup.get(("fact", message_id))
            message_row = dict(row)
            if existing_fact is not None:
                message_row.update({key: value for key, value in self.authoritative_fact_table[existing_fact].items() if key not in {"scope", "content_ref_fields", "evidence_ref_ids"}})
            key = self._add_message(message_row, scope, fragment_keys=(frag_key,) if frag_key else (), primary=False)
            if key not in message_keys:
                message_keys.append(key)
        for fact_id in fact_ids:
            fact = self.authoritative_fact_table[fact_id]
            message_id = str(fact.get("message_id", UNKNOWN))
            key = self._add_message(fact, scope, fragment_keys=(), primary=False)
            if key not in message_keys:
                message_keys.append(key)

        candidate_ids: List[str] = []
        candidate_view_ids: Dict[str, List[str]] = {}
        candidate_lookup: Dict[Tuple[str, str], str] = {}
        candidate_rows = layers["candidates"]
        for view, row in candidate_rows:
            key = self._add_candidate(row, scope, view=view)
            candidate_id = self.candidate_row_table[key]["candidate_id"]
            candidate_lookup[("candidate", candidate_id)] = key
            candidate_lookup[("candidate", stable_hash(_without_body(row)))] = key
            if key not in candidate_ids:
                candidate_ids.append(key)
            view_name = self._candidate_view_name(view)
            candidate_view_ids.setdefault(view_name, [])
            if key not in candidate_view_ids[view_name]:
                candidate_view_ids[view_name].append(key)

        evidence_ids: List[str] = []
        for row in layers["evidence"]:
            evidence_ids.extend(self._register_evidence_rows((row,), scope))
        for candidate_key in candidate_ids:
            row = self.candidate_row_table.get(candidate_key, {})
            evidence_ids.extend(str(item) for item in row.get("evidence_ref_ids", ()) if isinstance(item, str))
        for fact_key in fact_ids:
            row = self.authoritative_fact_table.get(fact_key, {})
            evidence_ids.extend(str(item) for item in row.get("evidence_ref_ids", ()) if isinstance(item, str))
        evidence_ids = list(_unique(evidence_ids))

        source_ids: List[str] = []
        for row in layers["sources"]:
            key = self._add_source(row, scope)
            if key not in source_ids:
                source_ids.append(key)
        cue_ids: List[str] = []
        for row in layers["cues"]:
            key = self._add_cue(row, scope, packet_id=packet_id)
            if key not in cue_ids:
                cue_ids.append(key)

        # Register every row kind in a single lookup so the lossless source
        # template can replace repeated K5 mirrors with references.
        row_lookup: Dict[Tuple[str, str], str] = {}
        for key, row in self.fragment_table.items():
            if row.get("scope") == scope:
                row_lookup[("fragment", str(row.get("fragment_id")))] = key
        for key, row in self.authoritative_fact_table.items():
            if row.get("scope") == scope:
                row_lookup[("fact", str(row.get("message_id")))] = key
        for key, row in self.candidate_row_table.items():
            if row.get("scope") == scope:
                row_lookup[("candidate", str(row.get("candidate_id")))] = key
        for key, row in self.evidence_ref_table.items():
            if row.get("scope") == scope:
                row_lookup[("evidence", "%s:%s" % (_text(row.get("type"), "evidence"), str(row.get("source_ref_id"))))] = key
                row_lookup[("evidence", str(row.get("evidence_id")))] = key
        for key, row in self.source_ref_table.items():
            if row.get("scope") == scope:
                row_lookup[("source", "%s:%s" % (_text(row.get("type"), "source"), str(row.get("source_ref_id"))))] = key
        for key, row in self.activation_cue_table.items():
            if row.get("scope") == scope:
                row_lookup[("cue", str(row.get("cue_id")))] = key
        # Add body-free identity fallbacks for rows without source IDs.
        for key, row in self.fragment_table.items():
            row_lookup.setdefault(("fragment", stable_hash(_without_body(row))), key)
        for key, row in self.authoritative_fact_table.items():
            row_lookup.setdefault(("fact", stable_hash(_without_body(row))), key)
        for key, row in self.candidate_row_table.items():
            row_lookup.setdefault(("candidate", stable_hash(_without_body(row))), key)
        for key, row in self.evidence_ref_table.items():
            row_lookup.setdefault(("evidence", stable_hash(_without_body(row))), key)

        template_id, _ = self._make_source_template(data, scope=scope, row_lookup=row_lookup)
        anchor_fragment_ids = _unique(
            str(item.get("fragment_id")) for item in primary_refs if isinstance(item.get("fragment_id"), str)
        )
        source_message_ids = _unique(str(item.get("message_id")) for item in primary_refs if isinstance(item.get("message_id"), str))
        context_message_ids = _unique(
            str(item.get("message_id")) for item in adjacent_refs if isinstance(item.get("message_id"), str) and item.get("message_id") not in source_message_ids
        )
        # Authorities not represented by a fragment remain metadata-only
        # messages, and are included in provider message count/ref material.
        all_known_messages = _unique(
            list(source_message_ids)
            + list(context_message_ids)
            + [str(self.authoritative_fact_table[item].get("message_id", UNKNOWN)) for item in fact_ids]
        )
        for message_id in all_known_messages:
            if message_id not in source_message_ids and message_id not in context_message_ids:
                context_message_ids = context_message_ids + (message_id,)

        anchor_claim_ids = _unique(
            str(item) for item in (data.get("claim_ids") or ()) if isinstance(item, str)
        )
        candidate_reason_codes: List[str] = []
        for value in (data.get("candidate_reason"), self._nested_mapping(data, "dynamic_part").get("candidate_reason")):
            if isinstance(value, (list, tuple)):
                candidate_reason_codes.extend(str(item) for item in value if isinstance(item, str) and item)
        uncertainties = _unique(str(item) for item in (data.get("uncertainties") or ()) if isinstance(item, str))

        open_candidate_ids = tuple(candidate_view_ids.get("open_threads", ()))
        snapshot_payload = {
            "source_packet_id": source_packet_id,
            "scope": scope,
            "open_thread_ids": list(open_candidate_ids),
            "activation_cue_ids": list(cue_ids),
            "anchor_fragment_ids": list(anchor_fragment_ids),
            "uncertainties": list(uncertainties),
        }
        open_snapshot_ref = _scoped(scope, "open_snapshot", stable_hash(snapshot_payload)[:32])
        self.open_snapshot_table.setdefault(
            open_snapshot_ref,
            {
                "open_snapshot_id": open_snapshot_ref,
                "scope": scope,
                "open_thread_candidate_ids": list(open_candidate_ids),
                "activation_cue_ids": list(cue_ids),
                "anchor_fragment_ids": list(anchor_fragment_ids),
                "uncertainties": list(uncertainties),
                "open_boundary": True,
            },
        )

        primary_fragment_keys = [str(item.get("table_key")) for item in primary_refs if item.get("table_key")]
        adjacent_fragment_keys = [str(item.get("table_key")) for item in adjacent_refs if item.get("table_key")]
        content_ids = self._content_ids_for_refs(
            fragment_keys=primary_fragment_keys + adjacent_fragment_keys,
            message_keys=message_keys,
            fact_keys=fact_ids,
            candidate_keys=candidate_ids,
            evidence_keys=evidence_ids,
        )
        boundary = self._boundary_from_source(data)
        source_fixed = data.get("fixed_part")
        source_dynamic = data.get("dynamic_part")
        source_fixed_hash = stable_hash(_without_body(source_fixed if isinstance(source_fixed, Mapping) else {}))
        source_dynamic_hash = stable_hash(_without_body(source_dynamic if isinstance(source_dynamic, Mapping) else {}))
        fixed_payload = {
            "packet_version": self.packet_version,
            "scope": scope,
            "source_packet_id": source_packet_id,
            "source_fixed_hash": source_fixed_hash,
            "anchor_fragment_ids": list(anchor_fragment_ids),
            "anchor_claim_ids": list(anchor_claim_ids),
            "primary_refs": deepcopy(primary_refs),
            "authoritative_fact_ids": list(fact_ids),
            "source_ref_ids": list(source_ids),
        }
        dynamic_payload = {
            "packet_version": self.packet_version,
            "scope": scope,
            "window_scale": self._window_scale(data),
            "source_dynamic_hash": source_dynamic_hash,
            "adjacent_refs": deepcopy(adjacent_refs),
            "source_message_ids": list(source_message_ids),
            "context_message_ids": list(context_message_ids),
            "candidate_row_ids": list(candidate_ids),
            "candidate_view_ids": {key: list(value) for key, value in candidate_view_ids.items()},
            "evidence_ref_ids": list(evidence_ids),
            "activation_cue_ids": list(cue_ids),
            "candidate_reason_codes": list(candidate_reason_codes),
            "uncertainties": list(uncertainties),
            "open_snapshot_ref": open_snapshot_ref,
            "boundary": deepcopy(boundary),
        }
        fixed_hash = stable_hash(fixed_payload)
        dynamic_hash = stable_hash(dynamic_payload)
        content_hash = self._content_hash_for_ids(content_ids)
        packet_hash = stable_hash(
            {
                "packet_version": self.packet_version,
                "fixed_hash": fixed_hash,
                "dynamic_hash": dynamic_hash,
                "content_hash": content_hash,
            }
        )
        compact_id = packet_id
        if compact_id in self.packet_index:
            compact_id = _scoped(scope, "packet", "%s:%s" % (packet_id, packet_hash[:16]))
        cache_key = self.cache_key_for_hashes(
            fixed_hash=fixed_hash,
            dynamic_hash=dynamic_hash,
            content_hash=content_hash,
            packet_hash=packet_hash,
        )
        compact = CompactContextPacket(
            packet_id=compact_id,
            source_packet_id=source_packet_id,
            account_id=account_id,
            chat_id=chat_id,
            scope=scope,
            window_scale=self._window_scale(data),
            anchor_fragment_ids=anchor_fragment_ids,
            anchor_claim_ids=anchor_claim_ids,
            primary_refs=tuple(primary_refs),
            adjacent_refs=tuple(adjacent_refs),
            source_message_ids=source_message_ids,
            context_message_ids=context_message_ids,
            authoritative_fact_ids=tuple(fact_ids),
            candidate_row_ids=tuple(candidate_ids),
            evidence_ref_ids=tuple(evidence_ids),
            source_ref_ids=tuple(source_ids),
            activation_cue_ids=tuple(cue_ids),
            candidate_view_ids={key: tuple(value) for key, value in candidate_view_ids.items()},
            candidate_reason_codes=tuple(candidate_reason_codes),
            uncertainties=uncertainties,
            open_snapshot_ref=open_snapshot_ref,
            boundary=boundary,
            source_template_id=template_id,
            fixed_hash=fixed_hash,
            dynamic_hash=dynamic_hash,
            content_hash=content_hash,
            source_fixed_hash=source_fixed_hash,
            source_dynamic_hash=source_dynamic_hash,
            packet_hash=packet_hash,
            cache_key=cache_key,
            packet_version=self.packet_version,
        )
        self.packet_index[compact.packet_id] = compact
        self._source_sizes[compact.packet_id] = len(canonical_json(data))
        self._register_cache_parts(compact, content_ids)
        return compact

    def cache_key_for_hashes(
        self,
        *,
        fixed_hash: str,
        dynamic_hash: str,
        content_hash: str,
        packet_hash: str = "",
        packet_version: Optional[str] = None,
    ) -> str:
        return "compact-context-packet:%s" % stable_hash(
            {
                "schema_version": COMPACT_STORE_SCHEMA_VERSION,
                "packet_version": packet_version or self.packet_version,
                "fixed_hash": str(fixed_hash),
                "dynamic_hash": str(dynamic_hash),
                "content_hash": str(content_hash),
                "packet_hash": str(packet_hash),
            }
        )

    def cache_key_for(self, packet: Union[str, CompactContextPacket], *, part: str = "packet") -> str:
        resolved = self.get_packet(packet)
        if part == "fixed":
            return resolved.fixed_hash
        if part == "dynamic":
            return resolved.dynamic_hash
        if part == "content":
            return resolved.content_hash
        return resolved.cache_key or self.cache_key_for_hashes(
            fixed_hash=resolved.fixed_hash,
            dynamic_hash=resolved.dynamic_hash,
            content_hash=resolved.content_hash,
            packet_hash=resolved.packet_hash,
            packet_version=resolved.packet_version,
        )

    def get_packet(self, packet: Union[str, CompactContextPacket]) -> CompactContextPacket:
        if isinstance(packet, CompactContextPacket):
            return packet
        packet_id = str(packet)
        try:
            return self.packet_index[packet_id]
        except KeyError as exc:
            raise CompactContextPacketError("packet_not_found") from exc

    def _table_maps(self) -> Dict[str, Mapping[str, Mapping[str, Any]]]:
        return {
            "fragment": self.fragment_table,
            "message": self.message_table,
            "fact": self.authoritative_fact_table,
            "candidate": self.candidate_row_table,
            "evidence": self.evidence_ref_table,
            "source": self.source_ref_table,
            "cue": self.activation_cue_table,
        }

    def _entry_content_ids(self, packet: CompactContextPacket) -> Tuple[str, ...]:
        fragment_keys = [str(item.get("table_key")) for item in packet.primary_refs + packet.adjacent_refs if item.get("table_key")]
        message_keys = [_scoped(packet.scope, "message", message_id) for message_id in packet.all_message_ids]
        return self._content_ids_for_refs(
            fragment_keys=fragment_keys,
            message_keys=message_keys,
            fact_keys=packet.authoritative_fact_ids,
            candidate_keys=packet.candidate_row_ids,
            evidence_keys=packet.evidence_ref_ids,
        )

    def _fragment_record_for_ref(self, packet: CompactContextPacket, ref: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        key = ref.get("table_key")
        if isinstance(key, str) and key in self.fragment_table:
            return self.fragment_table[key]
        fragment_id = ref.get("fragment_id")
        if isinstance(fragment_id, str):
            return self.fragment_table.get(_scoped(packet.scope, "fragment", fragment_id))
        return None

    def _message_record_for_id(self, packet: CompactContextPacket, message_id: str) -> Optional[Mapping[str, Any]]:
        return self.message_table.get(_scoped(packet.scope, "message", message_id))

    def _fact_for_message(self, packet: CompactContextPacket, message_id: str) -> Optional[Mapping[str, Any]]:
        message_id = str(message_id or "")
        for fact_key in packet.authoritative_fact_ids:
            row = self.authoritative_fact_table.get(str(fact_key))
            if row is None:
                continue
            aliases: List[str] = []
            for key in ("message_id", "source_message_id", "message_alias", "source_message_alias", "message_handle", "source_message_handle", "alias", "handle"):
                value = row.get(key)
                if isinstance(value, str) and value:
                    aliases.append(value)
            for key in ("message_aliases", "aliases", "message_handles", "handles"):
                values = row.get(key)
                if isinstance(values, (list, tuple, set, frozenset)):
                    aliases.extend(str(value) for value in values if value not in (None, ""))
            if message_id in set(aliases):
                return row
        return None

    def _provider_has_evidence_span(self, packet: CompactContextPacket, ref: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
        """Require a concrete span before a retained primary is projected."""

        if _compact_valid_span(row.get("span")) or _compact_valid_span(ref.get("span")):
            return True
        evidence_ids: List[str] = []
        for source in (row, ref):
            values = source.get("evidence_ref_ids")
            if isinstance(values, (list, tuple, set, frozenset)):
                evidence_ids.extend(str(value) for value in values if value not in (None, ""))
            nested = source.get("evidence_refs")
            if isinstance(nested, (list, tuple)):
                for evidence in nested:
                    if isinstance(evidence, Mapping) and _compact_valid_span(evidence.get("span")):
                        return True
        for evidence_id in evidence_ids:
            evidence = self.evidence_ref_table.get(str(evidence_id))
            if isinstance(evidence, Mapping) and _compact_valid_span(evidence.get("span")):
                return True
        message_id = str(row.get("message_id") or ref.get("message_id") or "")
        fragment_id = str(row.get("fragment_id") or ref.get("fragment_id") or "")
        for evidence in self.evidence_ref_table.values():
            if not isinstance(evidence, Mapping):
                continue
            if message_id and str(evidence.get("message_id") or "") == message_id:
                if _compact_valid_span(evidence.get("span")):
                    return True
            if fragment_id and str(evidence.get("fragment_id") or "") == fragment_id:
                if _compact_valid_span(evidence.get("span")):
                    return True
        return False

    def _provider_context_only(self, packet: CompactContextPacket, ref: Mapping[str, Any]) -> bool:
        """Apply the K6 provider-role projection to one retained ref."""

        record = self._fragment_record_for_ref(packet, ref)
        row = dict(record) if isinstance(record, Mapping) else dict(ref)
        message_id = str(row.get("message_id") or ref.get("message_id") or "")
        # ``unknown`` is a storage fallback, never an authoritative binding.
        if not message_id or message_id.casefold() == UNKNOWN:
            return True
        fact = self._fact_for_message(packet, message_id)
        # A retained fragment can be rich and substantive, but without an
        # authoritative message metadata row it is a residual/candidate cue.
        # Keep it in context rather than allowing the cue to create a primary.
        if not isinstance(fact, Mapping):
            return True
        message_type = fact.get("message_type", row.get("message_type", "text"))
        if _compact_authority_blocks_cue(row, fact):
            # Typed media/system/event/social authority is context unless a
            # direct human caption is paired with a positive semantic role.
            # Candidate/merged fields are intentionally not inspected here.
            authority_labels = _compact_provider_labels(fact)
            valid_role = bool(authority_labels & _PROVIDER_POSITIVE_ROLES) if authority_labels else _compact_positive_role(row)
            direct = _compact_direct_human_presence(row) or _compact_direct_human_presence(fact)
            if not (direct and valid_role):
                return True
        if not self._provider_has_evidence_span(packet, ref, row):
            return True
        return _compact_context_only(row, message_type=message_type)

    def _provider_primary_message_ids(self, packet: CompactContextPacket) -> Tuple[str, ...]:
        """Return semantic primary message IDs without changing stored refs."""

        semantic_message_ids: List[str] = []
        for ref in packet.primary_refs:
            message_id = ref.get("message_id")
            if not isinstance(message_id, str) or not message_id or message_id.casefold() == UNKNOWN:
                continue
            if not self._provider_context_only(packet, ref):
                semantic_message_ids.append(message_id)
        return _unique(semantic_message_ids)

    def _materialize_fragment(self, packet: CompactContextPacket, ref: Mapping[str, Any], *, include_body: bool = True) -> Dict[str, Any]:
        record = self._fragment_record_for_ref(packet, ref)
        if record is None:
            return {"fragment_id": ref.get("fragment_id", UNKNOWN), "message_id": ref.get("message_id", UNKNOWN)}
        return self._materialize_record(record, self.content_table, self._table_maps(), include_body=include_body)

    def _materialize_candidate(self, key: str, *, include_body: bool = True) -> Dict[str, Any]:
        record = self.candidate_row_table.get(str(key))
        if record is None:
            raise CompactContextPacketError("candidate_ref_missing")
        return self._materialize_record(record, self.content_table, self._table_maps(), include_body=include_body)

    def _materialize_evidence(self, key: str, *, include_body: bool = True) -> Dict[str, Any]:
        record = self.evidence_ref_table.get(str(key))
        if record is None:
            raise CompactContextPacketError("evidence_ref_missing")
        return self._materialize_record(record, self.content_table, self._table_maps(), include_body=include_body)

    def _materialize_authoritative_fact(self, key: str, *, include_body: bool = False) -> Dict[str, Any]:
        record = self.authoritative_fact_table.get(str(key))
        if record is None:
            raise CompactContextPacketError("authoritative_fact_ref_missing")
        row = self._materialize_record(record, self.content_table, self._table_maps(), include_body=include_body)
        row.pop("evidence_refs", None)
        row["evidence_ref_ids"] = [
            self._public_id(self.evidence_ref_table[item], item)
            for item in record.get("evidence_ref_ids", ())
            if item in self.evidence_ref_table
        ]
        row["metadata_authoritative"] = True
        return row

    def _materialize_messages(self, packet: CompactContextPacket, *, include_body: bool) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[Dict[str, Any]]]:
        """Return ordered message refs, local handle map and private table."""

        ordered_ids = list(_unique(packet.source_message_ids + packet.context_message_ids))
        # Authorities that are metadata-only may not appear in either list.
        for fact_key in packet.authoritative_fact_ids:
            fact = self.authoritative_fact_table.get(str(fact_key), {})
            message_id = fact.get("message_id")
            if isinstance(message_id, str) and message_id not in ordered_ids:
                ordered_ids.append(message_id)
        content_to_handle: Dict[str, str] = {}
        content_table: List[Dict[str, Any]] = []
        provider_primary_message_ids = self._provider_primary_message_ids(packet)
        provider_primary_ids = set(provider_primary_message_ids)

        def handle_for_content(content_id: Optional[str]) -> Optional[str]:
            if not isinstance(content_id, str) or not content_id:
                return None
            if content_id not in content_to_handle:
                handle = "c%d" % len(content_to_handle)
                content_to_handle[content_id] = handle
                entry = self.content_table.get(content_id)
                if entry is None:
                    raise CompactContextPacketError("content_ref_missing")
                content_table.append(
                    {
                        "handle": handle,
                        "content_id": content_id,
                        "body": entry.body if include_body else None,
                        "body_hash": entry.body_hash,
                    }
                )
            return content_to_handle[content_id]

        messages: List[Dict[str, Any]] = []
        for message_id in ordered_ids:
            record = self._message_record_for_id(packet, message_id) or {}
            refs = record.get("content_ref_fields") if isinstance(record.get("content_ref_fields"), Mapping) else {}
            # Prefer the source's canonical content/content field, then the
            # first fragment body as a local message body fallback.
            content_id: Optional[str] = None
            for name in ("content", "text", "message_text", "body", "text_redacted"):
                value = refs.get(name) if isinstance(refs, Mapping) else None
                if isinstance(value, str):
                    content_id = value
                    break
            fragment_ids = [
                str(item.get("fragment_id"))
                for item in packet.primary_refs + packet.adjacent_refs
                if str(item.get("message_id")) == str(message_id) and item.get("fragment_id")
            ]
            if content_id is None and fragment_ids:
                frag = self.fragment_table.get(_scoped(packet.scope, "fragment", fragment_ids[0]))
                fields = frag.get("content_ref_fields") if isinstance(frag, Mapping) else {}
                if isinstance(fields, Mapping):
                    for name in ("text_redacted", "text", "content", "body"):
                        if isinstance(fields.get(name), str):
                            content_id = fields[name]
                            break
            handle = handle_for_content(content_id)
            message_payload: Dict[str, Any] = {
                "message_id": message_id,
                "content_handle": handle,
                "fragment_ids": fragment_ids,
                "is_primary": message_id in provider_primary_ids,
                "metadata_only": handle is None,
                "authoritative_fact_id": next(
                    (key for key in packet.authoritative_fact_ids if str(self.authoritative_fact_table.get(str(key), {}).get("message_id")) == str(message_id)),
                    None,
                ),
            }
            messages.append(message_payload)
        return messages, content_to_handle, content_table

    def _materialize_fragment_views(
        self,
        packet: CompactContextPacket,
        content_handles: MutableMapping[str, str],
        content_table: List[Dict[str, Any]],
        *,
        include_body: bool,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        def handle_for(record: Mapping[str, Any]) -> Optional[str]:
            fields = record.get("content_ref_fields") if isinstance(record.get("content_ref_fields"), Mapping) else {}
            content_id = None
            for name in ("text_redacted", "text", "content", "body", "message_text"):
                if isinstance(fields.get(name), str):
                    content_id = fields[name]
                    break
            if content_id is None:
                return None
            if content_id not in content_handles:
                handle = "c%d" % len(content_handles)
                content_handles[content_id] = handle
                entry = self.content_table.get(content_id)
                if entry is None:
                    raise CompactContextPacketError("content_ref_missing")
                content_table.append({"handle": handle, "content_id": content_id, "body": entry.body if include_body else None, "body_hash": entry.body_hash})
            return content_handles[content_id]

        primary: List[Dict[str, Any]] = []
        for ref in packet.primary_refs:
            record = self._fragment_record_for_ref(packet, ref) or ref
            payload = self._materialize_record(record, self.content_table, self._table_maps(), include_body=False)
            payload["content_handle"] = handle_for(record)
            payload.pop("evidence_refs", None)
            payload["evidence_ref_ids"] = list(record.get("evidence_ref_ids", ())) if isinstance(record.get("evidence_ref_ids"), (list, tuple)) else []
            payload.pop("text_redacted", None)
            payload.pop("text", None)
            payload["fragment_id"] = ref.get("fragment_id", payload.get("fragment_id", UNKNOWN))
            primary.append(payload)
        adjacent: List[Dict[str, Any]] = []
        for ref in packet.adjacent_refs:
            # Adjacent is stored as refs only; materialisation resolves the
            # selected fragment exactly at send/request time.
            record = self._fragment_record_for_ref(packet, ref) or ref
            payload = self._materialize_record(record, self.content_table, self._table_maps(), include_body=False)
            payload["content_handle"] = handle_for(record)
            payload.pop("evidence_refs", None)
            payload["evidence_ref_ids"] = list(record.get("evidence_ref_ids", ())) if isinstance(record.get("evidence_ref_ids"), (list, tuple)) else []
            payload["fragment_id"] = ref.get("fragment_id", payload.get("fragment_id", UNKNOWN))
            payload.pop("text_redacted", None)
            payload.pop("text", None)
            adjacent.append(payload)
        return primary, adjacent

    @staticmethod
    def _public_id(record: Mapping[str, Any], fallback: str) -> str:
        for key in ("candidate_id", "evidence_id", "source_ref_id", "message_id", "fragment_id", "cue_id"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        return fallback

    def _materialize_candidate_rows(
        self,
        packet: CompactContextPacket,
        content_handles: MutableMapping[str, str],
        content_table: List[Dict[str, Any]],
        *,
        include_body: bool,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for key in packet.candidate_row_ids:
            record = self.candidate_row_table.get(str(key))
            if record is None:
                raise CompactContextPacketError("candidate_ref_missing")
            row = self._materialize_record(record, self.content_table, self._table_maps(), include_body=False)
            row.pop("evidence_refs", None)
            fields = record.get("content_ref_fields") if isinstance(record.get("content_ref_fields"), Mapping) else {}
            handles: Dict[str, str] = {}
            for field_name, content_id in fields.items():
                if not isinstance(content_id, str):
                    continue
                if content_id not in content_handles:
                    handle = "c%d" % len(content_handles)
                    content_handles[content_id] = handle
                    entry = self.content_table.get(content_id)
                    if entry is None:
                        raise CompactContextPacketError("content_ref_missing")
                    content_table.append({"handle": handle, "content_id": content_id, "body": entry.body if include_body else None, "body_hash": entry.body_hash})
                handles[str(field_name)] = content_handles[content_id]
            if handles:
                row["content_handles"] = handles
            row["candidate_id"] = record.get("candidate_id", self._public_id(record, str(key)))
            row["evidence_ref_ids"] = [
                self._public_id(self.evidence_ref_table.get(str(item), {}), str(item))
                for item in record.get("evidence_ref_ids", ())
                if isinstance(item, str)
            ]
            row["candidate_only"] = True
            rows.append(row)
        return rows

    def _materialize_evidence_rows(
        self,
        packet: CompactContextPacket,
        content_handles: MutableMapping[str, str],
        content_table: List[Dict[str, Any]],
        *,
        include_body: bool,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for key in packet.evidence_ref_ids:
            record = self.evidence_ref_table.get(str(key))
            if record is None:
                raise CompactContextPacketError("evidence_ref_missing")
            row = self._materialize_record(record, self.content_table, self._table_maps(), include_body=False)
            row.pop("evidence_refs", None)
            fields = record.get("content_ref_fields") if isinstance(record.get("content_ref_fields"), Mapping) else {}
            handles: Dict[str, str] = {}
            for field_name, content_id in fields.items():
                if not isinstance(content_id, str):
                    continue
                if content_id not in content_handles:
                    handle = "c%d" % len(content_handles)
                    content_handles[content_id] = handle
                    entry = self.content_table.get(content_id)
                    if entry is None:
                        raise CompactContextPacketError("content_ref_missing")
                    content_table.append({"handle": handle, "content_id": content_id, "body": entry.body if include_body else None, "body_hash": entry.body_hash})
                handles[str(field_name)] = content_handles[content_id]
            if handles:
                row["content_handles"] = handles
            row["evidence_id"] = record.get("evidence_id", self._public_id(record, str(key)))
            rows.append(row)
        return rows

    @staticmethod
    def _cue_message_refs(row: Mapping[str, Any]) -> Tuple[str, ...]:
        values: List[str] = []
        for key in (
            "message_id",
            "source_message_id",
            "message_handle",
            "source_message_handle",
            "message_alias",
            "source_message_alias",
        ):
            value = row.get(key)
            if isinstance(value, str) and value:
                values.append(value)
        for key in ("message_ids", "source_message_ids", "message_handles", "message_aliases", "aliases"):
            value = row.get(key)
            if isinstance(value, (list, tuple, set, frozenset)):
                values.extend(str(item) for item in value if item not in (None, ""))
        return _unique(values)

    def _materialize_cues(self, packet: CompactContextPacket) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen_messages: Set[str] = set()
        for key in packet.activation_cue_ids:
            row = self.activation_cue_table.get(str(key))
            if row is not None:
                materialized = self._materialize_record(row, self.content_table, self._table_maps(), include_body=False)
                message_refs = set(self._cue_message_refs(materialized))
                # Activation cues are mirrors across K2/K5 views.  Keep the
                # first deterministic cue for a message and never multiply a
                # provider-facing cue merely because it arrived through two
                # candidate paths.  Cues without a message binding retain
                # their replay-key dedupe semantics.
                if message_refs and message_refs.intersection(seen_messages):
                    continue
                if message_refs:
                    seen_messages.update(message_refs)
                rows.append(materialized)
        return rows

    def _materialize_model_packet(
        self,
        packet: CompactContextPacket,
        *,
        include_content: bool = True,
    ) -> Tuple[Dict[str, Any], CompactPacketMaterialStats]:
        messages, content_handles, content_table = self._materialize_messages(packet, include_body=include_content)
        primary, adjacent = self._materialize_fragment_views(
            packet,
            content_handles,
            content_table,
            include_body=include_content,
        )
        provider_primary_message_ids = self._provider_primary_message_ids(packet)
        provider_primary_ids = set(provider_primary_message_ids)
        retained_primary = deepcopy(primary)
        retained_context: List[Dict[str, Any]] = []
        semantic_primary: List[Dict[str, Any]] = []
        for index, row in enumerate(primary):
            ref = packet.primary_refs[index] if index < len(packet.primary_refs) else row
            message_id = str(row.get("message_id") or ref.get("message_id") or "")
            context_only = self._provider_context_only(packet, ref)
            if message_id in provider_primary_ids and not context_only:
                semantic_primary.append(row)
            else:
                retained_context.append(row)
        provider_adjacent: List[Dict[str, Any]] = list(adjacent)
        adjacent_ids = {str(row.get("fragment_id")) for row in provider_adjacent}
        for row in retained_context:
            fragment_id = str(row.get("fragment_id") or "")
            if fragment_id and fragment_id not in adjacent_ids:
                provider_adjacent.append(row)
                adjacent_ids.add(fragment_id)
        provider_context_ids = _unique(
            list(packet.context_message_ids)
            + [str(row.get("message_id")) for row in retained_context if isinstance(row.get("message_id"), str)]
        )
        provider_context_ids = tuple(item for item in provider_context_ids if item not in provider_primary_ids)
        candidates = self._materialize_candidate_rows(
            packet,
            content_handles,
            content_table,
            include_body=include_content,
        )
        evidence = self._materialize_evidence_rows(
            packet,
            content_handles,
            content_table,
            include_body=include_content,
        )
        facts = [self._materialize_authoritative_fact(key, include_body=False) for key in packet.authoritative_fact_ids]
        source_refs = [
            self._materialize_record(self.source_ref_table[key], self.content_table, self._table_maps(), include_body=False)
            for key in packet.source_ref_ids
            if key in self.source_ref_table
        ]
        public_candidate_views = {
            str(view): [
                self._public_id(self.candidate_row_table[key], str(key))
                for key in keys
                if key in self.candidate_row_table
            ]
            for view, keys in packet.candidate_view_ids.items()
        }
        candidate_context: Dict[str, Any] = {
            # Candidate rows are physically emitted once.  Views are IDs only;
            # never repeat a candidate object under each K1/K2 alias.
            "candidate_rows": deepcopy(candidates),
            "candidate_view_ids": public_candidate_views,
            "primary_fragments": deepcopy(semantic_primary),
            "adjacent_context": deepcopy(provider_adjacent),
            # K2/K6 retention remains recoverable from the compact packet
            # refs; keep only IDs here so the provider envelope does not
            # duplicate the context rows already carried by adjacent_context.
            "retained_context_fragment_ids": [
                str(row.get("fragment_id"))
                for row in retained_context
                if row.get("fragment_id")
            ],
            "activation_cues": self._materialize_cues(packet),
            "uncertainties": list(packet.uncertainties),
            "candidate_reason_codes": list(packet.candidate_reason_codes),
            "candidate_only": True,
        }
        authority = {
            "message_metadata": deepcopy(facts),
            "source_refs": deepcopy(source_refs),
            "evidence_ref_ids": [row.get("evidence_id", UNKNOWN) for row in evidence],
            "scope": {"account_id": packet.account_id, "chat_id": packet.chat_id},
            "authority_only": True,
        }
        snapshot_record = self.open_snapshot_table.get(packet.open_snapshot_ref, {})
        # Open snapshots are deliberately link-only in a provider request;
        # the authoritative table remains available for local reactivation.
        open_snapshot = {
            "open_snapshot_id": packet.open_snapshot_ref,
            "open_boundary": bool(snapshot_record.get("open_boundary", True)) if isinstance(snapshot_record, Mapping) else True,
            "open_thread_candidate_ids": list(snapshot_record.get("open_thread_candidate_ids", ())) if isinstance(snapshot_record, Mapping) else [],
            "activation_cue_ids": list(snapshot_record.get("activation_cue_ids", ())) if isinstance(snapshot_record, Mapping) else [],
            "uncertainties": list(snapshot_record.get("uncertainties", ())) if isinstance(snapshot_record, Mapping) else list(packet.uncertainties),
        }
        open_snapshot["predecessor_refs"] = list(packet.predecessor_refs)
        open_snapshot["successor_refs"] = list(packet.successor_refs)
        model_packet: Dict[str, Any] = {
            "schema_version": COMPACT_CONTEXT_PACKET_VERSION,
            "packet_id": packet.packet_id,
            "source_packet_id": packet.source_packet_id,
            "scope": packet.scope,
            "account_id": packet.account_id,
            "chat_id": packet.chat_id,
            # ``message_ids`` is the complete selected window.  The explicit
            # primary subset is the semantic Stage-A projection; context-only
            # K2 refs remain in the window and in the adjacent marker.
            "message_ids": list(packet.all_message_ids),
            "primary_message_ids": list(provider_primary_message_ids),
            "context_message_ids": list(provider_context_ids),
            "messages": messages,
            "primary_fragment_ids": [str(row.get("fragment_id")) for row in semantic_primary if row.get("fragment_id")],
            "evidence_ids": [self._public_id(self.evidence_ref_table[key], str(key)) for key in packet.evidence_ref_ids if key in self.evidence_ref_table],
            "evidence": evidence,
            "content_table": content_table,
            "authoritative_facts": authority,
            "candidate_context": candidate_context,
            "open_snapshot": open_snapshot,
            "boundary": deepcopy(dict(packet.boundary)),
            "status": packet.status,
            # A normal open leaf needs only the snapshot link.  Pending leaves
            # carry the complete reactivation payload so no work is lost.
            "reactivation": deepcopy(dict(packet.reactivation)) if packet.status == "pending" else {},
            "metadata": {
                "compact_store_schema_version": COMPACT_STORE_SCHEMA_VERSION,
                "compact_packet_version": packet.packet_version,
                "source_packet_id": packet.source_packet_id,
                "source_packet_hash": packet.packet_hash,
                "fixed_hash": packet.fixed_hash,
                "dynamic_hash": packet.dynamic_hash,
                "content_hash": packet.content_hash,
                "packet_hash": packet.packet_hash,
                "cache_key": packet.cache_key,
                "open_snapshot_ref": packet.open_snapshot_ref,
                "parent_packet_id": packet.parent_packet_id,
                "predecessor_refs": list(packet.predecessor_refs),
                "successor_refs": list(packet.successor_refs),
            },
        }
        if include_content:
            # ``content_table`` above is the sole body-bearing location.  The
            # request envelope intentionally has no ``content``/``text`` key.
            pass
        else:
            for row in model_packet["content_table"]:
                row["body"] = None
        canonical_chars = len(canonical_json(model_packet))
        stats = CompactPacketMaterialStats(
            input_token_proxy=(canonical_chars + 3) // 4,
            canonical_chars=canonical_chars,
            message_count=len(messages),
            candidate_row_count=len(candidates),
            evidence_ref_count=len(evidence),
        )
        return model_packet, stats

    def _final_model_packet(
        self,
        packet: CompactContextPacket,
        *,
        include_content: bool = True,
    ) -> Tuple[Dict[str, Any], CompactPacketMaterialStats]:
        """Build the exact envelope used for capacity accounting.

        The request-level ``material_stats`` field is part of the provider
        input, so checking only the inner model would under-count every leaf.
        This helper appends the same final fields as the public materializer,
        measures canonical JSON, and writes the resulting stats back once.
        """

        model, inner_stats = self._materialize_model_packet(packet, include_content=include_content)
        if not model.get("context_message_ids"):
            model.pop("context_message_ids", None)
        if not packet.reactivation:
            model.pop("reactivation", None)
        if packet.pending_reason:
            model["pending_reason"] = packet.pending_reason
        if packet.status == "pending":
            # Pending leaves are local recovery records, never provider
            # requests.  Their complete reactivation payload remains in the
            # compact store; the envelope already carries the same snapshot
            # and predecessor/successor links.  Avoid emitting those repeated
            # long handles here so even an inspection materialisation stays
            # within the provider-size bound.
            reactivation = model.get("reactivation")
            if isinstance(reactivation, Mapping):
                model["reactivation"] = {
                    key: reactivation[key]
                    for key in ("reactivation_required", "reason_code")
                    if key in reactivation
                }
            metadata = model.get("metadata")
            if isinstance(metadata, MutableMapping):
                metadata.pop("predecessor_refs", None)
                metadata.pop("successor_refs", None)
        model["material_stats"] = inner_stats.to_dict()
        canonical_chars = len(canonical_json(model))
        stats = CompactPacketMaterialStats(
            input_token_proxy=(canonical_chars + 3) // 4,
            canonical_chars=canonical_chars,
            message_count=inner_stats.message_count,
            candidate_row_count=inner_stats.candidate_row_count,
            evidence_ref_count=inner_stats.evidence_ref_count,
        )
        model["material_stats"] = stats.to_dict()
        return model, stats

    def capacity_for(
        self,
        packet: Union[str, CompactContextPacket],
        *,
        capacity: Optional[CompactCapacity] = None,
    ) -> CompactPacketCapacity:
        resolved = self.get_packet(packet)
        target = capacity or self.capacity
        if resolved.is_container:
            return CompactPacketCapacity("pending", CompactPacketMaterialStats(0, 0, 0, 0, 0), target, "container_not_sendable")
        _, stats = self._final_model_packet(resolved, include_content=True)
        violations = (
            stats.input_token_proxy > target.max_input_token_proxy,
            stats.message_count > target.max_messages,
            stats.candidate_row_count > target.max_candidate_rows,
            stats.evidence_ref_count > target.max_evidence_refs,
        )
        if any(violations):
            return CompactPacketCapacity("over_capacity", stats, target, "capacity_exceeded")
        return CompactPacketCapacity("ok", stats, target, "")

    def materialize_stage_packet(
        self,
        packet: Union[str, CompactContextPacket],
        *,
        capacity: Optional[CompactCapacity] = None,
        allow_over_capacity: bool = False,
        include_content: bool = True,
    ) -> Dict[str, Any]:
        resolved = self.get_packet(packet)
        cap = self.capacity_for(resolved, capacity=capacity)
        if resolved.status == "pending" and not allow_over_capacity:
            raise CompactCapacityError(cap.stats.to_dict(), cap.limits)
        if resolved.is_container:
            raise CompactContextPacketError("container_not_sendable")
        if not cap.ok and not allow_over_capacity:
            raise CompactCapacityError(cap.stats.to_dict(), cap.limits)
        model, stats = self._final_model_packet(resolved, include_content=include_content)
        return model

    def _rehash_packet(
        self,
        packet: CompactContextPacket,
        *,
        packet_id: Optional[str] = None,
        status: Optional[str] = None,
        pending_reason: Optional[str] = None,
        parent_packet_id: Optional[str] = None,
        predecessor_refs: Optional[Sequence[str]] = None,
        successor_refs: Optional[Sequence[str]] = None,
        subpacket_ids: Optional[Sequence[str]] = None,
        is_container: Optional[bool] = None,
        boundary: Optional[Mapping[str, Any]] = None,
    ) -> CompactContextPacket:
        content_ids = self._entry_content_ids(packet)
        fixed_payload = {
            "packet_version": packet.packet_version,
            "scope": packet.scope,
            "source_packet_id": packet.source_packet_id,
            "source_fixed_hash": packet.source_fixed_hash,
            "anchor_fragment_ids": list(packet.anchor_fragment_ids),
            "anchor_claim_ids": list(packet.anchor_claim_ids),
            "primary_refs": [deepcopy(dict(item)) for item in packet.primary_refs],
            "authoritative_fact_ids": list(packet.authoritative_fact_ids),
            "source_ref_ids": list(packet.source_ref_ids),
        }
        dynamic_payload = {
            "packet_version": packet.packet_version,
            "scope": packet.scope,
            "window_scale": packet.window_scale,
            "source_dynamic_hash": packet.source_dynamic_hash,
            "adjacent_refs": [deepcopy(dict(item)) for item in packet.adjacent_refs],
            "source_message_ids": list(packet.source_message_ids),
            "context_message_ids": list(packet.context_message_ids),
            "candidate_row_ids": list(packet.candidate_row_ids),
            "candidate_view_ids": {str(key): list(value) for key, value in packet.candidate_view_ids.items()},
            "evidence_ref_ids": list(packet.evidence_ref_ids),
            "activation_cue_ids": list(packet.activation_cue_ids),
            "candidate_reason_codes": list(packet.candidate_reason_codes),
            "uncertainties": list(packet.uncertainties),
            "open_snapshot_ref": packet.open_snapshot_ref,
            "boundary": deepcopy(dict(boundary if boundary is not None else packet.boundary)),
        }
        fixed_hash = stable_hash(fixed_payload)
        dynamic_hash = stable_hash(dynamic_payload)
        content_hash = self._content_hash_for_ids(content_ids)
        packet_hash = stable_hash(
            {
                "packet_version": packet.packet_version,
                "fixed_hash": fixed_hash,
                "dynamic_hash": dynamic_hash,
                "content_hash": content_hash,
            }
        )
        new_packet = replace(
            packet,
            packet_id=packet_id or packet.packet_id,
            fixed_hash=fixed_hash,
            dynamic_hash=dynamic_hash,
            content_hash=content_hash,
            packet_hash=packet_hash,
            cache_key=self.cache_key_for_hashes(
                fixed_hash=fixed_hash,
                dynamic_hash=dynamic_hash,
                content_hash=content_hash,
                packet_hash=packet_hash,
                packet_version=packet.packet_version,
            ),
            status=packet.status if status is None else status,
            pending_reason=packet.pending_reason if pending_reason is None else pending_reason,
            parent_packet_id=packet.parent_packet_id if parent_packet_id is None else parent_packet_id,
            predecessor_refs=packet.predecessor_refs if predecessor_refs is None else tuple(predecessor_refs),
            successor_refs=packet.successor_refs if successor_refs is None else tuple(successor_refs),
            subpacket_ids=packet.subpacket_ids if subpacket_ids is None else tuple(subpacket_ids),
            is_container=packet.is_container if is_container is None else bool(is_container),
            boundary=packet.boundary if boundary is None else deepcopy(dict(boundary)),
        )
        self._register_cache_parts(new_packet, content_ids)
        return new_packet

    @staticmethod
    def _message_sets_for_split(packet: CompactContextPacket, max_messages: int) -> List[Set[str]]:
        ordered = list(_unique(packet.source_message_ids + packet.context_message_ids))
        if not ordered:
            return [set()]
        return [set(ordered[index : index + max_messages]) for index in range(0, len(ordered), max_messages)]

    def _slice_packet_by_messages(self, packet: CompactContextPacket, message_sets: Sequence[Set[str]]) -> List[CompactContextPacket]:
        result: List[CompactContextPacket] = []
        for index, selected in enumerate(message_sets):
            primary = tuple(item for item in packet.primary_refs if str(item.get("message_id")) in selected)
            adjacent = tuple(item for item in packet.adjacent_refs if str(item.get("message_id")) in selected)
            primary_ids = _unique(str(item.get("message_id")) for item in primary if isinstance(item.get("message_id"), str))
            context_ids = _unique(str(item.get("message_id")) for item in adjacent if isinstance(item.get("message_id"), str) and item.get("message_id") not in primary_ids)
            # Metadata-only authorities are assigned to the first child that
            # contains their message; all authorities survive the union.
            fact_ids = tuple(
                key
                for key in packet.authoritative_fact_ids
                if str(self.authoritative_fact_table.get(str(key), {}).get("message_id")) in selected
            )
            candidate_ids = tuple(packet.candidate_row_ids)
            child_views = self._candidate_refs_for_child(packet, candidate_ids)
            evidence_ids = tuple(packet.evidence_ref_ids)
            cues: List[str] = []
            for cue_id in packet.activation_cue_ids:
                cue = self.activation_cue_table.get(str(cue_id), {})
                cue_messages = set(_ref_message_ids(cue))
                if not cue_messages or cue_messages & selected:
                    cues.append(cue_id)
            child = replace(
                packet,
                packet_id="",
                primary_refs=primary,
                adjacent_refs=adjacent,
                source_message_ids=primary_ids,
                context_message_ids=context_ids,
                authoritative_fact_ids=fact_ids,
                candidate_row_ids=candidate_ids,
                evidence_ref_ids=evidence_ids,
                activation_cue_ids=tuple(cues),
                candidate_view_ids=child_views,
                anchor_fragment_ids=_unique(str(item.get("fragment_id")) for item in primary if isinstance(item.get("fragment_id"), str)),
                boundary=self._boundary_unknown(),
                status="open",
                pending_reason="",
                is_container=False,
            )
            result.append(child)
        return result

    @staticmethod
    def _split_ids(values: Sequence[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        pivot = max(1, len(values) // 2)
        return tuple(values[:pivot]), tuple(values[pivot:])

    def _slice_packet_by_candidate_ids(self, packet: CompactContextPacket) -> List[CompactContextPacket]:
        left_ids, right_ids = self._split_ids(packet.candidate_row_ids)
        output: List[CompactContextPacket] = []
        for candidate_ids in (left_ids, right_ids):
            if not candidate_ids:
                continue
            views = self._candidate_refs_for_child(packet, candidate_ids)
            output.append(
                replace(
                    packet,
                    packet_id="",
                    candidate_row_ids=tuple(candidate_ids),
                    candidate_view_ids=views,
                    boundary=self._boundary_unknown(),
                    status="open",
                    pending_reason="",
                    is_container=False,
                )
            )
        return output

    def _slice_packet_by_evidence_ids(self, packet: CompactContextPacket) -> List[CompactContextPacket]:
        left_ids, right_ids = self._split_ids(packet.evidence_ref_ids)
        output: List[CompactContextPacket] = []
        for evidence_ids in (left_ids, right_ids):
            if not evidence_ids:
                continue
            output.append(
                replace(
                    packet,
                    packet_id="",
                    evidence_ref_ids=tuple(evidence_ids),
                    boundary=self._boundary_unknown(),
                    status="open",
                    pending_reason="",
                    is_container=False,
                )
            )
        return output

    def _pending_packet(self, packet: CompactContextPacket, *, reason: str = "capacity_exceeded") -> CompactContextPacket:
        reactivation = dict(packet.reactivation) if isinstance(packet.reactivation, Mapping) else {}
        reactivation.update(
            {
                "reason_code": reason,
                "activation_cue_ids": list(packet.activation_cue_ids),
                "open_snapshot_ref": packet.open_snapshot_ref,
                "reactivation_required": True,
            }
        )
        return self._rehash_packet(
            replace(packet, reactivation=reactivation, boundary=self._boundary_unknown()),
            status="pending",
            pending_reason=reason,
            boundary=self._boundary_unknown(),
        )

    def _expand_packet(self, packet: CompactContextPacket) -> Tuple[CompactContextPacket, ...]:
        """Split one root into capacity-safe leaves or a pending leaf."""

        pending: List[CompactContextPacket] = []
        ready: List[CompactContextPacket] = []
        queue: List[CompactContextPacket] = [packet]
        seen: Set[str] = set()
        while queue:
            current = queue.pop(0)
            # Before hashing children, calculate material from current refs.
            cap = self.capacity_for(current)
            if cap.ok:
                ready.append(current)
                continue
            split: List[CompactContextPacket] = []
            # Message limits are independent and should be handled first.
            if len(current.all_message_ids) > self.capacity.max_messages:
                split = self._slice_packet_by_messages(current, self._message_sets_for_split(current, self.capacity.max_messages))
            elif len(current.candidate_row_ids) > self.capacity.max_candidate_rows:
                split = self._slice_packet_by_candidate_ids(current)
            elif len(current.evidence_ref_ids) > self.capacity.max_evidence_refs:
                split = self._slice_packet_by_evidence_ids(current)
            elif cap.stats.input_token_proxy > self.capacity.max_input_token_proxy:
                # Split the largest independently referenceable dimension.  A
                # single over-large body/row has no safe truncation path and
                # therefore becomes pending below.
                if len(current.candidate_row_ids) > 1:
                    split = self._slice_packet_by_candidate_ids(current)
                elif len(current.evidence_ref_ids) > 1:
                    split = self._slice_packet_by_evidence_ids(current)
                elif len(current.all_message_ids) > 1:
                    split = self._slice_packet_by_messages(current, self._message_sets_for_split(current, max(1, len(current.all_message_ids) // 2)))
            # De-duplicate split signatures so a pathological row cannot loop.
            for child in split:
                signature = stable_hash(
                    {
                        "primary": [dict(item) for item in child.primary_refs],
                        "adjacent": [dict(item) for item in child.adjacent_refs],
                        "facts": list(child.authoritative_fact_ids),
                        "candidates": list(child.candidate_row_ids),
                        "evidence": list(child.evidence_ref_ids),
                    }
                )
                if signature in seen:
                    continue
                seen.add(signature)
                queue.append(child)
            if not split:
                pending.append(self._pending_packet(current))
        # Deterministic child IDs and adjacency links are based on the final
        # order.  Keep source order, then candidate/evidence split order.
        children: List[CompactContextPacket] = []
        for index, child in enumerate(ready + pending):
            child_id = _scoped(
                packet.scope,
                "subpacket",
                stable_hash(
                    {
                        "parent": packet.packet_id,
                        "index": index,
                        "primary": [dict(item) for item in child.primary_refs],
                        "adjacent": [dict(item) for item in child.adjacent_refs],
                        "candidates": list(child.candidate_row_ids),
                        "evidence": list(child.evidence_ref_ids),
                    }
                )[:32],
            )
            child = self._rehash_packet(
                replace(
                    child,
                    packet_id=child_id,
                    parent_packet_id=packet.packet_id,
                    open_snapshot_ref=packet.open_snapshot_ref,
                    boundary=self._boundary_unknown(),
                    reactivation={
                        **dict(packet.reactivation),
                        "activation_cue_ids": list(packet.activation_cue_ids),
                        "open_snapshot_ref": packet.open_snapshot_ref,
                    },
                ),
                boundary=self._boundary_unknown(),
            )
            children.append(child)
        child_ids = tuple(item.packet_id for item in children)
        linked: List[CompactContextPacket] = []
        for index, child in enumerate(children):
            linked_child = self._rehash_packet(
                replace(
                    child,
                    predecessor_refs=(child_ids[index - 1],) if index > 0 else (),
                    successor_refs=(child_ids[index + 1],) if index + 1 < len(child_ids) else (),
                )
            )
            # ``materialize_stage_packet`` appends its accounting envelope
            # after the internal model is shaped.  Re-check that final view
            # here; an over-capacity child is explicitly pending rather than
            # being emitted as an apparently sendable packet.
            if not self.capacity_for(linked_child).ok and linked_child.status != "pending":
                linked_child = self._pending_packet(linked_child, reason="capacity_exceeded")
            linked.append(linked_child)
        return tuple(linked)

    def _resolve_template(self, value: Any, *, include_body: bool) -> Any:
        if isinstance(value, Mapping):
            if "$compact_content_ref" in value:
                content_id = value.get("$compact_content_ref")
                entry = self.content_table.get(str(content_id))
                if entry is None:
                    raise CompactContextPacketError("content_ref_missing")
                return entry.body if include_body else None
            ref = value.get("$compact_ref")
            if isinstance(ref, Mapping):
                kind = str(ref.get("kind") or "")
                key = str(ref.get("key") or "")
                table = self._table_maps().get(kind)
                if not isinstance(table, Mapping) or key not in table:
                    raise CompactContextPacketError("template_ref_missing")
                return self._materialize_record(table[key], self.content_table, self._table_maps(), include_body=include_body)
            return {str(key): self._resolve_template(child, include_body=include_body) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._resolve_template(child, include_body=include_body) for child in value]
        return deepcopy(value)

    def _resolved_rows(self, packet: CompactContextPacket, *, include_body: bool) -> Dict[str, List[Dict[str, Any]]]:
        primary = [self._materialize_fragment(packet, ref, include_body=include_body) for ref in packet.primary_refs]
        adjacent = [self._materialize_fragment(packet, ref, include_body=include_body) for ref in packet.adjacent_refs]
        facts = [self._materialize_authoritative_fact(key, include_body=include_body) for key in packet.authoritative_fact_ids]
        candidates = [self._materialize_candidate(key, include_body=include_body) for key in packet.candidate_row_ids]
        evidence = [self._materialize_evidence(key, include_body=include_body) for key in packet.evidence_ref_ids]
        sources = [
            self._materialize_record(self.source_ref_table[key], self.content_table, self._table_maps(), include_body=include_body)
            for key in packet.source_ref_ids
            if key in self.source_ref_table
        ]
        cues = [
            self._materialize_record(self.activation_cue_table[key], self.content_table, self._table_maps(), include_body=include_body)
            for key in packet.activation_cue_ids
            if key in self.activation_cue_table
        ]
        return {
            "primary": primary,
            "adjacent": adjacent,
            "facts": facts,
            "candidates": candidates,
            "evidence": evidence,
            "sources": sources,
            "cues": cues,
        }

    def recover_packet(
        self,
        packet: Union[str, CompactContextPacket],
        *,
        include_body: bool = True,
    ) -> Dict[str, Any]:
        """Rebuild a K2-shaped local mapping from compact tables.

        Recovery expands references but intentionally does not invoke any
        provider or semantic layer.  A child packet recovers its selected
        complete units; its open snapshot and split lineage remain explicit.
        """

        resolved = self.get_packet(packet)
        template = self.source_templates.get(resolved.source_template_id)
        if template is not None and resolved.is_container:
            output = self._resolve_template(template, include_body=include_body)
            if not isinstance(output, Mapping):
                output = {}
            result: Dict[str, Any] = {str(key): deepcopy(value) for key, value in output.items()}
        elif template is not None and not resolved.parent_packet_id:
            output = self._resolve_template(template, include_body=include_body)
            result = {str(key): deepcopy(value) for key, value in output.items()} if isinstance(output, Mapping) else {}
        else:
            result = {}
        rows = self._resolved_rows(resolved, include_body=include_body)
        # Replace canonical layers so a child cannot accidentally recover its
        # parent's full message window.  Mirrors are rebuilt from the same
        # single row tables and therefore remain lossless without duplication
        # inside the store.
        result.update(
            {
                "packet_id": resolved.source_packet_id if resolved.is_container else resolved.packet_id,
                "context_packet_id": resolved.source_packet_id if resolved.is_container else resolved.packet_id,
                "account_id": resolved.account_id,
                "chat_id": resolved.chat_id,
                "scope": {"account_id": resolved.account_id, "chat_id": resolved.chat_id},
                "anchor_fragment_ids": list(resolved.anchor_fragment_ids),
                "anchor_fragment_id": resolved.anchor_fragment_ids[0] if resolved.anchor_fragment_ids else UNKNOWN,
                "claim_ids": list(resolved.anchor_claim_ids),
                "source_message_ids": list(resolved.source_message_ids),
                "primary_fragments": rows["primary"],
                "adjacent_context": rows["adjacent"],
                "authoritative_facts": rows["facts"],
                "source_refs": rows["sources"],
                "evidence_refs": rows["evidence"],
                "candidate_qa_links": [row for row in rows["candidates"] if str(resolved.candidate_view_ids.get("qa_candidates", ())) and row.get("candidate_id") in {self._public_id(self.candidate_row_table[key], str(key)) for key in resolved.candidate_view_ids.get("qa_candidates", ())}],
                "candidate_person_history": [row for row in rows["candidates"] if row.get("candidate_id") in {self._public_id(self.candidate_row_table[key], str(key)) for key in resolved.candidate_view_ids.get("person_history", ())}],
                "candidate_object_history": [row for row in rows["candidates"] if row.get("candidate_id") in {self._public_id(self.candidate_row_table[key], str(key)) for key in resolved.candidate_view_ids.get("object_history", ())}],
                "candidate_state_history": [row for row in rows["candidates"] if row.get("candidate_id") in {self._public_id(self.candidate_row_table[key], str(key)) for key in resolved.candidate_view_ids.get("state_history", ())}],
                "open_thread_candidates": [row for row in rows["candidates"] if row.get("candidate_id") in {self._public_id(self.candidate_row_table[key], str(key)) for key in resolved.candidate_view_ids.get("open_threads", ())}],
                "activation_cues": rows["cues"],
                "candidate_reason": list(resolved.candidate_reason_codes),
                "uncertainties": list(resolved.uncertainties),
                "boundary": self._boundary_unknown() if resolved.parent_packet_id or resolved.status == "pending" else deepcopy(dict(resolved.boundary)),
                "open_snapshot_ref": resolved.open_snapshot_ref,
                "status": resolved.status,
                "pending_reason": resolved.pending_reason,
                "fixed_hash": resolved.fixed_hash,
                "dynamic_hash": resolved.dynamic_hash,
                "content_hash": resolved.content_hash,
                "packet_hash": resolved.packet_hash,
                "cache_key": resolved.cache_key,
                "packet_version": resolved.packet_version,
            }
        )
        if include_body:
            # The source template may have retained arbitrary scalar fields;
            # all known body fields are restored from the content table above.
            pass
        else:
            # Ensure no body-bearing scalar survives in a body-free recovery.
            result = _without_body(result)
        return result

    def add_packet(self, packet: Any) -> Tuple[CompactContextPacket, ...]:
        """Ingest one K2 packet and return its capacity-safe leaf packets."""

        root = self._ingest_packet(packet)
        # Idempotent source re-ingest returns an existing root.  Re-expanding
        # it would duplicate children, so use the current leaf set directly.
        existing_children = tuple(
            child for child in self.packet_index.values() if child.parent_packet_id == root.packet_id and not child.is_container
        )
        if existing_children:
            return existing_children
        leaves = self._expand_packet(root)
        if len(leaves) == 1 and leaves[0].status == "pending":
            pending = self._rehash_packet(
                replace(
                    leaves[0],
                    packet_id=root.packet_id,
                    parent_packet_id="",
                    is_container=False,
                    subpacket_ids=(),
                    predecessor_refs=(),
                    successor_refs=(),
                ),
                boundary=self._boundary_unknown(),
            )
            self.packet_index[root.packet_id] = pending
            return (pending,)
        if len(leaves) == 1 and leaves[0].packet_hash == root.packet_hash:
            self.packet_index[root.packet_id] = root
            return (root,)
        child_ids = tuple(item.packet_id for item in leaves)
        container = self._rehash_packet(
            replace(
                root,
                status="split",
                is_container=True,
                subpacket_ids=child_ids,
                predecessor_refs=(),
                successor_refs=(),
                boundary=self._boundary_unknown() if root.window_scale == "W2" or root.status != "open" else root.boundary,
            ),
            boundary=self._boundary_unknown() if root.window_scale == "W2" or root.status != "open" else root.boundary,
        )
        self.packet_index[root.packet_id] = container
        for child in leaves:
            self.packet_index[child.packet_id] = child
            self._source_sizes[child.packet_id] = self._source_sizes.get(root.packet_id, 0)
        return tuple(leaves)

    ingest = add_packet

    def add_packets(self, packets: Iterable[Any]) -> Tuple[CompactContextPacket, ...]:
        leaves: List[CompactContextPacket] = []
        for packet in packets:
            leaves.extend(self.add_packet(packet))
        return tuple(leaves)

    def _all_source_chars(self) -> int:
        # Root entries carry the original source size; child aliases point to
        # the same root size and are excluded from this aggregate.
        total = 0
        for packet_id, size in self._source_sizes.items():
            packet = self.packet_index.get(packet_id)
            if packet is None or packet.parent_packet_id:
                continue
            total += int(size)
        return total

    def compression_stats(self) -> CompactCompressionStats:
        source_chars = self._all_source_chars()
        index_payload = self.to_dict(include_content=False)
        private_payload = self.to_dict(include_content=True)
        index_chars = len(canonical_json(index_payload))
        private_chars = len(canonical_json(private_payload))
        content_chars = len(canonical_json({key: entry.to_dict(include_body=True) for key, entry in self.content_table.items()}))
        body_free_chars = len(canonical_json(index_payload))
        ratio = float(source_chars) / float(index_chars or 1)
        private_ratio = float(source_chars) / float(private_chars or 1)
        savings = 0.0 if source_chars <= 0 else max(0.0, (1.0 - (private_chars / float(source_chars))) * 100.0)
        return CompactCompressionStats(
            source_canonical_chars=source_chars,
            compact_index_chars=index_chars,
            compact_private_chars=private_chars,
            body_free_index_chars=body_free_chars,
            content_chars=content_chars,
            compression_ratio=ratio,
            private_compression_ratio=private_ratio,
            space_savings_pct=savings,
            duplicate_body_occurrences_removed=max(0, self._content_occurrences - len(self.content_table)),
            duplicate_candidate_occurrences_removed=max(0, self._candidate_occurrences - len(self.candidate_row_table)),
            duplicate_evidence_occurrences_removed=max(0, self._evidence_occurrences - len(self.evidence_ref_table)),
        )

    @property
    def compression_ratio(self) -> float:
        return self.compression_stats().compression_ratio

    def to_dict(self, *, include_content: bool = False) -> Dict[str, Any]:
        content = {
            str(key): value.to_dict(include_body=include_content)
            for key, value in sorted(self.content_table.items())
        }
        if not include_content:
            for row in content.values():
                row.pop("body", None)
        return {
            "schema_version": COMPACT_STORE_SCHEMA_VERSION,
            "projector_schema_version": COMPACT_PROJECTOR_SCHEMA_VERSION,
            "packet_version": self.packet_version,
            "pipeline_version": COMPACT_PIPELINE_VERSION,
            "capacity": self.capacity.to_dict(),
            "packets": [item.to_dict() for item in self.packets],
            "packet_index": {key: self.packet_index[key].to_dict() for key in sorted(self.packet_index)},
            "content_table": content,
            "fragment_table": {key: _body_free_export(value) for key, value in sorted(self.fragment_table.items())},
            "message_table": {key: _body_free_export(value) for key, value in sorted(self.message_table.items())},
            "authoritative_fact_table": {key: _body_free_export(value) for key, value in sorted(self.authoritative_fact_table.items())},
            "candidate_row_table": {key: _body_free_export(value) for key, value in sorted(self.candidate_row_table.items())},
            "evidence_ref_table": {key: _body_free_export(value) for key, value in sorted(self.evidence_ref_table.items())},
            "source_ref_table": {key: _body_free_export(value) for key, value in sorted(self.source_ref_table.items())},
            "activation_cue_table": {key: _body_free_export(value) for key, value in sorted(self.activation_cue_table.items())},
            "open_snapshot_table": {key: _body_free_export(value) for key, value in sorted(self.open_snapshot_table.items())},
            "source_templates": _body_free_export(self.source_templates),
            "cache": self.cache.to_dict(include_content=include_content),
        }

    export = to_dict


def _coerce_capacity(
    capacity: Optional[CompactCapacity],
    *,
    max_input_token_proxy: Optional[int],
    max_messages: Optional[int],
    max_candidate_rows: Optional[int],
    max_evidence_refs: Optional[int],
) -> CompactCapacity:
    if capacity is not None:
        if not isinstance(capacity, CompactCapacity):
            if isinstance(capacity, Mapping):
                capacity = CompactCapacity(
                    max_input_token_proxy=int(capacity.get("max_input_token_proxy", DEFAULT_MAX_INPUT_TOKEN_PROXY)),
                    max_messages=int(capacity.get("max_messages", DEFAULT_MAX_MESSAGES)),
                    max_candidate_rows=int(capacity.get("max_candidate_rows", DEFAULT_MAX_CANDIDATE_ROWS)),
                    max_evidence_refs=int(capacity.get("max_evidence_refs", DEFAULT_MAX_EVIDENCE_REFS)),
                )
            else:
                raise ValueError("capacity must be CompactCapacity or mapping")
        if any(value is not None for value in (max_input_token_proxy, max_messages, max_candidate_rows, max_evidence_refs)):
            return CompactCapacity(
                max_input_token_proxy=int(max_input_token_proxy if max_input_token_proxy is not None else capacity.max_input_token_proxy),
                max_messages=int(max_messages if max_messages is not None else capacity.max_messages),
                max_candidate_rows=int(max_candidate_rows if max_candidate_rows is not None else capacity.max_candidate_rows),
                max_evidence_refs=int(max_evidence_refs if max_evidence_refs is not None else capacity.max_evidence_refs),
            )
        return capacity
    return CompactCapacity(
        max_input_token_proxy=int(max_input_token_proxy if max_input_token_proxy is not None else DEFAULT_MAX_INPUT_TOKEN_PROXY),
        max_messages=int(max_messages if max_messages is not None else DEFAULT_MAX_MESSAGES),
        max_candidate_rows=int(max_candidate_rows if max_candidate_rows is not None else DEFAULT_MAX_CANDIDATE_ROWS),
        max_evidence_refs=int(max_evidence_refs if max_evidence_refs is not None else DEFAULT_MAX_EVIDENCE_REFS),
    )


def _coerce_packet_sequence(value: Any) -> Tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        # A result mapping is accepted as a convenience; a packet mapping is
        # distinguished by its packet ID and primary layer.
        if "packets" in value and isinstance(value.get("packets"), (list, tuple)):
            return tuple(value["packets"])
        if "context_packets" in value and isinstance(value.get("context_packets"), (list, tuple)):
            return tuple(value["context_packets"])
        return (value,)
    packet_values = getattr(value, "packets", None)
    if packet_values is not None and not isinstance(value, (list, tuple)):
        try:
            return tuple(packet_values)
        except TypeError:
            pass
    if isinstance(value, (list, tuple)):
        return tuple(value)
    # K2 ContextPacket itself is not iterable; accepting it as one item keeps
    # the functional API ergonomic.
    return (value,)


def compact_context_packets(
    packets: Any,
    *,
    capacity: Optional[CompactCapacity] = None,
    max_input_token_proxy: Optional[int] = None,
    max_messages: Optional[int] = None,
    max_candidate_rows: Optional[int] = None,
    max_evidence_refs: Optional[int] = None,
    packet_version: str = COMPACT_CONTEXT_PACKET_VERSION,
    cache: Optional[CompactPacketCache] = None,
    store: Optional[CompactContextPacketStore] = None,
) -> CompactContextPacketResult:
    """Compact one or more K2 packets into a local deduplicated store.

    ``packets`` accepts a K2 ``ContextPacket``, a mapping, a packet result, or
    an iterable of those values.  The returned ``packets`` tuple contains
    only sendable/pending leaves; container parents are inspectable through
    ``result.store.packet_index``.
    """

    resolved_capacity = _coerce_capacity(
        capacity,
        max_input_token_proxy=max_input_token_proxy,
        max_messages=max_messages,
        max_candidate_rows=max_candidate_rows,
        max_evidence_refs=max_evidence_refs,
    )
    if store is None:
        target_store = CompactContextPacketStore(capacity=resolved_capacity, packet_version=packet_version, cache=cache)
    else:
        target_store = store
        target_store.capacity = resolved_capacity
        target_store.packet_version = _text(packet_version, COMPACT_CONTEXT_PACKET_VERSION)
        if cache is not None and target_store.cache is not cache:
            target_store.cache = cache
    sequence = _coerce_packet_sequence(packets)
    leaves = target_store.add_packets(sequence)
    source_ids: List[str] = []
    input_parts: List[str] = []
    for source in sequence:
        data = _deepcopy_mapping(source)
        source_id = _first_id(data, "packet_id", "context_packet_id") or "unknown"
        source_ids.append(source_id)
        input_parts.append(stable_hash(data))
    compression = target_store.compression_stats()
    return CompactContextPacketResult(
        store=target_store,
        packets=tuple(leaves),
        source_packet_ids=_unique(source_ids),
        input_hash=stable_hash({"packet_version": resolved_capacity.to_dict(), "sources": input_parts}),
        compression=compression,
    )


def materialize_stage_packet(
    store_or_packet: Any,
    packet_id: Optional[Union[str, CompactContextPacket]] = None,
    *,
    capacity: Optional[CompactCapacity] = None,
    allow_over_capacity: bool = False,
    include_content: bool = True,
) -> Dict[str, Any]:
    """Materialise a selected compact leaf into a provider-facing packet.

    The preferred call is ``materialize_stage_packet(store, packet_id)``.
    Passing a :class:`CompactContextPacketResult` as the first argument is
    also supported.  A standalone compact packet cannot be materialised
    because its content-table authority is intentionally held by the store.
    """

    if isinstance(store_or_packet, CompactContextPacketResult):
        result = store_or_packet
        target = packet_id if packet_id is not None else (result.packets[0] if result.packets else None)
        if target is None:
            raise CompactContextPacketError("packet_not_found")
        return result.store.materialize_stage_packet(target, capacity=capacity, allow_over_capacity=allow_over_capacity, include_content=include_content)
    if isinstance(store_or_packet, CompactContextPacketStore):
        target = packet_id
        if target is None:
            raise CompactContextPacketError("packet_id_missing")
        return store_or_packet.materialize_stage_packet(target, capacity=capacity, allow_over_capacity=allow_over_capacity, include_content=include_content)
    if isinstance(packet_id, CompactContextPacketStore):
        return packet_id.materialize_stage_packet(store_or_packet, capacity=capacity, allow_over_capacity=allow_over_capacity, include_content=include_content)
    raise CompactContextPacketError("store_required_for_materialization")


def recover_compact_context_packet(
    store_or_result: Union[CompactContextPacketStore, CompactContextPacketResult],
    packet_id: Union[str, CompactContextPacket],
    *,
    include_body: bool = True,
) -> Dict[str, Any]:
    store = store_or_result.store if isinstance(store_or_result, CompactContextPacketResult) else store_or_result
    if not isinstance(store, CompactContextPacketStore):
        raise CompactContextPacketError("store_required_for_recovery")
    return store.recover_packet(packet_id, include_body=include_body)


estimate_input_token_proxy = lambda value: (len(canonical_json(value)) + 3) // 4
project_compact_context_packet = compact_context_packets
CompactContextPacketProjector = CompactContextPacketStore
ContextPacketCompactStore = CompactContextPacketStore


__all__ = [
    "COMPACT_CONTEXT_PACKET_VERSION",
    "COMPACT_PIPELINE_VERSION",
    "COMPACT_PROJECTOR_SCHEMA_VERSION",
    "COMPACT_STORE_SCHEMA_VERSION",
    "DEFAULT_MAX_CANDIDATE_ROWS",
    "DEFAULT_MAX_EVIDENCE_REFS",
    "DEFAULT_MAX_INPUT_TOKEN_PROXY",
    "DEFAULT_MAX_MESSAGES",
    "UNKNOWN",
    "CompactCapacity",
    "CompactCapacityError",
    "CompactCompressionStats",
    "CompactContentEntry",
    "CompactContextPacket",
    "CompactContextPacketError",
    "CompactContextPacketProjector",
    "CompactContextPacketResult",
    "CompactContextPacketStore",
    "CompactPacketCache",
    "CompactPacketCapacity",
    "CompactPacketMaterialStats",
    "ContextPacketCompactStore",
    "canonical_json",
    "compact_context_packets",
    "estimate_input_token_proxy",
    "materialize_stage_packet",
    "project_compact_context_packet",
    "recover_compact_context_packet",
    "stable_hash",
]
