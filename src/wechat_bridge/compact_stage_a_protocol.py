"""Offline K15 Stage-A wire protocol.

This module is deliberately independent from the K14 runner and has no
provider, filesystem, private-artifact, frozen-data, or production-state
surface.  It specifies the smallest useful Stage-A exchange: the model sees a
single request-local handle table and returns only topic assignments.

The long authoritative handle is present once in the request table.  The
model uses the short, request-local ``m1``/``c1`` aliases in its response;
``resolve_compact_output`` maps those assignments back to the authoritative
handles locally.  Thus a message handle remains the assignment evidence while
the response does not repeat long scoped ids.  Candidate rows are deliberately
handle-only: claims, persons, objects, evidence bodies, and speaker/scope
facts belong to later local stages and are never part of this wire schema.

The size helpers count the complete OpenAI-compatible ``messages`` envelope
(system content plus the canonical user JSON string).  They are pure byte and
token-proxy calculations; they do not call a provider or save payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


PROTOCOL_VERSION = "stage_a_topic_assignment_compact_v2"
STAGE_A_PROTOCOL_VERSION = PROTOCOL_VERSION
STAGE_A_SCHEMA_VERSION = PROTOCOL_VERSION
COMPACT_STAGE_A_SCHEMA_VERSION = PROTOCOL_VERSION
PROMPT_VERSION = "stage_a_topic_assignment_compact_prompt_v2"

# Stage A is a partition of the authoritative primary messages.  This rule is
# deliberately expressed once as a compact, provider-facing formula and is
# reused by the request schema, the prompt, and the local validator.  A
# context-only row can never create a topic on its own: every topic needs at
# least one primary, and a primary may occur in exactly one topic.
TOPIC_LIMIT_RULE = (
    "primary_count=#h(m,p)>=1; topic_count<=primary_count; "
    "each t.p nonempty; primary exactly once"
)

# Keep this fixed.  A request hash and a provider cache key must not change
# because a caller supplied a slightly different instruction string.
SYSTEM_PROMPT = (
    "JSON only {t:[{i,p,c,u}]}; "
    + TOPIC_LIMIT_RULE
    + "; c=context 0/1; m aliases; u=certain|uncertain|unknown; "
    "no extras/prose/Stage-B fields"
)
STAGE_A_SYSTEM_PROMPT = SYSTEM_PROMPT

TOP_LEVEL_KEYS = frozenset({"v", "s", "h"})
MESSAGE_ROW_KEYS = frozenset({"i", "k", "h", "r", "x"})
CANDIDATE_ROW_KEYS = frozenset({"i", "k", "h"})
OUTPUT_TOP_KEYS = frozenset({"t"})
OUTPUT_TOPIC_KEYS = frozenset({"i", "p", "c", "u"})
REQUEST_SCHEMA = (
    "{v:string,s:{a:string,c:string},"
    "h:[{i:alias,k:m,r:p|c,h:handle,x:cue}|{i:alias,k:c,h:handle}],"
    + TOPIC_LIMIT_RULE
    + "}"
)
RESPONSE_SCHEMA = (
    "{t:[{i:topic_id,p:[primary_alias],c:[context_alias],"
    "u:certain|uncertain|unknown}];"
    + TOPIC_LIMIT_RULE
    + "}"
)

HANDLE_KINDS = frozenset({"m", "c"})
UNCERTAINTY_VALUES = frozenset({"certain", "uncertain", "unknown"})

MAX_MESSAGES = 14
MAX_CANDIDATES = 20
MAX_HANDLE_CHARS = 128
MAX_ALIAS_CHARS = 8
MAX_TOPIC_ID_CHARS = 32
MAX_MESSAGE_CUE_CHARS = 80
MAX_INPUT_TOKEN_PROXY = 1600
MAX_OUTPUT_TOKENS = 400
TOKEN_PROXY_CHARS = 4

_ALIAS_RE = re.compile(r"^[mc](?:0|[1-9])[0-9]{0,2}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CompactStageAProtocolError(ValueError):
    """Fail-closed, body-free K15 protocol error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


ProtocolError = CompactStageAProtocolError


