"""Independent, body-free audit for the K13 protocol-health artifact.

This side-car intentionally has no dependency on the Stage-A runner, a
provider client, frozen data, or development input.  It reads only the K13
artifact, one existing body-free capability-health record, and redacted
settings metadata.  The diagnostic candidate is kept separate from the
strict wire result: a health result can be complete while a prose-mixed
candidate is deliberately not accepted for development.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "linear_stage_a_protocol_health_v2"
AUDIT_SCHEMA_VERSION = "linear_stage_a_protocol_health_k13_audit_v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
CAPABILITY_VERSION = "contextual_bundle_pipeline_v2_10"
CAPABILITY_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / CAPABILITY_VERSION
SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"
REQUIRED_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "ledger.private.jsonl",
    "errors.private.jsonl",
    "diagnostic.private.json",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VISION_MARKERS = ("vision", "multimodal", "image", "audio", "video")
EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_SOURCE = "deepseek-openai-compatible"
EXPECTED_RESPONSE_FORMAT = "omitted"
EXPECTED_REPORT_SCHEMA = "linear_stage_a_protocol_health_report_v2"
EXPECTED_RUNNER_SCHEMA = "linear_stage_a_protocol_health_runner_v2"
LATENCY_LIMIT_MS = 30_000.0

# These are key-name checks, not value dumps.  Safe counts, hashes, enum
# values, and opaque handles remain reportable; body-bearing values never do.
BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "content",
        "content_body",
        "content_text",
        "detail",
        "details",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "output_text",
        "prompt",
        "quote",
        "raw",
        "raw_content",
        "raw_output",
        "raw_reasoning",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "redacted_text",
        "response_body",
        "response_text",
        "summary",
        "system_prompt",
        "text",
        "text_body",
        "text_redacted",
        "thoughts",
        "transcript",
        "user_input",
        "user_packet",
        "user_canonical_json",
    }
)
SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "private_key",
        "secret",
        "secrets",
        "token",
    }
)
REASONING_KEYS = frozenset({"analysis", "chain_of_thought", "completion", "thoughts"})
RETRY_KEYS = frozenset(
    {
        "attempt",
        "attempt_number",
        "attempts",
        "retry",
        "retries",
        "retry_after",
        "retry_attempts",
        "retry_count",
        "retry_used",
    }
)
CALL_KEYS = frozenset(
    {
        "actual_provider_calls",
        "calls",
        "diagnostic_call_count",
        "health_call_count",
        "provider_attempts",
        "provider_call_count",
        "provider_calls",
        "provider_request_attempts",
    }
)
DEV_KEYS = frozenset(
    {
        "development_call_count",
        "development_calls",
        "development_input_read",
        "development_read",
        "development_requests",
    }
)
HANDLE_KEYS = frozenset(
    {
        "candidate_handle",
        "candidate_handles",
        "candidate_handle_refs",
        "candidate_link_refs",
        "evidence_handle",
        "evidence_handles",
        "evidence_handle_refs",
        "message_handle",
        "message_handles",
        "message_handle_refs",
    }
)


class AuditError(ValueError):
    """Fail-closed error for malformed or out-of-scope audit input."""


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
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError("invalid_jsonl:%s:%d" % (path.name, index)) from exc
        if not isinstance(value, Mapping):
            raise AuditError("jsonl_object_required:%s:%d" % (path.name, index))
        rows.append({str(key): child for key, child in value.items()})
    return rows


def _iter_nodes(value: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            yield child_path, child
            yield from _iter_nodes(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = "%s[%d]" % (path, index)
            yield child_path, child
            yield from _iter_nodes(child, child_path)


def _key_name(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[", 1)[0].casefold()


def _key_values(value: Any, names: Iterable[str]) -> Iterator[Tuple[str, Any]]:
    wanted = {str(name).casefold() for name in names}
    for path, child in _iter_nodes(value):
        if _key_name(path) in wanted:
            yield path, child


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _safe_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.name != ARTIFACT_VERSION or resolved.parent.name != LOCAL_DAY:
        raise AuditError("out_of_scope_artifact_directory")
    expected_root = (ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY).resolve()
    try:
        resolved.relative_to(expected_root)
    except ValueError as exc:
        raise AuditError("artifact_directory_outside_private_root") from exc
    return resolved


def _sensitive_hits(documents: Mapping[str, Any]) -> Dict[str, List[str]]:
    hits: Dict[str, List[str]] = {"body": [], "secret": [], "reasoning": []}
    for name, document in documents.items():
        for path, value in _iter_nodes(document):
            key = _key_name(path)
            if key in BODY_KEYS:
                hits["body"].append("%s.%s" % (name, path))
            if key in SECRET_KEYS or key.endswith(("_secret", "_token", "_password", "_key")):
                hits["secret"].append("%s.%s" % (name, path))
            if key in REASONING_KEYS:
                hits["reasoning"].append("%s.%s" % (name, path))
    return hits


def _artifact_hash_check(artifact_dir: Path, manifest: Mapping[str, Any], actual: Sequence[str]) -> Dict[str, Any]:
    recorded = manifest.get("artifact_hashes")
    expected = {name for name in actual if name != "manifest.private.json"}
    if not isinstance(recorded, Mapping):
        return {"recorded": False, "all_match": False, "hashed_file_count": 0, "match_count": 0}
    rows: List[bool] = []
    for name in sorted(expected):
        recorded_hash = str(recorded.get(name) or "")
        try:
            computed = hashlib.sha256((artifact_dir / name).read_bytes()).hexdigest()
        except OSError:
            computed = ""
        rows.append(bool(HEX64.fullmatch(recorded_hash) and recorded_hash == computed))
    return {
        "recorded": True,
        "all_match": bool(set(str(key) for key in recorded) == expected and rows and all(rows)),
        "hashed_file_count": len(rows),
        "match_count": sum(bool(row) for row in rows),
    }


def _privacy_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]], errors: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    all_documents: Dict[str, Any] = dict(documents)
    all_documents["ledger"] = list(ledger)
    all_documents["errors"] = list(errors)
    hits = _sensitive_hits(all_documents)
    return {
        "body_field_hits": len(hits["body"]),
        "secret_field_hits": len(hits["secret"]),
        "reasoning_field_hits": len(hits["reasoning"]),
        "body_free": not any(hits.values()),
    }


def _raw_and_reasoning_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    raw_values: List[Any] = []
    reasoning_lengths: List[float] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"raw_response_saved", "raw_reasoning_saved", "raw_content_saved"}):
            raw_values.append(value)
        for _path, value in _key_values(document, {"reasoning_length", "reasoning_content_length"}):
            number = _as_number(value)
            if number is not None:
                reasoning_lengths.append(number)
    for row in ledger:
        for key in ("raw_response_saved", "raw_reasoning_saved", "raw_content_saved"):
            if key in row:
                raw_values.append(row.get(key))
        for key in ("reasoning_length", "reasoning_content_length"):
            number = _as_number(row.get(key))
            if number is not None:
                reasoning_lengths.append(number)
    raw_true = sum(value is True for value in raw_values)
    return {
        "raw_flags_observed": len(raw_values),
        "raw_true_count": raw_true,
        "raw_zero": bool(raw_values) and raw_true == 0,
        "reasoning_lengths_observed": len(reasoning_lengths),
        "reasoning_nonzero_count": sum(value != 0 for value in reasoning_lengths),
        "reasoning_zero": bool(reasoning_lengths) and all(value == 0 for value in reasoning_lengths),
    }


def _validate_handle(value: Any) -> Optional[Tuple[str, str, str, str]]:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        return None
    scope, kind, identifier = parts
    if "/" not in scope:
        return None
    account, chat = scope.split("/", 1)
    if not account or not chat or kind not in {"message", "candidate", "evidence"} or not identifier:
        return None
    return account, chat, kind, identifier


def _handle_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    all_documents: Dict[str, Any] = dict(documents)
    all_documents["ledger"] = list(ledger)
    total = 0
    valid = 0
    invalid = 0
    scope_pairs: set[Tuple[str, str]] = set()
    kind_counts = {"message": 0, "candidate": 0, "evidence": 0}
    field_presence = {"message": 0, "candidate": 0, "evidence": 0}
    for document in all_documents.values():
        for path, value in _iter_nodes(document):
            key = _key_name(path)
            if key not in HANDLE_KEYS:
                continue
            expected = "message" if "message" in key else "candidate" if "candidate" in key else "evidence"
            field_presence[expected] += 1
            values = value if isinstance(value, list) else [value]
            for item in values:
                total += 1
                parsed = _validate_handle(item)
                if parsed is None or parsed[2] != expected:
                    invalid += 1
                    continue
                valid += 1
                account, chat, kind, _identifier = parsed
                scope_pairs.add((account, chat))
                kind_counts[kind] += 1
    return {
        "observed_count": total,
        "valid_count": valid,
        "invalid_count": invalid,
        "format_ok": bool(total and valid == total),
        "scope_count": len(scope_pairs),
        "scope_consistent": len(scope_pairs) == 1,
        "field_presence": field_presence,
        "required_fields_present": all(field_presence.values()),
        "kind_counts": kind_counts,
    }


def _schema_check(documents: Mapping[str, Any]) -> Dict[str, Any]:
    manifest = documents["manifest"]
    aggregate = documents["aggregate"]
    diagnostic = documents["diagnostic"]
    observed: List[str] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"schema_version", "report_schema_version", "runner_schema_version"}):
            if isinstance(value, str) and value.strip():
                observed.append(value.strip())
    unexpected = sorted(set(observed) - {EXPECTED_REPORT_SCHEMA, EXPECTED_RUNNER_SCHEMA})
    mapping_ok = bool(
        manifest.get("schema_version") == EXPECTED_RUNNER_SCHEMA
        and manifest.get("report_schema_version") == EXPECTED_REPORT_SCHEMA
        and aggregate.get("schema_version") == EXPECTED_REPORT_SCHEMA
        and aggregate.get("runner_schema_version") == EXPECTED_RUNNER_SCHEMA
        and diagnostic.get("schema_version") == EXPECTED_REPORT_SCHEMA
    )
    return {
        "versions_observed": sorted(set(observed)),
        "expected_versions_present": {EXPECTED_REPORT_SCHEMA, EXPECTED_RUNNER_SCHEMA} <= set(observed),
        "unexpected_versions": unexpected,
        "mapping_ok": mapping_ok,
        "schema_valid": bool(observed) and not unexpected and mapping_ok,
    }


def _strict_candidate_check(diagnostic: Mapping[str, Any], aggregate: Mapping[str, Any]) -> Dict[str, Any]:
    strict = diagnostic.get("strict_result")
    aggregate_strict = aggregate.get("strict_result")
    response = diagnostic.get("response_diagnostics")
    candidate = diagnostic.get("diagnostic_candidate")
    strict_mapping = isinstance(strict, Mapping)
    aggregate_mapping = isinstance(aggregate_strict, Mapping)
    response_mapping = isinstance(response, Mapping)
    candidate_mapping = isinstance(candidate, Mapping)
    strict_result_ok = bool(
        strict_mapping
        and strict.get("strict_complete") is True
        and strict.get("parse_code") == "ok"
        and strict.get("validation_code") == "ok"
        and strict.get("status") == "available"
        and strict.get("accepted_for_stage_a_development") is False
        and aggregate_mapping
        and aggregate_strict.get("strict_complete") is True
    )
    wire_ok = bool(
        response_mapping
        and response.get("strict_parse_code") == "ok"
        and response.get("strict_validation_code") == "ok"
        and response.get("unique_json_object_count") == 1
        and response.get("leading_shape") == "brace"
        and response.get("fence_detected") is False
        and response.get("finish_reasons") == ["stop"]
    )
    candidate_ok = bool(
        candidate_mapping
        and candidate.get("present") is True
        and candidate.get("accepted_for_complete") is False
        and candidate.get("status") == "candidate_valid_schema_not_accepted"
        and candidate.get("parse_code") == "ok"
        and candidate.get("validation_code") == "ok"
        and isinstance(candidate.get("safe_field_names"), list)
    )
    strict_paths = [path for path, _value in _key_values(diagnostic, {"strict_result"})]
    candidate_paths = [path for path, _value in _key_values(diagnostic, {"diagnostic_candidate"})]
    nested = any(
        candidate_path.startswith(strict_path + ".") or strict_path.startswith(candidate_path + ".")
        for strict_path in strict_paths
        for candidate_path in candidate_paths
    )
    separate = len(strict_paths) == 1 and len(candidate_paths) == 1 and not nested
    return {
        "strict_result_ok": strict_result_ok,
        "wire_strict_ok": wire_ok,
        "strict_json_ok": strict_result_ok and wire_ok,
        "strict_complete": strict_result_ok and wire_ok,
        "candidate_ok": candidate_ok,
        "candidate_separate": separate,
        "candidate_accepted_for_development": False,
        "candidate_extraction_is_prose_mixed": candidate.get("extraction") == "single_json_object_with_prose"
        if candidate_mapping
        else False,
    }


def _sections(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    sections: List[Mapping[str, Any]] = []
    for document in documents.values():
        for key in ("provider", "response_diagnostics"):
            value = document.get(key)
            if isinstance(value, Mapping):
                sections.append(value)
    sections.extend(row for row in ledger if isinstance(row, Mapping))
    return sections


def _provider_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    sections = _sections(documents, ledger)
    models = [str(section["model"]).strip() for section in sections if isinstance(section.get("model"), str) and section["model"].strip()]
    sources = [str(section["source"]).strip() for section in sections if isinstance(section.get("source"), str) and section["source"].strip()]
    formats = [str(section["response_format_mode"]).strip().casefold() for section in sections if isinstance(section.get("response_format_mode"), str)]
    sent_values = [_as_bool(section.get("response_format_sent")) for section in sections if _as_bool(section.get("response_format_sent")) is not None]
    unique_models = sorted(set(models))
    unique_sources = sorted(set(sources))
    unique_formats = sorted(set(formats))
    aggregate = documents["aggregate"]
    manifest = documents["manifest"]
    response_format = aggregate.get("response_format")
    response_object_ok = bool(
        isinstance(response_format, Mapping)
        and response_format.get("mode") == EXPECTED_RESPONSE_FORMAT
        and response_format.get("predeclared") is True
        and response_format.get("sent") is False
    )
    provider = aggregate.get("provider") if isinstance(aggregate.get("provider"), Mapping) else {}
    manifest_provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
    protocol_ok = bool(
        unique_models == [EXPECTED_MODEL]
        and unique_sources == [EXPECTED_SOURCE]
        and unique_formats == [EXPECTED_RESPONSE_FORMAT]
        and sent_values
        and not any(sent_values)
        and response_object_ok
        and provider.get("response_format_mode") == EXPECTED_RESPONSE_FORMAT
        and provider.get("response_format_sent") is False
        and manifest_provider.get("response_format_mode") == EXPECTED_RESPONSE_FORMAT
        and manifest_provider.get("response_format_sent") is False
    )
    return {
        "models": unique_models,
        "sources": unique_sources,
        "response_formats": unique_formats,
        "model": unique_models[0] if len(unique_models) == 1 else "",
        "nonvision": len(unique_models) == 1 and not any(marker in unique_models[0].casefold() for marker in VISION_MARKERS),
        "response_format_protocol_ok": protocol_ok,
    }


def _thinking_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    values: List[bool] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"thinking_disabled"}):
            parsed = _as_bool(value)
            if parsed is not None:
                values.append(parsed)
    for row in ledger:
        parsed = _as_bool(row.get("thinking_disabled"))
        if parsed is not None:
            values.append(parsed)
    extra_body_checks: List[bool] = []
    for name in ("aggregate", "manifest"):
        extra = documents[name].get("extra_body")
        fields = extra.get("field_names") if isinstance(extra, Mapping) else None
        extra_body_checks.append(
            bool(
                isinstance(extra, Mapping)
                and extra.get("sent") is True
                and extra.get("per_call") is True
                and extra.get("global_settings_mutated") is False
                and extra.get("value_shape") == "disabled"
                and isinstance(fields, list)
                and fields == ["thinking"]
            )
        )
    return {
        "thinking_values_observed": len(values),
        "all_per_call_disabled": bool(values) and all(values),
        "extra_body_checks": extra_body_checks,
        "extra_body_protocol_ok": bool(extra_body_checks) and all(extra_body_checks),
        "thinking_disabled_ok": bool(values) and all(values) and bool(extra_body_checks) and all(extra_body_checks),
    }


def _call_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    values: List[float] = []
    for document in documents.values():
        for _path, value in _key_values(document, CALL_KEYS):
            number = _as_number(value)
            if number is not None:
                values.append(number)
    ledger_provider_calls = sum(row.get("provider_call") is True for row in ledger)
    provider = documents["aggregate"].get("provider") if isinstance(documents["aggregate"].get("provider"), Mapping) else {}
    call_limit = _as_number(provider.get("call_limit"))
    return {
        "observed_count_fields": len(values),
        "observed_max": int(max(values)) if values and max(values).is_integer() else max(values) if values else 0,
        "ledger_rows": len(ledger),
        "ledger_provider_calls": ledger_provider_calls,
        "provider_call_limit": int(call_limit) if call_limit is not None and call_limit.is_integer() else call_limit,
        "exactly_one": bool(values) and all(value == 1 for value in values) and ledger_provider_calls == 1 and len(ledger) == 1,
        "at_most_one": bool(values) and all(value <= 1 for value in values) and ledger_provider_calls <= 1,
        "within_declared_limit": call_limit == 1 and provider.get("within_call_limit") is True,
    }


def _retry_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    observed = 0
    violations: List[str] = []
    for name, document in documents.items():
        for path, value in _key_values(document, RETRY_KEYS):
            parsed_bool = _as_bool(value)
            number = _as_number(value)
            if parsed_bool is not None:
                observed += 1
                if parsed_bool and _key_name(path).startswith("retry"):
                    violations.append("%s.%s" % (name, path))
            elif number is not None:
                observed += 1
                key = _key_name(path)
                if number != 0 and not (key in {"attempt", "attempt_number", "attempts"} and number <= 1):
                    violations.append("%s.%s" % (name, path))
            elif _non_empty(value):
                violations.append("%s.%s" % (name, path))
    for index, row in enumerate(ledger):
        if row.get("retry") is True or _as_number(row.get("retry_count")) not in {None, 0.0}:
            violations.append("ledger[%d]" % index)
    return {
        "retry_fields_observed": observed,
        "retry_violations": len(violations),
        "retry_free": not violations,
    }


def _development_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    violations: List[str] = []
    development_calls = 0.0
    read_flags: List[bool] = []
    for name, document in documents.items():
        for path, value in _key_values(document, DEV_KEYS):
            key = _key_name(path)
            if key in {"development_input_read", "development_read"}:
                parsed = _as_bool(value)
                if parsed is not None:
                    read_flags.append(parsed)
                    if parsed:
                        violations.append("%s.%s" % (name, path))
            else:
                number = _as_number(value)
                if number is not None:
                    development_calls += number
                    if number != 0:
                        violations.append("%s.%s" % (name, path))
        for path, value in _key_values(document, {"phase"}):
            if isinstance(value, str) and "development" in value.casefold():
                violations.append("%s.%s" % (name, path))
    for index, row in enumerate(ledger):
        if "development" in str(row.get("phase") or "").casefold():
            violations.append("ledger[%d].phase" % index)
    aggregate = documents["aggregate"]
    manifest = documents["manifest"]
    stage_a_flags = [aggregate.get("stage_a_development")]
    stage_b_flags = [aggregate.get("stage_b_pilot")]
    stage_c_flags = [aggregate.get("stage_c_pilot")]
    return {
        "development_calls": int(development_calls) if development_calls.is_integer() else development_calls,
        "development_read_flags": read_flags,
        "development_violations": len(violations),
        "diagnostic_only_flags": [aggregate.get("diagnostic_only"), manifest.get("diagnostic_only")],
        "diagnostic_only": aggregate.get("diagnostic_only") is True and manifest.get("diagnostic_only") is True,
        "stage_a_flags_false": all(value is False for value in stage_a_flags),
        "stage_b_flags_false": all(value is False for value in stage_b_flags),
        # This schema does not emit a Stage-C flag.  Missing means no Stage-C
        # authorization; only an explicit true value is a violation here.
        "stage_c_flags_false": all(value is False or value is None for value in stage_c_flags),
        "development_free": not violations and development_calls == 0 and not any(read_flags),
    }


def _settings_digest(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return the runner's raw settings-file SHA-256, never file contents."""
    if not path.is_file():
        return None, None
    try:
        payload = path.read_bytes()
    except OSError:
        return None, None
    return hashlib.sha256(payload).hexdigest(), None


