"""Three-stage, evidence-bound semantic analysis for a ContextPacket v1.

This module is intentionally independent from the event, title, and frontend
surfaces.  It is a protocol boundary for a later K2 adapter: callers provide
an in-memory :class:`ContextPacket` and an injected stage model.  The model is
asked for three small outputs rather than one canonical 17-field object:

* Stage A groups messages into topics.
* Stage B extracts claims one topic at a time.
* Stage C audits the resulting claims without inventing new claims.

All model output is validated locally against the packet's message, entity,
and evidence scopes.  A failed stage becomes ``pending``; complete earlier
stages are retained and failed results are never cached.  The default cache is
process-local and is split by stage, so it is not a persistent transcript or
provider-result store.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Protocol, Sequence, Set, Tuple, Union

from .bundle_semantics import CLAIM_TYPES, MODALITIES, STATES, UNKNOWN


CONTEXT_PACKET_SCHEMA_VERSION = "context_packet_v1"
ANALYZER_SCHEMA_VERSION = "staged_deepseek_analyzer_v1"
STAGE_A_SCHEMA_VERSION = "stage_a_topics_v1"
STAGE_B_SCHEMA_VERSION = "stage_b_claims_v1"
STAGE_C_SCHEMA_VERSION = "stage_c_audit_v1"
STAGE_PROMPT_VERSION = "staged_deepseek_prompt_v1"
STAGES = ("A", "B", "C")
STAGE_STATUSES = frozenset({"complete", "pending"})

# These are intentionally finite wire labels.  The canonical bundle is not
# emitted by any provider-facing stage in this module.
ACTIONS = frozenset(
    {
        "assert",
        "inform",
        "ask",
        "request",
        "suggest",
        "plan",
        "resolve",
        "cancel",
        "fail",
        "mention",
        UNKNOWN,
    }
)
TOPIC_RELATIONS = frozenset(
    {
        "same_topic",
        "continuation",
        "answer",
        "question_followup",
        "request_followup",
        "contrast",
        "new_topic",
        "unrelated",
        UNKNOWN,
    }
)
CONFLICT_REASONS = frozenset(
    {"state_conflict", "entity_conflict", "evidence_conflict", "scope_conflict", UNKNOWN}
)
_FROZEN_SCOPES = frozenset({"frozen", "frozen_test", "frozen-test"})

CONTEXT_VALIDATION_TAXONOMY_VERSION = "stage_a_context_validation_taxonomy_v1"
CONTEXT_ERROR_TELEMETRY_KEYS = (
    "duplicate_within_topic",
    "duplicate_across_topics",
    "primary_as_context",
    "candidate_or_unknown_alias",
    "primary_context_overlap",
    "invalid_type",
)

STAGE_A_KEYS = frozenset({"topics"})
TOPIC_KEYS = frozenset(
    {"topic_id", "primary_message_ids", "context_message_ids", "relation", "uncertainties", "evidence_ids"}
)
STAGE_B_KEYS = frozenset({"topic_id", "claims"})
CLAIM_KEYS = frozenset(
    {
        "speaker",
        "subject",
        "mentioned",
        "target",
        "object",
        "action",
        "claim_type",
        "state",
        "modality",
        "evidence_ids",
        "uncertainties",
    }
)
STAGE_C_KEYS = frozenset(
    {"accepted_claim_ids", "conflicts", "missing_context", "overmerge", "undermerge", "needs_more_context"}
)
CONFLICT_KEYS = frozenset({"claim_ids", "reason", "evidence_ids"})
OVERMERGE_KEYS = frozenset({"topic_ids", "claim_ids"})
UNDERMERGE_KEYS = frozenset({"topic_ids", "message_ids"})


class StageProtocolError(ValueError):
    """Body-free protocol/schema error with a stable error code."""

    def __init__(self, code: str, *, context_telemetry: Optional[Mapping[str, Any]] = None) -> None:
        self.code = str(code)
        self.context_telemetry = _body_free_context_telemetry(context_telemetry)
        self.context_error_counts = dict(self.context_telemetry.get("error_counts", {}))
        self.context_error_flags = dict(self.context_telemetry.get("error_flags", {}))
        super().__init__(self.code)


class StageProviderError(RuntimeError):
    """Body-free provider/adapter error."""

    def __init__(
        self,
        code: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        finish_reason: str = "not_run",
        content_length: int = 0,
        output_sha256: str = "",
        reasoning_length: int = 0,
        context_telemetry: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.code = str(code)
        # Keep only scalar telemetry on an exception.  In particular, never
        # retain provider text or reasoning in the error object that crosses
        # the runner boundary.
        self.input_tokens = max(0, int(input_tokens or 0))
        self.output_tokens = max(0, int(output_tokens or 0))
        self.latency_ms = max(0.0, float(latency_ms or 0.0))
        self.finish_reason = str(finish_reason or "not_run")[:80]
        self.content_length = max(0, int(content_length or 0))
        self.output_sha256 = str(output_sha256 or "")[:128]
        self.reasoning_length = max(0, int(reasoning_length or 0))
        self.context_telemetry = _body_free_context_telemetry(context_telemetry)
        self.context_error_counts = dict(self.context_telemetry.get("error_counts", {}))
        self.context_error_flags = dict(self.context_telemetry.get("error_flags", {}))
        super().__init__(self.code)


def _empty_context_telemetry() -> Dict[str, Any]:
    counts = {name: 0 for name in CONTEXT_ERROR_TELEMETRY_KEYS}
    return {
        "telemetry_version": CONTEXT_VALIDATION_TAXONOMY_VERSION,
        "error_counts": counts,
        "error_flags": {name: False for name in CONTEXT_ERROR_TELEMETRY_KEYS},
        "has_error": False,
        "body_free": True,
    }


def _body_free_context_telemetry(value: Any) -> Dict[str, Any]:
    result = _empty_context_telemetry()
    if not isinstance(value, Mapping):
        return result
    source = value.get("error_counts", value.get("counts", value))
    if isinstance(source, Mapping):
        for name in CONTEXT_ERROR_TELEMETRY_KEYS:
            try:
                result["error_counts"][name] = max(0, int(source.get(name, 0) or 0))
            except (TypeError, ValueError, OverflowError):
                result["error_counts"][name] = 0
    result["error_flags"] = {
        name: bool(result["error_counts"][name]) for name in CONTEXT_ERROR_TELEMETRY_KEYS
    }
    result["has_error"] = any(result["error_flags"].values())
    result["counts"] = dict(result["error_counts"])
    result["flags"] = dict(result["error_flags"])
    return result


def context_validation_telemetry(value: Any, packet: Any = None) -> Dict[str, Any]:
    """Classify Stage-A context failures without retaining output payloads."""

    result = _empty_context_telemetry()
    counts = result["error_counts"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            counts["invalid_type"] += 1
            result["error_flags"]["invalid_type"] = True
            result["has_error"] = True
            result["counts"] = dict(counts)
            result["flags"] = dict(result["error_flags"])
            return result
    if not isinstance(value, Mapping):
        counts["invalid_type"] += 1
        result["error_flags"]["invalid_type"] = True
        result["has_error"] = True
        result["counts"] = dict(counts)
        result["flags"] = dict(result["error_flags"])
        return result
    primary_ids: set[str] = set()
    context_ids: set[str] = set()
    if isinstance(packet, ContextPacket):
        primary_ids = {str(item) for item in packet.message_ids}
        context_ids = {str(item) for item in packet.context_message_ids}
    elif isinstance(packet, Mapping):
        raw_primary = packet.get("message_ids", packet.get("primary_message_ids", ()))
        raw_context = packet.get("context_message_ids", ())
        if isinstance(raw_primary, (list, tuple, set, frozenset)):
            primary_ids = {str(item) for item in raw_primary}
        if isinstance(raw_context, (list, tuple, set, frozenset)):
            context_ids = {str(item) for item in raw_context}
    topics = value.get("topics")
    if type(topics) is not list:
        counts["invalid_type"] += 1
        topics = ()
    owners: Dict[str, int] = {}
    for topic_index, topic in enumerate(topics):
        if not isinstance(topic, Mapping):
            counts["invalid_type"] += 1
            continue
        primary = topic.get("primary_message_ids")
        context = topic.get("context_message_ids")
        if type(primary) is not list:
            counts["invalid_type"] += 1
            primary = ()
        if type(context) is not list:
            counts["invalid_type"] += 1
            context = ()
        primary_strings = {item for item in primary if type(item) is str}
        context_strings = {item for item in context if type(item) is str}
        counts["primary_context_overlap"] += len(primary_strings.intersection(context_strings))
        local_seen: set[str] = set()
        for item in context:
            if type(item) is not str:
                counts["invalid_type"] += 1
                continue
            if item in local_seen:
                counts["duplicate_within_topic"] += 1
            local_seen.add(item)
            if item in owners and owners[item] != topic_index:
                counts["duplicate_across_topics"] += 1
            else:
                owners.setdefault(item, topic_index)
            if item in primary_ids:
                counts["primary_as_context"] += 1
            elif item not in context_ids:
                counts["candidate_or_unknown_alias"] += 1
    result["error_flags"] = {name: bool(counts[name]) for name in CONTEXT_ERROR_TELEMETRY_KEYS}
    result["has_error"] = any(result["error_flags"].values())
    result["counts"] = dict(counts)
    result["flags"] = dict(result["error_flags"])
    return result


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


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(value: Any, expected: Iterable[str], code: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise StageProtocolError(code)
    if set(value) != set(expected):
        raise StageProtocolError(code)
    return value


def _safe_string(value: Any, code: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise StageProtocolError(code)
    if not allow_empty and not value:
        raise StageProtocolError(code)
    if value != value.strip() or len(value) > 240 or any(ord(char) < 32 for char in value):
        raise StageProtocolError(code)
    return value


def _safe_id(value: Any, code: str = "id_invalid", *, allow_unknown: bool = False) -> str:
    text = _safe_string(value, code)
    if text == UNKNOWN and allow_unknown:
        return text
    if text in _FROZEN_SCOPES:
        raise StageProtocolError("frozen_scope_forbidden")
    return text


def _string_list(value: Any, code: str, *, unique: bool = False, allow_empty: bool = True) -> List[str]:
    if type(value) is not list:
        raise StageProtocolError(code)
    if not allow_empty and not value:
        raise StageProtocolError(code)
    output: List[str] = []
    for item in value:
        output.append(_safe_string(item, code))
    if unique and len(output) != len(set(output)):
        raise StageProtocolError(code + "_duplicate")
    return output


def _enum(value: Any, allowed: Iterable[str], code: str) -> str:
    text = _safe_string(value, code)
    if text not in set(allowed):
        raise StageProtocolError(code)
    return text


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": bool(self.ok), "errors": list(self.errors)}


@dataclass(frozen=True)
class ContextPacket:
    """The minimal K2-to-K3 in-memory input contract.

    ``message_ids`` are the primary messages for this packet and
    ``context_message_ids`` are optional surrounding messages.  The optional
    ``messages`` and ``evidence`` mappings are passed to an injected model as
    dynamic input, but are never copied into the ledger or cache records.
    """

    packet_id: str
    scope: str
    message_ids: Tuple[str, ...]
    context_message_ids: Tuple[str, ...] = ()
    evidence_ids: Tuple[str, ...] = ()
    entity_ids: Tuple[str, ...] = ()
    messages: Tuple[Mapping[str, Any], ...] = ()
    evidence: Tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = CONTEXT_PACKET_SCHEMA_VERSION

    @property
    def all_message_ids(self) -> Tuple[str, ...]:
        return tuple(self.message_ids) + tuple(self.context_message_ids)

    @property
    def packet_sha256(self) -> str:
        return stable_hash(self.to_model_packet())

    def to_model_packet(self) -> Dict[str, Any]:
        """Return a bounded contract mapping for the dynamic user packet."""

        return {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "scope": self.scope,
            "message_ids": list(self.message_ids),
            "context_message_ids": list(self.context_message_ids),
            "evidence_ids": list(self.evidence_ids),
            "entity_ids": list(self.entity_ids),
            "messages": [_jsonable(item) for item in self.messages],
            "evidence": [_jsonable(item) for item in self.evidence],
            "metadata": _jsonable(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextPacket":
        if not isinstance(value, Mapping):
            raise StageProtocolError("context_packet_not_object")
        allowed = {
            "schema_version",
            "packet_id",
            "scope",
            "message_ids",
            "context_message_ids",
            "evidence_ids",
            "entity_ids",
            "messages",
            "evidence",
            "metadata",
        }
        if set(value) - allowed:
            raise StageProtocolError("context_packet_extra_field")

        def tuple_field(name: str) -> Tuple[Any, ...]:
            field_value = value.get(name)
            if field_value is None:
                return ()
            if type(field_value) not in (list, tuple):
                raise StageProtocolError("context_packet_%s_type" % name)
            return tuple(field_value)

        packet = cls(
            packet_id=value.get("packet_id", ""),
            scope=value.get("scope", ""),
            message_ids=tuple_field("message_ids"),
            context_message_ids=tuple_field("context_message_ids"),
            evidence_ids=tuple_field("evidence_ids"),
            entity_ids=tuple_field("entity_ids"),
            messages=tuple_field("messages"),
            evidence=tuple_field("evidence"),
            metadata=value.get("metadata") or {},
            schema_version=value.get("schema_version", CONTEXT_PACKET_SCHEMA_VERSION),
        )
        validation = validate_context_packet(packet)
        if not validation.ok:
            raise StageProtocolError(validation.errors[0])
        return packet


def validate_context_packet(packet: Union[ContextPacket, Mapping[str, Any]]) -> ValidationResult:
    """Validate packet identity, scope, and in-memory reference collections."""

    errors: List[str] = []
    if isinstance(packet, Mapping):
        try:
            packet = ContextPacket.from_mapping(packet)
        except StageProtocolError as exc:
            return ValidationResult(False, (exc.code,))
    if not isinstance(packet, ContextPacket):
        return ValidationResult(False, ("context_packet_type",))
    if packet.schema_version != CONTEXT_PACKET_SCHEMA_VERSION:
        errors.append("context_packet_schema_version")
    for value, code in ((packet.packet_id, "packet_id"), (packet.scope, "scope")):
        try:
            _safe_id(value, code)
        except StageProtocolError as exc:
            errors.append(exc.code)
    if str(packet.scope).casefold() in _FROZEN_SCOPES:
        errors.append("frozen_scope_forbidden")
    collections_to_check = (
        (packet.message_ids, "message_ids"),
        (packet.context_message_ids, "context_message_ids"),
        (packet.evidence_ids, "evidence_ids"),
        (packet.entity_ids, "entity_ids"),
    )
    for values, code in collections_to_check:
        try:
            _string_list(list(values), code, unique=True, allow_empty=(code != "message_ids"))
        except StageProtocolError as exc:
            errors.append(exc.code)
    if set(packet.message_ids) & set(packet.context_message_ids):
        errors.append("message_context_overlap")
    if not isinstance(packet.messages, (tuple, list)) or any(not isinstance(item, Mapping) for item in packet.messages):
        errors.append("messages_shape")
    if not isinstance(packet.evidence, (tuple, list)) or any(not isinstance(item, Mapping) for item in packet.evidence):
        errors.append("evidence_shape")
    if not isinstance(packet.metadata, Mapping):
        errors.append("metadata_shape")
    return ValidationResult(not errors, tuple(sorted(set(errors))))


def validate_stage_a(value: Any, packet: ContextPacket) -> ValidationResult:
    errors: List[str] = []
    try:
        root = _exact_keys(value, STAGE_A_KEYS, "stage_a_shape")
        topics = root["topics"]
        if type(topics) is not list:
            raise StageProtocolError("stage_a_topics_type")
        seen_topics: Set[str] = set()
        allowed_messages = set(packet.all_message_ids)
        allowed_evidence = set(packet.evidence_ids)
        for topic in topics:
            row = _exact_keys(topic, TOPIC_KEYS, "stage_a_topic_shape")
            topic_id = _safe_id(row["topic_id"], "stage_a_topic_id")
            if topic_id in seen_topics:
                raise StageProtocolError("stage_a_duplicate_topic")
            seen_topics.add(topic_id)
            primary = _string_list(row["primary_message_ids"], "stage_a_primary_message_ids", unique=True, allow_empty=False)
            context = _string_list(row["context_message_ids"], "stage_a_context_message_ids", unique=True)
            if not set(primary) <= allowed_messages:
                raise StageProtocolError("stage_a_primary_out_of_scope")
            if not set(context) <= allowed_messages:
                raise StageProtocolError("stage_a_context_out_of_scope")
            if set(primary) & set(context):
                raise StageProtocolError("stage_a_primary_context_overlap")
            _enum(row["relation"], TOPIC_RELATIONS, "stage_a_relation")
            _string_list(row["uncertainties"], "stage_a_uncertainties", unique=True)
            evidence = _string_list(row["evidence_ids"], "stage_a_evidence_ids", unique=True)
            if not set(evidence) <= allowed_evidence:
                raise StageProtocolError("stage_a_evidence_out_of_scope")
    except StageProtocolError as exc:
        errors.append(exc.code)
    return ValidationResult(not errors, tuple(sorted(set(errors))))


def validate_stage_b(value: Any, packet: ContextPacket, *, expected_topic_id: Optional[str] = None) -> ValidationResult:
    errors: List[str] = []
    try:
        root = _exact_keys(value, STAGE_B_KEYS, "stage_b_shape")
        topic_id = _safe_id(root["topic_id"], "stage_b_topic_id")
        if expected_topic_id is not None and topic_id != expected_topic_id:
            raise StageProtocolError("stage_b_topic_mismatch")
        claims = root["claims"]
        if type(claims) is not list:
            raise StageProtocolError("stage_b_claims_type")
        allowed_entities = set(packet.entity_ids)
        allowed_evidence = set(packet.evidence_ids)
        for claim in claims:
            row = _exact_keys(claim, CLAIM_KEYS, "stage_b_claim_shape")
            for key in ("speaker", "subject", "mentioned", "target", "object"):
                entity = _safe_id(row[key], "stage_b_%s_invalid" % key, allow_unknown=True)
                if entity != UNKNOWN and entity not in allowed_entities:
                    raise StageProtocolError("stage_b_%s_out_of_scope" % key)
            _enum(row["action"], ACTIONS, "stage_b_action")
            _enum(row["claim_type"], CLAIM_TYPES, "stage_b_claim_type")
            _enum(row["state"], STATES, "stage_b_state")
            _enum(row["modality"], MODALITIES, "stage_b_modality")
            evidence = _string_list(row["evidence_ids"], "stage_b_evidence_ids", unique=True)
            if not set(evidence) <= allowed_evidence:
                raise StageProtocolError("stage_b_evidence_out_of_scope")
            known_slot = any(
                row[key] != UNKNOWN
                for key in ("speaker", "subject", "mentioned", "target", "object", "action", "claim_type", "state", "modality")
            )
            if known_slot and not evidence:
                raise StageProtocolError("stage_b_known_claim_missing_evidence")
            _string_list(row["uncertainties"], "stage_b_uncertainties", unique=True)
    except StageProtocolError as exc:
        errors.append(exc.code)
    return ValidationResult(not errors, tuple(sorted(set(errors))))


def validate_stage_c(
    value: Any,
    packet: ContextPacket,
    *,
    known_claim_ids: Iterable[str],
    known_topic_ids: Iterable[str],
) -> ValidationResult:
    errors: List[str] = []
    try:
        root = _exact_keys(value, STAGE_C_KEYS, "stage_c_shape")
        claims = set(known_claim_ids)
        topics = set(known_topic_ids)
        messages = set(packet.all_message_ids)
        evidence_ids = set(packet.evidence_ids)
        accepted = _string_list(root["accepted_claim_ids"], "stage_c_accepted_claim_ids", unique=True)
        if not set(accepted) <= claims:
            raise StageProtocolError("stage_c_accepted_claim_out_of_scope")
        conflicts = root["conflicts"]
        if type(conflicts) is not list:
            raise StageProtocolError("stage_c_conflicts_type")
        for conflict in conflicts:
            row = _exact_keys(conflict, CONFLICT_KEYS, "stage_c_conflict_shape")
            conflict_claims = _string_list(row["claim_ids"], "stage_c_conflict_claim_ids", unique=True, allow_empty=False)
            if len(conflict_claims) < 2 or not set(conflict_claims) <= claims:
                raise StageProtocolError("stage_c_conflict_claim_scope")
            _enum(row["reason"], CONFLICT_REASONS, "stage_c_conflict_reason")
            conflict_evidence = _string_list(row["evidence_ids"], "stage_c_conflict_evidence_ids", unique=True)
            if not set(conflict_evidence) <= evidence_ids:
                raise StageProtocolError("stage_c_conflict_evidence_scope")
            if row["reason"] != UNKNOWN and not conflict_evidence:
                raise StageProtocolError("stage_c_conflict_missing_evidence")
        missing = _string_list(root["missing_context"], "stage_c_missing_context", unique=True)
        needs = _string_list(root["needs_more_context"], "stage_c_needs_more_context", unique=True)
        if not set(missing) <= messages or not set(needs) <= messages:
            raise StageProtocolError("stage_c_message_scope")
        overmerge = root["overmerge"]
        if type(overmerge) is not list:
            raise StageProtocolError("stage_c_overmerge_type")
        for item in overmerge:
            row = _exact_keys(item, OVERMERGE_KEYS, "stage_c_overmerge_shape")
            topic_values = _string_list(row["topic_ids"], "stage_c_overmerge_topics", unique=True, allow_empty=False)
            claim_values = _string_list(row["claim_ids"], "stage_c_overmerge_claims", unique=True)
            if len(topic_values) < 2 or not set(topic_values) <= topics or not set(claim_values) <= claims:
                raise StageProtocolError("stage_c_overmerge_scope")
        undermerge = root["undermerge"]
        if type(undermerge) is not list:
            raise StageProtocolError("stage_c_undermerge_type")
        for item in undermerge:
            row = _exact_keys(item, UNDERMERGE_KEYS, "stage_c_undermerge_shape")
            topic_values = _string_list(row["topic_ids"], "stage_c_undermerge_topics", unique=True, allow_empty=False)
            message_values = _string_list(row["message_ids"], "stage_c_undermerge_messages", unique=True, allow_empty=False)
            if not set(topic_values) <= topics or not set(message_values) <= messages:
                raise StageProtocolError("stage_c_undermerge_scope")
    except StageProtocolError as exc:
        errors.append(exc.code)
    return ValidationResult(not errors, tuple(sorted(set(errors))))


@dataclass(frozen=True)
class StageModelResponse:
    """Provider/fake response reduced to parsed payload plus safe telemetry."""

    payload: Any
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""
    request_id: str = ""
    # Keep the provider termination signal and hidden-reasoning size available
    # to one-shot callers.  They are metadata only; response text is still
    # never copied to a ledger or artifact.
    finish_reason: str = "stop"
    reasoning_length: int = 0


class StageModel(Protocol):
    model_id: str
    source: str

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> Union[StageModelResponse, Mapping[str, Any]]:
        ...


def _strict_json(text: str) -> Any:
    if type(text) is not str:
        raise StageProviderError("provider_response_not_text")

    def reject_constant(_value: str) -> Any:
        raise StageProviderError("provider_nonstandard_json", context_telemetry={"invalid_type": 1})

    def reject_duplicate(pairs: List[Tuple[Any, Any]]) -> Dict[Any, Any]:
        result: Dict[Any, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StageProviderError("provider_duplicate_json_key", context_telemetry={"invalid_type": 1})
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicate)
    except StageProviderError:
        raise
    except (TypeError, ValueError) as exc:
        raise StageProviderError(
            "provider_invalid_json", context_telemetry={"invalid_type": 1}
        ) from exc


def _output_metadata(value: Any) -> Tuple[int, str]:
    """Return body-free length/hash metadata for an in-memory output value."""

    if not isinstance(value, str):
        return 0, ""
    encoded = value.encode("utf-8")
    return len(value), hashlib.sha256(encoded).hexdigest()


class OpenAICompatibleStageModel:
    """Lazy OpenAI-compatible adapter for one small stage request.

    The adapter is inert until ``complete`` is called.  Tests can inject a
    client; production callers must explicitly provide a key or client.  The
    response body is parsed in memory and is not copied into any ledger.
    """

    source = "openai-compatible"

    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Any = None,
        timeout_seconds: Optional[float] = None,
        response_format_json: bool = False,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.model_id = str(model)
        self.api_key = api_key
        self.base_url = base_url
        self._client = client
        self.timeout_seconds = timeout_seconds
        self.response_format_json = bool(response_format_json)
        self._clock = clock or time.perf_counter

    @property
    def configured(self) -> bool:
        return self._client is not None or bool(self.api_key)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise StageProviderError("provider_unconfigured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise StageProviderError("provider_sdk_unavailable") from exc
        # Every call made through this adapter is bounded by the caller.  The
        # OpenAI SDK otherwise supplies its own retry policy, which would
        # violate the compact Stage-A one-attempt contract.
        kwargs: Dict[str, Any] = {"api_key": self.api_key, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.timeout_seconds is not None:
            kwargs["timeout"] = self.timeout_seconds
        try:
            self._client = OpenAI(**kwargs)
        except TypeError as exc:
            # Do not silently fall back to a retrying client when the SDK
            # cannot honor the bounded-call setting.
            raise StageProviderError("provider_sdk_unavailable") from exc
        return self._client

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Optional[Mapping[str, Any]] = None,
    ) -> StageModelResponse:
        client = self._get_client()
        started = self._clock()
        request: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_json(user_packet)},
            ],
            "max_tokens": int(max_output_tokens),
            "temperature": 0,
        }
        if self.response_format_json:
            request["response_format"] = {"type": "json_object"}
        # The compact Stage-A runner supplies the explicit provider extension
        # at its injection boundary.  Keeping this optional preserves the
        # OpenAI-compatible request shape for callers that do not need it.
        if extra_body:
            request["extra_body"] = dict(extra_body)
        try:
            response = client.chat.completions.create(**request)
        except Exception as exc:
            # Deliberately expose only a stable class-level code.
            raise StageProviderError("provider_request_failed") from exc
        elapsed = max(0.0, (self._clock() - started) * 1000.0)
        choices = response.get("choices") if isinstance(response, Mapping) else getattr(response, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            raise StageProviderError("provider_response_shape", latency_ms=elapsed)
        choice = choices[0]
        if isinstance(choice, Mapping):
            message = choice.get("message")
            finish = choice.get("finish_reason", "stop")
        else:
            message = getattr(choice, "message", None)
            finish = getattr(choice, "finish_reason", "stop")
        content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
        reasoning = message.get("reasoning_content", "") if isinstance(message, Mapping) else getattr(message, "reasoning_content", "")
        if not isinstance(reasoning, str):
            reasoning = ""
        finish_reason = str(finish or "stop")[:80]
        usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
        if isinstance(usage, Mapping):
            input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
        else:
            input_tokens = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0))
            output_tokens = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0))
        try:
            input_tokens = max(0, int(input_tokens or 0))
            output_tokens = max(0, int(output_tokens or 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise StageProviderError("provider_response_shape", latency_ms=elapsed) from exc
        content_length, output_sha256 = _output_metadata(content)
        if not isinstance(content, str):
            raise StageProviderError(
                "provider_response_shape",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=elapsed,
                finish_reason=finish_reason,
                content_length=content_length,
                output_sha256=output_sha256,
                reasoning_length=len(reasoning),
            )
        try:
            payload = _strict_json(content)
        except StageProviderError as exc:
            # Usage and output-shape metadata must survive a parse failure so
            # the body-free development ledger can distinguish an empty/invalid
            # output from an adapter/request failure without storing the text.
            raise StageProviderError(
                exc.code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=elapsed,
                finish_reason=finish_reason,
                content_length=content_length,
                output_sha256=output_sha256,
                reasoning_length=len(reasoning),
                context_telemetry=getattr(exc, "context_telemetry", None),
            ) from exc
        except Exception as exc:
            raise StageProviderError(
                "provider_response_shape",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=elapsed,
                finish_reason=finish_reason,
                content_length=content_length,
                output_sha256=output_sha256,
                reasoning_length=len(reasoning),
            ) from exc
        request_id = str(getattr(response, "id", "") or "")
        if isinstance(response, Mapping):
            request_id = str(response.get("id", "") or "")
        return StageModelResponse(
            payload=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed,
            model=self.model_id,
            request_id=request_id,
            finish_reason=finish_reason,
            reasoning_length=len(reasoning),
        )


class FakeStageModel:
    """Deterministic injectable fake for synthetic tests and local callers."""

    source = "fake"

    def __init__(self, responses: Mapping[str, Any], *, model_id: str = "fake-stage-model") -> None:
        self.model_id = model_id
        self.responses: Dict[str, Any] = dict(responses)
        self.calls: List[Dict[str, Any]] = []

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> Union[StageModelResponse, Mapping[str, Any]]:
        topic_id = str(user_packet.get("topic_id") or "")
        key = "%s:%s" % (stage, topic_id) if stage == "B" else stage
        self.calls.append(
            {
                "stage": stage,
                "topic_id": topic_id,
                "system_prefix_sha256": stable_hash(system_prompt),
                "user_packet_sha256": stable_hash(user_packet),
                "max_output_tokens": int(max_output_tokens),
            }
        )
        if key not in self.responses and stage not in self.responses:
            raise StageProviderError("fake_response_missing")
        value = self.responses.get(key, self.responses.get(stage))
        if isinstance(value, list):
            if not value:
                raise StageProviderError("fake_response_exhausted")
            value = value.pop(0)
        if callable(value):
            value = value(stage, user_packet)
        return value


@dataclass(frozen=True)
class StageLedgerRecord:
    stage: str
    topic_id: str
    status: str
    cache_hit: bool
    provider_call: bool
    attempt: int
    request_sha256: str
    system_prefix_sha256: str
    user_packet_sha256: str
    source: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error_code: Optional[str] = None
    context_telemetry: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        context_telemetry = _body_free_context_telemetry(self.context_telemetry)
        return {
            "stage": self.stage,
            "topic_id": self.topic_id,
            "status": self.status,
            "cache_hit": bool(self.cache_hit),
            "provider_call": bool(self.provider_call),
            "attempt": int(self.attempt),
            "request_sha256": self.request_sha256,
            "system_prefix_sha256": self.system_prefix_sha256,
            "user_packet_sha256": self.user_packet_sha256,
            "source": self.source,
            "model": self.model,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "latency_ms": float(self.latency_ms),
            "error_code": self.error_code,
            "context_telemetry": context_telemetry,
            "context_error_counts": dict(context_telemetry.get("error_counts", {})),
            "context_error_flags": dict(context_telemetry.get("error_flags", {})),
        }


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    payload: Optional[Mapping[str, Any]] = None
    errors: Tuple[str, ...] = ()
    cache_key: str = ""
    context_telemetry: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        context_telemetry = _body_free_context_telemetry(self.context_telemetry)
        result: Dict[str, Any] = {
            "stage": self.stage,
            "status": self.status,
            "errors": list(self.errors),
            "cache_key": self.cache_key,
            "context_telemetry": context_telemetry,
            "context_error_counts": dict(context_telemetry.get("error_counts", {})),
            "context_error_flags": dict(context_telemetry.get("error_flags", {})),
        }
        if self.payload is not None:
            result["payload"] = deepcopy(dict(self.payload))
        return result


@dataclass(frozen=True)
class StagedAnalysisResult:
    packet_id: str
    packet_sha256: str
    status: str
    stage_a: StageResult
    stage_b: Mapping[str, StageResult]
    stage_c: StageResult
    claim_index: Mapping[str, Mapping[str, Any]]
    ledger: Tuple[StageLedgerRecord, ...]

    @property
    def pending_stages(self) -> Tuple[str, ...]:
        pending: List[str] = []
        if self.stage_a.status != "complete":
            pending.append("A")
        if any(result.status != "complete" for result in self.stage_b.values()):
            pending.append("B")
        if self.stage_c.status != "complete":
            pending.append("C")
        return tuple(pending)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": ANALYZER_SCHEMA_VERSION,
            "packet_id": self.packet_id,
            "packet_sha256": self.packet_sha256,
            "status": self.status,
            "pending_stages": list(self.pending_stages),
            "stage_a": self.stage_a.to_dict(),
            "stage_b": {key: value.to_dict() for key, value in self.stage_b.items()},
            "stage_c": self.stage_c.to_dict(),
            "claim_index": {key: dict(value) for key, value in self.claim_index.items()},
            "ledger": [record.to_dict() for record in self.ledger],
        }


@dataclass(frozen=True)
class _CacheEntry:
    payload: Mapping[str, Any]


class StageCache:
    """Process-local cache with separate namespaces for A, B, and C."""

    def __init__(self) -> None:
        self._entries: Dict[str, Dict[str, _CacheEntry]] = {stage: {} for stage in STAGES}

    def get(self, stage: str, key: str) -> Optional[Mapping[str, Any]]:
        entry = self._entries.setdefault(stage, {}).get(key)
        return deepcopy(dict(entry.payload)) if entry is not None else None

    def put(self, stage: str, key: str, payload: Mapping[str, Any]) -> None:
        if stage not in STAGES:
            raise ValueError("unknown stage")
        self._entries[stage][key] = _CacheEntry(deepcopy(dict(payload)))

    def sizes(self) -> Dict[str, int]:
        return {stage: len(self._entries[stage]) for stage in STAGES}

    @property
    def persistent(self) -> bool:
        return False


STAGE_SYSTEM_PROMPTS: Mapping[str, str] = {
    "A": (
        "Stage A only groups the supplied message handles into topics. "
        "Return exactly {topics:[{topic_id,primary_message_ids,context_message_ids,relation,uncertainties,evidence_ids}]} "
        "as JSON; never include prose or message text."
    ),
    "B": (
        "Stage B only extracts claims for the supplied topic. Keep speaker, subject, and mentioned distinct. "
        "Return exactly {topic_id,claims}; each claim has the eleven fixed slots and evidence_ids/uncertainties. "
        "Use unknown when unsupported and never invent evidence."
    ),
    "C": (
        "Stage C is audit-only. It may accept or flag the local claim handles, missing context, merge risks, and conflicts. "
        "Return exactly the six fixed audit fields as JSON; do not create claims or free-text explanations."
    ),
}


def _stage_prompt_version(stage: str) -> str:
    return "%s:%s" % (STAGE_PROMPT_VERSION, stage)


def _coerce_response(value: Any, model: StageModel) -> StageModelResponse:
    if isinstance(value, StageModelResponse):
        return value
    if isinstance(value, Mapping):
        return StageModelResponse(payload=value, model=str(getattr(model, "model_id", "")))
    raise StageProviderError("provider_response_object")


class StagedDeepseekAnalyzer:
    """Run Stage A, per-topic Stage B, and Stage C with fail-closed gates."""

    def __init__(
        self,
        model: StageModel,
        *,
        cache: Optional[StageCache] = None,
        max_output_tokens: int = 400,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not hasattr(model, "complete") or not callable(model.complete):
            raise TypeError("model must provide complete")
        if int(max_output_tokens) < 1:
            raise ValueError("max_output_tokens must be positive")
        self.model = model
        self.cache = cache or StageCache()
        self.max_output_tokens = int(max_output_tokens)
        self._clock = clock or time.perf_counter
        self._attempts: Dict[Tuple[str, str], int] = {}

    @property
    def model_id(self) -> str:
        return str(getattr(self.model, "model_id", "unknown"))

    @property
    def source(self) -> str:
        return str(getattr(self.model, "source", "unknown"))

    def _request_key(self, packet: ContextPacket, stage: str, topic_id: str, user_packet: Mapping[str, Any]) -> str:
        return stable_hash(
            {
                "analyzer_schema_version": ANALYZER_SCHEMA_VERSION,
                "packet_schema_version": packet.schema_version,
                "packet_sha256": packet.packet_sha256,
                "stage": stage,
                "topic_id": topic_id,
                "model": self.model_id,
                "source": self.source,
                "stage_prompt_version": _stage_prompt_version(stage),
                "user_packet_sha256": stable_hash(user_packet),
            }
        )

    def _record(
        self,
        *,
        stage: str,
        topic_id: str,
        status: str,
        cache_hit: bool,
        provider_call: bool,
        request_key: str,
        user_packet: Mapping[str, Any],
        source: Optional[str] = None,
        model: Optional[str] = None,
        attempt: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        error_code: Optional[str] = None,
        context_telemetry: Optional[Mapping[str, Any]] = None,
    ) -> StageLedgerRecord:
        return StageLedgerRecord(
            stage=stage,
            topic_id=topic_id,
            status=status,
            cache_hit=bool(cache_hit),
            provider_call=bool(provider_call),
            attempt=int(attempt),
            request_sha256=request_key,
            system_prefix_sha256=stable_hash(STAGE_SYSTEM_PROMPTS[stage]),
            user_packet_sha256=stable_hash(user_packet),
            source=str(source or self.source),
            model=str(model or self.model_id),
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
            latency_ms=max(0.0, float(latency_ms or 0.0)),
            error_code=error_code,
            context_telemetry=_body_free_context_telemetry(context_telemetry),
        )

    def _pending(
        self,
        stage: str,
        topic_id: str,
        key: str,
        error_code: str,
        context_telemetry: Optional[Mapping[str, Any]] = None,
    ) -> StageResult:
        return StageResult(
            stage=stage,
            status="pending",
            payload=None,
            errors=(str(error_code),),
            cache_key=key,
            context_telemetry=_body_free_context_telemetry(context_telemetry),
        )

    def _execute(
        self,
        packet: ContextPacket,
        *,
        stage: str,
        topic_id: str,
        user_packet: Mapping[str, Any],
        validator: Callable[[Any], ValidationResult],
        ledger: List[StageLedgerRecord],
        force_retry: bool = False,
    ) -> StageResult:
        key = self._request_key(packet, stage, topic_id, user_packet)
        if not force_retry:
            cached = self.cache.get(stage, key)
            if cached is not None:
                ledger.append(
                    self._record(
                        stage=stage,
                        topic_id=topic_id,
                        status="complete",
                        cache_hit=True,
                        provider_call=False,
                        request_key=key,
                        user_packet=user_packet,
                        source="stage_cache",
                    )
                )
                return StageResult(stage=stage, status="complete", payload=cached, cache_key=key)
        attempt_key = (stage, topic_id)
        attempt = self._attempts.get(attempt_key, 0) + 1
        self._attempts[attempt_key] = attempt
        started = self._clock()
        try:
            response = _coerce_response(
                self.model.complete(
                    stage,
                    STAGE_SYSTEM_PROMPTS[stage],
                    user_packet,
                    max_output_tokens=self.max_output_tokens,
                ),
                self.model,
            )
            elapsed = response.latency_ms if response.latency_ms else max(0.0, (self._clock() - started) * 1000.0)
            validation = validator(response.payload)
            if not validation.ok:
                code = validation.errors[0] if validation.errors else "stage_validation_failed"
                context_telemetry = (
                    context_validation_telemetry(response.payload, packet)
                    if stage == "A"
                    else _empty_context_telemetry()
                )
                ledger.append(
                    self._record(
                        stage=stage,
                        topic_id=topic_id,
                        status="pending",
                        cache_hit=False,
                        provider_call=True,
                        request_key=key,
                        user_packet=user_packet,
                        attempt=attempt,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        latency_ms=elapsed,
                        error_code=code,
                        context_telemetry=context_telemetry,
                    )
                )
                return self._pending(stage, topic_id, key, code, context_telemetry)
            payload = deepcopy(dict(response.payload))
            self.cache.put(stage, key, payload)
            ledger.append(
                self._record(
                    stage=stage,
                    topic_id=topic_id,
                    status="complete",
                    cache_hit=False,
                    provider_call=True,
                    request_key=key,
                    user_packet=user_packet,
                    attempt=attempt,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=elapsed,
                    source=getattr(response, "source", None),
                    model=response.model or None,
                )
            )
            return StageResult(stage=stage, status="complete", payload=payload, cache_key=key)
        except StageProviderError as exc:
            elapsed = max(0.0, (self._clock() - started) * 1000.0)
            context_telemetry = _body_free_context_telemetry(getattr(exc, "context_telemetry", None))
            ledger.append(
                self._record(
                    stage=stage,
                    topic_id=topic_id,
                    status="pending",
                    cache_hit=False,
                    provider_call=True,
                    request_key=key,
                    user_packet=user_packet,
                    attempt=attempt,
                    latency_ms=elapsed,
                    error_code=exc.code,
                    context_telemetry=context_telemetry,
                )
            )
            return self._pending(stage, topic_id, key, exc.code, context_telemetry)
        except Exception:
            # Unexpected fake/provider failures remain body-free and retryable.
            elapsed = max(0.0, (self._clock() - started) * 1000.0)
            ledger.append(
                self._record(
                    stage=stage,
                    topic_id=topic_id,
                    status="pending",
                    cache_hit=False,
                    provider_call=True,
                    request_key=key,
                    user_packet=user_packet,
                    attempt=attempt,
                    latency_ms=elapsed,
                    error_code="stage_call_failed",
                )
            )
            return self._pending(stage, topic_id, key, "stage_call_failed")

    def _previous_stage_result(
        self,
        stage: str,
        topic_id: str,
        previous: Optional[StagedAnalysisResult],
        *,
        allow: bool,
        user_packet: Mapping[str, Any],
        key: str,
        ledger: List[StageLedgerRecord],
    ) -> Optional[StageResult]:
        if not allow or previous is None:
            return None
        candidate: Optional[StageResult]
        if stage == "A":
            candidate = previous.stage_a
        elif stage == "C":
            candidate = previous.stage_c
        else:
            candidate = previous.stage_b.get(topic_id)
        if candidate is None or candidate.status != "complete" or candidate.payload is None:
            return None
        ledger.append(
            self._record(
                stage=stage,
                topic_id=topic_id,
                status="complete",
                cache_hit=True,
                provider_call=False,
                request_key=key,
                user_packet=user_packet,
                source="previous_result",
            )
        )
        return StageResult(stage=stage, status="complete", payload=deepcopy(dict(candidate.payload)), cache_key=key)

    def analyze(
        self,
        packet: Union[ContextPacket, Mapping[str, Any]],
        *,
        previous: Optional[StagedAnalysisResult] = None,
        retry_stages: Optional[Iterable[str]] = None,
    ) -> StagedAnalysisResult:
        """Analyze a packet, retaining complete earlier stages on retries."""

        if isinstance(packet, Mapping):
            packet = ContextPacket.from_mapping(packet)
        validation = validate_context_packet(packet)
        if not validation.ok:
            raise StageProtocolError(validation.errors[0])
        if previous is not None and previous.packet_sha256 != packet.packet_sha256:
            raise StageProtocolError("previous_packet_mismatch")
        retry: Set[str] = set()
        for item in retry_stages or ():
            text = str(item).upper().replace("STAGE_", "")
            if text not in STAGES:
                raise ValueError("unknown retry stage")
            retry.add(text)
        if previous is not None and not retry:
            retry = set(previous.pending_stages)
        ledger: List[StageLedgerRecord] = []

        stage_a_packet = {"stage": "A", "packet": packet.to_model_packet()}
        stage_a_key = self._request_key(packet, "A", "", stage_a_packet)
        stage_a = self._previous_stage_result(
            "A",
            "",
            previous,
            allow=previous is not None and "A" not in retry,
            user_packet=stage_a_packet,
            key=stage_a_key,
            ledger=ledger,
        )
        if stage_a is None:
            stage_a = self._execute(
                packet,
                stage="A",
                topic_id="",
                user_packet=stage_a_packet,
                validator=lambda value: validate_stage_a(value, packet),
                ledger=ledger,
                force_retry="A" in retry,
            )

        stage_b: "OrderedDict[str, StageResult]" = OrderedDict()
        claim_index: Dict[str, Dict[str, Any]] = {}
        if stage_a.status == "complete" and stage_a.payload is not None:
            topics = list(stage_a.payload.get("topics") or ())
            for topic_index, topic in enumerate(topics):
                topic_id = str(topic.get("topic_id") or "")
                stage_b_packet = {
                    "stage": "B",
                    "topic_id": topic_id,
                    "topic": {
                        "topic_id": topic_id,
                        "primary_message_ids": list(topic.get("primary_message_ids") or ()),
                        "context_message_ids": list(topic.get("context_message_ids") or ()),
                        "relation": topic.get("relation"),
                        "evidence_ids": list(topic.get("evidence_ids") or ()),
                    },
                    "packet": packet.to_model_packet(),
                }
                stage_b_key = self._request_key(packet, "B", topic_id, stage_b_packet)
                previous_b = previous.stage_b.get(topic_id) if previous is not None else None
                # A retry of Stage B targets pending topic calls.  Completed
                # topics remain reusable so one bad topic does not erase good
                # work from the same Stage A projection.
                previous_allowed = (
                    previous is not None
                    and "A" not in retry
                    and previous_b is not None
                    and previous_b.status == "complete"
                )
                result = self._previous_stage_result(
                    "B",
                    topic_id,
                    previous,
                    allow=previous_allowed,
                    user_packet=stage_b_packet,
                    key=stage_b_key,
                    ledger=ledger,
                )
                if (
                    result is None
                    and previous is not None
                    and "A" not in retry
                    and previous_b is not None
                    and previous_b.status != "complete"
                    and "B" not in retry
                ):
                    # An explicit retry of another stage must not silently
                    # spend a new B call.  Keep the pending topic result and
                    # its stable error until B is explicitly retried.
                    result = previous_b
                    ledger.append(
                        self._record(
                            stage="B",
                            topic_id=topic_id,
                            status="pending",
                            cache_hit=False,
                            provider_call=False,
                            request_key=stage_b_key,
                            user_packet=stage_b_packet,
                            source="previous_result",
                            error_code=(previous_b.errors[0] if previous_b.errors else "stage_pending"),
                        )
                    )
                if result is None:
                    result = self._execute(
                        packet,
                        stage="B",
                        topic_id=topic_id,
                        user_packet=stage_b_packet,
                        validator=lambda value, expected=topic_id: validate_stage_b(
                            value, packet, expected_topic_id=expected
                        ),
                        ledger=ledger,
                        force_retry="B" in retry and not previous_allowed,
                    )
                stage_b[topic_id] = result
                if result.status == "complete" and result.payload is not None:
                    for claim_index_value, _claim in enumerate(result.payload.get("claims") or ()):
                        claim_id = "c%d_%d" % (topic_index, claim_index_value)
                        claim_index[claim_id] = {"topic_id": topic_id, "ordinal": claim_index_value}

        all_b_complete = stage_a.status == "complete" and all(
            result.status == "complete" for result in stage_b.values()
        )
        stage_c_packet = {
            "stage": "C",
            "topic_ids": list(stage_b.keys()),
            "claim_ids": sorted(claim_index),
            "packet": {
                "packet_id": packet.packet_id,
                "scope": packet.scope,
                "message_ids": list(packet.all_message_ids),
                # Keep the same explicit primary/context split in the C audit
                # request as in A/B.  C is audit-only, but it still needs to
                # see the complete K2→K3 context boundary; otherwise an
                # adapter's adjacent/greeting evidence silently disappears
                # from the final stage request.
                "context_message_ids": list(packet.context_message_ids),
                "entity_ids": list(packet.entity_ids),
                "evidence_ids": list(packet.evidence_ids),
                # Do not hand the analyzer's mutable metadata mapping to an
                # injected provider/fake.  C may inspect authority/candidate
                # context, but it must never be able to mutate the local
                # packet's authoritative projection in place.
                "metadata": deepcopy(dict(packet.metadata)),
            },
        }
        stage_c_key = self._request_key(packet, "C", "", stage_c_packet)
        stage_c: Optional[StageResult] = None
        previous_c_allowed = previous is not None and "A" not in retry and "B" not in retry and "C" not in retry
        if all_b_complete:
            stage_c = self._previous_stage_result(
                "C",
                "",
                previous,
                allow=previous_c_allowed,
                user_packet=stage_c_packet,
                key=stage_c_key,
                ledger=ledger,
            )
            if stage_c is None:
                stage_c = self._execute(
                    packet,
                    stage="C",
                    topic_id="",
                    user_packet=stage_c_packet,
                    validator=lambda value: validate_stage_c(
                        value,
                        packet,
                        known_claim_ids=claim_index,
                        known_topic_ids=stage_b,
                    ),
                    ledger=ledger,
                    force_retry="C" in retry,
                )
        else:
            stage_c = self._pending("C", "", stage_c_key, "stage_dependency_pending")
            ledger.append(
                self._record(
                    stage="C",
                    topic_id="",
                    status="pending",
                    cache_hit=False,
                    provider_call=False,
                    request_key=stage_c_key,
                    user_packet=stage_c_packet,
                    source="dependency",
                    error_code="stage_dependency_pending",
                )
            )

        overall = "complete" if stage_a.status == "complete" and all_b_complete and stage_c.status == "complete" else "pending"
        return StagedAnalysisResult(
            packet_id=packet.packet_id,
            packet_sha256=packet.packet_sha256,
            status=overall,
            stage_a=stage_a,
            stage_b=dict(stage_b),
            stage_c=stage_c,
            claim_index=claim_index,
            ledger=tuple(ledger),
        )


def summarize_ledger(records: Iterable[StageLedgerRecord]) -> Dict[str, Any]:
    """Return body-free call/cache/token/latency counters."""

    rows = list(records)
    return {
        "record_count": len(rows),
        "provider_calls": sum(1 for row in rows if row.provider_call),
        "cache_hits": sum(1 for row in rows if row.cache_hit),
        "cache_misses": sum(1 for row in rows if row.provider_call and not row.cache_hit),
        "complete_records": sum(1 for row in rows if row.status == "complete"),
        "pending_records": sum(1 for row in rows if row.status == "pending"),
        "input_tokens": sum(row.input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "latency_ms_total": round(sum(row.latency_ms for row in rows), 3),
        "by_stage": {
            stage: {
                "provider_calls": sum(1 for row in rows if row.stage == stage and row.provider_call),
                "cache_hits": sum(1 for row in rows if row.stage == stage and row.cache_hit),
                "pending": sum(1 for row in rows if row.stage == stage and row.status == "pending"),
            }
            for stage in STAGES
        },
        "error_codes": {
            code: sum(1 for row in rows if row.error_code == code)
            for code in sorted({row.error_code for row in rows if row.error_code})
        },
    }


__all__ = [
    "ACTIONS",
    "ANALYZER_SCHEMA_VERSION",
    "CLAIM_KEYS",
    "CONTEXT_PACKET_SCHEMA_VERSION",
    "CONFLICT_REASONS",
    "ContextPacket",
    "FakeStageModel",
    "OpenAICompatibleStageModel",
    "StageCache",
    "StageLedgerRecord",
    "StageModel",
    "StageModelResponse",
    "StageProviderError",
    "StageProtocolError",
    "StageResult",
    "StagedAnalysisResult",
    "StagedDeepseekAnalyzer",
    "STAGES",
    "STAGE_A_SCHEMA_VERSION",
    "STAGE_B_SCHEMA_VERSION",
    "STAGE_C_SCHEMA_VERSION",
    "STAGE_SYSTEM_PROMPTS",
    "TOPIC_RELATIONS",
    "ValidationResult",
    "canonical_json",
    "stable_hash",
    "summarize_ledger",
    "validate_context_packet",
    "validate_stage_a",
    "validate_stage_b",
    "validate_stage_c",
]
