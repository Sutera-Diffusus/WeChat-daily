"""Independent, body-free K12 audit for the Stage-A protocol diagnostic.

This file is intentionally a side-car audit.  It does not import the Stage-A
runner, a provider client, a frozen split, or a development input.  It reads
the diagnostic artifact, the existing body-free provider-capability matrix,
and only metadata from the current settings file.  No request, response,
message, or evidence body is ever emitted.

The diagnostic is a protocol gate, not a semantic result.  A ``complete``
diagnostic is eligible for the K11 five-page Stage-A run only when its strict
JSON parse, schema, and opaque handle checks all pass.  Stage B and Stage C
are never authorized by this audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "linear_stage_a_protocol_diagnostic_v1"
AUDIT_SCHEMA_VERSION = "linear_stage_a_protocol_diagnostic_k12_audit_v1"
DEFAULT_ARTIFACT_DIR = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / ARTIFACT_VERSION
)
CAPABILITY_DIR = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / "contextual_bundle_provider_capabilities_v1"
)
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "workbench_settings.json"

REQUIRED_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "ledger.private.jsonl",
    "errors.private.jsonl",
    "diagnostic.private.json",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Key based privacy checks are deliberately conservative.  Hashes, enum
# values, counts, and opaque handles remain reportable; body-bearing fields do
# not.  The audit never copies the value of a hit into its output.
BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "detail",
        "details",
        "error_message",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "output_text",
        "prompt",
        "quote",
        "raw",
        "raw_output",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "redacted_text",
        "response",
        "response_body",
        "summary",
        "system_prompt",
        "text",
        "text_body",
        "text_redacted",
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
REASONING_KEYS = frozenset(
    {
        "analysis",
        "chain_of_thought",
        "completion",
        "thoughts",
    }
)
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
        "call_count",
        "calls",
        "diagnostic_call_count",
        "health_call_count",
        "provider_attempts",
        "provider_call_count",
        "provider_calls",
        "provider_request_attempts",
        "successful_provider_calls",
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
    }
)
SCHEMA_KEYS = frozenset(
    {
        "schema",
        "schema_ok",
        "schema_status",
        "schema_valid",
        "schema_version",
        "wire_schema",
        "wire_schema_valid",
    }
)
STRICT_KEYS = frozenset(
    {
        "strict_json",
        "strict_json_parse",
        "strict_parse",
        "strict_parse_ok",
        "strict_schema_parse",
        "strict_result",
    }
)
CANDIDATE_KEYS = frozenset(
    {
        "candidate",
        "candidate_payload",
        "diagnostic_candidate",
        "parsed_candidate",
    }
)
VISION_MARKERS = ("vision", "multimodal", "image", "audio", "video")
EXPECTED_SCHEMA_VERSIONS = frozenset(
    {
        "linear_stage_a_protocol_diagnostic_report_v1",
        "linear_stage_a_protocol_diagnostic_runner_v1",
    }
)


class AuditError(ValueError):
    """Fail-closed error for malformed or out-of-scope K12 input."""


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
    for line_number, line in enumerate(lines, 1):
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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], (), {})


def _key_flags(key: str) -> Tuple[bool, bool, bool]:
    lowered = str(key).casefold()
    body = lowered in BODY_KEYS or lowered.endswith(("_body", "_text", "_transcript"))
    secret = lowered in SECRET_KEYS or lowered.endswith(("_secret", "_token", "_password"))
    reasoning = lowered in REASONING_KEYS or lowered.endswith(("_reasoning", "_thoughts"))
    return body, secret, reasoning


def _sensitive_hits(value: Any, path: str = "") -> Dict[str, List[str]]:
    hits: Dict[str, List[str]] = {"body": [], "secret": [], "reasoning": []}
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            body, secret, reasoning = _key_flags(key)
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


def _iter_nodes(value: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            yield from _iter_nodes(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _iter_nodes(child, "%s[%d]" % (path, index))


def _key_values(value: Any, keys: Iterable[str]) -> List[Tuple[str, Any]]:
    wanted = {str(key).casefold() for key in keys}
    return [
        (path, node)
        for path, node in _iter_nodes(value)
        if path.rsplit(".", 1)[-1].casefold() in wanted
    ]


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_scalar(value: Any, *, max_length: int = 240) -> bool:
    return (
        isinstance(value, (str, int, float, bool))
        and not isinstance(value, float) or isinstance(value, str)
    ) and (not isinstance(value, str) or (0 < len(value) <= max_length and "\n" not in value and "\r" not in value))


def _safe_path(path: Path, expected_name: str) -> Path:
    resolved = path.resolve()
    if resolved.name != expected_name:
        raise AuditError("wrong_artifact_directory")
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in resolved.parts):
        raise AuditError("frozen_path_refused")
    return resolved


def _handle_parts(value: Any) -> Optional[Tuple[str, str, str, str, str]]:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        return None
    pieces = value.split("|")
    if len(pieces) != 3 or not all(pieces):
        return None
    scope, kind, identifier = pieces
    if "/" not in scope:
        return None
    account, chat = scope.split("/", 1)
    if not account or not chat or kind not in {"message", "candidate", "evidence"}:
        return None
    return account, chat, kind, identifier, scope


def _validate_handles(documents: Mapping[str, Any]) -> Dict[str, Any]:
    total = 0
    valid = 0
    invalid = 0
    scope_pairs: set[Tuple[str, str]] = set()
    kind_counts = {"message": 0, "candidate": 0, "evidence": 0}
    field_counts = {"message": 0, "candidate": 0, "evidence": 0}
    malformed_paths: List[str] = []
    for document in documents.values():
        for path, value in _iter_nodes(document):
            key = path.rsplit(".", 1)[-1].casefold()
            if key not in HANDLE_KEYS:
                continue
            values = value if isinstance(value, list) else [value]
            expected_kind = ""
            if "message" in key:
                expected_kind = "message"
            elif "candidate" in key:
                expected_kind = "candidate"
            elif "evidence" in key:
                expected_kind = "evidence"
            for item in values:
                total += 1
                parts = _handle_parts(item)
                if parts is None or (expected_kind and parts[2] != expected_kind):
                    invalid += 1
                    malformed_paths.append(path)
                    continue
                valid += 1
                account, chat, kind, _identifier, _scope = parts
                scope_pairs.add((account, chat))
                kind_counts[kind] += 1
                field_counts[expected_kind or kind] += 1
    # A diagnostic may include one handle in both a candidate and ledger view;
    # duplicate values are not an error.  Every observed value must still be a
    # typed, scoped opaque handle.
    return {
        "observed_count": total,
        "valid_count": valid,
        "invalid_count": invalid,
        "format_ok": bool(total and valid == total),
        "scope_count": len(scope_pairs),
        "scope_consistent": len(scope_pairs) == 1,
        "kind_counts": kind_counts,
        "field_counts": field_counts,
        "malformed_count": len(malformed_paths),
    }


def _schema_check(documents: Mapping[str, Any]) -> Dict[str, Any]:
    values: List[Any] = []
    explicit: List[bool] = []
    for document in documents.values():
        for path, value in _key_values(document, SCHEMA_KEYS):
            key = path.rsplit(".", 1)[-1].casefold()
            if key in {"schema_ok", "schema_valid", "wire_schema_valid"}:
                parsed = _as_bool(value)
                if parsed is not None:
                    explicit.append(parsed)
            elif key in {"schema_status", "schema"}:
                if isinstance(value, str):
                    explicit.append(value.casefold() in {"pass", "passed", "complete", "valid", "ok"})
            elif key in {"schema_version", "wire_schema"} and _safe_scalar(value):
                values.append(str(value))
    unique = sorted(set(values))
    return {
        "schema_versions_observed": len(unique),
        "schema_versions": unique,
        "schema_versions_shape_ok": bool(values),
        "schema_versions_expected": EXPECTED_SCHEMA_VERSIONS <= set(unique),
        "schema_explicit_checks": len(explicit),
        "schema_explicit_ok": bool(explicit) and all(explicit),
        "schema_valid": bool(values)
        and EXPECTED_SCHEMA_VERSIONS <= set(unique)
        and (not explicit or all(explicit)),
    }


def _strict_parse_check(diagnostic: Mapping[str, Any]) -> Dict[str, Any]:
    found: List[Tuple[str, Any]] = []
    for path, value in _key_values(diagnostic, STRICT_KEYS):
        found.append((path, value))
    statuses: List[bool] = []
    strict_complete_values: List[bool] = []
    parse_codes: List[str] = []
    for _path, value in found:
        if isinstance(value, Mapping):
            if "strict_complete" in value:
                complete = _as_bool(value.get("strict_complete"))
                if complete is not None:
                    strict_complete_values.append(complete)
            parse_code = value.get("parse_code")
            if isinstance(parse_code, str) and parse_code:
                parse_codes.append(parse_code)
            parsed = None
            for key in ("ok", "valid", "complete", "passed"):
                if key in value:
                    parsed = _as_bool(value.get(key))
                    if parsed is not None:
                        break
            if parsed is None:
                status = str(value.get("status") or value.get("result") or "").casefold()
                parsed = status in {"pass", "passed", "complete", "valid", "ok"}
            statuses.append(bool(parsed))
        else:
            parsed = _as_bool(value)
            if parsed is not None:
                statuses.append(parsed)
            elif isinstance(value, str):
                statuses.append(value.casefold() in {"pass", "passed", "complete", "valid", "ok"})
    return {
        "observed": bool(found),
        "record_count": len(found),
        "strict_parse": bool(statuses) and all(statuses),
        "strict_parse_values": sum(1 for value in statuses if value),
        "strict_parse_failures": sum(1 for value in statuses if not value),
        "strict_complete_values": strict_complete_values,
        "parse_codes": sorted(set(parse_codes)),
        # A blocked protocol run is still auditable when the strict result is
        # explicitly recorded as incomplete.  This is intentionally separate
        # from ``strict_parse``: the latter is the gate for Stage A release.
        "recorded_consistently": bool(found)
        and bool(strict_complete_values or statuses)
        and not (strict_complete_values and any(strict_complete_values) and not all(statuses)),
    }


def _candidate_separation_check(diagnostic: Mapping[str, Any]) -> Dict[str, Any]:
    strict_paths = [path for path, _value in _key_values(diagnostic, STRICT_KEYS)]
    candidate_paths = [path for path, _value in _key_values(diagnostic, CANDIDATE_KEYS)]
    nested = False
    for strict_path in strict_paths:
        for candidate_path in candidate_paths:
            if candidate_path.startswith(strict_path + ".") or strict_path.startswith(candidate_path + "."):
                nested = True
    candidates = [value for _path, value in _key_values(diagnostic, CANDIDATE_KEYS)]
    candidate_mapping_count = sum(isinstance(value, Mapping) for value in candidates)
    candidate_body_hits = _sensitive_hits({"candidates": candidates})
    return {
        "candidate_observed": bool(candidate_paths),
        "candidate_record_count": len(candidate_paths),
        "candidate_mapping_count": candidate_mapping_count,
        "strict_parse_candidate_nested": nested,
        "candidate_body_free": not any(candidate_body_hits.values()),
        "separate": bool(candidate_paths) and bool(strict_paths) and not nested,
    }


def _provider_records(documents: Mapping[str, Any]) -> Dict[str, List[str]]:
    model: List[str] = []
    source: List[str] = []
    response_format: List[str] = []
    for document in documents.values():
        for path, value in _key_values(document, {"model", "model_id", "selected_model"}):
            if isinstance(value, str) and value.strip() and len(value.strip()) <= 240:
                model.append(value.strip())
        for _path, value in _key_values(document, {"source", "provider_source"}):
            if isinstance(value, str) and value.strip() and len(value.strip()) <= 240:
                source.append(value.strip())
        for _path, value in _key_values(document, {"response_format_mode", "protocol", "response_format"}):
            if isinstance(value, str) and value.strip() and len(value.strip()) <= 120:
                response_format.append(value.strip())
            elif isinstance(value, Mapping):
                mode = value.get("type", value.get("mode"))
                if isinstance(mode, str) and mode.strip():
                    response_format.append(mode.strip())
    return {
        "models": sorted(set(model)),
        "sources": sorted(set(source)),
        "response_formats": sorted(set(response_format)),
    }


def _load_capability_evidence(selected_model: str, capability_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "artifact_present": False,
        "candidate_present": False,
        "candidate_eligible": False,
        "nonvision_hints": False,
        "text_likely": False,
        "evidence_source": "none",
        "health_confirmed": False,
    }
    # Prefer the original v1 model-list matrix, then enrich it with the
    # already-reviewed v2.6 semantic-frame capability probe.  Both artifacts
    # are body-free metadata; no provider or development input is opened.
    candidate_files = (
        (capability_dir / "provider_capabilities.private.json", capability_dir / "manifest.private.json"),
        (
            capability_dir.parent / "contextual_bundle_provider_semantic_frame_bundle_v2_6" / "provider_capabilities.private.json",
            capability_dir.parent / "contextual_bundle_provider_semantic_frame_bundle_v2_6" / "manifest.private.json",
        ),
    )
    for capability_path, manifest_path in candidate_files:
        if not capability_path.is_file() or not manifest_path.is_file():
            continue
        try:
            capability = _load_json(capability_path)
            manifest = _load_json(manifest_path)
        except AuditError:
            continue
        result["artifact_present"] = True
        candidates = capability.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        rows = [
            row
            for row in candidates
            if isinstance(row, Mapping) and str(row.get("model_id") or "") == selected_model
        ]
        if not rows and str(capability.get("selected_model") or "") == selected_model:
            selected = capability.get("selected_health")
            rows = [
                {
                    "model_id": selected_model,
                    "eligible": True,
                    "health_ok": isinstance(selected, Mapping) and selected.get("ok") is True,
                    "json_object_confirmed": isinstance(selected, Mapping) and selected.get("ok") is True,
                    "capability_hints": {
                        "text_likely": True,
                        "vision_like": False,
                        "vision_exp": False,
                        "source": "selected_health_probe",
                    },
                }
            ]
        if not rows:
            continue
        row = rows[0]
        hints = row.get("capability_hints") if isinstance(row.get("capability_hints"), Mapping) else {}
        result["candidate_present"] = True
        result["candidate_eligible"] = row.get("eligible") is True or row.get("health_ok") is True
        result["text_likely"] = hints.get("text_likely") is True or row.get("health_ok") is True
        result["nonvision_hints"] = (
            (hints.get("vision_like") is False and hints.get("vision_exp") is False)
            or (not hints and not any(marker in selected_model.casefold() for marker in VISION_MARKERS))
        )
        result["evidence_source"] = str(hints.get("source") or manifest.get("protocol") or "unknown")[:80]
        result["health_confirmed"] = result["health_confirmed"] or (
            row.get("health_ok") is True and (row.get("json_object_confirmed") is not False)
        )
        result["settings_source_matches"] = manifest.get("settings_source") in {
            "workbench_settings",
            "workbench_settings+explicit_thinking_disabled",
        }
        if result["health_confirmed"]:
            break
    return result


def _redacted_settings_digest(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (digest, configured model), never the settings payload."""
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(value, Mapping):
        return None, None

    def redact(item: Any) -> Any:
        if isinstance(item, Mapping):
            output: Dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                lowered = key.casefold()
                if lowered in SECRET_KEYS or lowered.endswith(("_secret", "_token", "_password", "_key")):
                    output[key] = "<configured>" if _non_empty(child) else ""
                else:
                    output[key] = redact(child)
            return output
        if isinstance(item, (list, tuple)):
            return [redact(child) for child in item]
        return item

    ai = value.get("ai") if isinstance(value.get("ai"), Mapping) else {}
    model = ai.get("model") if isinstance(ai, Mapping) else None
    model_text = str(model).strip() if isinstance(model, str) and model.strip() else None
    digest = hashlib.sha256(_canonical(redact(value)).encode("utf-8")).hexdigest()
    return digest, model_text


