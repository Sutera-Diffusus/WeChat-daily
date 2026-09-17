"""Independent K21 audit of the compact Stage-A health-v2 artifact.

This is a read-only, side-car audit.  It reads only the body-free artifact
ledger and aggregate files, plus the raw-byte workbench-settings digest and
the durable authorization database in read-only mode.  It never imports a
runner or provider, never opens development/frozen input, and never makes a
provider call.  ``audit_status=pass`` means that the *blocked* health artifact
is internally consistent; it does not authorize development or production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "compact_stage_a_protocol_health_v2"
DEFAULT_ARTIFACT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"
AUTHORITY_ROOT = ROOT / ".runtime" / "compact-stage-a-authorizations"

AUDIT_SCHEMA_VERSION = "compact_stage_a_protocol_health_v2_k21_audit_v1"
EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROVIDER = "openai-compatible"
EXPECTED_SOURCE = "deepseek-openai-compatible"
EXPECTED_PROTOCOL = "stage_a_topic_assignment_compact_v2"
EXPECTED_PROMPT = "stage_a_topic_assignment_compact_prompt_v2"
EXPECTED_RESPONSE_FORMAT = "omitted"
EXPECTED_AUTHORIZATION = "K20_COMPACT_STAGE_A_HEALTH_V2"
EXPECTED_NAMESPACE = "compact-stage-a-health-v1"
EXPECTED_REPORT_SCHEMA = "compact_stage_a_protocol_health_report_v2"
EXPECTED_RUNNER_SCHEMA = "compact_stage_a_protocol_health_runner_v2"
EXPECTED_LEDGER_SCHEMA = "call_authorization_ledger_v1"
EXPECTED_RULE = "primary_count=#h(m,p)>=1; topic_count<=primary_count; each t.p nonempty; primary exactly once"
MAX_CALLS = 1
MAX_RETRIES = 0
MAX_INPUT_TOKEN_PROXY = 1600
MAX_OUTPUT_TOKENS = 400
MAX_LATENCY_MS = 30_000.0

REQUIRED_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "diagnostic.private.json",
    "ledger.private.jsonl",
    "errors.private.jsonl",
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_PATH_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})
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
        "message",
        "message_text",
        "messages",
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
        "response",
        "response_body",
        "response_text",
        "summary",
        "system_prompt",
        "text",
        "text_body",
        "thoughts",
        "transcript",
        "user_input",
        "user_packet",
    }
)
SECRET_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "password", "private_key", "secret", "secrets"}
)
IDENTITY_KEYS = frozenset(
    {"account_id", "chat_id", "contact_id", "display_name", "email", "person_id", "phone", "speaker", "user_id"}
)
REASONING_KEYS = frozenset({"analysis", "chain_of_thought", "reasoning", "reasoning_content", "thoughts"})


class AuditError(ValueError):
    """Body-free K21 audit failure."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical(value).encode("utf-8"))


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
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError("invalid_jsonl:%s:%d" % (path.name, number)) from exc
        if not isinstance(value, Mapping):
            raise AuditError("jsonl_object_required:%s:%d" % (path.name, number))
        rows.append({str(key): child for key, child in value.items()})
    return rows


