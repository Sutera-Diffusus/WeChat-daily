"""Independent K11 audit for the health-only Stage A pilot artifact.

This audit intentionally does not import the Stage A runner, the linear packet
implementation, a provider client, or any earlier audit.  It reads only the
new ``linear_stage_a_pilot_v1`` ledgers and writes body-free audit ledgers under
that artifact's ``audit/`` directory.  The expected K11 input is a blocked
health-only run: one synthetic provider call failed strict JSON validation
before development input was read or any page was selected.

The audit is evidence-oriented rather than a pilot runner.  A successful
audit means that the blocked health-only result is internally consistent; it
does not turn the failed health gate into permission for Stage A development
traffic or Stage B.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "linear_stage_a_pilot_v1"
REPORT_SCHEMA_VERSION = "linear_stage_a_pilot_report_v1"
RUNNER_SCHEMA_VERSION = "linear_stage_a_pilot_runner_v1"
DEFAULT_ARTIFACT_DIR = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / ARTIFACT_VERSION
)

EXPECTED_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "decisions.private.jsonl",
    "errors.private.jsonl",
    "ledger.private.jsonl",
    "selection.private.jsonl",
)
# The manifest cannot include its own byte hash without a circular digest.
# The runner therefore records hashes for every sibling ledger except itself.
HASHED_FILES = tuple(filename for filename in EXPECTED_FILES if filename != "manifest.private.json")

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "messagebody",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_body",
        "text_redacted",
        "transcript",
    }
)
SECRET_KEYS = frozenset(
    {
        "secret",
        "secrets",
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "password",
        "private_key",
    }
)
REASONING_KEYS = frozenset(
    {
        "reasoning",
        "chain_of_thought",
        "thoughts",
        "analysis",
        "completion",
    }
)
RETRY_KEY_MARKERS = frozenset(
    {
        "retry",
        "retries",
        "retry_count",
        "retry_after",
        "attempt",
        "attempts",
        "attempt_number",
    }
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AuditError(ValueError):
    """Fail-closed error for malformed or out-of-scope K11 input."""


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError("invalid_json:%s" % path.name) from exc
    if not isinstance(value, Mapping):
        raise AuditError("json_object_required:%s" % path.name)
    return {str(key): child for key, child in value.items()}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditError("unreadable_jsonl:%s" % path.name) from exc
    rows: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError("invalid_jsonl:%s:%d" % (path.name, line_number)) from exc
        if not isinstance(value, Mapping):
            raise AuditError("jsonl_object_required:%s:%d" % (path.name, line_number))
        rows.append({str(key): child for key, child in value.items()})
    return rows


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], (), {})


def _key_is_sensitive(key: str) -> Tuple[bool, bool, bool]:
    lowered = key.casefold()
    body = lowered in BODY_KEYS
    secret = lowered in SECRET_KEYS
    reasoning = lowered in REASONING_KEYS
    # Keep this strict and suffix-based so metadata such as
    # ``request_sha256`` and ``response_format_mode`` is not misclassified.
    body = body or lowered.endswith(("_body", "_text", "_transcript"))
    secret = secret or lowered.endswith(("_secret", "_token", "_password"))
    reasoning = reasoning or lowered.endswith(("_reasoning", "_thoughts"))
    return body, secret, reasoning


def _sensitive_hits(value: Any, path: str = "") -> Dict[str, List[str]]:
    hits: Dict[str, List[str]] = {"body": [], "secret": [], "reasoning": []}
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            body, secret, reasoning = _key_is_sensitive(key)
            if _non_empty(child):
                if body:
                    hits["body"].append(child_path)
                if secret:
                    hits["secret"].append(child_path)
                if reasoning:
                    hits["reasoning"].append(child_path)
            nested = _sensitive_hits(child, child_path)
            for name in hits:
                hits[name].extend(nested[name])
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            nested = _sensitive_hits(child, "%s[%d]" % (path, index))
            for name in hits:
                hits[name].extend(nested[name])
    return hits


def _retry_key_paths(value: Any, path: str = "") -> List[str]:
    paths: List[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            lowered = key.casefold()
            if lowered in RETRY_KEY_MARKERS or lowered.startswith("retry_"):
                paths.append(child_path)
            paths.extend(_retry_key_paths(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            paths.extend(_retry_key_paths(child, "%s[%d]" % (path, index)))
    return paths


def _get(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AuditError("integer_required:%s" % field) from exc


def _as_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AuditError("number_required:%s" % field) from exc


def _bool_equal(mapping: Mapping[str, Any], key: str, expected: bool) -> bool:
    return mapping.get(key) is expected


def _hash_record(value: Any, field: str) -> Dict[str, Any]:
    text = str(value or "")
    return {"field": field, "present": bool(text), "sha256_shape_ok": bool(HEX64.fullmatch(text))}


def _opaque_handles(row: Mapping[str, Any]) -> Tuple[List[str], bool]:
    refs = _get(row, "selected_opaque_refs", default={})
    if not isinstance(refs, Mapping):
        return [], False
    identifiers: List[str] = []
    for key in ("root_id", "page_id"):
        if refs.get(key) not in (None, ""):
            identifiers.append(str(refs[key]))
    handles: List[str] = []
    for key in ("message_handles", "candidate_handles", "evidence_handles"):
        children = refs.get(key, [])
        if not isinstance(children, (list, tuple)):
            return identifiers, False
        handles.extend(str(child) for child in children if child not in (None, ""))
    scope = refs.get("scope")
    scope_ok = isinstance(scope, Mapping) and bool(scope.get("account_id")) and bool(scope.get("chat_id"))
    format_ok = bool(identifiers and all("|" in value and value.count("|") >= 2 for value in handles))
    return identifiers + handles, bool(scope_ok and format_ok)


def _numeric_equal(left: Any, right: Any, tolerance: float = 1e-3) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def audit_artifact(artifact_dir: Path) -> Dict[str, Any]:
    artifact_dir = artifact_dir.resolve()
    if artifact_dir.name != ARTIFACT_VERSION:
        raise AuditError("wrong_artifact_directory")
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in artifact_dir.parts):
        raise AuditError("frozen_path_refused")
    if not artifact_dir.is_dir():
        raise AuditError("artifact_directory_missing")

    actual_files = {path.name for path in artifact_dir.iterdir() if path.is_file()}
    expected_files = set(EXPECTED_FILES)
    missing_files = sorted(expected_files - actual_files)
    unexpected_files = sorted(actual_files - expected_files)
    if missing_files:
        raise AuditError("required_file_missing:%s" % missing_files[0])

    manifest = _load_json(artifact_dir / "manifest.private.json")
    aggregate = _load_json(artifact_dir / "aggregate.private.json")
    cost = _load_json(artifact_dir / "cost.private.json")
    errors = _load_jsonl(artifact_dir / "errors.private.jsonl")
    ledger = _load_jsonl(artifact_dir / "ledger.private.jsonl")
    selection = _load_jsonl(artifact_dir / "selection.private.jsonl")
    decisions = _load_jsonl(artifact_dir / "decisions.private.jsonl")
    source_docs: Sequence[Any] = (manifest, aggregate, cost, errors, ledger, selection, decisions)

    artifact_hashes = _get(manifest, "artifact_hashes", default={})
    if not isinstance(artifact_hashes, Mapping):
        raise AuditError("artifact_hashes_missing")
    hash_results: List[Dict[str, Any]] = []
    for filename in HASHED_FILES:
        recorded = str(artifact_hashes.get(filename) or "")
        computed = _sha256_file(artifact_dir / filename)
        hash_results.append(
            {
                "file": filename,
                "recorded": recorded,
                "computed": computed,
                "matches": bool(recorded == computed and HEX64.fullmatch(recorded)),
            }
        )
    hashes_ok = bool(
        set(str(key) for key in artifact_hashes) == set(HASHED_FILES)
        and all(row["matches"] for row in hash_results)
    )

    sensitive = {"body": [], "secret": [], "reasoning": []}
    retry_paths: List[str] = []
    for index, document in enumerate(source_docs):
        label = "document[%d]" % index
        found = _sensitive_hits(document, label)
        for name in sensitive:
            sensitive[name].extend(found[name])
        retry_paths.extend(_retry_key_paths(document, label))
    body_free_source = not any(sensitive.values())

    provider_manifest = _get(manifest, "provider", default={})
    provider_aggregate = _get(aggregate, "provider", default={})
    health = _get(aggregate, "health", default={})
    aggregate_cost = _get(aggregate, "cost", default={})
    health_cost = _get(cost, "health", default={})
    topic_coverage = _get(aggregate, "topic_coverage", default={})
    coverage_pages = _get(topic_coverage, "pages", default={}) if isinstance(topic_coverage, Mapping) else {}
    ledger_row = ledger[0] if len(ledger) == 1 else {}
    ledger_refs, opaque_format_ok = _opaque_handles(ledger_row)

    manifest_shape = {
        "artifact_version": manifest.get("artifact_version") == ARTIFACT_VERSION,
        "report_schema_version": manifest.get("report_schema_version") == REPORT_SCHEMA_VERSION,
        "runner_schema_version": manifest.get("schema_version") == RUNNER_SCHEMA_VERSION,
        "local_day": manifest.get("local_day") == LOCAL_DAY,
        "split": manifest.get("split") == "development",
        "status": manifest.get("status") == "blocked",
        "success_false": manifest.get("success") is False,
        "development_input_unread": manifest.get("development_input_read") is False,
        "frozen_unread": manifest.get("frozen_read") is False,
        "gold_not_loaded": manifest.get("gold_loaded") is False,
        "production_state_not_written": manifest.get("production_state_written") is False,
    }

    call_values = {
        "manifest": _as_int(manifest.get("provider_calls"), "manifest.provider_calls"),
        "aggregate_cost": _as_int(aggregate_cost.get("provider_calls"), "aggregate.cost.provider_calls"),
        "aggregate_provider": _as_int(provider_aggregate.get("calls"), "aggregate.provider.calls"),
        "cost": _as_int(cost.get("provider_calls"), "cost.provider_calls"),
        "ledger_provider_rows": sum(1 for row in ledger if row.get("provider_call") is True),
    }
    calls_consistent = all(value == 1 for value in call_values.values())
    call_limit = _as_int(manifest.get("provider_call_limit"), "manifest.provider_call_limit")
    within_call_limit = 1 <= call_limit and call_values["manifest"] <= call_limit

    error_codes = [str(row.get("error_code") or "") for row in errors]
    strict_error = bool(
        len(errors) == 1
        and errors[0].get("error_code") == "provider_invalid_json"
        and errors[0].get("phase") == "health"
    )
    health_only = bool(
        len(ledger) == 1
        and ledger_row.get("phase") == "health"
        and ledger_row.get("provider_call") is True
        and not selection
        and not decisions
        and _as_int(_get(aggregate_cost, "development_calls", default=0), "aggregate.cost.development_calls") == 0
        and _as_int(_get(cost, "provider_calls", default=0), "cost.provider_calls") == 1
    )
    selected_page_count = _as_int(manifest.get("selected_page_count"), "manifest.selected_page_count")
    selected_zero = bool(
        selected_page_count == 0
        and not selection
        and _as_int(_get(coverage_pages, "selected", default=0), "topic_coverage.pages.selected") == 0
    )
    development_unread = bool(
        manifest.get("development_input_read") is False
        and aggregate.get("development_input_read") is False
        and _get(aggregate, "input_manifest_digest", default="") == ""
    )

    retry_free = bool(not retry_paths and len(ledger) == 1 and call_values["ledger_provider_rows"] == 1)
    cache_hits = sum(1 for row in ledger if row.get("cache_hit") is True)
    cache_complete_only = bool(cache_hits == 0 and _as_int(_get(aggregate_cost, "cached_completions", default=0), "aggregate.cost.cached_completions") == 0)

    health_ok = bool(
        isinstance(health, Mapping)
        and health.get("ok") is False
        and health.get("status") == "blocked"
        and health.get("error_code") == "provider_invalid_json"
        and health.get("provider_call") is True
    )
    health_cost_match = bool(
        isinstance(health_cost, Mapping)
        and health_cost.get("error_code") == "provider_invalid_json"
        and health_cost.get("status") == "blocked"
        and _numeric_equal(health_cost.get("input_token_proxy"), health.get("input_token_proxy"))
        and _numeric_equal(health_cost.get("latency_ms"), health.get("latency_ms"))
        and health_cost.get("request_sha256") == health.get("request_sha256")
    )
    latency_ms = _as_float(_get(health, "latency_ms"), "health.latency_ms")
    input_proxy = _as_int(_get(health, "input_token_proxy"), "health.input_token_proxy")
    output_limit = _as_int(_get(health, "output_limit"), "health.output_limit")
    limits = _get(cost, "limits", default={})
    token_latency_ok = bool(
        latency_ms > 0
        and input_proxy >= 0
        and input_proxy <= _as_int(_get(limits, "health_input_proxy"), "cost.limits.health_input_proxy")
        and output_limit == _as_int(_get(limits, "max_output_tokens"), "cost.limits.max_output_tokens")
        and _as_int(_get(health, "input_tokens", default=0), "health.input_tokens") == 0
        and _as_int(_get(health, "output_tokens", default=0), "health.output_tokens") == 0
    )

    model_source_values = {
        "manifest_model": _get(provider_manifest, "model"),
        "aggregate_model": _get(provider_aggregate, "model"),
        "health_model": _get(health, "model"),
        "cost_model": _get(health_cost, "model"),
        "ledger_model": _get(ledger_row, "model"),
        "manifest_source": _get(provider_manifest, "source"),
        "aggregate_source": _get(provider_aggregate, "source"),
        "health_source": _get(health, "source"),
        "cost_source": _get(health_cost, "source"),
        "ledger_source": _get(ledger_row, "source"),
        "manifest_response_format": _get(provider_manifest, "response_format_mode"),
        "aggregate_response_format": _get(provider_aggregate, "response_format_mode"),
        "health_response_format": _get(health, "response_format_mode"),
        "cost_response_format": _get(health_cost, "response_format_mode"),
    }
    model_source_recorded = bool(
        all(model_source_values[key] for key in ("manifest_model", "aggregate_model", "health_model", "cost_model", "ledger_model"))
        and all(model_source_values[key] for key in ("manifest_source", "aggregate_source", "health_source", "cost_source", "ledger_source"))
        and all(model_source_values[key] for key in ("manifest_response_format", "aggregate_response_format", "health_response_format", "cost_response_format"))
        and len({model_source_values[key] for key in ("manifest_model", "aggregate_model", "health_model", "cost_model", "ledger_model")}) == 1
        and len({model_source_values[key] for key in ("manifest_source", "aggregate_source", "health_source", "cost_source", "ledger_source")}) == 1
        and len({model_source_values[key] for key in ("manifest_response_format", "aggregate_response_format", "health_response_format", "cost_response_format")}) == 1
    )

    request_hashes = {
        "aggregate_health": _get(health, "request_sha256"),
        "cost_health": _get(health_cost, "request_sha256"),
        "ledger": _get(ledger_row, "request_sha256"),
    }
    system_hashes = {
        "manifest": _get(manifest, "system_prompt_sha256"),
        "ledger": _get(ledger_row, "system_prompt_sha256"),
    }
    user_hashes = {"ledger": _get(ledger_row, "user_packet_sha256")}
    hashes_recorded = bool(
        all(HEX64.fullmatch(str(value or "")) for value in request_hashes.values())
        and all(HEX64.fullmatch(str(value or "")) for value in system_hashes.values())
        and all(HEX64.fullmatch(str(value or "")) for value in user_hashes.values())
        and len(set(str(value) for value in request_hashes.values())) == 1
        and len(set(str(value) for value in system_hashes.values())) == 1
    )

    audit_checks = {
        "files_exact_and_hashes": bool(not missing_files and not unexpected_files and hashes_ok),
        "manifest_shape": all(manifest_shape.values()),
        "health_only": health_only,
        "calls_one_and_within_limit": bool(calls_consistent and within_call_limit),
        "development_input_unread": development_unread,
        "selected_pages_zero": selected_zero,
        "retry_free": retry_free,
        "source_body_free": body_free_source,
        "opaque_health_refs": opaque_format_ok,
        "strict_error": strict_error,
        "health_blocked": health_ok,
        "model_source_response_recorded": model_source_recorded,
        "request_system_user_hashes_recorded": hashes_recorded,
        "health_cost_match": health_cost_match,
        "token_latency_within_health_limit": token_latency_ok,
        "cache_complete_only": cache_complete_only,
    }
    audit_pass = all(audit_checks.values())

    # Stage B is never enabled by a health-only artifact.  One bounded,
    # synthetic-only protocol diagnostic is useful for this exact error, but
    # it is a separate gate and must not read development pages.
    allow_protocol_diagnostic = bool(
        audit_pass
        and health_ok
        and strict_error
        and calls_consistent
        and within_call_limit
        and selected_zero
        and development_unread
    )
    report: Dict[str, Any] = {
        "audit_schema_version": "linear_stage_a_pilot_k11_audit_v1",
        "artifact_version": ARTIFACT_VERSION,
        "artifact_directory": artifact_dir.name,
        "audit_status": "pass" if audit_pass else "fail",
        "artifact_status": "blocked_health_only" if health_only and health_ok else "unexpected",
        "health_gate": "blocked" if health_ok else "invalid",
        "allow_stage_a_development": False,
        "allow_stage_b_pilot": False,
        "allow_one_protocol_diagnostic": allow_protocol_diagnostic,
        "audit_checks": audit_checks,
        "manifest_shape": manifest_shape,
        "missing_files": missing_files,
        "unexpected_root_files": unexpected_files,
        "hash_results": hash_results,
        "body_free": bool(body_free_source),
        "body_field_hits": len(sensitive["body"]),
        "secret_field_hits": len(sensitive["secret"]),
        "reasoning_field_hits": len(sensitive["reasoning"]),
        "retry_field_hits": len(retry_paths),
        "call_counts": call_values,
        "provider_call_limit": call_limit,
        "call_budget_remaining": call_limit - call_values["manifest"],
        "development_input_read": bool(manifest.get("development_input_read")),
        "development_calls": _as_int(_get(aggregate_cost, "development_calls", default=0), "aggregate.cost.development_calls"),
        "selected_page_count": selected_page_count,
        "health_opaque_ref_count": len(ledger_refs),
        "health_opaque_ref_format_ok": opaque_format_ok,
        "cache_hits": cache_hits,
        "cache_complete_only": cache_complete_only,
        "error_codes": error_codes,
        "strict_error": "provider_invalid_json" if strict_error else "invalid_error_set",
        "provider_record": {
            "provider": _get(provider_manifest, "provider"),
            "model": _get(provider_manifest, "model"),
            "source": _get(provider_manifest, "source"),
            "response_format_mode": _get(provider_manifest, "response_format_mode"),
            "model_source_response_consistent": model_source_recorded,
        },
        "hash_record": {
            "request_sha256": _hash_record(request_hashes["aggregate_health"], "health.request_sha256"),
            "system_prompt_sha256": _hash_record(system_hashes["manifest"], "manifest.system_prompt_sha256"),
            "user_packet_sha256": _hash_record(user_hashes["ledger"], "ledger.user_packet_sha256"),
            "all_required_hashes_recorded": hashes_recorded,
        },
        "health_metrics": {
            "input_token_proxy": input_proxy,
            "input_token_proxy_limit": _as_int(_get(limits, "health_input_proxy"), "cost.limits.health_input_proxy"),
            "output_limit": output_limit,
            "latency_ms": latency_ms,
            "input_tokens": _as_int(_get(health, "input_tokens", default=0), "health.input_tokens"),
            "output_tokens": _as_int(_get(health, "output_tokens", default=0), "health.output_tokens"),
            "within_health_limit": token_latency_ok,
        },
        "stage_a_semantics": {
            "topic_grouping_evaluated": False,
            "primary_coverage": "not_applicable_health_blocked",
            "candidate_overmerge_check": "not_applicable_health_blocked",
            "person_object_metadata_preservation": "not_evaluable_health_blocked",
            "evidence_binding": "not_run",
            "reason": "selected_page_count_zero",
        },
        "next_step": {
            "allow_stage_a_development": False,
            "allow_stage_b_pilot": False,
            "allow_one_protocol_diagnostic": allow_protocol_diagnostic,
            "diagnostic_call_count": 1 if allow_protocol_diagnostic else 0,
            "diagnostic_scope": "synthetic_health_only" if allow_protocol_diagnostic else "none",
            "diagnostic_is_stage_b": False,
            "diagnostic_must_not_read_development_input": True,
            "reason": "provider_invalid_json" if strict_error else "health_gate_not_verified",
        },
        "scope": {
            "opaque_only": True,
            "provider_calls_by_audit": 0,
            "frozen_path_read_by_audit": False,
            "production_state_written_by_audit": False,
        },
    }
    report_body_hits = _sensitive_hits(report)
    if any(report_body_hits.values()):
        raise AuditError("audit_output_not_body_free")
    return report


def _human_rows(report: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    checks = report.get("audit_checks", {})
    for name, value in checks.items():
        yield {"check": name, "status": "pass" if value is True else "fail"}
    yield {"check": "health_gate", "status": report.get("health_gate")}
    yield {"check": "strict_error", "status": report.get("strict_error")}
    yield {"check": "allow_stage_b_pilot", "status": report.get("next_step", {}).get("allow_stage_b_pilot")}
    yield {"check": "allow_one_protocol_diagnostic", "status": report.get("next_step", {}).get("allow_one_protocol_diagnostic")}


def write_audit(report: Mapping[str, Any], artifact_dir: Path) -> Tuple[Path, Path]:
    audit_dir = artifact_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary_path = audit_dir / "audit_summary.private.json"
    human_path = audit_dir / "human_audit.private.jsonl"
    summary_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    human_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in _human_rows(report)),
        encoding="utf-8",
    )
    return summary_path, human_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Independent K11 Stage A pilot health-only audit")
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args(argv)
    report = audit_artifact(args.artifact_dir)
    summary_path, human_path = write_audit(report, args.artifact_dir.resolve())
    print(json.dumps({"audit_status": report["audit_status"], "summary": str(summary_path), "human": str(human_path)}, sort_keys=True))
    return 0 if report["audit_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