def _settings_check(documents: Mapping[str, Any]) -> Dict[str, Any]:
    before: List[str] = []
    after: List[str] = []
    flags: List[bool] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"settings_before_sha256", "settings_before_hash"}):
            if isinstance(value, str) and HEX64.fullmatch(value):
                before.append(value)
        for _path, value in _key_values(document, {"settings_after_sha256", "settings_after_hash", "settings_sha256"}):
            if isinstance(value, str) and HEX64.fullmatch(value):
                after.append(value)
        for path, value in _key_values(document, {"settings_unchanged", "settings_mutated", "settings_written"}):
            parsed = _as_bool(value)
            if parsed is not None:
                key = _key_name(path)
                flags.append((not parsed) if key in {"settings_mutated", "settings_written"} else parsed)
    current_digest, _current_model = _settings_digest(SETTINGS_PATH)
    before_after_equal = bool(before and after and set(before) == set(after))
    current_match = bool(current_digest and after and current_digest in set(after))
    explicit = bool(flags) and all(flags)
    return {
        "settings_file_present": current_digest is not None,
        "settings_hash_shape_ok": bool(before and after),
        "before_after_hash_equal": before_after_equal,
        "current_hash_matches_recorded": current_match,
        "explicit_unchanged": explicit,
        "settings_unchanged": before_after_equal and current_match and explicit,
        "hash_basis": "raw_settings_file_sha256",
    }


