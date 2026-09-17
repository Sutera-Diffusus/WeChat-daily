"""Offline P0 semantic baseline runner.

This module is intentionally kept under ``tests``.  It adapts the private
pilot contract to the pure shadow pipeline and never imports the production
analysis/service path.  The pilot contract has relative local time, a
``redacted_text`` field, and pilot-only dialogue eligibility metadata; the
pipeline accepts a small legacy-shaped mapping instead.  The adapter below is
the only boundary between those two shapes.

The runner uses two deterministic passes:

* all pilot records are available to mention extraction;
* only text records explicitly eligible for event evidence are available to
  claim, relation, event, and presentation construction.

The split prevents context-only and greeting-only records from becoming event
evidence while still measuring mention extraction over the selected sample.
No private fixture is imported at module import time.  The command-line entry
point is intended for a local, offline evaluation invocation only.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # pytest adds ``src`` through pyproject.toml.
    from wechat_bridge.semantic_gold import (
        PRIVATE_JSONL_FILES,
        score_gold_predictions,
        semantic_result_to_evaluation_payload,
        validate_contract_directory,
    )
    from wechat_bridge.semantic_pipeline import (
        EVENT_RELATIONS,
        SemanticResultV2,
        run_semantic_pipeline,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script convenience.
    _SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))
    from wechat_bridge.semantic_gold import (
        PRIVATE_JSONL_FILES,
        score_gold_predictions,
        semantic_result_to_evaluation_payload,
        validate_contract_directory,
    )
    from wechat_bridge.semantic_pipeline import (
        EVENT_RELATIONS,
        SemanticResultV2,
        run_semantic_pipeline,
    )


ADAPTER_VERSION = "semantic_baseline_runner_p0_v1"
REPORT_VERSION = "semantic_baseline_aggregate_v1"
_BEIJING = timezone(timedelta(hours=8))
_CONTEXT_ROLES = frozenset(("context", "context_only"))
_FORBIDDEN_REPORT_KEYS = frozenset(
    {
        "content",
        "raw_text",
        "raw_message",
        "redacted_text",
        "claim_text",
        "surface_text",
        "surface_redacted",
        "title",
        "summary",
        "speaker_name",
    }
)
_COLLECTION_BY_FILE = {
    filename: filename[: -len(".private.jsonl")]
    for filename in PRIVATE_JSONL_FILES
}


@dataclass(frozen=True)
class BaselineRun:
    """In-memory result of one offline P0 evaluation."""

    predictions: Dict[str, Any]
    metrics: Dict[str, Any]
    aggregate_error_report: Dict[str, Any]
    mention_result: SemanticResultV2
    event_result: SemanticResultV2


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read JSONL strictly, without logging or returning record contents."""

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("JSONL input must be a file")
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("%s:%d is invalid JSON" % (source.name, line_number)) from exc
        if not isinstance(value, dict):
            raise ValueError("%s:%d must contain an object" % (source.name, line_number))
        records.append(value)
    return records


def load_pre_release_dataset(directory: Path) -> Dict[str, Any]:
    """Load and validate a complete private pre-release contract directory."""

    root = Path(directory).expanduser().resolve(strict=True)
    validation = validate_contract_directory(root)
    if not validation.ok:
        raise ValueError("gold contract is invalid: %s" % "; ".join(validation.errors))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset: Dict[str, Any] = {"manifest": manifest}
    for filename, collection in _COLLECTION_BY_FILE.items():
        dataset[collection] = read_jsonl(root / filename)
    return dataset


def _record_value(record: Mapping[str, Any], key: str) -> Any:
    if key not in record or record[key] is None:
        raise ValueError("pilot record is missing %s" % key)
    return record[key]


