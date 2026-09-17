"""Body-free readiness audit for a contextual-bundle v2 pilot artifact.

This is an audit utility, not a production runner.  It intentionally reads
only the v2 aggregate, manifest, cost, and errors projections.  It never opens
bundles, decisions, requests, registry, gate, snapshots, or any input/frozen
directory.  The report is therefore suitable for sharing as a readiness
pointer without copying message text or identity fields.

Example::

    py tests/audit_contextual_bundle_pipeline_v2.py \
        --artifact-dir data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2

The command exits zero after producing a report, including when production
readiness is blocked.  Use ``--strict`` when a blocked readiness result should
be represented by a non-zero process status.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, Mapping, Sequence


_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wechat_bridge.contextual_bundle_pipeline import (  # noqa: E402
    ContextualBundlePipeline,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_LLM_BUNDLE_CALLS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    PIPELINE_SCHEMA_VERSION,
    PIPELINE_VERSION,
)
from wechat_bridge.contextual_bundle_pipeline_runner import (  # noqa: E402
    OUTPUT_FILENAMES,
    RUNNER_SCHEMA_VERSION,
    run_development_shadow_pilot,
)


REPORT_SCHEMA_VERSION = "contextual_bundle_pipeline_v2_readiness_v1"
ARTIFACT_VERSION = "contextual_bundle_pipeline_v2"
EXPECTED_DECISION_COUNT = 848
EXPECTED_BUNDLE_COUNT = 848
EXPECTED_BUDGET = {
    "max_bundle_calls": DEFAULT_MAX_LLM_BUNDLE_CALLS,
    "max_input_tokens": DEFAULT_MAX_INPUT_TOKENS,
    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
}
ALLOWED_METADATA_FILES = frozenset(
    {
        "aggregate.private.json",
        "manifest.private.json",
        "cost.private.json",
        "errors.private.jsonl",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# These names are checked only in the four allowed metadata projections.  A
# finding reports a key name, never the value attached to it.
BODY_KEY_NAMES = frozenset(
    {
        "body",
        "claim_text_redacted",
        "content",
        "evidence_text",
        "fragment_text_redacted",
        "message_text",
        "narrative",
        "prompt",
        "quote",
        "raw",
        "raw_message",
        "raw_text",
        "response",
        "surface",
        "surface_redacted",
        "text",
    }
)


def _load_allowed_json(root: Path, filename: str) -> Any:
    if filename not in ALLOWED_METADATA_FILES or filename.endswith(".jsonl"):
        raise ValueError("audit is restricted to allowed JSON metadata files")
    return json.loads((root / filename).read_text(encoding="utf-8"))


def _load_allowed_jsonl(root: Path, filename: str) -> list[Dict[str, Any]]:
    if filename not in ALLOWED_METADATA_FILES or not filename.endswith(".jsonl"):
        raise ValueError("audit is restricted to the allowed errors JSONL file")
    rows: list[Dict[str, Any]] = []
    for line in (root / filename).read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("errors projection must contain JSON objects")
            rows.append(value)
    return rows


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _total(mapping: Any) -> int:
    if not isinstance(mapping, Mapping):
        return 0
    total = 0
    for value in mapping.values():
        if isinstance(value, bool):
            continue
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
    return total


def _walk_key_names(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            lower = name.casefold()
            if name in BODY_KEY_NAMES or lower.endswith("_text") or lower.endswith("_content"):
                found.append(path + "." + name)
            found.extend(_walk_key_names(child, path + "." + name))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_walk_key_names(child, "%s[%d]" % (path, index)))
    return found


def _check(name: str, status: str, *, codes: Iterable[str] = (), evidence: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "name": name,
        "status": status,
        "codes": list(codes),
    }
    if evidence:
        result["evidence"] = dict(evidence)
    return result


def _interface_check() -> Dict[str, Any]:
    required_pipeline = {
        "run": ("messages",),
        "__init__": (
            "mode",
            "model",
            "cache",
            "max_bundle_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_retries",
            "window_size",
            "max_candidates",
        ),
    }
    missing: list[str] = []
    try:
        for method_name, parameters in required_pipeline.items():
            method = getattr(ContextualBundlePipeline, method_name)
            available = set(inspect.signature(method).parameters)
            missing.extend("%s.%s" % (method_name, parameter) for parameter in parameters if parameter not in available)
        runner_parameters = set(inspect.signature(run_development_shadow_pilot).parameters)
        for parameter in ("input_directory", "output_directory", "mode", "model", "max_bundle_calls", "max_retries"):
            if parameter not in runner_parameters:
                missing.append("runner.%s" % parameter)
    except (AttributeError, TypeError, ValueError) as exc:
        missing.append(type(exc).__name__.casefold())
    return _check(
        "code_interface",
        "pass" if not missing else "fail",
        codes=() if not missing else ("INTERFACE_SURFACE_MISSING",),
        evidence={
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "runner_schema_version": RUNNER_SCHEMA_VERSION,
            "missing": missing,
        },
    )


def _synthetic_replay_check() -> Dict[str, Any]:
    """Exercise the public pipeline twice without reading or writing data."""

    synthetic_messages = (
        {
            "message_id": "synthetic-readiness-message",
            "account_id": "synthetic-readiness-account",
            "chat_id": "synthetic-readiness-chat",
            "chat_type": "direct",
            "speaker_id": "synthetic-readiness-speaker",
            "content": "synthetic readiness body",
            "message_type": "text",
            "sequence_in_chat": 1,
            "time_offset_seconds": 1,
            "split": "development",
            "source_mode": "synthetic",
        },
    )
    body_keys = BODY_KEY_NAMES | {"sender_name", "private_sender_id"}

    def keys(value: Any) -> set[str]:
        if isinstance(value, Mapping):
            output = {str(key) for key in value}
            for child in value.values():
                output.update(keys(child))
            return output
        if isinstance(value, (list, tuple)):
            output: set[str] = set()
            for child in value:
                output.update(keys(child))
            return output
        return set()

    try:
        first = ContextualBundlePipeline(mode="disabled").run(synthetic_messages, split="development")
        second = ContextualBundlePipeline(mode="disabled").run(synthetic_messages, split="development")
        first_artifacts = first.artifacts()
        second_artifacts = second.artifacts()
        stable = (
            first.input_sha256 == second.input_sha256
            and first.manifest["replay_key"] == second.manifest["replay_key"]
            and first_artifacts == second_artifacts
        )
        body_free = not (keys(first_artifacts) & body_keys) and not (keys(second_artifacts) & body_keys)
        status = "pass" if stable and body_free else "fail"
        codes = () if status == "pass" else ("SYNTHETIC_REPLAY_OR_BODY_FREE_FAILED",)
        evidence = {
            "stable_input_hash": first.input_sha256 == second.input_sha256,
            "stable_replay_key": first.manifest["replay_key"] == second.manifest["replay_key"],
            "stable_body_free_artifacts": first_artifacts == second_artifacts,
            "body_free": body_free,
            "candidate_bundle_count": len(first.bundles),
        }
    except Exception as exc:  # pragma: no cover - surfaced as an audit finding
        status = "fail"
        codes = ("SYNTHETIC_REPLAY_INTERFACE_ERROR",)
        evidence = {"exception_type": type(exc).__name__}
    return _check("synthetic_code_replay", status, codes=codes, evidence=evidence)


def audit_artifact(artifact_directory: str | Path) -> Dict[str, Any]:
    """Audit a v2 output directory without opening body-bearing artifacts."""

    root = Path(artifact_directory)
    if not root.is_dir():
        raise ValueError("artifact directory does not exist")

    # This is the complete read allow-list.  Do not broaden it to make a
    # failed check look better: the readiness report must remain body-free.
    aggregate = _load_allowed_json(root, "aggregate.private.json")
    manifest = _load_allowed_json(root, "manifest.private.json")
    cost = _load_allowed_json(root, "cost.private.json")
    errors = _load_allowed_jsonl(root, "errors.private.jsonl")
    if not all(isinstance(item, Mapping) for item in (aggregate, manifest, cost)):
        raise ValueError("allowed JSON projections must be objects")

    expected_names = set(OUTPUT_FILENAMES.values())
    actual_names = {item.name for item in root.iterdir() if item.is_file()}
    missing_names = sorted(expected_names - actual_names)
    unexpected_names = sorted(actual_names - expected_names)
    output_files = manifest.get("output_files") if isinstance(manifest, Mapping) else None
    manifest_names = set(output_files.values()) if isinstance(output_files, Mapping) else set()
    inventory_ok = (
        len(expected_names) == 12
        and len(actual_names & expected_names) == 12
        and not missing_names
        and not unexpected_names
        and manifest_names == expected_names
    )
    inventory = _check(
        "twelve_artifacts",
        "pass" if inventory_ok else "fail",
        codes=() if inventory_ok else ("ARTIFACT_INVENTORY_MISMATCH",),
        evidence={
            "expected_file_count": len(expected_names),
            "present_expected_count": len(actual_names & expected_names),
            "missing": missing_names,
            "unexpected_root_files": unexpected_names,
            "manifest_output_file_count": len(manifest_names),
        },
    )

    hash_fields = {
        "aggregate.input_sha256": aggregate.get("input_sha256"),
        "aggregate.code_sha256": aggregate.get("code_sha256"),
        "aggregate.replay_key": aggregate.get("replay_key"),
        "manifest.input_sha256": manifest.get("input_sha256"),
        "manifest.pipeline_input_sha256": manifest.get("pipeline_input_sha256"),
        "manifest.registry_input_sha256": manifest.get("registry_input_sha256"),
        "manifest.dialogue_input_sha256": manifest.get("dialogue_input_sha256"),
        "manifest.code_sha256": manifest.get("code_sha256"),
        "manifest.replay_key": manifest.get("replay_key"),
    }
    hash_shapes_ok = all(_sha256(value) for value in hash_fields.values())
    hash_links_ok = (
        aggregate.get("input_sha256") == manifest.get("input_sha256")
        and aggregate.get("code_sha256") == manifest.get("code_sha256")
        and aggregate.get("replay_key") == manifest.get("replay_key")
    )
    hashes = _check(
        "input_and_replay_hashes",
        "pass" if hash_shapes_ok and hash_links_ok else "fail",
        codes=() if hash_shapes_ok and hash_links_ok else ("HASH_LINK_OR_SHAPE_MISMATCH",),
        evidence={
            "sha256_field_count": len(hash_fields),
            "all_sha256_shaped": hash_shapes_ok,
            "aggregate_manifest_links_equal": hash_links_ok,
            "input_recomputed": False,
            "input_recompute_reason": "raw input is outside this body-free audit allow-list",
        },
    )

    candidate_count = aggregate.get("candidate_bundle_count")
    decision_count = aggregate.get("decision_count")
    status_counts = aggregate.get("status_counts") or {}
    source_counts = aggregate.get("source_counts") or {}
    request_status_counts = aggregate.get("request_status_counts") or {}
    consistency_ok = (
        candidate_count == EXPECTED_BUNDLE_COUNT
        and decision_count == EXPECTED_DECISION_COUNT
        and manifest.get("candidate_bundle_count") == EXPECTED_BUNDLE_COUNT
        and manifest.get("decision_count") == EXPECTED_DECISION_COUNT
        and cost.get("candidate_bundle_count") == EXPECTED_BUNDLE_COUNT
        and cost.get("decision_count") == EXPECTED_DECISION_COUNT
        and _total(status_counts) == EXPECTED_DECISION_COUNT
        and _total(source_counts) == EXPECTED_DECISION_COUNT
        and _total(request_status_counts) == EXPECTED_DECISION_COUNT
        and aggregate.get("open_context_snapshot_count") == EXPECTED_DECISION_COUNT
    )
    decision_consistency = _check(
        "848_decision_consistency",
        "pass" if consistency_ok else "fail",
        codes=() if consistency_ok else ("DECISION_COUNT_OR_STATUS_SUM_MISMATCH",),
        evidence={
            "candidate_bundle_count": candidate_count,
            "decision_count": decision_count,
            "open_context_snapshot_count": aggregate.get("open_context_snapshot_count"),
            "status_counts": dict(status_counts) if isinstance(status_counts, Mapping) else {},
            "source_counts": dict(source_counts) if isinstance(source_counts, Mapping) else {},
            "request_status_counts": dict(request_status_counts) if isinstance(request_status_counts, Mapping) else {},
        },
    )

    budget = cost.get("budget") if isinstance(cost, Mapping) else {}
    budget_ok = isinstance(budget, Mapping) and all(
        budget.get(key) == value for key, value in EXPECTED_BUDGET.items()
    ) and budget.get("calls_used") == 0 and budget.get("calls_remaining") == DEFAULT_MAX_LLM_BUNDLE_CALLS and budget.get("rejection_count") == 0
    budget_check = _check(
        "budget_14_2000_400",
        "pass" if budget_ok else "fail",
        codes=() if budget_ok else ("BUDGET_LEDGER_MISMATCH",),
        evidence={
            "configured": {key: budget.get(key) for key in EXPECTED_BUDGET},
            "calls_used": budget.get("calls_used"),
            "calls_remaining": budget.get("calls_remaining"),
            "rejection_count": budget.get("rejection_count"),
            "cache_misses": budget.get("cache_misses"),
        },
    )

    semantic_stats = cost.get("semantic_stats") if isinstance(cost, Mapping) else {}
    fallback_ok = (
        cost.get("mode") == "real"
        and manifest.get("mode") == "real"
        and cost.get("provider") == "disabled"
        and manifest.get("provider") == "disabled"
        and cost.get("provider_configured") is False
        and manifest.get("provider_configured") is False
        and cost.get("fallback_count") == EXPECTED_DECISION_COUNT
        and cost.get("complete_count") == 0
        and cost.get("pending_count") == 0
        and isinstance(semantic_stats, Mapping)
        and semantic_stats.get("model_calls") == 0
        and semantic_stats.get("model_successes") == 0
        and semantic_stats.get("fallback_calls") == EXPECTED_DECISION_COUNT
    )
    source_model_check = _check(
        "source_model_fallback_markers",
        "pass" if fallback_ok else "fail",
        codes=() if fallback_ok else ("SOURCE_MODEL_FALLBACK_MARKER_MISMATCH",),
        evidence={
            "mode": cost.get("mode"),
            "provider": cost.get("provider"),
            "provider_configured": cost.get("provider_configured"),
            "model": cost.get("model"),
            "fallback_count": cost.get("fallback_count"),
            "complete_count": cost.get("complete_count"),
            "pending_count": cost.get("pending_count"),
            "model_calls": semantic_stats.get("model_calls") if isinstance(semantic_stats, Mapping) else None,
        },
    )

    provider_error_codes = {
        str(item.get("code"))
        for item in errors
        if isinstance(item, Mapping) and item.get("code")
    }
    blocked_ok = (
        cost.get("blocked") is True
        and cost.get("provider") == "disabled"
        and cost.get("provider_configured") is False
        and "provider_not_configured" in provider_error_codes
        and cost.get("complete_count") == 0
        and isinstance(semantic_stats, Mapping)
        and semantic_stats.get("model_successes") == 0
    )
    provider_blocked = _check(
        "provider_blocked_never_masquerades_as_ai",
        "pass" if blocked_ok else "fail",
        codes=() if blocked_ok else ("BLOCKED_PROVIDER_MASQUERADE_OR_MISSING",),
        evidence={
            "blocked": cost.get("blocked"),
            "provider": cost.get("provider"),
            "provider_configured": cost.get("provider_configured"),
            "provider_error_codes": sorted(provider_error_codes),
            "model_successes": semantic_stats.get("model_successes") if isinstance(semantic_stats, Mapping) else None,
            "fallback_count": cost.get("fallback_count"),
        },
    )

    body_paths = []
    for label, value in (("aggregate", aggregate), ("manifest", manifest), ("cost", cost), ("errors", errors)):
        body_paths.extend(label + path[1:] for path in _walk_key_names(value))
    body_free = _check(
        "allowed_metadata_has_no_body_fields",
        "pass" if not body_paths else "fail",
        codes=() if not body_paths else ("BODY_FIELD_IN_ALLOWED_METADATA",),
        evidence={"body_field_paths": body_paths},
    )

    v2_identity_ok = (
        manifest.get("artifact_version") == ARTIFACT_VERSION
        and manifest.get("output_directory_name") == root.name
        and manifest.get("runner_schema_version") == RUNNER_SCHEMA_VERSION
    )
    # No v1 artifact is opened or used as evidence.  This is intentionally a
    # visible gap: a v2 pilot cannot claim v1 comparison coverage by itself.
    v1_coverage = _check(
        "v1_coverage_is_not_claimed",
        "pass" if v2_identity_ok else "fail",
        codes=() if v2_identity_ok else ("V2_IDENTITY_MISMATCH",),
        evidence={
            "artifact_version": manifest.get("artifact_version"),
            "v2_identity": v2_identity_ok,
            "v1_artifact_read": False,
            "v1_comparison_coverage": "not_evaluated",
            "v1_gap_code": "V1_NOT_COVERED",
        },
    )

    interface = _interface_check()
    synthetic_replay = _synthetic_replay_check()
    pilot_checks = {
        "twelve_artifacts": inventory,
        "input_and_replay_hashes": hashes,
        "source_model_fallback_markers": source_model_check,
        "848_decision_consistency": decision_consistency,
        "budget_14_2000_400": budget_check,
        "allowed_metadata_has_no_body_fields": body_free,
        "provider_blocked_never_masquerades_as_ai": provider_blocked,
        "v1_coverage_is_not_claimed": v1_coverage,
        "code_interface": interface,
        "synthetic_code_replay": synthetic_replay,
    }

    # These are deliberately conservative production-gate determinations.  A
    # passing artifact-inventory check is not the same thing as semantic
    # acceptance: the allowed projections contain no fragment body or labels.
    d_gates: Dict[str, Dict[str, Any]] = {
        "D1": {
            "status": "partial",
            "codes": ["D1_ARTIFACT_SCHEMA_PRESENT", "D1_FULL_CONTRACT_ACCEPTANCE_NOT_REPROVEN"],
            "evidence": ["schema_version", "pipeline_version", "ruleset_version", "code_interface"],
        },
        "D2": {
            "status": "partial",
            "codes": ["D2_INPUT_HASH_PRESENT", "D2_REGISTRY_CONTENT_NOT_INSPECTED"],
            "evidence": ["input_sha256", "registry_input_sha256", "message_count"],
        },
        "D3": {
            "status": "blocked",
            "codes": ["D3_FRAGMENT_ROLE_OBJECT_STATE_METRICS_MISSING"],
            "evidence": [],
        },
        "D4": {
            "status": "blocked",
            "codes": ["D4_GATE_LOG_NOT_INSPECTED", "D4_PROVIDER_BLOCKED_FALLBACK_ONLY"],
            "evidence": ["provider_blocked_never_masquerades_as_ai"],
        },
        "D5": {
            "status": "partial",
            "codes": ["D5_OPEN_SNAPSHOT_COUNT_PRESENT", "D5_TYPED_EVIDENCE_NOT_INSPECTED"],
            "evidence": ["open_context_snapshot_count", "candidate_bundle_count"],
        },
        "D6": {
            "status": "blocked",
            "codes": ["D6_RECALL_CURVE_NOT_PRESENT", "D6_CROSS_CHAT_AND_TIME_ONLY_METRICS_MISSING"],
            "evidence": [],
        },
        "D7": {
            "status": "blocked",
            "codes": ["D7_LLM_NOT_EXECUTED", "D7_SCHEMA_EVIDENCE_METRICS_MISSING"],
            "evidence": ["model_calls", "fallback_count"],
        },
        "D8": {
            "status": "blocked",
            "codes": ["D8_THREAD_EVENT_DERIVATION_NOT_DELIVERED", "D8_RELATION_COUNT_ZERO_IS_NOT_ACCURACY"],
            "evidence": ["relation_count"],
        },
        "D9": {
            "status": "blocked",
            "codes": ["D9_ACCURACY_NA", "D9_REAL_GOLD_EVALUATION_MISSING", "V1_NOT_COVERED"],
            "evidence": ["accuracy", "scoring", "gold_loaded"],
        },
        "D10": {
            "status": "partial",
            "codes": ["D10_REPLAY_KEY_PRESENT", "D10_SECOND_RUN_AND_FEEDBACK_DIFF_NOT_PROVEN"],
            "evidence": ["input_and_replay_hashes"],
        },
        "D11": {
            "status": "blocked",
            "codes": ["D11_API_DOM_SCREENSHOT_E2E_MISSING", "D11_PRODUCTION_SIDE_EFFECT_PROOF_MISSING"],
            "evidence": [],
        },
    }
    missing_gates = [name for name, gate in d_gates.items() if gate["status"] != "accepted"]
    overall_status = "blocked" if missing_gates else "ready_for_d12_review"
    report: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_directory_name": root.name,
        "audit_scope": {
            "allowed_files": sorted(ALLOWED_METADATA_FILES),
            "body_free": True,
            "private_body_read": False,
            "frozen_read": False,
            "v1_artifact_read": False,
        },
        "pilot": {
            "local_day": aggregate.get("local_day"),
            "split": aggregate.get("split"),
            "message_count": aggregate.get("message_count"),
            "candidate_bundle_count": candidate_count,
            "decision_count": decision_count,
            "error_count": aggregate.get("error_count"),
            "relation_count": aggregate.get("relation_count"),
            "open_context_snapshot_count": aggregate.get("open_context_snapshot_count"),
            "mode": cost.get("mode"),
            "model": cost.get("model"),
            "provider": cost.get("provider"),
            "accuracy": manifest.get("accuracy"),
            "scoring": manifest.get("scoring"),
        },
        "checks": pilot_checks,
        "d1_d11": d_gates,
        "production_readiness": {
            "status": overall_status,
            "production_ready": False,
            "missing_gates": missing_gates,
            "zero_tolerance_status": "not_established",
            "reason_codes": [
                "PROVIDER_BLOCKED_FALLBACK_ONLY",
                "REAL_SEMANTIC_ACCURACY_NOT_EVALUATED",
                "D9_AND_D11_REQUIRED_BEFORE_D12",
            ],
            "next_iteration": [
                "run an authorized non-frozen semantic evaluation with fixed gold and holdout scoring",
                "record fragment/role/object/state and context relation metrics with N/A reasons",
                "execute LLM schema/evidence/timeout fallback checks under the bounded budget",
                "complete thread/event provenance and API/DOM/screenshot shadow E2E",
                "repeat the same development input and compare stable hashes/IDs plus audit feedback diff",
            ],
        },
    }
    return report


def write_report(artifact_directory: str | Path, output_path: str | Path | None = None) -> Dict[str, Any]:
    root = Path(artifact_directory)
    report = audit_artifact(root)
    destination = Path(output_path) if output_path is not None else root / "audit" / "readiness.private.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit contextual bundle v2 body-free readiness")
    parser.add_argument("--artifact-dir", required=True, help="v2 output directory")
    parser.add_argument("--output", default=None, help="readiness report path (defaults to v2/audit/readiness.private.json)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when production readiness is blocked")
    args = parser.parse_args(argv)
    report = write_report(args.artifact_dir, args.output)
    print(
        json.dumps(
            {
                "status": report["production_readiness"]["status"],
                "production_ready": report["production_readiness"]["production_ready"],
                "missing_gates": report["production_readiness"]["missing_gates"],
                "output": str(Path(args.output) if args.output else Path(args.artifact_dir) / "audit" / "readiness.private.json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if args.strict and not report["production_readiness"]["production_ready"] else 0


if __name__ == "__main__":  # pragma: no cover - exercised as an audit command
    raise SystemExit(main())