def _settings_unchanged(documents: Mapping[str, Any], settings_path: Path) -> Dict[str, Any]:
    values: List[bool] = []
    before: List[str] = []
    after: List[str] = []
    for document in documents.values():
        for path, value in _key_values(document, {"settings_unchanged", "settings_mutated", "settings_written"}):
            key = path.rsplit(".", 1)[-1].casefold()
            parsed = _as_bool(value)
            if parsed is not None:
                values.append((not parsed) if key in {"settings_mutated", "settings_written"} else parsed)
        for path, value in _key_values(document, {"settings_before_sha256", "settings_before_hash"}):
            if isinstance(value, str) and HEX64.fullmatch(value):
                before.append(value)
        for path, value in _key_values(document, {"settings_after_sha256", "settings_after_hash", "settings_sha256"}):
            if isinstance(value, str) and HEX64.fullmatch(value):
                after.append(value)
    current_digest, current_model = _redacted_settings_digest(settings_path)
    baseline_models: List[str] = []
    override_models: List[str] = []
    for document in documents.values():
        for _path, value in _key_values(document, {"k11_model", "configured_model", "settings_model"}):
            if isinstance(value, str) and value.strip():
                baseline_models.append(value.strip())
        for _path, value in _key_values(document, {"fallback_model", "selected_model", "override_model"}):
            if isinstance(value, str) and value.strip():
                override_models.append(value.strip())
    digest_match = bool(before and after and set(before) == set(after))
    current_match = bool(current_digest and after and current_digest in set(after))
    explicit = bool(values) and all(values)
    # K12 intentionally overrides the model in memory.  The prior K11 model
    # and the current settings model matching, while the fallback model differs,
    # is an independent body-free indication that settings were not rewritten.
    model_override_only = bool(
        current_model
        and current_model in set(baseline_models)
        and any(model != current_model for model in override_models)
    )
    unchanged = bool(explicit or digest_match or current_match or model_override_only)
    return {
        "settings_file_present": current_digest is not None,
        "settings_model_present": bool(current_model),
        "settings_unchanged_evidence": unchanged,
        "explicit_unchanged": explicit,
        "before_after_hash_match": digest_match,
        "current_hash_matches_recorded": current_match,
        "model_override_only": model_override_only,
        "settings_hash_shape_ok": bool(before or after or current_digest),
        "configured_model_is_nonvision": bool(current_model and not any(marker in current_model.casefold() for marker in VISION_MARKERS)),
    }


