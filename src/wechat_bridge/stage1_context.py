"""Development-only Stage 1 semantic projection and scorer.

This module is deliberately independent from the production semantic pipeline.
It projects only the people/argument/state/fragment/context portion of the
authoritative gold contract and scores a prediction against that projection.
It does not read a database, open a frozen split, build events, generate
titles, or touch the web layer.

The projection is an in-memory/private object for evaluation.  The aggregate
summary returned by :func:`score_stage1` contains counts, finite labels, and
metric values only; it never copies message, claim, mention, surface, title,
or uncertainty text.  A missing field is represented as ``"N/A"`` rather
than as a zero score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union


STAGE1_SCHEMA_VERSION = "stage1_context_projection_v1"
STAGE1_SCOPE = "development"
NA = "N/A"

# These values intentionally mirror docs/gold-standard-2026-08-25-contract.md.
FRAGMENT_TYPES = frozenset(
    {
        "conversation_opener",
        "statement",
        "question",
        "request",
        "answer",
        "acknowledgement",
        "reaction",
        "context",
        "media",
        "unknown",
    }
)
PERSON_ROLES = frozenset({"speaker", "mentioned_person", "subject"})
OBJECT_RESOLUTIONS = frozenset({"explicit", "inherited", "unknown"})
STATE_VALUES = frozenset({"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"})
MODALITY_VALUES = frozenset({"certain", "probable", "possible", "required", "desired", "unknown"})
CLAIM_TYPES = frozenset({"fact", "opinion", "question", "suggestion", "hypothesis"})
CONTEXT_RELATION_LABELS = frozenset(
    {
        "continues",
        "elaborates",
        "answers",
        "contrasts",
        "topic_shift",
        "possibly_related",
        "insufficient",
    }
)
EVIDENCE_STRENGTH_VALUES = frozenset({"strong", "medium", "weak", "none"})
TIME_EVIDENCE_VALUES = frozenset({"strong", "weak", "none"})
ATTRIBUTION_VALUES = frozenset({"direct", "reply", "quote", "forwarded", "inferred_context"})
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})

COLLECTIONS = (
    "messages",
    "fragments",
    "persons",
    "arguments",
    "mentions",
    "claims",
    "discourse_threads",
    "context_relations",
)

# Body-bearing fields are intentionally absent.  The projection is structural
# and may be used in aggregate reports without copying redacted正文.
_SAFE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "messages": (
        "message_id", "account_id", "chat_id", "chat_type", "speaker_id", "direction",
        "message_type", "local_day", "time_offset_seconds", "time_bucket",
        "sequence_in_chat", "reply_to_message_id", "redaction_types", "media_state",
        "source_mode", "context_message_ids", "split",
    ),
    "fragments": (
        "fragment_id", "prediction_fragment_id", "message_id", "span_start", "span_end", "fragment_type",
        "speaker_id", "mentioned_person_ids", "subject_id", "subject_type", "object_id",
        "object_resolution", "object_evidence_refs", "object_inherited_from_id", "state",
        "state_evidence", "closure_reason", "temporal_qualifier", "start_time_offset_seconds",
        "start_time_source", "end_time_offset_seconds", "end_time_source", "information_value",
        "event_completeness", "claim_ids", "evidence_refs", "context_message_ids",
        "uncertainties",
    ),
    "persons": (
        "person_ref_id", "person_id", "resolution", "role", "message_id", "fragment_id",
        "claim_id", "span_start", "span_end", "source", "evidence_refs", "confidence",
    ),
    "arguments": (
        "argument_id", "fragment_id", "claim_id", "role", "entity_id", "entity_type",
        "resolution", "evidence_refs", "inherited_from_id", "confidence",
    ),
    "mentions": (
        "mention_id", "message_id", "fragment_id", "mention_type", "span_start", "span_end",
        "normalized_id", "normalized_type", "entity_role", "attributes", "certainty",
    ),
    "claims": (
        "claim_id", "prediction_claim_id", "message_id", "fragment_id", "speaker_id", "mentioned_person_ids",
        "subject_id", "subject_type", "object_id", "object_resolution", "object_evidence_refs",
        "object_inherited_from_id", "claim_type", "target_entity_ids", "event_mention_ids",
        "evidence_spans", "stance", "polarity", "modality", "status", "state",
        "state_evidence", "closure_reason", "temporal_qualifier", "start_time_offset_seconds",
        "start_time_source", "end_time_offset_seconds", "end_time_source", "information_value",
        "event_completeness", "attribution", "timestamp_message_id", "context_message_ids",
    ),
    "discourse_threads": (
        "discourse_thread_id", "fragment_ids", "claim_ids", "conversation_opener_fragment_ids",
        "speaker_ids", "mentioned_person_ids", "subject_ids", "object_refs", "state_sequence",
        "closure_reason", "start_fragment_id", "end_fragment_id", "start_time_offset_seconds",
        "start_time_source", "end_time_offset_seconds", "end_time_source", "information_value",
        "event_completeness", "event_candidate", "context_relation_ids", "evidence_refs",
        "uncertainties",
    ),
    "context_relations": (
        "context_relation_id", "prediction_context_relation_id", "left_anchor_id", "right_anchor_id", "anchor_type", "label",
        "supporting_slot_codes", "conflicting_slot_codes", "evidence_refs",
        "evidence_message_ids", "explicit_reply_present", "time_distance_seconds", "time_evidence",
        "evidence_strength", "confidence", "annotator_a_label", "annotator_b_label",
        "adjudication_id", "provenance",
    ),
}

_BODY_FIELDS = frozenset(
    {
        "redacted_text", "fragment_text_redacted", "surface_redacted", "claim_text_redacted",
        "title_redacted", "summary_of_boundary_redacted", "text_redacted", "raw_text", "content",
        "raw_message", "sentence_units", "annotator_notes", "uncertainties", "provenance",
    }
)


@dataclass(frozen=True)
class Stage1Validation:
    """Structure-only validation result for a Stage 1 projection."""

    ok: bool
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass(frozen=True)
class Stage1Projection:
    """An in-memory, development-scoped structural projection.

    IDs remain available to the evaluator inside this object.  Callers should
    use :meth:`aggregate` or ``score_stage1`` for reports; those methods omit
    all record-level values.
    """

    records: Mapping[str, Tuple[Mapping[str, Any], ...]]
    scope: str = STAGE1_SCOPE
    source_kind: str = "gold"
    validation: Stage1Validation = Stage1Validation(True)

    def collection(self, name: str) -> Tuple[Mapping[str, Any], ...]:
        return tuple(self.records.get(name, ()))

    def to_dict(self, *, include_records: bool = True) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "schema_version": STAGE1_SCHEMA_VERSION,
            "scope": self.scope,
            "source_kind": self.source_kind,
            "validation": self.validation.to_dict(),
        }
        if include_records:
            result["records"] = {
                name: [dict(item) for item in self.collection(name)] for name in COLLECTIONS
            }
        return result

    def aggregate(self) -> Dict[str, Any]:
        return summarize_stage1_projection(self)


def _records(source: Union[Stage1Projection, Mapping[str, Any]], name: str) -> Tuple[Mapping[str, Any], ...]:
    if isinstance(source, Stage1Projection):
        return source.collection(name)
    value = source.get(name, ())
    if not value and isinstance(source.get("records"), Mapping):
        value = source["records"].get(name, ())
    if isinstance(value, Mapping):
        # A common accidental shape is {id: record}; accept it without
        # exposing the map keys in any aggregate output.
        value = tuple(value.values())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _copy_safe_record(name: str, record: Mapping[str, Any]) -> Dict[str, Any]:
    result = {field: record[field] for field in _SAFE_FIELDS[name] if field in record}
    # The public contract calls this field ``fragment_type``.  Accept the
    # descriptive ``fragment_role`` alias from prediction adapters, but emit
    # only the canonical contract field.
    if name == "fragments" and "fragment_type" not in result and record.get("fragment_role") not in (None, ""):
        result["fragment_type"] = record["fragment_role"]
    # A body field is never copied even if a future contract accidentally adds
    # it to a whitelist.  This guard also keeps prediction adapters safe.
    for field in tuple(result):
        field_lower = field.lower()
        is_body_field = (
            field in _BODY_FIELDS
            or field_lower.endswith("_text")
            or field_lower.endswith("_content")
            or field_lower.endswith("_surface")
            or field_lower.endswith("_title")
        )
        if is_body_field:
            result.pop(field, None)
    return result


def _domain_id(name: str, record: Mapping[str, Any]) -> str:
    fields = {
        "messages": ("message_id",),
        "fragments": ("fragment_id",),
        "persons": ("person_ref_id",),
        "arguments": ("argument_id",),
        "mentions": ("mention_id",),
        "claims": ("claim_id", "prediction_claim_id"),
        "discourse_threads": ("discourse_thread_id", "thread_id"),
        "context_relations": ("context_relation_id", "prediction_context_relation_id"),
    }[name]
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return ""


def _duplicate_errors(name: str, rows: Sequence[Mapping[str, Any]]) -> List[str]:
    seen: Set[str] = set()
    errors: List[str] = []
    for row in rows:
        value = _domain_id(name, row)
        if not value:
            errors.append("%s record is missing its domain ID" % name)
        elif value in seen:
            errors.append("duplicate %s ID" % name[:-1])
        seen.add(value)
    return errors


def _as_id_set(rows: Sequence[Mapping[str, Any]], field: str) -> Set[str]:
    return {str(row[field]) for row in rows if row.get(field) not in (None, "")}


def _scope_errors(dataset: Mapping[str, Any]) -> List[str]:
    messages = _records(dataset, "messages")
    if not messages:
        return ["development projection requires messages"]
    errors: List[str] = []
    for row in messages:
        split = row.get("split")
        if split != STAGE1_SCOPE:
            errors.append("stage1 projection accepts development messages only")
            break
    for name in COLLECTIONS:
        for row in _records(dataset, name):
            split = row.get("split")
            if split is not None and split != STAGE1_SCOPE:
                errors.append("%s contains a non-development record" % name)
                break
    return errors


def _foreign_key_errors(dataset: Mapping[str, Any]) -> List[str]:
    messages = _records(dataset, "messages")
    fragments = _records(dataset, "fragments")
    claims = _records(dataset, "claims")
    message_ids = _as_id_set(messages, "message_id")
    fragment_ids = _as_id_set(fragments, "fragment_id")
    claim_ids = _as_id_set(claims, "claim_id") | {
        str(row["prediction_claim_id"]) for row in claims if row.get("prediction_claim_id") not in (None, "")
    }
    errors: List[str] = []

    for name in ("fragments", "persons", "mentions", "claims"):
        for row in _records(dataset, name):
            value = row.get("message_id")
            if value not in (None, "") and str(value) not in message_ids:
                errors.append("%s message_id references a non-development message" % name)
    for name in ("persons", "mentions", "arguments", "claims"):
        for row in _records(dataset, name):
            value = row.get("fragment_id")
            if value not in (None, "") and str(value) not in fragment_ids:
                errors.append("%s fragment_id references an unknown fragment" % name)
    for name in ("arguments",):
        for row in _records(dataset, name):
            value = row.get("claim_id")
            if value not in (None, "") and str(value) not in claim_ids:
                errors.append("%s claim_id references an unknown claim" % name)
    for row in _records(dataset, "context_relations"):
        anchor_type = row.get("anchor_type")
        if anchor_type not in {"fragment", "claim"}:
            errors.append("context relation has invalid anchor_type")
            continue
        valid_ids = fragment_ids if anchor_type == "fragment" else claim_ids
        for field in ("left_anchor_id", "right_anchor_id"):
            value = row.get(field)
            if value not in (None, "") and str(value) not in valid_ids:
                errors.append("context relation anchor references an unknown %s" % anchor_type)
    return errors


def _enum_errors(source: Union[Stage1Projection, Mapping[str, Any]]) -> List[str]:
    errors: List[str] = []
    enum_fields = {
        "fragments": {"fragment_type": FRAGMENT_TYPES, "object_resolution": OBJECT_RESOLUTIONS, "state": STATE_VALUES},
        "persons": {"role": PERSON_ROLES, "resolution": OBJECT_RESOLUTIONS},
        "arguments": {"resolution": OBJECT_RESOLUTIONS},
        "mentions": {"entity_role": PERSON_ROLES | {"subject", "object", "other", "unknown"},
                      "certainty": frozenset({"asserted", "reported", "hypothetical", "negated", "unknown"})},
        "claims": {"claim_type": CLAIM_TYPES, "object_resolution": OBJECT_RESOLUTIONS,
                    "modality": MODALITY_VALUES, "state": STATE_VALUES, "attribution": ATTRIBUTION_VALUES},
        "context_relations": {"label": CONTEXT_RELATION_LABELS,
                               "evidence_strength": EVIDENCE_STRENGTH_VALUES,
                               "time_evidence": TIME_EVIDENCE_VALUES},
    }
    for name, fields in enum_fields.items():
        for row in _records(source, name):
            for field, allowed in fields.items():
                value = row.get(field)
                if value not in (None, "") and value not in allowed:
                    errors.append("%s.%s has invalid value" % (name, field))
    return errors


def _semantic_invariant_errors(source: Union[Stage1Projection, Mapping[str, Any]]) -> List[str]:
    """Check only finite, structure-level Stage 1 invariants.

    Missing fields are deliberately not errors: development artifacts from a
    previous schema are valid inputs whose unsupported metrics must be N/A.
    """

    errors: List[str] = []
    context_rows = _records(source, "context_relations")
    for row in context_rows:
        label = row.get("label")
        if not row.get("evidence_refs"):
            errors.append("context relation lacks typed evidence")
        if "explicit_reply_present" not in row or not isinstance(row.get("explicit_reply_present"), bool):
            errors.append("context relation must declare explicit_reply_present")
        if "time_evidence" not in row:
            errors.append("context relation must declare time_evidence")
        if label == "insufficient" and row.get("evidence_strength") not in (None, "", "none"):
            errors.append("insufficient context relation must use none strength")
        if row.get("evidence_strength") == "none" and label not in (None, "", "insufficient"):
            errors.append("none context strength is reserved for insufficient")
        if label in {"continues", "elaborates", "answers"} and row.get("explicit_reply_present") is False:
            slots = [str(value) for value in row.get("supporting_slot_codes") or []]
            if len(set(slots)) < 2:
                errors.append("no-reply positive context relation needs two independent signals")
        if row.get("explicit_reply_present") is False and row.get("time_evidence") == "weak":
            slots = {str(value).lower() for value in row.get("supporting_slot_codes") or []}
            if slots and slots.issubset({"time", "time_near", "time_proximity", "temporal", "time_distance"}):
                if row.get("label") != "insufficient":
                    errors.append("time proximity alone cannot create a context relation")

    for name in ("fragments", "claims"):
        for row in _records(source, name):
            resolution = row.get("object_resolution")
            if resolution == "explicit" and not row.get("object_evidence_refs"):
                errors.append("%s explicit object lacks evidence" % name)
            if resolution == "inherited" and not row.get("object_inherited_from_id"):
                errors.append("%s inherited object lacks source ID" % name)
            state = row.get("state")
            if state in TERMINAL_STATES:
                if row.get("state_evidence") in (None, "", "unknown"):
                    errors.append("%s terminal state lacks state evidence" % name)
                if name == "fragments" and not row.get("evidence_refs"):
                    errors.append("fragments terminal state lacks evidence refs")
                if name == "claims" and not row.get("evidence_spans"):
                    errors.append("claims terminal state lacks evidence spans")
    return errors


def validate_stage1_projection(source: Union[Stage1Projection, Mapping[str, Any]], *, require_development: bool = True) -> Stage1Validation:
    """Validate finite Stage 1 structure without inspecting body text."""

    errors: List[str] = []
    if require_development:
        errors.extend(_scope_errors(source if isinstance(source, Mapping) else source.records))
    for name in COLLECTIONS:
        errors.extend(_duplicate_errors(name, _records(source, name)))
    if require_development:
        errors.extend(_foreign_key_errors(source if isinstance(source, Mapping) else source.records))
    errors.extend(_enum_errors(source))
    errors.extend(_semantic_invariant_errors(source))
    return Stage1Validation(not errors, tuple(sorted(set(errors))), ())


def project_stage1_gold(dataset: Mapping[str, Any]) -> Stage1Projection:
    """Build a body-free Stage 1 projection from development gold records.

    The function refuses a mixed or frozen split.  It does not infer missing
    fields and does not map legacy ``status`` values to the canonical Stage 1
    ``state`` field; the evaluator reports such fields as N/A.
    """

    scope_errors = _scope_errors(dataset)
    if scope_errors:
        raise ValueError("; ".join(sorted(set(scope_errors))))
    records = {
        name: tuple(_copy_safe_record(name, row) for row in _records(dataset, name))
        for name in COLLECTIONS
    }
    projection = Stage1Projection(records=records, scope=STAGE1_SCOPE, source_kind="gold")
    validation = validate_stage1_projection(projection, require_development=False)
    # Referential/split checks are performed before the body-free copy; they
    # operate only on structural IDs and finite labels.
    validation = Stage1Validation(
        validation.ok and not _foreign_key_errors(dataset),
        tuple(sorted(set(validation.errors + tuple(_foreign_key_errors(dataset))))),
        validation.warnings,
    )
    return Stage1Projection(records=records, scope=STAGE1_SCOPE, source_kind="gold", validation=validation)


def load_development_gold_directory(directory: Union[str, Path]) -> Stage1Projection:
    """Load only known Stage 1 JSONL files from a ``development`` directory.

    This is intentionally a narrow local adapter for the private working
    artifact.  The final path component must be ``development`` and any
    ancestor named ``frozen``/``frozen_test`` is rejected before opening a
    file.  Unknown files (including aggregate reports and prediction files)
    are ignored.  Body fields are parsed only long enough to project and are
    never returned by this module.
    """

    root = Path(directory)
    if root.name.casefold() != STAGE1_SCOPE:
        raise ValueError("Stage1 gold directory must end in development")
    if any(part.casefold() in {"frozen", "frozen_test"} for part in root.parts):
        raise ValueError("Stage1 loader refuses frozen directories")
    if not root.is_dir():
        raise ValueError("Stage1 gold directory does not exist")

    dataset: Dict[str, Any] = {name: [] for name in COLLECTIONS}
    for name in COLLECTIONS:
        path = root / (name + ".private.jsonl")
        if not path.is_file():
            continue
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid %s JSONL at line %d" % (name, line_number)) from exc
                if not isinstance(value, Mapping):
                    raise ValueError("%s JSONL line %d is not an object" % (name, line_number))
                rows.append(dict(value))
        dataset[name] = rows
    return project_stage1_gold(dataset)


def project_stage1_predictions(predictions: Mapping[str, Any]) -> Stage1Projection:
    """Normalize a prediction mapping for Stage 1 scoring.

    Prediction payloads may omit all Stage 1 collections or any individual
    field.  Such omissions are preserved as omissions so scoring can return
    N/A instead of silently treating them as negative labels.
    """

    records = {
        name: tuple(_copy_safe_record(name, row) for row in _records(predictions, name))
        for name in COLLECTIONS
    }
    validation = validate_stage1_projection(
        Stage1Projection(records=records, scope=STAGE1_SCOPE, source_kind="prediction"),
        require_development=False,
    )
    return Stage1Projection(records=records, scope=STAGE1_SCOPE, source_kind="prediction", validation=validation)


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _metric(value: Any, *, numerator: Optional[int] = None, denominator: Optional[int] = None,
            reason: Optional[str] = None, observed: bool = True) -> Dict[str, Any]:
    if not observed:
        return {"value": NA, "numerator": None, "denominator": None, "reason": reason or "field_absent"}
    return {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "reason": reason,
    }


def _prf(tp: int, predicted: int, gold: int, *, observed: bool = True, reason: Optional[str] = None) -> Dict[str, Any]:
    if not observed:
        return _metric(NA, reason=reason or "field_absent", observed=False)
    if not gold and not predicted:
        return _metric(NA, reason="no_scoring_support", observed=False)
    precision = tp / predicted if predicted else 0.0
    recall = tp / gold if gold else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "value": round(f1, 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "numerator": tp,
        "denominator": max(gold, predicted),
        "gold_count": gold,
        "predicted_count": predicted,
        "reason": reason,
    }


def _accuracy(matches: int, total: int, *, observed: bool = True, reason: Optional[str] = None) -> Dict[str, Any]:
    if not observed:
        return _metric(NA, reason=reason or "field_absent", observed=False)
    if not total:
        return _metric(NA, reason="no_scoring_support", observed=False)
    return _metric(round(matches / total, 6), numerator=matches, denominator=total, reason=reason)


def _macro_f1(gold_values: Sequence[str], predicted_values: Sequence[str], *, observed: bool = True,
              reason: Optional[str] = None) -> Dict[str, Any]:
    if not observed:
        return _metric(NA, reason=reason or "field_absent", observed=False)
    labels = sorted(set(gold_values) | set(predicted_values))
    if not labels:
        return _metric(NA, reason="no_scoring_support", observed=False)
    scores: List[float] = []
    for label in labels:
        tp = sum(1 for gold, pred in zip(gold_values, predicted_values) if gold == label and pred == label)
        gold_count = sum(1 for value in gold_values if value == label)
        pred_count = sum(1 for value in predicted_values if value == label)
        precision = tp / pred_count if pred_count else 0.0
        recall = tp / gold_count if gold_count else 0.0
        scores.append(2.0 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return _metric(round(sum(scores) / len(scores), 6), numerator=len(labels), denominator=len(labels), reason=reason)


def _fragment_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    message_id = str(row.get("message_id") or "")
    if _present(row.get("span_start")) and _present(row.get("span_end")):
        return (message_id, int(row.get("span_start")), int(row.get("span_end")))
    return (message_id, str(row.get("fragment_id") or row.get("prediction_fragment_id") or ""))


def _claim_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    spans = tuple(sorted(
        (int(item.get("start", 0)), int(item.get("end", 0)))
        for item in row.get("evidence_spans") or [] if isinstance(item, Mapping)
    ))
    # Do not include claim_type, target entities, state, modality, or any
    # field that is scored below.  Including a label in the identity key would
    # make a wrong label look like a missing/extra claim and inflate its F1.
    if spans:
        return (str(row.get("message_id") or ""), spans)
    return (str(row.get("message_id") or ""), str(row.get("fragment_id") or ""))


def _aligned_records(gold: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]], key_fn) -> Tuple[List[Tuple[Mapping[str, Any], Mapping[str, Any]]], Set[Tuple[Any, ...]], Set[Tuple[Any, ...]]]:
    gold_map: Dict[Tuple[Any, ...], Mapping[str, Any]] = {}
    pred_map: Dict[Tuple[Any, ...], Mapping[str, Any]] = {}
    for row in gold:
        key = key_fn(row)
        if key in gold_map:
            raise ValueError("ambiguous gold Stage1 semantic match key")
        gold_map[key] = row
    for row in predicted:
        key = key_fn(row)
        if key in pred_map:
            raise ValueError("ambiguous predicted Stage1 semantic match key")
        pred_map[key] = row
    common = set(gold_map) & set(pred_map)
    return [(gold_map[key], pred_map[key]) for key in sorted(common, key=repr)], set(gold_map) - common, set(pred_map) - common


def _aligned_records_many(gold: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]], key_fn) -> Tuple[List[Tuple[Mapping[str, Any], Mapping[str, Any]]], Set[Tuple[Any, ...]], Set[Tuple[Any, ...]]]:
    """Align duplicate span keys one-to-one while counting leftovers.

    Mentions can legitimately share one span/type key in a rule-based
    prediction (for example, competing normalizations).  They are evaluated
    as one aligned pair plus explicit extras rather than silently collapsed.
    """

    gold_map: Dict[Tuple[Any, ...], List[Mapping[str, Any]]] = {}
    pred_map: Dict[Tuple[Any, ...], List[Mapping[str, Any]]] = {}
    for row in gold:
        gold_map.setdefault(key_fn(row), []).append(row)
    for row in predicted:
        pred_map.setdefault(key_fn(row), []).append(row)
    pairs: List[Tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    missing: Set[Tuple[Any, ...]] = set()
    extra: Set[Tuple[Any, ...]] = set()
    for key in sorted(set(gold_map) | set(pred_map), key=repr):
        gold_rows = gold_map.get(key, [])
        pred_rows = pred_map.get(key, [])
        for index in range(min(len(gold_rows), len(pred_rows))):
            pairs.append((gold_rows[index], pred_rows[index]))
        missing.update((key, index) for index in range(min(len(gold_rows), len(pred_rows)), len(gold_rows)))
        extra.update((key, index) for index in range(min(len(gold_rows), len(pred_rows)), len(pred_rows)))
    return pairs, missing, extra


def _field_values(pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]], field: str) -> Tuple[List[str], List[str], bool, bool]:
    gold_values: List[str] = []
    pred_values: List[str] = []
    gold_present = False
    pred_present = False
    for gold, pred in pairs:
        if _present(gold.get(field)):
            gold_present = True
        if _present(pred.get(field)):
            pred_present = True
        if _present(gold.get(field)) and _present(pred.get(field)):
            gold_values.append(str(gold[field]))
            pred_values.append(str(pred[field]))
    return gold_values, pred_values, gold_present, pred_present


def _field_accuracy(pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]], field: str) -> Dict[str, Any]:
    values = [(str(gold[field]), str(pred[field])) for gold, pred in pairs if _present(gold.get(field)) and _present(pred.get(field))]
    gold_present = any(_present(gold.get(field)) for gold, _ in pairs)
    pred_present = any(_present(pred.get(field)) for _, pred in pairs)
    if not gold_present:
        return _metric(NA, reason="gold_field_absent", observed=False)
    if not pred_present:
        return _metric(NA, reason="prediction_field_absent", observed=False)
    return _accuracy(sum(gold == pred for gold, pred in values), len(values), reason="unmatched_or_missing_rows" if len(values) < len(pairs) else None)


def _field_macro_f1(pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]], field: str) -> Dict[str, Any]:
    gold_values, pred_values, gold_present, pred_present = _field_values(pairs, field)
    if not gold_present:
        return _metric(NA, reason="gold_field_absent", observed=False)
    if not pred_present:
        return _metric(NA, reason="prediction_field_absent", observed=False)
    return _macro_f1(gold_values, pred_values, reason="unmatched_or_missing_rows" if len(gold_values) < len(pairs) else None)


def _set_field_f1(pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]], field: str) -> Dict[str, Any]:
    """Micro F1 for a list-valued semantic field on aligned records."""

    gold_present = any(field in gold and gold.get(field) is not None for gold, _ in pairs)
    pred_present = any(field in pred and pred.get(field) is not None for _, pred in pairs)
    if not gold_present:
        return _metric(NA, reason="gold_field_absent", observed=False)
    if not pred_present:
        return _metric(NA, reason="prediction_field_absent", observed=False)
    gold_total = 0
    pred_total = 0
    tp = 0
    for gold, pred in pairs:
        gold_values = {str(value) for value in gold.get(field) or []}
        pred_values = {str(value) for value in pred.get(field) or []}
        gold_total += len(gold_values)
        pred_total += len(pred_values)
        tp += len(gold_values & pred_values)
    return _prf(tp, pred_total, gold_total, reason="unmatched_or_missing_rows" if len(pairs) else None)


def _mention_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    start = row.get("span_start")
    end = row.get("span_end")
    return (
        str(row.get("message_id") or ""),
        int(start) if _present(start) else 0,
        int(end) if _present(end) else 0,
    )


def _argument_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    # Role and entity are scored fields, so they must not define identity.
    return (
        str(row.get("fragment_id") or ""),
        str(row.get("claim_id") or ""),
    )


def _person_set(source: Union[Stage1Projection, Mapping[str, Any]], role: str) -> Set[Tuple[Any, ...]]:
    result: Set[Tuple[Any, ...]] = set()
    for row in _records(source, "persons"):
        if row.get("role") != role:
            continue
        result.add((
            str(row.get("message_id") or ""), str(row.get("fragment_id") or ""),
            int(row.get("span_start")) if _present(row.get("span_start")) else None,
            int(row.get("span_end")) if _present(row.get("span_end")) else None,
            str(row.get("person_id") or "unknown"),
        ))
    return result


def _set_prf(gold: Set[Any], predicted: Set[Any], *, observed: bool, reason: str) -> Dict[str, Any]:
    return _prf(len(gold & predicted), len(predicted), len(gold), observed=observed, reason=reason)


def _anchor_semantic_map(source: Union[Stage1Projection, Mapping[str, Any]], anchor_type: str) -> Dict[str, Tuple[Any, ...]]:
    name = "fragments" if anchor_type == "fragment" else "claims"
    key_fn = _fragment_key if anchor_type == "fragment" else _claim_key
    result: Dict[str, Tuple[Any, ...]] = {}
    for row in _records(source, name):
        for field in (("fragment_id", "prediction_fragment_id") if anchor_type == "fragment" else ("claim_id", "prediction_claim_id")):
            value = row.get(field)
            if value not in (None, ""):
                result[str(value)] = key_fn(row)
    return result


def _context_map(source: Union[Stage1Projection, Mapping[str, Any]]) -> Dict[Tuple[Any, ...], Mapping[str, Any]]:
    result: Dict[Tuple[Any, ...], Mapping[str, Any]] = {}
    for row in _records(source, "context_relations"):
        anchor_type = str(row.get("anchor_type") or "")
        id_map = _anchor_semantic_map(source, anchor_type) if anchor_type in {"fragment", "claim"} else {}
        left = id_map.get(str(row.get("left_anchor_id")), (str(row.get("left_anchor_id") or ""),))
        right = id_map.get(str(row.get("right_anchor_id")), (str(row.get("right_anchor_id") or ""),))
        result[(anchor_type, min(left, right, key=repr), max(left, right, key=repr))] = row
    return result


def _context_pair_map(gold: Union[Stage1Projection, Mapping[str, Any]], predicted: Union[Stage1Projection, Mapping[str, Any]]) -> Tuple[Dict[Tuple[Any, ...], Mapping[str, Any]], Dict[Tuple[Any, ...], Mapping[str, Any]]]:
    # Maps are built independently then aligned by each side's semantic anchor
    # key.  Same development IDs are preferred; span/claim keys handle local
    # prediction IDs.
    return _context_map(gold), _context_map(predicted)


def _context_aligned(gold: Union[Stage1Projection, Mapping[str, Any]], predicted: Union[Stage1Projection, Mapping[str, Any]]) -> Tuple[List[Tuple[Mapping[str, Any], Mapping[str, Any]]], Set[Tuple[Any, ...]], Set[Tuple[Any, ...]]]:
    gold_map, pred_map = _context_pair_map(gold, predicted)
    common = set(gold_map) & set(pred_map)
    return [(gold_map[key], pred_map[key]) for key in sorted(common, key=repr)], set(gold_map) - common, set(pred_map) - common


def _time_only(row: Mapping[str, Any]) -> bool:
    if row.get("explicit_reply_present") is not False or row.get("time_evidence") != "weak":
        return False
    slots = {str(value).lower() for value in row.get("supporting_slot_codes") or []}
    return bool(slots) and slots.issubset({"time", "time_near", "time_proximity", "temporal", "time_distance"})


def _field_presence(source: Union[Stage1Projection, Mapping[str, Any]], name: str, field: str) -> bool:
    return any(field in row and row.get(field) is not None for row in _records(source, name))


def _rows_have_fields(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bool:
    return any(all(field in row and row.get(field) is not None for field in fields) for row in rows)


def summarize_stage1_projection(source: Union[Stage1Projection, Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a structure-only aggregate summary with no record values."""

    distributions: Dict[str, Dict[str, int]] = {}
    for name, field in (
        ("fragments", "fragment_type"), ("fragments", "object_resolution"),
        ("fragments", "state"), ("claims", "claim_type"), ("claims", "modality"),
        ("claims", "state"), ("context_relations", "label"),
        ("context_relations", "evidence_strength"),
    ):
        key = "%s.%s" % (name, field)
        counter = Counter(str(row[field]) for row in _records(source, name) if _present(row.get(field)))
        distributions[key] = dict(sorted(counter.items()))
    return {
        "schema_version": STAGE1_SCHEMA_VERSION,
        "scope": STAGE1_SCOPE,
        "record_counts": {name: len(_records(source, name)) for name in COLLECTIONS},
        "field_presence": {
            "%s.%s" % (name, field): _field_presence(source, name, field)
            for name, fields in _SAFE_FIELDS.items()
            for field in fields
            if field not in _BODY_FIELDS and field not in {"message_id", "fragment_id", "claim_id", "mention_id", "person_ref_id", "argument_id", "discourse_thread_id", "context_relation_id"}
        },
        "label_counts": distributions,
        "validation": (source.validation.to_dict() if isinstance(source, Stage1Projection) else validate_stage1_projection(source, require_development=False).to_dict()),
    }


