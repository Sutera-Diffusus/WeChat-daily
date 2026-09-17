"""Independent, body-free K25 audit for the K24 health artifact.

This side-car reads only the K24 synthetic health artifact, the raw bytes of
the already recorded Workbench settings file (for a hash comparison), and the
single durable authorization SQLite ledger in read-only mode.  It does not
import the execution runner or a provider client, and it never opens
development, frozen, or chat-message data.  A successful audit authorizes a
*new* K25 development authorization with a five-page/five-call ceiling; it
never authorizes Stage B/C or production.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "compact_stage_a_protocol_health_v3"
AUDIT_SCHEMA_VERSION = "compact_stage_a_protocol_health_v3_k25_audit_v1"
EXPECTED_AUTHORIZATION = "K23_COMPACT_STAGE_A_HEALTH_V3"
NEXT_AUTHORIZATION = "K25_COMPACT_STAGE_A_DEVELOPMENT_V3"
EXPECTED_NAMESPACE = "compact-stage-a-health-v3"
EXPECTED_PROVIDER = "openai-compatible"
EXPECTED_SOURCE = "deepseek-openai-compatible"
EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROTOCOL = "stage_a_topic_assignment_compact_v3"
EXPECTED_PROMPT = "stage_a_topic_assignment_compact_prompt_v3"
EXPECTED_CACHE = "stage_a_topic_assignment_compact_cache_v3"
EXPECTED_RESPONSE_FORMAT = "omitted"
EXPECTED_REPORT_SCHEMA = "compact_stage_a_protocol_health_report_v3"
EXPECTED_RUNNER_SCHEMA = "compact_stage_a_protocol_health_runner_v3_real"
EXPECTED_LEDGER_SCHEMA = "persistent_call_budget_v1"
EXPECTED_TOPIC_RULE = (
    "primary_count=count(message rows with role primary)>=1; "
    "topic_count<=primary_count; every topic has >=1 primary; "
    "each primary is assigned exactly once; each context at most once"
)
EXPECTED_OUTPUT_FIELDS = frozenset(
    {"topics", "topic_id", "primary_message_ids", "context_message_ids", "uncertainty"}
)
MAX_INPUT_TOKEN_PROXY = 1600
MAX_OUTPUT_TOKENS = 400
MAX_LATENCY_MS = 30_000.0
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"
PROTOCOL_SOURCE_PATH = ROOT / "src" / "wechat_bridge" / "compact_stage_a_protocol_v3.py"
AUTHORITY_ROOT = ROOT / ".runtime" / "compact-stage-a-authorizations"
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

# These are key-name checks only.  Hashes, counts, enums, and opaque ledger
# identifiers remain safe to report; body-bearing values are never copied.
BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "content",
        "content_body",
        "content_text",
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
        "response_body",
        "response_text",
        "summary",
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
        "username",
    }
)
REASONING_KEYS = frozenset({"analysis", "chain_of_thought", "reasoning", "reasoning_content", "thoughts"})
SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization_header",
        "password",
        "private_key",
        "secret",
        "secrets",
    }
)


class AuditError(ValueError):
    """Fail-closed K25 audit input or output error."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


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
        for raw_key, child in value.items():
            key = str(raw_key)
            yield key, child
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _values(documents: Iterable[Mapping[str, Any]], wanted: Iterable[str]) -> List[Any]:
    names = {str(name).casefold() for name in wanted}
    result: List[Any] = []
    for document in documents:
        result.extend(child for key, child in _walk(document) if key.casefold() in names)
    return result


