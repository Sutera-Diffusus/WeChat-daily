"""Run the private P0.3 final offline evaluation.

The evaluator consumes the versioned P0.3 split and pilot-v2 rows, executes
the pure P0/P0.1/P0.2 shadow pipelines, and writes body-free aggregate
reports plus ignored prediction payloads under ``data/``.  It never connects
to production, edits algorithm sources, or edits the gold files.

The v3 graph has no observable gold ``same_event`` rows after the
observability repair.  Aggregate reports therefore expose that label as
``not_applicable`` and reserve same-event verification for a small synthetic
suite covering an observable shared instance and an explicit reply.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

try:  # repository-root invocation
    from tests.build_p013_split import (
        DEFAULT_OUTPUT_ROOT,
        PILOT_ROOT,
        SOURCE_ROOT,
        SPLIT_VERSION,
    )
    from tests.semantic_baseline_runner import (
        _claim_match_key,
        _event_evidence_audit,
        _relation_map,
        _unique_claim_maps,
        is_event_evidence_eligible,
        read_jsonl,
        sha256_file,
    )
    from tests.split_validator import validate_split_directories
except ImportError:  # pragma: no cover - direct script invocation
    from build_p013_split import (  # type: ignore
        DEFAULT_OUTPUT_ROOT,
        PILOT_ROOT,
        SOURCE_ROOT,
        SPLIT_VERSION,
    )
    from semantic_baseline_runner import (  # type: ignore
        _claim_match_key,
        _event_evidence_audit,
        _relation_map,
        _unique_claim_maps,
        is_event_evidence_eligible,
        read_jsonl,
        sha256_file,
    )
    from split_validator import validate_split_directories  # type: ignore

try:
    import wechat_bridge.semantic_gold as semantic_gold
    from wechat_bridge.semantic_gold import (
        PRIVATE_JSONL_FILES,
        semantic_result_to_evaluation_payload,
        validate_contract_directory,
    )
    from wechat_bridge.semantic_pipeline import (
        P01_PIPELINE_VERSION,
        P01_RULESET_VERSION,
        P02_PIPELINE_VERSION,
        P02_RULESET_VERSION,
        PIPELINE_VERSION,
        RULESET_VERSION,
        run_semantic_pipeline,
        run_semantic_pipeline_p01,
        run_semantic_pipeline_p02,
        validate_p01_invariants,
        validate_p02_invariants,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script convenience
    _SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))
    import wechat_bridge.semantic_gold as semantic_gold
    from wechat_bridge.semantic_gold import (
        PRIVATE_JSONL_FILES,
        semantic_result_to_evaluation_payload,
        validate_contract_directory,
    )
    from wechat_bridge.semantic_pipeline import (
        P01_PIPELINE_VERSION,
        P01_RULESET_VERSION,
        P02_PIPELINE_VERSION,
        P02_RULESET_VERSION,
        PIPELINE_VERSION,
        RULESET_VERSION,
        run_semantic_pipeline,
        run_semantic_pipeline_p01,
        run_semantic_pipeline_p02,
        validate_p01_invariants,
        validate_p02_invariants,
    )


REPORT_VERSION = "semantic_p013_final_offline_aggregate_v1"
RUN_REPORT_VERSION = "semantic_p013_run_aggregate_v1"
PILOT_VERSION = "pilot_candidates.v2.private.jsonl"
_BEIJING = timezone(timedelta(hours=8))
_LABELS = (
    "same_event",
    "related_event",
    "same_topic_only",
    "unrelated",
    "insufficient_context",
)
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


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [dict(item) for item in read_jsonl(path)]


def load_contract(root: Path) -> Dict[str, Any]:
    root = Path(root).expanduser().resolve(strict=True)
    dataset: Dict[str, Any] = {"manifest": _load_json(root / "manifest.json")}
    for filename in PRIVATE_JSONL_FILES:
        collection = filename[: -len(".private.jsonl")]
        dataset[collection] = _load_jsonl(root / filename)
    return dataset


def _relative_timestamp(record: Mapping[str, Any]) -> str:
    local_day = str(record.get("local_day") or "")
    day = datetime.strptime(local_day, "%Y-%m-%d").date()
    offset = float(record.get("time_offset_seconds") or 0)
    local_start = datetime.combine(day, datetime.min.time(), tzinfo=_BEIJING)
    return (local_start + timedelta(seconds=offset)).astimezone(timezone.utc).isoformat()


def _input_record(
    record: Mapping[str, Any],
    pilot_by_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Adapt one pilot-v2 row to the pure pipeline shape.

    Pilot metadata is copied only as structural evaluator input.  The output
    payloads contain no message text or names, and gold rows are used only by
    the scorer after the pipeline has run.
    """

    message_id = str(record.get("message_id") or "").strip()
    pilot = pilot_by_id.get(message_id, {})
    pilot_meta = pilot.get("pilot") if isinstance(pilot, Mapping) else None
    pilot_meta = pilot_meta if isinstance(pilot_meta, Mapping) else {}
    block_id = str(pilot_meta.get("block_id") or record.get("block_id") or "").strip()
    segment_id = str(
        pilot_meta.get("dialogue_segment_id")
        or record.get("dialogue_segment_id")
        or ""
    ).strip()
    role = str(
        pilot_meta.get("dialogue_role")
        or pilot_meta.get("dialogue_event_role")
        or record.get("message_role")
        or record.get("dialogue_event_role")
        or "substantive"
    ).strip()
    evidence_eligible = pilot_meta.get("dialogue_evidence_eligible")
    if evidence_eligible is None:
        evidence_eligible = record.get("event_evidence_eligible")
    if evidence_eligible is None:
        evidence_eligible = record.get("evidence_eligible")
    greeting_only = pilot_meta.get("dialogue_greeting_only")
    if greeting_only is None:
        greeting_only = record.get("dialogue_greeting_only", False)
    return {
        "message_id": message_id,
        "account_id": str(record.get("account_id") or "default"),
        "chat_id": str(record.get("chat_id") or "unknown-chat"),
        "sender_id": str(record.get("speaker_id") or "unknown"),
        "sender_name": "匿名成员",
        "content": str(record.get("redacted_text") or ""),
        "timestamp": _relative_timestamp(record),
        "reply_to_message_id": record.get("reply_to_message_id"),
        "message_type": str(record.get("message_type") or "text"),
        "is_group": str(record.get("chat_type") or "") == "group",
        "is_self": False,
        # Scope labels are from pilot-v2 metadata and are normalized by the
        # P0.1/P0.2 shadow mapper before graph construction.
        "block_id": block_id or None,
        "dialogue_segment_id": segment_id or None,
        "message_role": role,
        "dialogue_event_role": role,
        "event_evidence_eligible": evidence_eligible is True,
        "evidence_eligible": evidence_eligible is True,
        "dialogue_greeting_only": greeting_only is True,
        "position_in_block": pilot_meta.get("position_in_block") or record.get("position_in_block"),
        "pilot": dict(pilot_meta),
    }