def score_stage1(gold: Union[Stage1Projection, Mapping[str, Any]], predictions: Union[Stage1Projection, Mapping[str, Any]]) -> Dict[str, Any]:
    """Score Stage 1 people/argument/state/fragment/context semantics.

    The result is intentionally aggregate-only.  Each metric is an object
    whose ``value`` is numeric when scorable and ``"N/A"`` when a field is
    absent or has no valid denominator.  Zero is reserved for an observed
    field with a measured zero result.
    """

    if not isinstance(gold, Stage1Projection):
        gold = project_stage1_gold(gold)
    if not isinstance(predictions, Stage1Projection):
        predictions = project_stage1_predictions(predictions)
    if gold.scope != STAGE1_SCOPE:
        raise ValueError("Stage1 gold scope must be development")
    if not gold.validation.ok:
        raise ValueError("invalid Stage1 gold projection: %s" % "; ".join(gold.validation.errors))

    metrics: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, Any] = {
        "gold": {name: len(gold.collection(name)) for name in COLLECTIONS},
        "predicted": {name: len(predictions.collection(name)) for name in COLLECTIONS},
    }

    gold_fragments = gold.collection("fragments")
    pred_fragments = predictions.collection("fragments")
    fragment_pairs, missing_fragments, extra_fragments = _aligned_records(gold_fragments, pred_fragments, _fragment_key)
    counts["fragments"] = {"aligned": len(fragment_pairs), "missing": len(missing_fragments), "extra": len(extra_fragments)}
    metrics["fragment_span_f1"] = _prf(
        len(fragment_pairs), len(pred_fragments), len(gold_fragments),
        observed=_rows_have_fields(pred_fragments, ("message_id", "span_start", "span_end")),
        reason="prediction_fragment_spans_absent",
    )
    metrics["fragment_role_macro_f1"] = _field_macro_f1(fragment_pairs, "fragment_type")
    metrics["speaker_accuracy"] = _field_accuracy(fragment_pairs, "speaker_id")
    metrics["subject_accuracy"] = _field_accuracy(fragment_pairs, "subject_id")
    metrics["object_resolution_macro_f1"] = _field_macro_f1(fragment_pairs, "object_resolution")
    metrics["object_referent_accuracy"] = _field_accuracy(fragment_pairs, "object_id")
    metrics["state_macro_f1"] = _field_macro_f1(fragment_pairs, "state")
    metrics["state_evidence_accuracy"] = _field_accuracy(fragment_pairs, "state_evidence")

    gold_mentions = gold.collection("mentions")
    pred_mentions = predictions.collection("mentions")
    mention_pairs, missing_mentions, extra_mentions = _aligned_records_many(gold_mentions, pred_mentions, _mention_key)
    counts["mentions"] = {"aligned": len(mention_pairs), "missing": len(missing_mentions), "extra": len(extra_mentions)}
    metrics["mention_span_f1"] = _prf(
        len(mention_pairs), len(pred_mentions), len(gold_mentions),
        observed=_rows_have_fields(pred_mentions, ("message_id", "span_start", "span_end")),
        reason="prediction_mention_spans_absent",
    )
    metrics["mention_type_accuracy"] = _field_accuracy(mention_pairs, "mention_type")
    metrics["mention_referent_accuracy"] = _field_accuracy(mention_pairs, "normalized_id")
    metrics["mention_role_accuracy"] = _field_accuracy(mention_pairs, "entity_role")

    gold_arguments = gold.collection("arguments")
    pred_arguments = predictions.collection("arguments")
    argument_pairs, missing_arguments, extra_arguments = _aligned_records_many(
        gold_arguments, pred_arguments, _argument_key,
    )
    counts["arguments"] = {
        "aligned": len(argument_pairs), "missing": len(missing_arguments), "extra": len(extra_arguments),
    }
    metrics["argument_role_accuracy"] = _field_accuracy(argument_pairs, "role")
    metrics["argument_resolution_accuracy"] = _field_accuracy(argument_pairs, "resolution")
    metrics["argument_referent_accuracy"] = _field_accuracy(argument_pairs, "entity_id")

    gold_claims = gold.collection("claims")
    pred_claims = predictions.collection("claims")
    claim_pairs, missing_claims, extra_claims = _aligned_records(gold_claims, pred_claims, _claim_key)
    counts["claims"] = {"aligned": len(claim_pairs), "missing": len(missing_claims), "extra": len(extra_claims)}
    claim_key_fields = ("message_id", "evidence_spans")
    metrics["claim_coverage"] = _prf(
        len(claim_pairs), len(pred_claims), len(gold_claims),
        observed=_rows_have_fields(pred_claims, claim_key_fields),
        reason="prediction_claim_match_fields_absent",
    )
    for field, metric_name in (
        ("claim_type", "claim_type_macro_f1"), ("modality", "modality_macro_f1"),
        ("state", "claim_state_macro_f1"), ("attribution", "attribution_accuracy"),
        ("state_evidence", "claim_state_evidence_accuracy"),
        ("speaker_id", "claim_speaker_accuracy"), ("subject_id", "claim_subject_accuracy"),
        ("object_resolution", "claim_object_resolution_macro_f1"), ("object_id", "claim_object_referent_accuracy"),
    ):
        metrics[metric_name] = _field_macro_f1(claim_pairs, field) if metric_name.endswith("macro_f1") else _field_accuracy(claim_pairs, field)
    metrics["claim_mentioned_person_f1"] = _set_field_f1(claim_pairs, "mentioned_person_ids")
    metrics["claim_target_entity_f1"] = _set_field_f1(claim_pairs, "target_entity_ids")

    gold_persons = gold.collection("persons")
    pred_persons = predictions.collection("persons")
    for role, metric_name in (
        ("mentioned_person", "mentioned_person_referent_f1"),
        ("speaker", "person_speaker_role_f1"),
        ("subject", "person_subject_role_f1"),
    ):
        gold_set = _person_set(gold, role)
        pred_set = _person_set(predictions, role)
        observed = bool(pred_persons) and any(row.get("role") == role for row in pred_persons)
        metrics[metric_name] = _set_prf(gold_set, pred_set, observed=observed, reason="prediction_person_role_absent")
    counts["persons"] = {"gold": len(gold_persons), "predicted": len(pred_persons)}

    gold_context = gold.collection("context_relations")
    pred_context = predictions.collection("context_relations")
    context_pairs, missing_context, extra_context = _context_aligned(gold, predictions)
    counts["context_relations"] = {"aligned": len(context_pairs), "missing": len(missing_context), "extra": len(extra_context)}
    metrics["context_relation_label_macro_f1"] = _field_macro_f1(context_pairs, "label")
    metrics["context_evidence_strength_macro_f1"] = _field_macro_f1(context_pairs, "evidence_strength")
    evidence_observed = bool(pred_context) and any("evidence_refs" in row for row in pred_context)
    if evidence_observed:
        evidence_hit = sum(bool(pred_row.get("evidence_refs")) for _, pred_row in context_pairs)
        metrics["context_typed_evidence_coverage"] = _metric(
            round(evidence_hit / len(gold_context), 6) if gold_context else NA,
            numerator=evidence_hit, denominator=len(gold_context),
            reason="no_gold_context_relations" if not gold_context else None,
            observed=bool(gold_context),
        )
    else:
        metrics["context_typed_evidence_coverage"] = _metric(
            NA, reason="prediction_context_evidence_absent", observed=False,
        )

    no_reply_gold = [row for row in gold_context if row.get("label") == "answers" and row.get("explicit_reply_present") is False]
    no_reply_pairs = [pair for pair in context_pairs if pair[0].get("label") == "answers" and pair[0].get("explicit_reply_present") is False]
    no_reply_hit = sum(1 for gold_row, pred_row in no_reply_pairs if pred_row.get("label") == "answers")
    metrics["no_reply_answers_recall"] = _accuracy(
        no_reply_hit, len(no_reply_gold),
        observed=_rows_have_fields(pred_context, ("label", "explicit_reply_present")),
        reason="no_reply_answers_gold_absent" if not no_reply_gold else "prediction_context_reply_fields_absent",
    )

    time_only_pred = sum(_time_only(row) for row in pred_context)
    positive_pred = sum(str(row.get("label")) != "insufficient" for row in pred_context if _present(row.get("label")))
    time_observed = bool(pred_context) and all("explicit_reply_present" in row and "time_evidence" in row and "supporting_slot_codes" in row for row in pred_context)
    metrics["time_only_relation_rate"] = _metric(
        round(time_only_pred / positive_pred, 6) if positive_pred else NA,
        numerator=time_only_pred, denominator=positive_pred,
        reason="no_positive_relations" if not positive_pred else None,
        observed=time_observed,
    )
    metrics["context_relation_coverage"] = _prf(
        len(context_pairs), len(pred_context), len(gold_context),
        observed=_rows_have_fields(pred_context, ("anchor_type", "left_anchor_id", "right_anchor_id", "label")),
        reason="prediction_context_relation_fields_absent",
    )

    # Conditional inheritance safety: an observed inherited prediction is
    # correct only when its referent and source agree with gold.  Missing
    # inheritance fields remain N/A.
    inherited_pairs = [pair for pair in fragment_pairs if pair[1].get("object_resolution") == "inherited"]
    inheritance_observed = bool(pred_fragments) and any("object_inherited_from_id" in row for row in pred_fragments)
    if inheritance_observed:
        correct_inherited = sum(
            gold_row.get("object_resolution") == "inherited"
            and pred_row.get("object_id") == gold_row.get("object_id")
            and pred_row.get("object_inherited_from_id") == gold_row.get("object_inherited_from_id")
            for gold_row, pred_row in inherited_pairs
        )
        metrics["object_inheritance_precision"] = _accuracy(correct_inherited, len(inherited_pairs), observed=bool(inherited_pairs), reason="no_predicted_inherited_objects" if not inherited_pairs else None)
    else:
        metrics["object_inheritance_precision"] = _metric(NA, reason="prediction_inheritance_source_absent", observed=False)

    report = {
        "schema_version": STAGE1_SCHEMA_VERSION,
        "scope": STAGE1_SCOPE,
        "metrics": metrics,
        "metric_values": {name: item.get("value", NA) for name, item in metrics.items()},
        "counts": counts,
        "field_status": {
            "gold": summarize_stage1_projection(gold)["field_presence"],
            "predicted": summarize_stage1_projection(predictions)["field_presence"],
        },
        "validation": {
            "gold": gold.validation.to_dict(),
            "predicted": predictions.validation.to_dict(),
        },
    }
    return report


# Verbose aliases make the scaffold discoverable without coupling callers to
# one naming choice.
build_stage1_gold_projection = project_stage1_gold
build_stage1_prediction_projection = project_stage1_predictions
score_stage1_predictions = score_stage1


__all__ = [
    "STAGE1_SCHEMA_VERSION", "STAGE1_SCOPE", "NA", "FRAGMENT_TYPES", "PERSON_ROLES",
    "OBJECT_RESOLUTIONS", "STATE_VALUES", "MODALITY_VALUES", "CLAIM_TYPES",
    "CONTEXT_RELATION_LABELS", "EVIDENCE_STRENGTH_VALUES", "TIME_EVIDENCE_VALUES",
    "ATTRIBUTION_VALUES", "TERMINAL_STATES", "COLLECTIONS", "Stage1Validation",
    "Stage1Projection", "validate_stage1_projection", "project_stage1_gold",
    "load_development_gold_directory",
    "project_stage1_predictions", "summarize_stage1_projection", "score_stage1",
    "build_stage1_gold_projection", "build_stage1_prediction_projection",
    "score_stage1_predictions",
]