def _walk(value: Any) -> Iterator[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _privacy_hits(documents: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    hits = {"body": 0, "identity": 0, "reasoning": 0, "secret": 0}
    value: Dict[str, Any] = dict(documents)
    value["ledger_rows"] = list(rows)
    for key, child in _walk(value):
        normalized = key.casefold()
        if normalized in BODY_KEYS and child not in (None, "", [], {}, ()):
            hits["body"] += 1
        if normalized in IDENTITY_KEYS:
            hits["identity"] += 1
        if normalized in REASONING_KEYS:
            hits["reasoning"] += 1
        if normalized in SECRET_KEYS or normalized.endswith(("_secret", "_password", "_api_key")):
            hits["secret"] += 1
    return hits


def _safe_artifact_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_root = (ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY).resolve()
    if resolved.name != ARTIFACT_VERSION or resolved.parent != expected_root:
        raise AuditError("out_of_scope_artifact_directory")
    if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in resolved.parts):
        raise AuditError("forbidden_artifact_path")
    return resolved


def _artifact_hash_check(artifact_dir: Path, manifest: Mapping[str, Any], actual: Sequence[str]) -> Dict[str, Any]:
    recorded = manifest.get("artifact_hashes")
    expected = {name for name in actual if name != "manifest.private.json"}
    if not isinstance(recorded, Mapping):
        return {"recorded": False, "all_match": False, "hashed_file_count": 0, "match_count": 0}
    keys = {str(key) for key in recorded}
    matches = 0
    for filename in sorted(expected):
        expected_hash = str(recorded.get(filename) or "")
        try:
            actual_hash = _sha256_bytes((artifact_dir / filename).read_bytes())
        except OSError:
            actual_hash = ""
        if HEX64.fullmatch(expected_hash) and expected_hash == actual_hash:
            matches += 1
    return {
        "recorded": True,
        "all_match": keys == expected and matches == len(expected) and bool(expected),
        "hashed_file_count": len(expected),
        "match_count": matches,
    }


def _find_values(value: Any, wanted: Iterable[str]) -> List[Any]:
    names = {str(name).casefold() for name in wanted}
    return [child for key, child in _walk(value) if key.casefold() in names]


def _settings_check(documents: Mapping[str, Any]) -> Dict[str, Any]:
    before: List[str] = []
    after: List[str] = []
    flags: List[bool] = []
    for document in documents.values():
        before.extend(str(value) for value in _find_values(document, {"settings_before_sha256"}) if isinstance(value, str))
        after.extend(str(value) for value in _find_values(document, {"settings_after_sha256"}) if isinstance(value, str))
        flags.extend(value for value in _find_values(document, {"settings_unchanged"}) if isinstance(value, bool))
    manifest = documents.get("manifest", {})
    ledger = manifest.get("authorization_ledger") if isinstance(manifest, Mapping) else None
    binding = ledger.get("binding") if isinstance(ledger, Mapping) and isinstance(ledger.get("binding"), Mapping) else {}
    binding_hash = binding.get("settings_sha256") if isinstance(binding, Mapping) else None
    if isinstance(binding_hash, str):
        before.append(binding_hash)
        after.append(binding_hash)
    valid_before = [value for value in before if HEX64.fullmatch(value)]
    valid_after = [value for value in after if HEX64.fullmatch(value)]
    try:
        current_hash = _sha256_bytes(SETTINGS_PATH.read_bytes())
        current_read = True
    except OSError:
        current_hash = ""
        current_read = False
    expected = valid_before[0] if valid_before else ""
    return {
        "settings_hash_shape_ok": bool(before and after and len(valid_before) == len(before) and len(valid_after) == len(after)),
        "before_after_equal": bool(valid_before and valid_before == valid_after),
        "explicit_unchanged_flags": bool(flags) and all(flags),
        "current_file_read_as_raw_bytes": current_read,
        "current_file_hash_matches": bool(current_hash and expected and current_hash == expected),
        "durable_binding_matches": bool(binding_hash and expected and binding_hash == expected),
        "settings_unchanged": bool(
            valid_before
            and valid_after
            and valid_before == valid_after
            and flags
            and all(flags)
            and current_read
            and current_hash == expected
            and binding_hash == expected
        ),
        "hash_basis": "raw_workbench_settings_bytes",
    }


def _durable_ledger_check(manifest: Mapping[str, Any], ledger_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    authorization = manifest.get("authorization") if isinstance(manifest.get("authorization"), Mapping) else {}
    auth_id = str(authorization.get("authorization_id") or "")
    db_path = AUTHORITY_ROOT / ("authorization-%s.sqlite3" % _sha256_bytes(auth_id.encode("utf-8"))[:32])
    result: Dict[str, Any] = {
        "authorization_id": auth_id,
        "database_present": db_path.is_file(),
        "read_only": True,
        "max_calls": None,
        "calls_reserved": None,
        "reservation_rows": 0,
        "rejection_rows": 0,
        "status_counts": {},
        "binding_match": False,
        "reservation_match": False,
        "ledger_rows_hash_match": False,
        "authorization_hash_match": False,
        "global_one_of_one": False,
    }
    if not db_path.is_file():
        return result
    try:
        uri = "file:%s?mode=ro" % db_path.as_posix()
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        auth_row = connection.execute(
            "SELECT authorization_id,binding_sha256,max_calls,provider,model,protocol,settings_sha256,scope_sha256,input_sha256,artifact_namespace,calls_reserved FROM authorizations WHERE authorization_id = ?",
            (auth_id,),
        ).fetchone()
        reservations = [
            dict(row)
            for row in connection.execute(
                "SELECT reservation_id,authorization_id,ordinal,request_sha256,unit_ref_sha256,attempt,provider,model,protocol,status,error_code,input_tokens,output_tokens,latency_ms FROM reservations WHERE authorization_id = ? ORDER BY ordinal",
                (auth_id,),
            ).fetchall()
        ]
        rejection_count = int(connection.execute("SELECT COUNT(*) FROM rejections WHERE authorization_id = ?", (auth_id,)).fetchone()[0])
        connection.close()
    except (OSError, sqlite3.Error):
        return result
    if auth_row is None:
        return result
    auth = dict(auth_row)
    result.update(
        {
            "max_calls": auth.get("max_calls"),
            "calls_reserved": auth.get("calls_reserved"),
            "reservation_rows": len(reservations),
            "rejection_rows": rejection_count,
            "status_counts": {
                status: sum(1 for row in reservations if row.get("status") == status)
                for status in sorted({str(row.get("status")) for row in reservations})
            },
        }
    )
    binding = manifest.get("authorization_ledger", {}).get("binding", {})
    binding_expected = {
        "authorization_id": binding.get("authorization_id"),
        "max_calls": binding.get("max_calls"),
        "provider": binding.get("provider"),
        "model": binding.get("model"),
        "protocol": binding.get("protocol"),
        "settings_sha256": binding.get("settings_sha256"),
        "scope_sha256": binding.get("scope_sha256"),
        "input_sha256": binding.get("input_sha256"),
        "artifact_namespace": binding.get("artifact_namespace"),
    }
    result["binding_match"] = bool(binding_expected) and {key: auth.get(key) for key in binding_expected} == binding_expected
    result["authorization_hash_match"] = (
        manifest.get("authorization_ledger", {}).get("authorization_sha256") == _sha256_json(binding)
    )
    result["ledger_rows_hash_match"] = (
        manifest.get("authorization_ledger", {}).get("ledger_rows_sha256") == _sha256_json(list(ledger_rows))
    )
    result["reservation_match"] = bool(
        len(reservations) == len(ledger_rows) == 1
        and reservations[0].get("authorization_id") == auth_id
        and reservations[0].get("ordinal") == ledger_rows[0].get("ordinal")
        and reservations[0].get("request_sha256") == ledger_rows[0].get("request_sha256")
        and reservations[0].get("status") == ledger_rows[0].get("status")
        and reservations[0].get("error_code") == ledger_rows[0].get("error_code")
    )
    result["global_one_of_one"] = bool(
        auth.get("max_calls") == MAX_CALLS
        and auth.get("calls_reserved") == 1
        and len(reservations) == 1
        and rejection_count == 0
        and reservations[0].get("ordinal") == 1
        and manifest.get("authorization_ledger", {}).get("calls_used") == 1
        and manifest.get("authorization_ledger", {}).get("calls_remaining") == 0
    )
    return result


def _values(documents: Sequence[Mapping[str, Any]], name: str) -> List[Any]:
    result: List[Any] = []
    for document in documents:
        result.extend(_find_values(document, {name}))
    return result


def audit_artifact(artifact_dir: Path) -> Dict[str, Any]:
    artifact_dir = _safe_artifact_dir(artifact_dir)
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
    documents = {"manifest": manifest, "aggregate": aggregate, "cost": cost, "diagnostic": diagnostic}
    document_list = [manifest, aggregate, cost, diagnostic]

    privacy_hits = _privacy_hits(documents, [*ledger, *errors])
    privacy = {
        "body_key_hits": privacy_hits["body"],
        "identity_key_hits": privacy_hits["identity"],
        "reasoning_key_hits": privacy_hits["reasoning"],
        "secret_key_hits": privacy_hits["secret"],
        "body_free": not any(privacy_hits.values()),
    }
    hashes = _artifact_hash_check(artifact_dir, manifest, actual)
    settings = _settings_check(documents)
    durable = _durable_ledger_check(manifest, ledger)

    provider_values = [
        manifest.get("provider", {}).get("model") if isinstance(manifest.get("provider"), Mapping) else None,
        aggregate.get("model"),
        diagnostic.get("model"),
        ledger[0].get("model") if ledger else None,
    ]
    protocol_values = [manifest.get("protocol_version"), aggregate.get("protocol_version"), diagnostic.get("protocol_version"), ledger[0].get("protocol") if ledger else None]
    prompt_values = [manifest.get("prompt_version"), aggregate.get("prompt_version")]
    provider_ids = [manifest.get("provider", {}).get("id") if isinstance(manifest.get("provider"), Mapping) else None, ledger[0].get("provider") if ledger else None]
    source_values = [
        manifest.get("provider", {}).get("source") if isinstance(manifest.get("provider"), Mapping) else None,
        aggregate.get("source"),
        diagnostic.get("source"),
    ]
    model_protocol_source = {
        "model_values": sorted({str(value) for value in provider_values if isinstance(value, str)}),
        "model_unchanged": provider_values == [EXPECTED_MODEL] * 4,
        "protocol_values": sorted({str(value) for value in protocol_values if isinstance(value, str)}),
        "protocol_unchanged": protocol_values == [EXPECTED_PROTOCOL] * 4,
        "prompt_values": sorted({str(value) for value in prompt_values if isinstance(value, str)}),
        "prompt_unchanged": prompt_values == [EXPECTED_PROMPT] * 2,
        "provider_values": sorted({str(value) for value in provider_ids if isinstance(value, str)}),
        "provider_unchanged": provider_ids == [EXPECTED_PROVIDER] * 2,
        "source_values": sorted({str(value) for value in source_values if isinstance(value, str)}),
        "source_unchanged": source_values == [EXPECTED_SOURCE] * 3,
    }

    extra_body = aggregate.get("extra_body") if isinstance(aggregate.get("extra_body"), Mapping) else {}
    response_format = {
        "mode_values": sorted({str(value) for value in _values(document_list, "response_format_mode") if isinstance(value, str)}),
        "sent_values": sorted({value for value in _values(document_list, "response_format_sent") if isinstance(value, bool)}),
    }
    response_format["omitted_and_not_sent"] = response_format["mode_values"] == [EXPECTED_RESPONSE_FORMAT] and response_format["sent_values"] == [False]
    thinking = {
        "all_true": all(document.get("thinking_disabled") is True for document in document_list),
        "extra_body_per_call": extra_body.get("per_call") is True and extra_body.get("sent") is True,
        "extra_body_shape": extra_body.get("field_names") == ["thinking"] and extra_body.get("value_shape") == "disabled",
        "global_settings_not_mutated": extra_body.get("global_settings_mutated") is False,
    }
    thinking["unchanged"] = bool(all(thinking.values()))

    strict = diagnostic.get("strict_result") if isinstance(diagnostic.get("strict_result"), Mapping) else {}
    aggregate_strict = aggregate.get("strict_result") if isinstance(aggregate.get("strict_result"), Mapping) else {}
    response = diagnostic.get("response_diagnostics") if isinstance(diagnostic.get("response_diagnostics"), Mapping) else {}
    candidate = diagnostic.get("diagnostic_candidate") if isinstance(diagnostic.get("diagnostic_candidate"), Mapping) else {}
    strict_json = {
        "json_parse_pass": all(value.get("strict_parse_code") == "ok" for value in (strict, aggregate_strict, response)),
        "schema_validation_code": sorted({str(value.get("strict_validation_code")) for value in (strict, aggregate_strict, response)}),
        "schema_output_topic_keys": all(value.get("strict_validation_code") == "output_topic_keys" for value in (strict, aggregate_strict, response)),
        "strict_complete_false": all(value.get("strict_complete") is False for value in (strict, aggregate_strict, response)),
        "not_accepted_for_complete": strict.get("accepted_for_complete") is False and aggregate_strict.get("accepted_for_complete") is False and candidate.get("accepted_for_complete") is False,
        "diagnostic_candidate_separate": strict.get("diagnostic_candidate_separate") is True and candidate.get("diagnostic_only") is True,
        "candidate_schema_invalid": candidate.get("present") is True and candidate.get("json_object_candidate") is True and candidate.get("schema_valid") is False,
    }
    strict_json["blocked_as_expected"] = bool(strict_json["json_parse_pass"] and strict_json["schema_output_topic_keys"] and strict_json["strict_complete_false"] and strict_json["not_accepted_for_complete"])

    request = aggregate.get("request") if isinstance(aggregate.get("request"), Mapping) else {}
    response_limits = {
        "topic_limit_one": request.get("topic_limit") == 1,
        "topic_rule_present": request.get("topic_limit_rule") == EXPECTED_RULE,
        "input_proxy_present": isinstance(request.get("input_token_proxy"), int),
        "input_proxy_within_limit": isinstance(request.get("input_token_proxy"), int) and request.get("input_token_proxy") <= MAX_INPUT_TOKEN_PROXY,
        "max_output_tokens_four_hundred": cost.get("max_output_tokens") == MAX_OUTPUT_TOKENS,
        "actual_input_within_limit": isinstance(response.get("input_tokens"), int) and response.get("input_tokens") <= MAX_INPUT_TOKEN_PROXY,
        "actual_output_within_limit": isinstance(response.get("output_tokens"), int) and response.get("output_tokens") <= MAX_OUTPUT_TOKENS,
        "latency_within_limit": isinstance(response.get("latency_ms"), (int, float)) and 0 < response.get("latency_ms") <= MAX_LATENCY_MS,
        "finish_stop": response.get("finish_reason") == "stop",
        "reasoning_zero": response.get("reasoning_length") == 0,
        "output_hash_shape": bool(HEX64.fullmatch(str(response.get("output_sha256") or ""))),
    }
    response_limits["within_limits"] = bool(all(response_limits.values()))

    stage_b_values = _values(document_list, "stage_b_pilot") + _values(document_list, "stage_b")
    stage_c_values = _values(document_list, "stage_c_pilot") + _values(document_list, "stage_c")
    no_development = bool(
        manifest.get("development_input_read") is False
        and aggregate.get("development_input_read") is False
        and manifest.get("private_input_read") is False
        and aggregate.get("private_input_read") is False
        and manifest.get("frozen_read") is False
        and aggregate.get("frozen_read") is False
        and manifest.get("gold_loaded") is False
        and aggregate.get("gold_loaded") is False
        and manifest.get("development_calls", 0) == 0
        and aggregate.get("development_calls", 0) == 0
    )
    stage_flags = {
        "stage_a_development_false": no_development,
        "stage_b_observed_values": stage_b_values,
        "stage_c_observed_values": stage_c_values,
        "stage_b_false": all(value is False for value in stage_b_values),
        "stage_c_false": all(value is False for value in stage_c_values),
    }

    calls = {
        "manifest_aggregate_cost_one": manifest.get("provider_calls") == aggregate.get("provider_calls") == cost.get("provider_calls") == 1,
        "provider_called": manifest.get("provider_called") is True,
        "ledger_one": len(ledger) == 1,
        "errors_one": len(errors) == 1 and errors[0].get("provider_calls") == 1,
        "retry_zero": manifest.get("retry_count") == aggregate.get("retry_count") == cost.get("retry_count") == MAX_RETRIES,
        "declared_limit_one": manifest.get("provider_call_limit") == aggregate.get("provider_call_limit") == MAX_CALLS,
    }
    calls["exactly_one_no_retry"] = bool(all(calls.values()))

    audit_checks = {
        "required_files_and_hashes": bool(not missing and not unexpected and hashes["all_match"]),
        "artifact_is_synthetic_blocked_health_only": bool(
            manifest.get("artifact_version") == ARTIFACT_VERSION
            and manifest.get("local_day") == LOCAL_DAY
            and manifest.get("split") == "synthetic"
            and manifest.get("status") == "blocked"
            and manifest.get("success") is False
            and manifest.get("diagnostic_only") is True
            and manifest.get("synthetic_only") is True
        ),
        "exact_one_call_no_retry": calls["exactly_one_no_retry"],
        "global_authorization_one_of_one": durable["global_one_of_one"],
        "authorization_binding_and_hashes": bool(durable["binding_match"] and durable["reservation_match"] and durable["authorization_hash_match"] and durable["ledger_rows_hash_match"]),
        "model_protocol_source_unchanged": bool(all(model_protocol_source[key] for key in ("model_unchanged", "protocol_unchanged", "prompt_unchanged", "provider_unchanged", "source_unchanged"))),
        "response_format_omitted": response_format["omitted_and_not_sent"],
        "thinking_disabled_per_call": thinking["unchanged"],
        "settings_unchanged": settings["settings_unchanged"],
        "json_parse_pass_schema_output_topic_keys_failure": strict_json["blocked_as_expected"],
        "topic_limit_one": response_limits["topic_limit_one"] and response_limits["topic_rule_present"],
        "diagnostic_not_complete": strict_json["strict_complete_false"] and strict_json["not_accepted_for_complete"] and strict_json["diagnostic_candidate_separate"],
        "tokens_finish_latency_within_limits": response_limits["within_limits"],
        "development_stage_a_b_c_false": no_development and stage_flags["stage_b_false"] and stage_flags["stage_c_false"],
        "body_free": privacy["body_free"],
    }
    audit_ok = bool(all(audit_checks.values()))

    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_status": manifest.get("status", "unknown"),
        "audit_status": "pass" if audit_ok else "fail",
        "health_complete": False,
        "audit_checks": audit_checks,
        "missing_files": missing,
        "unexpected_root_files": unexpected,
        "hashes": hashes,
        "privacy": privacy,
        "call_counts": calls,
        "authorization_ledger": durable,
        "model_protocol_source": model_protocol_source,
        "response_format": response_format,
        "thinking": thinking,
        "settings": settings,
        "strict_json": strict_json,
        "topic_limit_and_limits": response_limits,
        "stage_flags": stage_flags,
        "response_diagnostics": {
            "input_tokens": response.get("input_tokens"),
            "output_tokens": response.get("output_tokens"),
            "latency_ms": response.get("latency_ms"),
            "finish_reason": response.get("finish_reason"),
            "reasoning_length": response.get("reasoning_length"),
        },
        "errors": {
            "rows": len(errors),
            "codes": sorted({str(row.get("error_code")) for row in errors}),
            "schema_failure_expected": len(errors) == 1 and errors[0].get("error_code") == "schema_validation_failed",
        },
        "scope": {
            "audit_provider_calls": 0,
            "audit_development_input_read": False,
            "audit_private_input_read": False,
            "audit_frozen_read": False,
            "audit_production_state_written": False,
            "artifact_synthetic_only": True,
        },
        "next_step": {
            "allow_one_synthetic_health_only": False,
            "allow_stage_a_development": False,
            "allow_development_input": False,
            "allow_stage_b": False,
            "allow_stage_c": False,
            "allow_production": False,
            "new_authorization_required": True,
            "requires_offline_protocol_repair": True,
            "requires_independent_tests": True,
            "reason": "v2 health is blocked by output_topic_keys; do not reuse diagnostic candidate or directly authorize development; future v3 requires offline implementation and an independent authorization",
        },
    }
    output_hits = _privacy_hits({"audit": report}, [])
    if any(output_hits.values()):
        raise AuditError("audit_output_not_body_free")
    return report


def _human_rows(report: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
    checks = report.get("audit_checks") if isinstance(report.get("audit_checks"), Mapping) else {}
    for name, value in checks.items():
        yield {"check": str(name), "status": "pass" if value is True else "fail"}
    yield {"check": "audit_status", "status": report.get("audit_status")}
    yield {"check": "artifact_status", "status": report.get("artifact_status")}
    yield {"check": "health_complete", "status": report.get("health_complete")}
    next_step = report.get("next_step") if isinstance(report.get("next_step"), Mapping) else {}
    for name in ("allow_one_synthetic_health_only", "allow_stage_a_development", "allow_stage_b", "allow_stage_c", "allow_production"):
        yield {"check": name, "status": next_step.get(name)}


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
    parser = argparse.ArgumentParser(description="Independent K21 compact Stage-A health-v2 audit")
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