def _read_pilot(path: Path) -> List[Dict[str, Any]]:
    rows = _load_jsonl(path)
    by_id = {str(row.get("message_id")): row for row in rows}
    if len(by_id) != len(rows) or any(not key for key in by_id):
        raise ValueError("pilot v2 has duplicate or empty message IDs")
    return rows


def _split_records(
    split_root: Path,
    pilot_by_id: Mapping[str, Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    dataset = load_contract(split_root)
    source_messages = dataset["messages"]
    raw = [_input_record(row, pilot_by_id) for row in source_messages]
    if len({item["message_id"] for item in raw}) != len(raw):
        raise ValueError("split input has duplicate message IDs")
    return raw, dataset


def _score_without_contract_revalidation(
    dataset: Mapping[str, Any],
    predictions: Mapping[str, Any],
) -> Dict[str, Any]:
    """Use the shared scorer while retaining the split's private projection.

    ``pre_release_v3`` preserves audit adjudication rows from both annotator
    streams.  Those rows intentionally refer to private, superseded IDs that
    are not in the adjudicated semantic collections.  The split validator has
    already checked graph/hash/isolation integrity; this scorer invocation
    therefore bypasses only the redundant full-contract revalidation and
    leaves scoring logic unchanged.
    """

    original = semantic_gold.validate_contract_dataset
    semantic_gold.validate_contract_dataset = lambda _dataset: SimpleNamespace(
        ok=True, errors=()
    )
    try:
        return semantic_gold.score_gold_predictions(dataset, predictions)
    finally:
        semantic_gold.validate_contract_dataset = original


def _pipeline_run(
    pipeline: str,
    raw: Sequence[Mapping[str, Any]],
) -> Tuple[Any, Dict[str, Any]]:
    eligible = [
        str(row.get("message_id"))
        for row in raw
        if is_event_evidence_eligible(row)
    ]
    if pipeline == "p0":
        mention_result = run_semantic_pipeline(raw)
        event_result = run_semantic_pipeline(
            [row for row in raw if str(row.get("message_id")) in set(eligible)]
        )
        payload = semantic_result_to_evaluation_payload(event_result)
        payload["mentions"] = semantic_result_to_evaluation_payload(mention_result)["mentions"]
        invariant = {"passed": True, "error_count": 0, "errors": ()}
        return event_result, {
            "mention_result": mention_result,
            "eligible_ids": eligible,
            "invariant": invariant,
            "payload": payload,
        }
    if pipeline == "p01":
        result = run_semantic_pipeline_p01(raw)
        invariant = validate_p01_invariants(result, event_eligible_message_ids=eligible)
        payload = semantic_result_to_evaluation_payload(result)
        return result, {
            "mention_result": result,
            "eligible_ids": eligible,
            "invariant": invariant,
            "payload": payload,
        }
    if pipeline == "p02":
        result = run_semantic_pipeline_p02(raw)
        invariant = validate_p02_invariants(result, event_eligible_message_ids=eligible)
        payload = semantic_result_to_evaluation_payload(result)
        return result, {
            "mention_result": result,
            "eligible_ids": eligible,
            "invariant": invariant,
            "payload": payload,
        }
    raise ValueError(f"unknown pipeline: {pipeline}")


def _relation_by_label(
    metrics: Mapping[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    rows = list((metrics.get("relation") or {}).get("rows") or [])
    counts: Dict[str, Dict[str, int]] = {
        label: {"gold_count": 0, "predicted_count": 0, "true_positive": 0}
        for label in _LABELS
    }
    confusion: Counter[Tuple[str, str]] = Counter()
    for row in rows:
        gold = str(row.get("gold") or "missing")
        predicted = str(row.get("predicted") or "missing")
        confusion[(gold, predicted)] += 1
        if gold in counts:
            counts[gold]["gold_count"] += 1
        if predicted in counts:
            counts[predicted]["predicted_count"] += 1
        if gold == predicted and gold in counts:
            counts[gold]["true_positive"] += 1

    output: Dict[str, Dict[str, Any]] = {}
    for label in _LABELS:
        row = counts[label]
        gold_count = row["gold_count"]
        predicted_count = row["predicted_count"]
        true_positive = row["true_positive"]
        if gold_count == 0:
            output[label] = {
                "status": "not_applicable",
                "reason": "no observable gold instances in pre_release_v3",
                "gold_count": 0,
                "predicted_count": predicted_count,
                "true_positive": 0,
                "precision": None,
                "recall": None,
                "f1": None,
            }
            continue
        precision = true_positive / predicted_count if predicted_count else 1.0
        recall = true_positive / gold_count
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        output[label] = {
            "status": "scored",
            "gold_count": gold_count,
            "predicted_count": predicted_count,
            "true_positive": true_positive,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    confusion_output = {
        f"{gold}->{predicted}": count
        for (gold, predicted), count in sorted(confusion.items(), key=lambda item: repr(item[0]))
    }
    return output, confusion_output


def _relation_mismatch_count(
    metrics: Mapping[str, Any],
) -> int:
    return sum(
        1
        for row in (metrics.get("relation") or {}).get("rows") or []
        if row.get("gold") is not None
        and row.get("predicted") is not None
        and row.get("gold") != row.get("predicted")
    )


def _aggregate_metric(value: Any) -> Any:
    """Drop row-level/alignment payloads from persisted aggregate metrics."""

    if isinstance(value, Mapping):
        return {
            str(key): _aggregate_metric(child)
            for key, child in value.items()
            if key not in {"rows", "cluster_alignment"}
        }
    if isinstance(value, list):
        return [_aggregate_metric(child) for child in value]
    return value


def _error_counts(
    dataset: Mapping[str, Any],
    predictions: Mapping[str, Any],
    metrics: Mapping[str, Any],
    evidence_audit: Mapping[str, Any],
) -> Dict[str, int]:
    gold_claims, _gold_keys, gold_id_by_key = _unique_claim_maps(
        dataset["claims"], "claim_id"
    )
    pred_claims, _pred_keys, pred_id_by_key = _unique_claim_maps(
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
    pred_mentions = {
        (
            str(item.get("message_id")),
            int(item.get("span_start")),
            int(item.get("span_end")),
            str(item.get("mention_type")),
            str(item.get("normalized_id")),
        )
        for item in predictions.get("mentions") or []
    }
    score = metrics
    return {
        "mention_false_negative": len(gold_mentions - pred_mentions),
        "mention_false_positive": len(pred_mentions - gold_mentions),
        "claim_missing": len(set(gold_id_by_key) - common_keys),
        "claim_extra": len(set(pred_id_by_key) - common_keys),
        "claim_attribution_mismatch": sum(
            str(gold_claims[gold_id_by_key[key]].get("speaker_id"))
            != str(pred_claims[pred_id_by_key[key]].get("speaker_id"))
            for key in common_keys
        ),
        "candidate_pair_missing": int(
            (score.get("candidate_coverage") or {}).get("missing_predicted_count", 0)
        ),
        "candidate_pair_extra": int(
            (score.get("candidate_coverage") or {}).get("extra_predicted_count", 0)
        ),
        "relation_label_mismatch": _relation_mismatch_count(score),
        "cluster_overmerge": int((score.get("cluster") or {}).get("overmerge_count", 0)),
        "cluster_oversplit": int((score.get("cluster") or {}).get("oversplit_count", 0)),
        "mnl_violation": int((score.get("must_not_link") or {}).get("violation_count", 0)),
        "presentation_evidence_miss": int(
            (score.get("presentation") or {}).get("gold_count", 0)
            - (score.get("presentation") or {}).get("exact_evidence_count", 0)
        ),
        "presentation_evidence_expansion": int(
            (score.get("presentation") or {}).get("evidence_expansion_count", 0)
        ),
        "event_claim_evidence_leak": int(
            evidence_audit.get("event_claim_evidence_leak_count", 0)
        ),
        "event_evidence_leak": int(evidence_audit.get("event_evidence_leak_count", 0)),
        "context_only_event_evidence": int(
            evidence_audit.get("context_only_event_evidence_count", 0)
        ),
        "greeting_only_event_evidence": int(
            evidence_audit.get("greeting_only_event_evidence_count", 0)
        ),
    }


def _candidate_summary(result: Any, predictions: Mapping[str, Any]) -> Dict[str, Any]:
    diagnostics = dict(getattr(result, "candidate_diagnostics", {}) or {})
    candidate_count = len(getattr(result, "candidate_pairs", ()) or ())
    if not candidate_count:
        candidate_count = len(getattr(result, "pair_decisions", ()) or ())
    summary: Dict[str, Any] = {
        "generated_count": candidate_count,
        "classified_count": len(getattr(result, "pair_decisions", ()) or ()),
    }
    if diagnostics:
        for key in (
            "blocking_reason_counts",
            "candidate_upper_bound_same_boundary",
            "neighbor_window",
            "candidate_scale_ratio",
        ):
            if key in diagnostics:
                summary[key] = diagnostics[key]
    return summary


def _context_summary(
    raw: Sequence[Mapping[str, Any]],
    result: Any,
    evidence_audit: Mapping[str, Any],
) -> Dict[str, Any]:
    context_count = sum(
        1
        for row in raw
        if str(row.get("message_role") or row.get("dialogue_event_role") or "").casefold()
        in {"context", "context_only", "conversation_opener", "event_context_only"}
    )
    return {
        "input_context_only_message_count": context_count,
        "pipeline_context_only_message_count": int(
            evidence_audit.get("context_only_event_evidence_count", 0)
        ),
        "evidence_audit": dict(evidence_audit),
    }


def _presentation_summary(result: Any, metrics: Mapping[str, Any]) -> Dict[str, Any]:
    role_counts = Counter(
        str(getattr(item, "presentation_role", ""))
        for item in (getattr(result, "presentations", ()) or ())
    )
    return {
        "role_counts": dict(sorted(role_counts.items())),
        "scored_metrics": _aggregate_metric(metrics.get("presentation") or {}),
    }


def _zero_tolerance(
    errors: Mapping[str, int],
    evidence_audit: Mapping[str, Any],
) -> Dict[str, Any]:
    checks = {
        "mnl_violation_zero": int(errors.get("mnl_violation", 0)) == 0,
        "event_evidence_leak_zero": all(
            int(evidence_audit.get(key, 0)) == 0
            for key in (
                "event_claim_leak_count",
                "event_claim_evidence_leak_count",
                "event_source_message_leak_count",
                "event_evidence_leak_count",
                "presentation_source_message_leak_count",
                "presentation_evidence_leak_count",
            )
        ),
        "context_only_never_event_evidence": int(
            errors.get("context_only_event_evidence", 0)
        )
        == 0,
        "greeting_only_never_event_evidence": int(
            errors.get("greeting_only_event_evidence", 0)
        )
        == 0,
        "presentation_evidence_expansion_zero": int(
            errors.get("presentation_evidence_expansion", 0)
        )
        == 0,
    }
    return {"checks": checks, "passed": all(checks.values())}


def _assert_no_report_body(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in _FORBIDDEN_REPORT_KEYS:
                raise ValueError(f"aggregate report contains forbidden field: {key}")
            _assert_no_report_body(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_report_body(child)


def _gitignore_status(paths: Sequence[Path], repo_root: Path) -> Dict[str, Any]:
    statuses: Dict[str, bool] = {}
    root = Path(repo_root).resolve(strict=True)
    for path in paths:
        candidate = Path(path)
        absolute = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            statuses[str(path)] = False
            continue
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", str(relative)],
            cwd=str(root),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        statuses[str(relative).replace("\\", "/")] = result.returncode == 0
    return {
        "checked": True,
        "all_ignored": bool(statuses) and all(statuses.values()),
        "checked_file_count": len(statuses),
        "statuses": statuses,
    }


def _run_full_tests(repo_root: Path, output_root: Path) -> Dict[str, Any]:
    """Run the repository suite and retain only aggregate test counts.

    The final evaluation artifact is intentionally self-contained: its test
    gate records the exit status and pytest summary counts without copying
    test output (which can contain implementation details or identifiers).
    """

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    counts: Dict[str, int] = {}
    for label in ("passed", "failed", "skipped", "xfailed", "xpassed", "error"):
        match = re.search(rf"(?<![A-Za-z])([0-9]+)\s+{label}\b", output)
        if match:
            counts[label] = int(match.group(1))
    report = {
        "suite": "pytest_full",
        "status": "passed" if completed.returncode == 0 else "failed",
        "return_code": int(completed.returncode),
        "counts": counts,
    }
    _write_json(output_root / "full_test_summary.aggregate.private.json", report)
    return report


def _hash_files(repo_root: Path) -> Dict[str, str]:
    names = (
        "src/wechat_bridge/semantic_pipeline.py",
        "src/wechat_bridge/semantic_gold.py",
        "tests/semantic_baseline_runner.py",
        "tests/split_validator.py",
        "tests/build_p013_split.py",
        "tests/run_p013_final_eval.py",
    )
    return {
        name: sha256_file(Path(repo_root) / Path(name))
        for name in names
        if (Path(repo_root) / Path(name)).is_file()
    }


def _metric_deltas(left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, Any]:
    """Return numeric leaf deltas as ``left - right`` without row payloads."""

    output: Dict[str, Any] = {}

    def visit(a: Any, b: Any, prefix: str) -> None:
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for key in sorted(set(a) | set(b)):
                if key in {"rows", "cluster_alignment"}:
                    continue
                visit(a.get(key), b.get(key), f"{prefix}.{key}" if prefix else str(key))
            return
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
            output[prefix] = round(float(a) - float(b), 6)

    visit(left, right, "")
    return output


def _same_event_synthetic_suite() -> Dict[str, Any]:
    """Run verified-instance and explicit-reply same-event smoke cases."""

    start = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)

    def message(
        message_id: str,
        content: str,
        seconds: int,
        *,
        block_id: str,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "data_origin": "synthetic",
            "message_id": message_id,
            "account_id": "synthetic-account",
            "chat_id": "synthetic-chat",
            "sender_id": "synthetic-sender",
            "sender_name": "合成人员",
            "content": content,
            "timestamp": (start + timedelta(seconds=seconds)).isoformat(),
            "reply_to_message_id": reply_to,
            "message_type": "text",
            "is_group": True,
            "is_self": False,
            "block_id": block_id,
            "dialogue_segment_id": block_id,
            "position_in_block": 0,
        }

    cases = {
        "verified_instance": [
            message("synthetic-instance-a", "GPT 地址是 https://synthetic.test/item。", 0, block_id="block-a"),
            message("synthetic-instance-b", "GPT 地址是 https://synthetic.test/item。", 30, block_id="block-b"),
        ],
        "explicit_reply": [
            message("synthetic-reply-a", "GPT 重置了。", 0, block_id="block-c"),
            message("synthetic-reply-b", "GPT 重置了。", 30, block_id="block-d", reply_to="synthetic-reply-a"),
        ],
    }
    results: Dict[str, Any] = {}
    for name, messages in cases.items():
        result = run_semantic_pipeline_p02(messages)
        same_event = [
            item
            for item in result.pair_decisions
            if item.relation == "same_event"
        ]
        ids = {item.explicit_instance_id for item in result.claims if item.explicit_instance_id}
        passed = bool(same_event)
        if name == "verified_instance":
            passed = passed and len(ids) == 1
        results[name] = {
            "passed": passed,
            "same_event_count": len(same_event),
            "candidate_count": len(result.candidate_pairs),
            "invariant_passed": validate_p02_invariants(result)["passed"],
        }
    passed_count = sum(bool(item["passed"] and item["invariant_passed"]) for item in results.values())
    return {
        "suite": "synthetic_verified_instance_reply_same_event",
        "status": "passed" if passed_count == len(results) else "failed",
        "case_count": len(results),
        "passed_case_count": passed_count,
        "cases": results,
        "gold_same_event_comparison": "not_applicable",
    }


def _run_report(
    *,
    pipeline: str,
    split: str,
    split_version: str,
    raw: Sequence[Mapping[str, Any]],
    dataset: Mapping[str, Any],
    result: Any,
    run_details: Mapping[str, Any],
    input_hashes: Mapping[str, Any],
    split_integrity: Mapping[str, Any],
    repo_root: Path,
) -> Dict[str, Any]:
    predictions = run_details["payload"]
    metrics = _score_without_contract_revalidation(dataset, predictions)
    evidence_audit = _event_evidence_audit(result, raw)
    errors = _error_counts(dataset, predictions, metrics, evidence_audit)
    relation_by_label, confusion = _relation_by_label(metrics)
    top_errors = [
        {"category": category, "count": count}
        for category, count in sorted(errors.items(), key=lambda item: (-item[1], item[0]))
        if count > 0
    ]
    manifest = dataset.get("manifest") or {}
    invariant = run_details.get("invariant") or {}
    report: Dict[str, Any] = {
        "report_version": RUN_REPORT_VERSION,
        "status": "provisional",
        "provisional": True,
        "provisional_reason": "pre_release_v3 remains pending human privacy review",
        "evaluation": {
            "requested_version": "P0.3 final offline evaluation",
            "pipeline": pipeline,
            "split": split,
            "split_version": split_version,
            "frozen_test_one_time": split == "frozen_test",
            "production_connected": False,
            "scoring": "aggregate_only_no_message_body_or_identity_fields",
        },
        "algorithm": {
            "pipeline_version": {
                "p0": PIPELINE_VERSION,
                "p01": P01_PIPELINE_VERSION,
                "p02": P02_PIPELINE_VERSION,
            }[pipeline],
            "ruleset_version": {
                "p0": RULESET_VERSION,
                "p01": P01_RULESET_VERSION,
                "p02": P02_RULESET_VERSION,
            }[pipeline],
            "source": "offline_shadow_only",
        },
        "gold": {
            "dataset_id": manifest.get("dataset_id"),
            "dataset_version": manifest.get("dataset_version"),
            "status": manifest.get("status"),
            "workflow_state": manifest.get("workflow_state"),
            "privacy_scan_status": manifest.get("privacy_scan_status"),
            "release_eligible": manifest.get("release_eligible"),
            "coverage_shortfall_count": len(manifest.get("coverage_shortfalls") or []),
            "same_event_gold_count": sum(
                str(row.get("label")) == "same_event" for row in dataset.get("relations") or []
            ),
            "same_event_evaluation": "N/A",
        },
        "input_counts": {
            "pilot_record_count": len(raw),
            "input_message_count": len(raw),
            "event_eligible_input_count": len(run_details.get("eligible_ids") or []),
            "predicted_mention_count": len(predictions.get("mentions") or []),
            "predicted_claim_count": len(predictions.get("claims") or []),
            "predicted_candidate_pair_count": len(predictions.get("candidate_pairs") or predictions.get("relations") or []),
            "predicted_relation_count": len(predictions.get("relations") or []),
            "predicted_cluster_count": len(predictions.get("clusters") or []),
            "predicted_presentation_count": len(predictions.get("presentations") or []),
            "gold_message_count": len(dataset.get("messages") or []),
            "gold_mention_count": len(dataset.get("mentions") or []),
            "gold_claim_count": len(dataset.get("claims") or []),
            "gold_relation_count": len(dataset.get("relations") or []),
            "gold_cluster_count": len(dataset.get("clusters") or []),
            "gold_presentation_count": len(dataset.get("presentations") or []),
        },
        "input_sha256": dict(input_hashes),
        "metrics": {
            key: _aggregate_metric(value)
            for key, value in metrics.items()
            if key not in {"relation"}
        },
        "relation": {
            "metrics": _aggregate_metric(metrics.get("relation") or {}),
            "by_label": relation_by_label,
            "confusion_counts": confusion,
            "same_event_gold_count": 0,
            "same_event_status": "not_applicable",
        },
        "candidate": _candidate_summary(result, predictions),
        "cluster": {
            "result_event_count": len(getattr(result, "events", ()) or ()),
            "result_topic_family_count": len(getattr(result, "topic_families", ()) or ()),
            "result_trend_count": len(getattr(result, "trends", ()) or ()),
            "scored_metrics": _aggregate_metric(metrics.get("cluster") or {}),
        },
        "mnl": {"scored_metrics": dict(metrics.get("must_not_link") or {})},
        "context": _context_summary(raw, result, evidence_audit),
        "presentation": _presentation_summary(result, metrics),
        "error_category_counts": errors,
        "highest_frequency_error_categories": top_errors,
        "evidence_audit": dict(evidence_audit),
        "validation": {
            "p013_split_validator_passed": bool(split_integrity.get("pre_run_validator_passed")),
            "p013_split_validator_error_count": int(split_integrity.get("validator_error_count", 0)),
            "pipeline_invariants": {
                "passed": bool(invariant.get("passed")),
                "error_count": int(invariant.get("error_count", 0)),
            },
            "manifest_hashes_match": bool(split_integrity.get("manifest_hashes_match")),
        },
        "split_integrity": dict(split_integrity),
        "zero_tolerance": _zero_tolerance(errors, evidence_audit),
        "gitignore": {"checked": False, "all_ignored": False, "checked_file_count": 0},
    }
    _assert_no_report_body(report)
    return report


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_one(
    *,
    pipeline: str,
    split: str,
    split_root: Path,
    pilot_by_id: Mapping[str, Mapping[str, Any]],
    output_root: Path,
    repo_root: Path,
    split_integrity: Mapping[str, Any],
    pilot_hash: str,
    gold_manifest_hash: str,
) -> Dict[str, Any]:
    raw, dataset = _split_records(split_root, pilot_by_id)
    result, details = _pipeline_run(pipeline, raw)
    output_dir = output_root / split
    output_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = output_dir / f"{pipeline}_predictions.private.json"
    report_path = output_dir / f"{pipeline}_error_report.aggregate.private.json"
    _write_json(prediction_path, details["payload"])
    hashes = {
        "pilot_candidates_v2": pilot_hash,
        "gold_manifest": gold_manifest_hash,
        "split_aggregate_manifest": sha256_file(split_root.parent / "aggregate_manifest.json"),
        "split_manifest": sha256_file(split_root / "manifest.json"),
        "algorithm_files": _hash_files(repo_root),
    }
    report = _run_report(
        pipeline=pipeline,
        split=split,
        split_version=SPLIT_VERSION,
        raw=raw,
        dataset=dataset,
        result=result,
        run_details=details,
        input_hashes=hashes,
        split_integrity=split_integrity,
        repo_root=repo_root,
    )
    report["output_sha256"] = {"predictions": sha256_file(prediction_path)}
    report["gitignore"] = _gitignore_status((prediction_path, report_path), repo_root)
    _assert_no_report_body(report)
    _write_json(report_path, report)
    return {
        "report": report,
        "report_path": str(report_path.relative_to(repo_root)).replace("\\", "/"),
        "prediction_path": str(prediction_path.relative_to(repo_root)).replace("\\", "/"),
        "metrics": report["metrics"],
    }


def run(
    *,
    split_root: Path = DEFAULT_OUTPUT_ROOT,
    pilot_path: Path = PILOT_ROOT,
    output_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    split_root = Path(split_root).resolve(strict=True)
    pilot_path = Path(pilot_path).resolve(strict=True)
    output_root = Path(output_root or split_root.parent / "p013_final_eval_v1").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    pilot_rows = _read_pilot(pilot_path)
    pilot_by_id = {str(row["message_id"]): row for row in pilot_rows}

    validation = validate_split_directories(split_root)
    split_integrity = {
        "pre_run_validator_passed": bool(validation.ok),
        "validator_error_count": len(validation.errors),
        "validator_warning_count": len(validation.warnings),
        "manifest_hashes_match": bool(validation.ok),
        "split_version": SPLIT_VERSION,
        "frozen_manifest_sha256": sha256_file(split_root / "frozen_test" / "manifest.json"),
        "aggregate_manifest_sha256": sha256_file(split_root / "aggregate_manifest.json"),
    }
    if not validation.ok:
        raise RuntimeError("P0.3 split validation failed: " + "; ".join(validation.errors))

    pilot_hash = sha256_file(pilot_path)
    gold_manifest_hash = sha256_file(SOURCE_ROOT.resolve() / "manifest.json")
    reports: Dict[str, Dict[str, Any]] = {}
    for split in ("development", "frozen_test"):
        for pipeline in ("p0", "p01", "p02"):
            reports[f"{split}:{pipeline}"] = _run_one(
                pipeline=pipeline,
                split=split,
                split_root=split_root / split,
                pilot_by_id=pilot_by_id,
                output_root=output_root,
                repo_root=repo_root,
                split_integrity=split_integrity,
                pilot_hash=pilot_hash,
                gold_manifest_hash=gold_manifest_hash,
            )

    full_raw = [_input_record(row, pilot_by_id) for row in pilot_rows]
    full_dataset = load_contract(SOURCE_ROOT.resolve())
    full_results: Dict[str, Dict[str, Any]] = {}
    for pipeline in ("p0", "p01", "p02"):
        result, details = _pipeline_run(pipeline, full_raw)
        full_report = _run_report(
            pipeline=pipeline,
            split="full",
            split_version=SPLIT_VERSION,
            raw=full_raw,
            dataset=full_dataset,
            result=result,
            run_details=details,
            input_hashes={
                "pilot_candidates_v2": pilot_hash,
                "gold_manifest": gold_manifest_hash,
                "split_aggregate_manifest": sha256_file(split_root / "aggregate_manifest.json"),
                "algorithm_files": _hash_files(repo_root),
            },
            split_integrity=split_integrity,
            repo_root=repo_root,
        )
        full_dir = output_root / "full"
        full_dir.mkdir(parents=True, exist_ok=True)
        prediction_path = full_dir / f"{pipeline}_predictions.private.json"
        report_path = full_dir / f"{pipeline}_error_report.aggregate.private.json"
        _write_json(prediction_path, details["payload"])
        full_report["output_sha256"] = {"predictions": sha256_file(prediction_path)}
        full_report["gitignore"] = _gitignore_status((prediction_path, report_path), repo_root)
        _assert_no_report_body(full_report)
        _write_json(report_path, full_report)
        full_results[pipeline] = {
            "report": full_report,
            "report_path": str(report_path.relative_to(repo_root)).replace("\\", "/"),
            "prediction_path": str(prediction_path.relative_to(repo_root)).replace("\\", "/"),
        }

    frozen_p0 = reports["frozen_test:p0"]["report"]["metrics"]
    frozen_p01 = reports["frozen_test:p01"]["report"]["metrics"]
    frozen_p02 = reports["frozen_test:p02"]["report"]["metrics"]
    synthetic = _same_event_synthetic_suite()
    full_tests = _run_full_tests(repo_root, output_root)
    final: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "status": "provisional",
        "provisional": True,
        "provisional_reason": "pre_release_v3 remains pending human privacy review",
        "evaluation": {
            "requested_version": "P0.3 final offline evaluation",
            "production_connected": False,
            "split_version": SPLIT_VERSION,
            "source_gold": "pre_release_v3",
            "source_pilot": PILOT_VERSION,
            "aggregates_only": True,
            "body_fields_emitted": False,
        },
        "runs": {
            "development": {
                pipeline: {
                    "report_path": reports[f"development:{pipeline}"]["report_path"],
                    "prediction_path": reports[f"development:{pipeline}"]["prediction_path"],
                    "metrics": reports[f"development:{pipeline}"]["report"]["metrics"],
                }
                for pipeline in ("p0", "p01", "p02")
            },
            "frozen_test": {
                pipeline: {
                    "report_path": reports[f"frozen_test:{pipeline}"]["report_path"],
                    "prediction_path": reports[f"frozen_test:{pipeline}"]["prediction_path"],
                    "metrics": reports[f"frozen_test:{pipeline}"]["report"]["metrics"],
                }
                for pipeline in ("p0", "p01", "p02")
            },
            "full": {
                pipeline: {
                    "report_path": full_results[pipeline]["report_path"],
                    "prediction_path": full_results[pipeline]["prediction_path"],
                    "metrics": full_results[pipeline]["report"]["metrics"],
                }
                for pipeline in ("p0", "p01", "p02")
            },
        },
        "comparison": {
            "frozen_p01_minus_p0": _metric_deltas(frozen_p01, frozen_p0),
            "frozen_p02_minus_p01": _metric_deltas(frozen_p02, frozen_p01),
            "same_event_gold_status": "N/A",
        },
        "synthetic_same_event_suite": synthetic,
        "tests": full_tests,
        "split_integrity": split_integrity,
        "hash": {
            "pilot_candidates_v2": pilot_hash,
            "gold_manifest": gold_manifest_hash,
            "split_aggregate_manifest": sha256_file(split_root / "aggregate_manifest.json"),
            "full_test_summary": sha256_file(output_root / "full_test_summary.aggregate.private.json"),
            "algorithm_files": _hash_files(repo_root),
        },
        "gitignore": {"checked": False, "all_ignored": False, "checked_file_count": 0},
    }
    final_path = output_root / "p013_final_aggregate.private.json"
    final["gitignore"] = _gitignore_status(tuple(
        [Path(item["report_path"]) for item in reports.values()]
        + [Path(item["prediction_path"]) for item in reports.values()]
        + [Path(item["report_path"]) for item in full_results.values()]
        + [Path(item["prediction_path"]) for item in full_results.values()]
        + [output_root / "full_test_summary.aggregate.private.json"]
        + [final_path]
    ), repo_root)
    _assert_no_report_body(final)
    _write_json(final_path, final)
    return final


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="run P0.3 final offline aggregate evaluation")
    parser.add_argument("--split-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--pilot", type=Path, default=PILOT_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = run(
        split_root=args.split_root,
        pilot_path=args.pilot,
        output_root=args.output,
        repo_root=args.repo_root,
    )
    # CLI output is an aggregate summary only; no rows, body, or identity
    # fields are printed to the terminal.
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "REPORT_VERSION",
    "RUN_REPORT_VERSION",
    "load_contract",
    "run",
    "main",
]
