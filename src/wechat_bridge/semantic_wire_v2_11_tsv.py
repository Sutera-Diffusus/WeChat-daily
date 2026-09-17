"""Strict multi-line TSV semantic wire protocol for C2.11.

The provider emits only fixed-width TSV rows.  CLAIM rows are ordered
implicitly and contain no free-text semantic labels except a closed action
enum.  REL rows refer to claim indexes in the same frame.  All handles are
checked against the request-local symbol table and authoritative speaker,
chat, scope, message, and span metadata are assembled locally.
"""

from __future__ import annotations

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
from .semantic_wire import RequestLocalSymbolTable, build_symbol_table, validate_symbol_table
from .semantic_wire_v3_compact import _add_evidence, _evidence_row, _speaker_id_for_handle


TSV_SCHEMA_VERSION = "semantic_wire_v2_11_tsv"
TSV_PROMPT_VERSION = "semantic_wire_v2_11_tsv_prompt_v1"
TSV_RULESET_VERSION = "semantic_wire_v2_11_tsv_rules_v1"
CANONICAL_SCHEMA_VERSION = BUNDLE_SCHEMA_VERSION

CLAIM_TAG = "CLAIM"
RELATION_TAG = "REL"
CLAIM_COLUMN_COUNT = 11
RELATION_COLUMN_COUNT = 7
ACTION_ENUM = frozenset(
    {"assert", "inform", "ask", "request", "suggest", "plan", "resolve", "cancel", "fail", "mention", UNKNOWN}
)
UNCERTAINTY_ENUM = frozenset(
    {
        "none",
        "subject",
        "mentioned_person",
        "target",
        "object",
        "action",
        "claim_type",
        "state",
        "modality",
        "relation",
        "multiple",
        UNKNOWN,
    }
)
RELATION_SIGNAL_ENUM = frozenset(
    {
        "subject",
        "object",
        "state",
        "claim_type",
        "speaker",
        "mention",
        "lexical",
        "question",
        "request",
        "explicit_reply",
        "topic",
        "time",
        "temporal",
        "same_segment",
        UNKNOWN,
    }
)