def _capability_check(model: str, aggregate: Mapping[str, Any]) -> Dict[str, Any]:
    prior = aggregate.get("prior_state") if isinstance(aggregate.get("prior_state"), Mapping) else {}
    referenced = prior.get("fallback_capability_artifact") == CAPABILITY_VERSION
    path = CAPABILITY_DIR / "provider_health.private.json"
    if not path.is_file():
        return {
            "artifact_present": False,
            "referenced": referenced,
            "model_match": False,
            "nonvision": False,
            "health_ok": False,
            "response_format_omitted": False,
            "thinking_disabled": False,
            "raw_zero": False,
            "evidence_ok": False,
        }
    try:
        health = _load_json(path)
    except AuditError:
        return {
            "artifact_present": False,
            "referenced": referenced,
            "model_match": False,
            "nonvision": False,
            "health_ok": False,
            "response_format_omitted": False,
            "thinking_disabled": False,
            "raw_zero": False,
            "evidence_ok": False,
        }
    diagnostics = health.get("diagnostics") if isinstance(health.get("diagnostics"), Mapping) else {}
    capability_hits = _sensitive_hits({"provider_health": health})
    health_model = health.get("model")
    # Older capability-health records carry this flag only in diagnostics;
    # absence at the top level is not evidence that the protocol was sent.
    response_format_ok = health.get("response_format_sent") in {None, False} and diagnostics.get("response_format_sent") is False
    thinking_ok = diagnostics.get("thinking_disabled") is True
    raw_zero = diagnostics.get("raw_content_saved") is False and diagnostics.get("raw_reasoning_saved") is False
    nonvision = isinstance(health_model, str) and not any(marker in health_model.casefold() for marker in VISION_MARKERS)
    evidence_ok = bool(
        referenced
        and health_model == model == EXPECTED_MODEL
        and nonvision
        and health.get("ok") is True
        and health.get("status") == "available"
        and not any(capability_hits.values())
    )
    return {
        "artifact_present": True,
        "referenced": referenced,
        "model_match": health_model == model == EXPECTED_MODEL,
        "nonvision": nonvision,
        "health_ok": health.get("ok") is True and health.get("status") == "available",
        "response_format_omitted": response_format_ok,
        "thinking_disabled": thinking_ok,
        "raw_zero": raw_zero,
        "evidence_ok": evidence_ok,
    }


