"""Private, body-free qualitative audit for the v2.9 Flash development run.

The caller supplies one development-only artifact and its corresponding
development message file.  This helper may inspect message text to make a
conservative audit judgement, but it never writes text, names, chat labels, or
raw identifiers.  Every emitted reference is a one-way digest.  The script
does not traverse frozen/frozen_test and does not read labels or v1 joins.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from audit_contextual_bundle_pipeline_v2_8_private import (
    _aggregate_signals,
    _body_free,
    _bundle_messages,
    _field_audit,
    _file_sha256,
    _load_json,
    _load_jsonl,
    _message_signal,
    _nested_body_keys,
    _opaque,
    _overall_status,
    _safe_path,
    _severity,
    _sha256_bytes,
    _write_json,
    _write_jsonl,
)


PENDING_SAMPLE_SIZE = 20
PENDING_STRATUM_SIZE = 5
CANONICAL_STATES = frozenset(
    {"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"}
)
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})
RELATION_LABELS = frozenset(
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
AUDIT_FIELDS = (
    "speaker_metadata",
    "mentioned_person",
    "subject",
    "target",
    "object",
    "action",
    "claim_type",
    "state",
    "modality",
    "coreference",
    "context_relation",
    "evidence",
)
EVIDENCE_FIELDS = frozenset(
    {
        "speaker",
        "mentioned_person",
        "subject",
        "target",
        "object",
        "action",
        "claim_type",
        "state",
        "modality",
        "coreference",
        "context_relation",
    }
)
SAFE_OBSERVED_KEYS = frozenset(
    {"count", "valid_count", "field_count", "known", "evidence_bound", "unknown", "boundary", "value"}
)
SAFE_OBSERVED_VALUES = frozenset(
    {"unknown", "known", "unknown_allowed", "no_cue_observed", "none", "N/A"}
)
ZERO_TOLERANCE_KEYS = frozenset(
    {
        "cross_chat_relation_violations",
        "time_only_relation_violations",
        "same_segment_unsafe_strong",
        "silence_terminal_violations",
        "fallback_accepted",
    }
)
KNOWN_CUE_REQUIRED_FIELDS = frozenset(
    {"object", "person", "state", "reactivation", "evidence", "reference", "budget", "replay"}
)


def _counter_dict(values: Iterable[Any]) -> Dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sample_evenly(rows: Sequence[Mapping[str, Any]], count: int) -> List[Mapping[str, Any]]:
    """Take deterministic, spread-out samples without exposing identifiers."""
    if count <= 0 or not rows:
        return []
    if len(rows) <= count:
        return list(rows)
    if count == 1:
        return [rows[len(rows) // 2]]
    indices = [round(index * (len(rows) - 1) / (count - 1)) for index in range(count)]
    return [rows[index] for index in indices]


def _safe_field_verdict(verdict: Mapping[str, Any]) -> Dict[str, Any]:
    observed = verdict.get("observed") if isinstance(verdict.get("observed"), Mapping) else {}
    safe_observed: Dict[str, Any] = {}
    for key in SAFE_OBSERVED_KEYS:
        value = observed.get(key)
        if isinstance(value, bool) or isinstance(value, (int, float)):
            safe_observed[key] = value
        elif isinstance(value, str) and value in SAFE_OBSERVED_VALUES:
            safe_observed[key] = value
    return {
        "status": str(verdict.get("status") or "uncertain"),
        "error_codes": sorted(str(code) for code in (verdict.get("error_codes") or [])),
        "observed": safe_observed,
    }


def _strict_evidence_audit(
    semantic: Mapping[str, Any],
    bundle: Mapping[str, Any],
    source_messages: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate typed evidence against local message scope and span bounds."""
    evidence = semantic.get("evidence")
    source_ids = {str(value) for value in (semantic.get("message_ids") or bundle.get("source_message_ids") or [])}
    source_by_id = {
        str(item.get("message_id")): item
        for item in source_messages
        if item.get("message_id") is not None
    }
    errors: List[str] = []
    valid = 0
    fields: List[str] = []
    span_valid = 0
    if not isinstance(evidence, list):
        return {
            "status": "fail",
            "count": 0,
            "valid_count": 0,
            "span_valid_count": 0,
            "field_count": 0,
            "fields": [],
            "error_codes": ["EVIDENCE_FIELD_MISSING"],
        }
    for item in evidence:
        if not isinstance(item, Mapping):
            errors.append("EVIDENCE_ITEM_NOT_OBJECT")
            continue
        required = {"evidence_id", "field", "kind", "message_id", "span"}
        if not required <= set(item):
            errors.append("EVIDENCE_SCHEMA_MISSING")
            continue
        if not str(item.get("evidence_id") or ""):
            errors.append("EVIDENCE_ID_EMPTY")
            continue
        field = str(item.get("field") or "")
        if field not in EVIDENCE_FIELDS:
            errors.append("EVIDENCE_FIELD_UNKNOWN")
            continue
        message_id = str(item.get("message_id") or "")
        message = source_by_id.get(message_id)
        if message is None or message_id not in source_ids:
            errors.append("EVIDENCE_MESSAGE_UNBOUND")
            continue
        if not str(item.get("kind") or ""):
            errors.append("EVIDENCE_KIND_EMPTY")
            continue
        span = item.get("span")
        if not isinstance(span, Mapping) or not {"start", "end"} <= set(span):
            errors.append("EVIDENCE_SPAN_MISSING")
            continue
        start = span.get("start")
        end = span.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            errors.append("EVIDENCE_SPAN_NOT_INTEGER")
            continue
        text_length = len(str(message.get("redacted_text") or message.get("content") or ""))
        if start < 0 or end < start or end > text_length:
            errors.append("EVIDENCE_SPAN_OUT_OF_BOUNDS")
            continue
        valid += 1
        span_valid += 1
        fields.append(field)
    status = "pass" if evidence and valid == len(evidence) else "uncertain" if valid else "fail"
    return {
        "status": status,
        "count": len(evidence),
        "valid_count": valid,
        "span_valid_count": span_valid,
        "field_count": len(set(fields)),
        "fields": sorted(set(fields)),
        "error_codes": sorted(set(errors)),
    }