def _relative_timestamp(record: Mapping[str, Any]) -> str:
    local_day = str(_record_value(record, "local_day"))
    try:
        day = datetime.strptime(local_day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("pilot local_day must be YYYY-MM-DD") from exc
    raw_offset = _record_value(record, "time_offset_seconds")
    if isinstance(raw_offset, bool):
        raise ValueError("pilot time_offset_seconds must be numeric")
    try:
        offset = float(raw_offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("pilot time_offset_seconds must be numeric") from exc
    if not math.isfinite(offset) or offset < 0 or offset >= 24 * 60 * 60:
        raise ValueError("pilot time_offset_seconds must be within the local day")
    local_start = datetime.combine(day, datetime.min.time(), tzinfo=_BEIJING)
    return (local_start + timedelta(seconds=offset)).astimezone(timezone.utc).isoformat()


def pilot_record_to_legacy_message(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Adapt one pilot-contract record to the pure pipeline input shape.

    Only ``redacted_text`` is accepted as content.  In particular, this
    function does not fall back to ``content``/``raw_text`` and does not use
    pilot ``direction`` as ``is_self``: the latter would collapse pseudonymous
    speaker attribution to the literal ``self`` in ``legacy_messages_to_v2``.
    ``sender_name`` is intentionally generic because presentations are not
    the evaluation payload and no identity should be copied into output.
    """

    if not isinstance(record, Mapping):
        raise TypeError("pilot record must be a mapping")
    message_id = str(_record_value(record, "message_id")).strip()
    chat_id = str(_record_value(record, "chat_id")).strip()
    speaker_id = str(_record_value(record, "speaker_id")).strip()
    redacted_text = _record_value(record, "redacted_text")
    if not message_id or not chat_id or not speaker_id:
        raise ValueError("pilot message_id/chat_id/speaker_id must be non-empty")
    if not isinstance(redacted_text, str):
        raise ValueError("pilot redacted_text must be a string")
    chat_type = str(_record_value(record, "chat_type"))
    if chat_type not in {"direct", "group"}:
        raise ValueError("pilot chat_type must be direct or group")
    message_type = str(_record_value(record, "message_type"))
    reply_to = record.get("reply_to_message_id")
    if reply_to is not None:
        reply_to = str(reply_to).strip() or None
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": speaker_id,
        "sender_name": "匿名成员",
        "content": redacted_text,
        "timestamp": _relative_timestamp(record),
        "reply_to_message_id": reply_to,
        "message_type": message_type,
        "is_group": chat_type == "group",
        # Do not map outbound to is_self; see the docstring above.
        "is_self": False,
    }


def _pilot_metadata(record: Mapping[str, Any]) -> Mapping[str, Any]:
    pilot = record.get("pilot")
    if not isinstance(pilot, Mapping):
        raise ValueError("pilot record requires a pilot metadata object")
    return pilot


def is_event_evidence_eligible(record: Mapping[str, Any]) -> bool:
    """Return whether a pilot record may enter claim/event construction.

    Eligibility is deliberately deny-by-default.  Media/other placeholders,
    context-only records, and greeting-only records cannot become event
    evidence even if an upstream flag is accidentally inconsistent.  A
    greeting-prefix record remains eligible because its metadata says the
    remainder is substantive; the gold claim evidence then points to the
    substantive message span rather than treating the prefix as a standalone
    event.
    """

    if not isinstance(record, Mapping):
        return False
    pilot = record.get("pilot")
    if not isinstance(pilot, Mapping):
        return False
    if pilot.get("dialogue_evidence_eligible") is not True:
        return False
    if record.get("event_evidence_eligible") is False or record.get("evidence_eligible") is False:
        return False
    role = pilot.get("dialogue_role")
    if role is None:
        role = pilot.get("dialogue_event_role")
    if role is None:
        role = record.get("dialogue_event_role") or record.get("message_role")
    if role in _CONTEXT_ROLES:
        return False
    if pilot.get("dialogue_greeting_only") is True or pilot.get("greeting_only") is True:
        return False
    if record.get("dialogue_greeting_only") is True:
        return False
    return str(record.get("message_type") or "") == "text"


def pilot_records_to_legacy_messages(
    records: Iterable[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], ...]:
    """Adapt records in a stable order and reject duplicate message IDs."""

    adapted: List[Dict[str, Any]] = []
    seen: set = set()
    for record in records:
        message = pilot_record_to_legacy_message(record)
        message_id = message["message_id"]
        if message_id in seen:
            raise ValueError("duplicate pilot message_id: %s" % message_id)
        seen.add(message_id)
        adapted.append(message)
    return tuple(sorted(adapted, key=lambda item: item["message_id"]))


def _event_evidence_audit(
    result: SemanticResultV2,
    records: Sequence[Mapping[str, Any]],
) -> Dict[str, int]:
    eligible_ids = {
        str(record.get("message_id"))
        for record in records
        if is_event_evidence_eligible(record)
    }
    context_ids = {
        str(record.get("message_id"))
        for record in records
        if not is_event_evidence_eligible(record)
        and (
            _pilot_metadata(record).get("dialogue_role") in _CONTEXT_ROLES
            or _pilot_metadata(record).get("dialogue_event_role") in _CONTEXT_ROLES
        )
    }
    greeting_only_ids = {
        str(record.get("message_id"))
        for record in records
        if _pilot_metadata(record).get("dialogue_greeting_only") is True
    }
    event_claim_leaks = sum(
        1 for claim in result.claims if claim.message_id not in eligible_ids
    )
    event_claim_evidence_leaks = sum(
        1
        for claim in result.claims
        for evidence in claim.evidence_refs
        if evidence.message_id not in eligible_ids
    )
    event_source_leaks = sum(
        1
        for event in result.events
        for message_id in event.source_message_ids
        if message_id not in eligible_ids
    )
    event_evidence_leaks = sum(
        1
        for event in result.events
        for evidence in event.evidence_refs
        if evidence.message_id not in eligible_ids
    )
    presentation_source_leaks = sum(
        1
        for card in result.presentations
        for message_id in card.source_message_ids
        if message_id not in eligible_ids
    )
    presentation_evidence_leaks = sum(
        1
        for card in result.presentations
        for evidence in card.evidence_refs
        if evidence.message_id not in eligible_ids
    )
    return {
        "eligible_event_input_count": len(eligible_ids),
        "event_claim_leak_count": event_claim_leaks,
        "event_claim_evidence_leak_count": event_claim_evidence_leaks,
        "event_source_message_leak_count": event_source_leaks,
        "event_evidence_leak_count": event_evidence_leaks,
        "presentation_source_message_leak_count": presentation_source_leaks,
        "presentation_evidence_leak_count": presentation_evidence_leaks,
        "context_only_event_evidence_count": sum(
            1
            for claim in result.claims
            if claim.message_id in context_ids
        )
        + sum(
            1
            for event in result.events
            for evidence in event.evidence_refs
            if evidence.message_id in context_ids
        ),
        "greeting_only_event_evidence_count": sum(
            1
            for claim in result.claims
            if claim.message_id in greeting_only_ids
        )
        + sum(
            1
            for event in result.events
            for evidence in event.evidence_refs
            if evidence.message_id in greeting_only_ids
        ),
    }


def _claim_match_key(record: Mapping[str, Any]) -> Tuple[Any, ...]:
    spans = tuple(
        sorted(
            (int(item.get("start", 0)), int(item.get("end", 0)))
            for item in record.get("evidence_spans") or []
        )
    )
    return (
        str(record.get("message_id") or ""),
        spans,
        str(record.get("claim_type") or ""),
        tuple(sorted(str(value) for value in record.get("target_entity_ids") or [])),
    )


def _unique_claim_maps(
    records: Sequence[Mapping[str, Any]], id_field: str,
) -> Tuple[Dict[str, Mapping[str, Any]], Dict[str, Tuple[Any, ...]], Dict[Tuple[Any, ...], str]]:
    by_id: Dict[str, Mapping[str, Any]] = {}
    for record in records:
        record_id = str(record.get(id_field) or "")
        if not record_id or record_id in by_id:
            raise ValueError("duplicate or missing predicted/gold claim id")
        by_id[record_id] = record
    key_by_id = {record_id: _claim_match_key(record) for record_id, record in by_id.items()}
    ids_by_key: Dict[Tuple[Any, ...], List[str]] = {}
    for record_id, key in key_by_id.items():
        ids_by_key.setdefault(key, []).append(record_id)
    ambiguous = [ids for ids in ids_by_key.values() if len(ids) > 1]
    if ambiguous:
        raise ValueError("ambiguous claim match keys")
    return by_id, key_by_id, {key: ids[0] for key, ids in ids_by_key.items()}


def _relation_map(
    records: Sequence[Mapping[str, Any]],
    id_to_key: Mapping[str, Tuple[Any, ...]],
) -> Dict[Tuple[Any, Any], str]:
    output: Dict[Tuple[Any, Any], str] = {}
    for record in records:
        left = id_to_key.get(str(record.get("left_anchor_id")))
        right = id_to_key.get(str(record.get("right_anchor_id")))
        if left is not None and right is not None and left != right:
            output[tuple(sorted((left, right), key=repr))] = str(record.get("label"))
    return output


def _sanitized_metrics(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop scorer rows/alignment IDs that are not aggregate metrics."""

    output = dict(metrics)
    output["relation"] = {
        key: value
        for key, value in (metrics.get("relation") or {}).items()
        if key != "rows"
    }
    output["presentation"] = {
        key: value
        for key, value in (metrics.get("presentation") or {}).items()
        if key != "cluster_alignment"
    }
    return output


def _relation_label_metrics(
    gold_relations: Mapping[Tuple[Any, Any], str],
    predicted_relations: Mapping[Tuple[Any, Any], str],
) -> Dict[str, Dict[str, Any]]:
    """Return aggregate precision/recall/F1 for each of the five labels."""

    rows: Dict[str, Dict[str, Any]] = {}
    all_pairs = set(gold_relations) | set(predicted_relations)
    for label in sorted(EVENT_RELATIONS):
        gold_count = sum(value == label for value in gold_relations.values())
        predicted_count = sum(value == label for value in predicted_relations.values())
        true_positive = sum(
            gold_relations.get(pair) == label
            and predicted_relations.get(pair) == label
            for pair in all_pairs
        )
        precision = true_positive / predicted_count if predicted_count else 1.0
        recall = true_positive / gold_count if gold_count else 1.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        rows[label] = {
            "gold_count": gold_count,
            "predicted_count": predicted_count,
            "true_positive": true_positive,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    return rows


def _assert_no_report_body(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in _FORBIDDEN_REPORT_KEYS:
                raise ValueError("aggregate report contains forbidden body field")
            _assert_no_report_body(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_report_body(child)


def build_aggregate_error_report(
    dataset: Mapping[str, Any],
    predictions: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    evidence_audit: Mapping[str, int],
    input_counts: Optional[Mapping[str, Any]] = None,
    input_hashes: Optional[Mapping[str, Any]] = None,
    gitignore: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a body-free aggregate report from scorer output."""

    gold_claims, gold_key_by_id, gold_id_by_key = _unique_claim_maps(
        dataset["claims"], "claim_id"
    )
    pred_claims, pred_key_by_id, pred_id_by_key = _unique_claim_maps(
        predictions.get("claims") or [], "prediction_claim_id"
    )
    common_keys = set(gold_id_by_key) & set(pred_id_by_key)
    gold_mentions = {
        (
            str(item.get("message_id")),
            int(item.get("span_start")),
            int(item.get("span_end")),
            str(item.get("mention_type")),
            str(item.get("normalized_id")),
        )
        for item in dataset["mentions"]
    }
    predicted_mentions = {
        (
            str(item.get("message_id")),
            int(item.get("span_start")),
            int(item.get("span_end")),
            str(item.get("mention_type")),
            str(item.get("normalized_id")),
        )
        for item in predictions.get("mentions") or []
    }
    attribution_mismatch_count = sum(
        str(gold_claims[gold_id_by_key[key]].get("speaker_id"))
        != str(pred_claims[pred_id_by_key[key]].get("speaker_id"))
        for key in common_keys
    )
    gold_relations = _relation_map(dataset["relations"], gold_key_by_id)
    predicted_relations = _relation_map(predictions.get("relations") or [], pred_key_by_id)
    relation_confusion: Counter = Counter()
    relation_label_mismatch = 0
    relation_missing = 0
    relation_extra = 0
    for pair in sorted(set(gold_relations) | set(predicted_relations), key=repr):
        gold_label = gold_relations.get(pair)
        predicted_label = predicted_relations.get(pair)
        relation_confusion[(gold_label or "missing", predicted_label or "missing")] += 1
        if gold_label is None:
            relation_extra += 1
        elif predicted_label is None:
            relation_missing += 1
        elif gold_label != predicted_label:
            relation_label_mismatch += 1

    score = _sanitized_metrics(metrics)
    error_counts = {
        "mention_false_negative": len(gold_mentions - predicted_mentions),
        "mention_false_positive": len(predicted_mentions - gold_mentions),
        "claim_missing": len(set(gold_id_by_key) - common_keys),
        "claim_extra": len(set(pred_id_by_key) - common_keys),
        "claim_attribution_mismatch": attribution_mismatch_count,
        "candidate_pair_missing": relation_missing,
        "candidate_pair_extra": relation_extra,
        "relation_label_mismatch": relation_label_mismatch,
        "cluster_overmerge": int(score["cluster"]["overmerge_count"]),
        "cluster_oversplit": int(score["cluster"]["oversplit_count"]),
        "mnl_violation": int(score["must_not_link"]["violation_count"]),
        "presentation_evidence_miss": int(
            score["presentation"]["gold_count"]
            - score["presentation"]["exact_evidence_count"]
        ),
        "presentation_evidence_expansion": int(
            score["presentation"]["evidence_expansion_count"]
        ),
        "event_claim_evidence_leak": int(
            evidence_audit["event_claim_evidence_leak_count"]
        ),
        "event_evidence_leak": int(evidence_audit["event_evidence_leak_count"]),
        "context_only_event_evidence": int(
            evidence_audit["context_only_event_evidence_count"]
        ),
        "greeting_only_event_evidence": int(
            evidence_audit["greeting_only_event_evidence_count"]
        ),
    }
    top_errors = [
        {"category": category, "count": count}
        for category, count in sorted(
            error_counts.items(), key=lambda item: (-item[1], item[0])
        )
        if count > 0
    ]
    zero_tolerance_checks = {
        "mnl_violation_zero": error_counts["mnl_violation"] == 0,
        "event_evidence_leak_zero": all(
            int(evidence_audit[key]) == 0
            for key in (
                "event_claim_leak_count",
                "event_claim_evidence_leak_count",
                "event_source_message_leak_count",
                "event_evidence_leak_count",
                "presentation_source_message_leak_count",
                "presentation_evidence_leak_count",
            )
        ),
        "context_only_never_event_evidence": error_counts[
            "context_only_event_evidence"
        ]
        == 0,
        "greeting_only_never_event_evidence": error_counts[
            "greeting_only_event_evidence"
        ]
        == 0,
        "presentation_evidence_expansion_zero": error_counts[
            "presentation_evidence_expansion"
        ]
        == 0,
    }
    manifest = dataset.get("manifest") or {}
    report: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "status": "provisional",
        "provisional": True,
        "provisional_reason": "pre_release remains pending human privacy review",
        "adapter_version": ADAPTER_VERSION,
        "pipeline": {
            "mode": "offline_shadow_only",
            "production_connected": False,
            "event_evidence_policy": "text_and_explicitly_eligible_substantive_only",
        },
        "gold": {
            "status": manifest.get("status"),
            "workflow_state": manifest.get("workflow_state"),
            "privacy_scan_status": manifest.get("privacy_scan_status"),
            "release_eligible": manifest.get("release_eligible"),
            "coverage_shortfall_count": len(manifest.get("coverage_shortfalls") or []),
        },
        "input_counts": dict(input_counts or {}),
        "input_sha256": dict(input_hashes or {}),
        "metrics": score,
        "relation_confusion_counts": {
            "%s->%s" % (gold_label, predicted_label): count
            for (gold_label, predicted_label), count in sorted(
                relation_confusion.items(), key=lambda item: repr(item[0])
            )
        },
        "relation_by_label": _relation_label_metrics(
            gold_relations, predicted_relations
        ),
        "error_category_counts": error_counts,
        "highest_frequency_error_categories": top_errors,
        "evidence_audit": dict(evidence_audit),
        "zero_tolerance": {
            "checks": zero_tolerance_checks,
            "passed": all(zero_tolerance_checks.values()),
        },
        "gitignore": dict(gitignore or {}),
    }
    _assert_no_report_body(report)
    return report


def run_p0_baseline(
    pilot_records: Sequence[Mapping[str, Any]],
    gold_dataset: Mapping[str, Any],
    *,
    input_counts: Optional[Mapping[str, Any]] = None,
    input_hashes: Optional[Mapping[str, Any]] = None,
    gitignore: Optional[Mapping[str, Any]] = None,
) -> BaselineRun:
    """Run mention and event-safe P0 passes, then score against private gold."""

    records = list(pilot_records)
    all_messages = pilot_records_to_legacy_messages(records)
    event_records = [record for record in records if is_event_evidence_eligible(record)]
    event_messages = pilot_records_to_legacy_messages(event_records)
    mention_result = run_semantic_pipeline(all_messages)
    event_result = run_semantic_pipeline(event_messages)
    predictions = semantic_result_to_evaluation_payload(event_result)
    predictions["mentions"] = semantic_result_to_evaluation_payload(mention_result)["mentions"]
    metrics = score_gold_predictions(gold_dataset, predictions)
    evidence_audit = _event_evidence_audit(event_result, records)
    counts = {
        "pilot_record_count": len(records),
        "mention_pass_input_count": len(all_messages),
        "event_pass_input_count": len(event_messages),
        **dict(input_counts or {}),
    }
    report = build_aggregate_error_report(
        gold_dataset,
        predictions,
        metrics,
        evidence_audit=evidence_audit,
        input_counts=counts,
        input_hashes=input_hashes,
        gitignore=gitignore,
    )
    return BaselineRun(predictions, metrics, report, mention_result, event_result)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gitignore_status(paths: Sequence[Path], repo_root: Optional[Path]) -> Dict[str, Any]:
    if repo_root is None:
        return {"checked": False, "all_ignored": False, "checked_file_count": 0}
    root = Path(repo_root).expanduser().resolve(strict=True)
    statuses: List[bool] = []
    for path in paths:
        absolute = Path(path).expanduser().resolve()
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            statuses.append(False)
            continue
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", str(relative)],
            cwd=str(root),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        statuses.append(result.returncode == 0)
    return {
        "checked": True,
        "all_ignored": bool(statuses) and all(statuses),
        "checked_file_count": len(statuses),
    }


def write_private_outputs(
    run: BaselineRun,
    output_directory: Path,
    *,
    repo_root: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """Write only private, body-free prediction and aggregate report files."""

    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    prediction_path = destination / "p0_baseline_predictions.private.json"
    report_path = destination / "p0_baseline_error_report.aggregate.private.json"
    # The prediction payload intentionally contains IDs/spans/types only.
    prediction_path.write_text(
        json.dumps(run.predictions, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    report = dict(run.aggregate_error_report)
    report["output_sha256"] = {"predictions": sha256_file(prediction_path)}
    report["gitignore"] = _gitignore_status(
        (prediction_path, report_path), repo_root
    )
    _assert_no_report_body(report)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    # Keep the in-memory result aligned with the persisted aggregate report so
    # CLI callers and library callers observe the same hashes/ignore status.
    run.aggregate_error_report.clear()
    run.aggregate_error_report.update(report)
    return prediction_path, report_path


def run_from_files(
    pilot_path: Path,
    gold_directory: Path,
    *,
    output_directory: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> BaselineRun:
    """Run the private-file evaluation; callers choose whether to persist."""

    pilot_source = Path(pilot_path).expanduser().resolve(strict=True)
    gold_source = Path(gold_directory).expanduser().resolve(strict=True)
    records = read_jsonl(pilot_source)
    dataset = load_pre_release_dataset(gold_source)
    input_hashes = {
        "pilot_candidates": sha256_file(pilot_source),
        "gold_manifest": sha256_file(gold_source / "manifest.json"),
        "gold_files": {
            filename: sha256_file(gold_source / filename)
            for filename in PRIVATE_JSONL_FILES
        },
    }
    run = run_p0_baseline(
        records,
        dataset,
        input_counts={
            "gold_message_count": len(dataset["messages"]),
            "gold_mention_count": len(dataset["mentions"]),
            "gold_claim_count": len(dataset["claims"]),
            "gold_relation_count": len(dataset["relations"]),
            "gold_cluster_count": len(dataset["clusters"]),
            "gold_presentation_count": len(dataset["presentations"]),
        },
        input_hashes=input_hashes,
    )
    if output_directory is not None:
        write_private_outputs(run, output_directory, repo_root=repo_root)
    return run


def _default_paths() -> Tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[1]
    working = root / "data" / "private" / "gold_standard" / "2026-08-25" / "working"
    return (
        working / "pilot_candidates.v2.private.jsonl",
        working / "adjudication" / "pre_release",
        working / "baseline_p0",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    default_pilot, default_gold, default_output = _default_paths()
    parser = argparse.ArgumentParser(description="Run the offline P0 semantic baseline")
    parser.add_argument("--pilot", type=Path, default=default_pilot)
    parser.add_argument("--gold-dir", type=Path, default=default_gold)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    run = run_from_files(
        args.pilot,
        args.gold_dir,
        output_directory=args.output_dir,
        repo_root=args.repo_root,
    )
    # CLI output is the sanitized aggregate report only; it never prints
    # records, text, or identity-bearing IDs.
    print(json.dumps(run.aggregate_error_report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the local CLI.
    raise SystemExit(main())


__all__ = [
    "ADAPTER_VERSION",
    "REPORT_VERSION",
    "BaselineRun",
    "read_jsonl",
    "load_pre_release_dataset",
    "pilot_record_to_legacy_message",
    "is_event_evidence_eligible",
    "pilot_records_to_legacy_messages",
    "build_aggregate_error_report",
    "run_p0_baseline",
    "sha256_file",
    "write_private_outputs",
    "run_from_files",
    "main",
]