def _fail(code: str) -> None:
    raise CompactStageAProtocolError(code)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON, rejecting non-standard numbers."""

    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompactStageAProtocolError("canonical_json_invalid") from exc


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _reject_constant(value: str) -> None:
    _fail("nonstandard_json_number")


def _reject_duplicate_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def strict_json_loads(value: Any) -> Any:
    """Parse one strict JSON value; markdown/prose and duplicate keys fail."""

    if type(value) is not str:
        _fail("json_not_text")
    try:
        return json.loads(
            value,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except CompactStageAProtocolError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CompactStageAProtocolError("invalid_json") from exc


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], code: str) -> None:
    if set(value) != set(expected):
        _fail(code)


def _safe_string(value: Any, code: str, *, max_length: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _fail(code)
    if not allow_empty and not value:
        _fail(code)
    if len(value) > max_length or _CONTROL_RE.search(value):
        _fail(code)
    if value != value.strip():
        _fail(code)
    return value


def _safe_handle(value: Any, code: str = "handle_invalid") -> str:
    return _safe_string(value, code, max_length=MAX_HANDLE_CHARS)


def _safe_alias(value: Any, code: str = "alias_invalid") -> str:
    alias = _safe_string(value, code, max_length=MAX_ALIAS_CHARS)
    if not _ALIAS_RE.fullmatch(alias):
        _fail(code)
    return alias


def _scope_pair(value: Any, code: str = "scope_invalid") -> Tuple[str, str]:
    scope = _mapping(value, code)
    account = scope.get("a", scope.get("account_id", scope.get("account")))
    chat = scope.get("c", scope.get("chat_id", scope.get("chat")))
    return (
        _safe_string(account, code + "_account", max_length=MAX_HANDLE_CHARS),
        _safe_string(chat, code + "_chat", max_length=MAX_HANDLE_CHARS),
    )


def _parse_handle_scope(value: str) -> Optional[Tuple[str, str]]:
    """Parse the conventional ``account/chat|kind|id`` prefix if present."""

    if "|" not in value:
        return None
    prefix = value.split("|", 1)[0]
    if "/" not in prefix:
        return None
    account, chat = prefix.split("/", 1)
    if not account or not chat:
        return None
    return account, chat


def _assert_scope(handle: str, scope: Tuple[str, str], code: str = "cross_scope_handle") -> None:
    parsed = _parse_handle_scope(handle)
    if parsed is not None and parsed != scope:
        _fail(code)


def _assert_handle_kind(handle: str, kind: str) -> None:
    parts = handle.split("|")
    if len(parts) >= 3 and parts[1] in {"message", "candidate"}:
        expected = "message" if kind == "m" else "candidate"
        if parts[1] != expected:
            _fail("handle_kind_mismatch")


def _text_from_record(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("text", "material", "message_text", "body", "content"):
        candidate = value.get(key)
        if candidate is not None:
            if not isinstance(candidate, str):
                _fail("message_cue_invalid")
            # The wire cue is intentionally bounded once, upstream of this
            # protocol.  It is not copied into candidate/evidence rows.
            cue = " ".join(candidate.split())
            if len(cue) > MAX_MESSAGE_CUE_CHARS:
                _fail("message_cue_too_long")
            return cue
    return ""


def _role_from_record(value: Any) -> str:
    """Project local primary/context metadata to one compact wire enum."""

    if not isinstance(value, Mapping):
        return "p"
    raw = value.get("role", value.get("layer", value.get("message_role")))
    text = str(raw or "").casefold()
    if text in {"context", "adjacent", "secondary", "unknown_context", "unknown"}:
        return "c"
    return "p"


def _handle_from_record(value: Any, code: str) -> str:
    if isinstance(value, str):
        return _safe_handle(value, code)
    row = _mapping(value, code + "_shape")
    for key in ("handle", "message_handle", "candidate_handle", "id"):
        if row.get(key) not in (None, ""):
            return _safe_handle(row[key], code)
    _fail(code)


def _normalise_scope_wire(scope: Any) -> Dict[str, str]:
    account, chat = _scope_pair(scope)
    return {"a": account, "c": chat}


def build_compact_stage_a_request(
    scope: Optional[Mapping[str, Any]] = None,
    messages: Optional[Sequence[Any]] = None,
    candidates: Sequence[Any] = (),
    *,
    packet: Optional[Mapping[str, Any]] = None,
    context_packet: Optional[Mapping[str, Any]] = None,
    max_input_token_proxy: int = MAX_INPUT_TOKEN_PROXY,
) -> Dict[str, Any]:
    """Build and validate one minimal request-local Stage-A handle table.

    ``messages`` and ``candidates`` may be strings or local mappings.  Mapping
    values can carry transient ``text``/``material`` for a message; only the
    bounded cue is emitted as ``x``.  Speaker fields and all candidate body
    fields are intentionally ignored and never cross this boundary.
    """

    # Also accept one synthetic/context packet mapping.  This keeps the K15
    # boundary usable without importing K14's runner or store classes.
    source_packet = packet if packet is not None else context_packet
    if source_packet is None and isinstance(scope, Mapping) and messages is None and "messages" in scope:
        source_packet = scope
    if source_packet is not None:
        source = _mapping(source_packet, "packet_shape")
        if scope is None or (isinstance(scope, Mapping) and "messages" in scope):
            scope = source.get("scope")
        if messages is None:
            messages = source.get("messages")
        if candidates == ():
            candidates = source.get("candidates", ())

    if scope is None:
        _fail("scope_missing")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        _fail("messages_shape")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        _fail("candidates_shape")
    if not messages:
        _fail("messages_empty")
    if len(messages) > MAX_MESSAGES:
        _fail("messages_limit")
    if len(candidates) > MAX_CANDIDATES:
        _fail("candidates_limit")

    scope_wire = _normalise_scope_wire(scope)
    scope_pair = (scope_wire["a"], scope_wire["c"])
    rows: List[Dict[str, Any]] = []
    seen_handles: set[str] = set()

    for index, value in enumerate(messages, 1):
        handle = _handle_from_record(value, "message_handle_missing")
        _assert_scope(handle, scope_pair)
        _assert_handle_kind(handle, "m")
        if handle in seen_handles:
            _fail("duplicate_authoritative_handle")
        seen_handles.add(handle)
        rows.append(
            {
                "i": "m%d" % index,
                "k": "m",
                "h": handle,
                "r": _role_from_record(value),
                "x": _text_from_record(value),
            }
        )

    for index, value in enumerate(candidates, 1):
        handle = _handle_from_record(value, "candidate_handle_missing")
        _assert_scope(handle, scope_pair)
        _assert_handle_kind(handle, "c")
        if handle in seen_handles:
            _fail("duplicate_authoritative_handle")
        seen_handles.add(handle)
        rows.append({"i": "c%d" % index, "k": "c", "h": handle})

    wire_packet: Dict[str, Any] = {"v": PROTOCOL_VERSION, "s": scope_wire, "h": rows}
    validate_compact_stage_a_request(wire_packet)
    stats = measure_wire_size(wire_packet)
    if stats.http_token_proxy > int(max_input_token_proxy):
        _fail("request_input_token_proxy_exceeded")
    return wire_packet


def validate_compact_stage_a_request(value: Any) -> Dict[str, Any]:
    """Strictly validate one K15 request and return a normalized copy."""

    packet = dict(_mapping(value, "request_shape"))
    _exact_keys(packet, TOP_LEVEL_KEYS, "request_keys")
    if packet.get("v") != PROTOCOL_VERSION:
        _fail("request_version")
    scope = _mapping(packet.get("s"), "request_scope_shape")
    _exact_keys(scope, {"a", "c"}, "request_scope_keys")
    account = _safe_string(scope.get("a"), "request_scope_account", max_length=MAX_HANDLE_CHARS)
    chat = _safe_string(scope.get("c"), "request_scope_chat", max_length=MAX_HANDLE_CHARS)
    scope_pair = (account, chat)

    rows = packet.get("h")
    if type(rows) is not list or not rows:
        _fail("request_handle_table")
    if len(rows) > MAX_MESSAGES + MAX_CANDIDATES:
        _fail("request_handle_table_limit")

    aliases: set[str] = set()
    handles: set[str] = set()
    message_count = 0
    candidate_count = 0
    normalized_rows: List[Dict[str, Any]] = []
    for raw_row in rows:
        row = dict(_mapping(raw_row, "request_handle_row_shape"))
        kind = row.get("k")
        expected = MESSAGE_ROW_KEYS if kind == "m" else CANDIDATE_ROW_KEYS if kind == "c" else ()
        if not expected:
            _fail("request_handle_kind")
        _exact_keys(row, expected, "request_handle_row_keys")
        alias = _safe_alias(row.get("i"), "request_alias_invalid")
        if alias in aliases:
            _fail("duplicate_request_alias")
        if (kind == "m" and not alias.startswith("m")) or (kind == "c" and not alias.startswith("c")):
            _fail("request_alias_kind")
        aliases.add(alias)
        handle = _safe_handle(row.get("h"), "request_authoritative_handle_invalid")
        _assert_scope(handle, scope_pair)
        _assert_handle_kind(handle, str(kind))
        if handle in handles:
            _fail("duplicate_authoritative_handle")
        handles.add(handle)
        if kind == "m":
            message_count += 1
            if message_count > MAX_MESSAGES:
                _fail("request_message_limit")
            role = row.get("r")
            if role not in {"p", "c"}:
                _fail("request_message_role")
            cue = row.get("x")
            if type(cue) is not str or len(cue) > MAX_MESSAGE_CUE_CHARS or _CONTROL_RE.search(cue):
                _fail("request_message_cue")
            normalized_rows.append({"i": alias, "k": "m", "h": handle, "r": role, "x": cue})
        else:
            candidate_count += 1
            if candidate_count > MAX_CANDIDATES:
                _fail("request_candidate_limit")
            normalized_rows.append({"i": alias, "k": "c", "h": handle})

    if message_count == 0:
        _fail("request_messages_empty")
    if not any(row["k"] == "m" and row["r"] == "p" for row in normalized_rows):
        # A request with no primary cannot produce a legal Stage-A topic.  It
        # is rejected before a provider call instead of manufacturing a topic
        # from context-only/greeting rows.
        _fail("request_primary_messages_empty")
    return {"v": PROTOCOL_VERSION, "s": {"a": account, "c": chat}, "h": normalized_rows}


def _primary_aliases_from_packet(packet: Mapping[str, Any]) -> List[str]:
    """Return the authoritative primary aliases used by all topic gates."""

    return [str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "p"]


def topic_limit_for_request(request: Mapping[str, Any]) -> int:
    """Return the derived maximum topic count for one validated request.

    This is intentionally not a configurable constant.  Since every topic
    must contain at least one primary and every primary is covered exactly
    once, the only safe upper bound is the number of primary rows in ``h``.
    """

    packet = validate_compact_stage_a_request(request)
    return len(_primary_aliases_from_packet(packet))


def _string_list(value: Any, code: str, *, allow_empty: bool = True) -> List[str]:
    if type(value) is not list or (not allow_empty and not value):
        _fail(code)
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        item_text = _safe_alias(item, code + "_item")
        if item_text in seen:
            _fail(code + "_duplicate")
        seen.add(item_text)
        result.append(item_text)
    return result


def validate_compact_stage_a_output(value: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate only compact assignment output; K14 verbose output is rejected."""

    packet = validate_compact_stage_a_request(request)
    root = dict(_mapping(value, "output_shape"))
    _exact_keys(root, OUTPUT_TOP_KEYS, "output_keys")
    raw_topics = root.get("t")
    if type(raw_topics) is not list or not raw_topics:
        _fail("output_topics")
    message_aliases = [str(row["i"]) for row in packet["h"] if row["k"] == "m"]
    primary_aliases = _primary_aliases_from_packet(packet)
    context_aliases = [str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "c"]
    # Hand-built requests without a local role distinction treat every
    # message as eligible primary/context; the builder normally emits r=p/c.
    if not context_aliases:
        context_aliases = list(message_aliases)
    allowed_primary = set(primary_aliases)
    allowed_context = set(context_aliases)
    if len(raw_topics) > len(primary_aliases):
        _fail("output_topic_limit")

    seen_topics: set[str] = set()
    seen_primary: set[str] = set()
    seen_context: set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for raw_topic in raw_topics:
        topic = dict(_mapping(raw_topic, "output_topic_shape"))
        _exact_keys(topic, OUTPUT_TOPIC_KEYS, "output_topic_keys")
        topic_id = _safe_string(topic.get("i"), "output_topic_id", max_length=MAX_TOPIC_ID_CHARS)
        if topic_id in seen_topics:
            _fail("duplicate_topic_id")
        seen_topics.add(topic_id)
        primary = _string_list(topic.get("p"), "output_primary", allow_empty=False)
        context = _string_list(topic.get("c"), "output_context", allow_empty=True)
        uncertainty = topic.get("u")
        if uncertainty not in UNCERTAINTY_VALUES:
            _fail("output_uncertainty_enum")
        if not set(primary) <= allowed_primary:
            _fail("output_primary_handle_scope")
        if not set(context) <= allowed_context:
            _fail("output_context_handle_scope")
        if set(primary).intersection(context):
            _fail("output_primary_context_overlap")
        if seen_primary.intersection(primary):
            _fail("duplicate_primary_handle")
        # Context assignment is optional but at most one topic per message.
        # This keeps the 14-message response bounded while still permitting a
        # page to contain any number of topics.
        if seen_context.intersection(context):
            _fail("duplicate_context_handle")
        seen_primary.update(primary)
        seen_context.update(context)
        normalized.append({"i": topic_id, "p": primary, "c": context, "u": str(uncertainty)})

    if seen_primary != allowed_primary:
        _fail("primary_coverage")
    result = {"t": normalized}
    output_stats = measure_output_size(result)
    if output_stats["token_proxy"] > MAX_OUTPUT_TOKENS:
        _fail("output_token_proxy_exceeded")
    return result


