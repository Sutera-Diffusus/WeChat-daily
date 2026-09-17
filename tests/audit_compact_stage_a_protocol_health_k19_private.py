"""Independent K19 audit for the compact Stage-A protocol health artifact.

This audit is deliberately a side-car.  It uses only the six body-free K18
artifact files and, for the settings check, the raw-byte digest of the
ordinary workbench settings file.  It does not import a runner or provider,
does not call a provider, and never opens development, frozen, or frozen_test
input.  The K18 health exchange is expected to be *blocked*: JSON parsing
passed, but the compact Stage-A schema rejected the returned topic count.

The audit can therefore pass while ``health_complete`` remains false.  A
future provider call is not authorized by this report; a new authorization
must wait for an offline protocol repair and independent tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "compact_stage_a_protocol_health_v1"
AUDIT_SCHEMA_VERSION = "compact_stage_a_protocol_health_k19_audit_v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"
AUTHORITY_ROOT = ROOT / ".runtime" / "compact-stage-a-authorizations"

REQUIRED_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "diagnostic.private.json",
    "ledger.private.jsonl",
    "errors.private.jsonl",
)

EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROVIDER = "openai-compatible"
EXPECTED_SOURCE = "deepseek-openai-compatible"
EXPECTED_PROTOCOL = "stage_a_topic_assignment_compact_v1"
EXPECTED_PROMPT = "stage_a_topic_assignment_compact_prompt_v1"
EXPECTED_RESPONSE_FORMAT = "omitted"
EXPECTED_AUTHORIZATION = "K18_COMPACT_STAGE_A_HEALTH_V1"
EXPECTED_NAMESPACE = "compact-stage-a-health-v1"
EXPECTED_REPORT_SCHEMA = "compact_stage_a_protocol_health_report_v1"
EXPECTED_RUNNER_SCHEMA = "compact_stage_a_protocol_health_runner_v1"
EXPECTED_LEDGER_SCHEMA = "call_authorization_ledger_v1"
MAX_CALLS = 1
MAX_RETRIES = 0
MAX_INPUT_TOKEN_PROXY = 1600
MAX_OUTPUT_TOKENS = 400
MAX_LATENCY_MS = 30_000.0

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
    {
        "access_token",
        "api_key",
        "apikey",
        "password",
        "private_key",
        "secret",
        "secrets",
    }
)
IDENTITY_KEYS = frozenset(
    {
        "account_id",
        "chat_id",
        "contact_id",
        "display_name",
        "email",
        "person_id",
        "phone",
        "speaker",
        "user_id",
    }
)
REASONING_KEYS = frozenset({"analysis", "chain_of_thought", "reasoning", "reasoning_content", "thoughts"})


class AuditError(ValueError):
    """Body-free audit failure."""


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


def _find(value: Any, names: Iterable[str]) -> List[Tuple[str, Any]]:
    wanted = {str(name).casefold() for name in names}
    return [(path, child) for path, child in _iter_nodes(value) if _key_name(path) in wanted]


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _safe_artifact_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_root = (ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY).resolve()
    if resolved.name != ARTIFACT_VERSION or resolved.parent != expected_root:
        raise AuditError("out_of_scope_artifact_directory")
    if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in resolved.parts):
        raise AuditError("forbidden_artifact_path")
    return resolved


def _privacy_hits(documents: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    hits = {"body": 0, "identity": 0, "reasoning": 0, "secret": 0}
    values: Dict[str, Any] = dict(documents)
    values["ledger"] = list(rows)
    for _path, _value in _iter_nodes(values):
        key = _key_name(_path)
        if key in BODY_KEYS:
            hits["body"] += 1
        if key in IDENTITY_KEYS:
            hits["identity"] += 1
        if key in REASONING_KEYS:
            hits["reasoning"] += 1
        if key in SECRET_KEYS or key.endswith(("_secret", "_password", "_api_key")):
            hits["secret"] += 1
    return hits


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


def _settings_check(documents: Mapping[str, Any], durable: Mapping[str, Any]) -> Dict[str, Any]:
    before: List[str] = []
    after: List[str] = []
    flags: List[bool] = []
    for document in documents.values():
        for _path, value in _find(document, {"settings_before_sha256"}):
            if isinstance(value, str):
                before.append(value)
        for _path, value in _find(document, {"settings_after_sha256"}):
            if isinstance(value, str):
                after.append(value)
        for _path, value in _find(document, {"settings_unchanged"}):
            if isinstance(value, bool):
                flags.append(value)
    manifest_ledger = documents.get("manifest", {}).get("authorization_ledger") if isinstance(documents.get("manifest"), Mapping) else {}
    binding = manifest_ledger.get("binding") if isinstance(manifest_ledger, Mapping) and isinstance(manifest_ledger.get("binding"), Mapping) else {}
    binding_settings = binding.get("settings_sha256") if isinstance(binding, Mapping) else None
    if isinstance(binding_settings, str):
        before.append(binding_settings)
        after.append(binding_settings)
    valid_before = [value for value in before if HEX64.fullmatch(value)]
    valid_after = [value for value in after if HEX64.fullmatch(value)]
    current_hash = ""
    current_read = False
    try:
        current_hash = _sha256_bytes(SETTINGS_PATH.read_bytes())
        current_read = True
    except OSError:
        pass
    expected = valid_before[0] if valid_before else ""
    return {
        "settings_hash_shape_ok": bool(valid_before and valid_after and len(valid_before) == len(before) and len(valid_after) == len(after)),
        "before_after_equal": bool(valid_before and valid_before == valid_after),
        "explicit_unchanged_flags": bool(flags) and all(flags),
        "current_file_read_as_raw_bytes": current_read,
        "current_file_hash_matches": bool(current_hash and expected and current_hash == expected),
        "durable_binding_matches": bool(binding_settings and expected and binding_settings == expected),
        "settings_unchanged": bool(
            valid_before
            and valid_after
            and valid_before == valid_after
            and flags
            and all(flags)
            and current_read
            and current_hash == expected
            and binding_settings == expected
        ),
        "hash_basis": "raw_workbench_settings_bytes",
    }


def _durable_ledger_check(manifest: Mapping[str, Any], ledger_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    authorization = manifest.get("authorization") if isinstance(manifest.get("authorization"), Mapping) else {}
    auth_id = str(authorization.get("authorization_id") or "")
    db_path = AUTHORITY_ROOT / ("authorization-%s.sqlite3" % _sha256_bytes(auth_id.encode("utf-8"))[:32])
    exported_ids = {str(row.get("authorization_id") or "") for row in ledger_rows}
    exported_ordinals = [row.get("ordinal") for row in ledger_rows]
    result: Dict[str, Any] = {
        "path_derived_from_authorization": bool(auth_id and db_path.parent == AUTHORITY_ROOT),
        "database_present": db_path.is_file(),
        "read_only": True,
        "authorization_id": auth_id,
        "max_calls": None,
        "calls_reserved": None,
        "calls_used": None,
        "calls_remaining": None,
        "reservation_rows": 0,
        "rejection_rows": 0,
        "status_counts": {},
        "binding_match": False,
        "reservation_match": False,
        "global_one_of_one": False,
        "cross_output_nonreset": False,
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
        rejection_count = int(
            connection.execute("SELECT COUNT(*) FROM rejections WHERE authorization_id = ?", (auth_id,)).fetchone()[0]
        )
        connection.close()
    except (OSError, sqlite3.Error):
        return result
    if auth_row is None:
        return result
    auth = dict(auth_row)
    result.update(
        {
            "max_calls": auth["max_calls"],
            "calls_reserved": auth["calls_reserved"],
            "calls_used": auth["calls_reserved"],
            "calls_remaining": max(0, int(auth["max_calls"]) - int(auth["calls_reserved"])),
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
    binding_actual = {key: auth.get(key) for key in binding_expected}
    result["binding_match"] = bool(binding_expected and binding_actual == binding_expected)
    result["reservation_match"] = bool(
        len(reservations) == len(ledger_rows) == 1
        and all(str(row.get("authorization_id")) == auth_id for row in ledger_rows)
        and reservations[0].get("ordinal") == ledger_rows[0].get("ordinal")
        and reservations[0].get("request_sha256") == ledger_rows[0].get("request_sha256")
        and reservations[0].get("status") == ledger_rows[0].get("status")
        and reservations[0].get("error_code") == ledger_rows[0].get("error_code")
    )
    snapshot = manifest.get("authorization_ledger") if isinstance(manifest.get("authorization_ledger"), Mapping) else {}
    result["global_one_of_one"] = bool(
        auth.get("max_calls") == MAX_CALLS
        and auth.get("calls_reserved") == 1
        and len(reservations) == 1
        and rejection_count == 0
        and reservations[0].get("ordinal") == 1
    )
    result["cross_output_nonreset"] = bool(
        result["global_one_of_one"]
        and result["binding_match"]
        and result["reservation_match"]
        and snapshot.get("calls_used") == auth.get("calls_reserved")
        and snapshot.get("calls_remaining") == max(0, int(auth.get("max_calls", 0)) - int(auth.get("calls_reserved", 0)))
        and exported_ids == {auth_id}
        and exported_ordinals == [1]
    )
    return result


def _cross_output_check(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    snapshots = [
        manifest.get("authorization_ledger"),
        aggregate.get("authorization_ledger"),
    ]
    snapshot_equal = bool(all(isinstance(value, Mapping) for value in snapshots) and snapshots[0] == snapshots[1])
    auth_id = EXPECTED_AUTHORIZATION
    exact_provider_calls = bool(
        manifest.get("provider_calls") == aggregate.get("provider_calls") == cost.get("provider_calls") == 1
        and manifest.get("provider_called") is True
        and len(ledger) == 1
        and len(errors) == 1
        and errors[0].get("provider_calls") == 1
        and str(ledger[0].get("authorization_id")) == auth_id
    )
    return {
        "authorization_snapshots_equal": snapshot_equal,
        "manifest_aggregate_cost_calls_equal": exact_provider_calls,
        "ledger_rows_one": len(ledger) == 1,
        "errors_rows_one": len(errors) == 1,
        "cost_authorization_calls_used_one": cost.get("authorization_calls_used") == 1,
        "no_per_output_reset": bool(snapshot_equal and exact_provider_calls and cost.get("authorization_calls_used") == 1),
    }


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

    privacy_hits = _privacy_hits(documents, [*ledger, *errors])
    privacy = {
        "body_key_hits": privacy_hits["body"],
        "identity_key_hits": privacy_hits["identity"],
        "reasoning_key_hits": privacy_hits["reasoning"],
        "secret_key_hits": privacy_hits["secret"],
        "body_free": not any(privacy_hits.values()),
    }

    strict_manifest = {
        "artifact_version": manifest.get("artifact_version") == ARTIFACT_VERSION,
        "local_day": manifest.get("local_day") == LOCAL_DAY,
        "split_synthetic": manifest.get("split") == "synthetic",
        "status_blocked": manifest.get("status") == "blocked",
        "success_false": manifest.get("success") is False,
        "diagnostic_only": manifest.get("diagnostic_only") is True,
        "synthetic_only": manifest.get("synthetic_only") is True,
        "development_unread": manifest.get("development_input_read") is False and manifest.get("private_input_read") is False,
        "frozen_unread": manifest.get("frozen_read") is False,
        "gold_unread": manifest.get("gold_loaded") is False,
        "production_not_written": manifest.get("production_state_written") is False,
        "provider_called": manifest.get("provider_called") is True,
        "provider_calls_one": manifest.get("provider_calls") == 1,
        "retry_zero": manifest.get("retry_count") == 0,
        "thinking_disabled": manifest.get("thinking_disabled") is True,
        "response_format_omitted": manifest.get("response_format_mode") == EXPECTED_RESPONSE_FORMAT and manifest.get("response_format_sent") is False,
        "settings_unchanged_reported": manifest.get("settings_unchanged") is True,
    }

    provider_values: List[str] = []
    for value in (
        manifest.get("provider", {}).get("model") if isinstance(manifest.get("provider"), Mapping) else None,
        aggregate.get("model"),
        diagnostic.get("model"),
        ledger[0].get("model") if ledger else None,
    ):
        if isinstance(value, str):
            provider_values.append(value)
    protocol_values: List[str] = []
    for value in (
        manifest.get("protocol_version"),
        aggregate.get("protocol_version"),
        diagnostic.get("protocol_version"),
        ledger[0].get("protocol") if ledger else None,
    ):
        if isinstance(value, str):
            protocol_values.append(value)
    prompt_values: List[str] = []
    for value in (
        manifest.get("prompt_version"),
        aggregate.get("prompt_version"),
        diagnostic.get("prompt_version"),
    ):
        if isinstance(value, str):
            prompt_values.append(value)
    provider_values_explicit: List[str] = []
    for value in (
        manifest.get("provider", {}).get("id") if isinstance(manifest.get("provider"), Mapping) else None,
        ledger[0].get("provider") if ledger else None,
    ):
        if isinstance(value, str):
            provider_values_explicit.append(value)
    source_values: List[str] = []
    for value in (
        manifest.get("provider", {}).get("source") if isinstance(manifest.get("provider"), Mapping) else None,
        aggregate.get("source"),
        diagnostic.get("source"),
    ):
        if isinstance(value, str):
            source_values.append(value)
    model_protocol = {
        "model_values": sorted(set(provider_values)),
        "model_unchanged": provider_values == [EXPECTED_MODEL] * len(provider_values) and len(provider_values) == 4,
        "protocol_values": sorted(set(protocol_values)),
        "protocol_unchanged": protocol_values == [EXPECTED_PROTOCOL] * len(protocol_values) and len(protocol_values) == 4,
        "prompt_values": sorted(set(prompt_values)),
        "prompt_unchanged": prompt_values == [EXPECTED_PROMPT] * len(prompt_values) and len(prompt_values) == 2,
        "provider_values": sorted(set(provider_values_explicit)),
        "provider_unchanged": provider_values_explicit == [EXPECTED_PROVIDER] * len(provider_values_explicit) and len(provider_values_explicit) == 2,
        "source_values": sorted(set(source_values)),
        "source_unchanged": source_values == [EXPECTED_SOURCE] * len(source_values) and len(source_values) == 3,
    }

    extra_body = aggregate.get("extra_body") if isinstance(aggregate.get("extra_body"), Mapping) else {}
    response_format = {
        "mode_values": sorted(
            {
                str(value)
                for document in (manifest, aggregate, diagnostic, cost)
                for _path, value in _find(document, {"response_format_mode"})
                if isinstance(value, str)
            }
        ),
        "sent_values": sorted(
            {
                value
                for document in (manifest, aggregate, diagnostic, cost)
                for _path, value in _find(document, {"response_format_sent"})
                if isinstance(value, bool)
            }
        ),
    }
    response_format["omitted_and_not_sent"] = bool(
        response_format["mode_values"] == [EXPECTED_RESPONSE_FORMAT]
        and response_format["sent_values"] == [False]
    )
    thinking = {
        "manifest_aggregate_diagnostic_cost_true": all(
            document.get("thinking_disabled") is True
            for document in (manifest, aggregate, diagnostic, cost)
        ),
        "extra_body_per_call": extra_body.get("per_call") is True and extra_body.get("sent") is True,
        "extra_body_shape": extra_body.get("field_names") == ["thinking"] and extra_body.get("value_shape") == "disabled",
        "global_settings_not_mutated": extra_body.get("global_settings_mutated") is False,
        "thinking_disabled_unchanged": all(
            document.get("thinking_disabled") is True for document in (manifest, aggregate, diagnostic, cost)
        ) and extra_body.get("global_settings_mutated") is False,
    }

    strict = diagnostic.get("strict_result") if isinstance(diagnostic.get("strict_result"), Mapping) else {}
    aggregate_strict = aggregate.get("strict_result") if isinstance(aggregate.get("strict_result"), Mapping) else {}
    candidate = diagnostic.get("diagnostic_candidate") if isinstance(diagnostic.get("diagnostic_candidate"), Mapping) else {}
    response = diagnostic.get("response_diagnostics") if isinstance(diagnostic.get("response_diagnostics"), Mapping) else {}
    strict_result = {
        "json_parse_pass": strict.get("strict_parse_code") == "ok" and aggregate_strict.get("strict_parse_code") == "ok" and response.get("strict_parse_code") == "ok",
        "schema_validation_failed_as_expected": strict.get("strict_validation_code") == "output_topic_limit" and aggregate_strict.get("strict_validation_code") == "output_topic_limit" and response.get("strict_validation_code") == "output_topic_limit",
        "strict_complete_false": strict.get("strict_complete") is False and aggregate_strict.get("strict_complete") is False and response.get("strict_complete") is False,
        "not_accepted_for_complete": strict.get("accepted_for_complete") is False and candidate.get("accepted_for_complete") is False and aggregate_strict.get("accepted_for_complete") is False,
        "diagnostic_candidate_separate": strict.get("diagnostic_candidate_separate") is True,
        "candidate_body_free_shape": candidate.get("present") is True and candidate.get("json_object_candidate") is True and candidate.get("schema_valid") is False and candidate.get("diagnostic_only") is True,
        "schema_gate_blocked": False,
    }
    strict_result["schema_gate_blocked"] = bool(
        strict_result["schema_validation_failed_as_expected"] and strict_result["strict_complete_false"]
    )

    response_values = {
        "content_length": response.get("content_length"),
        "input_tokens": response.get("input_tokens"),
        "output_tokens": response.get("output_tokens"),
        "latency_ms": response.get("latency_ms"),
        "finish_reason": response.get("finish_reason"),
        "reasoning_length": response.get("reasoning_length"),
        "output_sha256": response.get("output_sha256"),
    }
    request = aggregate.get("request") if isinstance(aggregate.get("request"), Mapping) else {}
    token_latency = {
        "request_input_proxy_present": isinstance(request.get("input_token_proxy"), int),
        "request_input_proxy_within_limit": isinstance(request.get("input_token_proxy"), int) and request.get("input_token_proxy") <= MAX_INPUT_TOKEN_PROXY,
        "actual_input_within_limit": isinstance(response.get("input_tokens"), int) and 0 <= response.get("input_tokens") <= MAX_INPUT_TOKEN_PROXY,
        "output_within_limit": isinstance(response.get("output_tokens"), int) and 0 <= response.get("output_tokens") <= MAX_OUTPUT_TOKENS,
        "latency_within_limit": isinstance(response.get("latency_ms"), (int, float)) and 0 < response.get("latency_ms") <= MAX_LATENCY_MS,
        "finish_stop": response.get("finish_reason") == "stop",
        "reasoning_zero": response.get("reasoning_length") == 0,
        "hash_shape": isinstance(response.get("output_sha256"), str) and bool(HEX64.fullmatch(response.get("output_sha256") or "")),
    }
    token_latency["within_limits"] = bool(all(token_latency.values()))

    cross_output = _cross_output_check(manifest, aggregate, cost, diagnostic, ledger, errors)
    durable = _durable_ledger_check(manifest, ledger)
    settings = _settings_check({"manifest": manifest, "aggregate": aggregate, "diagnostic": diagnostic}, durable)
    hashes = _artifact_hash_check(artifact_dir, manifest, actual)

    no_dev_or_frozen = bool(
        manifest.get("development_input_read") is False
        and aggregate.get("development_input_read") is False
        and manifest.get("frozen_read") is False
        and aggregate.get("frozen_read") is False
        and manifest.get("development_calls", 0) == 0
        and aggregate.get("development_calls", 0) == 0
    )
    stage_b_values = [value for document in documents.values() for _path, value in _find(document, {"stage_b", "stage_b_pilot"})]
    stage_c_values = [value for document in documents.values() for _path, value in _find(document, {"stage_c", "stage_c_pilot"})]
    stage_b_c_not_run = bool(
        all(value is False for value in stage_b_values) if stage_b_values else True
    ) and bool(
        all(value is False for value in stage_c_values) if stage_c_values else True
    )
    calls = {
        "manifest_aggregate_cost_one": manifest.get("provider_calls") == aggregate.get("provider_calls") == cost.get("provider_calls") == 1,
        "ledger_one": len(ledger) == 1,
        "errors_one": len(errors) == 1 and errors[0].get("provider_calls") == 1,
        "retry_zero": manifest.get("retry_count") == aggregate.get("retry_count") == cost.get("retry_count") == 0,
        "declared_limit_one": manifest.get("provider_call_limit") == aggregate.get("provider_call_limit") == MAX_CALLS,
        "exactly_one": True,
    }
    calls["exactly_one"] = bool(all(calls[name] for name in ("manifest_aggregate_cost_one", "ledger_one", "errors_one", "retry_zero", "declared_limit_one")))

    audit_checks = {
        "required_files_and_hashes": bool(not missing and not unexpected and hashes["all_match"]),
        "manifest_scope_and_status": bool(all(strict_manifest.values())),
        "exact_one_call_no_retry": calls["exactly_one"],
        "global_authorization_one_of_one": durable["global_one_of_one"],
        "cross_output_authorization_not_reset": bool(durable["cross_output_nonreset"] and cross_output["no_per_output_reset"]),
        "model_protocol_source_unchanged": bool(
            model_protocol["model_unchanged"]
            and model_protocol["protocol_unchanged"]
            and model_protocol["prompt_unchanged"]
            and model_protocol["provider_unchanged"]
            and model_protocol["source_unchanged"]
        ),
        "response_format_unchanged": response_format["omitted_and_not_sent"],
        "thinking_per_call_unchanged": thinking["thinking_disabled_unchanged"],
        "settings_unchanged": settings["settings_unchanged"],
        "json_parse_pass_schema_failure_recorded": bool(strict_result["json_parse_pass"] and strict_result["schema_validation_failed_as_expected"]),
        "diagnostic_not_complete": bool(
            strict_result["strict_complete_false"]
            and strict_result["not_accepted_for_complete"]
            and strict_result["diagnostic_candidate_separate"]
        ),
        "tokens_finish_latency_within_limits": token_latency["within_limits"],
        "development_stage_b_c_false": bool(no_dev_or_frozen and stage_b_c_not_run),
        "body_free": privacy["body_free"],
    }
    audit_integrity_pass = bool(all(audit_checks.values()))

    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_status": manifest.get("status", "unknown"),
        "audit_status": "pass" if audit_integrity_pass else "fail",
        "health_complete": False,
        "audit_checks": audit_checks,
        "missing_files": missing,
        "unexpected_root_files": unexpected,
        "hashes": hashes,
        "privacy": privacy,
        "call_counts": calls,
        "authorization_ledger": durable,
        "cross_output": cross_output,
        "model_protocol_source": model_protocol,
        "response_format": response_format,
        "thinking": thinking,
        "settings": settings,
        "strict_json": strict_result,
        "response_diagnostics": response_values,
        "tokens_finish_latency": token_latency,
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
            "synthetic_artifact_only": True,
        },
        "next_step": {
            "allow_one_synthetic_health_only": False,
            "allow_development_input": False,
            "allow_stage_b": False,
            "allow_stage_c": False,
            "new_authorization_required": True,
            "requires_offline_protocol_repair": True,
            "requires_independent_tests": True,
            "reason": "K18_schema_output_topic_limit; wait for offline K19 repair and independent tests before any new authorization",
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
    for name in ("allow_one_synthetic_health_only", "allow_development_input", "allow_stage_b", "allow_stage_c"):
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
    parser = argparse.ArgumentParser(description="Independent K19 compact Stage-A health audit")
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
