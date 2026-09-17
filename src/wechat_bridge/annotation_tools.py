"""Offline A/B annotation workflow for the private semantic gold standard.

This module deliberately sits beside :mod:`semantic_gold` instead of changing
the exporter or production pipeline.  It consumes two independently-authored
JSONL streams containing already-redacted records and provides four small,
composable operations:

* load/write an annotation stream with annotator and guide provenance;
* align A/B records and emit a structure-only disagreement report;
* apply field-level adjudications to produce an ``adjudicated`` stream; and
* validate the cross-file annotation graph (claims, relations, clusters and
  presentations); and
* gate a stream before it can be marked ``frozen``.

The disagreement report is intentionally not a copy of either annotation
stream.  Text-bearing fields (including redacted message text, claim text,
mention surfaces, titles, sentence units, summaries, notes and uncertainties)
are represented only by their field name and a ``value_omitted`` marker.  This
keeps the report useful for triage without creating a second text export.

No database, live connector, model API, or production API is used here.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .semantic_gold import ANNOTATION_GUIDE_VERSION, DATASET_VERSION, SCHEMA_VERSION


TOOL_VERSION = "annotation_tools_v1"
ANNOTATOR_A = "ANN_A"
ANNOTATOR_B = "ANN_B"
DEFAULT_ADJUDICATOR = "ADJ_1"

_MISSING = object()

# Contract records have a common record_id, while the semantic object itself
# has a type-specific id.  The latter is useful when callers omit the optional
# record_type marker in a hand-authored stream.
_TYPE_ALIASES = {
    "messages": "message",
    "message": "message",
    "mentions": "mention",
    "mention": "mention",
    "claims": "claim",
    "claim": "claim",
    "relations": "relation",
    "relation": "relation",
    "clusters": "cluster",
    "cluster": "cluster",
    "presentations": "presentation",
    "presentation": "presentation",
    "adjudications": "adjudication",
    "adjudication": "adjudication",
}
_TYPE_ID_FIELDS = {
    "message": "message_id",
    "mention": "mention_id",
    "claim": "claim_id",
    "relation": "relation_id",
    "cluster": "cluster_id",
    "presentation": "presentation_id",
    "adjudication": "adjudication_id",
}
_METADATA_FIELDS = {
    "schema_version",
    "dataset_version",
    "record_id",
    "record_type",
    "annotator_id",
    "annotation_status",
    "provenance",
    "guide_version",
}
# IDs are used to find an aligned pair, not treated as an annotation choice.
# A and B may legitimately mint local claim/relation IDs independently.  Once
# alignment succeeds, these identity fields (and canonical relation anchors)
# must not create spurious adjudication work.
_IDENTITY_FIELDS = {
    "message_id",
    "mention_id",
    "claim_id",
    "relation_id",
    "cluster_id",
    "presentation_id",
    "left_anchor_id",
    "right_anchor_id",
}

# These are safe to copy into a disagreement report.  They are identifiers,
# offsets, labels, and finite decision metadata, not free-form text.
_SAFE_VALUE_FIELDS = {
    "message_id",
    "account_id",
    "chat_id",
    "chat_type",
    "speaker_id",
    "direction",
    "message_type",
    "local_day",
    "time_offset_seconds",
    "time_bucket",
    "sequence_in_chat",
    "reply_to_message_id",
    "redaction_types",
    "media_state",
    "source_mode",
    "context_message_ids",
    "split",
    "mention_id",
    "mention_type",
    "span_start",
    "span_end",
    "normalized_id",
    "normalized_type",
    "certainty",
    "claim_id",
    "claim_type",
    "target_entity_ids",
    "event_mention_ids",
    "evidence_spans",
    "stance",
    "polarity",
    "modality",
    "status",
    "attribution",
    "timestamp_message_id",
    "relation_id",
    "left_anchor_id",
    "right_anchor_id",
    "anchor_type",
    "label",
    "supporting_slot_codes",
    "conflicting_slot_codes",
    "evidence_message_ids",
    "must_not_link",
    "must_not_link_reason_codes",
    "confidence",
    "adjudication_id",
    "cluster_id",
    "cluster_type",
    "event_type",
    "core_entity_ids",
    "action_types",
    "intent_types",
    "state_sequence",
    "mention_ids",
    "claim_ids",
    "member_message_ids",
    "relation_ids",
    "must_not_link_checked",
    "start_message_id",
    "end_message_id",
    "topic_family_ids",
    "presentation_id",
    "presentation_type",
    "source_cluster_ids",
    "source_claim_ids",
    "participant_ids",
    "fact_claim_ids",
    "opinion_claim_ids",
    "question_claim_ids",
    "detail_policy",
    "expected_order_group",
    "must_remain_separate_from",
    "display_decision_reason_codes",
    # Typed, non-text evidence for a same_event decision.  These refs are
    # safe to retain in structure-only disagreement reports because they
    # carry IDs, span offsets and finite support codes, never message text.
    "observable_support_refs",
}

# A field can be unknown to this version of the contract.  A conservative
# name-based check ensures that a future free-form field cannot accidentally
# leak into a public disagreement file.
_BODY_FIELD_PARTS = (
    "text",
    "content",
    "surface",
    "title",
    "summary",
    "sentence",
    "note",
    "uncertaint",
    "description",
    "raw",
    "transcript",
)

# Keep the graph checks local to the offline annotation tool.  The semantic
# exporter has a deliberately smaller structural validator; this layer is the
# pre-release/freeze boundary where cross-file identity and adjudication
# invariants can be checked without opening a source database or looking at
# message正文.
_RELATION_LABELS = frozenset(
    {"same_event", "related_event", "same_topic_only", "unrelated", "insufficient_context"}
)
_CLUSTER_TYPES = frozenset({"event", "non_event_context", "insufficient_context"})
_PRESENTATION_TYPES = frozenset(
    {"event_card", "topic_observation", "trend_item", "do_not_display"}
)
_VISIBLE_PRESENTATION_TYPES = frozenset({"event_card", "topic_observation", "trend_item"})
_ANCHOR_TYPES = frozenset({"mention", "claim", "event_seed"})
_CONTEXT_ONLY_VALUES = frozenset(
    {
        "context",
        "context only",
        "non event",
        "non event context",
        "social only",
    }
)
_MISSING_LABEL_VALUES = frozenset({"", "missing", "absent", "none", "null", "n/a", "na"})
_GRAPH_COLLECTION_TYPES = {
    "messages": "message",
    "mentions": "mention",
    "claims": "claim",
    "relations": "relation",
    "clusters": "cluster",
    "presentations": "presentation",
    "adjudications": "adjudication",
}
_GRAPH_ID_FIELDS = {
    "message": "message_id",
    "mention": "mention_id",
    "claim": "claim_id",
    "relation": "relation_id",
    "cluster": "cluster_id",
    "presentation": "presentation_id",
    "adjudication": "adjudication_id",
}

# A ``same_event`` relation is not justified by a bare reason code.  The
# relation must carry typed references back to the claim/mention/message
# evidence that makes the reason observable to a downstream algorithm.  The
# aliases keep the validator tolerant of the compact forms used by older
# annotation tooling while the canonical wire form remains
# ``{"type": ..., "id": ..., "support_code": ...}``.
_OBSERVABLE_SUPPORT_REF_FIELD = "observable_support_refs"
_OBSERVABLE_REF_TYPE_ALIASES = {
    "message": "message",
    "messages": "message",
    "message_id": "message",
    "mention": "mention",
    "mentions": "mention",
    "mention_id": "mention",
    "claim": "claim",
    "claims": "claim",
    "claim_id": "claim",
}
_OBSERVABLE_INSTANCE_FIELDS = (
    "event_instance_id",
    "instance_id",
    "shared_instance_id",
    "explicit_instance_id",
    "event_key",
    "event_id",
    "event_seed_id",
)
_OBSERVABLE_INSTANCE_SLOT_ALIASES = frozenset(
    {
        "explicit_shared_instance",
        "shared_instance",
        "same_instance",
        "instance",
        "same_message",
        "explicit_reply",
        "reply",
        "reply_to_message",
        "explicit_continuation",
        "explicit_continuation_cue",
        "continuation_cue",
    }
)
_OBSERVABLE_REF_SLOT_ALIASES = {
    "explicit_shared_instance": frozenset(
        {
            "explicit_shared_instance",
            "shared_instance",
            "same_instance",
            "instance",
        }
    ),
    "shared_action": frozenset({"shared_action", "action", "action_type"}),
    "shared_core_object": frozenset(
        {"shared_core_object", "core_object", "core_entity", "entity", "object"}
    ),
    "explicit_continuation_cue": frozenset(
        {"explicit_continuation_cue", "explicit_continuation", "continuation_cue"}
    ),
    "shared_event_or_state": frozenset(
        {"shared_event_or_state", "shared_event", "shared_state", "event_or_state"}
    ),
    "same_message": frozenset({"same_message"}),
    "explicit_reply": frozenset({"explicit_reply", "reply", "reply_to_message"}),
}


class AnnotationFormatError(ValueError):
    """Raised when an annotation JSONL stream is structurally unsafe."""


class UnresolvedDisagreementsError(ValueError):
    """Raised when a merge/freeze operation would carry open disputes."""


class FreezeGateError(ValueError):
    """Raised when a merged stream fails the frozen-release gate."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _record_type(record: Mapping[str, Any]) -> str:
    explicit = record.get("record_type") or record.get("kind")
    if explicit:
        normalized = _TYPE_ALIASES.get(str(explicit).casefold())
        if normalized:
            return normalized
        return str(explicit)
    for candidate, field_name in _TYPE_ID_FIELDS.items():
        if record.get(field_name) is not None:
            return candidate
    # Relation records are common in small hand-authored fixtures and may only
    # carry anchors plus a label.
    if "left_anchor_id" in record or "right_anchor_id" in record or "label" in record:
        return "relation"
    return "record"


def _record_id(record: Mapping[str, Any], record_type: Optional[str] = None) -> str:
    value = record.get("record_id")
    if value is None or str(value) == "":
        type_name = record_type or _record_type(record)
        field_name = _TYPE_ID_FIELDS.get(type_name)
        if field_name:
            value = record.get(field_name)
    return str(value or "")


def _type_specific_id(record: Mapping[str, Any], record_type: Optional[str] = None) -> str:
    """Return the domain ID before falling back to the common record_id."""

    type_name = record_type or _record_type(record)
    field_name = _TYPE_ID_FIELDS.get(type_name)
    if field_name and record.get(field_name) is not None:
        return str(record[field_name])
    return _record_id(record, type_name)


def _graph_collections(value: Any) -> Dict[str, List[Mapping[str, Any]]]:
    """Normalize a flat merged stream or contract collections for graph checks.

    The public validator is intentionally usable with both inputs already
    produced by this module (a flat sequence of merged records) and the
    contract-shaped mapping used by the pre-release tooling.  It never reads a
    source archive and keeps the values in memory only long enough to inspect
    IDs and finite labels.
    """

    result: Dict[str, List[Mapping[str, Any]]] = {
        name: [] for name in _GRAPH_COLLECTION_TYPES
    }
    if isinstance(value, Mapping):
        found_collection = False
        for collection_name in _GRAPH_COLLECTION_TYPES:
            rows = value.get(collection_name)
            if isinstance(rows, list) or isinstance(rows, tuple):
                found_collection = True
                result[collection_name].extend(
                    row for row in rows if isinstance(row, Mapping)
                )
        if found_collection:
            return result
        # A single record is useful in small caller fixtures.  Treat it as a
        # flat stream rather than silently accepting an empty graph.
        value = (value,)
    if isinstance(value, (str, bytes)):
        return result
    try:
        iterator = iter(value)
    except TypeError:
        return result
    for row in iterator:
        if not isinstance(row, Mapping):
            continue
        type_name = _record_type(row)
        collection_name = next(
            (name for name, candidate in _GRAPH_COLLECTION_TYPES.items() if candidate == type_name),
            None,
        )
        if collection_name is not None:
            result[collection_name].append(row)
    return result


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _graph_id_list(
    record: Mapping[str, Any],
    field_name: str,
    owner: str,
    errors: List[str],
) -> List[str]:
    """Read a typed FK list and reject non-string IDs.

    Returning an empty list for an absent field preserves compatibility with
    hand-authored annotation streams whose optional evidence lists are not
    populated yet.  If a field is present, however, its type is part of the
    release contract and is checked strictly.
    """

    if field_name not in record or record.get(field_name) is None:
        return []
    raw = record.get(field_name)
    if not isinstance(raw, (list, tuple)):
        errors.append("%s.%s must be a list of string IDs" % (owner, field_name))
        return []
    result: List[str] = []
    for index, value in enumerate(raw):
        if not _is_nonempty_string(value):
            errors.append("%s.%s[%d] must be a non-empty string ID" % (owner, field_name, index))
            continue
        result.append(str(value))
    return result


def _graph_id_value(
    record: Mapping[str, Any],
    field_name: str,
    owner: str,
    errors: List[str],
    *,
    required: bool = False,
) -> str:
    if field_name not in record or record.get(field_name) is None:
        if required:
            errors.append("%s.%s must be a non-empty string ID" % (owner, field_name))
        return ""
    value = record.get(field_name)
    if not _is_nonempty_string(value):
        errors.append("%s.%s must be a non-empty string ID" % (owner, field_name))
        return ""
    return str(value)