def parse_compact_stage_a_output(text: str, request: Mapping[str, Any]) -> Dict[str, Any]:
    return validate_compact_stage_a_output(strict_json_loads(text), request)


def resolve_compact_output(value: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve local aliases to authoritative handles without changing wire keys."""

    compact = validate_compact_stage_a_output(value, request)
    packet = validate_compact_stage_a_request(request)
    table = {str(row["i"]): str(row["h"]) for row in packet["h"] if row["k"] == "m"}
    return {
        "t": [
            {
                "i": topic["i"],
                "p": [table[item] for item in topic["p"]],
                "c": [table[item] for item in topic["c"]],
                "u": topic["u"],
            }
            for topic in compact["t"]
        ]
    }


def canonical_http_messages(system_prompt: str, request: Mapping[str, Any]) -> str:
    """Canonical actual ``messages`` JSON used by an HTTP adapter.

    The user content is itself the canonical JSON string, matching the
    OpenAI-compatible adapter shape.  No model name, API key, or provider call
    is involved here.
    """

    if type(system_prompt) is not str or _CONTROL_RE.search(system_prompt):
        _fail("system_prompt_invalid")
    canonical_user = canonical_json(request)
    return canonical_json(
        {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_user},
            ]
        }
    )


def _proxy(chars: int) -> int:
    return int(math.ceil(max(0, chars) / float(TOKEN_PROXY_CHARS)))


def measure_full_http_messages(system_prompt: str, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Measure arbitrary old/new payloads without retaining their bodies."""

    user = canonical_json(request)
    messages = canonical_http_messages(system_prompt, request)
    schema = canonical_json({"t": [{"i": "t1", "p": ["m1"], "c": [], "u": "unknown"}]})
    return {
        "system_chars": len(system_prompt),
        "user_chars": len(user),
        "messages_chars": len(messages),
        "schema_chars": len(schema),
        "system_bytes": len(system_prompt.encode("utf-8")),
        "user_bytes": len(user.encode("utf-8")),
        "messages_bytes": len(messages.encode("utf-8")),
        "schema_bytes": len(schema.encode("utf-8")),
        "system_token_proxy": _proxy(len(system_prompt)),
        "user_token_proxy": _proxy(len(user)),
        "input_token_proxy": _proxy(len(system_prompt) + len(user)),
        "http_token_proxy": _proxy(len(messages)),
        "schema_token_proxy": _proxy(len(schema)),
        "messages_sha256": hashlib.sha256(messages.encode("utf-8")).hexdigest(),
    }


def measure_wire_size(
    request: Mapping[str, Any],
    system_prompt: str = SYSTEM_PROMPT,
) -> "WireSizeStats":
    validate_compact_stage_a_request(request)
    raw = measure_full_http_messages(system_prompt, request)
    return WireSizeStats(**raw)


def measure_output_size(value: Mapping[str, Any]) -> Dict[str, int]:
    text = canonical_json(value)
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "token_proxy": _proxy(len(text)),
    }