def _speaker_binding(semantic: Mapping[str, Any], source_messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    speaker = semantic.get("speaker")
    source_speakers = {
        str(item.get("speaker_id"))
        for item in source_messages
        if item.get("speaker_id") is not None
    }
    if not isinstance(speaker, Mapping):
        return {"status": "fail", "error_codes": ["SPEAKER_METADATA_OR_EVIDENCE_INVALID"]}
    speaker_id = str(speaker.get("id") or "")
    role_ok = str(speaker.get("role") or "") == "speaker"
    type_ok = bool(str(speaker.get("type") or ""))
    if not speaker_id or speaker_id == "unknown" or not role_ok or not type_ok:
        return {"status": "fail", "error_codes": ["SPEAKER_METADATA_OR_EVIDENCE_INVALID"]}
    if speaker_id not in source_speakers:
        return {"status": "fail", "error_codes": ["SPEAKER_NOT_BOUND_TO_SOURCE"]}
    return {
        "status": "pass",
        "error_codes": [],
        "source_speaker_count": len(source_speakers),
        "source_binding": True,
    }


def _boundary_audit(bundle: Mapping[str, Any], source_messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    latest_state = str(bundle.get("latest_state") or "unknown")
    state_valid = latest_state in CANONICAL_STATES
    media_only = bool(source_messages) and all(
        str(item.get("media_state") or "") in {"media", "placeholder", "silent", "unknown"}
        and not str(item.get("redacted_text") or item.get("content") or "").strip()
        for item in source_messages
    )
    silence_terminal = latest_state in TERMINAL_STATES and media_only
    open_boundary = bundle.get("open_boundary") is True
    closed = bundle.get("closed") is True
    codes: List[str] = []
    if not state_valid:
        codes.append("STATE_ENUM_INVALID")
    if silence_terminal:
        codes.append("SILENCE_AS_TERMINAL_STATE")
    if not open_boundary or closed:
        codes.append("OPEN_BOUNDARY_NOT_PRESERVED")
    status = "pass" if not codes else "fail"
    return {
        "status": status,
        "error_codes": sorted(set(codes)),
        "latest_state": latest_state if latest_state in CANONICAL_STATES else "unknown",
        "open_boundary": open_boundary,
        "closed": closed,
        "start_time_unknown": str(bundle.get("start_time_source") or "") in {"", "unknown"},
        "end_time_unknown": str(bundle.get("end_time_source") or "") in {"", "unknown"},
        "silence_terminal_violation": silence_terminal,
    }


def _cue_audit(
    decision: Mapping[str, Any],
    bundle: Mapping[str, Any],
    source_messages: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    cues = decision.get("activation_cues") or []
    source_chats = {
        str(item.get("chat_id"))
        for item in source_messages
        if item.get("chat_id") is not None
    }
    executable = 0
    replay_keys = 0
    cue_ids = 0
    required_fields = 0
    positive_targets = 0
    scope_bound = 0
    unknown_required_fields = 0
    malformed = 0
    for cue in cues:
        if not isinstance(cue, Mapping):
            malformed += 1
            continue
        if cue.get("executable") is True:
            executable += 1
        if str(cue.get("replay_key") or ""):
            replay_keys += 1
        if str(cue.get("cue_id") or ""):
            cue_ids += 1
        if isinstance(cue.get("required_fields"), list) and cue.get("required_fields"):
            required_fields += 1
            unknown_required_fields += sum(
                str(field) not in KNOWN_CUE_REQUIRED_FIELDS
                for field in cue.get("required_fields") or []
            )
        if int(cue.get("target_message_count") or 0) > 0:
            positive_targets += 1
        if str(cue.get("target_scope") or "") in source_chats:
            scope_bound += 1
    count = len(cues)
    all_valid = (
        count == int(decision.get("activation_cue_count") or 0)
        and count > 0
        and malformed == 0
        and executable == count
        and replay_keys == count
        and cue_ids == count
        and required_fields == count
        and unknown_required_fields == 0
        and positive_targets == count
        and scope_bound == count
        and decision.get("activation_cue_replayable") is True
    )
    codes: List[str] = []
    if count != int(decision.get("activation_cue_count") or 0):
        codes.append("ACTIVATION_CUE_COUNT_MISMATCH")
    if malformed or executable != count:
        codes.append("ACTIVATION_CUE_NOT_EXECUTABLE")
    if replay_keys != count or cue_ids != count:
        codes.append("ACTIVATION_CUE_NOT_REPLAYABLE")
    if required_fields != count or positive_targets != count:
        codes.append("ACTIVATION_CUE_TARGET_INCOMPLETE")
    if unknown_required_fields:
        codes.append("ACTIVATION_CUE_REQUIRED_FIELD_UNKNOWN")
    if scope_bound != count:
        codes.append("ACTIVATION_CUE_SCOPE_UNBOUND")
    if decision.get("activation_cue_replayable") is not True:
        codes.append("ACTIVATION_CUE_REPLAY_FLAG_FALSE")
    snapshot_recoverable = bool(
        snapshot
        and (
            snapshot.get("open_thread_ids")
            or snapshot.get("pending_relation_candidates")
            or snapshot.get("recent_claim_ids")
            or snapshot.get("recent_fragment_ids")
            or snapshot.get("unresolved_slots")
        )
    )
    return {
        "status": "pass" if all_valid else "fail",
        "error_codes": sorted(set(codes)),
        "declared_count": int(decision.get("activation_cue_count") or 0),
        "observed_count": count,
        "executable_count": executable,
        "replay_key_count": replay_keys,
        "cue_id_count": cue_ids,
        "required_fields_count": required_fields,
        "unknown_required_field_count": unknown_required_fields,
        "positive_target_count": positive_targets,
        "scope_bound_count": scope_bound,
        "replayable_flag": decision.get("activation_cue_replayable") is True,
        "snapshot_present": snapshot is not None,
        "snapshot_activation_cue_count": len((snapshot or {}).get("activation_cues") or []),
        "snapshot_recoverable_line_present": snapshot_recoverable,
    }


def _valuable_signal(source_messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    signals = [_message_signal(item) for item in source_messages]
    return {
        "source_message_count": len(source_messages),
        "valuable_message_count": sum(bool(item.get("valuable")) for item in signals),
        "evidence_eligible_count": sum(bool(item.get("evidence_eligible")) for item in signals),
        "event_evidence_eligible_count": sum(bool(item.get("event_evidence_eligible")) for item in signals),
        "text_present_count": sum(bool(item.get("text_present")) for item in signals),
        "media_placeholder_count": sum(bool(item.get("media_placeholder")) for item in signals),
        "state_signal_count": sum(bool(item.get("state_codes")) for item in signals),
        "question_signal_count": sum(bool(item.get("question_signal")) for item in signals),
        "context_signal_count": sum(bool(item.get("context_signal")) for item in signals),
    }


def _complete_audit(
    decision: Mapping[str, Any],
    bundle: Mapping[str, Any],
    messages: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    semantic = decision.get("semantic_bundle") if isinstance(decision.get("semantic_bundle"), Mapping) else {}
    source_messages = _bundle_messages(bundle, messages)
    field_verdicts, detail = _field_audit(semantic, bundle, source_messages)
    strict_evidence = _strict_evidence_audit(semantic, bundle, source_messages)
    speaker = _speaker_binding(semantic, source_messages)
    if speaker.get("status") != "pass":
        field_verdicts["speaker_metadata"] = {
            "status": "fail",
            "error_codes": speaker.get("error_codes") or ["SPEAKER_METADATA_OR_EVIDENCE_INVALID"],
        }
    boundary = _boundary_audit(bundle, source_messages)
    codes = list(detail.get("error_codes") or [])
    codes.extend(str(code) for code in strict_evidence.get("error_codes") or [])
    codes.extend(str(code) for code in boundary.get("error_codes") or [])
    if strict_evidence.get("status") == "fail":
        field_verdicts["evidence"] = {
            "status": "fail",
            "error_codes": strict_evidence.get("error_codes") or ["EVIDENCE_INVALID"],
        }
    overall = _overall_status(field_verdicts)
    if boundary.get("status") == "fail" or strict_evidence.get("status") == "fail":
        overall = "fail"
    validation = decision.get("validation") if isinstance(decision.get("validation"), Mapping) else {}
    return {
        "record_type": "complete_bundle_audit",
        "audit_ref": _opaque(decision.get("candidate_id"), "complete"),
        "selection": {
            "selected_for_encode": decision.get("selected_for_encode") is True,
            "selection_status": str(decision.get("selection_status") or "unknown"),
            "selection_reason": str(decision.get("selection_reason") or "unknown"),
            "semantic_status": str(decision.get("semantic_status") or "unknown"),
            "semantic_source": str(decision.get("semantic_source") or "unknown"),
            "channel": str(decision.get("channel") or "unknown"),
            "scale": str(decision.get("scale") or "unknown"),
            "source_message_count": len(source_messages),
            "provider_attempt_count": int(decision.get("provider_attempt_count") or 0),
            "package_attempt_count": int(decision.get("package_attempt_count") or 0),
        },
        "schema": {
            "semantic_schema_version": str(semantic.get("schema_version") or "unknown"),
            "validation_ok": validation.get("ok") is True,
            "validation_error_count": len(validation.get("errors") or []),
            "validation_warning_count": len(validation.get("warnings") or []),
        },
        "field_verdicts": {
            field: _safe_field_verdict(field_verdicts.get(field, {}))
            for field in AUDIT_FIELDS
        },
        "evidence": strict_evidence,
        "speaker_binding": speaker,
        "signal": _valuable_signal(source_messages),
        "boundary": boundary,
        "unknown_field_count": int(detail.get("unknown_field_count") or 0),
        "status": overall,
        "error_codes": sorted(set(codes)),
        "severity": _severity(overall, sorted(set(codes))),
    }


def _pending_audit(
    decision: Mapping[str, Any],
    bundle: Mapping[str, Any],
    messages: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None,
    stratum: str,
) -> Dict[str, Any]:
    source_messages = _bundle_messages(bundle, messages)
    cue = _cue_audit(decision, bundle, source_messages, snapshot)
    signal = _valuable_signal(source_messages)
    boundary = _boundary_audit(bundle, source_messages)
    valuable = signal["valuable_message_count"] > 0
    codes = list(cue.get("error_codes") or []) + list(boundary.get("error_codes") or [])
    if valuable and cue.get("status") != "pass":
        codes.append("VALUABLE_PENDING_WITHOUT_REACTIVATION_CUE")
    value_status = "pass" if valuable and cue.get("status") == "pass" else "N/A_no_valuable_signal" if not valuable else "fail"
    overall = "fail" if any(code for code in codes if code in {"ACTIVATION_CUE_NOT_EXECUTABLE", "ACTIVATION_CUE_NOT_REPLAYABLE", "ACTIVATION_CUE_SCOPE_UNBOUND", "VALUABLE_PENDING_WITHOUT_REACTIVATION_CUE", "SILENCE_AS_TERMINAL_STATE", "OPEN_BOUNDARY_NOT_PRESERVED"}) else "pass"
    return {
        "record_type": "pending_bundle_audit",
        "audit_ref": _opaque(decision.get("candidate_id"), "pending"),
        "stratum": stratum,
        "selection": {
            "selected_for_encode": decision.get("selected_for_encode") is True,
            "selection_status": str(decision.get("selection_status") or "unknown"),
            "selection_reason": str(decision.get("selection_reason") or "unknown"),
            "semantic_status": str(decision.get("semantic_status") or "unknown"),
            "semantic_source": str(decision.get("semantic_source") or "unknown"),
            "channel": str(decision.get("channel") or "unknown"),
            "scale": str(decision.get("scale") or "unknown"),
            "budget_deferred": decision.get("budget_deferred") is True,
            "source_message_count": len(source_messages),
            "provider_attempt_count": int(decision.get("provider_attempt_count") or 0),
            "package_attempt_count": int(decision.get("package_attempt_count") or 0),
            "retry_count": int(decision.get("retry_count") or 0),
        },
        "value": {
            **signal,
            "valuable_content_delayed": valuable,
            "valuable_deferred_status": value_status,
            "discarded": False,
            "misclassified_as_unrelated": False,
        },
        "activation_cue": cue,
        "object_unknown_boundary": {
            "object_ref_count": len(bundle.get("object_refs") or []),
            "unresolved_slot_count": len(bundle.get("unresolved_slot_codes") or []),
            "unknown_allowed": len(bundle.get("object_refs") or []) == 0,
        },
        "boundary": boundary,
        "status": overall,
        "error_codes": sorted(set(codes)),
        "severity": _severity(overall, sorted(set(codes))),
    }


def _provider_accounting(
    requests: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
) -> Dict[str, Any]:
    provider_rows = [row for row in requests if str(row.get("source") or "") == "provider"]
    attempt_rows = [
        row for row in provider_rows
        if str(row.get("status") or "") in {"started", "complete", "failed"}
    ]
    successful_outputs = sum(
        str(row.get("semantic_source") or "") == "model_wire"
        and str(row.get("semantic_status") or "") == "complete"
        for row in decisions
    )
    failed = sum(str(row.get("status") or "") == "failed" for row in provider_rows)
    unfinished = sum(str(row.get("status") or "") == "started" for row in provider_rows)
    budget_deferred = sum(
        str(row.get("semantic_status") or "") == "pending"
        and row.get("budget_deferred") is True
        for row in decisions
    )
    selected_package_count = len(
        {
            str(row.get("semantic_package_id"))
            for row in decisions
            if row.get("selected_for_encode") is True and row.get("semantic_package_id") is not None
        }
    )
    message_count = int(aggregate.get("message_count") or 0)
    candidate_count = len(decisions)
    aggregate_counters = aggregate.get("counters") if isinstance(aggregate.get("counters"), Mapping) else {}
    legacy_provider_calls = aggregate_counters.get("provider_calls_used")
    if isinstance(legacy_provider_calls, Mapping):
        legacy_provider_calls = legacy_provider_calls.get("value")
    return {
        "definition_note": (
            "provider_request_attempts counts provider rows with started/complete/failed only; pending provider rows "
            "are deferred/rejected work. successful_model_outputs counts complete model_wire decisions. "
            "candidate_decisions counts every candidate decision and is not a call count."
        ),
        "provider_request_attempts": len(attempt_rows),
        "successful_model_outputs": successful_outputs,
        "failed_provider_attempts": failed,
        "unfinished_provider_attempts": unfinished,
        "provider_pending_rows": sum(str(row.get("status") or "") == "pending" for row in provider_rows),
        "candidate_decisions": candidate_count,
        "budget_deferred": budget_deferred,
        "selected_bundle_count": selected_package_count,
        "selected_bundle_limit": int(cost.get("max_provider_calls") or (cost.get("budget") or {}).get("max_bundle_calls") or 0),
        "provider_request_attempts_per_1000_messages": round(len(attempt_rows) / max(1, message_count) * 1000, 4),
        "successful_model_outputs_per_1000_messages": round(successful_outputs / max(1, message_count) * 1000, 4),
        "candidate_decisions_per_1000_messages": round(candidate_count / max(1, message_count) * 1000, 4),
        "budget_deferred_rate": round(budget_deferred / max(1, candidate_count), 4),
        "request_status_counts": _counter_dict(row.get("status") for row in requests),
        "request_source_status_counts": _counter_dict(
            "%s/%s" % (str(row.get("source") or "unknown"), str(row.get("status") or "unknown"))
            for row in requests
        ),
        "deprecated_legacy_metrics": {
            "aggregate_provider_calls_used": {
                "value": legacy_provider_calls,
                "deprecated": True,
                "meaning": "legacy aggregate alias; retained for reconciliation only",
                "replacement": "provider_request_attempts",
            },
            "budget_calls_used": {
                "value": (cost.get("budget") or {}).get("calls_used"),
                "deprecated": True,
                "meaning": "historical budget reservation counter; may include a row not materialized as a provider output",
                "replacement": "provider_request_attempts plus provider_pending_rows",
            },
            "semantic_model_calls": {
                "value": None,
                "deprecated": True,
                "meaning": "not exposed by the v2.9 artifact; never inferred from candidate_decisions",
                "replacement": "explicit request and decision counters",
            },
        },
    }


def _zero_tolerance(
    decisions: Sequence[Mapping[str, Any]],
    bundles: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    aggregate: Mapping[str, Any],
) -> Dict[str, Any]:
    cross_chat = 0
    silence_terminal = 0
    for bundle in bundles:
        source_messages = _bundle_messages(bundle, messages)
        source_chats = {str(item.get("chat_id")) for item in source_messages if item.get("chat_id") is not None}
        bundle_scope = bundle.get("chat_scope")
        bundle_chat_ids = set()
        if isinstance(bundle_scope, Mapping):
            bundle_chat_ids = {str(value) for value in (bundle_scope.get("chat_ids") or [])}
        if len(source_chats) > 1 or (bundle_chat_ids and source_chats - bundle_chat_ids):
            cross_chat += 1
        latest_state = str(bundle.get("latest_state") or "unknown")
        media_only = bool(source_messages) and all(
            not str(item.get("redacted_text") or item.get("content") or "").strip()
            for item in source_messages
        )
        if latest_state in TERMINAL_STATES and media_only:
            silence_terminal += 1
    time_only = 0
    same_segment = 0
    for relation in relations:
        evidence = {str(item) for item in (relation.get("evidence") or relation.get("supporting_signals") or [])}
        if evidence and evidence <= {"time", "time_proximity", "weak_time"}:
            time_only += 1
        subtype = str(relation.get("segment_relation") or relation.get("relation_subtype") or "")
        if subtype in {"same_segment_time", "same_segment"} and str(relation.get("strength") or "") == "strong":
            same_segment += 1
    fallback_accepted = sum(
        str(row.get("semantic_status") or "") == "complete"
        and str(row.get("semantic_source") or "") == "fallback"
        for row in decisions
    )
    observed = {
        "cross_chat_relation_violations": cross_chat,
        "time_only_relation_violations": time_only,
        "same_segment_unsafe_strong": same_segment,
        "silence_terminal_violations": silence_terminal,
        "fallback_accepted": fallback_accepted,
    }
    aggregate_zero = aggregate.get("zero_tolerance") if isinstance(aggregate.get("zero_tolerance"), Mapping) else {}
    return {
        "observed": observed,
        "aggregate_reported": {
            key: int(aggregate_zero.get(key) or 0)
            for key in ZERO_TOLERANCE_KEYS
        },
        "all_zero": all(value == 0 for value in observed.values()),
        "status": "pass" if all(value == 0 for value in observed.values()) else "fail",
    }


def _selection_value_coverage(
    decisions: Sequence[Mapping[str, Any]],
    bundles_by_candidate: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    scheduler: Mapping[str, Any],
) -> Dict[str, Any]:
    all_message_ids = set(messages)
    valuable_message_ids = {
        message_id
        for message_id, message in messages.items()
        if _message_signal(message).get("valuable")
    }
    selected = [row for row in decisions if row.get("selected_for_encode") is True]
    selected_ids = {
        str(message_id)
        for row in selected
        for message_id in (bundles_by_candidate.get(str(row.get("candidate_id")), {}).get("source_message_ids") or [])
    }
    selected_valuable = selected_ids & valuable_message_ids
    scheduler_manifest = scheduler.get("manifest") if isinstance(scheduler.get("manifest"), Mapping) else {}
    scheduler_coverage = scheduler.get("coverage") if isinstance(scheduler.get("coverage"), Mapping) else {}
    return {
        "candidate_decisions": len(decisions),
        "selected_package_count": len(
            {
                str(row.get("semantic_package_id"))
                for row in selected
                if row.get("semantic_package_id") is not None
            }
        ),
        "selected_representative_count": len(
            {
                str(row.get("selected_representative_id"))
                for row in selected
                if row.get("selected_representative_id") is not None
            }
        ),
        "message_count": len(all_message_ids),
        "selected_unique_message_count": len(selected_ids),
        "selected_message_coverage_rate": round(len(selected_ids) / max(1, len(all_message_ids)), 4),
        "valuable_message_count": len(valuable_message_ids),
        "selected_valuable_unique_message_count": len(selected_valuable),
        "selected_valuable_coverage_rate": round(len(selected_valuable) / max(1, len(valuable_message_ids)), 4),
        "scheduler_estimated_message_coverage_count": scheduler_manifest.get("estimated_message_coverage_count"),
        "scheduler_estimated_message_coverage_ratio": scheduler_manifest.get("estimated_message_coverage_ratio"),
        "scheduler_reported_candidate_count": scheduler_coverage.get("candidate_count"),
        "scheduler_reported_selected_count": scheduler_coverage.get("selected_count"),
    }


def _selected_map(
    selected: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    input_sha256: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = [
        {
            "record_type": "selected_package_audit_map_metadata",
            "schema_version": "contextual_bundle_v2_9_selected_package_map_private_v1",
            "scope": "development",
            "artifact_version": str(manifest.get("artifact_version") or "contextual_bundle_pipeline_v2_9"),
            "input_ref": _opaque(input_sha256, "input"),
            "source_guard": {
                "frozen_or_frozen_test_read": False,
                "gold_loaded": False,
                "body_fields_written": False,
                "identity_fields_written": False,
            },
            "selected_count": len(selected),
            "matching_key": "opaque semantic_package_ref plus opaque selected_representative_ref",
        }
    ]
    for ordinal, decision in enumerate(selected, 1):
        rows.append(
            {
                "record_type": "selected_package_audit_map",
                "sample_ordinal": ordinal,
                "semantic_package_ref": _opaque(decision.get("semantic_package_id"), "semantic_package"),
                "selected_representative_ref": _opaque(decision.get("selected_representative_id"), "representative"),
                "candidate_ref": _opaque(decision.get("candidate_id"), "candidate"),
                "selection_status": str(decision.get("selection_status") or "unknown"),
                "selection_reason": str(decision.get("selection_reason") or "unknown"),
                "semantic_status": str(decision.get("semantic_status") or "unknown"),
                "semantic_source": str(decision.get("semantic_source") or "unknown"),
                "channel": str(decision.get("channel") or "unknown"),
                "scale": str(decision.get("scale") or "unknown"),
                "source_message_count": int(decision.get("source_message_count") or 0),
                "activation_cue_count": int(decision.get("activation_cue_count") or 0),
                "activation_cue_replayable": decision.get("activation_cue_replayable") is True,
                "budget_deferred": decision.get("budget_deferred") is True,
                "provider_attempt_count": int(decision.get("provider_attempt_count") or 0),
                "package_attempt_count": int(decision.get("package_attempt_count") or 0),
            }
        )
    return rows


def run_audit(artifact_dir: Path, input_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    for path in (artifact_dir, input_path.parent):
        _safe_path(path)
    required_files = (
        "manifest.private.json",
        "aggregate.private.json",
        "cost.private.json",
        "decisions.private.jsonl",
        "bundles.private.jsonl",
        "requests.private.jsonl",
        "gate.private.json",
        "open_context_snapshots.private.jsonl",
        "relations.private.jsonl",
        "scheduler.private.json",
        "selection_mapping.private.jsonl",
    )
    paths = {name: artifact_dir / name for name in required_files}
    for path in paths.values():
        if not path.exists():
            raise FileNotFoundError(path)
    manifest_bytes = paths["manifest.private.json"].read_bytes()
    manifest = _load_json(paths["manifest.private.json"])
    aggregate = _load_json(paths["aggregate.private.json"])
    cost = _load_json(paths["cost.private.json"])
    decisions = _load_jsonl(paths["decisions.private.jsonl"])
    bundles = _load_jsonl(paths["bundles.private.jsonl"])
    requests = _load_jsonl(paths["requests.private.jsonl"])
    gate = _load_json(paths["gate.private.json"])
    snapshots = _load_jsonl(paths["open_context_snapshots.private.jsonl"])
    relations = _load_jsonl(paths["relations.private.jsonl"])
    scheduler = _load_json(paths["scheduler.private.json"])
    selection_mapping = _load_jsonl(paths["selection_mapping.private.jsonl"])
    messages = {
        str(row.get("message_id")): row
        for row in _load_jsonl(input_path)
        if row.get("message_id") is not None
    }

    if str(manifest.get("split") or "") != "development" or str(manifest.get("input_directory_name") or "") != "development":
        raise AssertionError("v2.9 artifact is not development scoped")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise AssertionError("v2.9 artifact reports frozen/gold reads")
    if _file_sha256(input_path) != str(manifest.get("input_sha256") or ""):
        raise AssertionError("development input hash mismatch")
    if scheduler.get("frozen_read") is True or scheduler.get("gold_loaded") is True:
        raise AssertionError("scheduler artifact reports frozen/gold reads")
    if len(decisions) != int(manifest.get("candidate_decision_count") or len(decisions)):
        raise AssertionError("decision count mismatch")
    if len(selection_mapping) != len(decisions):
        raise AssertionError("selection mapping count mismatch")

    bundles_by_candidate = {str(row.get("bundle_id")): row for row in bundles}
    snapshots_by_id = {str(row.get("snapshot_id")): row for row in snapshots}
    complete = [row for row in decisions if str(row.get("semantic_status") or "") == "complete"]
    pending = [row for row in decisions if str(row.get("semantic_status") or "") == "pending"]
    selected = [row for row in decisions if row.get("selected_for_encode") is True]
    mapping_by_candidate = {str(row.get("candidate_id")): row for row in selection_mapping}

    audit_rows: List[Dict[str, Any]] = [
        {
            "record_type": "audit_metadata",
            "schema_version": "contextual_bundle_v2_9_flash_human_audit_private_v1",
            "scope": "development",
            "artifact_version": str(manifest.get("artifact_version") or "contextual_bundle_pipeline_v2_9"),
            "manifest_ref": _opaque(_sha256_bytes(manifest_bytes), "manifest"),
            "input_ref": _opaque(manifest.get("input_sha256"), "input"),
            "source_guard": {
                "frozen_or_frozen_test_read": False,
                "gold_loaded": False,
                "development_input_read": manifest.get("development_input_read") is True,
                "body_fields_written": False,
                "identity_fields_written": False,
            },
            "counts": {
                "messages": len(messages),
                "decisions": len(decisions),
                "bundles": len(bundles),
                "complete": len(complete),
                "pending": len(pending),
                "selected": len(selected),
            },
        }
    ]

    for decision in complete:
        bundle = bundles_by_candidate.get(str(decision.get("candidate_id")))
        if bundle is None:
            audit_rows.append(
                {
                    "record_type": "complete_bundle_audit",
                    "audit_ref": _opaque(decision.get("candidate_id"), "complete"),
                    "status": "fail",
                    "severity": "high",
                    "error_codes": ["BUNDLE_NOT_FOUND"],
                }
            )
            continue
        audit_rows.append(_complete_audit(decision, bundle, messages))

    strata_rows: Dict[str, List[Mapping[str, Any]]] = {
        "selected_budget": [
            row for row in pending
            if row.get("selected_for_encode") is True and str(row.get("semantic_source") or "") == "budget"
        ],
        "selected_model_failed": [
            row for row in pending
            if row.get("selected_for_encode") is True and str(row.get("semantic_source") or "") == "model_failed"
        ],
        "unselected_duplicate_semantic_package": [
            row for row in pending
            if row.get("selected_for_encode") is not True
            and str(row.get("selection_reason") or "") == "duplicate_semantic_package"
        ],
        "unselected_scheduler_budget_exhausted": [
            row for row in pending
            if row.get("selected_for_encode") is not True
            and str(row.get("selection_reason") or "") == "scheduler_budget_exhausted"
        ],
    }
    sampled_pending = 0
    pending_strata_summary: Dict[str, Any] = {}
    for stratum, rows in strata_rows.items():
        sample = _sample_evenly(rows, PENDING_STRATUM_SIZE)
        sampled_pending += len(sample)
        pending_strata_summary[stratum] = {
            "available": len(rows),
            "sampled": len(sample),
            "status": "pass" if len(sample) == PENDING_STRATUM_SIZE else "fail",
        }
        for decision in sample:
            bundle = bundles_by_candidate.get(str(decision.get("candidate_id")))
            if bundle is None:
                audit_rows.append(
                    {
                        "record_type": "pending_bundle_audit",
                        "audit_ref": _opaque(decision.get("candidate_id"), "pending"),
                        "stratum": stratum,
                        "status": "fail",
                        "severity": "high",
                        "error_codes": ["BUNDLE_NOT_FOUND"],
                    }
                )
                continue
            snapshot = snapshots_by_id.get(str(bundle.get("open_context_snapshot_id")))
            audit_rows.append(_pending_audit(decision, bundle, messages, snapshot, stratum))

    mapping_match_count = sum(
        str(mapping_by_candidate.get(str(row.get("candidate_id")), {}).get("semantic_package_id"))
        == str(row.get("semantic_package_id"))
        for row in decisions
    )
    selected_mapping_match_count = sum(
        str(mapping_by_candidate.get(str(row.get("candidate_id")), {}).get("semantic_package_id"))
        == str(row.get("semantic_package_id"))
        for row in selected
    )
    selected_cue_preserved_count = sum(
        mapping_by_candidate.get(str(row.get("candidate_id")), {}).get("activation_cue_preserved") is True
        and int(mapping_by_candidate.get(str(row.get("candidate_id")), {}).get("activation_cue_count") or 0)
        == int(row.get("activation_cue_count") or 0)
        for row in selected
    )
    provider_accounting = _provider_accounting(requests, decisions, aggregate, cost)
    selection_coverage = _selection_value_coverage(decisions, bundles_by_candidate, messages, scheduler)
    zero_tolerance = _zero_tolerance(decisions, bundles, relations, messages, aggregate)
    map_rows = _selected_map(selected, manifest, str(manifest.get("input_sha256") or ""))

    audit_dir = artifact_dir / "audit"
    _safe_path(audit_dir)
    map_path = audit_dir / "selected_package_audit_map.private.jsonl"
    _write_jsonl(map_path, [_body_free(row) for row in map_rows])
    map_sha = _file_sha256(map_path)
    summary = {
        "schema_version": "contextual_bundle_v2_9_flash_human_audit_summary_private_v1",
        "audit_mode": "private_qualitative_body_free",
        "scope": "development",
        "artifact_version": str(manifest.get("artifact_version") or "contextual_bundle_pipeline_v2_9"),
        "manifest_binding": {
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "input_sha256": str(manifest.get("input_sha256") or ""),
            "decisions_sha256": _file_sha256(paths["decisions.private.jsonl"]),
            "bundles_sha256": _file_sha256(paths["bundles.private.jsonl"]),
            "requests_sha256": _file_sha256(paths["requests.private.jsonl"]),
            "cost_sha256": _file_sha256(paths["cost.private.json"]),
            "selection_mapping_sha256": _file_sha256(paths["selection_mapping.private.jsonl"]),
            "selected_package_audit_map_sha256": map_sha,
        },
        "source_guard": {
            "frozen_or_frozen_test_read": False,
            "gold_loaded": False,
            "body_fields_written": False,
            "identity_fields_written": False,
        },
        "counts": {
            "messages": len(messages),
            "decisions": len(decisions),
            "bundles": len(bundles),
            "complete_audited": len(complete),
            "pending_total": len(pending),
            "pending_sampled": sampled_pending,
            "selected": len(selected),
            "mapping_rows": len(selection_mapping),
        },
        "complete_field_summary": {
            field: _counter_dict(
                row.get("field_verdicts", {}).get(field, {}).get("status")
                for row in audit_rows
                if row.get("record_type") == "complete_bundle_audit"
            )
            for field in AUDIT_FIELDS
        },
        "complete_status_counts": _counter_dict(
            row.get("status")
            for row in audit_rows
            if row.get("record_type") == "complete_bundle_audit"
        ),
        "pending_strata": pending_strata_summary,
        "pending_status_counts": _counter_dict(
            row.get("status")
            for row in audit_rows
            if row.get("record_type") == "pending_bundle_audit"
        ),
        "activation_cue_audit": {
            "sampled_pending_rows": sampled_pending,
            "rows_with_executable_cues": sum(
                row.get("activation_cue", {}).get("status") == "pass"
                for row in audit_rows
                if row.get("record_type") == "pending_bundle_audit"
            ),
            "cue_coverage_rate": round(
                sum(
                    row.get("activation_cue", {}).get("status") == "pass"
                    for row in audit_rows
                    if row.get("record_type") == "pending_bundle_audit"
                ) / max(1, sampled_pending),
                4,
            ),
            "policy": "pending/cold rows require executable, replayable, scope-bound cues; no cue means not v2.9-ready",
        },
        "valuable_pending": {
            "sampled_valuable_pending_count": sum(
                row.get("value", {}).get("valuable_content_delayed") is True
                for row in audit_rows
                if row.get("record_type") == "pending_bundle_audit"
            ),
            "sampled_valuable_pending_with_reactivation": sum(
                row.get("value", {}).get("valuable_content_delayed") is True
                and row.get("activation_cue", {}).get("status") == "pass"
                for row in audit_rows
                if row.get("record_type") == "pending_bundle_audit"
            ),
            "valuable_pending_is_discarded_count": sum(
                row.get("value", {}).get("valuable_content_delayed") is True
                and row.get("value", {}).get("discarded") is True
                for row in audit_rows
                if row.get("record_type") == "pending_bundle_audit"
            ),
        },
        "provider_and_candidate_accounting": provider_accounting,
        "selected_value_coverage": selection_coverage,
        "selection_mapping": {
            "artifact_rows": len(selection_mapping),
            "candidate_package_match_count": mapping_match_count,
            "candidate_package_match_rate": round(mapping_match_count / max(1, len(decisions)), 4),
            "selected_package_match_count": selected_mapping_match_count,
            "selected_package_match_rate": round(selected_mapping_match_count / max(1, len(selected)), 4),
            "selected_activation_cue_preserved_count": selected_cue_preserved_count,
            "selected_activation_cue_preservation_rate": round(selected_cue_preserved_count / max(1, len(selected)), 4),
            "selected_package_map_file": "selected_package_audit_map.private.jsonl",
            "selected_package_map_row_count": len(selected),
        },
        "zero_tolerance": zero_tolerance,
        "unknown_policy": {
            "start_end_unknown_is_allowed": True,
            "silence_is_not_terminal": True,
            "unknown_is_not_resolved": True,
            "time_only_and_same_segment_are_not_strong_evidence": True,
        },
        "known_limitations": [
            "No frozen or gold labels were read; semantic correctness without an observable signal is uncertain.",
            "The Flash artifact exposes four complete outputs; all four were audited without substitution.",
            "Selection value coverage is measured against development message signals, not hidden labels.",
            "Snapshot activation_cues may be empty while decision-level replay cues remain executable; those surfaces are reported separately.",
        ],
        "status": (
            "pass"
            if len(complete) == 4
            and sampled_pending == PENDING_SAMPLE_SIZE
            else "blocked_by_sample_or_complete_gap"
        ),
    }
    summary["status"] = (
        "audited_with_quality_gaps"
        if len(complete) == 4
        and sampled_pending == PENDING_SAMPLE_SIZE
        and zero_tolerance.get("status") == "pass"
        and summary["activation_cue_audit"]["cue_coverage_rate"] == 1.0
        and selected_mapping_match_count == len(selected)
        and selected_cue_preserved_count == len(selected)
        else "blocked_by_sample_or_zero_tolerance"
    )

    output_values = [audit_rows, summary, map_rows]
    body_paths = sorted({path for value in output_values for path in _nested_body_keys(value)})
    if body_paths:
        raise AssertionError("body-shaped fields detected in audit output")
    _write_jsonl(audit_dir / "human_audit.private.jsonl", [_body_free(row) for row in audit_rows])
    _write_json(audit_dir / "audit_summary.private.json", _body_free(summary))
    return audit_rows, summary, map_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    _, summary, _ = run_audit(args.artifact_dir, args.input)
    print(
        "v2.9 Flash private audit written: complete=%d pending_sample=%d selected=%d cue_coverage=%s status=%s"
        % (
            int(summary["counts"]["complete_audited"]),
            int(summary["counts"]["pending_sampled"]),
            int(summary["counts"]["selected"]),
            str(summary["activation_cue_audit"]["cue_coverage_rate"]),
            str(summary["status"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