def _build_graph_lookup(
    records: Sequence[Mapping[str, Any]],
    type_name: str,
    errors: List[str],
) -> Dict[str, Mapping[str, Any]]:
    """Index one typed collection by its domain ID and common record_id."""

    field_name = _GRAPH_ID_FIELDS[type_name]
    result: Dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        owner = "%s[%d]" % (type_name, index)
        type_id = record.get(field_name)
        record_id = record.get("record_id")
        if type_id is None:
            type_id = record_id
        if not _is_nonempty_string(type_id):
            errors.append("%s is missing a non-empty %s" % (owner, field_name))
            continue
        keys = [str(type_id)]
        if _is_nonempty_string(record_id) and str(record_id) not in keys:
            keys.append(str(record_id))
        for key in keys:
            previous = result.get(key)
            if previous is not None and previous is not record:
                errors.append("duplicate %s ID %s" % (type_name, key))
            else:
                result[key] = record
    return result


def _normalise_role(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().casefold().replace("_", " ").replace("-", " ")


def _is_context_only_record(record: Optional[Mapping[str, Any]]) -> bool:
    """Return whether a record is explicitly marked context-only.

    Different annotation passes use ``dialogue_role`` or
    ``dialogue_event_role``; accepting the finite aliases here keeps the
    release gate independent from the candidate-generation implementation.
    No text field is inspected.
    """

    if not isinstance(record, Mapping):
        return False
    boolean_fields = (
        "context_only",
        "is_context_only",
        "dialogue_context_only",
        "event_context_only",
    )
    if any(record.get(field) is True for field in boolean_fields):
        return True
    role_fields = (
        "role",
        "dialogue_role",
        "event_role",
        "dialogue_event_role",
        "message_role",
        "context_role",
    )
    for field in role_fields:
        role = _normalise_role(record.get(field))
        if role in _CONTEXT_ONLY_VALUES:
            return True
    # Pilot metadata is sometimes carried through a private working stream;
    # the nested values are finite labels and safe to inspect structurally.
    for nested_name in ("pilot", "dialogue", "metadata"):
        nested = record.get(nested_name)
        if not isinstance(nested, Mapping):
            continue
        for field in role_fields:
            role = _normalise_role(nested.get(field))
            if role in _CONTEXT_ONLY_VALUES:
                return True
        if any(nested.get(field) is True for field in boolean_fields):
            return True
    return False


def _observable_slot_key(value: Any) -> str:
    """Normalize a support/reason code without interpreting free-form text."""

    if not isinstance(value, str):
        return ""
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _observable_ref_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _OBSERVABLE_REF_TYPE_ALIASES.get(_observable_slot_key(value), "")


def _observable_ref_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _observable_ref_value(
    raw: Any,
    owner: str,
    index: int,
    errors: List[str],
) -> Dict[str, Any]:
    """Parse one explicit typed observable-support reference.

    Unlike ordinary graph IDs, observable refs intentionally do not accept a
    bare ID: the type is part of the safety contract.  Compact
    ``mention:MENTION_1`` strings are accepted for callers that serialize
    typed references compactly, but untyped strings remain a hard error.
    """

    ref_type = ""
    ref_id = ""
    support_code = ""
    side = ""
    signal = ""
    span: Any = None
    if isinstance(raw, Mapping):
        ref_type = _observable_ref_type(
            raw.get("type")
            or raw.get("ref_type")
            or raw.get("record_type")
            or raw.get("collection")
            or raw.get("kind")
        )
        ref_id = _observable_ref_id(
            raw.get("id")
            or raw.get("ref_id")
            or raw.get("typed_id")
            or raw.get("value")
        )
        support_code = _observable_slot_key(
            raw.get("support_code")
            or raw.get("slot_code")
            or raw.get("reason_code")
            or raw.get("reason")
            or raw.get("for")
        )
        side = _observable_slot_key(raw.get("side"))
        signal = _observable_slot_key(raw.get("signal"))
        span = raw.get("span")
    elif isinstance(raw, str):
        text = raw.strip()
        for separator in (":", "/", "#"):
            if separator in text:
                prefix, identifier = text.split(separator, 1)
                ref_type = _observable_ref_type(prefix)
                ref_id = _observable_ref_id(identifier)
                break
    if not ref_type:
        errors.append(
            "%s.observable_support_refs[%d] must have an explicit type"
            % (owner, index)
        )
    if not ref_id:
        errors.append(
            "%s.observable_support_refs[%d] must have a non-empty typed ID"
            % (owner, index)
        )
    if not support_code:
        errors.append(
            "%s.observable_support_refs[%d] must bind a support_code"
            % (owner, index)
        )
    return {
        "type": ref_type,
        "id": ref_id,
        "support_code": support_code,
        "side": side,
        "signal": signal,
        "span": span,
    }


def _observable_slot_matches(slot: str, support_code: str) -> bool:
    slot_key = _observable_slot_key(slot)
    code_key = _observable_slot_key(support_code)
    if not slot_key or not code_key:
        return False
    aliases = _OBSERVABLE_REF_SLOT_ALIASES.get(code_key)
    if aliases is not None:
        return slot_key in aliases
    return slot_key == code_key


def _observable_signal_values(record: Optional[Mapping[str, Any]]) -> set[str]:
    """Return stable identity values explicitly carried by one record."""

    if not isinstance(record, Mapping):
        return set()
    values: set[str] = set()
    for field in _OBSERVABLE_INSTANCE_FIELDS:
        value = record.get(field)
        if _is_nonempty_string(value):
            values.add(str(value))
    return values


def _observable_span_is_valid(span: Any) -> bool:
    if not isinstance(span, Mapping):
        return False
    start, end = span.get("start"), span.get("end")
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
    )


def _observable_ref_span_matches(
    ref: Mapping[str, Any],
    target: Optional[Mapping[str, Any]],
    ref_type: str,
    owner: str,
    errors: List[str],
) -> bool:
    """Verify that a typed ref has concrete span-bearing evidence."""

    if target is None:
        return False
    explicit_span = ref.get("span")
    if explicit_span is not None and not _observable_span_is_valid(explicit_span):
        errors.append("%s.observable_support_refs has an invalid span" % owner)
        return False
    if ref_type == "mention":
        start, end = target.get("span_start"), target.get("span_end")
        if not (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end
        ):
            errors.append("%s.observable_support_refs mention lacks a concrete span" % owner)
            return False
        if explicit_span is not None and (
            explicit_span.get("start") != start or explicit_span.get("end") != end
        ):
            errors.append("%s.observable_support_refs mention span does not match target" % owner)
            return False
        return True
    if ref_type == "claim":
        evidence = target.get("evidence_spans")
        if not isinstance(evidence, (list, tuple)) or not any(
            _observable_span_is_valid(item) for item in evidence
        ):
            errors.append("%s.observable_support_refs claim lacks a concrete evidence span" % owner)
            return False
        if explicit_span is not None and not any(
            _observable_span_is_valid(item)
            and item.get("start") == explicit_span.get("start")
            and item.get("end") == explicit_span.get("end")
            for item in evidence
        ):
            errors.append("%s.observable_support_refs claim span does not match evidence" % owner)
            return False
        return True
    # A message ref is allowed for stable metadata signals such as an exact
    # same-message or reply edge.  If a caller supplies a span, validate it
    # against the redacted message length without copying the text anywhere.
    if explicit_span is not None:
        text = target.get("redacted_text")
        if (
            not isinstance(text, str)
            or not _observable_span_is_valid(explicit_span)
            or explicit_span.get("end") > len(text)
        ):
            errors.append("%s.observable_support_refs message span is outside message" % owner)
            return False
    return True


def _validate_relation_observable_support(
    record: Mapping[str, Any],
    *,
    relation_id: str,
    claims: Mapping[str, Mapping[str, Any]],
    mentions: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    errors: List[str],
) -> None:
    """Fail closed unless a same_event has typed, span-bound identity support."""

    if record.get("label") != "same_event":
        return
    owner = "relation %s" % relation_id
    raw_refs = record.get(_OBSERVABLE_SUPPORT_REF_FIELD)
    if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
        errors.append("%s same_event requires observable_support_refs" % owner)
        return
    left_id = str(record.get("left_anchor_id") or "")
    right_id = str(record.get("right_anchor_id") or "")
    anchor_type = str(record.get("anchor_type") or "claim")
    anchor_lookup = claims if anchor_type == "claim" else mentions if anchor_type == "mention" else {}
    left_anchor = anchor_lookup.get(left_id)
    right_anchor = anchor_lookup.get(right_id)

    parsed_refs: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_refs):
        parsed = _observable_ref_value(raw, owner, index, errors)
        ref_type, ref_id = parsed["type"], parsed["id"]
        target_map = {
            "message": messages,
            "mention": mentions,
            "claim": claims,
        }.get(ref_type)
        target = target_map.get(ref_id) if target_map is not None else None
        if not ref_type or ref_type not in {"message", "mention", "claim"}:
            errors.append(
                "%s.observable_support_refs[%d] has an unsupported type" % (owner, index)
            )
        if target is None:
            errors.append(
                "%s.observable_support_refs[%d] references unknown %s %s"
                % (owner, index, ref_type or "typed", ref_id)
            )
            continue
        if not _observable_ref_span_matches(parsed, target, ref_type, owner, errors):
            continue
        parsed["target"] = target
        parsed_refs.append(parsed)

    if not parsed_refs:
        return

    # Every declared positive reason must be bound to at least one typed ref.
    support_codes = record.get("supporting_slot_codes")
    if not isinstance(support_codes, (list, tuple)) or not support_codes:
        errors.append("%s same_event requires supporting_slot_codes" % owner)
    else:
        for support_code in support_codes:
            if not any(
                _observable_slot_matches(item.get("support_code", ""), str(support_code))
                for item in parsed_refs
            ):
                errors.append(
                    "%s support code %s is not bound to observable_support_refs"
                    % (owner, support_code)
                )

    # Ref locality is part of the evidence contract: a claim/mention ref must
    # be one of the two anchors, while a message ref must be a listed evidence
    # message or the source message for one of those anchors.
    anchor_claims = [left_anchor, right_anchor]
    anchor_message_ids = {
        str(item.get("message_id"))
        for item in anchor_claims
        if isinstance(item, Mapping) and _is_nonempty_string(item.get("message_id"))
    }
    anchor_mention_ids: set[str] = set()
    for item in anchor_claims:
        if not isinstance(item, Mapping):
            continue
        anchor_mention_ids.update(
            str(value)
            for value in item.get("event_mention_ids") or []
            if _is_nonempty_string(value)
        )
    evidence_message_ids = {
        str(value)
        for value in record.get("evidence_message_ids") or []
        if _is_nonempty_string(value)
    }
    for item in parsed_refs:
        ref_type, ref_id = item["type"], item["id"]
        side = item.get("side")
        side_anchor = left_anchor if side == "left" else right_anchor if side == "right" else None
        if side in {"left", "right"} and side_anchor is None:
            errors.append("%s observable_support_refs has invalid side" % owner)
            continue
        if ref_type == "claim":
            allowed = {left_id, right_id}
            if side == "left":
                allowed = {left_id}
            elif side == "right":
                allowed = {right_id}
            if ref_id not in allowed:
                errors.append("%s observable claim ref is outside relation anchors" % owner)
        elif ref_type == "mention":
            allowed_mentions = anchor_mention_ids
            if side_anchor is not None:
                allowed_mentions = {
                    str(value)
                    for value in side_anchor.get("event_mention_ids") or []
                    if _is_nonempty_string(value)
                }
            if ref_id not in allowed_mentions:
                errors.append("%s observable mention ref is outside relation anchors" % owner)
        elif ref_type == "message":
            if ref_id not in evidence_message_ids | anchor_message_ids:
                errors.append("%s observable message ref is outside relation evidence" % owner)

    # Shared action/core-object refs are useful evidence but are not event
    # identity.  A real identity signal must be present in an input message or
    # claim (or be an exact same-message/reply edge).  This intentionally does
    # not count block, segment, speaker, ordering, or time proximity.
    stable_instance = bool(
        _observable_signal_values(left_anchor) & _observable_signal_values(right_anchor)
    )
    left_message = messages.get(str((left_anchor or {}).get("message_id")))
    right_message = messages.get(str((right_anchor or {}).get("message_id")))
    left_message_id = str((left_anchor or {}).get("message_id") or "")
    right_message_id = str((right_anchor or {}).get("message_id") or "")
    if left_message_id and left_message_id == right_message_id:
        stable_instance = True
    if isinstance(left_message, Mapping) and isinstance(right_message, Mapping):
        stable_instance = stable_instance or bool(
            _observable_signal_values(left_message)
            & _observable_signal_values(right_message)
        )
        left_reply = left_message.get("reply_to_message_id")
        right_reply = right_message.get("reply_to_message_id")
        stable_instance = stable_instance or left_reply == right_message_id or right_reply == left_message_id
        if left_reply and right_reply and left_reply == right_reply:
            stable_instance = True
    for item in parsed_refs:
        code = item.get("support_code", "")
        signal = item.get("signal", "")
        target = item.get("target") or {}
        if signal in _OBSERVABLE_INSTANCE_SLOT_ALIASES or code in _OBSERVABLE_INSTANCE_SLOT_ALIASES:
            if item["type"] == "message":
                stable_instance = stable_instance or bool(_observable_signal_values(target))
            elif item["type"] == "claim":
                stable_instance = stable_instance or bool(_observable_signal_values(target))
            elif item["type"] == "mention":
                attrs = target.get("attributes") or {}
                stable_instance = stable_instance or bool(
                    {
                        str(attrs.get(field))
                        for field in _OBSERVABLE_INSTANCE_FIELDS
                        if _is_nonempty_string(attrs.get(field))
                    }
                )
    if not stable_instance:
        errors.append("%s same_event lacks algorithm-readable observable instance signal" % owner)


