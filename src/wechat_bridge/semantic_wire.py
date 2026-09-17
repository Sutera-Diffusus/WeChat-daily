"""Request-local semantic wire protocol.

The provider-facing frame deliberately does not contain authoritative speaker,
chat, scope, or message identifiers.  ``RequestLocalSymbolTable`` keeps those
values in memory and exposes only short ``m*``/``f*``/``e*`` handles to the
model.  A strict local assembler maps selected handles back to a canonical
bundle and runs the normal bundle validator before returning it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .bundle_semantics import (
    BUNDLE_FIELDS,
    BUNDLE_SCHEMA_VERSION,
    CLAIM_TYPES,
    ENTITY_RESOLUTIONS,
    ENTITY_TYPES,
    MODALITIES,
    RELATION_LABELS,
    RELATION_STRENGTHS,
    STATES,
    UNKNOWN,
    empty_bundle,
    stable_hash,
    validate_bundle,
)
from .contextual_bundle_pipeline import (
    BudgetExceeded,
    DEFAULT_MAX_OUTPUT_TOKENS,
    SemanticFrameBundleModel,
    _canonical_json,
)
from .semantic_frame import SemanticFrameParseError


WIRE_SCHEMA_VERSION = "semantic_wire_v1"
CANONICAL_SCHEMA_VERSION = BUNDLE_SCHEMA_VERSION
# C2.9 candidate prompt.  The wire schema remains v1; changing this value is
# intentionally enough to invalidate cached v1-prompt requests without
# claiming that a new real-provider pilot has been run.
WIRE_PROMPT_VERSION = "semantic_wire_prompt_v2"

WIRE_FIELDS = (
    "wire_schema_version",
    "claim_type",
    "state",
    "modality",
    "subject",
    "mentioned_person",
    "target",
    "object",
    "action",
    "coreference_candidates",
    "context_relations",
    "uncertainties",
    "evidence_handles",
)
WIRE_ENTITY_FIELDS = frozenset({"subject", "mentioned_person", "target", "object"})
WIRE_EVIDENCE_FIELDS = (
    "subject",
    "mentioned_person",
    "target",
    "object",
    "action",
    "claim_type",
    "state",
    "modality",
    "coreference_candidates",
    "context_relations",
)
_FENCE_LINES = {"```", "```text", "```semantic_wire_v1"}
_WIRE_ENTITY_KEYS = {"id", "type", "role", "resolution", "evidence_handles"}
_WIRE_ACTION_KEYS = {"label", "resolution", "evidence_handles"}
_WIRE_COREF_KEYS = {"source_id", "target_id", "relation", "score", "evidence_handles"}
_WIRE_RELATION_KEYS = {
    "source_bundle_id",
    "target_bundle_id",
    "label",
    "strength",
    "supporting_signals",
    "evidence_handles",
    "left_chat_handle",
    "right_chat_handle",
}
_WIRE_UNCERTAINTY_KEYS = {"code", "field", "severity"}


class SemanticWireParseError(SemanticFrameParseError):
    """Stable body-free wire/parser error."""


def _strict_json(value: str) -> Any:
    def reject_constant(_value: str) -> Any:
        raise SemanticWireParseError("wire_nonstandard_json")

    def reject_duplicate_keys(pairs: List[Tuple[Any, Any]]) -> Dict[Any, Any]:
        output: Dict[Any, Any] = {}
        for key, item in pairs:
            if key in output:
                raise SemanticWireParseError("wire_duplicate_object_key")
            output[key] = item
        return output

    try:
        return json.loads(
            value,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except SemanticWireParseError:
        raise
    except (TypeError, ValueError) as exc:
        raise SemanticWireParseError("wire_invalid_json") from exc


def _wire_lines(text: Any) -> List[str]:
    if not isinstance(text, str):
        raise SemanticWireParseError("wire_response_not_text")
    lines = text.splitlines()
    if not lines:
        raise SemanticWireParseError("wire_empty")
    output: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in _FENCE_LINES:
            continue
        if "\t" not in line:
            raise SemanticWireParseError("wire_prose_forbidden")
        output.append(line)
    if not output:
        raise SemanticWireParseError("wire_empty")
    return output


def _require_type(value: Any, expected: type, code: str) -> None:
    if type(value) is not expected:
        raise SemanticWireParseError(code)


def _require_string(value: Any, code: str) -> None:
    _require_type(value, str, code)
    if len(value) > 240 or "\n" in value or "\r" in value:
        raise SemanticWireParseError(code)


def _require_string_list(value: Any, code: str) -> None:
    _require_type(value, list, code)
    for item in value:
        _require_string(item, code)


def _require_handle_list(value: Any, symbol_table: "RequestLocalSymbolTable") -> None:
    _require_string_list(value, "wire_evidence_handles_type")
    if len(value) != len(set(value)):
        raise SemanticWireParseError("wire_evidence_handle_duplicate")
    for handle in value:
        if handle not in symbol_table.evidence_by_handle:
            raise SemanticWireParseError("wire_evidence_handle_unknown")


def _exact_keys(value: Any, keys: Iterable[str], code: str) -> Dict[str, Any]:
    _require_type(value, dict, code)
    if set(value) != set(keys):
        raise SemanticWireParseError(code)
    return value


@dataclass(frozen=True)
class SymbolMessage:
    message_handle: str
    message_id: str
    chat_id: str
    speaker_id: str
    content: str
    span_handle: str
    evidence_handle: str
    span_start: int
    span_end: int
    speaker_handle: str
    account_id: str = UNKNOWN


@dataclass(frozen=True)
class SymbolEvidence:
    evidence_handle: str
    message_handle: str
    span_handle: str
    message_id: str
    chat_id: str
    start: int
    end: int
    account_id: str = UNKNOWN


@dataclass(frozen=True)
class RequestLocalSymbolTable:
    """Authoritative request-local ids and bounded span candidates."""

    bundle_id: str
    chat_id: str
    bundle_handle: str
    chat_handle: str
    messages: Tuple[SymbolMessage, ...]
    evidence_by_handle: Mapping[str, SymbolEvidence]
    symbol_table_sha256: str
    scope: str = UNKNOWN
    account_id: str = UNKNOWN

    @property
    def message_ids(self) -> Tuple[str, ...]:
        return tuple(item.message_id for item in self.messages)

    @property
    def authoritative_speaker_id(self) -> str:
        values = {item.speaker_id for item in self.messages if item.speaker_id not in {"", UNKNOWN}}
        return next(iter(values)) if len(values) == 1 else UNKNOWN

    @property
    def authoritative_speaker_handle(self) -> str:
        speaker_id = self.authoritative_speaker_id
        for item in self.messages:
            if item.speaker_id == speaker_id and speaker_id != UNKNOWN:
                return item.speaker_handle
        return UNKNOWN

    def to_model_payload(self, *, content_limit: int = 240) -> Dict[str, Any]:
        """Return the provider-visible handle table with no long identifiers."""

        messages = [
            {
                "handle": item.message_handle,
                "chat_handle": self.chat_handle,
                "speaker_handle": item.speaker_handle,
                "message_type": "text",
                "content": item.content[: max(0, int(content_limit))],
                "span_handles": [item.span_handle],
            }
            for item in self.messages
        ]
        spans = [
            {
                "handle": item.span_handle,
                "message_handle": item.message_handle,
                "start": item.span_start,
                "end": item.span_end,
            }
            for item in self.messages
        ]
        evidence = [
            {
                "handle": item.evidence_handle,
                "message_handle": item.message_handle,
                "span_handle": item.span_handle,
            }
            for item in self.messages
        ]
        return {
            "wire_schema_version": WIRE_SCHEMA_VERSION,
            "wire_prompt_version": WIRE_PROMPT_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "bundle_handle": self.bundle_handle,
            "chat_handle": self.chat_handle,
            "messages": messages,
            "span_candidates": spans,
            "evidence_candidates": evidence,
            "semantic_fields": list(WIRE_FIELDS[1:]),
        }

    def cache_context(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        """Return only stable version/hash metadata for cache and request hashes."""

        return {
            "wire_schema_version": WIRE_SCHEMA_VERSION,
            "wire_prompt_version": WIRE_PROMPT_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "symbol_table_sha256": self.symbol_table_sha256,
            "message_handle_count": len(self.messages),
            "evidence_handle_count": len(self.evidence_by_handle),
            "scope": self.scope,
            "account_id": self.account_id,
        }


def validate_symbol_table(table: RequestLocalSymbolTable) -> None:
    """Validate local handles, chat scope, and span bounds before provider use."""

    if not isinstance(table, RequestLocalSymbolTable):
        raise SemanticWireParseError("wire_symbol_table_type")
    message_by_handle = {item.message_handle: item for item in table.messages}
    if len(message_by_handle) != len(table.messages):
        raise SemanticWireParseError("wire_message_handle_duplicate")
    if len(set(table.message_ids)) != len(table.message_ids):
        raise SemanticWireParseError("wire_message_id_duplicate_or_missing")
    if len(table.evidence_by_handle) != len(table.messages):
        raise SemanticWireParseError("wire_evidence_handle_count_mismatch")
    known_chats = set()
    known_accounts = set()
    known_spans = set()
    known_evidence = set()
    for item in table.messages:
        expected_index = len(known_spans)
        if item.message_handle != "m%d" % expected_index:
            raise SemanticWireParseError("wire_message_handle_invalid")
        if item.span_handle != "f%d" % expected_index or item.evidence_handle != "e%d" % expected_index:
            raise SemanticWireParseError("wire_symbol_handle_invalid")
        if item.span_handle in known_spans or item.evidence_handle in known_evidence:
            raise SemanticWireParseError("wire_symbol_handle_duplicate")
        known_spans.add(item.span_handle)
        known_evidence.add(item.evidence_handle)
        if item.chat_id not in {"", UNKNOWN}:
            known_chats.add(item.chat_id)
        if item.account_id not in {"", UNKNOWN}:
            known_accounts.add(item.account_id)
        if item.span_start < 0 or item.span_end < item.span_start or item.span_end > len(item.content):
            raise SemanticWireParseError("wire_span_out_of_bounds")
        evidence = table.evidence_by_handle.get(item.evidence_handle)
        if evidence is None:
            raise SemanticWireParseError("wire_evidence_handle_missing")
        if evidence.message_handle != item.message_handle or evidence.span_handle != item.span_handle:
            raise SemanticWireParseError("wire_evidence_handle_mismatch")
        if evidence.message_id != item.message_id:
            raise SemanticWireParseError("wire_evidence_message_mismatch")
        if evidence.account_id not in {"", UNKNOWN, item.account_id}:
            raise SemanticWireParseError("wire_cross_scope_input")
        if evidence.start < 0 or evidence.end < evidence.start or evidence.end > len(item.content):
            raise SemanticWireParseError("wire_span_out_of_bounds")
        if table.chat_id not in {"", UNKNOWN} and evidence.chat_id not in {"", UNKNOWN, table.chat_id}:
            raise SemanticWireParseError("wire_cross_chat_input")
        if table.account_id not in {"", UNKNOWN} and item.account_id not in {"", UNKNOWN, table.account_id}:
            raise SemanticWireParseError("wire_cross_scope_input")
    if len(known_chats) > 1:
        raise SemanticWireParseError("wire_cross_chat_input")
    if len(known_accounts) > 1:
        raise SemanticWireParseError("wire_cross_scope_input")
    if table.chat_id not in {"", UNKNOWN} and known_chats and table.chat_id not in known_chats:
        raise SemanticWireParseError("wire_cross_chat_input")
    if table.account_id not in {"", UNKNOWN} and known_accounts and table.account_id not in known_accounts:
        raise SemanticWireParseError("wire_cross_scope_input")
    for handle, evidence in table.evidence_by_handle.items():
        if handle != evidence.evidence_handle or handle not in known_evidence or evidence.message_handle not in message_by_handle:
            raise SemanticWireParseError("wire_evidence_handle_mismatch")

def build_symbol_table(request: Mapping[str, Any]) -> RequestLocalSymbolTable:
    bundle_id = str(request.get("bundle_id") or UNKNOWN)
    chat_id = str(request.get("chat_id") or UNKNOWN)
    requested_account_id = str(request.get("account_id") or UNKNOWN)
    values = request.get("messages")
    if not isinstance(values, (list, tuple)) or not values:
        raise SemanticWireParseError("wire_messages_empty")
    messages: List[SymbolMessage] = []
    evidence: Dict[str, SymbolEvidence] = {}
    seen_ids = set()
    speaker_handles: Dict[str, str] = {}
    known_chats = set()
    known_accounts = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            raise SemanticWireParseError("wire_message_not_object")
        message_id = str(raw.get("message_id") or "")
        if not message_id or message_id in seen_ids:
            raise SemanticWireParseError("wire_message_id_duplicate_or_missing")
        seen_ids.add(message_id)
        row_chat_id = str(raw.get("chat_id") or chat_id or UNKNOWN)
        if chat_id not in {"", UNKNOWN} and row_chat_id not in {"", UNKNOWN, chat_id}:
            raise SemanticWireParseError("wire_cross_chat_input")
        if row_chat_id not in {"", UNKNOWN}:
            known_chats.add(row_chat_id)
        row_account_id = str(raw.get("account_id") or requested_account_id or UNKNOWN)
        if row_account_id not in {"", UNKNOWN}:
            known_accounts.add(row_account_id)
        speaker_id = str(raw.get("speaker_id") or raw.get("sender_id") or UNKNOWN)
        if speaker_id not in {"", UNKNOWN} and speaker_id not in speaker_handles:
            speaker_handles[speaker_id] = "p%d" % len(speaker_handles)
        speaker_handle = speaker_handles.get(speaker_id, UNKNOWN)
        content = raw.get("content")
        content = content if isinstance(content, str) else ""
        message_handle = "m%d" % index
        span_handle = "f%d" % index
        evidence_handle = "e%d" % index
        span_start, span_end = 0, len(content)
        symbol = SymbolMessage(
            message_handle=message_handle,
            message_id=message_id,
            chat_id=row_chat_id,
            speaker_id=speaker_id,
            content=content,
            span_handle=span_handle,
            evidence_handle=evidence_handle,
            span_start=span_start,
            span_end=span_end,
            speaker_handle=speaker_handle,
            account_id=row_account_id,
        )
        messages.append(symbol)
        evidence[evidence_handle] = SymbolEvidence(
            evidence_handle=evidence_handle,
            message_handle=message_handle,
            span_handle=span_handle,
            message_id=message_id,
            chat_id=row_chat_id,
            start=span_start,
            end=span_end,
            account_id=row_account_id,
        )
    if len(known_chats) > 1:
        raise SemanticWireParseError("wire_cross_chat_input")
    if len(known_accounts) > 1:
        raise SemanticWireParseError("wire_cross_scope_input")
    if chat_id in {"", UNKNOWN} and known_chats:
        chat_id = next(iter(known_chats))
    if requested_account_id in {"", UNKNOWN} and known_accounts:
        requested_account_id = next(iter(known_accounts))
    requested_scope = request.get("scope")
    if requested_scope is None:
        if requested_account_id not in {"", UNKNOWN} and chat_id not in {"", UNKNOWN}:
            scope = "%s:%s" % (requested_account_id, chat_id)
        elif chat_id not in {"", UNKNOWN}:
            scope = "chat:%s" % chat_id
        else:
            scope = UNKNOWN
    elif isinstance(requested_scope, str):
        scope = requested_scope.strip() or UNKNOWN
    else:
        scope = _canonical_json(requested_scope)
    material = {
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "chat_id": chat_id,
        "account_id": requested_account_id,
        "scope": scope,
        "messages": [
            {
                "handle": item.message_handle,
                "message_id": item.message_id,
                "chat_id": item.chat_id,
                "speaker_id": item.speaker_id,
                "span_handle": item.span_handle,
                "evidence_handle": item.evidence_handle,
                "start": item.span_start,
                "end": item.span_end,
            }
            for item in messages
        ],
    }
    table = RequestLocalSymbolTable(
        bundle_id=bundle_id,
        chat_id=chat_id,
        bundle_handle="b0",
        chat_handle="c0",
        messages=tuple(messages),
        evidence_by_handle=dict(evidence),
        symbol_table_sha256=stable_hash(material),
        scope=scope,
        account_id=requested_account_id,
    )
    validate_symbol_table(table)
    return table


def compact_wire_payload(table: RequestLocalSymbolTable, *, max_chars: int = 1800) -> Tuple[Dict[str, Any], int]:
    """Shorten only provider-visible message text; never drop handles."""

    payload = table.to_model_payload(content_limit=240)
    encoded = _canonical_json(payload)
    if len(encoded) > int(max_chars):
        payload = table.to_model_payload(content_limit=64)
        encoded = _canonical_json(payload)
    if len(encoded) > int(max_chars):
        payload = table.to_model_payload(content_limit=0)
        encoded = _canonical_json(payload)
    return payload, len(encoded)


def _speaker_id_for_handle(table: RequestLocalSymbolTable, value: str) -> str:
    for item in table.messages:
        if item.speaker_handle == value:
            return item.speaker_id if item.speaker_id not in {"", UNKNOWN} else UNKNOWN
    return UNKNOWN


def _validate_semantic_id(value: str, table: RequestLocalSymbolTable, code: str) -> None:
    """Reject forged local-handle namespaces while permitting semantic labels."""
    if value == UNKNOWN:
        return
    if len(value) >= 2 and value[1:].isdigit() and value[0] in {"m", "f", "e", "p", "c", "b"}:
        if value[0] == "p" and _speaker_id_for_handle(table, value) != UNKNOWN:
            return
        raise SemanticWireParseError(code)


def _resolve_semantic_id(value: str, table: RequestLocalSymbolTable) -> str:
    """Map a provider-visible speaker handle to its registry authority."""
    resolved = _speaker_id_for_handle(table, value)
    if resolved != UNKNOWN:
        return resolved
    _validate_semantic_id(value, table, "wire_entity_handle_unknown")
    return value


def _validate_entity(value: Any, *, role: str, table: RequestLocalSymbolTable) -> None:
    item = _exact_keys(value, _WIRE_ENTITY_KEYS, "wire_%s_shape" % role)
    _require_string(item["id"], "wire_%s_id_type" % role)
    _validate_semantic_id(item["id"], table, "wire_%s_handle_unknown" % role)
    _require_string(item["type"], "wire_%s_type" % role)
    if item["type"] not in ENTITY_TYPES:
        raise SemanticWireParseError("wire_%s_entity_type" % role)
    _require_string(item["role"], "wire_%s_role_type" % role)
    if item["role"] not in {role, UNKNOWN}:
        raise SemanticWireParseError("wire_%s_role" % role)
    _require_string(item["resolution"], "wire_%s_resolution_type" % role)
    if item["resolution"] not in ENTITY_RESOLUTIONS:
        raise SemanticWireParseError("wire_%s_resolution" % role)
    _require_handle_list(item["evidence_handles"], table)


def _validate_entity_list(value: Any, *, field: str, table: RequestLocalSymbolTable) -> None:
    _require_type(value, list, "wire_%s_type" % field)
    for item in value:
        _validate_entity(item, role=field, table=table)


def _validate_action_list(value: Any, table: RequestLocalSymbolTable) -> None:
    _require_type(value, list, "wire_action_type")
    for item in value:
        row = _exact_keys(item, _WIRE_ACTION_KEYS, "wire_action_shape")
        _require_string(row["label"], "wire_action_label_type")
        _require_string(row["resolution"], "wire_action_resolution_type")
        if row["resolution"] not in ENTITY_RESOLUTIONS:
            raise SemanticWireParseError("wire_action_resolution")
        _require_handle_list(row["evidence_handles"], table)


def _validate_coreference_list(value: Any, table: RequestLocalSymbolTable) -> None:
    _require_type(value, list, "wire_coreference_candidates_type")
    for item in value:
        row = _exact_keys(item, _WIRE_COREF_KEYS, "wire_coreference_shape")
        for key in ("source_id", "target_id", "relation"):
            _require_string(row[key], "wire_coreference_%s_type" % key)
        for key in ("source_id", "target_id"):
            _validate_semantic_id(row[key], table, "wire_coreference_handle_unknown")
        if type(row["score"]) not in {int, float} or isinstance(row["score"], bool) or not 0.0 <= float(row["score"]) <= 1.0:
            raise SemanticWireParseError("wire_coreference_score")
        _require_handle_list(row["evidence_handles"], table)


def _validate_relation_list(value: Any, table: RequestLocalSymbolTable) -> None:
    _require_type(value, list, "wire_context_relations_type")
    for item in value:
        row = _exact_keys(item, _WIRE_RELATION_KEYS, "wire_context_relation_shape")
        for key in ("source_bundle_id", "target_bundle_id", "label", "strength", "left_chat_handle", "right_chat_handle"):
            _require_string(row[key], "wire_context_relation_%s_type" % key)
        if row["label"] not in RELATION_LABELS or row["strength"] not in RELATION_STRENGTHS:
            raise SemanticWireParseError("wire_context_relation_enum")
        _require_string_list(row["supporting_signals"], "wire_context_relation_signals_type")
        _require_handle_list(row["evidence_handles"], table)
        for key in ("left_chat_handle", "right_chat_handle"):
            if row[key] not in {UNKNOWN, table.chat_handle}:
                raise SemanticWireParseError("wire_cross_chat_forbidden")


def _validate_uncertainty_list(value: Any) -> None:
    _require_type(value, list, "wire_uncertainties_type")
    for item in value:
        row = _exact_keys(item, _WIRE_UNCERTAINTY_KEYS, "wire_uncertainty_shape")
        for key in _WIRE_UNCERTAINTY_KEYS:
            _require_string(row[key], "wire_uncertainty_%s_type" % key)


def _validate_wire_mapping(
    output: Any,
    *,
    symbol_table: RequestLocalSymbolTable,
    expected_wire_schema_version: str = WIRE_SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Validate an already materialized wire mapping at the local boundary.

    ``parse_wire_frame`` uses this after parsing text, while the assembler uses
    it again for callers that already hold a mapping.  The second path is
    important: no caller can bypass handle, enum, type, or field-set checks by
    invoking canonical assembly directly.
    """
    _require_type(output, dict, "wire_frame_not_object")
    if tuple(output.keys()) != WIRE_FIELDS:
        raise SemanticWireParseError("wire_field_set_or_order")
    _require_string(output["wire_schema_version"], "wire_schema_version_type")
    if output["wire_schema_version"] != expected_wire_schema_version:
        raise SemanticWireParseError("wire_schema_version_mismatch")
    for field in ("claim_type", "state", "modality"):
        _require_string(output[field], "wire_%s_type" % field)
    if output["claim_type"] not in CLAIM_TYPES:
        raise SemanticWireParseError("wire_claim_type_enum")
    if output["state"] not in STATES:
        raise SemanticWireParseError("wire_state_enum")
    if output["modality"] not in MODALITIES:
        raise SemanticWireParseError("wire_modality_enum")
    _validate_entity(output["subject"], role="subject", table=symbol_table)
    for field in ("mentioned_person", "target", "object"):
        _validate_entity_list(output[field], field=field, table=symbol_table)
    _validate_action_list(output["action"], symbol_table)
    _validate_coreference_list(output["coreference_candidates"], symbol_table)
    _validate_relation_list(output["context_relations"], symbol_table)
    _validate_uncertainty_list(output["uncertainties"])
    evidence_map = _exact_keys(output["evidence_handles"], WIRE_EVIDENCE_FIELDS, "wire_evidence_map_shape")
    for field in WIRE_EVIDENCE_FIELDS:
        _require_handle_list(evidence_map[field], symbol_table)
    return output