def _numeric_consistency(values: Sequence[float], tolerance: float = 1e-6) -> bool:
    return bool(values) and max(values) - min(values) <= tolerance


def _token_latency_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    input_tokens: List[float] = []
    output_tokens: List[float] = []
    latencies: List[float] = []
    proxies: List[float] = []
    proxy_limits: List[float] = []
    output_limits: List[float] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"input_tokens"}):
            number = _as_number(value)
            if number is not None:
                input_tokens.append(number)
        for _path, value in _key_values(document, {"output_tokens"}):
            number = _as_number(value)
            if number is not None:
                output_tokens.append(number)
        for _path, value in _key_values(document, {"latency_ms"}):
            number = _as_number(value)
            if number is not None:
                latencies.append(number)
        for _path, value in _key_values(document, {"input_token_proxy"}):
            number = _as_number(value)
            if number is not None:
                proxies.append(number)
        for _path, value in _key_values(document, {"input_token_proxy_limit"}):
            number = _as_number(value)
            if number is not None:
                proxy_limits.append(number)
        for _path, value in _key_values(document, {"max_output_tokens"}):
            number = _as_number(value)
            if number is not None:
                output_limits.append(number)
    for row in ledger:
        for key, target in (("input_tokens", input_tokens), ("output_tokens", output_tokens), ("latency_ms", latencies)):
            number = _as_number(row.get(key))
            if number is not None:
                target.append(number)
    input_limit = min(proxy_limits) if proxy_limits else 0.0
    output_limit = min(output_limits) if output_limits else 0.0
    within_limits = bool(
        input_tokens
        and output_tokens
        and latencies
        and proxies
        and input_limit > 0
        and output_limit > 0
        and max(proxies) <= input_limit
        and max(input_tokens) <= input_limit
        and max(output_tokens) <= output_limit
        and min(latencies) > 0
        and max(latencies) <= LATENCY_LIMIT_MS
    )
    return {
        "input_tokens_consistent": _numeric_consistency(input_tokens),
        "output_tokens_consistent": _numeric_consistency(output_tokens),
        "latency_consistent": _numeric_consistency(latencies),
        "input_token_proxy_limit": int(input_limit) if input_limit.is_integer() else input_limit,
        "max_output_tokens_limit": int(output_limit) if output_limit.is_integer() else output_limit,
        "latency_limit_ms": int(LATENCY_LIMIT_MS),
        "within_limits": within_limits,
        "token_latency_ok": bool(
            within_limits
            and _numeric_consistency(input_tokens)
            and _numeric_consistency(output_tokens)
            and _numeric_consistency(latencies)
        ),
    }