def _call_counts(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    observed: Dict[str, List[float]] = {}
    for name, document in documents.items():
        for path, value in _key_values(document, CALL_KEYS):
            number = _as_number(value)
            if number is not None:
                observed.setdefault(path, []).append(number)
    ledger_provider_calls = sum(row.get("provider_call") is True for row in ledger)
    all_values = [number for values in observed.values() for number in values]
    if ledger:
        all_values.append(float(ledger_provider_calls))
    maximum = max(all_values) if all_values else 0.0
    return {
        "observed_fields": len(observed),
        "provider_call_count_max": int(maximum) if maximum.is_integer() else maximum,
        "ledger_rows": len(ledger),
        "ledger_provider_calls": ledger_provider_calls,
        "strict_at_most_one": maximum <= 1.0,
        "ledger_one_or_zero": ledger_provider_calls <= 1,
    }


def _retry_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    violations: List[str] = []
    observed = 0
    for name, document in documents.items():
        for path, value in _key_values(document, RETRY_KEYS):
            parsed_bool = _as_bool(value)
            number = _as_number(value)
            if parsed_bool is not None:
                observed += 1
                if parsed_bool and path.rsplit(".", 1)[-1].casefold().startswith("retry"):
                    violations.append(path)
            elif number is not None:
                observed += 1
                key = path.rsplit(".", 1)[-1].casefold()
                allowed_initial_attempt = key in {"attempt", "attempt_number", "attempts"} and number <= 1
                if number > 0 and not allowed_initial_attempt:
                    violations.append(path)
            elif _non_empty(value):
                violations.append(path)
    for index, row in enumerate(ledger):
        if row.get("retry") is True or _as_number(row.get("retry_count")) not in (None, 0.0):
            violations.append("ledger[%d]" % index)
    return {
        "retry_fields_observed": observed,
        "retry_violations": len(violations),
        "retry_free": not violations,
    }


def _development_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    violations: List[str] = []
    dev_calls = 0.0
    read_flags: List[bool] = []
    for name, document in documents.items():
        for path, value in _key_values(document, DEV_KEYS):
            key = path.rsplit(".", 1)[-1].casefold()
            if key in {"development_input_read", "development_read"}:
                parsed = _as_bool(value)
                if parsed is not None:
                    read_flags.append(parsed)
                    if parsed:
                        violations.append(path)
            else:
                number = _as_number(value)
                if number is not None:
                    dev_calls += number
                    if number != 0:
                        violations.append(path)
        for path, value in _key_values(document, {"phase"}):
            if isinstance(value, str) and "development" in value.casefold():
                violations.append(path)
    for index, row in enumerate(ledger):
        if "development" in str(row.get("phase") or "").casefold():
            violations.append("ledger[%d].phase" % index)
    return {
        "development_calls": int(dev_calls) if dev_calls.is_integer() else dev_calls,
        "development_read_flags": read_flags,
        "development_violations": len(violations),
        "development_free": not violations and dev_calls == 0 and not any(read_flags),
    }


def _manifest_check(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    status = manifest.get("status")
    return {
        "artifact_version": manifest.get("artifact_version") == ARTIFACT_VERSION,
        "local_day": manifest.get("local_day") == LOCAL_DAY,
        "diagnostic_only": manifest.get("diagnostic_only") is not False,
        "frozen_unread": manifest.get("frozen_read") is False,
        "development_unread": manifest.get("development_input_read") is False,
        "production_state_not_written": manifest.get("production_state_written") is False,
        "status_recorded": isinstance(status, str) and bool(status),
        "status_valid": isinstance(status, str)
        and status.casefold() in {"blocked", "complete", "partial"},
    }


def _hash_check(artifact_dir: Path, manifest: Mapping[str, Any], actual_files: Sequence[str]) -> Dict[str, Any]:
    recorded = manifest.get("artifact_hashes")
    if not isinstance(recorded, Mapping):
        return {"recorded": False, "all_match": False, "match_count": 0, "hashed_file_count": 0}
    expected = {name for name in actual_files if name != "manifest.private.json"}
    rows: List[Dict[str, Any]] = []
    for name in sorted(expected):
        value = str(recorded.get(name) or "")
        computed = _sha256_file(artifact_dir / name)
        rows.append({"file": name, "matches": bool(HEX64.fullmatch(value) and value == computed)})
    return {
        "recorded": True,
        "all_match": bool(set(str(key) for key in recorded) == expected and rows and all(row["matches"] for row in rows)),
        "match_count": sum(bool(row["matches"]) for row in rows),
        "hashed_file_count": len(rows),
    }


def audit_artifact(artifact_dir: Path) -> Dict[str, Any]:
    artifact_dir = _safe_path(artifact_dir, ARTIFACT_VERSION)
    if not artifact_dir.is_dir():
        raise AuditError("artifact_directory_missing")
    actual = sorted(path.name for path in artifact_dir.iterdir() if path.is_file())
    missing = sorted(set(REQUIRED_FILES) - set(actual))
    if missing:
        raise AuditError("required_file_missing:%s" % missing[0])
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
        "ledger": ledger,
        "errors": errors,
    }
    all_hits = {"body": [], "secret": [], "reasoning": []}
    for name, document in documents.items():
        found = _sensitive_hits(document, name)
        for key in all_hits:
            all_hits[key].extend(found[key])
    body_free = not any(all_hits.values())
    handles = _validate_handles(documents)
    schema = _schema_check(documents)
    strict = _strict_parse_check(diagnostic)
    candidate = _candidate_separation_check(diagnostic)
    provider = _provider_records(documents)
    selected_models = provider["models"]
    selected_model = selected_models[0] if len(selected_models) == 1 else ""
    nonvision = bool(selected_model and not any(marker in selected_model.casefold() for marker in VISION_MARKERS))
    cap = _load_capability_evidence(selected_model, CAPABILITY_DIR) if nonvision else {
        "artifact_present": False,
        "candidate_present": False,
        "candidate_eligible": False,
        "nonvision_hints": False,
        "text_likely": False,
        "evidence_source": "none",
        "health_confirmed": False,
    }
    settings = _settings_unchanged(documents, SETTINGS_PATH)
    calls = _call_counts(documents, ledger)
    retry = _retry_check(documents, ledger)
    development = _development_check(documents, ledger)
    manifest_shape = _manifest_check(manifest)
    hash_result = _hash_check(artifact_dir, manifest, actual)
    errors_codes = sorted(
        str(row.get("error_code") or row.get("code") or "")
        for row in errors
        if row.get("error_code") or row.get("code")
    )
    status = str(manifest.get("status") or aggregate.get("status") or diagnostic.get("status") or "").casefold()
    status_complete = status == "complete"
    response_format_values = {value.casefold() for value in provider["response_formats"]}
    response_format_ok = bool(
        response_format_values
        and response_format_values <= {"json_object", "json", "tool", "function", "function_call", "omitted"}
    )
    if "omitted" in response_format_values:
        # The selected non-vision model has an existing successful compact
        # health probe with response_format_sent=false.  ``omitted`` is a
        # deliberate protocol mode here, not an accidental missing field.
        response_format_ok = bool(response_format_ok and cap.get("health_confirmed"))
    model_capability_ok = bool(
        nonvision
        and cap.get("artifact_present")
        and cap.get("candidate_present")
        and cap.get("candidate_eligible")
        and cap.get("nonvision_hints")
        and cap.get("text_likely")
    )
    schema_handles_ok = bool(
        schema["schema_valid"] and handles["format_ok"] and handles["scope_consistent"]
    )
    strict_complete = bool(
        status_complete
        and strict["recorded_consistently"]
        and candidate["separate"]
        and candidate["candidate_body_free"]
        and schema_handles_ok
        and body_free
        and calls["strict_at_most_one"]
        and retry["retry_free"]
        and development["development_free"]
        and model_capability_ok
        and settings["settings_unchanged_evidence"]
        and response_format_ok
    )
    preconditions_for_retry = bool(
        nonvision and model_capability_ok and settings["settings_unchanged_evidence"] and response_format_ok and development["development_free"]
    )
    allow_stage_a = strict_complete
    allow_protocol_health = bool(not strict_complete and preconditions_for_retry)
    if allow_stage_a:
        next_reason = "strict_protocol_complete"
    elif allow_protocol_health:
        next_reason = "repeat_one_synthetic_health_only"
    elif not nonvision or not model_capability_ok:
        next_reason = "nonvision_capability_evidence_missing"
    elif not settings["settings_unchanged_evidence"]:
        next_reason = "settings_unchanged_unverified"
    else:
        next_reason = "diagnostic_incomplete_or_invalid"
    audit_checks = {
        "files_present_and_hashes": bool(not missing and hash_result["all_match"]),
        "manifest_shape": all(manifest_shape.values()),
        "real_calls_at_most_one": calls["strict_at_most_one"],
        "no_retry": retry["retry_free"],
        "no_development": development["development_free"],
        "nonvision_model": nonvision,
        "capability_evidence": model_capability_ok,
        "settings_unchanged": settings["settings_unchanged_evidence"],
        "source_body_free": body_free,
        "strict_result_recorded": strict["recorded_consistently"],
        "diagnostic_candidate_separate": candidate["separate"],
        "diagnostic_candidate_body_free": candidate["candidate_body_free"],
        "schema_and_handles_valid": schema_handles_ok,
        "response_format_protocol": response_format_ok,
    }
    audit_pass = all(audit_checks.values())
    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_status": status or "unknown",
        "audit_status": "pass" if audit_pass else "fail",
        "audit_checks": audit_checks,
        "missing_files": missing,
        "unexpected_root_files": sorted(set(actual) - set(REQUIRED_FILES)),
        "hashes": hash_result,
        "body_free": body_free,
        "body_field_hits": len(all_hits["body"]),
        "secret_field_hits": len(all_hits["secret"]),
        "reasoning_field_hits": len(all_hits["reasoning"]),
        "call_counts": calls,
        "retry": retry,
        "development": development,
        "provider": {
            "model": selected_model,
            "model_count": len(selected_models),
            "nonvision": nonvision,
            "source_count": len(provider["sources"]),
            "response_formats": provider["response_formats"],
            "response_format_ok": response_format_ok,
        },
        "capability_evidence": cap,
        "settings": settings,
        "strict_parse": strict,
        "strict_complete": strict_complete,
        "diagnostic_candidate": candidate,
        "schema": schema,
        "handles": handles,
        "error_codes": errors_codes,
        "next_step": {
            "allow_one_protocol_health": allow_protocol_health,
            "protocol_health_call_count": 1 if allow_protocol_health else 0,
            "protocol_health_scope": "synthetic_health_only" if allow_protocol_health else "none",
            "model": selected_model if (allow_protocol_health or allow_stage_a) else "",
            "protocol": provider["response_formats"][0] if (allow_protocol_health or allow_stage_a) and provider["response_formats"] else "",
            "extra_body_mode": "explicit_thinking_disabled"
            if (allow_protocol_health or allow_stage_a)
            else "none",
            "thinking_disabled": bool(allow_protocol_health or allow_stage_a),
            "max_output_tokens": 400 if (allow_protocol_health or allow_stage_a) else 0,
            "allow_k11_stage_a_five_pages": allow_stage_a,
            "allow_stage_b_pilot": False,
            "allow_stage_c_pilot": False,
            "diagnostic_is_stage_b": False,
            "diagnostic_must_not_read_development_input": True,
            "reason": next_reason,
        },
        "scope": {
            "opaque_only": True,
            "provider_calls_by_audit": 0,
            "frozen_path_read_by_audit": False,
            "development_input_read_by_audit": False,
            "production_state_written_by_audit": False,
        },
    }
    report_hits = _sensitive_hits(report)
    if any(report_hits.values()):
        raise AuditError("audit_output_not_body_free")
    return report


def _human_rows(report: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
    checks = report.get("audit_checks", {})
    if isinstance(checks, Mapping):
        for name, value in checks.items():
            yield {"check": str(name), "status": "pass" if value is True else "fail"}
    next_step = report.get("next_step") if isinstance(report.get("next_step"), Mapping) else {}
    yield {"check": "audit_status", "status": report.get("audit_status")}
    yield {"check": "artifact_status", "status": report.get("artifact_status")}
    yield {"check": "allow_one_protocol_health", "status": next_step.get("allow_one_protocol_health")}
    yield {"check": "allow_k11_stage_a_five_pages", "status": next_step.get("allow_k11_stage_a_five_pages")}
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
    parser = argparse.ArgumentParser(description="Independent K12 Stage-A protocol diagnostic audit")
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args(argv)
    report = audit_artifact(args.artifact_dir)
    summary, human = write_audit(report, args.artifact_dir.resolve())
    print(json.dumps({"audit_status": report["audit_status"], "summary": str(summary), "human": str(human)}, sort_keys=True))
    return 0 if report["audit_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