@dataclass(frozen=True)
class WireSizeStats:
    system_chars: int
    user_chars: int
    messages_chars: int
    schema_chars: int
    system_bytes: int
    user_bytes: int
    messages_bytes: int
    schema_bytes: int
    system_token_proxy: int
    user_token_proxy: int
    input_token_proxy: int
    http_token_proxy: int
    schema_token_proxy: int
    messages_sha256: str

    @property
    def within_input_limit(self) -> bool:
        return self.http_token_proxy <= MAX_INPUT_TOKEN_PROXY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "system_chars": self.system_chars,
            "user_chars": self.user_chars,
            "messages_chars": self.messages_chars,
            "schema_chars": self.schema_chars,
            "system_bytes": self.system_bytes,
            "user_bytes": self.user_bytes,
            "messages_bytes": self.messages_bytes,
            "schema_bytes": self.schema_bytes,
            "system_token_proxy": self.system_token_proxy,
            "user_token_proxy": self.user_token_proxy,
            "input_token_proxy": self.input_token_proxy,
            "http_token_proxy": self.http_token_proxy,
            "schema_token_proxy": self.schema_token_proxy,
            "within_input_limit": self.within_input_limit,
            "messages_sha256": self.messages_sha256,
        }


def compare_wire_sizes(
    old_system_prompt: str,
    old_request: Mapping[str, Any],
    new_request: Mapping[str, Any],
    *,
    new_system_prompt: str = SYSTEM_PROMPT,
) -> Dict[str, Any]:
    """Return body-free K14/K15 wire-size deltas for an offline fixture."""

    old = measure_full_http_messages(old_system_prompt, old_request)
    new = measure_full_http_messages(new_system_prompt, new_request)
    return {
        "old": old,
        "new": new,
        "reduction": {
            "messages_chars": old["messages_chars"] - new["messages_chars"],
            "messages_bytes": old["messages_bytes"] - new["messages_bytes"],
            "http_token_proxy": old["http_token_proxy"] - new["http_token_proxy"],
        },
        "new_within_input_limit": new["http_token_proxy"] <= MAX_INPUT_TOKEN_PROXY,
    }