def _error_cache_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]], errors: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    aggregate_errors = documents["aggregate"].get("errors")
    error_count = _nested(aggregate_errors, "count") if isinstance(aggregate_errors, Mapping) else None
    ledger_errors = [row.get("error_code") for row in ledger if _non_empty(row.get("error_code"))]
    error_codes = [row.get("error_code") or row.get("code") for row in errors if _non_empty(row.get("error_code") or row.get("code"))]
    cache_values = [row.get("cache_hit") for row in ledger if row.get("cache_hit") is not None]
    return {
        "errors_file_rows": len(errors),
        "aggregate_error_count_zero": error_count == 0,
        "ledger_error_codes": len(ledger_errors),
        "errors_file_codes": len(error_codes),
        "cache_hit_values": cache_values,
        "no_errors_or_cache": not errors and not ledger_errors and not error_codes and all(value is False for value in cache_values),
    }


def _opaque_hash_shape(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    values: List[str] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"request_sha256", "user_packet_sha256", "system_prompt_sha256", "output_sha256"}):
            if isinstance(value, str):
                values.append(value)
    for row in ledger:
        for key in ("request_sha256", "user_packet_sha256", "system_prompt_sha256", "output_sha256"):
            if isinstance(row.get(key), str):
                values.append(row[key])
    return {
        "hashes_observed": len(values),
        "hashes_shape_ok": bool(values) and all(bool(HEX64.fullmatch(value)) for value in values),
    }