def _privacy_hits(documents: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    combined: Dict[str, Any] = dict(documents)
    combined["ledger_rows"] = list(rows)
    hits = {"body": 0, "identity": 0, "reasoning": 0, "secret": 0}
    for key, child in _walk(combined):
        normalized = key.casefold()
        if normalized in BODY_KEYS and child not in (None, "", [], {}, ()):
            hits["body"] += 1
        if normalized in IDENTITY_KEYS:
            hits["identity"] += 1
        if normalized in REASONING_KEYS:
            hits["reasoning"] += 1
        if normalized in SECRET_KEYS or normalized.endswith(("_secret", "_password")):
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
    recorded_names = {str(name) for name in recorded}
    matches = 0
    for name in sorted(expected):
        recorded_hash = str(recorded.get(name) or "").lower()
        try:
            actual_hash = _sha256_bytes((artifact_dir / name).read_bytes())
        except OSError:
            actual_hash = ""
        if HEX64.fullmatch(recorded_hash) and recorded_hash == actual_hash:
            matches += 1
    return {
        "recorded": True,
        "all_match": recorded_names == expected and bool(expected) and matches == len(expected),
        "hashed_file_count": len(expected),
        "match_count": matches,
    }


def _settings_check(documents: Mapping[str, Any], manifest: Mapping[str, Any]) -> Dict[str, Any]:
    before = [value for value in _values(documents.values(), {"settings_before_sha256"}) if isinstance(value, str)]
    after = [value for value in _values(documents.values(), {"settings_after_sha256"}) if isinstance(value, str)]
    flags = [value for value in _values(documents.values(), {"settings_unchanged"}) if isinstance(value, bool)]
    ledger = manifest.get("authorization_ledger")
    binding = ledger.get("binding") if isinstance(ledger, Mapping) else None
    binding_hash = binding.get("settings_sha256") if isinstance(binding, Mapping) else None
    if isinstance(binding_hash, str):
        before.append(binding_hash)
        after.append(binding_hash)
    valid_before = [value.lower() for value in before if HEX64.fullmatch(value.lower())]
    valid_after = [value.lower() for value in after if HEX64.fullmatch(value.lower())]
    expected = valid_before[0] if valid_before else ""
    try:
        current_hash = _sha256_bytes(SETTINGS_PATH.read_bytes())
        current_read = True
    except OSError:
        current_hash = ""
        current_read = False
    return {
        "hash_fields_present": bool(before and after),
        "hash_fields_well_formed": bool(before and after and len(valid_before) == len(before) and len(valid_after) == len(after)),
        "before_after_equal": bool(valid_before and valid_after and len(set(valid_before + valid_after)) == 1),
        "explicit_unchanged_flags_present": bool(flags),
        "explicit_unchanged_flags_true": bool(flags) and all(flags),
        "current_file_read_as_raw_bytes": current_read,
        "current_file_hash_matches": bool(current_read and expected and current_hash == expected),
        "binding_hash_matches": bool(binding_hash and expected and binding_hash == expected),
        "settings_unchanged": bool(
            before
            and after
            and valid_before
            and valid_after
            and len(set(valid_before + valid_after)) == 1
            and flags
            and all(flags)
            and current_read
            and current_hash == expected
            and binding_hash == expected
        ),
        "hash_basis": "raw_workbench_settings_bytes",
    }


def _db_path(authorization_id: str) -> Path:
    return AUTHORITY_ROOT / ("authorization-%s.sqlite3" % _sha256_bytes(authorization_id.encode("utf-8"))[:32])


def _durable_ledger_check(manifest: Mapping[str, Any], ledger_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    authorization = manifest.get("authorization") if isinstance(manifest.get("authorization"), Mapping) else {}
    auth_id = str(authorization.get("authorization_id") or "")
    path = _db_path(auth_id)
    result: Dict[str, Any] = {
        "authorization_id_matches": auth_id == EXPECTED_AUTHORIZATION,
        "database_present": path.is_file(),
        "read_only": True,
        "max_calls": None,
        "calls_reserved": None,
        "reservation_rows": 0,
        "rejection_rows": 0,
        "status_counts": {},
        "binding_match": False,
        "reservation_match": False,
        "authorization_hash_match": False,
        "ledger_rows_hash_match": False,
        "global_one_of_one": False,
    }
    if not path.is_file():
        return result
    connection: Optional[sqlite3.Connection] = None
    try:
        uri = "file:%s?mode=ro" % path.as_posix()
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
        rejection_rows = int(
            connection.execute("SELECT COUNT(*) FROM rejections WHERE authorization_id = ?", (auth_id,)).fetchone()[0]
        )
    except (OSError, sqlite3.Error):
        return result
    finally:
        if connection is not None:
            connection.close()
    if auth_row is None:
        return result
    auth = dict(auth_row)
    snapshot = manifest.get("authorization_ledger") if isinstance(manifest.get("authorization_ledger"), Mapping) else {}
    binding = snapshot.get("binding") if isinstance(snapshot.get("binding"), Mapping) else {}
    binding_keys = (
        "authorization_id",
        "max_calls",
        "provider",
        "model",
        "protocol",
        "settings_sha256",
        "scope_sha256",
        "input_sha256",
        "artifact_namespace",
    )
    result.update(
        {
            "max_calls": auth.get("max_calls"),
            "calls_reserved": auth.get("calls_reserved"),
            "reservation_rows": len(reservations),
            "rejection_rows": rejection_rows,
            "status_counts": {
                status: sum(1 for row in reservations if row.get("status") == status)
                for status in sorted({str(row.get("status")) for row in reservations})
            },
            "binding_match": bool(binding) and {key: auth.get(key) for key in binding_keys} == {key: binding.get(key) for key in binding_keys},
            "authorization_hash_match": snapshot.get("authorization_sha256") == _sha256_json(binding),
            "ledger_rows_hash_match": snapshot.get("ledger_rows_sha256") == _sha256_json(list(ledger_rows)),
            "reservation_match": bool(
                len(reservations) == len(ledger_rows) == 1
                and reservations[0].get("authorization_id") == ledger_rows[0].get("authorization_id")
                and reservations[0].get("ordinal") == ledger_rows[0].get("ordinal")
                and reservations[0].get("request_sha256") == ledger_rows[0].get("request_sha256")
                and reservations[0].get("unit_ref_sha256") == ledger_rows[0].get("unit_ref_sha256")
                and reservations[0].get("attempt") == ledger_rows[0].get("attempt")
                and reservations[0].get("status") == ledger_rows[0].get("status")
                and reservations[0].get("error_code") == ledger_rows[0].get("error_code")
            ),
        }
    )
    result["global_one_of_one"] = bool(
        result["authorization_id_matches"]
        and auth.get("max_calls") == 1
        and auth.get("calls_reserved") == 1
        and len(reservations) == 1
        and rejection_rows == 0
        and reservations[0].get("ordinal") == 1
        and reservations[0].get("status") == "complete"
        and snapshot.get("max_calls") == 1
        and snapshot.get("calls_used") == 1
        and snapshot.get("calls_remaining") == 0
    )
    return result


def _extract_frozenset_assignments(source: str) -> Dict[str, frozenset[str]]:
    """Read only public schema constants without importing project code."""

    tree = ast.parse(source)
    found: Dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        targets: List[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None or len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "frozenset" or len(value.args) != 1:
            continue
        argument = value.args[0]
        if not isinstance(argument, (ast.Set, ast.List, ast.Tuple)):
            continue
        strings: List[str] = []
        for item in argument.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                break
            strings.append(item.value)
        else:
            found[targets[0].id] = frozenset(strings)
    return found


def _full_semantic_key_check() -> Dict[str, Any]:
    try:
        source = PROTOCOL_SOURCE_PATH.read_text(encoding="utf-8")
        assignments = _extract_frozenset_assignments(source)
        top = assignments.get("OUTPUT_TOP_KEYS", frozenset())
        topic = assignments.get("OUTPUT_TOPIC_KEYS", frozenset())
        prompt_mentions = all(field in source for field in EXPECTED_OUTPUT_FIELDS)
        source_read = True
    except (OSError, SyntaxError, UnicodeError):
        source = ""
        top = frozenset()
        topic = frozenset()
        prompt_mentions = False
        source_read = False
    return {
        "source_read_without_import": source_read,
        "top_level_fields_exact": top == {"topics"},
        "topic_fields_exact": topic == EXPECTED_OUTPUT_FIELDS - {"topics"},
        "prompt_mentions_full_descriptive_fields": prompt_mentions,
        "full_semantic_keys": sorted(EXPECTED_OUTPUT_FIELDS),
        "full_semantic_keys_ok": bool(
            source_read
            and top == {"topics"}
            and topic == EXPECTED_OUTPUT_FIELDS - {"topics"}
            and prompt_mentions
        ),
    }


def _model_protocol_check(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    documents = (manifest, aggregate, cost, diagnostic)
    manifest_provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
    aggregate_provider = aggregate.get("provider") if isinstance(aggregate.get("provider"), Mapping) else {}
    diagnostic_provider = diagnostic.get("provider") if isinstance(diagnostic.get("provider"), Mapping) else {}
    model_values = [manifest_provider.get("model"), aggregate_provider.get("model"), diagnostic_provider.get("model"), ledger_rows[0].get("model") if ledger_rows else None]
    source_values = [manifest_provider.get("source"), aggregate_provider.get("source"), diagnostic_provider.get("source")]
    provider_values = [manifest_provider.get("id"), aggregate_provider.get("id"), ledger_rows[0].get("provider") if ledger_rows else None]
    protocol_values = [manifest.get("protocol_version"), aggregate.get("protocol_version"), diagnostic.get("protocol_version"), ledger_rows[0].get("protocol") if ledger_rows else None]
    prompt_values = [manifest.get("prompt_version"), aggregate.get("prompt_version"), diagnostic.get("prompt_version")]
    cache_values = [manifest.get("cache_version"), aggregate.get("cache_version"), diagnostic.get("cache_version")]
    return {
        "model_values": sorted({str(value) for value in model_values if isinstance(value, str)}),
        "model_unchanged": model_values == [EXPECTED_MODEL] * 4,
        "source_values": sorted({str(value) for value in source_values if isinstance(value, str)}),
        "source_unchanged": source_values == [EXPECTED_SOURCE] * 3,
        "provider_values": sorted({str(value) for value in provider_values if isinstance(value, str)}),
        "provider_unchanged": provider_values == [EXPECTED_PROVIDER] * 3,
        "protocol_values": sorted({str(value) for value in protocol_values if isinstance(value, str)}),
        "protocol_unchanged": protocol_values == [EXPECTED_PROTOCOL] * 4,
        "prompt_values": sorted({str(value) for value in prompt_values if isinstance(value, str)}),
        "prompt_unchanged": prompt_values == [EXPECTED_PROMPT] * 3,
        "cache_values": sorted({str(value) for value in cache_values if isinstance(value, str)}),
        "cache_unchanged": cache_values == [EXPECTED_CACHE] * 3,
        "all_documents_present": all(isinstance(document, Mapping) for document in documents),
    }


def _response_format_and_thinking_check(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
) -> Dict[str, Any]:
    documents = (manifest, aggregate, cost, diagnostic)
    format_values = [value for value in _values(documents, {"response_format_mode"}) if isinstance(value, str)]
    sent_values = [value for value in _values(documents, {"response_format_sent"}) if isinstance(value, bool)]
    thinking_values = [value for value in _values(documents, {"thinking_disabled"}) if isinstance(value, bool)]
    manifest_extra = manifest.get("extra_body") if isinstance(manifest.get("extra_body"), Mapping) else {}
    aggregate_extra = aggregate.get("extra_body") if isinstance(aggregate.get("extra_body"), Mapping) else {}
    extra_body = {
        "manifest_aggregate_equal": manifest_extra == aggregate_extra,
        "field_names": aggregate_extra.get("field_names"),
        "per_call": aggregate_extra.get("per_call"),
        "sent": aggregate_extra.get("sent"),
        "global_settings_mutated": aggregate_extra.get("global_settings_mutated"),
        "value_shape": aggregate_extra.get("value_shape"),
    }
    return {
        "response_format_modes": sorted(set(format_values)),
        "response_format_sent_values": sorted(set(sent_values)),
        "omitted_and_not_sent": bool(
            len(format_values) >= 2
            and all(value == EXPECTED_RESPONSE_FORMAT for value in format_values)
            and len(sent_values) >= 2
            and all(value is False for value in sent_values)
        ),
        "thinking_values": sorted(set(thinking_values)),
        "thinking_true_everywhere": len(thinking_values) >= 4 and all(value is True for value in thinking_values),
        "extra_body": extra_body,
        "thinking_per_call_disabled": bool(
            extra_body["manifest_aggregate_equal"]
            and extra_body["field_names"] == ["thinking"]
            and extra_body["per_call"] is True
            and extra_body["sent"] is True
            and extra_body["global_settings_mutated"] is False
            and extra_body["value_shape"] == "disabled"
        ),
    }


def _strict_and_topic_check(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
) -> Dict[str, Any]:
    strict = diagnostic.get("strict_result") if isinstance(diagnostic.get("strict_result"), Mapping) else {}
    aggregate_strict = aggregate.get("strict_result") if isinstance(aggregate.get("strict_result"), Mapping) else {}
    response = diagnostic.get("response_diagnostics") if isinstance(diagnostic.get("response_diagnostics"), Mapping) else {}
    aggregate_response = aggregate.get("response_diagnostics") if isinstance(aggregate.get("response_diagnostics"), Mapping) else {}
    candidate = diagnostic.get("diagnostic_candidate") if isinstance(diagnostic.get("diagnostic_candidate"), Mapping) else {}
    aggregate_candidate = aggregate.get("diagnostic_candidate") if isinstance(aggregate.get("diagnostic_candidate"), Mapping) else {}
    strict_rows = (strict, aggregate_strict, response)
    parse_ok = bool(strict_rows) and all(row.get("strict_parse_code") == "ok" for row in strict_rows)
    validation_ok = bool(strict_rows) and all(row.get("strict_validation_code") == "ok" for row in strict_rows)
    complete_ok = bool(strict_rows) and all(row.get("strict_complete") is True for row in strict_rows)
    accepted_ok = strict.get("accepted_for_complete") is True and aggregate_strict.get("accepted_for_complete") is True
    candidate_ok = bool(
        candidate
        and aggregate_candidate == candidate
        and candidate.get("present") is True
        and candidate.get("json_object_candidate") is True
        and candidate.get("schema_valid") is True
        and candidate.get("diagnostic_only") is True
        and candidate.get("accepted_for_complete") is False
    )
    candidate_separate = bool(
        strict.get("diagnostic_candidate_separate") is True
        and aggregate_strict.get("diagnostic_candidate_separate") is True
    )
    request = aggregate.get("request") if isinstance(aggregate.get("request"), Mapping) else {}
    rule_values = [manifest.get("topic_limit_rule"), aggregate.get("topic_limit_rule")]
    topic_values = [strict.get("topic_count"), aggregate_strict.get("topic_count"), response.get("topic_count")]
    limit_values = [strict.get("topic_limit"), aggregate_strict.get("topic_limit"), response.get("topic_limit"), request.get("topic_limit")]
    return {
        "parse_all_ok": parse_ok,
        "schema_validation_all_ok": validation_ok,
        "strict_complete_all_true": complete_ok,
        "accepted_for_complete_true": accepted_ok,
        "diagnostic_candidate_valid_but_not_accepted": candidate_ok,
        "diagnostic_candidate_separate": candidate_separate,
        "candidate_hash_matches_output": candidate.get("candidate_sha256") == response.get("output_sha256"),
        "topic_count_values": topic_values,
        "topic_limit_values": limit_values,
        "topic_count_one": topic_values == [1, 1, 1],
        "topic_limit_one": limit_values == [1, 1, 1, 1],
        "topic_rule_exact": rule_values == [EXPECTED_TOPIC_RULE, EXPECTED_TOPIC_RULE],
        "request_shape_counts": {
            "message_count": request.get("message_count"),
            "primary_count": request.get("primary_count"),
            "candidate_count": request.get("candidate_count"),
        },
        "request_shape_ok": request.get("message_count") == 2 and request.get("primary_count") == 1 and request.get("candidate_count") == 1,
        "strict_health_complete": bool(parse_ok and validation_ok and complete_ok and accepted_ok and candidate_separate),
        "aggregate_response_matches_diagnostic": aggregate_response == response,
    }


def _token_latency_check(aggregate: Mapping[str, Any], cost: Mapping[str, Any], diagnostic: Mapping[str, Any]) -> Dict[str, Any]:
    request = aggregate.get("request") if isinstance(aggregate.get("request"), Mapping) else {}
    response = diagnostic.get("response_diagnostics") if isinstance(diagnostic.get("response_diagnostics"), Mapping) else {}
    numbers_ok = all(isinstance(response.get(key), (int, float)) and not isinstance(response.get(key), bool) for key in ("input_tokens", "output_tokens", "latency_ms", "reasoning_length"))
    cost_equal = all(cost.get(key) == response.get(key) for key in ("input_tokens", "output_tokens", "latency_ms"))
    result = {
        "input_proxy_present": isinstance(request.get("input_token_proxy"), int),
        "input_proxy_within_limit": isinstance(request.get("input_token_proxy"), int) and request.get("input_token_proxy") <= MAX_INPUT_TOKEN_PROXY,
        "input_tokens_within_limit": numbers_ok and 0 <= response.get("input_tokens", -1) <= MAX_INPUT_TOKEN_PROXY,
        "output_limit_declared_400": cost.get("max_output_tokens") == MAX_OUTPUT_TOKENS,
        "output_tokens_within_limit": numbers_ok and 0 <= response.get("output_tokens", -1) <= MAX_OUTPUT_TOKENS,
        "latency_within_limit": numbers_ok and 0 < float(response.get("latency_ms", 0)) <= MAX_LATENCY_MS,
        "finish_stop": response.get("finish_reason") == "stop",
        "reasoning_zero": response.get("reasoning_length") == 0,
        "cost_metrics_equal": cost_equal,
        "output_hash_shape": bool(HEX64.fullmatch(str(response.get("output_sha256") or ""))),
    }
    result["within_limits"] = bool(all(result.values()))
    return result


def _diagnostic_complete_consistency(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    aggregate_response = aggregate.get("response_diagnostics") if isinstance(aggregate.get("response_diagnostics"), Mapping) else {}
    diagnostic_response = diagnostic.get("response_diagnostics") if isinstance(diagnostic.get("response_diagnostics"), Mapping) else {}
    aggregate_strict = aggregate.get("strict_result") if isinstance(aggregate.get("strict_result"), Mapping) else {}
    diagnostic_strict = diagnostic.get("strict_result") if isinstance(diagnostic.get("strict_result"), Mapping) else {}
    fields = ("status", "success", "diagnostic_only", "synthetic_only", "health_complete", "provider_calls", "retry_count", "development_input_read", "private_input_read", "frozen_read", "gold_loaded", "production_state_written")
    manifest_values_equal = all(manifest.get(field) == aggregate.get(field) for field in fields if field in manifest or field in aggregate)
    return {
        "manifest_aggregate_status_flags_equal": manifest_values_equal,
        "status_available": manifest.get("status") == aggregate.get("status") == "available",
        "success_true": manifest.get("success") is True and aggregate.get("success") is True,
        "health_complete_true": manifest.get("health_complete") is True and aggregate.get("health_complete") is True,
        "strict_maps_equal": diagnostic_strict == aggregate_strict,
        "response_maps_equal": diagnostic_response == aggregate_response,
        "candidate_maps_equal": diagnostic.get("diagnostic_candidate") == aggregate.get("diagnostic_candidate"),
        "cost_provider_metrics_equal": bool(
            cost.get("provider_calls") == aggregate.get("provider_calls") == manifest.get("provider_calls")
            and cost.get("retry_count") == aggregate.get("retry_count") == manifest.get("retry_count")
            and all(cost.get(key) == diagnostic_response.get(key) for key in ("input_tokens", "output_tokens", "latency_ms"))
        ),
        "ledger_complete_and_errorless": bool(len(ledger_rows) == 1 and ledger_rows[0].get("status") == "complete" and ledger_rows[0].get("error_code") is None),
        "errors_empty": len(errors) == 0 and aggregate.get("errors") == {"count": 0, "codes": []},
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
    ledger_rows = _load_jsonl(artifact_dir / "ledger.private.jsonl")
    errors = _load_jsonl(artifact_dir / "errors.private.jsonl")
    documents = {"manifest": manifest, "aggregate": aggregate, "cost": cost, "diagnostic": diagnostic}
    document_list = tuple(documents.values())

    privacy_hits = _privacy_hits(documents, [*ledger_rows, *errors])
    privacy = {
        "body_key_hits": privacy_hits["body"],
        "identity_key_hits": privacy_hits["identity"],
        "reasoning_key_hits": privacy_hits["reasoning"],
        "secret_key_hits": privacy_hits["secret"],
        "body_free": not any(privacy_hits.values()),
    }

    artifact_hashes = _artifact_hash_check(artifact_dir, manifest, actual)
    settings = _settings_check(documents, manifest)
    durable = _durable_ledger_check(manifest, ledger_rows)
    model_protocol = _model_protocol_check(manifest, aggregate, cost, diagnostic, ledger_rows)
    format_thinking = _response_format_and_thinking_check(manifest, aggregate, cost, diagnostic)
    strict_topic = _strict_and_topic_check(manifest, aggregate, diagnostic)
    token_latency = _token_latency_check(aggregate, cost, diagnostic)
    consistency = _diagnostic_complete_consistency(manifest, aggregate, cost, diagnostic, ledger_rows, errors)
    semantic_keys = _full_semantic_key_check()

    authorization = manifest.get("authorization") if isinstance(manifest.get("authorization"), Mapping) else {}
    snapshot = manifest.get("authorization_ledger") if isinstance(manifest.get("authorization_ledger"), Mapping) else {}
    binding = snapshot.get("binding") if isinstance(snapshot.get("binding"), Mapping) else {}
    ledger_shape = {
        "authorization_id": authorization.get("authorization_id"),
        "authorization_id_expected": authorization.get("authorization_id") == EXPECTED_AUTHORIZATION,
        "namespace_expected": authorization.get("artifact_namespace") == EXPECTED_NAMESPACE,
        "max_calls_one": authorization.get("max_calls") == 1,
        "binding_snapshot_equal": bool(snapshot.get("authorization_id") == EXPECTED_AUTHORIZATION and binding.get("authorization_id") == EXPECTED_AUTHORIZATION),
        "calls_used_one": snapshot.get("calls_used") == 1,
        "calls_remaining_zero": snapshot.get("calls_remaining") == 0,
        "reservation_count_one": snapshot.get("reservation_count") == 1,
        "status_complete_one": snapshot.get("status_counts") == {"complete": 1},
        "rejection_count_zero": snapshot.get("rejection_count") == 0,
        "body_free": snapshot.get("body_free") is True,
        "schema_expected": snapshot.get("schema_version") == EXPECTED_LEDGER_SCHEMA and snapshot.get("ledger_schema_version") == "call_authorization_ledger_v1",
    }
    ledger_shape["global_snapshot_ok"] = bool(all(ledger_shape[key] for key in ("authorization_id_expected", "namespace_expected", "max_calls_one", "binding_snapshot_equal", "calls_used_one", "calls_remaining_zero", "reservation_count_one", "status_complete_one", "rejection_count_zero", "body_free", "schema_expected")))

    manifest_scope = {
        "artifact_version": manifest.get("artifact_version") == ARTIFACT_VERSION,
        "local_day": manifest.get("local_day") == LOCAL_DAY,
        "split_synthetic": manifest.get("split") == "synthetic",
        "status_available": manifest.get("status") == "available",
        "success_true": manifest.get("success") is True,
        "health_complete_true": manifest.get("health_complete") is True,
        "diagnostic_only": manifest.get("diagnostic_only") is True,
        "synthetic_only": manifest.get("synthetic_only") is True,
        "production_blocked": manifest.get("production_blocked") is True,
        "development_unread": manifest.get("development_input_read") is False and manifest.get("private_input_read") is False and manifest.get("development_calls") == 0,
        "frozen_unread": manifest.get("frozen_read") is False,
        "gold_unread": manifest.get("gold_loaded") is False,
        "production_not_written": manifest.get("production_state_written") is False,
        "stage_a_development_false": manifest.get("stage_a_development") is False,
        "stage_b_false": manifest.get("stage_b_pilot") is False,
        "stage_c_false": manifest.get("stage_c_pilot") is False,
    }

    calls = {
        "provider_called": manifest.get("provider_called") is True,
        "provider_calls_one": manifest.get("provider_calls") == 1,
        "provider_limit_one": manifest.get("provider_call_limit") == 1,
        "aggregate_calls_one": aggregate.get("provider_calls") == 1,
        "cost_calls_one": cost.get("provider_calls") == 1,
        "ledger_row_one": len(ledger_rows) == 1,
        "errors_zero": len(errors) == 0,
        "retry_zero": manifest.get("retry_count") == aggregate.get("retry_count") == cost.get("retry_count") == 0,
        "no_rejection": snapshot.get("rejection_count") == 0,
    }
    calls["exactly_one_no_retry_rejection"] = bool(all(calls.values()))

    audit_checks = {
        "required_files_and_hashes": bool(not missing and not unexpected and artifact_hashes["all_match"]),
        "artifact_scope_and_health_status": bool(all(manifest_scope.values())),
        "authorization_snapshot_one_of_one": ledger_shape["global_snapshot_ok"],
        "global_durable_ledger_one_of_one": durable["global_one_of_one"],
        "durable_binding_and_rows_hashes": bool(durable["binding_match"] and durable["reservation_match"] and durable["authorization_hash_match"] and durable["ledger_rows_hash_match"]),
        "exactly_one_provider_call_no_retry_rejection": calls["exactly_one_no_retry_rejection"],
        "model_protocol_source_unchanged": bool(all(model_protocol[key] for key in ("all_documents_present", "model_unchanged", "source_unchanged", "provider_unchanged", "protocol_unchanged", "prompt_unchanged", "cache_unchanged"))),
        "full_semantic_keys": semantic_keys["full_semantic_keys_ok"],
        "response_format_omitted": format_thinking["omitted_and_not_sent"],
        "thinking_disabled_per_call": format_thinking["thinking_per_call_disabled"],
        "strict_parse_schema_complete": strict_topic["strict_health_complete"],
        "topic_count_and_limit_one": bool(strict_topic["topic_count_one"] and strict_topic["topic_limit_one"] and strict_topic["topic_rule_exact"] and strict_topic["request_shape_ok"]),
        "tokens_finish_latency_within_limits": token_latency["within_limits"],
        "settings_unchanged": settings["settings_unchanged"],
        "diagnostic_complete_consistent": bool(all(consistency.values())),
        "body_free": privacy["body_free"],
    }
    audit_ok = bool(all(audit_checks.values()))

    if audit_ok:
        next_step = {
            "authorization_id": NEXT_AUTHORIZATION,
            "allow_stage_a_development": True,
            "allow_k10_v2_complete_pages_only": True,
            "max_pages": 5,
            "global_max_calls": 5,
            "per_page_max_calls": 1,
            "per_page_retries": 0,
            "reuse_model": EXPECTED_MODEL,
            "reuse_protocol": EXPECTED_PROTOCOL,
            "response_format_mode": EXPECTED_RESPONSE_FORMAT,
            "thinking_disabled": True,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "accept_complete_only": True,
            "failure_policy": "pending_preserve_and_stop",
            "allow_stage_b": False,
            "allow_stage_c": False,
            "allow_frozen": False,
            "allow_production": False,
            "new_persistent_global_ledger_required": True,
            "reason": "K24 health passed; authorize only five K10_v2 complete pages under one new global five-call budget",
        }
    else:
        next_step = {
            "authorization_id": NEXT_AUTHORIZATION,
            "allow_stage_a_development": False,
            "allow_k10_v2_complete_pages_only": False,
            "max_pages": 0,
            "global_max_calls": 0,
            "per_page_max_calls": 0,
            "per_page_retries": 0,
            "allow_stage_b": False,
            "allow_stage_c": False,
            "allow_frozen": False,
            "allow_production": False,
            "new_authorization_required": True,
            "reason": "K24 health audit failed; do not authorize development or reuse the diagnostic result",
        }

    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_status": manifest.get("status", "unknown"),
        "audit_status": "pass" if audit_ok else "fail",
        "health_complete": bool(audit_ok),
        "audit_checks": audit_checks,
        "missing_files": missing,
        "unexpected_root_files": unexpected,
        "artifact_hashes": artifact_hashes,
        "privacy": privacy,
        "call_counts": calls,
        "ledger_snapshot": ledger_shape,
        "durable_ledger": durable,
        "model_protocol_source": model_protocol,
        "semantic_key_contract": semantic_keys,
        "response_format_and_thinking": format_thinking,
        "strict_topic_contract": strict_topic,
        "tokens_finish_latency": token_latency,
        "settings": settings,
        "diagnostic_complete_consistency": consistency,
        "errors": {"rows": len(errors), "codes": sorted({str(row.get("error_code")) for row in errors})},
        "scope": {
            "audit_provider_calls": 0,
            "audit_development_input_read": False,
            "audit_private_input_read": False,
            "audit_frozen_read": False,
            "audit_production_state_written": False,
            "artifact_synthetic_only": True,
            "runner_imported": False,
        },
        "next_step": next_step,
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
    for name in (
        "authorization_id",
        "allow_stage_a_development",
        "allow_k10_v2_complete_pages_only",
        "max_pages",
        "global_max_calls",
        "per_page_max_calls",
        "per_page_retries",
        "allow_stage_b",
        "allow_stage_c",
        "allow_frozen",
        "allow_production",
    ):
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
    parser = argparse.ArgumentParser(description="Independent K25 compact Stage-A protocol health audit")
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