def parse_wire_frame(
    text: Any,
    *,
    symbol_table: RequestLocalSymbolTable,
    expected_wire_schema_version: str = WIRE_SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Parse exactly one fixed wire frame and validate every local handle."""

    validate_symbol_table(symbol_table)
    lines = _wire_lines(text)
    if len(lines) != len(WIRE_FIELDS):
        raise SemanticWireParseError("wire_field_count")
    output: Dict[str, Any] = {}
    for index, line in enumerate(lines):
        if line.count("\t") != 1:
            raise SemanticWireParseError("wire_field_shape")
        field, encoded = line.split("\t", 1)
        expected = WIRE_FIELDS[index]
        if field != expected:
            if field in WIRE_FIELDS:
                raise SemanticWireParseError("wire_field_order")
            raise SemanticWireParseError("wire_extra_field")
        if field in output:
            raise SemanticWireParseError("wire_duplicate_field")
        output[field] = _strict_json(encoded)
    return _validate_wire_mapping(
        output,
        symbol_table=symbol_table,
        expected_wire_schema_version=expected_wire_schema_version,
    )


def _evidence_row(
    table: RequestLocalSymbolTable,
    handle: str,
    *,
    field: str,
) -> Tuple[str, Dict[str, Any]]:
    item = table.evidence_by_handle[handle]
    evidence_id = "evidence:%s" % stable_hash(
        {"symbol_table": table.symbol_table_sha256, "field": field, "handle": handle}
    )[:24]
    return evidence_id, {
        "evidence_id": evidence_id,
        "message_id": item.message_id,
        "span": {"start": item.start, "end": item.end},
        "field": field,
        "kind": "span",
    }


def _entity_to_canonical(
    value: Mapping[str, Any],
    *,
    role: str,
    table: RequestLocalSymbolTable,
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    handles = value["evidence_handles"]
    evidence_ids: List[str] = []
    for handle in handles:
        evidence_id, row = _evidence_row(table, handle, field=role)
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
        if not any(item["evidence_id"] == evidence_id for item in evidence):
            evidence.append(row)
    entity_id = _resolve_semantic_id(value["id"], table)
    resolution = value["resolution"]
    if entity_id != UNKNOWN and resolution != UNKNOWN and not evidence_ids:
        entity_id = UNKNOWN
        resolution = UNKNOWN
    if entity_id == UNKNOWN:
        resolution = UNKNOWN
    return {
        "id": entity_id,
        "type": value["type"],
        "role": role if role in {"speaker", "subject"} else value["role"],
        "resolution": resolution,
        "evidence_ids": evidence_ids,
    }


def assemble_wire_bundle(
    wire: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    symbol_table: RequestLocalSymbolTable,
    wire_payload_sha256: str = "",
) -> Dict[str, Any]:
    """Map validated wire slots to authoritative canonical bundle metadata."""

    validate_symbol_table(symbol_table)
    wire = _validate_wire_mapping(wire, symbol_table=symbol_table)
    bundle = empty_bundle(
        symbol_table.bundle_id,
        symbol_table.message_ids,
        chat_id=symbol_table.chat_id,
        status="complete",
        source="model_wire",
        schema_version=CANONICAL_SCHEMA_VERSION,
    )
    evidence: List[Dict[str, Any]] = []
    uncertainties: List[Dict[str, Any]] = list(wire.get("uncertainties", ()))

    # Speaker is fully authoritative: the wire frame has no speaker slot.
    speaker_id = symbol_table.authoritative_speaker_id
    speaker_evidence: List[str] = []
    if speaker_id != UNKNOWN and symbol_table.messages:
        speaker_handle = symbol_table.authoritative_speaker_handle
        if speaker_handle != UNKNOWN:
            speaker_message = next(
                (
                    item
                    for item in symbol_table.messages
                    if item.speaker_handle == speaker_handle
                ),
                None,
            )
            if speaker_message is None:
                raise SemanticWireParseError("wire_authoritative_speaker_missing")
            evidence_id, row = _evidence_row(table=symbol_table, handle=speaker_message.evidence_handle, field="speaker")
            speaker_evidence.append(evidence_id)
            evidence.append(row)
    bundle["speaker"] = {
        "id": speaker_id,
        "type": "person",
        "role": "speaker",
        "resolution": "explicit" if speaker_id != UNKNOWN else UNKNOWN,
        "evidence_ids": speaker_evidence,
    }
    bundle["subject"] = _entity_to_canonical(
        wire["subject"], role="subject", table=symbol_table, evidence=evidence
    )
    for field in ("mentioned_person", "target", "object"):
        bundle[field] = [
            _entity_to_canonical(item, role=field, table=symbol_table, evidence=evidence)
            for item in wire[field]
        ]
    actions: List[Dict[str, Any]] = []
    for item in wire["action"]:
        ids: List[str] = []
        for handle in item["evidence_handles"]:
            evidence_id, row = _evidence_row(table=symbol_table, handle=handle, field="action")
            ids.append(evidence_id)
            if not any(existing["evidence_id"] == evidence_id for existing in evidence):
                evidence.append(row)
        label = item["label"]
        resolution = item["resolution"]
        if label != UNKNOWN and resolution != UNKNOWN and not ids:
            label = UNKNOWN
            resolution = UNKNOWN
        actions.append({"label": label, "resolution": resolution, "evidence_ids": ids})
    bundle["action"] = actions

    evidence_map = wire["evidence_handles"]
    for field in ("claim_type", "state", "modality"):
        value = wire[field]
        ids: List[str] = []
        for handle in evidence_map[field]:
            evidence_id, row = _evidence_row(table=symbol_table, handle=handle, field=field)
            ids.append(evidence_id)
            if not any(existing["evidence_id"] == evidence_id for existing in evidence):
                evidence.append(row)
        if value != UNKNOWN and not ids:
            uncertainties.append({"code": "wire_ungrounded_%s" % field, "field": field, "severity": "high"})
            value = UNKNOWN
        bundle[field] = value

    coreferences: List[Dict[str, Any]] = []
    for item in wire["coreference_candidates"]:
        ids: List[str] = []
        for handle in item["evidence_handles"]:
            evidence_id, row = _evidence_row(table=symbol_table, handle=handle, field="coreference_candidates")
            ids.append(evidence_id)
            if not any(existing["evidence_id"] == evidence_id for existing in evidence):
                evidence.append(row)
        coreferences.append(
            {
                "source_id": _resolve_semantic_id(item["source_id"], symbol_table),
                "target_id": _resolve_semantic_id(item["target_id"], symbol_table),
                "relation": item["relation"],
                "score": item["score"],
                "evidence_ids": ids,
            }
        )
    bundle["coreference_candidates"] = coreferences

    # Bundle relations require an external bundle pair.  Keep only a locally
    # chat-safe candidate; the symbol table intentionally has no cross-chat
    # bundle ids.  A self-link is rejected by the canonical validator.
    relations: List[Dict[str, Any]] = []
    for item in wire["context_relations"]:
        if item["left_chat_handle"] not in {UNKNOWN, symbol_table.chat_handle} or item["right_chat_handle"] not in {
            UNKNOWN,
            symbol_table.chat_handle,
        }:
            raise SemanticWireParseError("wire_cross_chat_forbidden")
        if symbol_table.bundle_id in {item["source_bundle_id"], item["target_bundle_id"]}:
            uncertainties.append({"code": "wire_relation_not_promoted", "field": "context_relations", "severity": "medium"})
            continue
        # There is no local external bundle table; leave the candidate pending.
        uncertainties.append({"code": "wire_relation_external_bundle_unknown", "field": "context_relations", "severity": "medium"})
    bundle["context_relations"] = relations
    bundle["uncertainties"] = uncertainties
    bundle["evidence"] = evidence
    bundle["metadata"].update(
        {
            "wire_schema_version": WIRE_SCHEMA_VERSION,
            "wire_prompt_version": WIRE_PROMPT_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "symbol_table_sha256": symbol_table.symbol_table_sha256,
            "wire_payload_sha256": wire_payload_sha256 or stable_hash(wire),
            "scope": symbol_table.scope,
            "account_id": symbol_table.account_id,
        }
    )
    report = validate_bundle(
        bundle,
        message_ids=symbol_table.message_ids,
        chat_id=symbol_table.chat_id,
        expected_schema_version=CANONICAL_SCHEMA_VERSION,
    )
    if not report.ok:
        raise SemanticWireParseError("wire_canonical_validation_failed")
    return bundle


def wire_frame_exemplar() -> str:
    """Shortest complete all-unknown wire exemplar for the provider prompt."""

    example: Dict[str, Any] = {
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "claim_type": UNKNOWN,
        "state": UNKNOWN,
        "modality": UNKNOWN,
        "subject": {
            "id": UNKNOWN,
            "type": "person",
            "role": "subject",
            "resolution": UNKNOWN,
            "evidence_handles": [],
        },
        "mentioned_person": [],
        "target": [],
        "object": [],
        "action": [],
        "coreference_candidates": [],
        "context_relations": [],
        "uncertainties": [],
        "evidence_handles": {field: [] for field in WIRE_EVIDENCE_FIELDS},
    }
    lines = [
        "%s\t%s" % (field, json.dumps(example[field], ensure_ascii=False, separators=(",", ":")))
        for field in WIRE_FIELDS
    ]
    return "BEGIN_SEMANTIC_WIRE_V1_EXAMPLE\n" + "\n".join(lines) + "\nEND_SEMANTIC_WIRE_V1_EXAMPLE"


class SemanticWireBundleModel(SemanticFrameBundleModel):
    """OpenAI-compatible model adapter for the handle-only wire protocol."""

    source = "openai-semantic-wire"
    protocol_version = WIRE_SCHEMA_VERSION

    def cache_context(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        return build_symbol_table(request).cache_context(request)

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        table = build_symbol_table(request)
        payload, input_chars = compact_wire_payload(table, max_chars=self.max_input_chars)
        if input_chars > self.max_input_chars:
            raise BudgetExceeded("wire_input_payload_limit_exceeded")
        wire_payload_sha256 = stable_hash(payload)
        fields = ",".join(WIRE_FIELDS)
        instructions = (
            "Return exactly one semantic_wire_v1 frame under semantic_wire_prompt_v2, "
            "fixed field order, one FIELD<TAB><JSON-value> per line, no prose/fence/markers. "
            "The wire fields are "
            + fields
            + ". Do not output speaker, chat, scope, bundle, message ids, spans, or new "
            "evidence. Select evidence only from supplied e0 handles; use unknown/empty "
            "arrays when uncertain. Copy wire_schema_version exactly, keep all evidence "
            "handles local, never cross chat, and use compact JSON. Semantic role rules: "
            "subject is what the utterance predicates, asks, or requests about; it is not "
            "automatically the speaker. Never copy speaker_handle into subject or "
            "mentioned_person without explicit textual evidence. For explicit first-person "
            "reference, use the supplied pN speaker handle only when evidence supports it; "
            "the local assembler maps that handle to authoritative speaker metadata. Treat "
            "second-person reference as unknown unless a grounded candidate is supplied. "
            "For third-person or named entities, keep only evidence-backed candidates. "
            "mentioned_person is a referred person, not a speaker shortcut; do not force it "
            "to equal subject. If candidates are not unique, preserve unknown and candidates "
            "rather than selecting one. Claim rules: declarative assertion=fact, evaluation "
            "or belief=opinion, information-seeking=question, actionable ask or imperative="
            "request, recommendation=suggestion, tentative proposition=hypothesis; use "
            "unknown when intent is not grounded, and do not classify from punctuation alone. "
            "Every concrete entity, action, claim_type, state, or modality must cite one or "
            "more supplied eN handles; without evidence emit unknown/empty. BEGIN/END below "
            "delimit a prompt-only exemplar; do not emit them:\n"
            + wire_frame_exemplar()
        )
        client = self._get_client()
        if self.config.base_url:
            call_kwargs: Dict[str, Any] = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": _canonical_json(payload)},
                ],
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            }
            extra_body = self._extra_body()
            if extra_body is not None:
                call_kwargs["extra_body"] = extra_body
            response = client.chat.completions.create(**call_kwargs)
        else:
            response = client.responses.create(
                model=self.config.model,
                instructions=instructions,
                input=_canonical_json(payload),
                store=False,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
        text = self._response_text(response)
        try:
            wire = parse_wire_frame(text, symbol_table=table)
            result = assemble_wire_bundle(
                wire,
                request,
                symbol_table=table,
                wire_payload_sha256=wire_payload_sha256,
            )
        except SemanticWireParseError as exc:
            metadata = self._safe_response_metadata(response, response_text=text)
            metadata.update(exc.diagnostics)
            metadata["wire_schema_version"] = WIRE_SCHEMA_VERSION
            metadata["wire_prompt_version"] = WIRE_PROMPT_VERSION
            metadata["canonical_schema_version"] = CANONICAL_SCHEMA_VERSION
            metadata["symbol_table_sha256"] = table.symbol_table_sha256
            metadata["wire_payload_sha256"] = wire_payload_sha256
            if getattr(exc, "validation_categories", ()):
                metadata["validation_categories"] = list(exc.validation_categories)
            exc.provider_metadata = metadata
            raise
        input_tokens = 0
        output_tokens = 0
        usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
        if isinstance(usage, Mapping):
            try:
                input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            except (TypeError, ValueError):
                input_tokens = 0
            try:
                output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            except (TypeError, ValueError):
                output_tokens = 0
        result["usage"] = {"prompt_tokens": max(0, input_tokens), "completion_tokens": max(0, output_tokens)}
        return result


__all__ = [
    "WIRE_SCHEMA_VERSION",
    "CANONICAL_SCHEMA_VERSION",
    "WIRE_PROMPT_VERSION",
    "WIRE_FIELDS",
    "WIRE_EVIDENCE_FIELDS",
    "SemanticWireParseError",
    "SymbolMessage",
    "SymbolEvidence",
    "RequestLocalSymbolTable",
    "build_symbol_table",
    "validate_symbol_table",
    "compact_wire_payload",
    "parse_wire_frame",
    "assemble_wire_bundle",
    "wire_frame_exemplar",
    "SemanticWireBundleModel",
]