def audit_artifact(artifact_dir: Path) -> Dict[str, Any]:
    artifact_dir = _safe_path(artifact_dir)
    if not artifact_dir.is_dir():
        raise AuditError("artifact_directory_missing")
    actual = sorted(path.name for path in artifact_dir.iterdir() if path.is_file())
    missing = sorted(set(REQUIRED_FILES) - set(actual))
    unexpected = sorted(set(actual) - set(REQUIRED_FILES))
    if missing:
        raise AuditError("required_artifact_file_missing:%s" % ",".join(missing))
    manifest = _load_json(artifact_dir / "manifest.private.json")
    aggregate = _load_json(artifact_dir / "aggregate.private.json")
    cost = _load_json(artifact_dir / "cost.private.json")
    diagnostic = _load_json(artifact_dir / "diagnostic.private.json")
    ledger = _load_jsonl(artifact_dir / "ledger.private.jsonl")
    errors = _load_jsonl(artifact_dir / "errors.private.jsonl")
    documents: Dict[str, Any] = {
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": diagnostic,
    }

    privacy = _privacy_check(documents, ledger, errors)
    raw = _raw_and_reasoning_check(documents, ledger)
    handles = _handle_check(documents, ledger)
    schema = _schema_check(documents)
    strict = _strict_candidate_check(diagnostic, aggregate)
    provider = _provider_check(documents, ledger)
    thinking = _thinking_check(documents, ledger)
    calls = _call_check(documents, ledger)
    retry = _retry_check(documents, ledger)
    development = _development_check(documents, ledger)
    settings = _settings_check(documents)
    capability = _capability_check(provider["model"], aggregate)
    tokens = _token_latency_check(documents, ledger)
    error_cache = _error_cache_check(documents, ledger, errors)
    opaque_hashes = _opaque_hash_shape(documents, ledger)
    hash_result = _artifact_hash_check(artifact_dir, manifest, actual)

    provider_meta = aggregate.get("provider") if isinstance(aggregate.get("provider"), Mapping) else {}
    manifest_provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
    status = str(manifest.get("status") or aggregate.get("status") or "").casefold()
    manifest_shape = {
        "artifact_version": manifest.get("artifact_version") == ARTIFACT_VERSION,
        "local_day": manifest.get("local_day") == LOCAL_DAY,
        "status_available": status == "available",
        "success": manifest.get("success") is True,
        "diagnostic_only": manifest.get("diagnostic_only") is True,
        "development_unread": manifest.get("development_input_read") is False,
        "frozen_unread": manifest.get("frozen_read") is False,
        "production_state_not_written": manifest.get("production_state_written") is False,
        "provider_called": manifest.get("provider_called") is True,
        "provider_calls_one": manifest.get("provider_calls") == 1,
        "diagnostic_call_one": manifest.get("diagnostic_call_count") == 1,
        "retry_zero": manifest.get("retry_count") == 0,
        "settings_unchanged": manifest.get("settings_unchanged") is True,
        "thinking_disabled": manifest.get("thinking_disabled") is True,
        "response_format_omitted": manifest_provider.get("response_format_mode") == EXPECTED_RESPONSE_FORMAT,
        "response_format_not_sent": manifest_provider.get("response_format_sent") is False,
    }
    diagnostic_only_not_development = bool(
        development["diagnostic_only"]
        and development["development_free"]
        and development["stage_a_flags_false"]
        and not strict["candidate_accepted_for_development"]
        and aggregate.get("diagnostic_call_count") == 1
    )
    raw_zero = bool(raw["raw_zero"] and raw["reasoning_zero"] and privacy["body_free"])
    schema_and_strict_ok = bool(schema["schema_valid"] and strict["strict_json_ok"])
    health_complete = bool(
        manifest_shape["status_available"]
        and schema_and_strict_ok
        and strict["candidate_ok"]
        and strict["candidate_separate"]
        and handles["format_ok"]
        and handles["scope_consistent"]
        and handles["required_fields_present"]
        and calls["exactly_one"]
        and retry["retry_free"]
        and development["development_free"]
        and diagnostic_only_not_development
        and provider["nonvision"]
        and provider["response_format_protocol_ok"]
        and thinking["thinking_disabled_ok"]
        and settings["settings_unchanged"]
        and capability["evidence_ok"]
        and tokens["token_latency_ok"]
        and raw_zero
        and error_cache["no_errors_or_cache"]
    )
    audit_checks = {
        "files_present_and_hashes": bool(not missing and not unexpected and hash_result["all_match"]),
        "manifest_shape": all(manifest_shape.values()),
        "real_calls_exactly_one_and_limited": calls["exactly_one"] and calls["at_most_one"] and calls["within_declared_limit"],
        "no_retry": retry["retry_free"],
        "no_development": development["development_free"],
        "diagnostic_only_not_development": diagnostic_only_not_development,
        "nonvision_model": provider["nonvision"],
        "capability_evidence": capability["evidence_ok"],
        "response_format_protocol": provider["response_format_protocol_ok"],
        "thinking_disabled_per_call": thinking["thinking_disabled_ok"],
        "settings_hash_unchanged": settings["settings_unchanged"],
        "strict_json_and_schema": schema_and_strict_ok,
        "diagnostic_candidate_separate": strict["candidate_separate"] and strict["candidate_ok"],
        "schema_message_evidence_handles": handles["format_ok"] and handles["scope_consistent"] and handles["required_fields_present"],
        "tokens_and_latency_within_limits": tokens["token_latency_ok"],
        "source_body_free": privacy["body_free"],
        "raw_output_and_reasoning_zero": raw_zero,
        "no_error_or_cache_substitution": error_cache["no_errors_or_cache"],
    }
    audit_pass = all(audit_checks.values()) and health_complete
    allow_k10 = bool(audit_pass and health_complete)
    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_status": status or "unknown",
        "audit_status": "pass" if audit_pass else "fail",
        "audit_checks": audit_checks,
        "missing_files": missing,
        "unexpected_root_files": unexpected,
        "hashes": hash_result,
        "privacy": privacy,
        "raw_and_reasoning": raw,
        "provider": {
            "model": provider["model"],
            "model_count": len(provider["models"]),
            "nonvision": provider["nonvision"],
            "source": EXPECTED_SOURCE if provider["sources"] == [EXPECTED_SOURCE] else "",
            "response_format": EXPECTED_RESPONSE_FORMAT if provider["response_formats"] == [EXPECTED_RESPONSE_FORMAT] else "",
        },
        "capability_evidence": capability,
        "settings": settings,
        "call_counts": calls,
        "retry": retry,
        "development": development,
        "thinking": thinking,
        "strict_json": strict,
        "schema": schema,
        "handles": handles,
        "tokens_and_latency": tokens,
        "errors_and_cache": error_cache,
        "opaque_hashes": opaque_hashes,
        "health_complete": health_complete,
        "next_step": {
            "allow_k10_v2_complete_development_pages": allow_k10,
            "max_k10_v2_complete_development_pages": 5 if allow_k10 else 0,
            "page_scope": "K10_v2_complete_development" if allow_k10 else "none",
            "protocol": EXPECTED_RESPONSE_FORMAT if allow_k10 else "",
            "model": EXPECTED_MODEL if allow_k10 else "",
            "thinking_disabled": allow_k10,
            "extra_body_mode": "explicit_thinking_disabled" if allow_k10 else "none",
            "max_output_tokens": 400 if allow_k10 else 0,
            "per_page_provider_call_limit": 1 if allow_k10 else 0,
            "per_page_retry_limit": 0,
            "allow_stage_b_pilot": False,
            "allow_stage_c_pilot": False,
            "diagnostic_is_stage_b": False,
            "diagnostic_is_development": False,
            "must_remain_complete_only": True,
            "reason": "health_strict_complete_allow_k10_v2_complete_only" if allow_k10 else "health_gate_failed",
        },
        "scope": {
            "opaque_only": True,
            "provider_calls_by_audit": 0,
            "frozen_path_read_by_audit": False,
            "development_input_read_by_audit": False,
            "production_state_written_by_audit": False,
        },
    }
    output_hits = _sensitive_hits({"audit": report})
    if any(output_hits.values()):
        raise AuditError("audit_output_not_body_free")
    return report


