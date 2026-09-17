"""Offline bundle-level semantic encoding and candidate retrieval.

This module is deliberately a boundary layer, not an event, title, or UI
pipeline.  It accepts caller-supplied in-memory message mappings and returns a
small fixed semantic bundle.  There is no filesystem access and no provider is
constructed by default.  A model and an embedder are optional injected
interfaces; tests and local callers can therefore exercise the complete path
with deterministic fakes.

The model-facing contract keeps actor roles separate (``speaker``,
``subject``, and ``mentioned_person``), carries object/action/target slots,
and requires typed evidence references.  Retrieval uses a sparse inverted
index and structural keys first.  A dense embedder, when explicitly injected,
can only add recall candidates; it cannot turn a dense-only match into a
strong semantic link.  Pairwise judgement remains a separate, evidence-checked
operation.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Protocol, Sequence, Set, Tuple, Union


BUNDLE_SCHEMA_VERSION = "bundle_semantics_v1"
BUNDLE_PROMPT_VERSION = "bundle_semantics_prompt_v1"
BUNDLE_RULESET_VERSION = "bundle_semantics_rules_v1"
UNKNOWN = "unknown"

ENTITY_RESOLUTIONS = frozenset({"explicit", "inherited", UNKNOWN})
ENTITY_TYPES = frozenset({"person", "object", "group", "organization", "target", "action", UNKNOWN})
CLAIM_TYPES = frozenset({"fact", "opinion", "question", "request", "suggestion", "hypothesis", UNKNOWN})
STATES = frozenset({"planned", "ongoing", "resolved", "failed", "cancelled", UNKNOWN})
MODALITIES = frozenset({"certain", "probable", "possible", "required", "desired", UNKNOWN})
RELATION_LABELS = frozenset(
    {"continues", "elaborates", "answers", "contrasts", "topic_shift", "possibly_related", "insufficient"}
)
RELATION_STRENGTHS = frozenset({"strong", "medium", "weak", "none"})
RUN_STATUSES = frozenset({"complete", "fallback", "pending"})

# Generic schema labels do not make a useful lexical key.  They remain in
# structural keys and the validated bundle, but excluding them here prevents
# every object/person/state from becoming a false sparse match.
_INDEX_STOPWORDS = frozenset(
    set(ENTITY_TYPES)
    | {"speaker", "subject", "mentioned_person", "target", "object", "action", "unknown"}
    | set(CLAIM_TYPES)
    | set(STATES)
    | set(MODALITIES)
)

BUNDLE_FIELDS = (
    "schema_version",
    "bundle_id",
    "message_ids",
    "speaker",
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
    "uncertainties",
    "evidence",
    "metadata",
)

_BODY_KEYS = frozenset(
    {
        "text",
        "content",
        "body",
        "raw",
        "raw_text",
        "raw_message",
        "message_text",
        "surface",
        "surface_text",
        "surface_redacted",
        "evidence_text",
        "quote",
        "summary",
        "narrative",
        "prompt",
        "response",
    }
)
_SAFE_MESSAGE_FIELDS = frozenset(
    {
        "message_id",
        "chat_id",
        "speaker_id",
        "sender_id",
        "timestamp",
        "time_offset_seconds",
        "sequence_in_chat",
        "reply_to_message_id",
        "segment_id",
        "message_type",
        "content",
    }
)


class BundleSemanticError(ValueError):
    """Base error for contract and adapter failures."""


class BundleSchemaError(BundleSemanticError):
    """Raised when a model response cannot be normalized safely."""


class BundleModel(Protocol):
    """Optional injected model interface.

    Implementations may provide either ``encode_bundle`` or ``encode``.
    Pairwise callers may additionally provide ``judge_pair``.
    """

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class BundleEmbedder(Protocol):
    """Optional injected embedder used only for recall."""

    def embed(self, value: Mapping[str, Any]) -> Sequence[float]:
        ...


class PairwiseModel(Protocol):
    def judge_pair(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    return value


def canonical_json(value: Any) -> str:
    """Return stable JSON for hashing and cache keys."""

    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _string(value: Any, default: str = UNKNOWN) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _known(value: Any) -> bool:
    return value not in (None, "", UNKNOWN, "UNKNOWN", "ACTOR_UNKNOWN", "OBJECT_UNKNOWN")


def _unique_strings(values: Iterable[Any]) -> Tuple[str, ...]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        item = _string(value)
        if item == UNKNOWN or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _unknown_entity(role: str = UNKNOWN) -> Dict[str, Any]:
    return {
        "id": UNKNOWN,
        "type": "person" if role in {"speaker", "subject", "mentioned_person"} else UNKNOWN,
        "role": role,
        "resolution": UNKNOWN,
        "evidence_ids": [],
    }


def _normalize_evidence(value: Any, fallback_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    message_id = _string(value.get("message_id"), UNKNOWN)
    span_value = value.get("span")
    if not isinstance(span_value, Mapping):
        span_value = {"start": value.get("start"), "end": value.get("end")}
    try:
        start = int(span_value.get("start"))
        end = int(span_value.get("end"))
    except (TypeError, ValueError):
        start = end = -1
    evidence_id = _string(value.get("evidence_id") or fallback_id, UNKNOWN)
    field = _string(value.get("field"), UNKNOWN)
    if evidence_id == UNKNOWN:
        return None
    return {
        "evidence_id": evidence_id,
        "message_id": message_id,
        "span": {"start": start, "end": end},
        "field": field,
        "kind": _string(value.get("kind"), "span"),
    }


def _normalize_entity(value: Any, role: str) -> Dict[str, Any]:
    if isinstance(value, str):
        value = {"id": value}
    if not isinstance(value, Mapping):
        return _unknown_entity(role)
    entity_id = _string(value.get("id") or value.get("entity_id") or value.get("person_id"))
    resolution = _string(value.get("resolution"), "explicit" if _known(entity_id) else UNKNOWN)
    if not _known(entity_id) or resolution not in ENTITY_RESOLUTIONS:
        entity_id = UNKNOWN if not _known(entity_id) else entity_id
        if resolution not in ENTITY_RESOLUTIONS:
            resolution = UNKNOWN
    entity_type = _string(value.get("type") or value.get("entity_type"), "person" if role in {"speaker", "subject", "mentioned_person"} else UNKNOWN)
    if entity_type not in ENTITY_TYPES:
        entity_type = UNKNOWN
    evidence_values = value.get("evidence_ids") or value.get("evidence_refs") or ()
    evidence_ids: List[str] = []
    for item in evidence_values if isinstance(evidence_values, (list, tuple, set)) else (evidence_values,):
        if isinstance(item, Mapping):
            item = item.get("evidence_id") or item.get("id")
        item = _string(item)
        if item != UNKNOWN and item not in evidence_ids:
            evidence_ids.append(item)
    return {
        "id": entity_id,
        "type": entity_type,
        "role": _string(value.get("role"), role),
        "resolution": resolution,
        "evidence_ids": evidence_ids,
    }


def _normalize_slot_list(value: Any, role: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [_normalize_entity(item, role) for item in values]


def _normalize_action(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        value = {"id": stable_hash({"action": value})[:20], "label": value}
    if not isinstance(value, Mapping):
        return {"id": UNKNOWN, "label": UNKNOWN, "resolution": UNKNOWN, "evidence_ids": []}
    action_id = _string(value.get("id") or value.get("action_id"))
    label = _string(value.get("label") or value.get("action"))
    resolution = _string(value.get("resolution"), "explicit" if _known(label) else UNKNOWN)
    if resolution not in ENTITY_RESOLUTIONS:
        resolution = UNKNOWN
    evidence_values = value.get("evidence_ids") or ()
    evidence_ids = [_string(item) for item in evidence_values if _string(item) != UNKNOWN] if isinstance(evidence_values, (list, tuple)) else []
    return {"id": action_id, "label": label, "resolution": resolution, "evidence_ids": sorted(set(evidence_ids))}


def _normalize_uncertainties(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    output: List[Dict[str, Any]] = []
    for item in values:
        if isinstance(item, str):
            item = {"code": item}
        if not isinstance(item, Mapping):
            continue
        code = _string(item.get("code") or item.get("reason"))
        if code == UNKNOWN:
            continue
        output.append(
            {
                "code": code,
                "field": _string(item.get("field"), UNKNOWN),
                "severity": _string(item.get("severity"), "medium"),
            }
        )
    return output


def _normalize_coreference(value: Any) -> List[Dict[str, Any]]:
    values = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
    output: List[Dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = -1.0
        evidence_ids = item.get("evidence_ids") or ()
        if not isinstance(evidence_ids, (list, tuple)):
            evidence_ids = ()
        output.append(
            {
                "source_id": _string(item.get("source_id") or item.get("source")),
                "target_id": _string(item.get("target_id") or item.get("target")),
                "relation": _string(item.get("relation"), "corefers"),
                "score": score,
                "evidence_ids": list(_unique_strings(evidence_ids)),
            }
        )
    return output


def _normalize_relations(value: Any) -> List[Dict[str, Any]]:
    values = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
    output: List[Dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        evidence_ids = item.get("evidence_ids") or ()
        if not isinstance(evidence_ids, (list, tuple)):
            evidence_ids = ()
        output.append(
            {
                "relation_id": _string(item.get("relation_id") or item.get("id")),
                "source_bundle_id": _string(item.get("source_bundle_id") or item.get("left_bundle_id") or item.get("source")),
                "target_bundle_id": _string(item.get("target_bundle_id") or item.get("right_bundle_id") or item.get("target")),
                "label": _string(item.get("label") or item.get("relation"), "insufficient"),
                "strength": _string(item.get("strength") or item.get("evidence_strength"), "none"),
                "evidence_ids": list(_unique_strings(evidence_ids)),
                "supporting_signals": list(_unique_strings(item.get("supporting_signals") or ())),
                "explicit_reply_present": bool(item.get("explicit_reply_present", False)),
                "left_chat_id": _string(item.get("left_chat_id"), UNKNOWN),
                "right_chat_id": _string(item.get("right_chat_id"), UNKNOWN),
            }
        )
    return output


def _normalize_metadata(value: Any, *, status: str, source: str, chat_id: Optional[str]) -> Dict[str, Any]:
    metadata = dict(value) if isinstance(value, Mapping) else {}
    # Metadata is intentionally a small, body-free audit surface.
    safe = {
        "status": _string(metadata.get("status"), status),
        "source": _string(metadata.get("source"), source),
        "chat_id": _string(metadata.get("chat_id") or chat_id, UNKNOWN),
        "model_version": _string(metadata.get("model_version"), UNKNOWN),
        "prompt_version": _string(metadata.get("prompt_version"), UNKNOWN),
        "ruleset_version": _string(metadata.get("ruleset_version"), UNKNOWN),
        "input_sha256": _string(metadata.get("input_sha256"), UNKNOWN),
    }
    if metadata.get("fallback") is True:
        safe["fallback"] = True
    return safe


def empty_bundle(
    bundle_id: str = "bundle:unknown",
    message_ids: Iterable[Any] = (),
    *,
    chat_id: Optional[str] = None,
    status: str = "pending",
    source: str = UNKNOWN,
    uncertainties: Iterable[Any] = (),
    schema_version: str = BUNDLE_SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Create a contract-shaped all-unknown bundle."""

    return {
        "schema_version": schema_version,
        "bundle_id": _string(bundle_id, "bundle:unknown"),
        "message_ids": list(_unique_strings(message_ids)),
        "speaker": _unknown_entity("speaker"),
        "subject": _unknown_entity("subject"),
        "mentioned_person": [],
        "target": [],
        "object": [],
        "action": [],
        "claim_type": UNKNOWN,
        "state": UNKNOWN,
        "modality": UNKNOWN,
        "coreference_candidates": [],
        "context_relations": [],
        "uncertainties": _normalize_uncertainties(uncertainties),
        "evidence": [],
        "metadata": _normalize_metadata({}, status=status, source=source, chat_id=chat_id),
    }


