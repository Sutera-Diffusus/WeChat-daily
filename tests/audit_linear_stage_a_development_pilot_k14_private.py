"""Independent, body-free K14 audit for both development-pilot attempts.

The K14 runner is deliberately not imported here.  This side-car reads the
final artifact, the moved first-attempt artifact, the already audited K13
health summary, and the K10v2 selection maps.  It never calls a provider,
opens a frozen path, or reads provider request/response bodies into the audit
output.

K14 has a two-run ledger split in this workspace.  The final artifact and the
moved first attempt each contain five one-shot development calls.  The audit
therefore treats the two directories as one logical protocol run for budget
accounting: ten observed calls against the K13-authorized total of five is a
hard BUDGET_LEDGER_SPLIT / UNAUTHORIZED_RERUN failure.  All ten responses are
incomplete at the 400-token output ceiling, so semantic grouping is explicitly
N/A and cannot authorize Stage B or Stage C.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "linear_stage_a_development_pilot_v1"
AUDIT_SCHEMA_VERSION = "linear_stage_a_development_pilot_k14_audit_v1"
REPORT_SCHEMA_VERSION = "linear_stage_a_development_pilot_report_v1"
RUNNER_SCHEMA_VERSION = "linear_stage_a_development_pilot_runner_v1"
HEALTH_VERSION = "linear_stage_a_protocol_health_v2"
K10_VERSION = "linear_stage_packet_development_v2"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINAL_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
DEFAULT_ATTEMPT_DIR = ROOT / "tmp" / "linear_stage_a_development_pilot_v1_attempt_20260828"
DEFAULT_K13_AUDIT = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / HEALTH_VERSION
    / "audit"
    / "audit_summary.private.json"
)
DEFAULT_K10_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / K10_VERSION
SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"

EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_SOURCE = "deepseek-openai-compatible"
EXPECTED_RESPONSE_FORMAT = "omitted"
EXPECTED_OUTPUT_LIMIT = 400
AUTHORIZED_DEVELOPMENT_CALLS = 5
PER_PAGE_CALL_LIMIT = 1
RETRY_LIMIT = 0
EXPECTED_STRATA = (
    "greeting_new_topic",
    "no_reply",
    "pronoun_person_object_state",
    "topic_shift",
    "candidate_competition",
)
ALLOWED_RELATIONS = frozenset(
    {
        "same_topic",
        "continuation",
        "answer",
        "question_followup",
        "request_followup",
        "contrast",
        "new_topic",
        "unrelated",
        "unknown",
    }
)
REQUIRED_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "ledger.private.jsonl",
    "selection.private.jsonl",
    "decisions.private.jsonl",
    "errors.private.jsonl",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# These checks are key-name based.  Values are never copied into the report.
BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "completion",
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
        "response",
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
REASONING_KEYS = frozenset({"analysis", "chain_of_thought", "completion", "reasoning", "reasoning_content", "thoughts"})
IDENTITY_KEYS = frozenset(
    {
        "account_id",
        "candidate_id",
        "candidate_handle",
        "chat_id",
        "evidence_id",
        "evidence_handle",
        "message_id",
        "message_handle",
        "page_id",
        "root_id",
        "source_packet_id",
        "thread_id",
    }
)


class AuditError(ValueError):
    """Fail-closed audit input error."""


def _safe_path(path: Path) -> Path:
    result = Path(path).expanduser().resolve()
    if {"frozen", "frozen_test", "frozen-test"} & {part.casefold() for part in result.parts}:
        raise AuditError("FROZEN_PATH_READ_FORBIDDEN")
    return result


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("INVALID_JSON:%s" % path.name) from exc
    if not isinstance(value, Mapping):
        raise AuditError("JSON_OBJECT_REQUIRED:%s" % path.name)
    return {str(key): child for key, child in value.items()}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AuditError("UNREADABLE_JSONL:%s" % path.name) from exc
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError("INVALID_JSONL:%s:%d" % (path.name, number)) from exc
        if not isinstance(value, Mapping):
            raise AuditError("JSONL_OBJECT_REQUIRED:%s:%d" % (path.name, number))
        rows.append({str(key): child for key, child in value.items()})
    return rows


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _opaque(value: Any, namespace: str) -> str:
    if isinstance(value, Mapping):
        raw = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = json.dumps(list(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    else:
        raw = "<missing>" if value is None else str(value)
    return "%s_%s" % (namespace, hashlib.sha256((namespace + "|" + raw).encode("utf-8")).hexdigest()[:24])


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], (), {})


def _iter_nodes(value: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            yield child_path, child
            yield from _iter_nodes(child, child_path)
    elif isinstance(value, (list, tuple)):
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


def _sensitive_hits(value: Any) -> Dict[str, List[str]]:
    hits = {"body": [], "secret": [], "reasoning": []}
    for path, child in _iter_nodes(value):
        key = _key_name(path)
        if not _nonempty(child):
            continue
        # extra_body is a protocol metadata object, not a response body.
        # Keep suffix checks narrow and rely on explicit *_body names in
        # BODY_KEYS for content-bearing fields.
        if key in BODY_KEYS or key.endswith(("_text", "_transcript")):
            hits["body"].append(path)
        if key in SECRET_KEYS or key.endswith(("_secret", "_token", "_password")):
            hits["secret"].append(path)
        if key in REASONING_KEYS or key.endswith(("_reasoning", "_thoughts")):
            hits["reasoning"].append(path)
    return hits


def _raw_reasoning_stats(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    raw_flags: List[bool] = []
    raw_true = 0
    reasoning_lengths: List[float] = []
    reasoning_nonzero = 0
    for document in list(documents.values()) + list(ledger):
        for _path, value in _key_values(document, {"raw_response_saved", "raw_reasoning_saved"}):
            if isinstance(value, bool):
                raw_flags.append(value)
                raw_true += int(value)
        for _path, value in _key_values(document, {"reasoning_length"}):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                reasoning_lengths.append(float(value))
                reasoning_nonzero += int(value != 0)
    return {
        "raw_flags_observed": len(raw_flags),
        "raw_true_count": raw_true,
        "raw_zero": bool(raw_flags) and raw_true == 0,
        "reasoning_lengths_observed": len(reasoning_lengths),
        "reasoning_nonzero_count": reasoning_nonzero,
        "reasoning_zero": bool(reasoning_lengths) and reasoning_nonzero == 0,
    }


def _scope_of_handle(value: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(value, str) or "|" not in value:
        return None
    prefix = value.split("|", 1)[0]
    if "/" not in prefix:
        return None
    account, chat = prefix.split("/", 1)
    return (account, chat) if account and chat else None


def _handle_values(value: Any) -> Iterator[Tuple[str, str]]:
    if isinstance(value, Mapping):
        for key in (
            "message_handles",
            "candidate_handles",
            "evidence_handles",
            "message_handle",
            "candidate_handle",
            "evidence_handle",
        ):
            child = value.get(key)
            if isinstance(child, str):
                yield key, child
            elif isinstance(child, (list, tuple)):
                for item in child:
                    if isinstance(item, str):
                        yield key, item
        for child in value.values():
            yield from _handle_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _handle_values(child)


def _handle_check(documents: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    values: List[Tuple[str, str]] = []
    for document in documents.values():
        values.extend(_handle_values(document))
    for row in ledger:
        values.extend(_handle_values(row.get("selected_opaque_refs", {})))
    invalid = 0
    scopes: Set[Tuple[str, str]] = set()
    row_scope_checks: List[bool] = []
    kinds: Dict[str, int] = {}
    for _key, handle in values:
        scope = _scope_of_handle(handle)
        pieces = handle.split("|", 2)
        kind = pieces[1] if len(pieces) > 1 else ""
        if scope is None or kind not in {"message", "candidate", "evidence"}:
            invalid += 1
        else:
            scopes.add(scope)
            kinds[kind] = kinds.get(kind, 0) + 1
    for row in ledger:
        selected = row.get("selected_opaque_refs") if isinstance(row.get("selected_opaque_refs"), Mapping) else {}
        declared = selected.get("scope") if isinstance(selected, Mapping) else None
        declared_pair: Optional[Tuple[str, str]] = None
        if isinstance(declared, Mapping):
            account = declared.get("account_id", declared.get("account"))
            chat = declared.get("chat_id", declared.get("chat"))
            if account not in (None, "") and chat not in (None, ""):
                declared_pair = (str(account), str(chat))
        row_handles = [handle for _key, handle in _handle_values(selected)]
        row_scopes = {_scope_of_handle(handle) for handle in row_handles}
        row_scope_checks.append(bool(declared_pair and row_scopes and None not in row_scopes and row_scopes == {declared_pair}))
    return {
        "observed_handle_count": len(values),
        "valid_handle_count": len(values) - invalid,
        "invalid_handle_count": invalid,
        "scope_count": len(scopes),
        # K14 intentionally samples pages from several chat scopes.  Scope
        # consistency is therefore checked within each page request, not
        # across the five independent selected pages.
        "scope_consistent": bool(row_scope_checks) and all(row_scope_checks),
        "scope_rows_checked": len(row_scope_checks),
        "scope_rows_ok": sum(row_scope_checks),
        "format_ok": bool(values) and invalid == 0,
        "kind_counts": kinds,
    }


def _hash_check(artifact_dir: Path, manifest: Mapping[str, Any]) -> Dict[str, Any]:
    recorded = manifest.get("artifact_hashes")
    if not isinstance(recorded, Mapping):
        return {"recorded": False, "hashed_file_count": 0, "match_count": 0, "failure_count": 0, "all_match": False}
    matches = 0
    failures: List[str] = []
    for raw_name, raw_hash in recorded.items():
        name = str(raw_name)
        actual = _sha256_file(artifact_dir / name)
        if not HEX64.fullmatch(str(raw_hash or "")) or actual != str(raw_hash):
            failures.append(name)
        else:
            matches += 1
    return {
        "recorded": True,
        "hashed_file_count": len(recorded),
        "match_count": matches,
        "failure_count": len(failures),
        "all_match": bool(recorded) and not failures,
    }


def _load_artifact(root: Path, label: str) -> Dict[str, Any]:
    root = _safe_path(root)
    if not root.is_dir():
        raise AuditError("ARTIFACT_DIRECTORY_MISSING:%s" % label)
    actual = {path.name for path in root.iterdir() if path.is_file()}
    missing = sorted(set(REQUIRED_FILES) - actual)
    if missing:
        raise AuditError("REQUIRED_ARTIFACT_FILE_MISSING:%s:%s" % (label, ",".join(missing)))
    manifest = _load_json(root / "manifest.private.json")
    aggregate = _load_json(root / "aggregate.private.json")
    cost = _load_json(root / "cost.private.json")
    ledger = _load_jsonl(root / "ledger.private.jsonl")
    selection = _load_jsonl(root / "selection.private.jsonl")
    decisions = _load_jsonl(root / "decisions.private.jsonl")
    errors = _load_jsonl(root / "errors.private.jsonl")
    documents = {"manifest": manifest, "aggregate": aggregate, "cost": cost}
    return {
        "label": label,
        "root": root,
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "ledger": ledger,
        "selection": selection,
        "decisions": decisions,
        "errors": errors,
        "documents": documents,
        "actual_files": sorted(actual),
        "hashes": _hash_check(root, manifest),
        "privacy": _sensitive_hits({**documents, "ledger": ledger, "selection": selection, "decisions": decisions, "errors": errors}),
        "raw": _raw_reasoning_stats(documents, ledger),
        "handles": _handle_check(documents, ledger),
    }


def _protocol_check(artifacts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    models: List[str] = []
    sources: List[str] = []
    formats: List[str] = []
    thinking: List[bool] = []
    output_limits: List[float] = []
    per_page_limits: List[float] = []
    extra_bodies: List[Mapping[str, Any]] = []
    response_formats: List[Mapping[str, Any]] = []
    for artifact in artifacts:
        manifest = artifact["manifest"]
        aggregate = artifact["aggregate"]
        cost = artifact["cost"]
        provider_values: List[Any] = [
            manifest.get("provider", {}),
            aggregate.get("provider", {}),
            manifest,
            aggregate,
            cost,
        ] + list(artifact["ledger"])
        for document in provider_values:
            if not isinstance(document, Mapping):
                continue
            for key in ("model", "model_id"):
                if isinstance(document.get(key), str) and document.get(key):
                    models.append(str(document[key]))
            if isinstance(document.get("source"), str) and document.get("source"):
                sources.append(str(document["source"]))
            if isinstance(document.get("response_format_mode"), str) and document.get("response_format_mode"):
                formats.append(str(document["response_format_mode"]))
            if isinstance(document.get("thinking_disabled"), bool):
                thinking.append(bool(document["thinking_disabled"]))
            if isinstance(document.get("max_output_tokens"), (int, float)) and not isinstance(document.get("max_output_tokens"), bool):
                output_limits.append(float(document["max_output_tokens"]))
            if isinstance(document.get("per_page_call_limit"), (int, float)) and not isinstance(document.get("per_page_call_limit"), bool):
                per_page_limits.append(float(document["per_page_call_limit"]))
        for document in (manifest, aggregate):
            extra = document.get("extra_body")
            if isinstance(extra, Mapping):
                extra_bodies.append(extra)
            response = document.get("response_format")
            if isinstance(response, Mapping):
                response_formats.append(response)
    extra_ok = bool(extra_bodies) and all(
        body.get("sent") is True
        and body.get("per_call") is True
        and body.get("global_settings_mutated") is False
        and body.get("value_shape") == "disabled"
        and body.get("field_names") == ["thinking"]
        for body in extra_bodies
    )
    response_ok = bool(response_formats) and all(
        body.get("mode") == EXPECTED_RESPONSE_FORMAT
        and body.get("sent") is False
        and body.get("predeclared") is True
        for body in response_formats
    )
    return {
        "models_observed": sorted(set(models)),
        "sources_observed": sorted(set(sources)),
        "response_formats_observed": sorted(set(formats)),
        "model_ok": bool(models) and set(models) == {EXPECTED_MODEL},
        "source_ok": bool(sources) and set(sources) == {EXPECTED_SOURCE},
        "response_format_ok": bool(formats) and set(formats) == {EXPECTED_RESPONSE_FORMAT} and response_ok,
        "thinking_disabled_ok": bool(thinking) and all(thinking) and extra_ok,
        "extra_body_ok": extra_ok,
        "response_format_record_ok": response_ok,
        "output_limit_ok": bool(output_limits) and set(output_limits) == {float(EXPECTED_OUTPUT_LIMIT)},
        "per_page_limit_ok": bool(per_page_limits) and set(per_page_limits) == {float(PER_PAGE_CALL_LIMIT)},
        "max_output_tokens": int(max(output_limits)) if output_limits else 0,
    }


def _call_and_retry_check(artifacts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rounds: Dict[str, Dict[str, Any]] = {}
    all_rows: List[Mapping[str, Any]] = []
    for artifact in artifacts:
        rows = artifact["ledger"]
        provider_rows = [row for row in rows if row.get("provider_call") is True]
        page_counts: Dict[str, int] = {}
        for row in provider_rows:
            page = str(row.get("page_id") or row.get("root_id") or "")
            page_counts[page] = page_counts.get(page, 0) + 1
        retry_values = [
            float(row["retry_count"])
            for row in rows
            if isinstance(row.get("retry_count"), (int, float)) and not isinstance(row.get("retry_count"), bool)
        ]
        finish_length = sum(
            isinstance(row.get("finish_reasons"), list)
            and "length" in [str(item).casefold() for item in row["finish_reasons"]]
            for row in rows
        )
        strict_incomplete = sum(
            row.get("strict_parse_code") != "ok"
            or row.get("strict_validation_code") != "ok"
            or row.get("unique_json_object_count") != 1
            for row in rows
        )
        rounds[str(artifact["label"])] = {
            "ledger_rows": len(rows),
            "provider_calls": len(provider_rows),
            "development_rows": sum(str(row.get("phase", "")).casefold() == "development" for row in rows),
            "health_rows": sum("health" in str(row.get("phase", "")).casefold() for row in rows),
            "per_page_max": max(page_counts.values(), default=0),
            "per_page_call_ok": bool(page_counts) and max(page_counts.values(), default=0) <= PER_PAGE_CALL_LIMIT,
            "page_count": len(page_counts),
            "retry_values": sorted(set(retry_values)),
            "retry_free": bool(retry_values) and all(value == RETRY_LIMIT for value in retry_values),
            "status_counts": {
                status: sum(str(row.get("status", "")).casefold() == status for row in rows)
                for status in ("complete", "pending")
            },
            "finish_length_rows": finish_length,
            "strict_incomplete_rows": strict_incomplete,
            "output_at_limit_rows": sum(row.get("output_tokens") == EXPECTED_OUTPUT_LIMIT for row in rows),
            "error_codes": sorted({str(row.get("error_code")) for row in rows if row.get("error_code")}),
        }
        all_rows.extend(rows)
    combined_page_attempts: Dict[str, int] = {}
    for row in all_rows:
        if row.get("provider_call") is True:
            key = str(row.get("page_id") or row.get("root_id") or "")
            combined_page_attempts[key] = combined_page_attempts.get(key, 0) + 1
    provider_calls = sum(data["provider_calls"] for data in rounds.values())
    return {
        "rounds": rounds,
        "combined_provider_calls": provider_calls,
        "authorized_total": AUTHORIZED_DEVELOPMENT_CALLS,
        "overage": max(0, provider_calls - AUTHORIZED_DEVELOPMENT_CALLS),
        "budget_ok": provider_calls <= AUTHORIZED_DEVELOPMENT_CALLS,
        "combined_ledger_rows": len(all_rows),
        "combined_health_rows": sum("health" in str(row.get("phase", "")).casefold() for row in all_rows),
        "combined_retry_free": all(data["retry_free"] for data in rounds.values()),
        "combined_per_page_max": max(combined_page_attempts.values(), default=0),
        "combined_page_attempts_max": max(combined_page_attempts.values(), default=0),
        "combined_page_attempts_unique_pages": len(combined_page_attempts),
    }


def _settings_check(artifacts: Sequence[Mapping[str, Any]], k13: Mapping[str, Any]) -> Dict[str, Any]:
    current = _sha256_file(SETTINGS_PATH)
    before: List[str] = []
    after: List[str] = []
    explicit: List[bool] = []
    for artifact in artifacts:
        for document in (artifact["manifest"], artifact["aggregate"]):
            if isinstance(document.get("settings_before_sha256"), str):
                before.append(document["settings_before_sha256"])
            if isinstance(document.get("settings_after_sha256"), str):
                after.append(document["settings_after_sha256"])
            if isinstance(document.get("settings_unchanged"), bool):
                explicit.append(document["settings_unchanged"])
    k13_settings = k13.get("settings") if isinstance(k13.get("settings"), Mapping) else {}
    k13_unchanged = k13_settings.get("settings_unchanged") is True
    shape_ok = bool(before) and bool(after) and all(bool(HEX64.fullmatch(value)) for value in before + after)
    equal = bool(before) and before == after and len(set(before)) == 1
    current_match = bool(current) and bool(before) and current == before[0]
    return {
        "settings_file_present": bool(current),
        "before_after_equal": equal,
        "current_hash_matches_recorded": current_match,
        "explicit_unchanged": bool(explicit) and all(explicit),
        "k13_settings_unchanged": k13_unchanged,
        "hash_shape_ok": shape_ok,
        "settings_unchanged": bool(equal and current_match and explicit and all(explicit) and k13_unchanged),
        "current_hash_ref": _opaque(current, "settings"),
    }


def _load_k13_audit(path: Path) -> Dict[str, Any]:
    path = _safe_path(path)
    report = _load_json(path)
    next_step = report.get("next_step") if isinstance(report.get("next_step"), Mapping) else {}
    checks = report.get("audit_checks") if isinstance(report.get("audit_checks"), Mapping) else {}
    return {
        "present": True,
        "artifact_ref": _opaque(_sha256_file(path), "k13"),
        "audit_status_pass": report.get("audit_status") == "pass",
        "health_complete": report.get("health_complete") is True,
        "settings": report.get("settings", {}),
        "next_step_ok": (
            next_step.get("allow_k10_v2_complete_development_pages") is True
            and next_step.get("max_k10_v2_complete_development_pages") == 5
            and next_step.get("protocol") == EXPECTED_RESPONSE_FORMAT
            and next_step.get("model") == EXPECTED_MODEL
            and next_step.get("thinking_disabled") is True
            and next_step.get("max_output_tokens") == EXPECTED_OUTPUT_LIMIT
            and next_step.get("per_page_provider_call_limit") == PER_PAGE_CALL_LIMIT
            and next_step.get("per_page_retry_limit") == RETRY_LIMIT
            and next_step.get("allow_stage_b_pilot") is False
            and next_step.get("allow_stage_c_pilot") is False
        ),
        "checks_all_pass": bool(checks) and all(value is True for value in checks.values()),
    }


def _load_k10_maps(root: Path) -> Dict[str, Any]:
    root = _safe_path(root)
    if not root.is_dir():
        raise AuditError("K10_ARTIFACT_DIRECTORY_MISSING")
    manifest = _load_json(root / "manifest.private.json")
    pages = _load_jsonl(root / "pages.private.jsonl")
    materialized = _load_jsonl(root / "materialized_map.private.jsonl")
    page_map = {str(row.get("page_id")): row for row in pages if row.get("page_id") not in (None, "")}
    material_map = {str(row.get("page_id")): row for row in materialized if row.get("page_id") not in (None, "")}
    return {
        "root": root,
        "manifest": manifest,
        "pages": page_map,
        "materialized": material_map,
        "artifact_ref": _opaque(_sha256_file(root / "manifest.private.json"), "k10"),
        "hashes": _hash_check(root, manifest),
        "manifest_ok": (
            manifest.get("artifact_version") == K10_VERSION
            and manifest.get("status") == "complete"
            and manifest.get("provider_called") is False
            and manifest.get("provider_calls") == 0
            and manifest.get("frozen_read") is False
        ),
    }


def _stratum_tokens(value: Any) -> Set[str]:
    if isinstance(value, Mapping):
        tokens: Set[str] = set()
        for key, child in value.items():
            if str(key).casefold() in {
                "categories",
                "category",
                "stratum",
                "strata",
                "selection_reason",
                "reason",
                "reason_codes",
            }:
                tokens.update(_stratum_tokens(child))
            elif isinstance(child, (Mapping, list, tuple)):
                tokens.update(_stratum_tokens(child))
        return tokens
    if isinstance(value, (list, tuple, set, frozenset)):
        result: Set[str] = set()
        for child in value:
            result.update(_stratum_tokens(child))
        return result
    if not isinstance(value, str):
        return set()
    compact = value.casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "greeting_new_topic": {"greeting_new_topic", "greeting", "opener", "conversation_opener"},
        "no_reply": {"no_reply", "noreply", "no_reply_continuity"},
        "pronoun_person_object_state": {
            "pronoun_person_object_state",
            "person_object_state",
            "pronoun_entity_state",
            "entity_state",
        },
        "topic_shift": {"topic_shift", "new_topic", "topic_change", "shift"},
        "candidate_competition": {"candidate_competition", "candidate_competition_stratum", "competition"},
    }
    result: Set[str] = set()
    for canonical, candidates in aliases.items():
        if compact in candidates or any(candidate in compact for candidate in candidates if len(candidate) > 5):
            result.add(canonical)
    return result


def _selection_check(artifacts: Sequence[Mapping[str, Any]], k10: Mapping[str, Any]) -> Dict[str, Any]:
    per_round: Dict[str, Any] = {}
    all_page_refs: Dict[str, Set[str]] = {}
    all_categories: Set[str] = set()
    selection_ok = True
    for artifact in artifacts:
        rows = artifact["selection"]
        page_refs: Set[str] = set()
        categories: Set[str] = set()
        selected_count = 0
        complete_source_count = 0
        reason_count = 0
        invalid_source_count = 0
        for row in rows:
            page_id = str(row.get("page_id") or "")
            if not page_id:
                continue
            selected_count += 1
            page_refs.add(_opaque(page_id, "page"))
            row_categories = _stratum_tokens(row)
            categories.update(row_categories)
            reason_count += int(bool(row_categories))
            source_page = k10["pages"].get(page_id)
            material = k10["materialized"].get(page_id)
            source_ok = bool(
                source_page
                and material
                and material.get("status") == "complete"
                and isinstance(material.get("stage_a"), Mapping)
                and material["stage_a"].get("status") == "complete"
                and material.get("within_limits") is True
            )
            complete_source_count += int(source_ok)
            invalid_source_count += int(not source_ok)
        all_page_refs[str(artifact["label"])] = page_refs
        all_categories.update(categories)
        selected_ok = selected_count == AUTHORIZED_DEVELOPMENT_CALLS and len(page_refs) == AUTHORIZED_DEVELOPMENT_CALLS
        source_ok = invalid_source_count == 0 and complete_source_count == AUTHORIZED_DEVELOPMENT_CALLS
        per_round[str(artifact["label"])] = {
            "selected_count": selected_count,
            "unique_page_ref_count": len(page_refs),
            "source_complete_count": complete_source_count,
            "source_invalid_count": invalid_source_count,
            "reason_count": reason_count,
            "strata": sorted(categories),
            "selected_five_ok": selected_ok,
            "all_from_k10_complete": source_ok,
        }
        selection_ok = selection_ok and selected_ok and source_ok and reason_count == AUTHORIZED_DEVELOPMENT_CALLS
    expected = set(EXPECTED_STRATA)
    missing = sorted(expected - all_categories)
    overlap = len(all_page_refs.get("final", set()) & all_page_refs.get("first_attempt", set()))
    return {
        "rounds": per_round,
        "expected_strata": list(EXPECTED_STRATA),
        "observed_strata": sorted(all_categories),
        "missing_strata": missing,
        "strata_complete": not missing,
        "same_page_refs_across_attempts": overlap == AUTHORIZED_DEVELOPMENT_CALLS,
        "duplicate_page_ref_count_across_attempts": overlap,
        "all_selected_from_k10_complete": selection_ok,
        "selection_gate": bool(selection_ok and not missing),
        "page_ref_count": len(set().union(*all_page_refs.values())) if all_page_refs else 0,
        "k10_manifest_ok": bool(k10["manifest_ok"]),
        "k10_hashes_ok": bool(k10["hashes"]["all_match"]),
    }


def _decision_check(artifacts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = [row for artifact in artifacts for row in artifact["decisions"]]
    topics = 0
    invalid_topics = 0
    for row in rows:
        value = row.get("topics")
        if not isinstance(value, list):
            invalid_topics += 1
            continue
        for topic in value:
            topics += 1
            if not isinstance(topic, Mapping):
                invalid_topics += 1
                continue
            required = {"topic_id", "message_handles", "candidate_handles", "evidence_handles", "relation"}
            if set(topic) != required or topic.get("relation") not in ALLOWED_RELATIONS:
                invalid_topics += 1
    pending = sum(str(row.get("status", "")).casefold() == "pending" for artifact in artifacts for row in artifact["selection"])
    complete = sum(str(row.get("status", "")).casefold() == "complete" for artifact in artifacts for row in artifact["selection"])
    expected_message = expected_candidate = expected_evidence = 0
    for artifact in artifacts:
        coverage = artifact["aggregate"].get("topic_coverage")
        if not isinstance(coverage, Mapping):
            continue
        for name, target in (
            ("message_handles", "message"),
            ("candidate_handles", "candidate"),
            ("evidence_handles", "evidence"),
        ):
            item = coverage.get(name)
            if isinstance(item, Mapping) and isinstance(item.get("expected"), (int, float)) and not isinstance(item.get("expected"), bool):
                if target == "message":
                    expected_message += int(item["expected"])
                elif target == "candidate":
                    expected_candidate += int(item["expected"])
                else:
                    expected_evidence += int(item["expected"])
    return {
        "decision_rows": len(rows),
        "topic_rows": topics,
        "invalid_topic_rows": invalid_topics,
        "complete_selection_rows": complete,
        "pending_selection_rows": pending,
        "message_coverage": {"expected": expected_message, "bound": 0, "rate": 0.0, "status": "N/A_no_accepted_payload"},
        "candidate_coverage": {"expected": expected_candidate, "bound": 0, "rate": 0.0, "status": "N/A_no_accepted_payload"},
        "evidence_coverage": {"expected": expected_evidence, "bound": 0, "rate": 0.0, "status": "N/A_no_accepted_payload"},
        "strict_schema_evaluable": bool(rows) and invalid_topics == 0,
        "semantic_evaluable": bool(rows) and invalid_topics == 0,
    }


def _root_cause_check(artifacts: Sequence[Mapping[str, Any]], calls: Mapping[str, Any]) -> Dict[str, Any]:
    rows = [row for artifact in artifacts for row in artifact["ledger"]]
    finish_length = sum(
        isinstance(row.get("finish_reasons"), list)
        and "length" in [str(item).casefold() for item in row["finish_reasons"]]
        for row in rows
    )
    at_limit = sum(row.get("output_tokens") == EXPECTED_OUTPUT_LIMIT for row in rows)
    strict_incomplete = sum(
        row.get("strict_parse_code") != "ok"
        or row.get("strict_validation_code") != "ok"
        or row.get("unique_json_object_count") != 1
        for row in rows
    )
    content_lengths = [
        int(row["content_length"])
        for row in rows
        if isinstance(row.get("content_length"), (int, float)) and not isinstance(row.get("content_length"), bool)
    ]
    input_tokens = [
        int(row["input_tokens"])
        for row in rows
        if isinstance(row.get("input_tokens"), (int, float)) and not isinstance(row.get("input_tokens"), bool)
    ]
    error_codes = sorted({str(row.get("error_code")) for row in rows if row.get("error_code")})
    root_cause_supported = bool(rows) and finish_length == len(rows) and at_limit == len(rows) and strict_incomplete == len(rows)
    return {
        "status": "blocked_incomplete_output" if root_cause_supported else "uncertain",
        "finish_length_rows": finish_length,
        "output_at_400_rows": at_limit,
        "strict_incomplete_rows": strict_incomplete,
        "content_length_min": min(content_lengths, default=0),
        "content_length_max": max(content_lengths, default=0),
        "input_tokens_min": min(input_tokens, default=0),
        "input_tokens_max": max(input_tokens, default=0),
        "error_codes_observed": error_codes,
        "inference_codes": [
            "OUTPUT_400_LIMIT_REACHED",
            "STRICT_JSON_INCOMPLETE_AT_LENGTH",
            "INPUT_OR_PROMPT_SCHEMA_SIZE_OR_DUPLICATION_PRESSURE",
        ],
        "not_a_relaxation_or_retry_problem": bool(
            root_cause_supported
            and calls.get("combined_retry_free") is True
            and all(data.get("per_page_call_ok") is True for data in calls.get("rounds", {}).values())
        ),
    }


def _privacy_output(value: Any) -> Dict[str, int]:
    body = secret = reasoning = identity = 0
    for path, child in _iter_nodes(value):
        key = _key_name(path)
        if not _nonempty(child):
            continue
        body += int(key in BODY_KEYS or key.endswith(("_body", "_text", "_transcript")))
        secret += int(key in SECRET_KEYS or key.endswith(("_secret", "_token", "_password")))
        reasoning += int(key in REASONING_KEYS or key.endswith(("_reasoning", "_thoughts")))
        identity += int(key in IDENTITY_KEYS)
    return {"body_key_hits": body, "secret_key_hits": secret, "reasoning_key_hits": reasoning, "identity_key_hits": identity}


def _round_summary(artifact: Mapping[str, Any]) -> Dict[str, Any]:
    manifest = artifact["manifest"]
    aggregate = artifact["aggregate"]
    ledger = artifact["ledger"]
    privacy = artifact["privacy"]
    raw = artifact["raw"]
    return {
        "artifact_ref": _opaque(_sha256_file(artifact["root"] / "manifest.private.json"), "artifact"),
        "moved_first_attempt": artifact["label"] == "first_attempt",
        "status": str(manifest.get("status") or aggregate.get("status") or "unknown"),
        "success": manifest.get("success") is True and aggregate.get("success") is True,
        "selected_page_count": manifest.get("selected_page_count"),
        "complete_pages": sum(str(row.get("status", "")).casefold() == "complete" for row in artifact["selection"]),
        "pending_pages": sum(str(row.get("status", "")).casefold() == "pending" for row in artifact["selection"]),
        "provider_calls": sum(row.get("provider_call") is True for row in ledger),
        "health_reused": manifest.get("health_reused") is True and aggregate.get("health_reused") is True,
        "health_calls": int(manifest.get("health_call_count") or 0) + int(manifest.get("health_provider_calls") or 0),
        "retry_count": manifest.get("retry_count"),
        "hashes_all_match": artifact["hashes"]["all_match"],
        "private_provider_input_body_allowed": True,
        "persisted_ledger_body_free": not any(privacy.values()),
        "provider_request_body_persisted": False,
        "raw_reasoning_zero": raw["raw_zero"] and raw["reasoning_zero"],
        "stage_b_pilot": manifest.get("stage_b_pilot"),
        "stage_c_pilot": manifest.get("stage_c_pilot"),
        "stage_b_disabled_check": manifest.get("stage_b_pilot") is False and aggregate.get("stage_b_pilot") is False,
        "stage_c_disabled_check": manifest.get("stage_c_pilot") is False and aggregate.get("stage_c_pilot") is False,
        "error_codes": sorted({str(row.get("error_code")) for row in artifact["errors"] if row.get("error_code")}),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in ledger),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in ledger),
        "latency_ms": round(sum(float(row.get("latency_ms") or 0.0) for row in ledger), 3),
    }


def audit_artifacts(
    final_dir: Path = DEFAULT_FINAL_DIR,
    attempt_dir: Path = DEFAULT_ATTEMPT_DIR,
    *,
    k13_audit_path: Path = DEFAULT_K13_AUDIT,
    k10_dir: Path = DEFAULT_K10_DIR,
) -> Dict[str, Any]:
    final = _load_artifact(final_dir, "final")
    attempt = _load_artifact(attempt_dir, "first_attempt")
    artifacts = (final, attempt)
    k13 = _load_k13_audit(k13_audit_path)
    k10 = _load_k10_maps(k10_dir)
    protocol = _protocol_check(artifacts)
    calls = _call_and_retry_check(artifacts)
    selection = _selection_check(artifacts, k10)
    decisions = _decision_check(artifacts)
    settings = _settings_check(artifacts, k13)
    root_cause = _root_cause_check(artifacts, calls)
    schema_ok = all(
        artifact["manifest"].get("schema_version") == RUNNER_SCHEMA_VERSION
        and artifact["manifest"].get("report_schema_version") == REPORT_SCHEMA_VERSION
        and artifact["aggregate"].get("schema_version") == REPORT_SCHEMA_VERSION
        and artifact["aggregate"].get("runner_schema_version") == RUNNER_SCHEMA_VERSION
        for artifact in artifacts
    )

    artifact_gate = all(
        artifact["hashes"]["all_match"]
        and not any(artifact["privacy"].values())
        and artifact["raw"]["raw_zero"]
        and artifact["raw"]["reasoning_zero"]
        and artifact["handles"]["format_ok"]
        and artifact["handles"]["scope_consistent"]
        for artifact in artifacts
    )
    protocol_gate = bool(
        k13["audit_status_pass"]
        and k13["health_complete"]
        and k13["next_step_ok"]
        and k13["checks_all_pass"]
        and all(
            artifact["manifest"].get("health_reused") is True
            and artifact["manifest"].get("health_provider_calls") == 0
            and artifact["manifest"].get("health_call_count") == 0
            and artifact["aggregate"].get("health_reused") is True
            and artifact["aggregate"].get("health_provider_calls") == 0
            and artifact["aggregate"].get("health_call_count") == 0
            and artifact["manifest"].get("frozen_read") is False
            and artifact["manifest"].get("production_state_written") is False
            and artifact["aggregate"].get("frozen_read") is False
            and artifact["aggregate"].get("production_state_written") is False
            for artifact in artifacts
        )
        and protocol["model_ok"]
        and protocol["source_ok"]
        and protocol["response_format_ok"]
        and protocol["thinking_disabled_ok"]
        and protocol["output_limit_ok"]
        and protocol["per_page_limit_ok"]
        and calls["combined_health_rows"] == 0
        and calls["combined_retry_free"]
        and settings["settings_unchanged"]
    )
    budget_gate = bool(calls["budget_ok"] and calls["combined_per_page_max"] <= PER_PAGE_CALL_LIMIT)
    semantic_status = "N/A"
    errors: List[str] = []
    if not artifact_gate:
        errors.append("ARTIFACT_PRIVACY_OR_HASH_GATE")
    if not schema_ok:
        errors.append("SCHEMA_VERSION_GATE")
    if not protocol_gate:
        errors.append("K13_PROTOCOL_HEALTH_REUSE_GATE")
    if not budget_gate:
        errors.extend(["BUDGET_LEDGER_SPLIT", "UNAUTHORIZED_RERUN"])
    if not selection["all_selected_from_k10_complete"] or not selection["k10_manifest_ok"]:
        errors.append("K10_COMPLETE_SELECTION_REF_GATE")
    if not selection["k10_hashes_ok"]:
        errors.append("K10_ARTIFACT_HASH_GATE")
    if not selection["strata_complete"]:
        errors.append("SELECTION_STRATA_INCOMPLETE")
    if decisions["decision_rows"] == 0 or decisions["pending_selection_rows"] > 0:
        errors.append("SEMANTIC_NOT_EVALUABLE_PENDING")
    if root_cause["not_a_relaxation_or_retry_problem"] is not True:
        errors.append("ROOT_CAUSE_EVIDENCE_INCOMPLETE")
    errors.extend(
        "ROUND_%s_%s" % (label.upper(), code.upper())
        for label, data in calls["rounds"].items()
        for code in data["error_codes"]
        if code
    )
    round_summaries = {artifact["label"]: _round_summary(artifact) for artifact in artifacts}
    combined_provider_calls = calls["combined_provider_calls"]
    combined_input_tokens = sum(item["input_tokens"] for item in round_summaries.values())
    combined_output_tokens = sum(item["output_tokens"] for item in round_summaries.values())
    combined_latency = round(sum(item["latency_ms"] for item in round_summaries.values()), 3)
    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_status": str(final["manifest"].get("status") or final["aggregate"].get("status") or "unknown"),
        "audit_status": "fail",
        "audit_pass": False,
        "error_codes": sorted(set(errors)),
        "artifact_ref": _opaque(_sha256_file(final["root"] / "manifest.private.json"), "final"),
        "attempt_ref": _opaque(_sha256_file(attempt["root"] / "manifest.private.json"), "attempt"),
        "k13_health_ref": k13["artifact_ref"],
        "k10_selection_ref": k10["artifact_ref"],
        "schema": {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "runner_schema_version": RUNNER_SCHEMA_VERSION,
            "audit_schema_version": AUDIT_SCHEMA_VERSION,
            "versions_ok": schema_ok,
        },
        "rounds": round_summaries,
        "protocol": protocol,
        "health_reuse": {
            "k13_audit_pass": k13["audit_status_pass"],
            "k13_health_complete": k13["health_complete"],
            "k13_next_step_ok": k13["next_step_ok"],
            "final_health_reused": final["manifest"].get("health_reused") is True,
            "attempt_health_reused": attempt["manifest"].get("health_reused") is True,
            "combined_new_health_calls": calls["combined_health_rows"],
            "health_reuse_gate": protocol_gate,
        },
        "calls_and_budget": calls,
        "selection": selection,
        "decisions_schema_and_coverage": decisions,
        "scope_and_handles": {
            artifact["label"]: {
                "format_ok": artifact["handles"]["format_ok"],
                "scope_consistent_per_page": artifact["handles"]["scope_consistent"],
                "scope_rows_checked": artifact["handles"].get("scope_rows_checked", 0),
                "scope_rows_ok": artifact["handles"].get("scope_rows_ok", 0),
                "invalid_handle_count": artifact["handles"]["invalid_handle_count"],
                "observed_handle_count": artifact["handles"]["observed_handle_count"],
            }
            for artifact in artifacts
        },
        "strict_result": {
            "final_pending_pages": calls["rounds"]["final"]["status_counts"]["pending"],
            "attempt_pending_pages": calls["rounds"]["first_attempt"]["status_counts"]["pending"],
            "final_finish_length_rows": calls["rounds"]["final"]["finish_length_rows"],
            "attempt_finish_length_rows": calls["rounds"]["first_attempt"]["finish_length_rows"],
            "all_strict_complete": False,
            "accepted_payload_rows": decisions["decision_rows"],
        },
        "semantic": {
            "status": semantic_status,
            "gate": "blocked",
            "primary_coverage": "N/A",
            "context_attribution": "N/A",
            "greeting_new_topic": "N/A",
            "no_reply_continuity": "N/A",
            "pronoun_person_object_state": "N/A",
            "topic_shift": "N/A",
            "candidate_competition": "N/A",
            "reason": "all_selected_pages_pending_and_strict_json_incomplete",
            "overmerge": "N/A",
            "oversplit": "N/A",
        },
        "coverage": {
            "message": decisions["message_coverage"],
            "candidate": decisions["candidate_coverage"],
            "evidence": decisions["evidence_coverage"],
            "decision_rows": decisions["decision_rows"],
            "pending_pages": decisions["pending_selection_rows"],
            "complete_pages": decisions["complete_selection_rows"],
        },
        "privacy": {
            "audit_output_only_opaque_refs": True,
            "artifact_gate": artifact_gate,
            "private_provider_input_body_allowed": True,
            "persisted_ledger_and_audit_body_free": artifact_gate,
            "provider_request_body_persisted": False,
            "raw_and_reasoning_zero": all(
                artifact["raw"]["raw_zero"] and artifact["raw"]["reasoning_zero"] for artifact in artifacts
            ),
            "settings_hashes_opaque": True,
        },
        "settings": settings,
        "root_cause": root_cause,
        "cost": {
            "authorized_development_calls": AUTHORIZED_DEVELOPMENT_CALLS,
            "final_provider_calls": calls["rounds"]["final"]["provider_calls"],
            "first_attempt_provider_calls": calls["rounds"]["first_attempt"]["provider_calls"],
            "combined_provider_calls": combined_provider_calls,
            "overage_calls": max(0, combined_provider_calls - AUTHORIZED_DEVELOPMENT_CALLS),
            "input_tokens_combined": combined_input_tokens,
            "output_tokens_combined": combined_output_tokens,
            "latency_ms_combined": combined_latency,
            "output_token_limit": EXPECTED_OUTPUT_LIMIT,
            "retry_limit": RETRY_LIMIT,
            "per_page_provider_call_limit": PER_PAGE_CALL_LIMIT,
        },
        "next_step": {
            "allow_stage_a_more_development_calls": False,
            "allow_stage_b_pilot": False,
            "allow_stage_c_pilot": False,
            "max_stage_b_topics": 0,
            "reason_codes": [
                "BUDGET_LEDGER_SPLIT",
                "UNAUTHORIZED_RERUN",
                "SEMANTIC_NOT_EVALUABLE_PENDING",
                "OUTPUT_400_LIMIT_REACHED",
                "NO_AUTORETRY_OR_RELAXATION",
            ],
            "required_fix_before_any_new_provider_call": "offline_synthetic_replay_with_smaller_request_and_strict_complete_output",
        },
        "scope": {
            "provider_calls_by_audit": 0,
            "frozen_path_read_by_audit": False,
            "production_state_written_by_audit": False,
            "development_input_read_by_audit": True,
            "attempt_directory_included": True,
            "opaque_only": True,
        },
    }
    privacy = _privacy_output(report)
    if any(privacy.values()):
        raise AuditError("AUDIT_OUTPUT_NOT_BODY_FREE_OR_OPAQUE:%s" % privacy)
    report["privacy"]["audit_output_key_hits"] = privacy
    if any(_privacy_output(report).values()):
        raise AuditError("AUDIT_OUTPUT_PRIVACY_RECHECK_FAILED")
    return report


def _human_rows(report: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
    for code in report.get("error_codes", ()):
        yield {"check": "error_code", "status": "fail", "code": code}
    yield {"check": "audit_status", "status": report.get("audit_status")}
    yield {"check": "artifact_status", "status": report.get("artifact_status")}
    yield {"check": "combined_provider_calls", "status": report.get("cost", {}).get("combined_provider_calls")}
    yield {"check": "authorized_development_calls", "status": report.get("cost", {}).get("authorized_development_calls")}
    yield {"check": "budget_gate", "status": report.get("calls_and_budget", {}).get("budget_ok")}
    yield {"check": "semantic_status", "status": report.get("semantic", {}).get("status")}
    yield {"check": "allow_stage_b_pilot", "status": report.get("next_step", {}).get("allow_stage_b_pilot")}
    yield {"check": "allow_stage_c_pilot", "status": report.get("next_step", {}).get("allow_stage_c_pilot")}


def write_audit(report: Mapping[str, Any], final_dir: Path) -> Tuple[Path, Path]:
    audit_dir = final_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary = audit_dir / "audit_summary.private.json"
    human = audit_dir / "human_audit.private.jsonl"
    summary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    human.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in _human_rows(report)
        ),
        encoding="utf-8",
    )
    return summary, human


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Independent K14 two-attempt development-pilot audit")
    parser.add_argument("--final-artifact-dir", type=Path, default=DEFAULT_FINAL_DIR)
    parser.add_argument("--attempt-artifact-dir", type=Path, default=DEFAULT_ATTEMPT_DIR)
    parser.add_argument("--k13-audit", type=Path, default=DEFAULT_K13_AUDIT)
    parser.add_argument("--k10-dir", type=Path, default=DEFAULT_K10_DIR)
    args = parser.parse_args(argv)
    try:
        report = audit_artifacts(
            args.final_artifact_dir,
            args.attempt_artifact_dir,
            k13_audit_path=args.k13_audit,
            k10_dir=args.k10_dir,
        )
        summary, human = write_audit(report, _safe_path(args.final_artifact_dir))
    except AuditError as exc:
        print(json.dumps({"audit_status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "audit_status": report["audit_status"],
                "combined_provider_calls": report["cost"]["combined_provider_calls"],
                "summary": str(summary),
                "human": str(human),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["audit_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
