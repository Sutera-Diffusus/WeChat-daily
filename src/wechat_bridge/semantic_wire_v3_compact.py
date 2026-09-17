"""Compact multi-claim semantic wire protocol.

This module is a provider boundary for the C2.10 shadow pilot.  The model
sees only request-local handles and bounded message text.  Authoritative
message, chat, speaker, scope, and span metadata stays in the local symbol
table.  A response is accepted only when it is one JSON object with the exact
short-key/position schema below; no semantic repair or type coercion is done.

The compact frame has four top-level keys in this order: v (protocol version),
q (claim tuples), r (claim-to-claim relations), and u (uncertainty rows).  A
claim tuple has eleven fixed positions: k, subject, mentioned_person, target,
object, action, claim_type, state, modality, coreference_candidates, and
evidence_map.  Empty arrays and unknown are deliberate conservative outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .bundle_semantics import (
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
from .semantic_wire import (
    RequestLocalSymbolTable,
    build_symbol_table,
    validate_symbol_table,
)


V3_SCHEMA_VERSION = "semantic_wire_v3_compact"
V3_PROMPT_VERSION = "semantic_wire_v3_compact_prompt_v1"
V3_RULESET_VERSION = "semantic_wire_v3_compact_rules_v1"
CANONICAL_SCHEMA_VERSION = BUNDLE_SCHEMA_VERSION
COMPACT_SCHEMA_VERSION = V3_SCHEMA_VERSION
COMPACT_PROMPT_VERSION = V3_PROMPT_VERSION

FRAME_KEYS = ("v", "q", "r", "u")
CLAIM_TUPLE_LENGTH = 11
CLAIM_FIELDS = (
    "claim_handle",
    "subject",
    "mentioned_person",
    "target",
    "object",
    "action",
    "claim_type",
    "state",
    "modality",
    "coreference_candidates",
    "evidence_map",
)
ENTITY_KEYS = ("i", "t", "r", "d", "e")
ACTION_KEYS = ("l", "d", "e")
COREF_KEYS = ("s", "t", "r", "p", "e")
RELATION_KEYS = ("s", "t", "l", "w", "g", "e")
UNCERTAINTY_KEYS = ("c", "f", "s")
EVIDENCE_KEYS = ("s", "p", "t", "o", "a", "c", "x", "d", "f")
EVIDENCE_FIELD_BY_KEY = {
    "s": "subject",
    "p": "mentioned_person",
    "t": "target",
    "o": "object",
    "a": "action",
    "c": "claim_type",
    "x": "state",
    "d": "modality",
    "f": "coreference_candidates",
}


class CompactWireParseError(SemanticFrameParseError):
    """Stable body-free parser/validation error for the compact protocol."""

    def __init__(self, code: str, *, diagnostics: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(code)
        self.code = str(code)
        self.diagnostics = dict(diagnostics or {})


def _require_type(value: Any, expected: type, code: str) -> None:
    if type(value) is not expected:
        raise CompactWireParseError(code)


def _require_string(value: Any, code: str, *, max_length: int = 240) -> None:
    _require_type(value, str, code)
    if len(value) > int(max_length) or chr(10) in value or chr(13) in value:
        raise CompactWireParseError(code)


def _require_string_list(value: Any, code: str, *, max_length: int = 240) -> None:
    _require_type(value, list, code)
    for item in value:
        _require_string(item, code, max_length=max_length)


def _strict_json(text: Any) -> Any:
    def reject_constant(_value: str) -> Any:
        raise CompactWireParseError("compact_nonstandard_json")

    def reject_duplicate(pairs: List[Tuple[Any, Any]]) -> Dict[Any, Any]:
        output: Dict[Any, Any] = {}
        for key, value in pairs:
            if key in output:
                raise CompactWireParseError(
                    "compact_duplicate_key",
                    diagnostics={"duplicate": [str(key)]},
                )
            output[key] = value
        return output

    if not isinstance(text, str):
        raise CompactWireParseError("compact_response_not_text")
    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate,
        )
    except CompactWireParseError:
        raise
    except (TypeError, ValueError) as exc:
        raise CompactWireParseError("compact_invalid_json") from exc


def _exact_mapping(value: Any, keys: Sequence[str], code: str) -> Dict[str, Any]:
    _require_type(value, dict, code)
    if tuple(value.keys()) != tuple(keys):
        missing = [key for key in keys if key not in value]
        extra = [str(key) for key in value if key not in keys]
        order_error = bool(not missing and not extra and tuple(value.keys()) != tuple(keys))
        raise CompactWireParseError(
            code,
            diagnostics={
                "missing_field_names": missing,
                "extra_field_names": extra,
                "order_error": order_error,
            },
        )
    return value


def _handle_list(
    value: Any,
    table: RequestLocalSymbolTable,
    code: str = "compact_evidence_handles",
) -> List[str]:
    _require_type(value, list, code + "_type")
    handles = list(value)
    for item in handles:
        _require_string(item, code + "_item")
        if item not in table.evidence_by_handle:
            raise CompactWireParseError("compact_unknown_evidence_handle")
    if len(handles) != len(set(handles)):
        raise CompactWireParseError("compact_duplicate_evidence_handle")
    return handles


def _validate_semantic_id(value: Any, table: RequestLocalSymbolTable, code: str) -> str:
    _require_string(value, code + "_type")
    if value == UNKNOWN:
        return value
    if len(value) >= 2 and value[1:].isdigit() and value[0] in {"m", "f", "e", "c", "b"}:
        raise CompactWireParseError(code + "_forged_local_handle")
    if len(value) >= 2 and value[1:].isdigit() and value[0] == "p":
        speaker_handles = {item.speaker_handle for item in table.messages}
        if value not in speaker_handles:
            raise CompactWireParseError(code + "_unknown_speaker_handle")
    return value


def _validate_entity(
    value: Any,
    *,
    role: str,
    table: RequestLocalSymbolTable,
) -> Dict[str, Any]:
    row = _exact_mapping(value, ENTITY_KEYS, "compact_entity_shape")
    identifier = _validate_semantic_id(row["i"], table, "compact_entity_id")
    _require_string(row["t"], "compact_entity_type")
    if row["t"] not in ENTITY_TYPES:
        raise CompactWireParseError("compact_entity_enum")
    _require_string(row["r"], "compact_entity_role")
    if row["r"] not in {role, UNKNOWN}:
        raise CompactWireParseError("compact_entity_role_mismatch")
    _require_string(row["d"], "compact_entity_resolution")
    if row["d"] not in ENTITY_RESOLUTIONS:
        raise CompactWireParseError("compact_entity_resolution_enum")
    handles = _handle_list(row["e"], table)
    if identifier != UNKNOWN and not handles:
        raise CompactWireParseError("compact_known_entity_without_evidence")
    return row


def _validate_entity_list(value: Any, *, role: str, table: RequestLocalSymbolTable) -> List[Dict[str, Any]]:
    _require_type(value, list, "compact_entity_list_type")
    return [_validate_entity(item, role=role, table=table) for item in value]


def _validate_action_list(value: Any, table: RequestLocalSymbolTable) -> List[Dict[str, Any]]:
    _require_type(value, list, "compact_action_list_type")
    output: List[Dict[str, Any]] = []
    for item in value:
        row = _exact_mapping(item, ACTION_KEYS, "compact_action_shape")
        _require_string(row["l"], "compact_action_label")
        _require_string(row["d"], "compact_action_resolution")
        if row["d"] not in ENTITY_RESOLUTIONS:
            raise CompactWireParseError("compact_action_resolution_enum")
        handles = _handle_list(row["e"], table, "compact_action_evidence")
        if row["l"] != UNKNOWN and not handles:
            raise CompactWireParseError("compact_known_action_without_evidence")
        output.append(row)
    return output


def _validate_coref_list(value: Any, table: RequestLocalSymbolTable) -> List[Dict[str, Any]]:
    _require_type(value, list, "compact_coreference_list_type")
    output: List[Dict[str, Any]] = []
    for item in value:
        row = _exact_mapping(item, COREF_KEYS, "compact_coreference_shape")
        source = _validate_semantic_id(row["s"], table, "compact_coreference_source")
        target = _validate_semantic_id(row["t"], table, "compact_coreference_target")
        if source == UNKNOWN or target == UNKNOWN or source == target:
            raise CompactWireParseError("compact_coreference_endpoint")
        _require_string(row["r"], "compact_coreference_relation")
        if type(row["p"]) not in {int, float} or isinstance(row["p"], bool) or not 0.0 <= float(row["p"]) <= 1.0:
            raise CompactWireParseError("compact_coreference_score")
        handles = _handle_list(row["e"], table, "compact_coreference_evidence")
        if not handles:
            raise CompactWireParseError("compact_coreference_without_evidence")
        output.append(row)
    return output


def _validate_claim_handle(value: Any, known: Sequence[str]) -> str:
    _require_string(value, "compact_claim_handle_type")
    if value not in known:
        raise CompactWireParseError("compact_claim_handle_unknown")
    return value


def _validate_relation_list(value: Any, table: RequestLocalSymbolTable, claim_handles: Sequence[str]) -> List[Dict[str, Any]]:
    _require_type(value, list, "compact_relation_list_type")
    output: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for item in value:
        row = _exact_mapping(item, RELATION_KEYS, "compact_relation_shape")
        source = _validate_claim_handle(row["s"], claim_handles)
        target = _validate_claim_handle(row["t"], claim_handles)
        if source == target:
            raise CompactWireParseError("compact_relation_self_link")
        pair = (source, target)
        if pair in seen:
            raise CompactWireParseError("compact_relation_duplicate")
        seen.add(pair)
        _require_string(row["l"], "compact_relation_label")
        if row["l"] not in RELATION_LABELS:
            raise CompactWireParseError("compact_relation_label_enum")
        _require_string(row["w"], "compact_relation_strength")
        if row["w"] not in RELATION_STRENGTHS:
            raise CompactWireParseError("compact_relation_strength_enum")
        _require_string_list(row["g"], "compact_relation_signal_type")
        if len(row["g"]) != len(set(row["g"])):
            raise CompactWireParseError("compact_relation_signal_duplicate")
        handles = _handle_list(row["e"], table, "compact_relation_evidence")
        only_time = bool(row["g"]) and set(row["g"]).issubset(
            {"time", "temporal", "time_near", "time_proximity", "time_distance"}
        )
        if row["w"] == "strong" and only_time:
            raise CompactWireParseError("compact_time_only_strong_forbidden")
        if row["l"] != "insufficient" and row["w"] != "none" and not handles:
            raise CompactWireParseError("compact_relation_without_evidence")
        output.append(row)
    return output


def _validate_uncertainties(value: Any) -> List[Dict[str, Any]]:
    _require_type(value, list, "compact_uncertainty_list_type")
    output: List[Dict[str, Any]] = []
    for item in value:
        row = _exact_mapping(item, UNCERTAINTY_KEYS, "compact_uncertainty_shape")
        _require_string(row["c"], "compact_uncertainty_code")
        _require_string(row["f"], "compact_uncertainty_field")
        _require_string(row["s"], "compact_uncertainty_severity")
        output.append(row)
    return output


def _validate_evidence_map(value: Any, table: RequestLocalSymbolTable) -> Dict[str, List[str]]:
    row = _exact_mapping(value, EVIDENCE_KEYS, "compact_evidence_map_shape")
    return {
        key: _handle_list(row[key], table, "compact_evidence_" + key)
        for key in EVIDENCE_KEYS
    }


def _validate_claim_tuple(value: Any, table: RequestLocalSymbolTable, expected_handle: str) -> List[Any]:
    _require_type(value, list, "compact_claim_tuple_type")
    if len(value) != CLAIM_TUPLE_LENGTH:
        raise CompactWireParseError("compact_claim_tuple_length")
    if value[0] != expected_handle:
        raise CompactWireParseError("compact_claim_handle_order")
    _validate_entity(value[1], role="subject", table=table)
    for index, role in ((2, "mentioned_person"), (3, "target"), (4, "object")):
        _validate_entity_list(value[index], role=role, table=table)
    _validate_action_list(value[5], table)
    for index, allowed, code in (
        (6, CLAIM_TYPES, "compact_claim_type"),
        (7, STATES, "compact_state"),
        (8, MODALITIES, "compact_modality"),
    ):
        _require_string(value[index], code + "_type")
        if value[index] not in allowed:
            raise CompactWireParseError(code + "_enum")
    corefs = _validate_coref_list(value[9], table)
    evidence_map = _validate_evidence_map(value[10], table)
    for index, key in ((1, "s"), (2, "p"), (3, "t"), (4, "o"), (5, "a"), (6, "c"), (7, "x"), (8, "d"), (9, "f")):
        if index in {1, 2, 3, 4}:
            values = value[index]
            known = any(item.get("i") != UNKNOWN for item in values) if isinstance(values, list) else value[index].get("i") != UNKNOWN
        elif index == 5:
            known = any(item.get("l") != UNKNOWN for item in value[index])
        elif index == 9:
            known = bool(corefs)
        else:
            known = value[index] != UNKNOWN
        if known and not evidence_map[key]:
            raise CompactWireParseError("compact_slot_missing_evidence")
    return value


def parse_compact_frame(
    text: Any,
    *,
    symbol_table: RequestLocalSymbolTable,
    max_claims: int,
) -> Dict[str, Any]:
    """Parse exactly one compact JSON frame, fail-closed."""

    validate_symbol_table(symbol_table)
    if int(max_claims) < 1:
        raise ValueError("max_claims must be positive")
    return _validate_frame_mapping(
        _strict_json(text),
        symbol_table=symbol_table,
        max_claims=max_claims,
    )


def _validate_frame_mapping(
    frame: Any,
    *,
    symbol_table: RequestLocalSymbolTable,
    max_claims: int,
) -> Dict[str, Any]:
    row = _exact_mapping(frame, FRAME_KEYS, "compact_frame_shape")
    if row["v"] != V3_SCHEMA_VERSION:
        raise CompactWireParseError("compact_version_mismatch")
    _require_type(row["q"], list, "compact_claims_type")
    if not row["q"] or len(row["q"]) > int(max_claims):
        raise CompactWireParseError("compact_claim_count")
    expected_handles = ["k%d" % index for index in range(len(row["q"]))]
    claims = [
        _validate_claim_tuple(item, symbol_table, expected_handles[index])
        for index, item in enumerate(row["q"])
    ]
    relations = _validate_relation_list(row["r"], symbol_table, expected_handles)
    uncertainties = _validate_uncertainties(row["u"])
    return {"v": row["v"], "q": claims, "r": relations, "u": uncertainties}


def compact_frame_exemplar(max_claims: int = 1) -> str:
    """Return the shortest complete all-unknown provider exemplar."""

    count = max(1, int(max_claims))
    def make_claim(index: int) -> List[Any]:
        return [
            "k%d" % index,
            {"i": UNKNOWN, "t": "person", "r": "subject", "d": UNKNOWN, "e": []},
            [],
            [],
            [],
            [],
            UNKNOWN,
            UNKNOWN,
            UNKNOWN,
            [],
            {key: [] for key in EVIDENCE_KEYS},
        ]
    frame = {"v": V3_SCHEMA_VERSION, "q": [make_claim(index) for index in range(count)], "r": [], "u": []}
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def compact_wire_payload(
    table: RequestLocalSymbolTable,
    *,
    max_claims: int,
    max_chars: int = 1800,
) -> Tuple[Dict[str, Any], int]:
    """Create a short-handle provider payload and bound only message text."""

    validate_symbol_table(table)
    if int(max_claims) < 1:
        raise ValueError("max_claims must be positive")
    def build(limit: int) -> Dict[str, Any]:
        messages = [
            [
                item.message_handle,
                table.chat_handle,
                item.speaker_handle,
                "text",
                item.content[: max(0, int(limit))],
            ]
            for item in table.messages
        ]
        spans = [[item.span_handle, item.message_handle, item.span_start, item.span_end] for item in table.messages]
        evidence = [[item.evidence_handle, item.message_handle, item.span_handle] for item in table.messages]
        return {
            "v": V3_SCHEMA_VERSION,
            "b": table.bundle_handle,
            "c": table.chat_handle,
            "n": int(max_claims),
            "m": messages,
            "f": spans,
            "e": evidence,
        }
    payload = build(120)
    encoded = _canonical_json(payload)
    if len(encoded) > int(max_chars):
        payload = build(32)
        encoded = _canonical_json(payload)
    if len(encoded) > int(max_chars):
        payload = build(0)
        encoded = _canonical_json(payload)
    return payload, len(encoded)


def build_compact_request(
    messages: Sequence[Mapping[str, Any]],
    *,
    bundle_id: str,
    chat_id: str,
    max_claims: int,
    account_id: Optional[str] = None,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a request carrying bounded content only in memory."""

    bounded: List[Dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise CompactWireParseError("compact_request_message_type")
        row = dict(item)
        content = row.get("content")
        if isinstance(content, str):
            row["content"] = content[:240]
        bounded.append(row)
    request: Dict[str, Any] = {
        "bundle_id": str(bundle_id),
        "chat_id": str(chat_id),
        "messages": bounded,
        "max_claims": int(max_claims),
        "wire_schema_version": V3_SCHEMA_VERSION,
        "wire_prompt_version": V3_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
    }
    if account_id is not None:
        request["account_id"] = str(account_id)
    if scope is not None:
        request["scope"] = str(scope)
    table = build_symbol_table(request)
    request["symbol_table_sha256"] = table.symbol_table_sha256
    return request


def _speaker_id_for_handle(table: RequestLocalSymbolTable, handle: str) -> str:
    values = {
        item.speaker_id
        for item in table.messages
        if item.speaker_handle == handle and item.speaker_id not in {"", UNKNOWN}
    }
    return next(iter(values)) if len(values) == 1 else UNKNOWN


def _evidence_row(
    table: RequestLocalSymbolTable,
    handle: str,
    *,
    field: str,
) -> Tuple[str, Dict[str, Any]]:
    item = table.evidence_by_handle.get(handle)
    if item is None:
        raise CompactWireParseError("compact_unknown_evidence_handle")
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


def _add_evidence(
    table: RequestLocalSymbolTable,
    handles: Iterable[str],
    *,
    field: str,
    evidence: List[Dict[str, Any]],
) -> List[str]:
    result: List[str] = []
    for handle in handles:
        evidence_id, row = _evidence_row(table, handle, field=field)
        if evidence_id not in result:
            result.append(evidence_id)
        if not any(item.get("evidence_id") == evidence_id for item in evidence):
            evidence.append(row)
    return result


def _canonical_entity(
    value: Mapping[str, Any],
    *,
    role: str,
    table: RequestLocalSymbolTable,
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    identifier = str(value["i"])
    if len(identifier) >= 2 and identifier[0] == "p" and identifier[1:].isdigit():
        resolved = _speaker_id_for_handle(table, identifier)
        identifier = resolved if resolved != UNKNOWN else UNKNOWN
    evidence_ids = _add_evidence(
        table,
        value["e"],
        field=role,
        evidence=evidence,
    )
    return {
        "id": identifier,
        "type": value["t"],
        "role": role,
        "resolution": value["d"] if identifier != UNKNOWN else UNKNOWN,
        "evidence_ids": evidence_ids,
    }


def _canonical_action(
    value: Mapping[str, Any],
    *,
    table: RequestLocalSymbolTable,
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    evidence_ids = _add_evidence(
        table,
        value["e"],
        field="action",
        evidence=evidence,
    )
    return {
        "id": UNKNOWN,
        "label": value["l"],
        "resolution": value["d"] if value["l"] != UNKNOWN else UNKNOWN,
        "evidence_ids": evidence_ids,
    }


def _canonical_coreference(
    value: Mapping[str, Any],
    *,
    table: RequestLocalSymbolTable,
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    def resolve(identifier: str) -> str:
        if len(identifier) >= 2 and identifier[0] == "p" and identifier[1:].isdigit():
            mapped = _speaker_id_for_handle(table, identifier)
            return mapped if mapped != UNKNOWN else UNKNOWN
        return identifier
    evidence_ids = _add_evidence(
        table,
        value["e"],
        field="coreference_candidates",
        evidence=evidence,
    )
    return {
        "source_id": resolve(value["s"]),
        "target_id": resolve(value["t"]),
        "relation": value["r"],
        "score": float(value["p"]),
        "evidence_ids": evidence_ids,
    }


def _canonical_uncertainty(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {"code": value["c"], "field": value["f"], "severity": value["s"]}


def assemble_compact_frame(
    frame: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    symbol_table: RequestLocalSymbolTable,
    wire_payload_sha256: str = "",
) -> List[Dict[str, Any]]:
    """Assemble every claim into a complete canonical bundle locally."""

    validate_symbol_table(symbol_table)
    if not isinstance(frame, Mapping):
        raise CompactWireParseError("compact_frame_not_object")
    claims = frame.get("q")
    relations = frame.get("r")
    uncertainties = frame.get("u")
    if not isinstance(claims, list) or not claims:
        raise CompactWireParseError("compact_claims_missing")
    parsed = _validate_frame_mapping(
        frame,
        symbol_table=symbol_table,
        max_claims=int(request.get("max_claims") or len(claims)),
    )
    claims = parsed["q"]
    claim_handles = [str(item[0]) for item in claims]
    bundle_ids = {
        handle: (
            symbol_table.bundle_id
            if len(claims) == 1
            else symbol_table.bundle_id + "::" + handle
        )
        for handle in claim_handles
    }
    output: List[Dict[str, Any]] = []
    top_uncertainties = [_canonical_uncertainty(item) for item in parsed["u"]]
    for claim in claims:
        handle = str(claim[0])
        evidence: List[Dict[str, Any]] = []
        bundle = empty_bundle(
            bundle_ids[handle],
            symbol_table.message_ids,
            chat_id=symbol_table.chat_id,
            status="complete",
            source="model_wire_v3_compact",
            schema_version=CANONICAL_SCHEMA_VERSION,
        )
        speaker_id = symbol_table.authoritative_speaker_id
        speaker_evidence: List[Dict[str, Any]] = []
        speaker_evidence_ids: List[str] = []
        if speaker_id != UNKNOWN:
            speaker_handle = symbol_table.authoritative_speaker_handle
            if speaker_handle == UNKNOWN:
                raise CompactWireParseError("compact_authoritative_speaker_missing")
            speaker_evidence_id, speaker_row = _evidence_row(
                symbol_table,
                next(
                    item.evidence_handle
                    for item in symbol_table.messages
                    if item.speaker_handle == speaker_handle
                ),
                field="speaker",
            )
            speaker_evidence.append(speaker_row)
            speaker_evidence_ids.append(speaker_evidence_id)
        bundle["speaker"] = {
            "id": speaker_id,
            "type": "person",
            "role": "speaker",
            "resolution": "explicit" if speaker_id != UNKNOWN else UNKNOWN,
            "evidence_ids": speaker_evidence_ids,
        }
        evidence.extend(speaker_evidence)
        bundle["subject"] = _canonical_entity(
            claim[1],
            role="subject",
            table=symbol_table,
            evidence=evidence,
        )
        bundle["mentioned_person"] = [
            _canonical_entity(item, role="mentioned_person", table=symbol_table, evidence=evidence)
            for item in claim[2]
        ]
        bundle["target"] = [
            _canonical_entity(item, role="target", table=symbol_table, evidence=evidence)
            for item in claim[3]
        ]
        bundle["object"] = [
            _canonical_entity(item, role="object", table=symbol_table, evidence=evidence)
            for item in claim[4]
        ]
        bundle["action"] = [
            _canonical_action(item, table=symbol_table, evidence=evidence)
            for item in claim[5]
        ]
        evidence_map = claim[10]
        for field, key in (("claim_type", "c"), ("state", "x"), ("modality", "d")):
            bundle[field] = claim[6 + {"claim_type": 0, "state": 1, "modality": 2}[field]]
            _add_evidence(
                symbol_table,
                evidence_map[key],
                field=field,
                evidence=evidence,
            )
        bundle["coreference_candidates"] = [
            _canonical_coreference(item, table=symbol_table, evidence=evidence)
            for item in claim[9]
        ]
        bundle["uncertainties"] = list(top_uncertainties)
        relation_values: List[Dict[str, Any]] = []
        for relation in parsed["r"]:
            if relation["s"] != handle:
                continue
            relation_evidence_ids = _add_evidence(
                symbol_table,
                relation["e"],
                field="context_relations",
                evidence=evidence,
            )
            relation_values.append(
                {
                    "relation_id": "relation:%s" % stable_hash(
                        {
                            "symbol_table": symbol_table.symbol_table_sha256,
                            "source": relation["s"],
                            "target": relation["t"],
                            "label": relation["l"],
                        }
                    )[:24],
                    "source_bundle_id": bundle_ids[relation["s"]],
                    "target_bundle_id": bundle_ids[relation["t"]],
                    "label": relation["l"],
                    "strength": relation["w"],
                    "evidence_ids": relation_evidence_ids,
                    "supporting_signals": list(relation["g"]),
                    "explicit_reply_present": False,
                    "left_chat_id": symbol_table.chat_id,
                    "right_chat_id": symbol_table.chat_id,
                }
            )
        bundle["context_relations"] = relation_values
        bundle["evidence"] = evidence
        bundle["metadata"].update(
            {
                "wire_schema_version": V3_SCHEMA_VERSION,
                "wire_prompt_version": V3_PROMPT_VERSION,
                "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                "symbol_table_sha256": symbol_table.symbol_table_sha256,
                "wire_payload_sha256": wire_payload_sha256 or stable_hash(frame),
                "scope": symbol_table.scope,
                "account_id": symbol_table.account_id,
                "claim_handle": handle,
                "claim_count": len(claims),
                "multi_claim": len(claims) > 1,
            }
        )
        report = validate_bundle(
            bundle,
            message_ids=symbol_table.message_ids,
            chat_id=symbol_table.chat_id,
            expected_schema_version=CANONICAL_SCHEMA_VERSION,
        )
        if not report.ok:
            raise CompactWireParseError(
                "compact_canonical_validation_failed",
                diagnostics={"validation_categories": list(report.errors)},
            )
        output.append(bundle)
    return output


def assemble_compact_bundles(
    frame: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    symbol_table: RequestLocalSymbolTable,
    wire_payload_sha256: str = "",
) -> List[Dict[str, Any]]:
    """Descriptive alias for callers that prefer the plural return name."""

    return assemble_compact_frame(
        frame,
        request,
        symbol_table=symbol_table,
        wire_payload_sha256=wire_payload_sha256,
    )


class SemanticWireV3CompactBundleModel(SemanticFrameBundleModel):
    """OpenAI-compatible adapter for one strict compact multi-claim frame."""

    source = "openai-semantic-wire-v3-compact"
    protocol_version = V3_SCHEMA_VERSION

    def __init__(
        self,
        config: Optional[Any] = None,
        *,
        thinking_disabled: bool = True,
        max_input_chars: int = 1800,
        max_claims: int = 2,
    ) -> None:
        super().__init__(
            config,
            thinking_disabled=thinking_disabled,
            max_input_chars=max_input_chars,
        )
        if int(max_claims) < 1:
            raise ValueError("max_claims must be positive")
        self.max_claims = int(max_claims)

    def cache_context(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        table = build_symbol_table(request)
        return {
            "wire_schema_version": V3_SCHEMA_VERSION,
            "wire_prompt_version": V3_PROMPT_VERSION,
            "ruleset_version": V3_RULESET_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "symbol_table_sha256": table.symbol_table_sha256,
            "max_claims": int(request.get("max_claims") or self.max_claims),
            "scope": table.scope,
        }

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        table = build_symbol_table(request)
        max_claims = int(request.get("max_claims") or self.max_claims)
        if max_claims < 1 or max_claims > self.max_claims:
            raise CompactWireParseError("compact_max_claims_mismatch")
        payload, input_chars = compact_wire_payload(
            table,
            max_claims=max_claims,
            max_chars=self.max_input_chars,
        )
        if input_chars > self.max_input_chars:
            raise BudgetExceeded("compact_wire_input_payload_limit_exceeded")
        wire_payload_sha256 = stable_hash(payload)
        instructions = (
            "Return exactly one JSON object, no prose, no markdown. Use the exact top-level "
            "key order v,q,r,u. v must be semantic_wire_v3_compact. q is one to "
            + str(max_claims)
            + " claim arrays, each exactly 11 positions in this order: k,subject,"
            "mentioned_person,target,object,action,claim_type,state,modality,"
            "coreference_candidates,evidence_map. Use the exact short object keys "
            "entity i,t,r,d,e; action l,d,e; coreference s,t,r,p,e; relation "
            "s,t,l,w,g,e; uncertainty c,f,s; evidence map s,p,t,o,a,c,x,d,f. "
            "Claim handles are k0,k1 in order. Use only supplied eN evidence handles "
            "and supplied pN speaker handles; never emit message IDs, chat IDs, scope, "
            "bundle IDs, spans, or new handles. Speaker is authoritative locally and "
            "must not be copied into subject or mentioned_person without textual support. "
            "Known entities, actions, claim type, state, modality, coreference, and "
            "relations require evidence; otherwise use unknown or an empty array. "
            "Unknown is valid. Keep each relation local to this frame; time alone cannot "
            "justify strong. The following is an exemplar only; do not copy its handles "
            "unless they occur in the supplied payload: "
            + compact_frame_exemplar(max_claims)
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
            frame = parse_compact_frame(
                text,
                symbol_table=table,
                max_claims=max_claims,
            )
            bundles = assemble_compact_frame(
                frame,
                request,
                symbol_table=table,
                wire_payload_sha256=wire_payload_sha256,
            )
        except CompactWireParseError as exc:
            metadata = self._safe_response_metadata(response, response_text=text)
            metadata.update(exc.diagnostics)
            metadata["wire_schema_version"] = V3_SCHEMA_VERSION
            metadata["wire_prompt_version"] = V3_PROMPT_VERSION
            metadata["canonical_schema_version"] = CANONICAL_SCHEMA_VERSION
            metadata["symbol_table_sha256"] = table.symbol_table_sha256
            metadata["wire_payload_sha256"] = wire_payload_sha256
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
        return {
            "claims": bundles,
            "bundle": bundles[0] if len(bundles) == 1 else None,
            "usage": {
                "prompt_tokens": max(0, input_tokens),
                "completion_tokens": max(0, output_tokens),
            },
            "metadata": {
                "wire_schema_version": V3_SCHEMA_VERSION,
                "wire_prompt_version": V3_PROMPT_VERSION,
                "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                "symbol_table_sha256": table.symbol_table_sha256,
                "wire_payload_sha256": wire_payload_sha256,
                "claim_count": len(bundles),
            },
        }


CompactMultiClaimModel = SemanticWireV3CompactBundleModel
SemanticWireCompactBundleModel = SemanticWireV3CompactBundleModel


__all__ = [
    "V3_SCHEMA_VERSION",
    "V3_PROMPT_VERSION",
    "V3_RULESET_VERSION",
    "CANONICAL_SCHEMA_VERSION",
    "COMPACT_SCHEMA_VERSION",
    "COMPACT_PROMPT_VERSION",
    "FRAME_KEYS",
    "CLAIM_FIELDS",
    "CLAIM_TUPLE_LENGTH",
    "ENTITY_KEYS",
    "ACTION_KEYS",
    "COREF_KEYS",
    "RELATION_KEYS",
    "UNCERTAINTY_KEYS",
    "EVIDENCE_KEYS",
    "CompactWireParseError",
    "build_compact_request",
    "compact_wire_payload",
    "compact_frame_exemplar",
    "parse_compact_frame",
    "assemble_compact_frame",
    "assemble_compact_bundles",
    "SemanticWireV3CompactBundleModel",
    "CompactMultiClaimModel",
    "SemanticWireCompactBundleModel",
]