def normalize_bundle(
    value: Any,
    *,
    bundle_id: Optional[str] = None,
    message_ids: Iterable[Any] = (),
    chat_id: Optional[str] = None,
    status: str = "complete",
    source: str = "model",
    schema_version: str = BUNDLE_SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Project an arbitrary model mapping into the fixed body-free schema."""

    raw = value.get("bundle") if isinstance(value, Mapping) and isinstance(value.get("bundle"), Mapping) else value
    if not isinstance(raw, Mapping):
        raise BundleSchemaError("model response is not an object")
    raw = dict(raw)
    expected_ids = tuple(_unique_strings(message_ids))
    output = empty_bundle(
        bundle_id if bundle_id is not None else raw.get("bundle_id", "bundle:unknown"),
        expected_ids,
        chat_id=chat_id,
        status=status,
        source=source,
        schema_version=_string(raw.get("schema_version"), schema_version),
    )
    if bundle_id is None and _known(raw.get("bundle_id")):
        output["bundle_id"] = _string(raw.get("bundle_id"))
    raw_message_ids = raw.get("message_ids")
    if isinstance(raw_message_ids, (list, tuple)):
        output["message_ids"] = list(_unique_strings(raw_message_ids))
    output["speaker"] = _normalize_entity(raw.get("speaker"), "speaker")
    output["subject"] = _normalize_entity(raw.get("subject"), "subject")
    output["mentioned_person"] = _normalize_slot_list(
        raw.get("mentioned_person", raw.get("mentioned_persons")), "mentioned_person"
    )
    output["target"] = _normalize_slot_list(raw.get("target", raw.get("targets")), "target")
    output["object"] = _normalize_slot_list(raw.get("object", raw.get("objects")), "object")
    action_values = raw.get("action", raw.get("actions"))
    if action_values is None:
        action_values = []
    action_values = action_values if isinstance(action_values, (list, tuple)) else [action_values]
    output["action"] = [_normalize_action(item) for item in action_values]
    output["claim_type"] = _string(raw.get("claim_type"))
    output["state"] = _string(raw.get("state"))
    output["modality"] = _string(raw.get("modality"))
    output["coreference_candidates"] = _normalize_coreference(raw.get("coreference_candidates"))
    output["context_relations"] = _normalize_relations(raw.get("context_relations"))
    evidence_values = raw.get("evidence") or ()
    evidence_values = evidence_values if isinstance(evidence_values, (list, tuple)) else [evidence_values]
    evidence: List[Dict[str, Any]] = []
    for item in evidence_values:
        normalized = _normalize_evidence(item)
        if normalized is not None:
            evidence.append(normalized)
    output["evidence"] = evidence
    output["uncertainties"] = _normalize_uncertainties(raw.get("uncertainties"))
    output["metadata"] = _normalize_metadata(
        raw.get("metadata"), status=status, source=source, chat_id=chat_id
    )
    return output


def bundle_schema(schema_version: str = BUNDLE_SCHEMA_VERSION) -> Dict[str, Any]:
    """Return the fixed model response schema as an ordinary JSON mapping."""

    entity_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "type", "role", "resolution", "evidence_ids"],
        "properties": {
            "id": {"type": "string"},
            "type": {"enum": sorted(ENTITY_TYPES)},
            "role": {"type": "string"},
            "resolution": {"enum": sorted(ENTITY_RESOLUTIONS)},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(BUNDLE_FIELDS),
        "properties": {
            "schema_version": {"const": _string(schema_version, BUNDLE_SCHEMA_VERSION)},
            "bundle_id": {"type": "string"},
            "message_ids": {"type": "array", "items": {"type": "string"}},
            "speaker": entity_schema,
            "subject": entity_schema,
            "mentioned_person": {"type": "array", "items": entity_schema},
            "target": {"type": "array", "items": entity_schema},
            "object": {"type": "array", "items": entity_schema},
            "action": {"type": "array", "items": {"type": "object"}},
            "claim_type": {"enum": sorted(CLAIM_TYPES)},
            "state": {"enum": sorted(STATES)},
            "modality": {"enum": sorted(MODALITIES)},
            "coreference_candidates": {"type": "array", "items": {"type": "object"}},
            "context_relations": {"type": "array", "items": {"type": "object"}},
            "uncertainties": {"type": "array", "items": {"type": "object"}},
            "evidence": {"type": "array", "items": {"type": "object"}},
            "metadata": {"type": "object"},
        },
    }


def _body_key_paths(value: Any, path: str = "") -> List[str]:
    paths: List[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            lower = key_text.casefold()
            if key_text in _BODY_KEYS or lower.endswith("_text") or lower.endswith("_content") or lower.endswith("_surface"):
                paths.append(path + "/" + key_text)
            paths.extend(_body_key_paths(item, path + "/" + key_text))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            paths.extend(_body_key_paths(item, path + "[%d]" % index))
    return paths


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)
    bundle_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checks": _jsonable(self.checks),
            "bundle_sha256": self.bundle_sha256,
        }


def _evidence_ids_for(value: Any) -> Set[str]:
    if not isinstance(value, Mapping):
        return set()
    values = value.get("evidence_ids") or ()
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {_string(item) for item in values if _known(item)}


def _validate_entity(
    value: Any,
    field_name: str,
    evidence_ids: Set[str],
    errors: List[str],
    warnings: List[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append("%s_not_object" % field_name)
        return
    entity_id = _string(value.get("id"))
    resolution = _string(value.get("resolution"))
    entity_type = _string(value.get("type"))
    if resolution not in ENTITY_RESOLUTIONS:
        errors.append("%s_invalid_resolution" % field_name)
    if entity_type not in ENTITY_TYPES:
        errors.append("%s_invalid_type" % field_name)
    local_evidence = _evidence_ids_for(value)
    if _known(entity_id) and resolution != UNKNOWN and not local_evidence:
        errors.append("%s_missing_evidence" % field_name)
    if local_evidence - evidence_ids:
        errors.append("%s_unknown_evidence" % field_name)
    if not _known(entity_id) and resolution != UNKNOWN:
        errors.append("%s_unknown_id_with_concrete_resolution" % field_name)
    if field_name in {"speaker", "subject"} and value.get("role") not in {field_name, UNKNOWN}:
        warnings.append("%s_role_mismatch" % field_name)


def _validate_relation_evidence(
    relation: Mapping[str, Any],
    evidence_ids: Set[str],
    errors: List[str],
    index: int,
    *,
    allow_cross_chat: bool = False,
) -> None:
    label = _string(relation.get("label"))
    strength = _string(relation.get("strength"))
    if label not in RELATION_LABELS:
        errors.append("context_relation_%d_invalid_label" % index)
    if strength not in RELATION_STRENGTHS:
        errors.append("context_relation_%d_invalid_strength" % index)
    source = _string(relation.get("source_bundle_id"))
    target = _string(relation.get("target_bundle_id"))
    if source == UNKNOWN or target == UNKNOWN:
        errors.append("context_relation_%d_missing_endpoint" % index)
    if source == target and source != UNKNOWN:
        errors.append("context_relation_%d_self_link" % index)
    local_evidence = _evidence_ids_for(relation)
    if label != "insufficient" and not local_evidence:
        errors.append("context_relation_%d_missing_evidence" % index)
    if local_evidence - evidence_ids:
        errors.append("context_relation_%d_unknown_evidence" % index)
    signals = {_string(item).casefold() for item in relation.get("supporting_signals", ()) or ()}
    only_time = bool(signals) and signals.issubset({"time", "time_near", "time_proximity", "temporal", "time_distance"})
    if only_time and strength == "strong":
        errors.append("context_relation_%d_time_only_strong_forbidden" % index)
    if not allow_cross_chat and relation.get("left_chat_id") not in (None, UNKNOWN) and relation.get("right_chat_id") not in (None, UNKNOWN):
        if relation.get("left_chat_id") != relation.get("right_chat_id"):
            errors.append("context_relation_%d_cross_chat" % index)


def validate_bundle(
    bundle: Mapping[str, Any],
    *,
    message_ids: Optional[Iterable[Any]] = None,
    chat_id: Optional[str] = None,
    expected_schema_version: str = BUNDLE_SCHEMA_VERSION,
    allow_cross_chat: bool = False,
) -> ValidationReport:
    """Validate schema, typed evidence, conflicts, and chat boundaries."""

    errors: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, Any] = {}
    if not isinstance(bundle, Mapping):
        return ValidationReport(False, ("bundle_not_object",), bundle_sha256=stable_hash(bundle))
    body_paths = _body_key_paths(bundle)
    if body_paths:
        errors.append("body_field_present")
        checks["body_field_count"] = len(body_paths)
    missing = [field_name for field_name in BUNDLE_FIELDS if field_name not in bundle]
    if missing:
        errors.append("missing_fields:%s" % ",".join(missing))
    if bundle.get("schema_version") != expected_schema_version:
        errors.append("schema_version_mismatch")
    if not _known(bundle.get("bundle_id")):
        errors.append("missing_bundle_id")
    actual_message_ids = [_string(item) for item in bundle.get("message_ids", ())] if isinstance(bundle.get("message_ids"), (list, tuple)) else []
    if len(actual_message_ids) != len(set(actual_message_ids)):
        errors.append("duplicate_message_ids")
    expected_message_ids = {_string(item) for item in message_ids} if message_ids is not None else set(actual_message_ids)
    if expected_message_ids and not set(actual_message_ids).issubset(expected_message_ids):
        errors.append("message_id_out_of_scope")
    evidence_values = bundle.get("evidence", ())
    if not isinstance(evidence_values, (list, tuple)):
        errors.append("evidence_not_list")
        evidence_values = ()
    evidence_ids: Set[str] = set()
    evidence_message_ids: Set[str] = set()
    for index, item in enumerate(evidence_values):
        if not isinstance(item, Mapping):
            errors.append("evidence_%d_not_object" % index)
            continue
        evidence_id = _string(item.get("evidence_id"))
        message_id = _string(item.get("message_id"))
        span = item.get("span")
        if evidence_id == UNKNOWN or evidence_id in evidence_ids:
            errors.append("evidence_%d_duplicate_or_missing_id" % index)
        evidence_ids.add(evidence_id)
        evidence_message_ids.add(message_id)
        if expected_message_ids and message_id not in expected_message_ids:
            errors.append("evidence_%d_message_out_of_scope" % index)
        if not isinstance(span, Mapping):
            errors.append("evidence_%d_missing_span" % index)
        else:
            try:
                start, end = int(span.get("start")), int(span.get("end"))
            except (TypeError, ValueError):
                start = end = -1
            if start < 0 or end < start:
                errors.append("evidence_%d_invalid_span" % index)
    for field_name in ("speaker", "subject"):
        _validate_entity(bundle.get(field_name), field_name, evidence_ids, errors, warnings)
    for field_name in ("mentioned_person", "target", "object"):
        values = bundle.get(field_name)
        if not isinstance(values, (list, tuple)):
            errors.append("%s_not_list" % field_name)
            continue
        for index, value in enumerate(values):
            _validate_entity(value, "%s_%d" % (field_name, index), evidence_ids, errors, warnings)
    actions = bundle.get("action")
    if not isinstance(actions, (list, tuple)):
        errors.append("action_not_list")
        actions = ()
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping):
            errors.append("action_%d_not_object" % index)
            continue
        resolution = _string(action.get("resolution"))
        label = _string(action.get("label"))
        if resolution not in ENTITY_RESOLUTIONS:
            errors.append("action_%d_invalid_resolution" % index)
        if _known(label) and resolution != UNKNOWN and not _evidence_ids_for(action):
            errors.append("action_%d_missing_evidence" % index)
        if _evidence_ids_for(action) - evidence_ids:
            errors.append("action_%d_unknown_evidence" % index)
    for field_name, allowed in (("claim_type", CLAIM_TYPES), ("state", STATES), ("modality", MODALITIES)):
        if bundle.get(field_name) not in allowed:
            errors.append("%s_invalid_value" % field_name)
        elif bundle.get(field_name) != UNKNOWN and not any(
            isinstance(item, Mapping) and item.get("field") == field_name for item in evidence_values
        ):
            errors.append("%s_missing_evidence" % field_name)
    corefs = bundle.get("coreference_candidates")
    if not isinstance(corefs, (list, tuple)):
        errors.append("coreference_candidates_not_list")
        corefs = ()
    seen_coref: Dict[Tuple[str, str], Tuple[str, float]] = {}
    for index, candidate in enumerate(corefs):
        if not isinstance(candidate, Mapping):
            errors.append("coreference_%d_not_object" % index)
            continue
        source = _string(candidate.get("source_id"))
        target = _string(candidate.get("target_id"))
        relation = _string(candidate.get("relation"))
        try:
            score = float(candidate.get("score"))
        except (TypeError, ValueError):
            score = -1.0
        if source == UNKNOWN or target == UNKNOWN or source == target:
            errors.append("coreference_%d_invalid_endpoint" % index)
        if not 0.0 <= score <= 1.0:
            errors.append("coreference_%d_invalid_score" % index)
        local_evidence = _evidence_ids_for(candidate)
        if local_evidence - evidence_ids:
            errors.append("coreference_%d_unknown_evidence" % index)
        key = (source, target)
        old = seen_coref.get(key)
        if old is not None and old != (relation, score):
            errors.append("coreference_conflict")
        seen_coref[key] = (relation, score)
        if relation == UNKNOWN:
            warnings.append("coreference_%d_relation_unknown" % index)
    relations = bundle.get("context_relations")
    if not isinstance(relations, (list, tuple)):
        errors.append("context_relations_not_list")
        relations = ()
    seen_relations: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for index, relation in enumerate(relations):
        if not isinstance(relation, Mapping):
            errors.append("context_relation_%d_not_object" % index)
            continue
        _validate_relation_evidence(relation, evidence_ids, errors, index, allow_cross_chat=allow_cross_chat)
        key = (_string(relation.get("source_bundle_id")), _string(relation.get("target_bundle_id")))
        value = (_string(relation.get("label")), _string(relation.get("strength")))
        old = seen_relations.get(key)
        if old is not None and old != value:
            errors.append("context_relation_conflict")
        seen_relations[key] = value
        if not allow_cross_chat and relation.get("left_chat_id") not in (None, UNKNOWN) and relation.get("right_chat_id") not in (None, UNKNOWN):
            if relation.get("left_chat_id") != relation.get("right_chat_id"):
                errors.append("cross_chat_link_forbidden")
    metadata = bundle.get("metadata")
    if not isinstance(metadata, Mapping):
        errors.append("metadata_not_object")
    elif chat_id is not None and _known(metadata.get("chat_id")) and metadata.get("chat_id") != chat_id:
        errors.append("bundle_chat_mismatch")
    checks.update(
        {
            "required_field_count": len(BUNDLE_FIELDS) - len(missing),
            "message_count": len(actual_message_ids),
            "evidence_count": len(evidence_ids),
            "known_evidence_message_count": len(evidence_message_ids - {UNKNOWN}),
            "relation_count": len(relations),
            "coreference_count": len(corefs),
        }
    )
    return ValidationReport(
        not errors,
        tuple(sorted(set(errors))),
        tuple(sorted(set(warnings))),
        checks,
        stable_hash(bundle),
    )


@dataclass
class PipelineStats:
    model_calls: int = 0
    model_successes: int = 0
    model_failures: int = 0
    model_retries: int = 0
    model_validation_failures: int = 0
    fallback_calls: int = 0
    fallback_failures: int = 0
    pending_count: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    embedder_calls: int = 0
    embedder_failures: int = 0
    pairwise_calls: int = 0
    pairwise_successes: int = 0
    pairwise_failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms_total: float = 0.0

    def snapshot(self) -> Dict[str, Any]:
        calls = self.model_calls + self.pairwise_calls
        return {
            "model_calls": self.model_calls,
            "model_successes": self.model_successes,
            "model_failures": self.model_failures,
            "model_retries": self.model_retries,
            "model_validation_failures": self.model_validation_failures,
            "fallback_calls": self.fallback_calls,
            "fallback_failures": self.fallback_failures,
            "pending_count": self.pending_count,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "embedder_calls": self.embedder_calls,
            "embedder_failures": self.embedder_failures,
            "pairwise_calls": self.pairwise_calls,
            "pairwise_successes": self.pairwise_successes,
            "pairwise_failures": self.pairwise_failures,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms_total": round(self.latency_ms_total, 3),
            "latency_ms_average": round(self.latency_ms_total / calls, 3) if calls else "N/A",
        }


@dataclass(frozen=True)
class EncodingOutcome:
    bundle: Mapping[str, Any]
    status: str
    input_sha256: str
    cache_key: str
    validation: ValidationReport
    stats: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle": _jsonable(self.bundle),
            "status": self.status,
            "input_sha256": self.input_sha256,
            "cache_key": self.cache_key,
            "validation": self.validation.to_dict(),
            "stats": _jsonable(self.stats),
        }


class VersionedBundleCache:
    """Small in-memory versioned cache; persistence is an explicit caller job."""

    def __init__(self) -> None:
        self._values: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def key(
        input_sha256: str,
        *,
        schema_version: str,
        model_version: str,
        prompt_version: str,
        ruleset_version: str = UNKNOWN,
    ) -> str:
        return stable_hash(
            {
                "input_sha256": input_sha256,
                "schema_version": schema_version,
                "model_version": model_version,
                "prompt_version": prompt_version,
                "ruleset_version": ruleset_version,
            }
        )

    def get(self, key: str) -> Optional[Mapping[str, Any]]:
        value = self._values.get(str(key))
        return deepcopy(value) if value is not None else None

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self._values[str(key)] = deepcopy(dict(value))

    def clear(self) -> None:
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)


def _safe_messages(messages: Iterable[Mapping[str, Any]]) -> Tuple[Dict[str, Any], ...]:
    """Copy only explicit in-memory public fields; never read raw/private keys."""

    output: List[Dict[str, Any]] = []
    for value in messages:
        if not isinstance(value, Mapping):
            raise BundleSemanticError("messages must be mappings")
        row = {key: _jsonable(value[key]) for key in _SAFE_MESSAGE_FIELDS if key in value}
        if "message_id" not in row:
            raise BundleSemanticError("message is missing message_id")
        row["message_id"] = _string(row["message_id"])
        output.append(row)
    if not output:
        raise BundleSemanticError("bundle messages are empty")
    ids = [item["message_id"] for item in output]
    if len(ids) != len(set(ids)):
        raise BundleSemanticError("bundle message_id is duplicated")
    return tuple(output)


def _estimate_tokens(value: Any) -> int:
    text = canonical_json(value)
    return max(1, len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))) if text else 0


def _model_method(model: Any, name: str) -> Callable[[Mapping[str, Any]], Any]:
    method = getattr(model, name, None)
    if callable(method):
        return method
    if name == "encode_bundle":
        method = getattr(model, "encode", None)
        if callable(method):
            return method
    if callable(model) and name == "encode_bundle":
        return model
    raise BundleSemanticError("injected model does not implement %s" % name)


def _usage_tokens(value: Any) -> Tuple[int, int]:
    if not isinstance(value, Mapping):
        return 0, 0
    usage = value.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0
    def number(*keys: str) -> int:
        for key in keys:
            try:
                if usage.get(key) is not None:
                    return max(0, int(usage[key]))
            except (TypeError, ValueError):
                continue
        return 0
    return number("input_tokens", "prompt_tokens"), number("output_tokens", "completion_tokens")


def _safe_ref(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _ref_id(value: Any, *names: str) -> str:
    item = _safe_ref(value)
    for name in names:
        if _known(item.get(name)):
            return _string(item.get(name))
    return UNKNOWN


def _ref_message_id(value: Any) -> str:
    item = _safe_ref(value)
    result = _string(item.get("message_id"))
    if result != UNKNOWN:
        return result
    refs = item.get("evidence_refs") or item.get("evidence") or ()
    if isinstance(refs, (list, tuple)):
        for ref in refs:
            candidate = _string(_safe_ref(ref).get("message_id"))
            if candidate != UNKNOWN:
                return candidate
    return UNKNOWN


def _fallback_evidence(
    field: str,
    value: Any,
    evidence: List[Dict[str, Any]],
    known_message_ids: Set[str],
) -> List[str]:
    item = _safe_ref(value)
    message_id = _ref_message_id(item)
    if message_id == UNKNOWN or message_id not in known_message_ids:
        return []
    try:
        start = int(item.get("span_start", item.get("start", 0)))
        end = int(item.get("span_end", item.get("end", start)))
    except (TypeError, ValueError):
        start = end = 0
    if start < 0 or end < start:
        return []
    evidence_id = "evidence:%s" % stable_hash({"field": field, "message_id": message_id, "start": start, "end": end})[:24]
    if not any(row["evidence_id"] == evidence_id for row in evidence):
        evidence.append(
            {
                "evidence_id": evidence_id,
                "message_id": message_id,
                "span": {"start": start, "end": end},
                "field": field,
                "kind": "span",
            }
        )
    return [evidence_id]


def _fallback_bundle(
    messages: Sequence[Mapping[str, Any]],
    *,
    bundle_id: str,
    chat_id: Optional[str],
    input_sha256: str,
    schema_version: str,
    model_version: str,
    prompt_version: str,
    ruleset_version: str,
) -> Dict[str, Any]:
    """Use contextual_fragments as a conservative, body-free fallback."""

    # Lazy import keeps the default module import independent of the extractor
    # and makes it impossible for this bundle module to touch any store.
    from . import contextual_fragments as fallback_extractor

    result = fallback_extractor.extract_context_fragments(messages)
    message_ids = tuple(_string(item.get("message_id")) for item in messages)
    known_message_ids = set(message_ids)
    output = empty_bundle(
        bundle_id,
        message_ids,
        chat_id=chat_id,
        status="fallback",
        source="contextual_fragments_fallback",
        uncertainties=(
            {"code": "fallback_only", "field": UNKNOWN, "severity": "medium"},
            {"code": "coreference_pending", "field": "coreference_candidates", "severity": "medium"},
            {"code": "context_relations_not_promoted", "field": "context_relations", "severity": "medium"},
        ),
        schema_version=schema_version,
    )
    evidence: List[Dict[str, Any]] = []
    fragments = tuple(getattr(result, "fragments", ()) or ())
    first_fragment = _safe_ref(fragments[0]) if fragments else {}
    first_fragment_dict = fragments[0].to_dict() if fragments and hasattr(fragments[0], "to_dict") else first_fragment
    speaker_ref = _safe_ref(first_fragment_dict.get("speaker"))
    subject_ref = _safe_ref(first_fragment_dict.get("subject"))
    for field_name, ref, role in (("speaker", speaker_ref, "speaker"), ("subject", subject_ref, "subject")):
        if _known(_ref_id(ref, "actor_id", "person_id", "id")):
            evidence_ids = _fallback_evidence(field_name, ref, evidence, known_message_ids)
            if not evidence_ids:
                # The fallback stays conservative if a DTO has no locatable evidence.
                continue
            entity = _normalize_entity(
                {
                    "id": _ref_id(ref, "actor_id", "person_id", "id"),
                    "type": "person",
                    "role": role,
                    "resolution": _string(ref.get("resolution"), "explicit"),
                    "evidence_ids": evidence_ids,
                },
                role,
            )
            output[field_name] = entity
    mentioned: List[Dict[str, Any]] = []
    for fragment in fragments:
        fragment_dict = fragment.to_dict() if hasattr(fragment, "to_dict") else _safe_ref(fragment)
        refs = fragment_dict.get("mentioned_person_refs") or fragment_dict.get("mentioned_persons") or ()
        refs = refs if isinstance(refs, (list, tuple)) else ()
        for ref in refs:
            ref = _safe_ref(ref)
            person_id = _ref_id(ref, "actor_id", "person_id", "id")
            evidence_ids = _fallback_evidence("mentioned_person", ref, evidence, known_message_ids)
            if person_id != UNKNOWN and evidence_ids:
                mentioned.append(
                    _normalize_entity(
                        {
                            "id": person_id,
                            "type": "person",
                            "role": "mentioned_person",
                            "resolution": _string(ref.get("resolution"), "explicit"),
                            "evidence_ids": evidence_ids,
                        },
                        "mentioned_person",
                    )
                )
    output["mentioned_person"] = mentioned
    objects: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    scalar_values: Dict[str, Set[str]] = {"claim_type": set(), "state": set(), "modality": set()}
    for fragment in fragments:
        fragment_dict = fragment.to_dict() if hasattr(fragment, "to_dict") else _safe_ref(fragment)
        object_refs = fragment_dict.get("object_refs") or fragment_dict.get("objects") or ()
        object_refs = object_refs if isinstance(object_refs, (list, tuple)) else ()
        for ref in object_refs:
            ref = _safe_ref(ref)
            object_id = _ref_id(ref, "object_id", "id")
            evidence_ids = _fallback_evidence("object", ref, evidence, known_message_ids)
            if object_id != UNKNOWN and evidence_ids:
                objects.append(
                    _normalize_entity(
                        {
                            "id": object_id,
                            "type": "object",
                            "role": "object",
                            "resolution": _string(ref.get("resolution") or ref.get("object_resolution"), UNKNOWN),
                            "evidence_ids": evidence_ids,
                        },
                        "object",
                    )
                )
        for action in fragment_dict.get("actions") or ():
            label = _string(action)
            evidence_ids = _fallback_evidence("action", fragment_dict, evidence, known_message_ids)
            if label != UNKNOWN and evidence_ids:
                actions.append(_normalize_action({"label": label, "resolution": "explicit", "evidence_ids": evidence_ids}))
        for field_name, aliases in (
            ("claim_type", ("claim_type", "claim_role", "intent")),
            ("state", ("state", "status")),
            ("modality", ("modality", "speech_modality")),
        ):
            for alias in aliases:
                value = _string(fragment_dict.get(alias))
                if value != UNKNOWN:
                    scalar_values[field_name].add(value)
                    break
    output["object"] = objects
    output["action"] = actions
    for field_name, allowed in (("claim_type", CLAIM_TYPES), ("state", STATES), ("modality", MODALITIES)):
        values = {item for item in scalar_values[field_name] if item in allowed and item != UNKNOWN}
        if len(values) == 1:
            value = next(iter(values))
            # Tie the scalar to the first fragment span; if unavailable keep unknown.
            evidence_ids = _fallback_evidence(field_name, first_fragment_dict, evidence, known_message_ids)
            if evidence_ids:
                output[field_name] = value
        elif len(values) > 1:
            output["uncertainties"].append({"code": "conflicting_%s" % field_name, "field": field_name, "severity": "high"})
    output["evidence"] = evidence
    output["metadata"].update(
        {
            "input_sha256": input_sha256,
            "model_version": model_version,
            "prompt_version": prompt_version,
            "ruleset_version": ruleset_version,
            "fallback": True,
        }
    )
    # No extractor relation is promoted to a cross-bundle edge here.  This is
    # intentional: candidate relations require a pairwise evidence decision.
    return output


class BundleSemanticEncoder:
    """Encode one in-memory message bundle with model/cache/fallback lanes."""

    def __init__(
        self,
        model: Optional[Any] = None,
        *,
        cache: Optional[VersionedBundleCache] = None,
        schema_version: str = BUNDLE_SCHEMA_VERSION,
        model_version: str = UNKNOWN,
        prompt_version: str = BUNDLE_PROMPT_VERSION,
        ruleset_version: str = BUNDLE_RULESET_VERSION,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.model = model
        self.cache = cache if cache is not None else VersionedBundleCache()
        self.schema_version = _string(schema_version, BUNDLE_SCHEMA_VERSION)
        self.model_version = _string(model_version)
        self.prompt_version = _string(prompt_version, BUNDLE_PROMPT_VERSION)
        self.ruleset_version = _string(ruleset_version, BUNDLE_RULESET_VERSION)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.sleep_fn = sleep_fn or (lambda seconds: time.sleep(seconds) if seconds > 0 else None)
        self.clock = clock or time.perf_counter
        self.stats = PipelineStats()

    def _cache_key(self, input_sha256: str) -> str:
        return self.cache.key(
            input_sha256,
            schema_version=self.schema_version,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            ruleset_version=self.ruleset_version,
        )

    def _outcome(
        self,
        bundle: Mapping[str, Any],
        *,
        status: str,
        input_sha256: str,
        cache_key: str,
    ) -> EncodingOutcome:
        validation = validate_bundle(bundle, expected_schema_version=self.schema_version)
        if status == "pending":
            self.stats.pending_count += 1
        return EncodingOutcome(deepcopy(dict(bundle)), status, input_sha256, cache_key, validation, self.stats.snapshot())

    def encode(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        bundle_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> EncodingOutcome:
        safe_messages = _safe_messages(messages)
        ids = tuple(item["message_id"] for item in safe_messages)
        resolved_bundle_id = _string(bundle_id, "bundle:%s" % stable_hash(ids)[:20])
        resolved_chat_id = _string(chat_id or safe_messages[0].get("chat_id"), UNKNOWN)
        request = {
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "ruleset_version": self.ruleset_version,
            "bundle_id": resolved_bundle_id,
            "chat_id": resolved_chat_id,
            "messages": list(safe_messages),
            "response_schema": bundle_schema(self.schema_version),
        }
        # Wire adapters may derive a request-local symbol table.  Include only
        # its fixed versions/hash in the cache identity; the adapter still
        # owns the long-id-to-handle mapping and the provider never sees this
        # context object as authoritative metadata.
        context_method = getattr(self.model, "cache_context", None) if self.model is not None else None
        if callable(context_method):
            context = context_method(request)
            if isinstance(context, Mapping):
                request["model_context"] = _jsonable(context)
        input_sha256 = stable_hash(request)
        cache_key = self._cache_key(input_sha256)
        cached = self.cache.get(cache_key)
        if cached is not None:
            self.stats.cache_hits += 1
            cached_bundle = normalize_bundle(
                cached,
                bundle_id=resolved_bundle_id,
                message_ids=ids,
                chat_id=resolved_chat_id,
                status="complete",
                source="cache",
                schema_version=self.schema_version,
            )
            cached_bundle["metadata"]["input_sha256"] = input_sha256
            return self._outcome(cached_bundle, status="complete", input_sha256=input_sha256, cache_key=cache_key)
        self.stats.cache_misses += 1
        if self.model is None:
            self.stats.fallback_calls += 1
            try:
                fallback = _fallback_bundle(
                    safe_messages,
                    bundle_id=resolved_bundle_id,
                    chat_id=resolved_chat_id,
                    input_sha256=input_sha256,
                    schema_version=self.schema_version,
                    model_version=self.model_version,
                    prompt_version=self.prompt_version,
                    ruleset_version=self.ruleset_version,
                )
                validation = validate_bundle(
                    fallback,
                    message_ids=ids,
                    chat_id=resolved_chat_id,
                    expected_schema_version=self.schema_version,
                )
                if validation.ok:
                    return self._outcome(fallback, status="fallback", input_sha256=input_sha256, cache_key=cache_key)
                # The fallback is deliberately strict: an invalid contract is
                # safer as pending/unknown than as a partially grounded answer.
                self.stats.fallback_failures += 1
            except Exception:
                self.stats.fallback_failures += 1
            pending = empty_bundle(
                resolved_bundle_id,
                ids,
                chat_id=resolved_chat_id,
                status="pending",
                source="fallback_failed",
                uncertainties=({"code": "fallback_failed", "field": UNKNOWN, "severity": "high"},),
                schema_version=self.schema_version,
            )
            pending["metadata"]["input_sha256"] = input_sha256
            return self._outcome(pending, status="pending", input_sha256=input_sha256, cache_key=cache_key)
        try:
            method = _model_method(self.model, "encode_bundle")
        except Exception:
            self.stats.model_failures += 1
            pending = empty_bundle(
                resolved_bundle_id,
                ids,
                chat_id=resolved_chat_id,
                status="pending",
                source="model_interface_missing",
                uncertainties=({"code": "model_interface_missing", "field": UNKNOWN, "severity": "high"},),
                schema_version=self.schema_version,
            )
            pending["metadata"].update(
                {
                    "input_sha256": input_sha256,
                    "model_version": self.model_version,
                    "prompt_version": self.prompt_version,
                    "ruleset_version": self.ruleset_version,
                }
            )
            return self._outcome(pending, status="pending", input_sha256=input_sha256, cache_key=cache_key)
        for attempt in range(self.max_retries + 1):
            started = self.clock()
            self.stats.model_calls += 1
            try:
                raw = method(request)
                input_tokens, output_tokens = _usage_tokens(raw)
                self.stats.tokens_in += input_tokens or _estimate_tokens(request)
                self.stats.tokens_out += output_tokens or _estimate_tokens(raw)
                normalized = normalize_bundle(
                    raw,
                    bundle_id=resolved_bundle_id,
                    message_ids=ids,
                    chat_id=resolved_chat_id,
                    status="complete",
                    source="model",
                    schema_version=self.schema_version,
                )
                model_context = request.get("model_context")
                if isinstance(model_context, Mapping):
                    normalized["metadata"].update(
                        {
                            str(key): _jsonable(value)
                            for key, value in model_context.items()
                            if str(key)
                            in {
                                "wire_schema_version",
                                "wire_prompt_version",
                                "canonical_schema_version",
                                "symbol_table_sha256",
                                "message_handle_count",
                                "evidence_handle_count",
                                "scope",
                                "account_id",
                            }
                        }
                    )
                validation = validate_bundle(
                    normalized,
                    message_ids=ids,
                    chat_id=resolved_chat_id,
                    expected_schema_version=self.schema_version,
                )
                if not validation.ok:
                    self.stats.model_validation_failures += 1
                    raise BundleSchemaError("model response failed bundle validation")
                self.stats.model_successes += 1
                self.stats.latency_ms_total += (self.clock() - started) * 1000.0
                normalized["metadata"].update(
                    {
                        "input_sha256": input_sha256,
                        "model_version": self.model_version,
                        "prompt_version": self.prompt_version,
                        "ruleset_version": self.ruleset_version,
                    }
                )
                self.cache.put(cache_key, normalized)
                return self._outcome(normalized, status="complete", input_sha256=input_sha256, cache_key=cache_key)
            except Exception:
                self.stats.model_failures += 1
                self.stats.latency_ms_total += (self.clock() - started) * 1000.0
                if attempt < self.max_retries:
                    self.stats.model_retries += 1
                    self.sleep_fn(self.retry_backoff_seconds * (attempt + 1))
        pending = empty_bundle(
            resolved_bundle_id,
            ids,
            chat_id=resolved_chat_id,
            status="pending",
            source="model_failed",
            uncertainties=({"code": "model_failed", "field": UNKNOWN, "severity": "high"},),
            schema_version=self.schema_version,
        )
        pending["metadata"].update(
            {
                "input_sha256": input_sha256,
                "model_version": self.model_version,
                "prompt_version": self.prompt_version,
                "ruleset_version": self.ruleset_version,
            }
        )
        return self._outcome(pending, status="pending", input_sha256=input_sha256, cache_key=cache_key)

    def encode_bundle(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        bundle_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> EncodingOutcome:
        """Explicit interface alias used by injected-pipeline callers."""

        return self.encode(messages, bundle_id=bundle_id, chat_id=chat_id)


# Short aliases make the boundary convenient without exposing provider details.
BundleEncoder = BundleSemanticEncoder


@dataclass(frozen=True)
class RetrievedCandidate:
    """A candidate ranked from sparse/structural evidence plus optional recall."""

    bundle_id: str
    score: float
    sparse_score: float
    structural_score: float
    dense_score: float = 0.0
    sources: Tuple[str, ...] = ()
    dense_only: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "score": round(float(self.score), 6),
            "sparse_score": round(float(self.sparse_score), 6),
            "structural_score": round(float(self.structural_score), 6),
            "dense_score": round(float(self.dense_score), 6),
            "sources": list(self.sources),
            "dense_only": self.dense_only,
        }


@dataclass(frozen=True)
class PairwiseJudgement:
    """Evidence-gated pairwise relation; it is not an event merge."""

    left_bundle_id: str
    right_bundle_id: str
    label: str = "insufficient"
    strength: str = "none"
    evidence_ids: Tuple[str, ...] = ()
    status: str = "pending"
    uncertainties: Tuple[str, ...] = ()
    source: str = UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "left_bundle_id": self.left_bundle_id,
            "right_bundle_id": self.right_bundle_id,
            "label": self.label,
            "strength": self.strength,
            "evidence_ids": list(self.evidence_ids),
            "status": self.status,
            "uncertainties": list(self.uncertainties),
            "source": self.source,
        }


def _tokenize(value: Any) -> Set[str]:
    """Tokenize only controlled semantic slots, never free-form body text."""

    if value is None:
        return set()
    text = str(value).casefold()
    return {item for item in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text) if item}


def _semantic_terms(bundle: Mapping[str, Any]) -> Set[str]:
    terms: Set[str] = set()
    # ``keywords`` is optional caller-provided structure, not an invitation to
    # index a message body.  Controlled labels and IDs remain inspectable.
    for field_name in ("claim_type", "state", "modality"):
        terms.update(_tokenize(bundle.get(field_name)))
    for field_name in ("speaker", "subject", "mentioned_person", "target", "object", "action"):
        values = bundle.get(field_name)
        values = values if isinstance(values, (list, tuple)) else [values]
        for value in values:
            if isinstance(value, Mapping):
                for key in ("id", "type", "role", "label", "action"):
                    terms.update(_tokenize(value.get(key)))
    metadata = bundle.get("metadata")
    if isinstance(metadata, Mapping):
        keywords = metadata.get("keywords") or metadata.get("terms") or ()
        if isinstance(keywords, (list, tuple, set)):
            for keyword in keywords:
                terms.update(_tokenize(keyword))
    return terms - _INDEX_STOPWORDS


def _bundle_chat_id(bundle: Mapping[str, Any]) -> str:
    metadata = bundle.get("metadata")
    return _string(metadata.get("chat_id")) if isinstance(metadata, Mapping) else UNKNOWN


def _bundle_segment_id(bundle: Mapping[str, Any]) -> str:
    metadata = bundle.get("metadata")
    return _string(metadata.get("segment_id")) if isinstance(metadata, Mapping) else UNKNOWN


def _slot_ids(bundle: Mapping[str, Any], field_name: str) -> Set[str]:
    value = bundle.get(field_name)
    values = value if isinstance(value, (list, tuple)) else [value]
    output: Set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            item_id = _string(item.get("id"))
            if _known(item_id):
                output.add(item_id)
    return output


def _structural_keys(bundle: Mapping[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    chat_id = _bundle_chat_id(bundle)
    segment_id = _bundle_segment_id(bundle)
    if _known(chat_id):
        keys.add("chat:" + chat_id)
    if _known(segment_id):
        keys.add("segment:" + segment_id)
    for field_name in ("speaker", "subject", "mentioned_person", "target", "object"):
        for item_id in _slot_ids(bundle, field_name):
            keys.add(field_name + ":" + item_id)
    for field_name in ("state", "claim_type"):
        value = _string(bundle.get(field_name))
        if _known(value):
            keys.add(field_name + ":" + value)
    return keys


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


class SparseStructuralIndex:
    """In-memory sparse and structural candidate index.

    The index accepts already-encoded bundles.  It never opens input paths or
    asks a provider for embeddings unless an embedder was explicitly supplied.
    """

    def __init__(
        self,
        bundles: Iterable[Mapping[str, Any]] = (),
        *,
        embedder: Optional[Any] = None,
        stats: Optional[PipelineStats] = None,
    ) -> None:
        self.embedder = embedder
        self.stats = stats
        self._bundles: Dict[str, Dict[str, Any]] = {}
        self._sparse: Dict[str, Set[str]] = defaultdict(set)
        self._structural: Dict[str, Set[str]] = defaultdict(set)
        self._vectors: Dict[str, Tuple[float, ...]] = {}
        self.embedder_calls = 0
        self.embedder_failures = 0
        for bundle in bundles:
            self.add(bundle)

    def add(self, bundle: Mapping[str, Any]) -> str:
        report = validate_bundle(bundle)
        if not report.ok:
            raise BundleSchemaError("cannot index invalid bundle: %s" % ",".join(report.errors))
        bundle_id = _string(bundle.get("bundle_id"))
        self.remove(bundle_id)
        copied = deepcopy(dict(bundle))
        self._bundles[bundle_id] = copied
        for token in _semantic_terms(copied):
            self._sparse[token].add(bundle_id)
        for key in _structural_keys(copied):
            self._structural[key].add(bundle_id)
        if self.embedder is not None:
            try:
                self._vectors[bundle_id] = tuple(float(item) for item in self._embed(copied))
            except Exception:
                self.embedder_failures += 1
                if self.stats is not None:
                    self.stats.embedder_failures += 1
        return bundle_id

    def remove(self, bundle_id: str) -> None:
        bundle_id = _string(bundle_id)
        old = self._bundles.pop(bundle_id, None)
        if old is None:
            return
        for token in _semantic_terms(old):
            self._sparse[token].discard(bundle_id)
        for key in _structural_keys(old):
            self._structural[key].discard(bundle_id)
        self._vectors.pop(bundle_id, None)

    def get(self, bundle_id: str) -> Optional[Mapping[str, Any]]:
        value = self._bundles.get(_string(bundle_id))
        return deepcopy(value) if value is not None else None

    def __len__(self) -> int:
        return len(self._bundles)

    def _embed(self, value: Mapping[str, Any]) -> Sequence[float]:
        if self.embedder is None:
            return ()
        method = getattr(self.embedder, "embed", None)
        if not callable(method) and callable(self.embedder):
            method = self.embedder
        if not callable(method):
            raise BundleSemanticError("embedder does not implement embed")
        self.embedder_calls += 1
        if self.stats is not None:
            self.stats.embedder_calls += 1
        return method(value)

    def _query_vector(self, query: Mapping[str, Any]) -> Optional[Tuple[float, ...]]:
        if self.embedder is None:
            return None
        try:
            return tuple(float(item) for item in self._embed(query))
        except Exception:
            self.embedder_failures += 1
            if self.stats is not None:
                self.stats.embedder_failures += 1
            return None

    def retrieve(
        self,
        query: Mapping[str, Any],
        *,
        top_k: int = 10,
        exclude_bundle_ids: Iterable[Any] = (),
        allow_cross_chat: bool = False,
    ) -> Tuple[RetrievedCandidate, ...]:
        if not isinstance(query, Mapping):
            raise BundleSemanticError("query must be a bundle mapping")
        limit = max(1, int(top_k))
        excluded = {_string(item) for item in exclude_bundle_ids}
        query_terms = _semantic_terms(query)
        query_structural = _structural_keys(query)
        sparse_ids: Set[str] = set()
        for token in query_terms:
            sparse_ids.update(self._sparse.get(token, ()))
        structural_ids: Set[str] = set()
        for key in query_structural:
            structural_ids.update(self._structural.get(key, ()))
        candidate_ids = (sparse_ids | structural_ids) - excluded
        query_chat = _bundle_chat_id(query)
        if not allow_cross_chat and _known(query_chat):
            candidate_ids = {
                bundle_id
                for bundle_id in candidate_ids
                if not _known(_bundle_chat_id(self._bundles[bundle_id])) or _bundle_chat_id(self._bundles[bundle_id]) == query_chat
            }
        dense_scores: Dict[str, float] = {}
        query_vector = self._query_vector(query)
        if query_vector is not None:
            for bundle_id, vector in self._vectors.items():
                if bundle_id in excluded:
                    continue
                if not allow_cross_chat and _known(query_chat) and _known(_bundle_chat_id(self._bundles[bundle_id])) and _bundle_chat_id(self._bundles[bundle_id]) != query_chat:
                    continue
                dense_scores[bundle_id] = _cosine(query_vector, vector)
            # Dense is only a recall lane.  It can add candidates, but its raw
            # score is never included in the primary sparse/structural score.
            dense_ids = sorted(dense_scores, key=lambda item: dense_scores[item], reverse=True)[: max(limit * 3, 10)]
            candidate_ids.update(dense_ids)
        results: List[RetrievedCandidate] = []
        for bundle_id in candidate_ids:
            bundle = self._bundles.get(bundle_id)
            if bundle is None:
                continue
            overlap = len(query_terms & _semantic_terms(bundle))
            sparse_score = overlap / max(1, len(query_terms)) if query_terms else 0.0
            structural_overlap = len(query_structural & _structural_keys(bundle))
            structural_score = float(structural_overlap)
            dense_score = max(0.0, dense_scores.get(bundle_id, 0.0))
            sources: List[str] = []
            if bundle_id in sparse_ids:
                sources.append("sparse")
            if bundle_id in structural_ids:
                sources.append("structural")
            if bundle_id in dense_scores:
                sources.append("dense_recall")
            dense_only = not (bundle_id in sparse_ids or bundle_id in structural_ids)
            # Primary order is structural + sparse; dense only breaks ties.
            primary = structural_score + sparse_score
            results.append(
                RetrievedCandidate(
                    bundle_id=bundle_id,
                    score=primary,
                    sparse_score=sparse_score,
                    structural_score=structural_score,
                    dense_score=dense_score,
                    sources=tuple(sources),
                    dense_only=dense_only,
                )
            )
        results.sort(
            key=lambda item: (
                item.dense_only,
                -item.structural_score,
                -item.sparse_score,
                -item.dense_score,
                item.bundle_id,
            )
        )
        return tuple(results[:limit])


BundleIndex = SparseStructuralIndex


def _public_bundle_for_judgement(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a deep-copied contract view with no body/free-form fields."""

    normalized = normalize_bundle(bundle, schema_version=_string(bundle.get("schema_version"), BUNDLE_SCHEMA_VERSION))
    # Evidence references are required for judgement, while metadata is kept
    # to controlled routing/version fields only.
    report = validate_bundle(normalized, expected_schema_version=normalized["schema_version"])
    if not report.ok:
        raise BundleSchemaError("invalid bundle for pairwise judgement")
    return normalized


def _normalize_pairwise(value: Any, *, left_id: str, right_id: str) -> PairwiseJudgement:
    raw = value.get("judgement") if isinstance(value, Mapping) and isinstance(value.get("judgement"), Mapping) else value
    if not isinstance(raw, Mapping):
        raise BundleSchemaError("pairwise response is not an object")
    label = _string(raw.get("label") or raw.get("relation"), "insufficient")
    strength = _string(raw.get("strength") or raw.get("evidence_strength"), "none")
    evidence_ids = raw.get("evidence_ids") or ()
    if not isinstance(evidence_ids, (list, tuple)):
        evidence_ids = ()
    status = _string(raw.get("status"), "complete")
    if status not in {"complete", "pending"}:
        status = "pending"
    uncertainties = raw.get("uncertainties") or ()
    if not isinstance(uncertainties, (list, tuple)):
        uncertainties = ()
    return PairwiseJudgement(
        left_bundle_id=left_id,
        right_bundle_id=right_id,
        label=label,
        strength=strength,
        evidence_ids=_unique_strings(evidence_ids),
        status=status,
        uncertainties=_unique_strings(uncertainties),
        source=_string(raw.get("source"), "model"),
    )


def validate_pairwise_judgement(
    judgement: PairwiseJudgement,
    *,
    evidence_ids: Iterable[Any] = (),
    left_chat_id: Optional[str] = None,
    right_chat_id: Optional[str] = None,
    allow_cross_chat: bool = False,
) -> ValidationReport:
    errors: List[str] = []
    warnings: List[str] = []
    if judgement.label not in RELATION_LABELS:
        errors.append("invalid_relation_label")
    if judgement.strength not in RELATION_STRENGTHS:
        errors.append("invalid_relation_strength")
    if not _known(judgement.left_bundle_id) or not _known(judgement.right_bundle_id):
        errors.append("missing_relation_endpoint")
    if judgement.left_bundle_id == judgement.right_bundle_id:
        errors.append("self_relation")
    known_evidence = {_string(item) for item in evidence_ids if _known(item)}
    if set(judgement.evidence_ids) - known_evidence:
        errors.append("unknown_evidence")
    if judgement.status == "complete" and judgement.label != "insufficient" and not judgement.evidence_ids:
        errors.append("missing_evidence")
    if not allow_cross_chat and left_chat_id is not None and right_chat_id is not None and _known(left_chat_id) and _known(right_chat_id) and left_chat_id != right_chat_id:
        errors.append("cross_chat_link_forbidden")
    return ValidationReport(not errors, tuple(sorted(set(errors))), tuple(sorted(set(warnings))), {"evidence_count": len(known_evidence)}, stable_hash(judgement.to_dict()))


class BundleSemanticPipeline:
    """Convenience facade combining encoding, retrieval, and pairwise judgement."""

    def __init__(
        self,
        model: Optional[Any] = None,
        *,
        embedder: Optional[Any] = None,
        cache: Optional[VersionedBundleCache] = None,
        schema_version: str = BUNDLE_SCHEMA_VERSION,
        model_version: str = UNKNOWN,
        prompt_version: str = BUNDLE_PROMPT_VERSION,
        ruleset_version: str = BUNDLE_RULESET_VERSION,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.encoder = BundleSemanticEncoder(
            model,
            cache=cache,
            schema_version=schema_version,
            model_version=model_version,
            prompt_version=prompt_version,
            ruleset_version=ruleset_version,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            sleep_fn=sleep_fn,
            clock=clock,
        )
        self.index = SparseStructuralIndex(embedder=embedder, stats=self.encoder.stats)

    @property
    def stats(self) -> PipelineStats:
        return self.encoder.stats

    def encode(self, messages: Iterable[Mapping[str, Any]], **kwargs: Any) -> EncodingOutcome:
        outcome = self.encoder.encode(messages, **kwargs)
        if outcome.status in {"complete", "fallback"} and outcome.validation.ok:
            self.index.add(outcome.bundle)
        return outcome

    def encode_bundle(self, messages: Iterable[Mapping[str, Any]], **kwargs: Any) -> EncodingOutcome:
        return self.encode(messages, **kwargs)

    def retrieve(self, query: Mapping[str, Any], **kwargs: Any) -> Tuple[RetrievedCandidate, ...]:
        return self.index.retrieve(query, **kwargs)

    def judge_pair(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> Tuple[PairwiseJudgement, ValidationReport]:
        left_bundle = _public_bundle_for_judgement(left)
        right_bundle = _public_bundle_for_judgement(right)
        left_id = _string(left_bundle.get("bundle_id"))
        right_id = _string(right_bundle.get("bundle_id"))
        if left_id == right_id:
            raise BundleSemanticError("pairwise judgement requires two bundles")
        request = {
            "schema_version": self.encoder.schema_version,
            "prompt_version": self.encoder.prompt_version,
            "left_bundle": left_bundle,
            "right_bundle": right_bundle,
            "response_schema": {
                "type": "object",
                "required": ["label", "strength", "evidence_ids", "status", "uncertainties"],
            },
        }
        known_evidence = tuple(
            _string(item.get("evidence_id"))
            for bundle in (left_bundle, right_bundle)
            for item in bundle.get("evidence", ())
            if isinstance(item, Mapping) and _known(item.get("evidence_id"))
        )
        if self.encoder.model is None:
            self.encoder.stats.pairwise_calls += 1
            self.encoder.stats.pairwise_failures += 1
            judgement = PairwiseJudgement(left_id, right_id, uncertainties=("model_unavailable",), source="unknown")
            report = validate_pairwise_judgement(
                judgement,
                evidence_ids=known_evidence,
                left_chat_id=_bundle_chat_id(left_bundle),
                right_chat_id=_bundle_chat_id(right_bundle),
            )
            return judgement, report
        try:
            method = _model_method(self.encoder.model, "judge_pair")
        except Exception:
            self.encoder.stats.pairwise_calls += 1
            self.encoder.stats.pairwise_failures += 1
            pending = PairwiseJudgement(
                left_id,
                right_id,
                label="insufficient",
                strength="none",
                status="pending",
                uncertainties=("pairwise_interface_missing",),
                source="model_interface_missing",
            )
            report = validate_pairwise_judgement(
                pending,
                evidence_ids=known_evidence,
                left_chat_id=_bundle_chat_id(left_bundle),
                right_chat_id=_bundle_chat_id(right_bundle),
            )
            return pending, report
        pair_input_sha256 = stable_hash(request)
        pair_cache_key = self.encoder.cache.key(
            pair_input_sha256,
            schema_version=self.encoder.schema_version,
            model_version=self.encoder.model_version,
            prompt_version=self.encoder.prompt_version + ":pairwise",
            ruleset_version=self.encoder.ruleset_version,
        )
        cached = self.encoder.cache.get(pair_cache_key)
        if cached is not None:
            self.encoder.stats.cache_hits += 1
            try:
                cached_judgement = _normalize_pairwise(cached, left_id=left_id, right_id=right_id)
                cached_report = validate_pairwise_judgement(
                    cached_judgement,
                    evidence_ids=known_evidence,
                    left_chat_id=_bundle_chat_id(left_bundle),
                    right_chat_id=_bundle_chat_id(right_bundle),
                )
                if cached_report.ok:
                    return cached_judgement, cached_report
            except Exception:
                # A stale or manually corrupted cache entry is simply ignored;
                # the next model attempt will replace it.
                pass
        else:
            self.encoder.stats.cache_misses += 1
        last: Optional[PairwiseJudgement] = None
        for attempt in range(self.encoder.max_retries + 1):
            started = self.encoder.clock()
            self.encoder.stats.pairwise_calls += 1
            try:
                raw = method(request)
                input_tokens, output_tokens = _usage_tokens(raw)
                self.encoder.stats.tokens_in += input_tokens or _estimate_tokens(request)
                self.encoder.stats.tokens_out += output_tokens or _estimate_tokens(raw)
                judgement = _normalize_pairwise(raw, left_id=left_id, right_id=right_id)
                report = validate_pairwise_judgement(
                    judgement,
                    evidence_ids=known_evidence,
                    left_chat_id=_bundle_chat_id(left_bundle),
                    right_chat_id=_bundle_chat_id(right_bundle),
                )
                if report.ok:
                    self.encoder.stats.latency_ms_total += (self.encoder.clock() - started) * 1000.0
                    self.encoder.stats.pairwise_successes += 1
                    self.encoder.cache.put(pair_cache_key, judgement.to_dict())
                    return judgement, report
                last = judgement
                raise BundleSchemaError("pairwise response failed validation")
            except Exception:
                self.encoder.stats.pairwise_failures += 1
                self.encoder.stats.latency_ms_total += (self.encoder.clock() - started) * 1000.0
                if attempt < self.encoder.max_retries:
                    self.encoder.stats.model_retries += 1
                    self.encoder.sleep_fn(self.encoder.retry_backoff_seconds * (attempt + 1))
        pending = PairwiseJudgement(
            left_id,
            right_id,
            label="insufficient",
            strength="none",
            status="pending",
            uncertainties=("pairwise_failed",),
            source="model_failed",
        )
        report = validate_pairwise_judgement(
            pending,
            evidence_ids=known_evidence,
            left_chat_id=_bundle_chat_id(left_bundle),
            right_chat_id=_bundle_chat_id(right_bundle),
        )
        return pending, report


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BUNDLE_PROMPT_VERSION",
    "BUNDLE_RULESET_VERSION",
    "BUNDLE_FIELDS",
    "ENTITY_RESOLUTIONS",
    "ENTITY_TYPES",
    "CLAIM_TYPES",
    "STATES",
    "MODALITIES",
    "RELATION_LABELS",
    "RELATION_STRENGTHS",
    "RUN_STATUSES",
    "BundleSemanticError",
    "BundleSchemaError",
    "BundleModel",
    "BundleEmbedder",
    "PairwiseModel",
    "canonical_json",
    "stable_hash",
    "bundle_schema",
    "empty_bundle",
    "normalize_bundle",
    "validate_bundle",
    "ValidationReport",
    "PipelineStats",
    "EncodingOutcome",
    "VersionedBundleCache",
    "BundleSemanticEncoder",
    "BundleEncoder",
    "RetrievedCandidate",
    "PairwiseJudgement",
    "validate_pairwise_judgement",
    "SparseStructuralIndex",
    "BundleIndex",
    "BundleSemanticPipeline",
]