class TsvWireParseError(SemanticFrameParseError):
    """Stable body-free TSV parser/validation error."""

    def __init__(self, code: str, *, diagnostics: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(code)
        self.code = str(code)
        self.diagnostics = dict(diagnostics or {})


def _require_string(value: Any, code: str, *, max_length: int = 120) -> str:
    if type(value) is not str or not value or len(value) > int(max_length):
        raise TsvWireParseError(code)
    if chr(9) in value or chr(10) in value or chr(13) in value:
        raise TsvWireParseError(code)
    return value


def _csv(value: str, *, code: str, allow_empty: bool = True) -> List[str]:
    if type(value) is not str:
        raise TsvWireParseError(code + "_type")
    if value == "":
        if allow_empty:
            return []
        raise TsvWireParseError(code + "_empty")
    parts = value.split(",")
    if any(not item for item in parts) or len(parts) != len(set(parts)):
        raise TsvWireParseError(code + "_duplicate_or_empty")
    for item in parts:
        _require_string(item, code + "_item")
    return parts


def _speaker_handle(value: str, table: RequestLocalSymbolTable, code: str) -> str:
    if value == UNKNOWN:
        return value
    if len(value) < 2 or value[0] != "p" or not value[1:].isdigit():
        raise TsvWireParseError(code + "_not_speaker_handle")
    known = {item.speaker_handle for item in table.messages}
    if value not in known:
        raise TsvWireParseError(code + "_unknown")
    return value


def _evidence_handles(value: str, table: RequestLocalSymbolTable, code: str) -> Tuple[str, ...]:
    values = _csv(value, code=code, allow_empty=True)
    for item in values:
        if len(item) < 2 or item[0] != "e" or not item[1:].isdigit():
            raise TsvWireParseError(code + "_not_evidence_handle")
        if item not in table.evidence_by_handle:
            raise TsvWireParseError("tsv_unknown_evidence_handle")
    return tuple(values)


def _parse_claim(parts: Sequence[str], table: RequestLocalSymbolTable) -> Dict[str, Any]:
    if len(parts) != CLAIM_COLUMN_COUNT or parts[0] != CLAIM_TAG:
        raise TsvWireParseError("tsv_claim_column_count")
    subject = _speaker_handle(_require_string(parts[1], "tsv_subject"), table, "tsv_subject")
    mentioned = tuple(
        _speaker_handle(item, table, "tsv_mentioned_person")
        for item in _csv(parts[2], code="tsv_mentioned_person", allow_empty=True)
    )
    target = _speaker_handle(_require_string(parts[3], "tsv_target"), table, "tsv_target")
    obj = _speaker_handle(_require_string(parts[4], "tsv_object"), table, "tsv_object")
    action = _require_string(parts[5], "tsv_action")
    if action not in ACTION_ENUM:
        raise TsvWireParseError("tsv_action_enum")
    claim_type = _require_string(parts[6], "tsv_claim_type")
    if claim_type not in CLAIM_TYPES:
        raise TsvWireParseError("tsv_claim_type_enum")
    state = _require_string(parts[7], "tsv_state")
    if state not in STATES:
        raise TsvWireParseError("tsv_state_enum")
    modality = _require_string(parts[8], "tsv_modality")
    if modality not in MODALITIES:
        raise TsvWireParseError("tsv_modality_enum")
    evidence = _evidence_handles(parts[9], table, "tsv_evidence")
    uncertainty = _require_string(parts[10], "tsv_uncertainty")
    if uncertainty not in UNCERTAINTY_ENUM:
        raise TsvWireParseError("tsv_uncertainty_enum")
    known_slots = [
        subject != UNKNOWN,
        bool(mentioned),
        target != UNKNOWN,
        obj != UNKNOWN,
        action != UNKNOWN,
        claim_type != UNKNOWN,
        state != UNKNOWN,
        modality != UNKNOWN,
    ]
    if any(known_slots) and not evidence:
        raise TsvWireParseError("tsv_known_slot_without_evidence")
    return {
        "subject": subject,
        "mentioned_person": mentioned,
        "target": target,
        "object": obj,
        "action": action,
        "claim_type": claim_type,
        "state": state,
        "modality": modality,
        "evidence": evidence,
        "uncertainty": uncertainty,
    }


def _parse_relation(
    parts: Sequence[str],
    table: RequestLocalSymbolTable,
    claim_count: int,
) -> Dict[str, Any]:
    if len(parts) != RELATION_COLUMN_COUNT or parts[0] != RELATION_TAG:
        raise TsvWireParseError("tsv_relation_column_count")
    values: List[int] = []
    for index in (1, 2):
        text = _require_string(parts[index], "tsv_relation_index")
        if not text.isdigit():
            raise TsvWireParseError("tsv_relation_index_type")
        value = int(text)
        if value < 0 or value >= claim_count:
            raise TsvWireParseError("tsv_relation_index_scope")
        values.append(value)
    if values[0] == values[1]:
        raise TsvWireParseError("tsv_relation_self_link")
    label = _require_string(parts[3], "tsv_relation_label")
    if label not in RELATION_LABELS:
        raise TsvWireParseError("tsv_relation_label_enum")
    strength = _require_string(parts[4], "tsv_relation_strength")
    if strength not in RELATION_STRENGTHS:
        raise TsvWireParseError("tsv_relation_strength_enum")
    signals = _csv(parts[5], code="tsv_relation_signals", allow_empty=True)
    if any(item not in RELATION_SIGNAL_ENUM for item in signals):
        raise TsvWireParseError("tsv_relation_signal_enum")
    evidence = _evidence_handles(parts[6], table, "tsv_relation_evidence")
    if label != "insufficient" and strength != "none" and not evidence:
        raise TsvWireParseError("tsv_relation_without_evidence")
    only_time = bool(signals) and set(signals).issubset(
        {"time", "temporal"}
    )
    if strength == "strong" and only_time:
        raise TsvWireParseError("tsv_time_only_strong_forbidden")
    return {
        "source": values[0],
        "target": values[1],
        "label": label,
        "strength": strength,
        "signals": tuple(signals),
        "evidence": evidence,
    }


def parse_tsv_frame(
    text: Any,
    *,
    symbol_table: RequestLocalSymbolTable,
    max_claims: int,
) -> Dict[str, Any]:
    """Parse only fixed CLAIM/REL rows; reject blank, prose, or extra columns."""

    validate_symbol_table(symbol_table)
    if type(text) is not str:
        raise TsvWireParseError("tsv_response_not_text")
    lines = text.splitlines()
    if not lines:
        raise TsvWireParseError("tsv_empty")
    claims: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    seen_claims: set[str] = set()
    seen_relations: set[Tuple[int, int]] = set()
    relation_started = False
    for line in lines:
        if not line or line.strip() != line:
            raise TsvWireParseError("tsv_blank_or_whitespace")
        parts = line.split(chr(9))
        if parts[0] == CLAIM_TAG:
            if relation_started:
                raise TsvWireParseError("tsv_claim_after_relation")
            if len(claims) >= int(max_claims):
                raise TsvWireParseError("tsv_claim_count")
            claim = _parse_claim(parts, symbol_table)
            identity = repr(
                (
                    claim["subject"],
                    claim["mentioned_person"],
                    claim["target"],
                    claim["object"],
                    claim["action"],
                    claim["claim_type"],
                    claim["state"],
                    claim["modality"],
                    claim["evidence"],
                    claim["uncertainty"],
                )
            )
            if identity in seen_claims:
                raise TsvWireParseError("tsv_duplicate_claim")
            seen_claims.add(identity)
            claims.append(claim)
        elif parts[0] == RELATION_TAG:
            relation_started = True
            relation = _parse_relation(parts, symbol_table, len(claims))
            pair = (relation["source"], relation["target"])
            if pair in seen_relations:
                raise TsvWireParseError("tsv_duplicate_relation")
            seen_relations.add(pair)
            relations.append(relation)
        else:
            raise TsvWireParseError("tsv_extra_or_unknown_row")
    if not claims:
        raise TsvWireParseError("tsv_claim_missing")
    return {
        "schema_version": TSV_SCHEMA_VERSION,
        "claims": claims,
        "relations": relations,
    }


def _tsv_payload(table: RequestLocalSymbolTable, *, max_claims: int, max_chars: int) -> Tuple[Dict[str, Any], int]:
    def build(limit: int) -> Dict[str, Any]:
        messages = [
            [
                item.message_handle,
                table.chat_handle,
                item.speaker_handle,
                item.content[: max(0, int(limit))],
            ]
            for item in table.messages
        ]
        spans = [[item.span_handle, item.message_handle, item.span_start, item.span_end] for item in table.messages]
        evidence = [[item.evidence_handle, item.message_handle, item.span_handle] for item in table.messages]
        return {
            "v": TSV_SCHEMA_VERSION,
            "n": int(max_claims),
            "m": messages,
            "f": spans,
            "e": evidence,
        }
    payload = build(120)
    size = len(_canonical_json(payload))
    if size > int(max_chars):
        payload = build(32)
        size = len(_canonical_json(payload))
    if size > int(max_chars):
        payload = build(0)
        size = len(_canonical_json(payload))
    return payload, size


def tsv_frame_exemplar() -> str:
    """Smallest valid all-unknown frame used only in the provider prompt."""

    return chr(9).join(
        [
            CLAIM_TAG,
            UNKNOWN,
            "",
            UNKNOWN,
            UNKNOWN,
            UNKNOWN,
            UNKNOWN,
            UNKNOWN,
            UNKNOWN,
            "",
            "none",
        ]
    )


def _entity(
    handle: str,
    *,
    role: str,
    entity_type: str,
    table: RequestLocalSymbolTable,
    evidence: List[Dict[str, Any]],
    evidence_handles: Iterable[str],
) -> Dict[str, Any]:
    identifier = UNKNOWN
    if handle != UNKNOWN:
        identifier = _speaker_id_for_handle(table, handle)
        if identifier == UNKNOWN:
            raise TsvWireParseError("tsv_authoritative_speaker_unresolved")
    evidence_ids = _add_evidence(
        table,
        evidence_handles,
        field=role,
        evidence=evidence,
    )
    return {
        "id": identifier,
        "type": entity_type,
        "role": role,
        "resolution": "explicit" if identifier != UNKNOWN else UNKNOWN,
        "evidence_ids": evidence_ids,
    }


def assemble_tsv_frame(
    frame: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    symbol_table: RequestLocalSymbolTable,
    wire_payload_sha256: str = "",
) -> List[Dict[str, Any]]:
    """Map TSV claims and relations to canonical 17-field bundles locally."""

    validate_symbol_table(symbol_table)
    claims = frame.get("claims")
    relations = frame.get("relations")
    if not isinstance(claims, list) or not claims:
        raise TsvWireParseError("tsv_claim_missing")
    parsed = parse_tsv_frame(
        chr(10).join(
            [
                chr(9).join(
                    [
                        CLAIM_TAG,
                        item["subject"],
                        ",".join(item["mentioned_person"]),
                        item["target"],
                        item["object"],
                        item["action"],
                        item["claim_type"],
                        item["state"],
                        item["modality"],
                        ",".join(item["evidence"]),
                        item["uncertainty"],
                    ]
                )
                for item in claims
            ]
            + [
                chr(9).join(
                    [
                        RELATION_TAG,
                        str(item["source"]),
                        str(item["target"]),
                        item["label"],
                        item["strength"],
                        ",".join(item["signals"]),
                        ",".join(item["evidence"]),
                    ]
                )
                for item in (relations if isinstance(relations, list) else ())
            ]
        ),
        symbol_table=symbol_table,
        max_claims=int(request.get("max_claims") or len(claims)),
    )
    claims = parsed["claims"]
    relations = parsed["relations"]
    bundle_ids = {
        index: (
            symbol_table.bundle_id
            if len(claims) == 1
            else symbol_table.bundle_id + "::k%d" % index
        )
        for index in range(len(claims))
    }
    output: List[Dict[str, Any]] = []
    for index, claim in enumerate(claims):
        evidence: List[Dict[str, Any]] = []
        bundle = empty_bundle(
            bundle_ids[index],
            symbol_table.message_ids,
            chat_id=symbol_table.chat_id,
            status="complete",
            source="model_wire_v2_11_tsv",
            schema_version=CANONICAL_SCHEMA_VERSION,
        )
        speaker_id = symbol_table.authoritative_speaker_id
        speaker_evidence_ids: List[str] = []
        if speaker_id != UNKNOWN:
            speaker_message = next(
                item for item in symbol_table.messages
                if item.speaker_handle == symbol_table.authoritative_speaker_handle
            )
            speaker_evidence_id, speaker_row = _evidence_row(
                symbol_table,
                speaker_message.evidence_handle,
                field="speaker",
            )
            speaker_evidence_ids.append(speaker_evidence_id)
            evidence.append(speaker_row)
        bundle["speaker"] = {
            "id": speaker_id,
            "type": "person",
            "role": "speaker",
            "resolution": "explicit" if speaker_id != UNKNOWN else UNKNOWN,
            "evidence_ids": speaker_evidence_ids,
        }
        handles = claim["evidence"]
        bundle["subject"] = _entity(
            claim["subject"],
            role="subject",
            entity_type="person",
            table=symbol_table,
            evidence=evidence,
            evidence_handles=handles,
        )
        bundle["mentioned_person"] = [
            _entity(
                item,
                role="mentioned_person",
                entity_type="person",
                table=symbol_table,
                evidence=evidence,
                evidence_handles=handles,
            )
            for item in claim["mentioned_person"]
        ]
        bundle["target"] = [
            _entity(
                claim["target"],
                role="target",
                entity_type="target",
                table=symbol_table,
                evidence=evidence,
                evidence_handles=handles,
            )
        ] if claim["target"] != UNKNOWN else []
        bundle["object"] = [
            _entity(
                claim["object"],
                role="object",
                entity_type="object",
                table=symbol_table,
                evidence=evidence,
                evidence_handles=handles,
            )
        ] if claim["object"] != UNKNOWN else []
        action_ids = _add_evidence(
            symbol_table,
            handles,
            field="action",
            evidence=evidence,
        )
        bundle["action"] = [
            {
                "id": UNKNOWN,
                "label": claim["action"],
                "resolution": "explicit" if claim["action"] != UNKNOWN else UNKNOWN,
                "evidence_ids": action_ids,
            }
        ] if claim["action"] != UNKNOWN else []
        for field in ("claim_type", "state", "modality"):
            bundle[field] = claim[field]
            _add_evidence(
                symbol_table,
                handles,
                field=field,
                evidence=evidence,
            )
        bundle["coreference_candidates"] = []
        bundle["uncertainties"] = (
            []
            if claim["uncertainty"] == "none"
            else [
                {
                    "code": "tsv_uncertain",
                    "field": claim["uncertainty"],
                    "severity": "medium",
                }
            ]
        )
        bundle["context_relations"] = []
        for relation in relations:
            if relation["source"] != index:
                continue
            relation_evidence_ids = _add_evidence(
                symbol_table,
                relation["evidence"],
                field="context_relations",
                evidence=evidence,
            )
            bundle["context_relations"].append(
                {
                    "relation_id": "relation:%s" % stable_hash(
                        {
                            "symbol_table": symbol_table.symbol_table_sha256,
                            "source": relation["source"],
                            "target": relation["target"],
                            "label": relation["label"],
                        }
                    )[:24],
                    "source_bundle_id": bundle_ids[relation["source"]],
                    "target_bundle_id": bundle_ids[relation["target"]],
                    "label": relation["label"],
                    "strength": relation["strength"],
                    "evidence_ids": relation_evidence_ids,
                    "supporting_signals": list(relation["signals"]),
                    "explicit_reply_present": False,
                    "left_chat_id": symbol_table.chat_id,
                    "right_chat_id": symbol_table.chat_id,
                }
            )
        bundle["evidence"] = evidence
        bundle["metadata"].update(
            {
                "wire_schema_version": TSV_SCHEMA_VERSION,
                "wire_prompt_version": TSV_PROMPT_VERSION,
                "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                "symbol_table_sha256": symbol_table.symbol_table_sha256,
                "wire_payload_sha256": wire_payload_sha256 or stable_hash(frame),
                "scope": symbol_table.scope,
                "account_id": symbol_table.account_id,
                "claim_index": index,
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
            raise TsvWireParseError(
                "tsv_canonical_validation_failed",
                diagnostics={"validation_categories": list(report.errors)},
            )
        output.append(bundle)
    return output


class SemanticWireV211TsvBundleModel(SemanticFrameBundleModel):
    """OpenAI-compatible adapter that sends no response_format."""

    source = "openai-semantic-wire-v2-11-tsv"
    protocol_version = TSV_SCHEMA_VERSION

    def __init__(
        self,
        config: Optional[Any] = None,
        *,
        thinking_disabled: bool = True,
        max_input_chars: int = 1800,
        max_claims: int = 8,
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
            "wire_schema_version": TSV_SCHEMA_VERSION,
            "wire_prompt_version": TSV_PROMPT_VERSION,
            "ruleset_version": TSV_RULESET_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "symbol_table_sha256": table.symbol_table_sha256,
            "max_claims": int(request.get("max_claims") or self.max_claims),
        }

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        table = build_symbol_table(request)
        max_claims = int(request.get("max_claims") or self.max_claims)
        if max_claims < 1 or max_claims > self.max_claims:
            raise TsvWireParseError("tsv_max_claims_mismatch")
        payload, input_chars = _tsv_payload(
            table,
            max_claims=max_claims,
            max_chars=self.max_input_chars,
        )
        if input_chars > self.max_input_chars:
            raise BudgetExceeded("tsv_input_payload_limit_exceeded")
        payload_hash = stable_hash(payload)
        instructions = (
            "Return only fixed TSV rows, no prose, no markdown, no blank lines. Each CLAIM row "
            "has exactly 11 tab-separated columns: CLAIM,subject_handle_or_unknown,"
            "mentioned_csv_or_empty,target_handle_or_unknown,object_handle_or_unknown,"
            "action_enum_or_unknown,claim_type,state,modality,evidence_handle_csv,"
            "uncertainty_enum. Emit zero or more REL rows only after CLAIM rows; each REL row "
            "has exactly 7 columns: REL,source_claim_index,target_claim_index,label,strength,"
            "signal_csv,evidence_handle_csv. Emit at least one CLAIM and at most "
            + str(max_claims)
            + " CLAIM rows. Allowed semantic handles are only supplied pN speaker handles "
            "or unknown; evidence is only supplied eN. Do not emit message IDs, chat IDs, "
            "scope, spans, names, aliases, or free text. Action enum is "
            + ",".join(sorted(ACTION_ENUM))
            + ". Use unknown and empty fields when not grounded. All known fields require "
            "one supplied evidence handle. Relation labels, strengths, signals, and "
            "uncertainty are closed enums. Time-only evidence cannot justify strong. "
            "Exemplar only, do not emit it: "
            + tsv_frame_exemplar()
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
            frame = parse_tsv_frame(
                text,
                symbol_table=table,
                max_claims=max_claims,
            )
            bundles = assemble_tsv_frame(
                frame,
                request,
                symbol_table=table,
                wire_payload_sha256=payload_hash,
            )
        except TsvWireParseError as exc:
            metadata = self._safe_response_metadata(response, response_text=text)
            metadata.update(exc.diagnostics)
            metadata["wire_schema_version"] = TSV_SCHEMA_VERSION
            metadata["wire_prompt_version"] = TSV_PROMPT_VERSION
            metadata["canonical_schema_version"] = CANONICAL_SCHEMA_VERSION
            metadata["symbol_table_sha256"] = table.symbol_table_sha256
            metadata["wire_payload_sha256"] = payload_hash
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
                "wire_schema_version": TSV_SCHEMA_VERSION,
                "wire_prompt_version": TSV_PROMPT_VERSION,
                "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                "symbol_table_sha256": table.symbol_table_sha256,
                "wire_payload_sha256": payload_hash,
                "claim_count": len(bundles),
            },
        }


SemanticWireV2_11TsvBundleModel = SemanticWireV211TsvBundleModel
TsvMultiClaimModel = SemanticWireV211TsvBundleModel


__all__ = [
    "TSV_SCHEMA_VERSION",
    "TSV_PROMPT_VERSION",
    "TSV_RULESET_VERSION",
    "CANONICAL_SCHEMA_VERSION",
    "CLAIM_COLUMN_COUNT",
    "RELATION_COLUMN_COUNT",
    "ACTION_ENUM",
    "UNCERTAINTY_ENUM",
    "RELATION_SIGNAL_ENUM",
    "TsvWireParseError",
    "parse_tsv_frame",
    "assemble_tsv_frame",
    "tsv_frame_exemplar",
    "SemanticWireV211TsvBundleModel",
    "SemanticWireV2_11TsvBundleModel",
    "TsvMultiClaimModel",
]