def _mention_entity_ids(record: Mapping[str, Any]) -> Tuple[str, ...]:
    values: List[str] = []
    for field_name in ("normalized_id", "normalized_entity_id", "entity_id"):
        value = record.get(field_name)
        if _is_nonempty_string(value):
            values.append(str(value))
    for field_name in ("target_entity_ids", "entity_ids"):
        value = record.get(field_name)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value if _is_nonempty_string(item))
    return tuple(dict.fromkeys(values))


def _relation_adjudication_id(record: Mapping[str, Any]) -> str:
    for field_name in ("adjudication_id", "final_adjudication_id"):
        value = record.get(field_name)
        if _is_nonempty_string(value):
            return str(value)
    value = record.get("adjudication_ids")
    if isinstance(value, (list, tuple)):
        for item in value:
            if _is_nonempty_string(item):
                return str(item)
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping):
        value = provenance.get("adjudication_ids")
        if isinstance(value, (list, tuple)):
            for item in value:
                if _is_nonempty_string(item):
                    return str(item)
    return ""


def _relation_missing_sides(record: Mapping[str, Any]) -> set:
    """Read explicit missing-A/B markers from final relation variants."""

    sides: set = set()
    for field_name in ("missing_side", "missing_sides", "source_missing_side", "source_missing_sides"):
        value = record.get(field_name)
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for item in values:
            if isinstance(item, str):
                normalized = item.strip().casefold().replace("_", "-")
                if normalized in {"a", "annotator-a", "left"}:
                    sides.add("a")
                elif normalized in {"b", "annotator-b", "right"}:
                    sides.add("b")
    for side, field_names in (
        (
            "a",
            (
                "missing_in_a",
                "annotator_a_missing",
                "source_a_missing",
                "missing_a",
                "a_missing",
            ),
        ),
        (
            "b",
            (
                "missing_in_b",
                "annotator_b_missing",
                "source_b_missing",
                "missing_b",
                "b_missing",
            ),
        ),
    ):
        if any(record.get(field_name) is True for field_name in field_names):
            sides.add(side)
    return sides


def _relation_source_label(record: Optional[Mapping[str, Any]], side: str) -> Any:
    if not isinstance(record, Mapping):
        return None
    field_name = "annotator_%s_label" % side
    value = record.get(field_name)
    if value is not None:
        return value
    # A source relation's own label is the source-side label when the explicit
    # A/B fields have not yet been materialized.
    return record.get("label")


def _has_relation_label(value: Any) -> bool:
    if not _is_nonempty_string(value):
        return False
    return str(value).strip().casefold() not in _MISSING_LABEL_VALUES


def _annotator_from_record(record: Mapping[str, Any]) -> str:
    explicit = record.get("annotator_id")
    if explicit:
        return str(explicit)
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get("created_by"):
        return str(provenance["created_by"])
    return ""


def _guide_from_record(record: Mapping[str, Any]) -> str:
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get("guide_version"):
        return str(provenance["guide_version"])
    return str(record.get("guide_version") or "")


def _forbidden_fields(value: Any, path: str = "$") -> List[str]:
    forbidden = {
        "raw_text",
        "raw_message",
        "raw_content",
        "sender_name",
        "chat_name",
        "media_path",
        "media_md5",
        "timestamp",
        "wxid",
        "wechat_id",
    }
    found: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = "%s.%s" % (path, key)
            if str(key) in forbidden:
                found.append(child_path)
            found.extend(_forbidden_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_fields(child, "%s[%d]" % (path, index)))
    return found


def _provenance(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("provenance")
    if not isinstance(value, Mapping):
        raise AnnotationFormatError("record %s provenance must be an object" % (_record_id(record),))
    return value


def _copy_safe_value(field_name: str, value: Any) -> Tuple[Any, bool]:
    """Return ``(value, omitted)`` for the public disagreement projection."""

    name = field_name.casefold()
    if name not in _SAFE_VALUE_FIELDS or any(part in name for part in _BODY_FIELD_PARTS):
        return None, True
    # IDs, offsets, labels and lists of them are safe.  Keep a defensive size
    # bound for custom future values; if it is not plainly scalar/list data,
    # omit rather than risk copying free-form content.
    if value is None or isinstance(value, (str, int, float, bool)):
        return deepcopy(value), False
    if isinstance(value, list):
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
            return deepcopy(value), False
        if field_name in {"evidence_spans"} and all(
            isinstance(item, Mapping)
            and set(item).issubset({"start", "end"})
            and isinstance(item.get("start"), int)
            and isinstance(item.get("end"), int)
            for item in value
        ):
            return deepcopy(value), False
    return None, True


def _public_side(present: bool, field_name: str, value: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"present": bool(present)}
    if not present:
        return result
    safe_value, omitted = _copy_safe_value(field_name, value)
    if omitted:
        result["value_omitted"] = True
    else:
        result["value"] = safe_value
    return result


def _claim_structural_key(record: Mapping[str, Any]) -> Optional[Tuple[Any, ...]]:
    message_id = str(record.get("message_id") or "")
    spans = record.get("evidence_spans") or []
    normalized_spans: List[Tuple[int, int]] = []
    for item in spans:
        if not isinstance(item, Mapping):
            return None
        try:
            normalized_spans.append((int(item.get("start")), int(item.get("end"))))
        except (TypeError, ValueError):
            return None
    # Deliberately exclude claim_type and target_entity_ids: both are
    # adjudicatable choices, not reasons to fail to align two claims.  In the
    # pilot each message has at most one claim; the message/evidence span key
    # therefore keeps target and type disagreements field-level.
    if message_id and normalized_spans:
        return ("claim_struct", message_id, tuple(sorted(normalized_spans)))
    return None


def _mention_structural_key(record: Mapping[str, Any]) -> Optional[Tuple[Any, ...]]:
    message_id = str(record.get("message_id") or "")
    mention_type = str(record.get("mention_type") or "")
    try:
        start, end = int(record.get("span_start")), int(record.get("span_end"))
    except (TypeError, ValueError):
        return None
    if message_id and end > start:
        # A message may legitimately carry multiple mention types over the
        # same span (for example an event trigger and a state).  Include the
        # type in the fallback key so those rows remain one-to-one alignable;
        # otherwise a valid multi-type annotation is reported as ambiguous.
        # The type is still compared as a normal field whenever IDs align, so
        # this only disambiguates the structural fallback.
        return ("mention_struct", message_id, start, end, mention_type)
    return None


def _relation_key(record: Mapping[str, Any], aliases: Mapping[str, str]) -> Tuple[Any, ...]:
    left = str(record.get("left_anchor_id") or "")
    right = str(record.get("right_anchor_id") or "")
    anchor_type = str(record.get("anchor_type") or "")
    left = aliases.get(left, left)
    right = aliases.get(right, right)
    if left and right:
        ordered = tuple(sorted((left, right)))
        return ("relation", anchor_type, ordered)
    return ("relation_id", _record_id(record, "relation"))


def _direct_key(record: Mapping[str, Any], record_type: str, aliases: Mapping[str, str]) -> Tuple[Any, ...]:
    if record_type == "relation":
        return _relation_key(record, aliases)
    return (record_type, "id", _type_specific_id(record, record_type))


def _fallback_key(
    record: Mapping[str, Any],
    record_type: str,
    aliases: Optional[Mapping[str, str]] = None,
) -> Optional[Tuple[Any, ...]]:
    aliases = aliases or {}
    if record_type == "claim":
        return _claim_structural_key(record)
    if record_type == "mention":
        return _mention_structural_key(record)
    if record_type == "cluster":
        values = tuple(sorted(aliases.get(str(value), str(value)) for value in record.get("claim_ids") or []))
        if values:
            return ("cluster_members", values)
        message_values = tuple(sorted(str(value) for value in record.get("member_message_ids") or []))
        return ("cluster_messages", message_values) if message_values else None
    if record_type == "presentation":
        values = tuple(sorted(aliases.get(str(value), str(value)) for value in record.get("source_claim_ids") or []))
        return ("presentation_sources", values) if values else None
    return None


@dataclass(frozen=True)
class AnnotationSet:
    """One independently authored annotation JSONL stream."""

    annotator_id: str
    guide_version: str
    dataset_version: str
    schema_version: str
    records: Tuple[Mapping[str, Any], ...]
    source_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "annotator_id": self.annotator_id,
            "guide_version": self.guide_version,
            "dataset_version": self.dataset_version,
            "schema_version": self.schema_version,
            "record_count": len(self.records),
        }


@dataclass(frozen=True)
class AlignedPair:
    """A/B alignment metadata; records remain private in memory only."""

    record_type: str
    alignment_key: Tuple[Any, ...]
    a_index: Optional[int]
    b_index: Optional[int]
    a_record_id: Optional[str]
    b_record_id: Optional[str]

    @property
    def anchor_id(self) -> str:
        return self.a_record_id or self.b_record_id or ""


@dataclass(frozen=True)
class Disagreement:
    disagreement_id: str
    record_type: str
    alignment_key: Tuple[Any, ...]
    anchor_id: str
    a_record_id: Optional[str]
    b_record_id: Optional[str]
    field: str
    kind: str
    a_present: bool
    b_present: bool
    a_public_value: Any = None
    b_public_value: Any = None
    a_value_omitted: bool = False
    b_value_omitted: bool = False
    reason_codes: Tuple[str, ...] = ()
    required_adjudication: bool = True
    guide_version: str = ANNOTATION_GUIDE_VERSION
    dataset_version: str = DATASET_VERSION

    def to_dict(self) -> Dict[str, Any]:
        # This is the only public serialization path for a disagreement.  It
        # never has access to the underlying records, and therefore cannot
        # accidentally include message/claim正文.
        result: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": self.dataset_version,
            "guide_version": self.guide_version,
            "tool_version": TOOL_VERSION,
            "disagreement_id": self.disagreement_id,
            "record_type": self.record_type,
            "alignment_key": _json_safe_key(self.alignment_key),
            "anchor_id": self.anchor_id,
            "annotator_a_record_id": self.a_record_id,
            "annotator_b_record_id": self.b_record_id,
            "field": self.field,
            "kind": self.kind,
            "reason_codes": list(self.reason_codes),
            "required_adjudication": self.required_adjudication,
            "annotator_a": _public_side(self.a_present, self.field, self.a_public_value)
            if self.a_present
            else {"present": False},
            "annotator_b": _public_side(self.b_present, self.field, self.b_public_value)
            if self.b_present
            else {"present": False},
            "provenance": {
                "source_record_ids": sorted(
                    set(value for value in (self.a_record_id, self.b_record_id) if value)
                ),
                "created_by": "comparison_tool",
                "guide_version": self.guide_version,
                "revision": 1,
            },
        }
        # Preserve explicit omission markers for text-bearing values, even
        # though the side object above intentionally does not carry them.
        if self.a_present and self.a_value_omitted:
            result["annotator_a"]["value_omitted"] = True
        if self.b_present and self.b_value_omitted:
            result["annotator_b"]["value_omitted"] = True
        return result


@dataclass(frozen=True)
class ComparisonResult:
    annotator_a: str
    annotator_b: str
    guide_version: str
    dataset_version: str
    pairs: Tuple[AlignedPair, ...]
    disagreements: Tuple[Disagreement, ...]

    @property
    def required_disagreement_ids(self) -> Tuple[str, ...]:
        return tuple(item.disagreement_id for item in self.disagreements if item.required_adjudication)

    def to_dict(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        by_field: Dict[str, int] = {}
        for item in self.disagreements:
            by_type[item.record_type] = by_type.get(item.record_type, 0) + 1
            by_field[item.field] = by_field.get(item.field, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": self.dataset_version,
            "guide_version": self.guide_version,
            "tool_version": TOOL_VERSION,
            "annotator_a": self.annotator_a,
            "annotator_b": self.annotator_b,
            "aligned_pair_count": sum(1 for pair in self.pairs if pair.a_index is not None and pair.b_index is not None),
            "a_only_count": sum(1 for pair in self.pairs if pair.a_index is not None and pair.b_index is None),
            "b_only_count": sum(1 for pair in self.pairs if pair.a_index is None and pair.b_index is not None),
            "disagreement_count": len(self.disagreements),
            "required_adjudication_count": len(self.required_disagreement_ids),
            "disagreement_counts_by_record_type": dict(sorted(by_type.items())),
            "disagreement_counts_by_field": dict(sorted(by_field.items())),
        }

    def write_jsonl(self, path: Path, *, overwrite: bool = False) -> Path:
        return write_disagreements_jsonl(path, self, overwrite=overwrite)


@dataclass(frozen=True)
class FreezeValidationResult:
    ok: bool
    errors: Tuple[str, ...]
    warnings: Tuple[str, ...] = ()
    required_disagreement_count: int = 0
    adjudicated_disagreement_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "required_disagreement_count": self.required_disagreement_count,
            "adjudicated_disagreement_count": self.adjudicated_disagreement_count,
        }


