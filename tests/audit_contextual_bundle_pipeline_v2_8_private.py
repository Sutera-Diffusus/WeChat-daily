"""Private, body-free qualitative audit for the v2.8 development artifact.

This is an audit helper, not production code.  It is intentionally scoped to
the development artifact named by the caller: it never traverses a
``frozen``/``frozen_test`` directory and it never writes message bodies,
display names, chat names, or raw source identifiers.  All emitted record
references are one-way opaque digests.

The audit is conservative: a field is marked ``uncertain`` when the available
metadata cannot establish correctness.  It does not turn an absent semantic
answer into a positive claim merely because the output schema is valid.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


REQUESTED_COMPLETE_COUNT = 10
PENDING_SAMPLE_SIZE = 20
CANONICAL_STATES = frozenset(
    {"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"}
)
CLAIM_TYPES = frozenset({"fact", "hypothesis", "opinion", "question", "suggestion", "unknown"})
MODALITIES = frozenset({"certain", "possible", "desired", "unknown"})
RESOLUTIONS = frozenset({"explicit", "inherited", "unknown"})
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})
BODY_KEYS = frozenset(
    {
        "content",
        "text",
        "raw_text",
        "redacted_text",
        "fragment_text",
        "fragment_text_redacted",
        "surface_text",
        "surface_redacted",
        "evidence_text",
        "quote",
        "summary",
        "narrative",
        "title",
        "display_name",
        "chat_name",
        "sender_name",
        "person_name",
        "object_name",
    }
)
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


def _load_json(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("JSONL row must be an object: %s" % path.name)
                rows.append(value)
    return rows


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _opaque(value: Any, namespace: str = "ref") -> str:
    digest = hashlib.sha256((namespace + "|" + str(value or "unknown")).encode("utf-8")).hexdigest()
    return "%s_%s" % (namespace, digest[:16])


def _safe_path(path: Path) -> None:
    lowered = "\\".join(path.parts).lower()
    if re.search(r"(?:^|\\)(?:frozen|frozen_test)(?:$|\\)", lowered):
        raise RuntimeError("refusing frozen path")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _counter_dict(values: Iterable[Any]) -> Dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _enum_result(status: str, codes: Iterable[str], **observed: Any) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "status": status,
        "error_codes": sorted(set(str(code) for code in codes if code)),
    }
    if observed:
        value["observed"] = observed
    return value


def _nested_body_keys(value: Any, path: str = "") -> List[str]:
    """Return body-key paths for the output privacy self-check only."""

    found: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = "%s.%s" % (path, key_text) if path else key_text
            if key_text.lower() in BODY_KEYS:
                found.append(child_path)
            found.extend(_nested_body_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_nested_body_keys(child, "%s[%d]" % (path, index)))
    return found


def _body_free(value: Any) -> Any:
    """Remove any accidental body-shaped fields before writing an audit row."""

    if isinstance(value, Mapping):
        return {
            str(key): _body_free(child)
            for key, child in value.items()
            if str(key).lower() not in BODY_KEYS
        }
    if isinstance(value, list):
        return [_body_free(child) for child in value]
    return value


def _message_signal(message: Mapping[str, Any]) -> Dict[str, Any]:
    media_state = str(message.get("media_state") or "unknown")
    message_type = str(message.get("message_type") or "unknown")
    message_role = str(message.get("message_role") or "unknown")
    redacted_text = str(message.get("redacted_text") or "")
    placeholder = (
        media_state == "placeholder"
        or message_type in {"image", "voice", "video", "emoji", "动画表情"}
        or (redacted_text.startswith("[") and redacted_text.endswith("]"))
    )
    text_present = bool(redacted_text) and not placeholder
    evidence_eligible = bool(message.get("evidence_eligible"))
    event_evidence_eligible = bool(message.get("event_evidence_eligible"))
    state_codes: List[str] = []
    state_patterns = {
        "planned": r"计划|准备|打算|安排|将|会",
        "ongoing": r"进行中|正在|目前|持续|处理中",
        "resolved": r"完成|好了|已|解决|恢复|结束",
        "failed": r"失败|不行|崩|报错",
        "cancelled": r"取消|撤销|不做",
    }
    for state, pattern in state_patterns.items():
        if re.search(pattern, redacted_text):
            state_codes.append(state)
    question_signal = bool(re.search(r"请问|吗|？|\?", redacted_text))
    context_ids = message.get("context_message_ids")
    context_signal = isinstance(context_ids, list) and bool(context_ids)
    explicit_reply = (
        bool(message.get("reply_to_message_id"))
        and str(message.get("reply_metadata_state") or "") in {"explicit", "present"}
    )
    valuable = evidence_eligible or event_evidence_eligible or (text_present and message_role == "substantive")
    return {
        "valuable": valuable,
        "evidence_eligible": evidence_eligible,
        "event_evidence_eligible": event_evidence_eligible,
        "media_placeholder": placeholder,
        "text_present": text_present,
        "state_codes": state_codes,
        "question_signal": question_signal,
        "context_signal": context_signal,
        "explicit_reply": explicit_reply,
        "person_redaction": "PERSON" in {str(x) for x in (message.get("redaction_types") or [])},
        "message_role": message_role,
        "media_state": media_state,
    }


def _bundle_messages(bundle: Mapping[str, Any], messages: Mapping[str, Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    values: List[Mapping[str, Any]] = []
    for message_id in bundle.get("source_message_ids") or bundle.get("member_message_ids") or []:
        item = messages.get(str(message_id))
        if item is not None:
            values.append(item)
    return values


def _aggregate_signals(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    signals = [_message_signal(item) for item in items]
    return {
        "message_count": len(items),
        "valuable_message_count": sum(bool(x["valuable"]) for x in signals),
        "evidence_eligible_count": sum(bool(x["evidence_eligible"]) for x in signals),
        "event_evidence_eligible_count": sum(bool(x["event_evidence_eligible"]) for x in signals),
        "media_placeholder_count": sum(bool(x["media_placeholder"]) for x in signals),
        "text_present_count": sum(bool(x["text_present"]) for x in signals),
        "state_signal_count": sum(bool(x["state_codes"]) for x in signals),
        "question_signal_count": sum(bool(x["question_signal"]) for x in signals),
        "context_signal_count": sum(bool(x["context_signal"]) for x in signals),
        "explicit_reply_count": sum(bool(x["explicit_reply"]) for x in signals),
        "person_redaction_count": sum(bool(x["person_redaction"]) for x in signals),
        "all_media_or_placeholder": bool(signals) and all(bool(x["media_placeholder"]) for x in signals),
    }


def _evidence_check(semantic: Mapping[str, Any], bundle: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = semantic.get("evidence")
    message_ids = {str(value) for value in (semantic.get("message_ids") or bundle.get("source_message_ids") or [])}
    if not isinstance(evidence, list):
        return _enum_result("fail", ["EVIDENCE_FIELD_MISSING"], count=0, valid_count=0)
    valid = 0
    codes: List[str] = []
    fields: List[str] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            codes.append("EVIDENCE_ITEM_NOT_OBJECT")
            continue
        required = {"evidence_id", "field", "kind", "message_id", "span"}
        if not required <= set(item):
            codes.append("EVIDENCE_SCHEMA_MISSING")
            continue
        if str(item.get("message_id")) not in message_ids:
            codes.append("EVIDENCE_MESSAGE_UNBOUND")
            continue
        span = item.get("span")
        if not isinstance(span, Mapping) or not {"start", "end"} <= set(span):
            codes.append("EVIDENCE_SPAN_MISSING")
            continue
        field = str(item.get("field") or "")
        if not field:
            codes.append("EVIDENCE_FIELD_EMPTY")
            continue
        fields.append(field)
        valid += 1
    if evidence and valid == len(evidence):
        status = "pass"
    elif valid:
        status = "uncertain"
    else:
        status = "fail"
    if not valid and str(bundle.get("event_completeness") or "") in {"unknown", "not_applicable"}:
        codes.append("EVIDENCE_NOT_TYPED")
    return _enum_result(status, codes, count=len(evidence), valid_count=valid, field_count=len(set(fields)))


def _field_audit(
    semantic: Mapping[str, Any],
    bundle: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    signals = [_message_signal(item) for item in messages]
    signal = _aggregate_signals(messages)
    valuable = bool(signal["valuable_message_count"])
    media_only = bool(signal["all_media_or_placeholder"])
    errors: List[str] = []
    fields: Dict[str, Dict[str, Any]] = {}

    speaker = semantic.get("speaker")
    evidence_ids = {
        str(item.get("evidence_id"))
        for item in (semantic.get("evidence") or [])
        if isinstance(item, Mapping)
    }
    if (
        isinstance(speaker, Mapping)
        and {"id", "resolution", "role", "type", "evidence_ids"} <= set(speaker)
        and str(speaker.get("id") or "") not in {"", "unknown"}
        and str(speaker.get("resolution") or "") in RESOLUTIONS
        and str(speaker.get("role") or "") == "speaker"
        and isinstance(speaker.get("evidence_ids"), list)
        and set(map(str, speaker.get("evidence_ids") or [])) <= evidence_ids
    ):
        fields["speaker_metadata"] = _enum_result("pass", [], known=True, evidence_bound=True)
    else:
        fields["speaker_metadata"] = _enum_result("fail", ["SPEAKER_METADATA_OR_EVIDENCE_INVALID"])
        errors.append("SPEAKER_METADATA_OR_EVIDENCE_INVALID")

    mentioned = semantic.get("mentioned_person")
    if not isinstance(mentioned, list):
        fields["mentioned_person"] = _enum_result("fail", ["MENTIONED_PERSON_FIELD_INVALID"])
        errors.append("MENTIONED_PERSON_FIELD_INVALID")
    elif mentioned:
        fields["mentioned_person"] = _enum_result("pass", [], count=len(mentioned), known=True)
    elif signal["person_redaction_count"]:
        fields["mentioned_person"] = _enum_result("uncertain", ["MENTIONED_PERSON_SIGNAL_NOT_DISAMBIGUATED"], count=0)
        errors.append("MENTIONED_PERSON_SIGNAL_NOT_DISAMBIGUATED")
    else:
        fields["mentioned_person"] = _enum_result("uncertain", ["MENTIONED_PERSON_NO_GOLD_SIGNAL"], count=0)
        errors.append("MENTIONED_PERSON_NO_GOLD_SIGNAL")

    subject = semantic.get("subject")
    subject_unknown = not isinstance(subject, Mapping) or str(subject.get("id") or "unknown") == "unknown"
    if not isinstance(subject, Mapping):
        fields["subject"] = _enum_result("fail", ["SUBJECT_FIELD_INVALID"])
        errors.append("SUBJECT_FIELD_INVALID")
    elif subject_unknown and valuable and not media_only:
        fields["subject"] = _enum_result("fail", ["SUBJECT_UNKNOWN_ON_VALUABLE"], unknown=True)
        errors.append("SUBJECT_UNKNOWN_ON_VALUABLE")
    elif subject_unknown:
        fields["subject"] = _enum_result("pass", [], unknown=True, boundary="unknown_allowed")
    else:
        fields["subject"] = _enum_result("pass", [], unknown=False)

    target = semantic.get("target")
    if not isinstance(target, list):
        fields["target"] = _enum_result("fail", ["TARGET_FIELD_INVALID"])
        errors.append("TARGET_FIELD_INVALID")
    elif target:
        fields["target"] = _enum_result("pass", [], count=len(target), known=True)
    elif media_only:
        fields["target"] = _enum_result("pass", [], count=0, boundary="unknown_allowed")
    else:
        fields["target"] = _enum_result("uncertain", ["TARGET_UNKNOWN_WITHOUT_GOLD"], count=0)
        errors.append("TARGET_UNKNOWN_WITHOUT_GOLD")

    objects = semantic.get("object")
    if not isinstance(objects, list):
        fields["object"] = _enum_result("fail", ["OBJECT_FIELD_INVALID"])
        errors.append("OBJECT_FIELD_INVALID")
    elif objects:
        fields["object"] = _enum_result("pass", [], count=len(objects), known=True)
    elif media_only:
        fields["object"] = _enum_result("pass", [], count=0, boundary="unknown_allowed")
    else:
        fields["object"] = _enum_result("uncertain", ["OBJECT_UNKNOWN_WITHOUT_GOLD"], count=0)
        errors.append("OBJECT_UNKNOWN_WITHOUT_GOLD")

    actions = semantic.get("action")
    if not isinstance(actions, list):
        fields["action"] = _enum_result("fail", ["ACTION_FIELD_INVALID"])
        errors.append("ACTION_FIELD_INVALID")
    elif actions:
        fields["action"] = _enum_result("pass", [], count=len(actions), known=True)
    elif media_only:
        fields["action"] = _enum_result("pass", [], count=0, boundary="unknown_allowed")
    elif valuable:
        fields["action"] = _enum_result("uncertain", ["ACTION_UNKNOWN_ON_VALUABLE"], count=0)
        errors.append("ACTION_UNKNOWN_ON_VALUABLE")
    else:
        fields["action"] = _enum_result("uncertain", ["ACTION_NO_GOLD_SIGNAL"], count=0)
        errors.append("ACTION_NO_GOLD_SIGNAL")

    claim_type = str(semantic.get("claim_type") or "unknown")
    if claim_type not in CLAIM_TYPES:
        fields["claim_type"] = _enum_result("fail", ["CLAIM_TYPE_ENUM_INVALID"])
        errors.append("CLAIM_TYPE_ENUM_INVALID")
    elif claim_type == "unknown" and valuable and not media_only:
        fields["claim_type"] = _enum_result("fail", ["CLAIM_TYPE_UNKNOWN_ON_VALUABLE"], value="unknown")
        errors.append("CLAIM_TYPE_UNKNOWN_ON_VALUABLE")
    elif claim_type == "unknown":
        fields["claim_type"] = _enum_result("pass", [], value="unknown", boundary="unknown_allowed")
    else:
        fields["claim_type"] = _enum_result("pass", [], value="known")

    state = str(semantic.get("state") or "unknown")
    state_signal = any(bool(item["state_codes"]) for item in signals)
    if state not in CANONICAL_STATES:
        fields["state"] = _enum_result("fail", ["STATE_ENUM_INVALID"])
        errors.append("STATE_ENUM_INVALID")
    elif state == "unknown" and state_signal:
        fields["state"] = _enum_result("fail", ["STATE_UNKNOWN_WITH_SIGNAL"], value="unknown")
        errors.append("STATE_UNKNOWN_WITH_SIGNAL")
    elif state == "unknown" and media_only:
        fields["state"] = _enum_result("pass", [], value="unknown", boundary="unknown_allowed")
    elif state == "unknown":
        fields["state"] = _enum_result("uncertain", ["STATE_UNKNOWN_NO_GOLD_SIGNAL"], value="unknown")
        errors.append("STATE_UNKNOWN_NO_GOLD_SIGNAL")
    else:
        fields["state"] = _enum_result("pass", [], value="known")

    modality = str(semantic.get("modality") or "unknown")
    if modality not in MODALITIES:
        fields["modality"] = _enum_result("fail", ["MODALITY_ENUM_INVALID"])
        errors.append("MODALITY_ENUM_INVALID")
    elif modality == "unknown" and valuable and not media_only:
        fields["modality"] = _enum_result("uncertain", ["MODALITY_UNKNOWN_ON_VALUABLE"], value="unknown")
        errors.append("MODALITY_UNKNOWN_ON_VALUABLE")
    elif modality == "unknown":
        fields["modality"] = _enum_result("pass", [], value="unknown", boundary="unknown_allowed")
    else:
        fields["modality"] = _enum_result("pass", [], value="known")

    coreference = semantic.get("coreference_candidates")
    context_ids_present = any(bool(item.get("context_message_ids")) for item in messages if isinstance(item, Mapping))
    if not isinstance(coreference, list):
        fields["coreference"] = _enum_result("fail", ["COREFERENCE_FIELD_INVALID"])
        errors.append("COREFERENCE_FIELD_INVALID")
    elif context_ids_present and not coreference:
        fields["coreference"] = _enum_result("fail", ["COREFERENCE_NOT_PROJECTED"], count=0)
        errors.append("COREFERENCE_NOT_PROJECTED")
    elif coreference:
        fields["coreference"] = _enum_result("pass", [], count=len(coreference))
    else:
        fields["coreference"] = _enum_result("pass", [], count=0, boundary="no_cue_observed")

    relations = semantic.get("context_relations")
    declared_relation_ids = bundle.get("context_relation_ids") or []
    relation_cue = any(bool(item["context_signal"] or item["explicit_reply"]) for item in signals)
    if not isinstance(relations, list):
        fields["context_relation"] = _enum_result("fail", ["CONTEXT_RELATION_FIELD_INVALID"])
        errors.append("CONTEXT_RELATION_FIELD_INVALID")
    elif declared_relation_ids and not relations:
        fields["context_relation"] = _enum_result("fail", ["CONTEXT_RELATION_NOT_PROJECTED"], count=0)
        errors.append("CONTEXT_RELATION_NOT_PROJECTED")
    elif relations:
        labels = [str(item.get("relation") or item.get("label") or "") for item in relations if isinstance(item, Mapping)]
        bad = [label for label in labels if label not in RELATION_LABELS]
        fields["context_relation"] = _enum_result(
            "fail" if bad else "pass",
            ["CONTEXT_RELATION_ENUM_INVALID"] if bad else [],
            count=len(relations),
        )
        if bad:
            errors.append("CONTEXT_RELATION_ENUM_INVALID")
    elif relation_cue:
        fields["context_relation"] = _enum_result("uncertain", ["CONTEXT_RELATION_CUE_UNRESOLVED"], count=0)
        errors.append("CONTEXT_RELATION_CUE_UNRESOLVED")
    else:
        fields["context_relation"] = _enum_result("pass", [], count=0, boundary="no_cue_observed")

    evidence = _evidence_check(semantic, bundle)
    fields["evidence"] = evidence
    errors.extend(str(code) for code in evidence.get("error_codes") or [])

    unknown_fields = sum(
        1
        for key in ("subject", "target", "object", "action", "claim_type", "state", "modality")
        if fields.get(key, {}).get("observed", {}).get("unknown") is True
        or fields.get(key, {}).get("observed", {}).get("boundary") == "unknown_allowed"
    )
    return fields, {
        "signals": signal,
        "valuable": valuable,
        "media_only": media_only,
        "error_codes": sorted(set(errors)),
        "unknown_field_count": unknown_fields,
    }


def _overall_status(fields: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = {str(value.get("status")) for value in fields.values()}
    if "fail" in statuses:
        return "fail"
    if "uncertain" in statuses:
        return "uncertain"
    return "pass"


def _severity(status: str, codes: Sequence[str]) -> str:
    if status == "fail":
        high = {
            "SUBJECT_UNKNOWN_ON_VALUABLE",
            "CLAIM_TYPE_UNKNOWN_ON_VALUABLE",
            "STATE_UNKNOWN_WITH_SIGNAL",
            "EVIDENCE_MESSAGE_UNBOUND",
            "EVIDENCE_FIELD_MISSING",
        }
        return "high" if any(code in high for code in codes) else "medium"
    if status == "uncertain":
        return "low"
    return "none"


def _request_index(requests: Sequence[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    result: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for request in requests:
        result[str(request.get("bundle_id"))].append(request)
    return result


def _gate_index(gate_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {str(row.get("message_id")): row for row in gate_rows}


def _gate_for_bundle(bundle: Mapping[str, Any], gate_by_message: Mapping[str, Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [
        gate_by_message[str(message_id)]
        for message_id in (bundle.get("source_message_ids") or bundle.get("member_message_ids") or [])
        if str(message_id) in gate_by_message
    ]


def _pending_bucket(
    decision: Mapping[str, Any],
    requests: Sequence[Mapping[str, Any]],
    gates: Sequence[Mapping[str, Any]],
) -> set[str]:
    buckets: set[str] = set()
    errors = {str(item.get("error_code")) for item in requests if item.get("error_code")}
    if "input_token_limit_exceeded" in errors:
        buckets.add("input_limit")
    if str(decision.get("source")) == "model_failed":
        buckets.add("model_failed")
    if str(decision.get("source")) == "budget":
        buckets.add("budget")
    gate_channels = {str(item.get("channel")) for item in gates}
    if "background" in gate_channels:
        buckets.add("gate_background")
    if "pending_context" in gate_channels:
        buckets.add("gate_pending_context")
    if not buckets:
        buckets.add("other_pending")
    return buckets


def _sample_pending(
    decisions: Sequence[Mapping[str, Any]],
    bundles_by_id: Mapping[str, Mapping[str, Any]],
    requests_by_id: Mapping[str, Sequence[Mapping[str, Any]]],
    gate_by_message: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    snapshots_by_id: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    pending = [row for row in decisions if str(row.get("status")) == "pending"]
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in pending:
        bundle = bundles_by_id.get(str(row.get("bundle_id")), {})
        gates = _gate_for_bundle(bundle, gate_by_message)
        requests = list(requests_by_id.get(str(row.get("bundle_id")), []))
        for bucket in _pending_bucket(row, requests, gates):
            grouped[bucket].append(row)

    selected: List[Tuple[str, Mapping[str, Any]]] = []
    taken: set[str] = set()
    quotas = {
        "input_limit": 3,
        "model_failed": 3,
        "gate_background": 4,
        "gate_pending_context": 4,
        "budget": 6,
    }
    for bucket, quota in quotas.items():
        candidates = sorted(grouped.get(bucket, []), key=lambda row: _opaque(row.get("bundle_id"), "sort"))
        count = 0
        for row in candidates:
            bundle_id = str(row.get("bundle_id"))
            if bundle_id in taken:
                continue
            selected.append((bucket, row))
            taken.add(bundle_id)
            count += 1
            if count >= quota:
                break
    if len(selected) < PENDING_SAMPLE_SIZE:
        remaining = sorted(
            (row for row in pending if str(row.get("bundle_id")) not in taken),
            key=lambda row: _opaque(row.get("bundle_id"), "sort"),
        )
        for row in remaining:
            selected.append(("other_pending", row))
            taken.add(str(row.get("bundle_id")))
            if len(selected) >= PENDING_SAMPLE_SIZE:
                break

    output: List[Dict[str, Any]] = []
    for bucket, row in selected[:PENDING_SAMPLE_SIZE]:
        bundle_id = str(row.get("bundle_id"))
        bundle = bundles_by_id.get(bundle_id, {})
        gates = _gate_for_bundle(bundle, gate_by_message)
        requests = list(requests_by_id.get(bundle_id, []))
        source_messages = _bundle_messages(bundle, messages)
        signal = _aggregate_signals(source_messages)
        has_valuable = bool(signal["valuable_message_count"])
        snapshot_cue_count = 0
        recoverable_line_present = False
        snapshot_id = bundle.get("open_context_snapshot_id")
        snapshot = snapshots_by_id.get(str(snapshot_id)) if snapshot_id else None
        if snapshot is not None:
            snapshot_cue_count = len(snapshot.get("activation_cues") or [])
            recoverable_line_present = bool(
                snapshot.get("unresolved_slots")
                or snapshot.get("pending_relation_candidates")
                or snapshot.get("open_thread_ids")
                or snapshot.get("recent_claim_ids")
                or snapshot.get("recent_fragment_ids")
            )
        if has_valuable:
            value_status, value_codes, value_severity = "fail", ["VALUABLE_CONTENT_PENDING"], "high"
        elif signal["all_media_or_placeholder"]:
            value_status, value_codes, value_severity = "pass", ["NON_SUBSTANTIVE_MEDIA_PENDING_ALLOWED"], "none"
        else:
            value_status, value_codes, value_severity = "uncertain", ["VALUE_SIGNAL_INSUFFICIENT"], "low"
        if recoverable_line_present:
            recover_status, recover_codes = "pass", ["RECOVERABLE_CONTEXT_CUE_PRESENT"]
        else:
            recover_status, recover_codes = "uncertain", ["RECOVERABLE_CONTEXT_CUE_UNOBSERVED"]
        error_codes = list(value_codes) + list(recover_codes)
        request_errors = sorted({str(item.get("error_code")) for item in requests if item.get("error_code")})
        if "input_token_limit_exceeded" in request_errors:
            error_codes.append("INPUT_LIMIT_RECOVERABLE")
        if str(row.get("source")) == "model_failed":
            error_codes.append("MODEL_FAILURE_RETRYABLE")
        if str(row.get("source")) == "budget":
            error_codes.append("BUDGET_PENDING")
        if "gate_pending_context" in _pending_bucket(row, requests, gates):
            error_codes.append("GATE_PENDING_CONTEXT_REVERSIBLE")
        if "gate_background" in _pending_bucket(row, requests, gates):
            error_codes.append("GATE_BACKGROUND_LOW_COST")
        output.append(
            {
                "record_type": "pending_bundle_sample",
                "audit_ref": _opaque(bundle_id, "pending"),
                "bucket": bucket,
                "source": str(row.get("source") or "unknown"),
                "status": str(row.get("status") or "unknown"),
                "channel": str(row.get("channel") or "unknown"),
                "scale": str(row.get("scale") or "unknown"),
                "source_message_count": len(source_messages),
                "signal": signal,
                "gate": {
                    "row_count": len(gates),
                    "channels": _counter_dict(item.get("channel") for item in gates),
                    "reversible_all": bool(gates) and all(item.get("reversible") is True for item in gates),
                    "metadata_authoritative_all": bool(gates) and all(item.get("metadata_authoritative") is True for item in gates),
                    "metadata_complete_all": bool(gates) and all(item.get("metadata_complete") is True for item in gates),
                    "activation_cue_count": sum(len(item.get("activation_cues") or []) for item in gates),
                },
                "requests": {
                    "row_count": len(requests),
                    "error_codes": request_errors,
                    "attempts": len(requests),
                },
                "open_context": {
                    "snapshot_present": bool(snapshot_id),
                    "snapshot_ref": _opaque(snapshot_id, "snapshot") if snapshot_id else None,
                    "activation_cue_count": snapshot_cue_count,
                    "activation_cue_observed": bool(snapshot_cue_count),
                    "recoverable_line_present": recoverable_line_present,
                },
                "judgement": {
                    "valuable_content_delayed": {
                        "status": value_status,
                        "error_codes": sorted(set(value_codes)),
                    },
                    "recoverable_cue": {
                        "status": recover_status,
                        "error_codes": sorted(set(recover_codes)),
                    },
                },
                "error_codes": sorted(set(error_codes)),
                "severity": value_severity,
            }
        )
    return output


def _v1_join_attempt(
    audit_row: Mapping[str, Any],
    v1_predictions: Sequence[Mapping[str, Any]],
    v2_bundles: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    target_ref = str(audit_row.get("target_ref") or "")
    # The historical audit intentionally stores only an opaque target token.
    # Try exact target-token joins against body-free IDs, but never emit either
    # side of a failed join.  A digest is not reversible and cannot be guessed.
    v1_matches: List[Mapping[str, Any]] = []
    v2_matches: List[Mapping[str, Any]] = []
    for row in v1_predictions:
        for key, value in row.items():
            if key in BODY_KEYS:
                continue
            if isinstance(value, str) and value == target_ref:
                v1_matches.append(row)
                break
    for row in v2_bundles:
        for key, value in row.items():
            if key in BODY_KEYS:
                continue
            if isinstance(value, str) and value == target_ref:
                v2_matches.append(row)
                break
            if isinstance(value, list) and target_ref in {str(item) for item in value if isinstance(item, str)}:
                v2_matches.append(row)
                break
    if len(v1_matches) == 1 and len(v2_matches) == 1:
        return {
            "status": "mapped",
            "v1_prediction_ref": _opaque(v1_matches[0].get("prediction_id"), "v1"),
            "v2_bundle_ref": _opaque(v2_matches[0].get("bundle_id"), "v2"),
            "comparison": "N/A_no_gold_for_v2_same_sample",
            "reason": None,
        }
    return {
        "status": "N/A",
        "v1_prediction_ref": None,
        "v2_bundle_ref": None,
        "comparison": "N/A",
        "reason": "V1_TARGET_REF_NOT_JOINABLE_TO_V1_OR_V2_SOURCE_REF",
    }


def _compare_v1(
    v1_audit: Sequence[Mapping[str, Any]],
    v1_predictions: Sequence[Mapping[str, Any]],
    v2_bundles: Sequence[Mapping[str, Any]],
    v1_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for item in v1_audit:
        join = _v1_join_attempt(item, v1_predictions, v2_bundles)
        rows.append(
            {
                "record_type": "v1_v28_sample_comparison",
                "v1_audit_ref": _opaque(item.get("audit_ref"), "v1audit"),
                "target_type": str(item.get("target_type") or "unknown"),
                "stratum": str(item.get("stratum") or "unknown"),
                "v1_status": str(item.get("status") or "unknown"),
                "v1_severity": str(item.get("severity") or "unknown"),
                "v1_error_codes": sorted(str(code) for code in (item.get("error_codes") or [])),
                **join,
            }
        )
    return {
        "schema_version": "contextual_bundle_v2_8_v1_comparison_private_v1",
        "scope": "development",
        "source_guard": {
            "frozen_or_frozen_test_read": False,
            "body_fields_written": False,
            "identity_fields_written": False,
        },
        "v1_manifest": {
            "schema_version": str(v1_manifest.get("schema_version") or "unknown"),
            "dataset_split": str(v1_manifest.get("dataset_split") or v1_manifest.get("split") or "unknown"),
            "input_sha256": str(v1_manifest.get("input_sha256") or ""),
        },
        "sample_count": len(rows),
        "mapped_count": sum(row.get("status") == "mapped" for row in rows),
        "na_count": sum(row.get("status") == "N/A" for row in rows),
        "na_reason_counts": _counter_dict(row.get("reason") for row in rows if row.get("reason")),
        "comparable_accuracy": "N/A",
        "mapping_rule": "exact opaque target_ref only; no guessed ID/hash inversion",
        "samples": rows,
    }


def _relation_metrics(relations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    time_only = 0
    same_segment_unsafe = 0
    cross_chat = 0
    for relation in relations:
        evidence = [str(item) for item in (relation.get("evidence") or relation.get("supporting_signals") or [])]
        if evidence and set(evidence) <= {"time", "time_proximity", "weak_time"}:
            time_only += 1
        if str(relation.get("segment_relation") or relation.get("relation_subtype") or "") in {"same_segment_time", "same_segment"} and str(relation.get("strength") or "") == "strong":
            same_segment_unsafe += 1
        if relation.get("cross_chat") is True or relation.get("chat_scope") == "cross_chat":
            cross_chat += 1
    return {
        "relation_count": len(relations),
        "time_only_relation_violation_count": time_only,
        "same_segment_unsafe_strong_count": same_segment_unsafe,
        "cross_chat_relation_violation_count": cross_chat,
        "time_only_status": "pass" if time_only == 0 else "fail",
        "same_segment_status": "N/A_no_relation_records" if not relations else ("pass" if same_segment_unsafe == 0 else "fail"),
        "cross_chat_status": "pass" if cross_chat == 0 else "fail",
    }


def _request_metrics(
    requests: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    *,
    provider_source: str = "openai",
) -> Dict[str, Any]:
    """Return disjoint request/decision counters with explicit denominators.

    The runner writes several ledgers for one candidate: a decision is emitted
    for every candidate bundle, a provider row is emitted for an actual
    reservation/attempt, and a ``model_wire`` row records a normalized model
    output.  Budget rejections are deferred work, not provider attempts.  Keep
    those ledgers separate so ``semantic_model_calls`` cannot be mistaken for
    the number of provider requests.
    """
    provider_attempt_statuses = frozenset({"started", "complete", "failed"})
    provider_rows = [
        row for row in requests
        if str(row.get("source") or "") == provider_source
    ]
    provider_attempt_rows = [
        row for row in provider_rows
        if str(row.get("status") or "") in provider_attempt_statuses
    ]
    successful_output_rows = [
        row for row in requests
        if str(row.get("source") or "") == "model_wire"
        and str(row.get("status") or "") == "complete"
    ]
    failed_provider_rows = [
        row for row in provider_rows
        if str(row.get("status") or "") == "failed"
    ]
    unfinished_provider_rows = [
        row for row in provider_rows
        if str(row.get("status") or "") == "started"
    ]
    budget_deferred = sum(
        str(row.get("status") or "") == "pending"
        and str(row.get("source") or "") == "budget"
        for row in decisions
    )
    selected_bundle_refs = {
        str(row.get("bundle_id"))
        for row in provider_attempt_rows
        if row.get("bundle_id") is not None
    }
    return {
        "provider_request_attempts": len(provider_attempt_rows),
        "successful_model_outputs": len(successful_output_rows),
        "failed_provider_attempts": len(failed_provider_rows),
        "unfinished_provider_attempts": len(unfinished_provider_rows),
        "candidate_decisions": len(decisions),
        "budget_deferred": budget_deferred,
        "selected_bundle_count": len(selected_bundle_refs),
        "request_status_counts": _counter_dict(row.get("status") for row in requests),
        "request_source_status_counts": _counter_dict(
            "%s/%s" % (str(row.get("source") or "unknown"), str(row.get("status") or "unknown"))
            for row in requests
        ),
    }


def _aggregate_field_metrics(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    fields = [
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
    ]
    out: Dict[str, Any] = {}
    for field in fields:
        counts = Counter(
            str((record.get("field_verdicts") or {}).get(field, {}).get("status") or "missing")
            for record in records
        )
        out[field] = dict(sorted(counts.items()))
    return out


def run_audit(artifact_dir: Path, input_path: Path, v1_dir: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    for path in (artifact_dir, input_path.parent, v1_dir):
        _safe_path(path)
    manifest_path = artifact_dir / "manifest.private.json"
    aggregate_path = artifact_dir / "aggregate.private.json"
    cost_path = artifact_dir / "cost.private.json"
    decisions_path = artifact_dir / "decisions.private.jsonl"
    bundles_path = artifact_dir / "bundles.private.jsonl"
    requests_path = artifact_dir / "requests.private.jsonl"
    gate_path = artifact_dir / "gate.private.json"
    snapshots_path = artifact_dir / "open_context_snapshots.private.jsonl"
    relations_path = artifact_dir / "relations.private.jsonl"
    for path in (manifest_path, aggregate_path, cost_path, decisions_path, bundles_path, requests_path, gate_path, snapshots_path, relations_path):
        if not path.exists():
            raise FileNotFoundError(path)

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    aggregate = _load_json(aggregate_path)
    cost = _load_json(cost_path)
    decisions = _load_jsonl(decisions_path)
    bundles = _load_jsonl(bundles_path)
    requests = _load_jsonl(requests_path)
    gate = _load_json(gate_path)
    snapshots = _load_jsonl(snapshots_path)
    relations = _load_jsonl(relations_path)
    messages = {str(row.get("message_id")): row for row in _load_jsonl(input_path)}

    v1_audit_path = v1_dir / "audit" / "human_audit.private.jsonl"
    v1_manifest_path = v1_dir / "manifest.private.json"
    v1_predictions_path = v1_dir / "predictions.private.jsonl"
    for path in (v1_audit_path, v1_manifest_path, v1_predictions_path):
        if not path.exists():
            raise FileNotFoundError(path)
    v1_audit = _load_jsonl(v1_audit_path)
    v1_manifest = _load_json(v1_manifest_path)
    v1_predictions = _load_jsonl(v1_predictions_path)

    if str(manifest.get("split")) != "development" or str(manifest.get("input_directory_name")) != "development":
        raise AssertionError("v2.8 artifact is not development scoped")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise AssertionError("v2.8 artifact reports frozen/gold reads")
    if _file_sha256(input_path) != str(manifest.get("input_sha256")):
        raise AssertionError("development input hash mismatch")
    if str(v1_manifest.get("dataset_split") or v1_manifest.get("split")) != "development":
        raise AssertionError("v1 comparison artifact is not development scoped")
    if v1_manifest.get("frozen_read") is True or v1_manifest.get("gold_loaded") is True:
        raise AssertionError("v1 comparison artifact reports frozen/gold reads")
    if len(v1_audit) != 48:
        raise AssertionError("expected 48 v1 audit rows")

    bundle_by_id = {str(row.get("bundle_id")): row for row in bundles}
    requests_by_id = _request_index(requests)
    gate_by_message = _gate_index(gate.get("decisions") or [])
    snapshot_by_id = {str(row.get("snapshot_id")): row for row in snapshots}
    complete = [row for row in decisions if str(row.get("status")) == "complete"]
    pending = [row for row in decisions if str(row.get("status")) == "pending"]

    audit_rows: List[Dict[str, Any]] = [
        {
            "record_type": "audit_metadata",
            "schema_version": "contextual_bundle_v2_8_human_audit_private_v1",
            "scope": "development",
            "artifact_version": str(manifest.get("artifact_version") or "contextual_bundle_pipeline_v2_8"),
            "split": str(manifest.get("split") or "unknown"),
            "manifest_ref": _opaque(_sha256_bytes(manifest_bytes), "manifest"),
            "input_ref": _opaque(manifest.get("input_sha256"), "input"),
            "source_guard": {
                "frozen_or_frozen_test_read": False,
                "gold_loaded": False,
                "development_input_read": bool(manifest.get("development_input_read")),
                "body_fields_written": False,
                "identity_fields_written": False,
            },
            "requested_complete_count": REQUESTED_COMPLETE_COUNT,
            "observed_complete_count": len(complete),
            "observed_pending_count": len(pending),
        }
    ]

    complete_records: List[Dict[str, Any]] = []
    cross_chat_bundle_violations = 0
    open_boundary_violations = 0
    unknown_start_count = 0
    unknown_end_count = 0
    silence_terminal_violations = 0
    evidence_valid_count = 0
    valuable_pending_bundle_count = 0
    valuable_background_pending_count = 0
    reactivation_cue_pending_count = 0
    all_pending_message_count = 0
    all_pending_unique_messages: set[str] = set()

    for decision in complete:
        bundle_id = str(decision.get("bundle_id"))
        bundle = bundle_by_id.get(bundle_id, {})
        semantic = decision.get("semantic_bundle") if isinstance(decision.get("semantic_bundle"), Mapping) else {}
        source_messages = _bundle_messages(bundle, messages)
        field_verdicts, detail = _field_audit(semantic, bundle, source_messages)
        overall = _overall_status(field_verdicts)
        codes = list(detail.get("error_codes") or [])
        if field_verdicts.get("evidence", {}).get("status") == "pass":
            evidence_valid_count += 1
        source_chat_ids = {str(item.get("chat_id")) for item in source_messages if item.get("chat_id") is not None}
        bundle_chat = str(bundle.get("chat_id") or "")
        if source_chat_ids and bundle_chat and source_chat_ids != {bundle_chat}:
            cross_chat_bundle_violations += 1
            codes.append("CROSS_CHAT_BUNDLE_SCOPE")
        if bundle.get("open_boundary") is not True or bundle.get("closed") is True:
            open_boundary_violations += 1
            codes.append("OPEN_BOUNDARY_NOT_PRESERVED")
        if str(bundle.get("start_time_source") or "") in {"unknown", ""}:
            unknown_start_count += 1
        if str(bundle.get("end_time_source") or "") in {"unknown", ""}:
            unknown_end_count += 1
        if str(bundle.get("latest_state") or "unknown") in TERMINAL_STATES and all(
            _message_signal(item)["media_placeholder"] for item in source_messages
        ):
            silence_terminal_violations += 1
            codes.append("SILENCE_AS_TERMINAL_STATE")
        complete_records.append(
            {
                "record_type": "complete_bundle_audit",
                "audit_ref": _opaque(bundle_id, "complete"),
                "source": str(decision.get("source") or "unknown"),
                "channel": str(decision.get("channel") or "unknown"),
                "scale": str(decision.get("scale") or "unknown"),
                "decision_status": "complete",
                "source_message_count": len(source_messages),
                "source_message_ref_count": len(bundle.get("source_message_ids") or []),
                "field_verdicts": field_verdicts,
                "signal": detail.get("signals") or {},
                "unknown_field_count": int(detail.get("unknown_field_count") or 0),
                "boundary": {
                    "open_boundary": bundle.get("open_boundary") is True,
                    "closed": bundle.get("closed") is True,
                    "start_time_unknown": str(bundle.get("start_time_source") or "") in {"unknown", ""},
                    "end_time_unknown": str(bundle.get("end_time_source") or "") in {"unknown", ""},
                    "latest_state_terminal": str(bundle.get("latest_state") or "unknown") in TERMINAL_STATES,
                },
                "evidence": {
                    "valid_typed_count": field_verdicts.get("evidence", {}).get("observed", {}).get("valid_count", 0),
                    "coverage_status": field_verdicts.get("evidence", {}).get("status"),
                },
                "status": overall,
                "error_codes": sorted(set(codes)),
                "severity": _severity(overall, sorted(set(codes))),
            }
        )
    audit_rows.extend(complete_records)

    pending_records = _sample_pending(
        decisions,
        bundle_by_id,
        requests_by_id,
        gate_by_message,
        messages,
        snapshot_by_id,
    )
    for record in pending_records:
        audit_rows.append(record)
        if record.get("judgement", {}).get("valuable_content_delayed", {}).get("status") == "fail":
            valuable_pending_bundle_count += 1
            if str(record.get("channel")) == "background":
                valuable_background_pending_count += 1
        if record.get("open_context", {}).get("activation_cue_observed"):
            reactivation_cue_pending_count += 1
    for decision in pending:
        bundle = bundle_by_id.get(str(decision.get("bundle_id")), {})
        ids = {str(value) for value in (bundle.get("source_message_ids") or bundle.get("member_message_ids") or [])}
        all_pending_unique_messages.update(ids)
        all_pending_message_count += len(ids)

    if len(complete) < REQUESTED_COMPLETE_COUNT:
        audit_rows.append(
            {
                "record_type": "coverage_gap",
                "audit_ref": _opaque("complete-count-shortfall", "gap"),
                "status": "fail",
                "severity": "high",
                "error_codes": ["COMPLETE_COUNT_SHORTFALL"],
                "requested": REQUESTED_COMPLETE_COUNT,
                "observed": len(complete),
                "missing": REQUESTED_COMPLETE_COUNT - len(complete),
            }
        )

    relation_metrics = _relation_metrics(relations)
    total_bundles = len(bundles)
    open_snapshot_cues = sum(bool(row.get("activation_cues")) for row in snapshots)
    pending_count = len(pending)
    background_pending_count = sum(str(row.get("channel")) == "background" for row in pending)
    pending_context_count = sum(str(row.get("channel")) == "pending_context" for row in pending)
    model_stats = (cost.get("semantic_stats") or {}) if isinstance(cost, Mapping) else {}
    budget = (cost.get("budget") or {}) if isinstance(cost, Mapping) else {}
    request_metrics = _request_metrics(
        requests,
        decisions,
        provider_source=str(manifest.get("provider") or "openai"),
    )
    manifest_budget_limits = manifest.get("budget_limits") if isinstance(manifest, Mapping) else {}
    if not isinstance(manifest_budget_limits, Mapping):
        manifest_budget_limits = {}
    selected_bundle_limit = budget.get("max_bundle_calls")
    if selected_bundle_limit is None:
        selected_bundle_limit = manifest_budget_limits.get("max_bundle_calls")
    message_count = len(messages)
    legacy_provider_calls_used = budget.get("calls_used")
    legacy_provider_calls_per_1000 = round(
        float(legacy_provider_calls_used or 0) / max(1, message_count) * 1000,
        4,
    )
    legacy_semantic_model_calls = model_stats.get("model_calls")
    legacy_semantic_calls_per_1000 = round(
        float(legacy_semantic_model_calls or 0) / max(1, message_count) * 1000,
        4,
    )
    metrics = {
        "coverage": {
            "requested_complete": REQUESTED_COMPLETE_COUNT,
            "observed_complete": len(complete),
            "complete_coverage_rate": round(len(complete) / REQUESTED_COMPLETE_COUNT, 4) if REQUESTED_COMPLETE_COUNT else 0.0,
            "pending_total": pending_count,
            "pending_sampled": len(pending_records),
            "pending_sample_coverage_rate": round(len(pending_records) / pending_count, 4) if pending_count else 0.0,
        },
        "value_and_reactivation": {
            "valuable_pending_bundle_count_in_sample": valuable_pending_bundle_count,
            "valuable_pending_rate_in_sample": round(valuable_pending_bundle_count / len(pending_records), 4) if pending_records else 0.0,
            "valuable_background_pending_bundle_count_in_sample": valuable_background_pending_count,
            "background_pending_total": background_pending_count,
            "valuable_cold_rate_in_sample": round(valuable_background_pending_count / max(1, sum(record.get("bucket") == "gate_background" for record in pending_records)), 4),
            "pending_context_total": pending_context_count,
            "reactivation_cue_pending_count_in_sample": reactivation_cue_pending_count,
            "reactivation_cue_rate_in_sample": round(reactivation_cue_pending_count / len(pending_records), 4) if pending_records else 0.0,
            "open_snapshot_count": len(snapshots),
            "open_snapshot_activation_cue_count": open_snapshot_cues,
        },
        "bundle_compression": {
            "message_count": message_count,
            "candidate_bundle_count": total_bundles,
            "selected_bundle_count": request_metrics["selected_bundle_count"],
            "selected_bundle_limit": selected_bundle_limit,
            "selected_bundle_budget_status": (
                "pass"
                if selected_bundle_limit is not None
                and request_metrics["selected_bundle_count"] <= int(selected_bundle_limit)
                else "N/A_limit_not_recorded"
            ),
            "unique_pending_message_count": len(all_pending_unique_messages),
            "mean_messages_per_candidate_bundle": round(all_pending_message_count / max(1, pending_count), 4),
            "message_to_bundle_ratio": round(len(messages) / max(1, total_bundles), 4),
        },
        "cost_and_latency": {
            "metric_definition_note": (
                "provider_request_attempts counts only rows from the configured provider source with status started, complete, or failed; "
                "successful_model_outputs counts normalized model_wire/complete rows; candidate_decisions counts "
                "all candidate decisions; budget_deferred counts pending budget decisions. semantic_model_calls "
                "is a separate legacy pipeline-attempt counter and must not be compared as provider requests."
            ),
            "provider_request_attempts": request_metrics["provider_request_attempts"],
            "successful_model_outputs": request_metrics["successful_model_outputs"],
            "failed_provider_attempts": request_metrics["failed_provider_attempts"],
            "unfinished_provider_attempts": request_metrics["unfinished_provider_attempts"],
            "candidate_decisions": request_metrics["candidate_decisions"],
            "budget_deferred": request_metrics["budget_deferred"],
            "provider_request_attempts_per_1000_messages": round(
                float(request_metrics["provider_request_attempts"]) / max(1, message_count) * 1000,
                4,
            ),
            "successful_model_outputs_per_1000_messages": round(
                float(request_metrics["successful_model_outputs"]) / max(1, message_count) * 1000,
                4,
            ),
            "failed_provider_attempts_per_1000_messages": round(
                float(request_metrics["failed_provider_attempts"]) / max(1, message_count) * 1000,
                4,
            ),
            "candidate_decisions_per_1000_messages": round(
                float(request_metrics["candidate_decisions"]) / max(1, message_count) * 1000,
                4,
            ),
            "budget_deferred_rate": round(
                float(request_metrics["budget_deferred"]) / max(1, request_metrics["candidate_decisions"]),
                4,
            ),
            "request_status_counts": request_metrics["request_status_counts"],
            "request_source_status_counts": request_metrics["request_source_status_counts"],
            # Retain the historical values verbatim for audit/reconciliation.
            # Consumers must use the explicit counters above for new reports.
            "provider_calls_used": legacy_provider_calls_used,
            "provider_calls_per_1000_messages": legacy_provider_calls_per_1000,
            "semantic_model_calls": legacy_semantic_model_calls,
            "semantic_calls_per_1000_messages": legacy_semantic_calls_per_1000,
            "deprecated_legacy_metrics": {
                "provider_calls_used": {
                    "value": legacy_provider_calls_used,
                    "deprecated": True,
                    "meaning": "historical budget slot counter; not a universal provider request definition",
                    "replacement": "provider_request_attempts",
                },
                "provider_calls_per_1000_messages": {
                    "value": legacy_provider_calls_per_1000,
                    "deprecated": True,
                    "meaning": "historical budget slot rate using the legacy counter",
                    "replacement": "provider_request_attempts_per_1000_messages",
                },
                "semantic_model_calls": {
                    "value": legacy_semantic_model_calls,
                    "deprecated": True,
                    "meaning": "semantic pipeline attempt counter including deferred/normalized rows; not provider_request_attempts",
                    "replacement": "candidate_decisions plus explicit request counters",
                },
                "semantic_calls_per_1000_messages": {
                    "value": legacy_semantic_calls_per_1000,
                    "deprecated": True,
                    "meaning": "historical semantic pipeline-attempt rate; not a provider request rate",
                    "replacement": "provider_request_attempts_per_1000_messages",
                },
            },
            "budget_input_tokens": budget.get("input_tokens"),
            "budget_output_tokens": budget.get("output_tokens"),
            "semantic_input_tokens": model_stats.get("tokens_in"),
            "semantic_output_tokens": model_stats.get("tokens_out"),
            "provider_latency_ms_total": budget.get("latency_ms_total"),
            "provider_latency_ms_average": round(float(budget.get("latency_ms_total") or 0) / max(1, int(request_metrics["provider_request_attempts"])), 4),
            "semantic_latency_ms_total": model_stats.get("latency_ms_total"),
            "semantic_latency_ms_average": model_stats.get("latency_ms_average"),
            "cache_hits": budget.get("cache_hits"),
            "cache_misses": budget.get("cache_misses"),
            "model_successes": model_stats.get("model_successes"),
            "model_failures": model_stats.get("model_failures"),
            "fallback_calls": model_stats.get("fallback_calls"),
        },
        "evidence_unknown": {
            "complete_evidence_valid_count": evidence_valid_count,
            "complete_evidence_coverage": round(evidence_valid_count / len(complete), 4) if complete else "N/A",
            "complete_unknown_state_count": sum(record.get("field_verdicts", {}).get("state", {}).get("observed", {}).get("value") == "unknown" for record in complete_records),
            "complete_unknown_subject_count": sum(record.get("field_verdicts", {}).get("subject", {}).get("observed", {}).get("unknown") is True for record in complete_records),
            "complete_unknown_object_count": sum(record.get("field_verdicts", {}).get("object", {}).get("observed", {}).get("boundary") == "unknown_allowed" for record in complete_records),
            "unknown_start_rate": round(unknown_start_count / len(complete), 4) if complete else "N/A",
            "unknown_end_rate": round(unknown_end_count / len(complete), 4) if complete else "N/A",
        },
        "zero_violation_guards": {
            **relation_metrics,
            "cross_chat_bundle_scope_violation_count": cross_chat_bundle_violations,
            "open_boundary_violation_count": open_boundary_violations,
            "silence_terminal_violation_count": silence_terminal_violations,
            "cross_chat_bundle_status": "pass" if cross_chat_bundle_violations == 0 else "fail",
            "open_boundary_status": "pass" if open_boundary_violations == 0 else "fail",
            "silence_terminal_status": "pass" if silence_terminal_violations == 0 else "fail",
        },
        "provider_and_fallback": {
            "provider": str(manifest.get("provider") or "unknown"),
            "model": str(manifest.get("model") or "unknown"),
            "provider_configured": bool(manifest.get("provider_configured")),
            "provider_blocked": bool(manifest.get("provider_blocked")),
            "fallback_count": cost.get("fallback_count"),
            "source_counts": dict(aggregate.get("source_counts") or {}),
            "status_counts": dict(aggregate.get("status_counts") or {}),
        },
    }
    thresholds = {
        "complete_coverage_rate": {"minimum": 1.0, "observed": metrics["coverage"]["complete_coverage_rate"], "status": "pass" if metrics["coverage"]["complete_coverage_rate"] >= 1.0 else "fail"},
        "pending_sample_size": {"minimum": PENDING_SAMPLE_SIZE, "observed": len(pending_records), "status": "pass" if len(pending_records) >= PENDING_SAMPLE_SIZE else "fail"},
        "cross_chat_bundle_scope_violations": {"maximum": 0, "observed": cross_chat_bundle_violations, "status": "pass" if cross_chat_bundle_violations == 0 else "fail"},
        "time_only_relation_violations": {"maximum": 0, "observed": relation_metrics["time_only_relation_violation_count"], "status": "pass" if relation_metrics["time_only_relation_violation_count"] == 0 else "fail"},
        "silence_terminal_violations": {"maximum": 0, "observed": silence_terminal_violations, "status": "pass" if silence_terminal_violations == 0 else "fail"},
        "fallback_count": {"maximum": 0, "observed": int(cost.get("fallback_count") or 0), "status": "pass" if int(cost.get("fallback_count") or 0) == 0 else "fail"},
    }
    comparison = _compare_v1(v1_audit, v1_predictions, bundles, v1_manifest)
    summary = {
        "schema_version": "contextual_bundle_v2_8_human_audit_summary_private_v1",
        "audit_mode": "private_qualitative_body_free",
        "scope": "development",
        "artifact_version": str(manifest.get("artifact_version") or "contextual_bundle_pipeline_v2_8"),
        "manifest_binding": {
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "input_sha256": str(manifest.get("input_sha256") or ""),
            "decisions_sha256": _file_sha256(decisions_path),
            "bundles_sha256": _file_sha256(bundles_path),
            "cost_sha256": _file_sha256(cost_path),
            "input_path_role": "development-only-corresponding-message-file",
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
            "complete_audited": len(complete_records),
            "complete_requested": REQUESTED_COMPLETE_COUNT,
            "pending_total": pending_count,
            "pending_sampled": len(pending_records),
            "v1_samples": len(v1_audit),
        },
        "field_verdicts_complete": _aggregate_field_metrics(complete_records),
        "metrics": metrics,
        "thresholds": thresholds,
        "coverage_gap": {
            "status": "fail" if len(complete) < REQUESTED_COMPLETE_COUNT else "pass",
            "error_code": "COMPLETE_COUNT_SHORTFALL" if len(complete) < REQUESTED_COMPLETE_COUNT else None,
            "requested": REQUESTED_COMPLETE_COUNT,
            "observed": len(complete),
            "missing": max(0, REQUESTED_COMPLETE_COUNT - len(complete)),
        },
        "pending_buckets_sampled": _counter_dict(record.get("bucket") for record in pending_records),
        "v1_mapping": {
            "mapped_count": comparison.get("mapped_count"),
            "na_count": comparison.get("na_count"),
            "na_reason_counts": comparison.get("na_reason_counts"),
            "comparable_accuracy": "N/A",
        },
        "known_limitations": [
            "No frozen or gold labels were read; field correctness without an observable signal is uncertain.",
            "The artifact exposed fewer complete records than requested; no synthetic completion was substituted.",
            "v1 target tokens had no exact join key in the permitted v1/v2 outputs, so all non-joinable comparisons are N/A.",
            "Same-segment relation safety is N/A when the relation file has zero records; zero is not a positive accuracy claim.",
        ],
        "status": "blocked_by_coverage_gap" if len(complete) < REQUESTED_COMPLETE_COUNT else "audited",
    }
    return audit_rows, summary, comparison


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--v1-dir", type=Path, required=True)
    args = parser.parse_args()
    audit_rows, summary, comparison = run_audit(args.artifact_dir, args.input, args.v1_dir)
    # Final privacy check is performed on the exact values that will be written.
    output_values = [audit_rows, summary, comparison]
    body_paths = sorted({path for value in output_values for path in _nested_body_keys(value)})
    if body_paths:
        raise AssertionError("body-shaped fields detected in audit output")
    audit_dir = args.artifact_dir / "audit"
    _safe_path(audit_dir)
    _write_jsonl(audit_dir / "human_audit.private.jsonl", [_body_free(row) for row in audit_rows])
    _write_json(audit_dir / "audit_summary.private.json", _body_free(summary))
    _write_json(audit_dir / "v1_v28_comparison.private.json", _body_free(comparison))
    print(
        "v2.8 private audit written: complete=%d pending_sample=%d v1_mapped=%d v1_na=%d status=%s"
        % (
            int(summary["counts"]["complete_audited"]),
            int(summary["counts"]["pending_sampled"]),
            int(comparison["mapped_count"]),
            int(comparison["na_count"]),
            str(summary["status"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