def build_max_size_response(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Construct a deterministic largest-shape valid response for this page.

    Each message is a separate topic (maximizing topic rows) and is context
    for the next topic once (maximizing optional context assignments without
    violating the 0/1 context rule).  Candidate count does not inflate the
    response because candidates are not Stage-A assignment output.
    """

    packet = validate_compact_stage_a_request(request)
    messages = [str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "p"]
    context_messages = [str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "c"]
    if not context_messages:
        # With no local context-role rows, rotate the primary aliases so the
        # synthetic max-shape response exercises optional context without
        # putting the same alias in p and c of one topic.
        context_messages = messages[1:] + messages[:1] if len(messages) > 1 else []
    topics: List[Dict[str, Any]] = []
    for index, alias in enumerate(messages):
        context = [context_messages[index]] if index < len(context_messages) else []
        topics.append({"i": "t%d" % (index + 1), "p": [alias], "c": context, "u": "unknown"})
    result = validate_compact_stage_a_output({"t": topics}, packet)
    if measure_output_size(result)["token_proxy"] > MAX_OUTPUT_TOKENS:
        _fail("max_size_response_exceeds_output_limit")
    return result


def max_size_proof(request: Mapping[str, Any]) -> Dict[str, Any]:
    packet = validate_compact_stage_a_request(request)
    response = build_max_size_response(packet)
    request_stats = measure_wire_size(packet)
    output_stats = measure_output_size(response)
    primary_count = len(_primary_aliases_from_packet(packet))
    return {
        "valid": True,
        "message_count": sum(row["k"] == "m" for row in packet["h"]),
        "candidate_count": sum(row["k"] == "c" for row in packet["h"]),
        "primary_count": primary_count,
        "topic_limit": primary_count,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "topic_count": len(response["t"]),
        "request": request_stats.to_dict(),
        "response": output_stats,
        "request_within_1600": request_stats.within_input_limit,
        "response_within_400": output_stats["token_proxy"] <= MAX_OUTPUT_TOKENS,
    }


def size_report(
    request: Mapping[str, Any],
    response: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return body-free request/response wire statistics for an audit ledger."""

    stats = measure_wire_size(request)
    result = stats.to_dict()
    if response is not None:
        validated = validate_compact_stage_a_output(response, request)
        output = measure_output_size(validated)
        result.update(
            {
                "output_chars": output["chars"],
                "output_bytes": output["bytes"],
                "output_token_proxy": output["token_proxy"],
            }
        )
    return result


def project_body_free_ledger(
    request: Mapping[str, Any],
    response: Optional[Mapping[str, Any]] = None,
    report: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Project protocol facts without copying any cue/body field.

    This is an in-memory audit projection only.  It intentionally stores
    handle references and counts, never the ``x`` message cue or any provider
    response text.
    """

    packet = validate_compact_stage_a_request(request)
    assignments: Optional[Dict[str, Any]] = None
    if response is not None:
        assignments = validate_compact_stage_a_output(response, packet)
    message_handles = [str(row["h"]) for row in packet["h"] if row["k"] == "m"]
    candidate_handles = [str(row["h"]) for row in packet["h"] if row["k"] == "c"]
    primary_count = len(_primary_aliases_from_packet(packet))
    result: Dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "scope": dict(packet["s"]),
        "message_handles": message_handles,
        "candidate_handles": candidate_handles,
        "message_count": len(message_handles),
        "candidate_count": len(candidate_handles),
        "primary_count": primary_count,
        "topic_limit": primary_count,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
    }
    if assignments is not None:
        result["assignments"] = assignments
        result["topic_count"] = len(assignments["t"])
    if report is None:
        result["size"] = size_report(packet, response)
    else:
        # Callers may pass the already body-free stats map; copying it cannot
        # reintroduce request cues because size_report never includes them.
        if isinstance(report, WireSizeStats):
            result["size"] = report.to_dict()
        elif isinstance(report, Mapping):
            result["size"] = dict(report)
        else:
            _fail("ledger_report_shape")
    return result


# Short aliases make the module convenient for tests and a future runner while
# keeping the canonical names explicit above.
build_request = build_compact_stage_a_request
build_stage_a_request = build_compact_stage_a_request
build_canonical_request = build_compact_stage_a_request
validate_request = validate_compact_stage_a_request
validate_output = validate_compact_stage_a_output


def validate_response(response: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Named public wrapper for callers that use ``response=...``."""

    return validate_compact_stage_a_output(response, request)


def validate_stage_a_response(response: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    return validate_compact_stage_a_output(response, request)


parse_output = parse_compact_stage_a_output
request_size = measure_wire_size
output_size = measure_output_size
request_size_report = size_report
stage_a_size_report = size_report
project_ledger = project_body_free_ledger
ledger_projection = project_body_free_ledger

# Explicitly freeze the small public surface for callers and independent
# audits.  The aliases above remain backwards-friendly names for this module;
# no K14 verbose schema is accepted by the validator.
PUBLIC_API = {
    "builder": "build_compact_stage_a_request",
    "validator": "validate_compact_stage_a_output",
    "sizer": "size_report",
    "ledger": "project_body_free_ledger",
}


__all__ = [
    "PROTOCOL_VERSION",
    "STAGE_A_PROTOCOL_VERSION",
    "STAGE_A_SCHEMA_VERSION",
    "COMPACT_STAGE_A_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "TOPIC_LIMIT_RULE",
    "SYSTEM_PROMPT",
    "STAGE_A_SYSTEM_PROMPT",
    "TOP_LEVEL_KEYS",
    "MESSAGE_ROW_KEYS",
    "CANDIDATE_ROW_KEYS",
    "OUTPUT_TOP_KEYS",
    "OUTPUT_TOPIC_KEYS",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "UNCERTAINTY_VALUES",
    "MAX_MESSAGES",
    "MAX_CANDIDATES",
    "MAX_HANDLE_CHARS",
    "MAX_MESSAGE_CUE_CHARS",
    "MAX_INPUT_TOKEN_PROXY",
    "MAX_OUTPUT_TOKENS",
    "CompactStageAProtocolError",
    "ProtocolError",
    "WireSizeStats",
    "canonical_json",
    "stable_hash",
    "strict_json_loads",
    "build_compact_stage_a_request",
    "build_request",
    "build_stage_a_request",
    "build_canonical_request",
    "validate_compact_stage_a_request",
    "topic_limit_for_request",
    "validate_request",
    "validate_compact_stage_a_output",
    "validate_output",
    "validate_response",
    "validate_stage_a_response",
    "parse_compact_stage_a_output",
    "parse_output",
    "resolve_compact_output",
    "canonical_http_messages",
    "measure_full_http_messages",
    "measure_wire_size",
    "request_size",
    "measure_output_size",
    "output_size",
    "size_report",
    "request_size_report",
    "stage_a_size_report",
    "compare_wire_sizes",
    "build_max_size_response",
    "max_size_proof",
    "project_body_free_ledger",
    "project_ledger",
    "ledger_projection",
    "PUBLIC_API",
]