def _human_rows(report: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
    checks = report.get("audit_checks") if isinstance(report.get("audit_checks"), Mapping) else {}
    for name, value in checks.items():
        yield {"check": str(name), "status": "pass" if value is True else "fail"}
    next_step = report.get("next_step") if isinstance(report.get("next_step"), Mapping) else {}
    yield {"check": "audit_status", "status": report.get("audit_status")}
    yield {"check": "artifact_status", "status": report.get("artifact_status")}
    yield {"check": "allow_k10_v2_complete_development_pages", "status": next_step.get("allow_k10_v2_complete_development_pages")}
    yield {"check": "max_k10_v2_complete_development_pages", "status": next_step.get("max_k10_v2_complete_development_pages")}
    yield {"check": "allow_stage_b_pilot", "status": next_step.get("allow_stage_b_pilot")}
    yield {"check": "allow_stage_c_pilot", "status": next_step.get("allow_stage_c_pilot")}


def write_audit(report: Mapping[str, Any], artifact_dir: Path) -> Tuple[Path, Path]:
    audit_dir = artifact_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary = audit_dir / "audit_summary.private.json"
    human = audit_dir / "human_audit.private.jsonl"
    summary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    human.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in _human_rows(report)),
        encoding="utf-8",
    )
    return summary, human


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Independent K13 protocol-health audit")
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args(argv)
    try:
        report = audit_artifact(args.artifact_dir)
        summary, human = write_audit(report, args.artifact_dir.resolve())
    except AuditError as exc:
        print(json.dumps({"audit_status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"audit_status": report["audit_status"], "summary": str(summary), "human": str(human)}, sort_keys=True))
    return 0 if report["audit_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