@dataclass(frozen=True)
class MergeResult:
    records: Tuple[Mapping[str, Any], ...]
    adjudications: Tuple[Mapping[str, Any], ...]
    unresolved_disagreement_ids: Tuple[str, ...]
    validation: FreezeValidationResult

    def to_dict(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for record in self.records:
            kind = _record_type(record)
            by_type[kind] = by_type.get(kind, 0) + 1
        return {
            "tool_version": TOOL_VERSION,
            "record_count": len(self.records),
            "record_counts_by_type": dict(sorted(by_type.items())),
            "adjudication_count": len(self.adjudications),
            "unresolved_disagreement_ids": list(self.unresolved_disagreement_ids),
            "validation": self.validation.to_dict(),
        }


def _json_safe_key(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_safe_key(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_key(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _validate_record_metadata(
    record: Mapping[str, Any],
    *,
    line_number: int,
    annotator_id: str,
    guide_version: str,
    dataset_version: str,
    schema_version: str,
) -> None:
    record_id = _record_id(record)
    if not record_id:
        raise AnnotationFormatError("line %d is missing record_id" % line_number)
    if record.get("schema_version") != schema_version:
        raise AnnotationFormatError("record %s has schema_version %r" % (record_id, record.get("schema_version")))
    if record.get("dataset_version") != dataset_version:
        raise AnnotationFormatError("record %s has dataset_version %r" % (record_id, record.get("dataset_version")))
    provenance = _provenance(record)
    record_guide = _guide_from_record(record)
    if record_guide != guide_version:
        raise AnnotationFormatError("record %s has guide_version %r, expected %r" % (record_id, record_guide, guide_version))
    record_annotator = _annotator_from_record(record)
    if record_annotator != annotator_id:
        raise AnnotationFormatError("record %s has annotator %r, expected %r" % (record_id, record_annotator, annotator_id))
    if str(provenance.get("created_by") or "") != annotator_id:
        raise AnnotationFormatError("record %s provenance.created_by must equal %s" % (record_id, annotator_id))
    if _forbidden_fields(record):
        raise AnnotationFormatError("record %s contains forbidden raw/private fields" % record_id)


def _resolve_annotation_metadata(
    records: Sequence[Mapping[str, Any]],
    *,
    annotator_id: Optional[str],
    guide_version: Optional[str],
    dataset_version: str,
    schema_version: str,
) -> Tuple[str, str]:
    if not records:
        if not annotator_id:
            raise AnnotationFormatError("empty annotation stream requires annotator_id")
        return str(annotator_id), str(guide_version or ANNOTATION_GUIDE_VERSION)
    observed_annotators = {_annotator_from_record(record) for record in records}
    observed_guides = {_guide_from_record(record) for record in records}
    if "" in observed_annotators:
        raise AnnotationFormatError("every annotation record needs annotator_id/provenance.created_by")
    if "" in observed_guides:
        raise AnnotationFormatError("every annotation record needs provenance.guide_version")
    resolved_annotator = str(annotator_id or next(iter(observed_annotators)))
    resolved_guide = str(guide_version or next(iter(observed_guides)))
    if observed_annotators != {resolved_annotator}:
        raise AnnotationFormatError("annotation stream contains multiple annotator IDs")
    if observed_guides != {resolved_guide}:
        raise AnnotationFormatError("annotation stream contains multiple guide versions")
    return resolved_annotator, resolved_guide


def load_annotation_jsonl(
    path: Path,
    *,
    annotator_id: Optional[str] = None,
    guide_version: Optional[str] = None,
    dataset_version: str = DATASET_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> AnnotationSet:
    """Load one independent A/B JSONL stream without contacting a source DB."""

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise AnnotationFormatError("annotation JSONL must be a file")
    parsed: List[Mapping[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnnotationFormatError("%s:%d invalid JSON: %s" % (source.name, line_number, exc)) from exc
        if not isinstance(value, Mapping):
            raise AnnotationFormatError("%s:%d must be a JSON object" % (source.name, line_number))
        parsed.append(dict(value))
    resolved_annotator, resolved_guide = _resolve_annotation_metadata(
        parsed,
        annotator_id=annotator_id,
        guide_version=guide_version,
        dataset_version=dataset_version,
        schema_version=schema_version,
    )
    seen_record_ids: set = set()
    for line_number, record in enumerate(parsed, 1):
        _validate_record_metadata(
            record,
            line_number=line_number,
            annotator_id=resolved_annotator,
            guide_version=resolved_guide,
            dataset_version=dataset_version,
            schema_version=schema_version,
        )
        record_id = _record_id(record)
        if record_id in seen_record_ids:
            raise AnnotationFormatError("duplicate record_id %s" % record_id)
        seen_record_ids.add(record_id)
    return AnnotationSet(
        annotator_id=resolved_annotator,
        guide_version=resolved_guide,
        dataset_version=dataset_version,
        schema_version=schema_version,
        records=tuple(deepcopy(record) for record in parsed),
        source_path=str(source),
    )


def _annotation_record_for_write(
    record: Mapping[str, Any],
    *,
    annotator_id: str,
    guide_version: str,
    dataset_version: str,
    schema_version: str,
) -> Dict[str, Any]:
    output = deepcopy(dict(record))
    type_name = _record_type(output)
    record_id = _record_id(output, type_name)
    if not record_id:
        raise AnnotationFormatError("annotation record is missing record_id")
    output.setdefault("record_id", record_id)
    output["record_type"] = type_name
    output["schema_version"] = schema_version
    output["dataset_version"] = dataset_version
    output["annotator_id"] = annotator_id
    output.setdefault("annotation_status", "draft")
    existing_provenance = output.get("provenance")
    provenance = dict(existing_provenance) if isinstance(existing_provenance, Mapping) else {}
    provenance["created_by"] = annotator_id
    provenance["guide_version"] = guide_version
    provenance.setdefault("source_record_ids", [record_id])
    provenance.setdefault("revision", 1)
    output["provenance"] = provenance
    _validate_record_metadata(
        output,
        line_number=1,
        annotator_id=annotator_id,
        guide_version=guide_version,
        dataset_version=dataset_version,
        schema_version=schema_version,
    )
    return output


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]], *, overwrite: bool) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError("refusing to overwrite %s" % destination)
    mode = "w" if overwrite else "x"
    with destination.open(mode, encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(_canonical(record) + "\n")
    return destination


def write_annotation_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    annotator_id: str,
    guide_version: str = ANNOTATION_GUIDE_VERSION,
    dataset_version: str = DATASET_VERSION,
    schema_version: str = SCHEMA_VERSION,
    overwrite: bool = False,
) -> Path:
    """Write a single independent annotation stream with provenance on each row."""

    if not str(annotator_id or ""):
        raise AnnotationFormatError("annotator_id is required")
    normalized = [
        _annotation_record_for_write(
            record,
            annotator_id=str(annotator_id),
            guide_version=str(guide_version),
            dataset_version=str(dataset_version),
            schema_version=str(schema_version),
        )
        for record in records
    ]
    ids = [_record_id(record) for record in normalized]
    if len(ids) != len(set(ids)):
        raise AnnotationFormatError("annotation records contain duplicate record_id")
    return _write_jsonl(path, normalized, overwrite=overwrite)


def _assert_compatible(a: AnnotationSet, b: AnnotationSet) -> None:
    if a.annotator_id == b.annotator_id:
        raise AnnotationFormatError("A/B streams must have different annotator IDs")
    if a.schema_version != b.schema_version:
        raise AnnotationFormatError("A/B schema versions differ")
    if a.dataset_version != b.dataset_version:
        raise AnnotationFormatError("A/B dataset versions differ")
    if a.guide_version != b.guide_version:
        raise AnnotationFormatError("A/B guide versions differ")


def _build_aliases(records: Sequence[Mapping[str, Any]], record_types: Sequence[str]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for record in records:
        type_name = _record_type(record)
        if type_name not in record_types:
            continue
        type_id = _record_id(record, type_name)
        record_id = _record_id(record)
        if type_id and record_id:
            aliases[type_id] = record_id
        if record_id and type_id:
            aliases[record_id] = record_id
    return aliases


def _index_by_key(
    records: Sequence[Mapping[str, Any]],
    indexes: Sequence[int],
    *,
    record_type: str,
    aliases: Mapping[str, str],
    fallback: bool,
) -> Dict[Tuple[Any, ...], List[int]]:
    result: Dict[Tuple[Any, ...], List[int]] = {}
    for index in indexes:
        record = records[index]
        key = _fallback_key(record, record_type, aliases) if fallback else _direct_key(record, record_type, aliases)
        if key is not None:
            result.setdefault(key, []).append(index)
    return result


def _align_records(a: AnnotationSet, b: AnnotationSet) -> Tuple[AlignedPair, ...]:
    a_types = [_record_type(record) for record in a.records]
    b_types = [_record_type(record) for record in b.records]
    # Claim/mention aliases allow a relation to align when two annotators used
    # locally different IDs but selected the same evidence span.  The
    # crosswalk is filled as claim/mention pairs are aligned, then used for
    # relation anchors below.  A relation therefore compares the same
    # underlying pair even when A and B generated different local IDs.
    b_to_a: Dict[str, str] = {}
    a_identity = {
        value: value
        for record in a.records
        for value in (_record_id(record), _type_specific_id(record, _record_type(record)))
        if value
    }
    pairs: List[AlignedPair] = []
    for record_type in sorted(set(a_types) | set(b_types)):
        a_indexes = [index for index, value in enumerate(a_types) if value == record_type]
        b_indexes = [index for index, value in enumerate(b_types) if value == record_type]
        a_direct = _index_by_key(a.records, a_indexes, record_type=record_type, aliases=a_identity, fallback=False)
        b_direct = _index_by_key(b.records, b_indexes, record_type=record_type, aliases=b_to_a, fallback=False)
        used_a: set = set()
        used_b: set = set()

        for key in sorted(set(a_direct) & set(b_direct), key=repr):
            if len(a_direct[key]) != 1 or len(b_direct[key]) != 1:
                raise AnnotationFormatError("ambiguous %s alignment key %r" % (record_type, key))
            ai, bi = a_direct[key][0], b_direct[key][0]
            used_a.add(ai)
            used_b.add(bi)
            pairs.append(
                AlignedPair(record_type, key, ai, bi, _record_id(a.records[ai], record_type), _record_id(b.records[bi], record_type))
            )
            if record_type in {"claim", "mention", "message", "cluster"}:
                a_type_id = _type_specific_id(a.records[ai], record_type)
                b_type_id = _type_specific_id(b.records[bi], record_type)
                a_record_id = _record_id(a.records[ai], record_type)
                b_record_id = _record_id(b.records[bi], record_type)
                if a_type_id and b_type_id:
                    b_to_a[b_type_id] = a_type_id
                if a_record_id and b_record_id:
                    b_to_a[b_record_id] = a_record_id

        a_remaining = [index for index in a_indexes if index not in used_a]
        b_remaining = [index for index in b_indexes if index not in used_b]
        # Relation direct keys use the A/B-local anchors.  If a relation did
        # not match in the first pass, a structural fallback is intentionally
        # not guessed: an unpaired relation must be adjudicated as a record.
        if record_type in {"claim", "mention", "cluster", "presentation"}:
            a_fallback = _index_by_key(a.records, a_remaining, record_type=record_type, aliases=a_identity, fallback=True)
            b_fallback = _index_by_key(b.records, b_remaining, record_type=record_type, aliases=b_to_a, fallback=True)
            for key in sorted(set(a_fallback) & set(b_fallback), key=repr):
                if len(a_fallback[key]) != 1 or len(b_fallback[key]) != 1:
                    raise AnnotationFormatError("ambiguous %s structural alignment key %r" % (record_type, key))
                ai, bi = a_fallback[key][0], b_fallback[key][0]
                used_a.add(ai)
                used_b.add(bi)
                pairs.append(
                    AlignedPair(record_type, key, ai, bi, _record_id(a.records[ai], record_type), _record_id(b.records[bi], record_type))
                )
                a_type_id = _type_specific_id(a.records[ai], record_type)
                b_type_id = _type_specific_id(b.records[bi], record_type)
                if a_type_id and b_type_id:
                    b_to_a[b_type_id] = a_type_id
                if a_type_id and b_type_id:
                    b_to_a[_record_id(b.records[bi])] = _record_id(a.records[ai])

        for ai in sorted(index for index in a_indexes if index not in used_a):
            key = _direct_key(a.records[ai], record_type, a_identity)
            pairs.append(AlignedPair(record_type, key, ai, None, _record_id(a.records[ai], record_type), None))
        for bi in sorted(index for index in b_indexes if index not in used_b):
            key = _direct_key(b.records[bi], record_type, b_to_a)
            pairs.append(AlignedPair(record_type, key, None, bi, None, _record_id(b.records[bi], record_type)))
    return tuple(sorted(pairs, key=lambda item: (item.record_type, repr(item.alignment_key), item.a_index or -1, item.b_index or -1)))


def compare_annotation_sets(a: AnnotationSet, b: AnnotationSet) -> ComparisonResult:
    """Align A/B annotations and return structure-only disagreements."""

    _assert_compatible(a, b)
    pairs = _align_records(a, b)
    disagreements: List[Disagreement] = []
    next_id = 1
    for pair in pairs:
        a_record = a.records[pair.a_index] if pair.a_index is not None else None
        b_record = b.records[pair.b_index] if pair.b_index is not None else None
        if a_record is None or b_record is None:
            missing_side = "a" if a_record is None else "b"
            disagreements.append(
                Disagreement(
                    disagreement_id="DISAGREEMENT_%06d" % next_id,
                    record_type=pair.record_type,
                    alignment_key=pair.alignment_key,
                    anchor_id=pair.anchor_id,
                    a_record_id=pair.a_record_id,
                    b_record_id=pair.b_record_id,
                    field="__record__",
                    kind="missing_in_%s" % missing_side,
                    a_present=a_record is not None,
                    b_present=b_record is not None,
                    reason_codes=("MISSING_RECORD_A" if missing_side == "a" else "MISSING_RECORD_B",),
                )
            )
            next_id += 1
            continue

        fields = (set(a_record) | set(b_record)) - _METADATA_FIELDS - _IDENTITY_FIELDS
        for field_name in sorted(fields):
            a_value = a_record.get(field_name, _MISSING)
            b_value = b_record.get(field_name, _MISSING)
            same = a_value is not _MISSING and b_value is not _MISSING and _canonical(a_value) == _canonical(b_value)
            if same:
                continue
            a_present = a_value is not _MISSING
            b_present = b_value is not _MISSING
            a_public, a_omitted = _copy_safe_value(field_name, None if a_value is _MISSING else a_value)
            b_public, b_omitted = _copy_safe_value(field_name, None if b_value is _MISSING else b_value)
            reasons = ["FIELD_VALUE_MISMATCH" if a_present and b_present else ("FIELD_MISSING_IN_A" if not a_present else "FIELD_MISSING_IN_B")]
            if field_name == "must_not_link" and (a_value is True or b_value is True):
                reasons.append("MNL_REQUIRES_ADJUDICATION")
            disagreements.append(
                Disagreement(
                    disagreement_id="DISAGREEMENT_%06d" % next_id,
                    record_type=pair.record_type,
                    alignment_key=pair.alignment_key,
                    anchor_id=pair.anchor_id,
                    a_record_id=pair.a_record_id,
                    b_record_id=pair.b_record_id,
                    field=field_name,
                    kind="value_mismatch" if a_present and b_present else ("missing_in_a" if not a_present else "missing_in_b"),
                    a_present=a_present,
                    b_present=b_present,
                    a_public_value=a_public,
                    b_public_value=b_public,
                    a_value_omitted=a_omitted,
                    b_value_omitted=b_omitted,
                    reason_codes=tuple(sorted(set(reasons))),
                )
            )
            next_id += 1

        # Any MNL assertion requires an adjudicator even when both annotators
        # independently made the same assertion.  This creates a single,
        # explicit field-level adjudication row without copying the relation
        # or its evidence text.
        a_mnl = a_record.get("must_not_link") is True
        b_mnl = b_record.get("must_not_link") is True
        has_mnl_disagreement = any(
            item.alignment_key == pair.alignment_key
            and item.record_type == pair.record_type
            and item.field == "must_not_link"
            for item in disagreements
        )
        if pair.record_type == "relation" and (a_mnl or b_mnl) and not has_mnl_disagreement:
            public_a, omitted_a = _copy_safe_value("must_not_link", a_record.get("must_not_link"))
            public_b, omitted_b = _copy_safe_value("must_not_link", b_record.get("must_not_link"))
            disagreements.append(
                Disagreement(
                    disagreement_id="DISAGREEMENT_%06d" % next_id,
                    record_type="relation",
                    alignment_key=pair.alignment_key,
                    anchor_id=pair.anchor_id,
                    a_record_id=pair.a_record_id,
                    b_record_id=pair.b_record_id,
                    field="must_not_link",
                    kind="mandatory_adjudication",
                    a_present=True,
                    b_present=True,
                    a_public_value=public_a,
                    b_public_value=public_b,
                    a_value_omitted=omitted_a,
                    b_value_omitted=omitted_b,
                    reason_codes=("MNL_REQUIRES_ADJUDICATION",),
                )
            )
            next_id += 1
    return ComparisonResult(
        annotator_a=a.annotator_id,
        annotator_b=b.annotator_id,
        guide_version=a.guide_version,
        dataset_version=a.dataset_version,
        pairs=pairs,
        disagreements=tuple(disagreements),
    )


def write_disagreements_jsonl(path: Path, comparison: ComparisonResult, *, overwrite: bool = False) -> Path:
    """Write one structure-only JSON object per disagreement."""

    return _write_jsonl(path, (item.to_dict() for item in comparison.disagreements), overwrite=overwrite)


def read_disagreements_jsonl(path: Path) -> Tuple[Mapping[str, Any], ...]:
    """Read a previously emitted structure-only disagreement list."""

    source = Path(path).expanduser().resolve(strict=True)
    rows: List[Mapping[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnnotationFormatError("%s:%d invalid JSON: %s" % (source.name, line_number, exc)) from exc
        if not isinstance(value, Mapping):
            raise AnnotationFormatError("%s:%d must contain a JSON object" % (source.name, line_number))
        # A report is a public projection; reject accidental text-bearing
        # fields if someone hand-edited it before feeding it to a later step.
        if _forbidden_fields(value):
            raise AnnotationFormatError("disagreement report contains forbidden raw/private fields")
        rows.append(dict(value))
    return tuple(rows)


def _adjudication_record_for_write(
    record: Mapping[str, Any],
    *,
    adjudicator_id: str,
    guide_version: str,
    dataset_version: str,
    schema_version: str,
) -> Dict[str, Any]:
    output = deepcopy(dict(record))
    adjudication_id = str(output.get("adjudication_id") or output.get("record_id") or "")
    disagreement_id = str(output.get("disagreement_id") or "")
    if not adjudication_id or not disagreement_id:
        raise AnnotationFormatError("adjudication requires adjudication_id and disagreement_id")
    if not str(output.get("record_id") or ""):
        output["record_id"] = adjudication_id
    output["adjudication_id"] = adjudication_id
    output["record_type"] = "adjudication"
    output["schema_version"] = schema_version
    output["dataset_version"] = dataset_version
    output["annotator_id"] = adjudicator_id
    output["annotation_status"] = "adjudicated"
    provenance = dict(output.get("provenance") or {})
    provenance.update({"created_by": adjudicator_id, "guide_version": guide_version})
    provenance.setdefault("source_record_ids", [disagreement_id])
    provenance.setdefault("revision", 1)
    output["provenance"] = provenance
    if str(output.get("guide_version") or guide_version) != guide_version:
        raise AnnotationFormatError("adjudication %s has mismatched guide version" % adjudication_id)
    return output


def write_adjudications_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    adjudicator_id: str = DEFAULT_ADJUDICATOR,
    guide_version: str = ANNOTATION_GUIDE_VERSION,
    dataset_version: str = DATASET_VERSION,
    schema_version: str = SCHEMA_VERSION,
    overwrite: bool = False,
) -> Path:
    normalized = [
        _adjudication_record_for_write(
            record,
            adjudicator_id=adjudicator_id,
            guide_version=guide_version,
            dataset_version=dataset_version,
            schema_version=schema_version,
        )
        for record in records
    ]
    ids = [str(item["adjudication_id"]) for item in normalized]
    disagreement_ids = [str(item["disagreement_id"]) for item in normalized]
    if len(ids) != len(set(ids)) or len(disagreement_ids) != len(set(disagreement_ids)):
        raise AnnotationFormatError("adjudications contain duplicate IDs")
    return _write_jsonl(path, normalized, overwrite=overwrite)


def load_adjudications_jsonl(
    path: Path,
    *,
    adjudicator_id: Optional[str] = None,
    guide_version: str = ANNOTATION_GUIDE_VERSION,
    dataset_version: str = DATASET_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> Tuple[Mapping[str, Any], ...]:
    source = Path(path).expanduser().resolve(strict=True)
    rows: List[Mapping[str, Any]] = []
    seen_adjudication_ids: set = set()
    seen_disagreement_ids: set = set()
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnnotationFormatError("%s:%d invalid JSON: %s" % (source.name, line_number, exc)) from exc
        if not isinstance(value, Mapping):
            raise AnnotationFormatError("%s:%d must contain a JSON object" % (source.name, line_number))
        record = dict(value)
        adjudication_id = str(record.get("adjudication_id") or record.get("record_id") or "")
        disagreement_id = str(record.get("disagreement_id") or "")
        if not adjudication_id or not disagreement_id:
            raise AnnotationFormatError("line %d missing adjudication/disagreement ID" % line_number)
        if adjudication_id in seen_adjudication_ids:
            raise AnnotationFormatError("duplicate adjudication_id %s" % adjudication_id)
        if disagreement_id in seen_disagreement_ids:
            raise AnnotationFormatError("duplicate disagreement_id %s" % disagreement_id)
        seen_adjudication_ids.add(adjudication_id)
        seen_disagreement_ids.add(disagreement_id)
        if record.get("schema_version") != schema_version or record.get("dataset_version") != dataset_version:
            raise AnnotationFormatError("adjudication %s has incompatible dataset metadata" % adjudication_id)
        record_guide = _guide_from_record(record)
        if record_guide != guide_version:
            raise AnnotationFormatError("adjudication %s has guide_version %r" % (adjudication_id, record_guide))
        record_adjudicator = _annotator_from_record(record)
        if adjudicator_id and record_adjudicator != adjudicator_id:
            raise AnnotationFormatError("adjudication %s has adjudicator %r" % (adjudication_id, record_adjudicator))
        if not record_adjudicator:
            raise AnnotationFormatError("adjudication %s is missing provenance.created_by" % adjudication_id)
        if str(record.get("decision") or "") not in {"accept_a", "accept_b", "custom", "merge"}:
            raise AnnotationFormatError("adjudication %s has invalid decision" % adjudication_id)
        if not str(record.get("record_type") or "") or not str(record.get("field") or ""):
            raise AnnotationFormatError("adjudication %s requires record_type and field" % adjudication_id)
        rows.append(record)
    return tuple(rows)


def _coerce_adjudications(
    adjudications: Iterable[Mapping[str, Any]],
    *,
    guide_version: str,
    dataset_version: str,
) -> Tuple[Mapping[str, Any], ...]:
    rows = tuple(deepcopy(dict(item)) for item in adjudications)
    by_disagreement: Dict[str, Mapping[str, Any]] = {}
    for row in rows:
        disagreement_id = str(row.get("disagreement_id") or "")
        adjudication_id = str(row.get("adjudication_id") or row.get("record_id") or "")
        if not disagreement_id or not adjudication_id:
            raise AnnotationFormatError("adjudication requires IDs")
        if disagreement_id in by_disagreement:
            raise AnnotationFormatError("duplicate adjudication for %s" % disagreement_id)
        if row.get("schema_version") != SCHEMA_VERSION:
            raise AnnotationFormatError("adjudication %s has incompatible schema_version" % adjudication_id)
        if row.get("dataset_version") != dataset_version:
            raise AnnotationFormatError("adjudication %s has incompatible dataset_version" % adjudication_id)
        row_guide = _guide_from_record(row)
        if row_guide not in {"", guide_version}:
            raise AnnotationFormatError("adjudication %s has incompatible guide_version" % adjudication_id)
        provenance = row.get("provenance")
        if not isinstance(provenance, Mapping) or not provenance.get("created_by"):
            raise AnnotationFormatError("adjudication %s is missing provenance.created_by" % adjudication_id)
        if not row_guide:
            raise AnnotationFormatError("adjudication %s is missing provenance.guide_version" % adjudication_id)
        if str(provenance.get("created_by")) != str(row.get("annotator_id") or provenance.get("created_by")):
            raise AnnotationFormatError("adjudication %s has inconsistent adjudicator provenance" % adjudication_id)
        if str(row.get("decision") or "") not in {"accept_a", "accept_b", "custom", "merge"}:
            raise AnnotationFormatError("adjudication %s has invalid decision" % adjudication_id)
        by_disagreement[disagreement_id] = row
    return rows


def build_adjudication(
    disagreement: Disagreement,
    *,
    decision: str,
    adjudicator_id: str = DEFAULT_ADJUDICATOR,
    final_value: Any = _MISSING,
    final_record: Optional[Mapping[str, Any]] = None,
    reason_codes: Sequence[str] = (),
    evidence_ids: Sequence[str] = (),
    override_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a minimal adjudication row for a comparison result."""

    if decision not in {"accept_a", "accept_b", "custom", "merge"}:
        raise AnnotationFormatError("invalid adjudication decision %s" % decision)
    adjudication_id = "ADJ_%s" % disagreement.disagreement_id
    row: Dict[str, Any] = {
        "record_id": adjudication_id,
        "adjudication_id": adjudication_id,
        "disagreement_id": disagreement.disagreement_id,
        "record_type": disagreement.record_type,
        "anchor_id": disagreement.anchor_id,
        "field": disagreement.field,
        "decision": decision,
        "reason_codes": list(reason_codes),
        "evidence_ids": list(evidence_ids),
        "provenance": {
            "created_by": adjudicator_id,
            "guide_version": disagreement.guide_version,
            "source_record_ids": [disagreement.anchor_id] if disagreement.anchor_id else [],
            "revision": 1,
        },
        "schema_version": SCHEMA_VERSION,
        "dataset_version": disagreement.dataset_version,
        "annotation_status": "adjudicated",
        "annotator_id": adjudicator_id,
    }
    if final_value is not _MISSING:
        row["final_value"] = deepcopy(final_value)
    if final_record is not None:
        row["final_record"] = deepcopy(dict(final_record))
    if override_reason is not None:
        row["override_reason"] = override_reason
    return row


def _adjudication_index(adjudications: Iterable[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for row in adjudications:
        disagreement_id = str(row.get("disagreement_id") or "")
        if not disagreement_id:
            raise AnnotationFormatError("adjudication missing disagreement_id")
        if disagreement_id in result:
            raise AnnotationFormatError("duplicate adjudication for %s" % disagreement_id)
        result[disagreement_id] = row
    return result


def _merged_record_provenance(
    a_record: Optional[Mapping[str, Any]],
    b_record: Optional[Mapping[str, Any]],
    *,
    adjudicator_id: str,
    guide_version: str,
    adjudication_ids: Sequence[str],
) -> Dict[str, Any]:
    source_ids: List[str] = []
    revisions: List[int] = []
    for record in (a_record, b_record):
        if not record:
            continue
        provenance = record.get("provenance")
        if isinstance(provenance, Mapping):
            source_ids.extend(str(value) for value in provenance.get("source_record_ids") or [])
            try:
                revisions.append(int(provenance.get("revision", 1)))
            except (TypeError, ValueError):
                revisions.append(1)
    return {
        "source_record_ids": sorted(set(source_ids)),
        "created_by": adjudicator_id,
        "guide_version": guide_version,
        "revision": max(revisions or [1]) + 1,
        "adjudication_ids": sorted(set(str(value) for value in adjudication_ids)),
    }


def _adjudication_value(
    row: Mapping[str, Any],
    *,
    field_name: str,
    a_record: Optional[Mapping[str, Any]],
    b_record: Optional[Mapping[str, Any]],
) -> Any:
    decision = str(row.get("decision") or "")
    if decision == "accept_a":
        if a_record is None or field_name not in a_record:
            raise AnnotationFormatError("adjudication %s accepts missing A value" % (row.get("adjudication_id"),))
        return deepcopy(a_record[field_name])
    if decision == "accept_b":
        if b_record is None or field_name not in b_record:
            raise AnnotationFormatError("adjudication %s accepts missing B value" % (row.get("adjudication_id"),))
        return deepcopy(b_record[field_name])
    if "final_value" not in row:
        raise AnnotationFormatError("adjudication %s requires final_value for %s" % (row.get("adjudication_id"), decision))
    return deepcopy(row["final_value"])


def _ensure_final_relation_sources(
    output: MutableMapping[str, Any],
    a_record: Optional[Mapping[str, Any]],
    b_record: Optional[Mapping[str, Any]],
    *,
    adjudication_ids: Sequence[str] = (),
) -> None:
    """Materialize A/B relation labels and missing-side provenance.

    A relation chosen during merge must remain auditable after the A/B source
    rows are no longer adjacent to it.  Older hand-authored streams only had a
    ``label`` field, so derive the explicit source labels from each source row
    rather than making those streams fail solely because the new fields were
    not present at authoring time.
    """

    if _record_type(output) != "relation":
        return
    a_label = _relation_source_label(a_record, "a")
    b_label = _relation_source_label(b_record, "b")
    if a_record is not None and not _has_relation_label(output.get("annotator_a_label")) and a_label is not None:
        output["annotator_a_label"] = deepcopy(a_label)
    if b_record is not None and not _has_relation_label(output.get("annotator_b_label")) and b_label is not None:
        output["annotator_b_label"] = deepcopy(b_label)
    missing = set(_relation_missing_sides(output))
    if a_record is None:
        missing.add("a")
    if b_record is None:
        missing.add("b")
    if missing:
        # Keep a deterministic scalar marker for the common one-sided case;
        # retain both sides in ``missing_sides`` when a custom final record has
        # no source on either side.
        output["missing_side"] = "a" if missing == {"a"} else ("b" if missing == {"b"} else None)
        output["missing_sides"] = sorted(missing)
    if adjudication_ids and not _is_nonempty_string(output.get("adjudication_id")):
        output["adjudication_id"] = str(adjudication_ids[0])


def merge_adjudicated_annotations(
    a: AnnotationSet,
    b: AnnotationSet,
    comparison: ComparisonResult,
    adjudications: Iterable[Mapping[str, Any]],
    *,
    adjudicator_id: str = DEFAULT_ADJUDICATOR,
    require_complete: bool = True,
) -> MergeResult:
    """Apply field-level adjudications and return a private merged stream."""

    _assert_compatible(a, b)
    if comparison.annotator_a != a.annotator_id or comparison.annotator_b != b.annotator_id:
        raise AnnotationFormatError("comparison does not belong to the supplied A/B streams")
    rows = _coerce_adjudications(
        adjudications,
        guide_version=comparison.guide_version,
        dataset_version=comparison.dataset_version,
    )
    adjudication_map = _adjudication_index(rows)
    required_ids = set(comparison.required_disagreement_ids)
    unresolved = tuple(sorted(required_ids - set(adjudication_map)))
    if unresolved and require_complete:
        raise UnresolvedDisagreementsError("unresolved disagreements: %s" % ",".join(unresolved))
    pair_disagreements: Dict[Tuple[str, Tuple[Any, ...]], List[Disagreement]] = {}
    for disagreement in comparison.disagreements:
        pair_disagreements.setdefault((disagreement.record_type, disagreement.alignment_key), []).append(disagreement)

    merged: List[Mapping[str, Any]] = []
    for pair in comparison.pairs:
        a_record = deepcopy(dict(a.records[pair.a_index])) if pair.a_index is not None else None
        b_record = deepcopy(dict(b.records[pair.b_index])) if pair.b_index is not None else None
        disputes = pair_disagreements.get((pair.record_type, pair.alignment_key), [])
        adjudication_ids: List[str] = []
        output: Optional[Dict[str, Any]] = None
        record_dispute = next((item for item in disputes if item.field == "__record__"), None)
        if record_dispute is not None:
            row = adjudication_map.get(record_dispute.disagreement_id)
            if row is not None:
                adjudication_ids.append(str(row.get("adjudication_id") or row.get("record_id")))
                decision = str(row.get("decision") or "")
                if decision == "accept_a":
                    if a_record is None:
                        raise AnnotationFormatError("record adjudication accepts missing A record")
                    output = a_record
                elif decision == "accept_b":
                    if b_record is None:
                        raise AnnotationFormatError("record adjudication accepts missing B record")
                    output = b_record
                elif isinstance(row.get("final_record"), Mapping):
                    output = deepcopy(dict(row["final_record"]))
                else:
                    raise AnnotationFormatError("record adjudication requires final_record")
            elif require_complete:
                raise UnresolvedDisagreementsError("unresolved disagreement %s" % record_dispute.disagreement_id)
        else:
            output = a_record if a_record is not None else b_record
            if output is None:
                continue

        if output is None:
            # In an intentionally partial merge, retain the available record
            # but leave its status as draft; the freeze gate will reject it.
            output = a_record or b_record
        if output is None:
            continue
        output = deepcopy(output)
        for disagreement in disputes:
            if disagreement.field == "__record__":
                continue
            row = adjudication_map.get(disagreement.disagreement_id)
            if row is None:
                continue
            adjudication_id = str(row.get("adjudication_id") or row.get("record_id") or "")
            adjudication_ids.append(adjudication_id)
            output[disagreement.field] = _adjudication_value(
                row,
                field_name=disagreement.field,
                a_record=a_record,
                b_record=b_record,
            )
            if disagreement.record_type == "relation" and row.get("override_reason"):
                output["override_reason"] = str(row["override_reason"])

        output["schema_version"] = SCHEMA_VERSION
        output["dataset_version"] = comparison.dataset_version
        output["record_id"] = _record_id(output, pair.record_type) or pair.anchor_id
        output["record_type"] = pair.record_type
        output["annotator_id"] = adjudicator_id
        output["annotation_status"] = "adjudicated"
        output["provenance"] = _merged_record_provenance(
            a_record,
            b_record,
            adjudicator_id=adjudicator_id,
            guide_version=comparison.guide_version,
            adjudication_ids=adjudication_ids,
        )
        if pair.record_type == "relation":
            _ensure_final_relation_sources(
                output,
                a_record,
                b_record,
                adjudication_ids=adjudication_ids,
            )
        merged.append(output)

    validation = validate_freeze_readiness(
        comparison,
        rows,
        merged_records=merged,
    )
    return MergeResult(
        records=tuple(merged),
        adjudications=rows,
        unresolved_disagreement_ids=unresolved,
        validation=validation,
    )


def _validate_final_relation_sources(
    relations: Sequence[Mapping[str, Any]],
    *,
    adjudication_ids: Optional[set] = None,
) -> List[str]:
    """Require auditable A/B labels on every final relation row.

    A merged relation normally carries both source labels.  A one-sided
    relation is also valid, but only when the absent side is explicitly marked
    and the final row points at an adjudication.  Merely retaining the chosen
    ``label`` would otherwise make it impossible to tell whether the other
    annotator was missing or silently discarded.
    """

    errors: List[str] = []
    known_adjudications = adjudication_ids or set()
    for index, record in enumerate(relations):
        relation_id = str(record.get("relation_id") or record.get("record_id") or index)
        a_label = record.get("annotator_a_label")
        b_label = record.get("annotator_b_label")
        a_present = _has_relation_label(a_label)
        b_present = _has_relation_label(b_label)
        for side, value, present in (("a", a_label, a_present), ("b", b_label, b_present)):
            if value is not None and present and str(value).strip() not in _RELATION_LABELS:
                errors.append("relation %s annotator_%s_label is not a valid relation label" % (relation_id, side))
        expected_missing = {side for side, present in (("a", a_present), ("b", b_present)) if not present}
        if not expected_missing:
            continue
        declared_missing = _relation_missing_sides(record)
        adjudication_id = _relation_adjudication_id(record)
        if declared_missing != expected_missing:
            missing_names = ",".join(sorted(expected_missing))
            errors.append(
                "relation %s is missing annotator_%s_label without an explicit missing-side marker"
                % (relation_id, missing_names)
            )
            continue
        if not adjudication_id:
            errors.append(
                "relation %s missing side(s) %s require adjudication"
                % (relation_id, ",".join(sorted(expected_missing)))
            )
        elif known_adjudications and adjudication_id not in known_adjudications:
            errors.append("relation %s references unknown adjudication %s" % (relation_id, adjudication_id))
    return errors


def _validate_annotation_graph(
    value: Any,
    *,
    adjudications: Iterable[Mapping[str, Any]] = (),
    phase: str = "pre_release",
) -> List[str]:
    """Validate cross-record semantic graph invariants for offline release.

    ``semantic_gold.validate_contract_dataset`` validates each contract file
    in isolation.  This companion gate validates the relationships between
    files and is deliberately kept here so it can be exercised using only
    redacted/synthetic records.  The ``phase`` argument documents the call
    site and leaves room for a future draft-only mode; pre-release and freeze
    currently enforce the same graph safety invariants.
    """

    del phase  # The release boundary is intentionally strict for both modes.
    collections = _graph_collections(value)
    errors: List[str] = []
    if isinstance(value, Mapping) and any(name in value for name in _GRAPH_COLLECTION_TYPES):
        active_types = {
            type_name
            for collection_name, type_name in _GRAPH_COLLECTION_TYPES.items()
            if collection_name in value
        }
    else:
        active_types = {
            _record_type(row)
            for rows in collections.values()
            for row in rows
        }
    lookups: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    for collection_name, type_name in _GRAPH_COLLECTION_TYPES.items():
        lookups[type_name] = _build_graph_lookup(collections[collection_name], type_name, errors)

    messages = lookups["message"]
    mentions = lookups["mention"]
    claims = lookups["claim"]
    relations = lookups["relation"]
    clusters = lookups["cluster"]
    presentations = lookups["presentation"]

    def check_reference(
        owner: str,
        field_name: str,
        values: Sequence[str],
        target: Mapping[str, Mapping[str, Any]],
        target_type: str,
    ) -> None:
        # A flat merged stream may intentionally contain only claims and
        # relations; without a message collection there is no target universe
        # against which to check message IDs.  A contract mapping, in
        # contrast, declares every collection (including an empty one), so an
        # FK into a declared empty collection is correctly rejected.
        if target_type not in active_types:
            return
        for value_item in values:
            if value_item not in target:
                errors.append("%s.%s references unknown %s %s" % (owner, field_name, target_type, value_item))

    # Basic typed foreign keys for mentions and claims.
    for index, record in enumerate(collections["mentions"]):
        mention_id = str(record.get("mention_id") or record.get("record_id") or index)
        owner = "mention %s" % mention_id
        message_id = _graph_id_value(record, "message_id", owner, errors, required=True)
        if message_id:
            check_reference(owner, "message_id", [message_id], messages, "message")

    for index, record in enumerate(collections["claims"]):
        claim_id = str(record.get("claim_id") or record.get("record_id") or index)
        owner = "claim %s" % claim_id
        message_id = _graph_id_value(record, "message_id", owner, errors, required=True)
        if message_id:
            check_reference(owner, "message_id", [message_id], messages, "message")
        event_mentions = _graph_id_list(record, "event_mention_ids", owner, errors)
        check_reference(owner, "event_mention_ids", event_mentions, mentions, "mention")
        context_messages = _graph_id_list(record, "context_message_ids", owner, errors)
        check_reference(owner, "context_message_ids", context_messages, messages, "message")
        # Entity arrays are identifiers, not free-form evidence.  Checking
        # their type here catches accidental object/string mixing before core
        # entity support is evaluated below.
        _graph_id_list(record, "target_entity_ids", owner, errors)

    # Relations: typed anchors, message evidence, finite labels and final A/B
    # source provenance.  event_seed anchors are intentionally opaque because
    # the contract does not define a separate event_seed collection.
    relation_rows = collections["relations"]
    known_adjudication_ids: set = set()
    for row in adjudications:
        if not isinstance(row, Mapping):
            continue
        for field_name in ("adjudication_id", "record_id"):
            if _is_nonempty_string(row.get(field_name)):
                known_adjudication_ids.add(str(row[field_name]))
    for row in collections["adjudications"]:
        for field_name in ("adjudication_id", "record_id"):
            if _is_nonempty_string(row.get(field_name)):
                known_adjudication_ids.add(str(row[field_name]))
    errors.extend(
        _validate_final_relation_sources(
            relation_rows,
            adjudication_ids=known_adjudication_ids,
        )
    )
    seen_relation_pairs: set = set()
    for index, record in enumerate(relation_rows):
        relation_id = str(record.get("relation_id") or record.get("record_id") or index)
        owner = "relation %s" % relation_id
        left = _graph_id_value(record, "left_anchor_id", owner, errors, required=True)
        right = _graph_id_value(record, "right_anchor_id", owner, errors, required=True)
        anchor_type = str(record.get("anchor_type") or "claim")
        if anchor_type not in _ANCHOR_TYPES:
            errors.append("relation %s has invalid anchor_type %s" % (relation_id, anchor_type))
        elif anchor_type == "claim":
            check_reference(owner, "left_anchor_id", [left] if left else [], claims, "claim")
            check_reference(owner, "right_anchor_id", [right] if right else [], claims, "claim")
        elif anchor_type == "mention":
            check_reference(owner, "left_anchor_id", [left] if left else [], mentions, "mention")
            check_reference(owner, "right_anchor_id", [right] if right else [], mentions, "mention")
        if left and right:
            if left >= right:
                errors.append("relation %s anchors are not in canonical order" % relation_id)
            pair_key = (anchor_type, left, right)
            if pair_key in seen_relation_pairs:
                errors.append("duplicate relation pair %s" % (pair_key,))
            seen_relation_pairs.add(pair_key)
        evidence_messages = _graph_id_list(record, "evidence_message_ids", owner, errors)
        check_reference(owner, "evidence_message_ids", evidence_messages, messages, "message")
        label = record.get("label")
        if label not in _RELATION_LABELS:
            errors.append("relation %s has invalid label" % relation_id)
        if "must_not_link" in record and not isinstance(record.get("must_not_link"), bool):
            errors.append("relation %s must_not_link must be boolean" % relation_id)
        _graph_id_list(record, "must_not_link_reason_codes", owner, errors)
        adjudication_id = _relation_adjudication_id(record)
        if adjudication_id and known_adjudication_ids and adjudication_id not in known_adjudication_ids:
            errors.append("relation %s references unknown adjudication %s" % (relation_id, adjudication_id))
        _validate_relation_observable_support(
            record,
            relation_id=relation_id,
            claims=claims,
            mentions=mentions,
            messages=messages,
            errors=errors,
        )

    # Build claim/mention/message membership before checking event edges.
    cluster_claims: Dict[str, List[str]] = {}
    claim_clusters: Dict[str, List[str]] = {}
    cluster_mentions: Dict[str, List[str]] = {}
    cluster_messages: Dict[str, List[str]] = {}
    cluster_types: Dict[str, str] = {}
    for index, record in enumerate(collections["clusters"]):
        cluster_id = str(record.get("cluster_id") or record.get("record_id") or index)
        owner = "cluster %s" % cluster_id
        cluster_type = str(record.get("cluster_type") or "")
        if cluster_type not in _CLUSTER_TYPES:
            errors.append("cluster %s has invalid cluster_type" % cluster_id)
        cluster_types[cluster_id] = cluster_type
        claim_ids = _graph_id_list(record, "claim_ids", owner, errors)
        mention_ids = _graph_id_list(record, "mention_ids", owner, errors)
        member_message_ids = _graph_id_list(record, "member_message_ids", owner, errors)
        relation_ids = _graph_id_list(record, "relation_ids", owner, errors)
        if len(claim_ids) != len(set(claim_ids)):
            errors.append("cluster %s repeats a claim" % cluster_id)
        if len(mention_ids) != len(set(mention_ids)):
            errors.append("cluster %s repeats a mention" % cluster_id)
        if len(member_message_ids) != len(set(member_message_ids)):
            errors.append("cluster %s repeats a member message" % cluster_id)
        check_reference(owner, "claim_ids", claim_ids, claims, "claim")
        check_reference(owner, "mention_ids", mention_ids, mentions, "mention")
        check_reference(owner, "member_message_ids", member_message_ids, messages, "message")
        check_reference(owner, "relation_ids", relation_ids, relations, "relation")
        for field_name in ("start_message_id", "end_message_id"):
            message_id = _graph_id_value(record, field_name, owner, errors)
            if message_id:
                check_reference(owner, field_name, [message_id], messages, "message")
        cluster_claims[cluster_id] = claim_ids
        cluster_mentions[cluster_id] = mention_ids
        cluster_messages[cluster_id] = member_message_ids
        for claim_id in claim_ids:
            claim_clusters.setdefault(claim_id, []).append(cluster_id)
        if record.get("core_entity_ids") is not None:
            _graph_id_list(record, "core_entity_ids", owner, errors)

    if clusters and claims:
        for claim_id in sorted(claims):
            assigned = claim_clusters.get(claim_id, [])
            if not assigned:
                errors.append("claim %s is not assigned to a cluster" % claim_id)
            elif len(assigned) > 1:
                errors.append("claim %s appears in multiple clusters: %s" % (claim_id, ",".join(sorted(assigned))))

    # Cluster evidence and core-entity support.  Event clusters must expose
    # exactly the mentions attached to their member claims; this catches both
    # omitted and unrelated mention IDs.
    for cluster_id, record in ((key, clusters[key]) for key in clusters):
        owner = "cluster %s" % cluster_id
        claim_ids = cluster_claims.get(cluster_id, [])
        mention_ids = cluster_mentions.get(cluster_id, [])
        member_message_ids = cluster_messages.get(cluster_id, [])
        claim_mention_union: set = set()
        supported_entities: set = set()
        claim_message_union: set = set()
        for claim_id in claim_ids:
            claim = claims.get(claim_id)
            if claim is None:
                continue
            claim_mention_union.update(
                _graph_id_list(claim, "event_mention_ids", "claim %s" % claim_id, errors)
            )
            supported_entities.update(
                str(value)
                for value in claim.get("target_entity_ids") or []
                if _is_nonempty_string(value)
            )
            message_id = claim.get("message_id")
            if _is_nonempty_string(message_id):
                claim_message_union.add(str(message_id))
        for mention_id in mention_ids:
            mention = mentions.get(mention_id)
            if mention is None:
                continue
            supported_entities.update(_mention_entity_ids(mention))
            mention_message_id = mention.get("message_id")
            if _is_nonempty_string(mention_message_id):
                if member_message_ids and str(mention_message_id) not in member_message_ids:
                    errors.append("%s mention %s is outside member_message_ids" % (owner, mention_id))
        if mention_ids or claim_mention_union:
            missing_mentions = sorted(claim_mention_union - set(mention_ids))
            extra_mentions = sorted(set(mention_ids) - claim_mention_union)
            if missing_mentions or extra_mentions:
                errors.append(
                    "%s mention coverage mismatch (missing=%s extra=%s)"
                    % (owner, ",".join(missing_mentions) or "none", ",".join(extra_mentions) or "none")
                )
        if member_message_ids:
            for message_id in sorted(claim_message_union - set(member_message_ids)):
                errors.append("%s claim evidence message %s is outside member_message_ids" % (owner, message_id))
        core_entity_ids = _graph_id_list(record, "core_entity_ids", owner, errors)
        for entity_id in core_entity_ids:
            if entity_id not in supported_entities:
                errors.append("%s core_entity %s lacks member claim/mention support" % (owner, entity_id))

        context_message_ids = set(member_message_ids) | claim_message_union
        context_message_ids.update(
            str(mentions[mention_id].get("message_id"))
            for mention_id in mention_ids
            if mention_id in mentions and _is_nonempty_string(mentions[mention_id].get("message_id"))
        )
        context_only_messages = sorted(
            message_id for message_id in context_message_ids
            if _is_context_only_record(messages.get(message_id))
        )
        context_only_claims = sorted(
            claim_id for claim_id in claim_ids
            if _is_context_only_record(claims.get(claim_id))
            or _is_context_only_record(messages.get(str((claims.get(claim_id) or {}).get("message_id"))))
        )
        if cluster_types.get(cluster_id) == "event":
            if context_only_messages or context_only_claims:
                errors.append(
                    "%s event cluster contains context_only evidence (messages=%s claims=%s)"
                    % (owner, ",".join(context_only_messages) or "none", ",".join(context_only_claims) or "none")
                )
            for mention_id in mention_ids:
                mention = mentions.get(mention_id)
                if mention is not None and _is_context_only_record(messages.get(str(mention.get("message_id")))):
                    errors.append("%s event cluster contains context_only mention %s" % (owner, mention_id))

    # A same_event edge is the only relation allowed to connect event-cluster
    # claims.  Conversely, all claims in one event cluster must be connected by
    # same_event paths; this prevents a cluster row from silently overriding a
    # contradictory relation graph.
    same_event_adjacency: Dict[str, set] = {}
    if clusters:
        for index, record in enumerate(relation_rows):
            relation_id = str(record.get("relation_id") or record.get("record_id") or index)
            if record.get("anchor_type") not in (None, "claim"):
                continue
            left = str(record.get("left_anchor_id") or "")
            right = str(record.get("right_anchor_id") or "")
            label = record.get("label")
            left_assignments = claim_clusters.get(left, [])
            right_assignments = claim_clusters.get(right, [])
            if not left_assignments or not right_assignments:
                if label == "same_event":
                    errors.append("same_event relation %s references an unclustered claim" % relation_id)
                continue
            common_clusters = set(left_assignments) & set(right_assignments)
            same_cluster = bool(common_clusters)
            if label == "same_event":
                if not same_cluster:
                    errors.append("same_event relation %s crosses cluster boundary" % relation_id)
                else:
                    cluster_id = sorted(common_clusters)[0]
                    if cluster_types.get(cluster_id) != "event":
                        errors.append("same_event relation %s is inside non-event cluster %s" % (relation_id, cluster_id))
                    same_event_adjacency.setdefault(left, set()).add(right)
                    same_event_adjacency.setdefault(right, set()).add(left)
                if record.get("must_not_link") is True:
                    if not record.get("override_reason"):
                        errors.append("same_event relation %s MNL requires override_reason" % relation_id)
                    if not _relation_adjudication_id(record):
                        errors.append("same_event relation %s MNL override requires adjudication_id" % relation_id)
            elif same_cluster and any(cluster_types.get(cid) == "event" for cid in common_clusters):
                errors.append("non-same_event relation %s is inside event cluster" % relation_id)
            if record.get("must_not_link") is True and same_cluster:
                # An overridden same_event MNL is the sole exception; every
                # other MNL pair would make the cluster internally unsafe.
                if not (label == "same_event" and record.get("override_reason") and _relation_adjudication_id(record)):
                    errors.append("relation %s MNL pair is inside cluster" % relation_id)

        for cluster_id, claim_ids in cluster_claims.items():
            if cluster_types.get(cluster_id) != "event" or len(set(claim_ids)) <= 1:
                continue
            start = claim_ids[0]
            visited = {start}
            pending = [start]
            while pending:
                current = pending.pop()
                for neighbor in same_event_adjacency.get(current, set()):
                    if neighbor in claim_ids and neighbor not in visited:
                        visited.add(neighbor)
                        pending.append(neighbor)
            if visited != set(claim_ids):
                errors.append("cluster %s claims are not connected by same_event edges" % cluster_id)

    # Presentations are typed foreign-key consumers.  A visible presentation
    # may never source a non-event/context-only cluster; do_not_display remains
    # useful as a reversible audit record.
    for index, record in enumerate(collections["presentations"]):
        presentation_id = str(record.get("presentation_id") or record.get("record_id") or index)
        owner = "presentation %s" % presentation_id
        presentation_type = str(record.get("presentation_type") or "")
        if presentation_type not in _PRESENTATION_TYPES:
            errors.append("%s has invalid presentation_type" % owner)
        source_cluster_ids = _graph_id_list(record, "source_cluster_ids", owner, errors)
        source_claim_ids = _graph_id_list(record, "source_claim_ids", owner, errors)
        check_reference(owner, "source_cluster_ids", source_cluster_ids, clusters, "cluster")
        check_reference(owner, "source_claim_ids", source_claim_ids, claims, "claim")
        for field_name in ("fact_claim_ids", "opinion_claim_ids", "question_claim_ids"):
            claim_field_ids = _graph_id_list(record, field_name, owner, errors)
            check_reference(owner, field_name, claim_field_ids, claims, "claim")
        separate_ids = _graph_id_list(record, "must_remain_separate_from", owner, errors)
        check_reference(owner, "must_remain_separate_from", separate_ids, presentations, "presentation")
        source_cluster_claim_ids = {
            claim_id
            for cluster_id in source_cluster_ids
            for claim_id in cluster_claims.get(cluster_id, [])
        }
        if source_cluster_ids and not set(source_claim_ids).issubset(source_cluster_claim_ids):
            errors.append("%s source_claim_ids are outside source clusters" % owner)
        source_context_only = any(
            cluster_types.get(cluster_id) == "non_event_context"
            or any(
                _is_context_only_record(messages.get(message_id))
                for message_id in cluster_messages.get(cluster_id, [])
            )
            or any(
                _is_context_only_record(messages.get(str((claims.get(claim_id) or {}).get("message_id"))))
                for claim_id in cluster_claims.get(cluster_id, [])
            )
            for cluster_id in source_cluster_ids
        )
        source_context_only = source_context_only or any(
            _is_context_only_record(claims.get(claim_id))
            or _is_context_only_record(messages.get(str((claims.get(claim_id) or {}).get("message_id"))))
            for claim_id in source_claim_ids
        )
        visible = presentation_type in _VISIBLE_PRESENTATION_TYPES
        if visible and source_context_only:
            errors.append("%s visible presentation contains context_only/non_event_context evidence" % owner)
        sentence_units = record.get("sentence_units")
        if sentence_units is None:
            sentence_units = []
        if not isinstance(sentence_units, (list, tuple)):
            errors.append("%s.sentence_units must be a list" % owner)
            sentence_units = []
        for sentence_index, sentence in enumerate(sentence_units):
            sentence_owner = "%s sentence[%d]" % (owner, sentence_index)
            if not isinstance(sentence, Mapping):
                errors.append("%s must be an object" % sentence_owner)
                continue
            sentence_claims = _graph_id_list(sentence, "claim_ids", sentence_owner, errors)
            sentence_messages = _graph_id_list(sentence, "message_ids", sentence_owner, errors)
            check_reference(sentence_owner, "claim_ids", sentence_claims, claims, "claim")
            check_reference(sentence_owner, "message_ids", sentence_messages, messages, "message")
            if visible and (not sentence_claims or not sentence_messages):
                errors.append("%s visible sentence lacks claim/message evidence" % sentence_owner)
            if not set(sentence_claims).issubset(set(source_claim_ids)):
                errors.append("%s claim evidence is outside source_claim_ids" % sentence_owner)
            if any(_is_context_only_record(messages.get(message_id)) for message_id in sentence_messages) and visible:
                errors.append("%s visible sentence uses context_only message" % sentence_owner)
    return errors


def validate_annotation_graph(
    records: Any,
    *,
    adjudications: Iterable[Mapping[str, Any]] = (),
    phase: str = "pre_release",
) -> FreezeValidationResult:
    """Public offline graph validator for pre-release and frozen annotations."""

    rows = tuple(adjudications)
    errors = _validate_annotation_graph(records, adjudications=rows, phase=phase)
    return FreezeValidationResult(
        not errors,
        tuple(sorted(set(errors))),
        (),
        adjudicated_disagreement_count=len(rows),
    )


# Short aliases make the boundary discoverable to release scripts without
# coupling callers to the internal graph implementation name.
validate_pre_release = validate_annotation_graph
validate_frozen_annotations = validate_annotation_graph


def _check_relation_mnl(
    comparison: ComparisonResult,
    adjudication_map: Mapping[str, Mapping[str, Any]],
    merged_records: Sequence[Mapping[str, Any]],
    errors: List[str],
) -> None:
    # Every relation carrying an MNL assertion must have a corresponding
    # explicit mandatory disagreement, even if A and B agreed on True.
    relation_pairs = {
        (item.alignment_key, item.record_type): item
        for item in comparison.pairs
        if item.record_type == "relation"
    }
    for record in merged_records:
        if _record_type(record) != "relation" or record.get("must_not_link") is not True:
            continue
        key = _relation_key(record, {})
        pair = relation_pairs.get((key, "relation"))
        if pair is None:
            # The merge may have accepted a relation with A/B-local IDs; use a
            # direct record ID fallback before failing closed.
            pair = next(
                (candidate for candidate in comparison.pairs
                 if candidate.record_type == "relation"
                 and candidate.a_record_id == _record_id(record, "relation")),
                None,
            )
        if pair is None:
            pair = next(
                (candidate for candidate in comparison.pairs
                 if candidate.record_type == "relation"
                 and candidate.b_record_id == _record_id(record, "relation")),
                None,
            )
        disputes = [
            item for item in comparison.disagreements
            if item.record_type == "relation"
            and pair is not None
            and item.alignment_key == pair.alignment_key
        ]
        if not disputes:
            errors.append("relation %s MNL was not present in comparison" % (_record_id(record, "relation"),))
            continue
        if not any(item.disagreement_id in adjudication_map for item in disputes):
            errors.append("relation %s MNL lacks adjudication" % (_record_id(record, "relation"),))
        if record.get("label") == "same_event" and not record.get("override_reason"):
            errors.append("relation %s same_event MNL requires override_reason" % (_record_id(record, "relation"),))


def validate_freeze_readiness(
    comparison: ComparisonResult,
    adjudications: Iterable[Mapping[str, Any]],
    *,
    merged_records: Optional[Sequence[Mapping[str, Any]]] = None,
    contract_dataset: Optional[Mapping[str, Any]] = None,
) -> FreezeValidationResult:
    """Fail closed unless every required dispute is explicitly adjudicated."""

    errors: List[str] = []
    warnings: List[str] = []
    try:
        rows = _coerce_adjudications(
            adjudications,
            guide_version=comparison.guide_version,
            dataset_version=comparison.dataset_version,
        )
        adjudication_map = _adjudication_index(rows)
    except AnnotationFormatError as exc:
        return FreezeValidationResult(
            False,
            (str(exc),),
            (),
            required_disagreement_count=len(comparison.required_disagreement_ids),
            adjudicated_disagreement_count=0,
        )
    required_ids = set(comparison.required_disagreement_ids)
    unknown_ids = sorted(set(adjudication_map) - required_ids)
    if unknown_ids:
        errors.append("adjudications reference unknown disagreements: %s" % ",".join(unknown_ids))
    unresolved = sorted(required_ids - set(adjudication_map))
    errors.extend("unresolved disagreement %s" % value for value in unresolved)
    if merged_records is not None:
        for record in merged_records:
            status = str(record.get("annotation_status") or "")
            if status not in {"adjudicated", "frozen"}:
                errors.append("record %s is not adjudicated" % (_record_id(record),))
        _check_relation_mnl(comparison, adjudication_map, merged_records, errors)
        errors.extend(
            _validate_annotation_graph(
                merged_records,
                adjudications=rows,
                phase="freeze",
            )
        )
    if contract_dataset is not None:
        # Lazy import prevents this workflow module from taking ownership of
        # the contract validator and keeps the exporter untouched.
        from .semantic_gold import validate_contract_dataset

        contract_validation = validate_contract_dataset(contract_dataset)
        errors.extend("contract: %s" % value for value in contract_validation.errors)
        warnings.extend(contract_validation.warnings)
        errors.extend(
            "graph: %s" % value
            for value in _validate_annotation_graph(
                contract_dataset,
                adjudications=rows,
                phase="pre_release",
            )
        )
    return FreezeValidationResult(
        not errors,
        tuple(sorted(set(errors))),
        tuple(sorted(set(warnings))),
        required_disagreement_count=len(required_ids),
        adjudicated_disagreement_count=len(required_ids & set(adjudication_map)),
    )


def freeze_adjudicated_annotations(
    merge_result: MergeResult,
    comparison: ComparisonResult,
    *,
    adjudicator_id: str = DEFAULT_ADJUDICATOR,
    contract_dataset: Optional[Mapping[str, Any]] = None,
) -> Tuple[Mapping[str, Any], ...]:
    """Mark merged rows frozen only after the disagreement gate passes."""

    validation = validate_freeze_readiness(
        comparison,
        merge_result.adjudications,
        merged_records=merge_result.records,
        contract_dataset=contract_dataset,
    )
    if not validation.ok:
        raise FreezeGateError("frozen gate failed: %s" % "; ".join(validation.errors))
    frozen: List[Mapping[str, Any]] = []
    for record in merge_result.records:
        output = deepcopy(dict(record))
        output["annotation_status"] = "frozen"
        output["annotator_id"] = adjudicator_id
        provenance = dict(output.get("provenance") or {})
        try:
            revision = int(provenance.get("revision", 1))
        except (TypeError, ValueError):
            revision = 1
        provenance["revision"] = revision + 1
        provenance["created_by"] = adjudicator_id
        provenance["guide_version"] = comparison.guide_version
        output["provenance"] = provenance
        frozen.append(output)
    return tuple(frozen)


# Concise aliases for callers that prefer operation names over workflow names.
load_annotations = load_annotation_jsonl
write_annotations = write_annotation_jsonl
compare_annotations = compare_annotation_sets
merge_annotations = merge_adjudicated_annotations
freeze_check = validate_freeze_readiness


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline A/B annotation comparison and freeze gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare = subparsers.add_parser("compare", help="compare A/B JSONL and emit a structure-only disagreement JSONL")
    compare.add_argument("--a", required=True, type=Path)
    compare.add_argument("--b", required=True, type=Path)
    compare.add_argument("--out", required=True, type=Path)
    compare.add_argument("--annotator-a", default=ANNOTATOR_A)
    compare.add_argument("--annotator-b", default=ANNOTATOR_B)

    for name, help_text in (
        ("freeze-check", "check whether all comparison disputes have adjudications"),
        ("merge", "merge A/B with adjudications"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--a", required=True, type=Path)
        command.add_argument("--b", required=True, type=Path)
        command.add_argument("--adjudications", required=True, type=Path)
        if name == "merge":
            command.add_argument("--out", required=True, type=Path)
        command.add_argument("--annotator-a", default=ANNOTATOR_A)
        command.add_argument("--annotator-b", default=ANNOTATOR_B)
        command.add_argument("--adjudicator", default=DEFAULT_ADJUDICATOR)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _cli_parser().parse_args(argv)
    if args.command == "compare":
        a = load_annotation_jsonl(args.a, annotator_id=args.annotator_a)
        b = load_annotation_jsonl(args.b, annotator_id=args.annotator_b)
        comparison = compare_annotation_sets(a, b)
        write_disagreements_jsonl(args.out, comparison)
        print(json.dumps(comparison.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    a = load_annotation_jsonl(args.a, annotator_id=args.annotator_a)
    b = load_annotation_jsonl(args.b, annotator_id=args.annotator_b)
    comparison = compare_annotation_sets(a, b)
    adjudications = load_adjudications_jsonl(
        args.adjudications,
        adjudicator_id=args.adjudicator,
        guide_version=comparison.guide_version,
        dataset_version=comparison.dataset_version,
    )
    if args.command == "freeze-check":
        result = validate_freeze_readiness(comparison, adjudications)
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if result.ok else 2
    merge_result = merge_adjudicated_annotations(a, b, comparison, adjudications, adjudicator_id=args.adjudicator)
    _write_jsonl(args.out, merge_result.records, overwrite=False)
    print(json.dumps(merge_result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "TOOL_VERSION",
    "ANNOTATOR_A",
    "ANNOTATOR_B",
    "DEFAULT_ADJUDICATOR",
    "AnnotationFormatError",
    "UnresolvedDisagreementsError",
    "FreezeGateError",
    "AnnotationSet",
    "AlignedPair",
    "Disagreement",
    "ComparisonResult",
    "FreezeValidationResult",
    "MergeResult",
    "load_annotation_jsonl",
    "write_annotation_jsonl",
    "load_annotations",
    "write_annotations",
    "compare_annotation_sets",
    "compare_annotations",
    "write_disagreements_jsonl",
    "read_disagreements_jsonl",
    "write_adjudications_jsonl",
    "load_adjudications_jsonl",
    "build_adjudication",
    "merge_adjudicated_annotations",
    "merge_annotations",
    "validate_annotation_graph",
    "validate_pre_release",
    "validate_frozen_annotations",
    "validate_freeze_readiness",
    "freeze_check",
    "freeze_adjudicated_annotations",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised through CLI smoke tests
    sys.exit(main())
