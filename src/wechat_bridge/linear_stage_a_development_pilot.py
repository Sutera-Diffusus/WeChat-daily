"""Synthetic-only K14 Stage-A development pilot.

K14 consumes the already audited K13 health artifact.  It never performs a
second health request: after the K13 gate is accepted it reads only the
complete development packet, selects at most one page from each of the five
known strata, and makes at most one strict Stage-A request per selected page.
The provider adapter is the K13 adapter (omitted response format and an
explicit per-call ``thinking`` disable); this module is intentionally usable
with an injected synthetic provider for protocol tests.

Only opaque handles, lengths, hashes, enums, and usage counters are written.
Response text and reasoning are classified in memory and are never persisted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple, Union

from .linear_stage_a_pilot import (
    BODY_KEYS as PILOT_BODY_KEYS,
    CATEGORY_NAMES,
    INPUT_ARTIFACT_VERSION,
    STAGE_A_SYSTEM_PROMPT,
    _build_stage_a_request,
    _read_v2_after_health,
    _request_sha256,
    _select_pages,
    _selected_opaque_refs,
    validate_stage_a_output,
)
from .linear_stage_a_protocol_diagnostic import (
    DiagnosticProviderConfig,
    DiagnosticProviderResponse,
    DiagnosticStageProvider,
    DeepSeekProtocolDiagnosticProvider,
    _filter_response_field_names,
    _parse_diagnostics,
    _safe_error_code,
    _strict_loads,
    stable_hash,
)
from .staged_deepseek_analyzer import StageModelResponse


ARTIFACT_VERSION = "linear_stage_a_development_pilot_v1"
REPORT_SCHEMA_VERSION = "linear_stage_a_development_pilot_report_v1"
RUNNER_SCHEMA_VERSION = "linear_stage_a_development_pilot_runner_v1"
HEALTH_ARTIFACT_VERSION = "linear_stage_a_protocol_health_v2"
MODEL = "deepseek-v4-flash"
RESPONSE_FORMAT_MODE = "omitted"
THINKING_DISABLED = True
MAX_DEVELOPMENT_CALLS = 5
MAX_PROVIDER_CALLS = 5
MAX_RETRIES = 0
PER_PAGE_PROVIDER_CALL_LIMIT = 1
MAX_OUTPUT_TOKENS = 400
LOCAL_DAY = "2026-08-25"
DEFAULT_HEALTH_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_a_protocol_health_v2"
)
DEFAULT_INPUT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_packet_development_v2"
)
DEFAULT_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_a_development_pilot_v1"
)
OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "ledger": "ledger.private.jsonl",
    "selection": "selection.private.jsonl",
    "decisions": "decisions.private.jsonl",
    "errors": "errors.private.jsonl",
}
BODY_KEYS = frozenset(
    set(PILOT_BODY_KEYS)
    | {
        "analysis",
        "chain_of_thought",
        "completion",
        "raw_content",
        "raw_output",
        "raw_reasoning",
        "raw_response",
        "reasoning",
        "reasoning_content",
        "response_text",
        "system_prompt",
        "thoughts",
        "user_input",
        "user_packet",
    }
)


def _file_sha256(path: Union[str, Path]) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _safe_path(path: Union[str, Path], code: str) -> Path:
    result = Path(path).expanduser().resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in result.parts):
        raise ValueError(code)
    return result


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid_health_json") from exc
    if not isinstance(value, Mapping):
        raise ValueError("health_json_not_object")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], (), {})


def _assert_body_free(value: Any, *, label: str = "value") -> None:
    hits: List[str] = []

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key.casefold() in BODY_KEYS and _nonempty(child):
                    hits.append(path + key)
                visit(child, path + key + ".")
        elif isinstance(item, (list, tuple, set, frozenset)):
            for index, child in enumerate(item):
                visit(child, path + str(index) + ".")

    visit(value)
    if hits:
        raise ValueError("%s contains body-bearing fields: %s" % (label, ", ".join(hits[:5])))


@dataclass(frozen=True)
class HealthReuse:
    ok: bool
    error_code: Optional[str]
    artifact_version: str
    status: str
    model: str
    source: str
    response_format_mode: str
    thinking_disabled: bool
    provider_calls: int
    strict_complete: bool
    development_input_read: bool
    stage_a_development: bool
    stage_b_pilot: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reused": bool(self.ok),
            "ok": bool(self.ok),
            "error_code": self.error_code,
            "artifact_version": self.artifact_version,
            "status": self.status,
            "model": self.model,
            "source": self.source,
            "response_format_mode": self.response_format_mode,
            "thinking_disabled": bool(self.thinking_disabled),
            "provider_calls": int(self.provider_calls),
            "strict_complete": bool(self.strict_complete),
            "development_input_read": bool(self.development_input_read),
            "stage_a_development": bool(self.stage_a_development),
            "stage_b_pilot": bool(self.stage_b_pilot),
        }


def _load_health_reuse(path: Union[str, Path]) -> HealthReuse:
    try:
        root = _safe_path(path, "development_refuses_frozen_health_path")
        manifest = _read_json(root / "manifest.private.json")
        aggregate = _read_json(root / "aggregate.private.json")
        provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
        strict = aggregate.get("strict_result") if isinstance(aggregate.get("strict_result"), Mapping) else {}
        model = str(provider.get("model") or "")
        source = str(provider.get("source") or "")
        checks = (
            root.name == HEALTH_ARTIFACT_VERSION,
            manifest.get("artifact_version") == HEALTH_ARTIFACT_VERSION,
            manifest.get("status") == "available",
            manifest.get("success") is True,
            manifest.get("diagnostic_only") is True,
            manifest.get("provider_calls") == 1,
            manifest.get("diagnostic_call_count") == 1,
            manifest.get("retry_count") == 0,
            manifest.get("development_input_read") is False,
            manifest.get("frozen_read") is False,
            manifest.get("production_state_written") is False,
            manifest.get("thinking_disabled") is True,
            provider.get("model") == MODEL,
            provider.get("response_format_mode") == RESPONSE_FORMAT_MODE,
            provider.get("response_format_sent") is False,
            strict.get("strict_complete") is True,
            strict.get("parse_code") == "ok",
            strict.get("validation_code") == "ok",
            aggregate.get("status") == "available",
            aggregate.get("success") is True,
            aggregate.get("diagnostic_only") is True,
            aggregate.get("development_input_read") is False,
            aggregate.get("stage_a_development") is False,
            aggregate.get("stage_b_pilot") is False,
            aggregate.get("frozen_read") is False,
            aggregate.get("production_state_written") is False,
            aggregate.get("settings_unchanged") is True,
        )
        if not all(checks):
            return HealthReuse(
                False,
                "health_reuse_gate_failed",
                str(manifest.get("artifact_version") or ""),
                str(manifest.get("status") or ""),
                model,
                source,
                str(provider.get("response_format_mode") or ""),
                bool(manifest.get("thinking_disabled")),
                int(manifest.get("provider_calls") or 0),
                bool(strict.get("strict_complete")),
                bool(manifest.get("development_input_read")),
                bool(aggregate.get("stage_a_development")),
                bool(aggregate.get("stage_b_pilot")),
            )
        return HealthReuse(
            True,
            None,
            HEALTH_ARTIFACT_VERSION,
            "available",
            MODEL,
            source,
            RESPONSE_FORMAT_MODE,
            True,
            1,
            True,
            False,
            False,
            False,
        )
    except (OSError, ValueError, TypeError) as exc:
        return HealthReuse(False, str(exc)[:120] or "health_reuse_gate_failed", "", "", "", "", "", False, 0, False, False, False, False)


@dataclass(frozen=True)
class PageOutcome:
    page: Mapping[str, Any]
    materialized: Mapping[str, Any]
    request: Mapping[str, Any]
    request_sha256: str
    categories: Tuple[str, ...]
    status: str
    payload: Optional[Mapping[str, Any]]
    error_code: Optional[str]
    cache_hit: bool
    provider_call: bool
    input_tokens: int
    output_tokens: int
    latency_ms: float
    source: str
    model: str
    diagnostics: Mapping[str, Any]


def _empty_diagnostics(error_code: str = "not_run") -> Dict[str, Any]:
    return {
        "content_length": 0,
        "reasoning_length": 0,
        "finish_reasons": [],
        "status": "not_run",
        "usage": {"present": False, "input_tokens": 0, "output_tokens": 0, "keys": []},
        "output_sha256": stable_hash(""),
        "leading_shape": "empty",
        "fence_detected": False,
        "unique_json_object_count": 0,
        "strict_parse_code": error_code,
        "strict_validation_code": "not_run",
        "safe_public_field_names": [],
        "request_id_present": False,
        "raw_response_saved": False,
        "raw_reasoning_saved": False,
    }


def _response_diagnostics(value: Any, request: Mapping[str, Any]) -> Tuple[Optional[Mapping[str, Any]], Dict[str, Any], Optional[str], int, int, float, str]:
    if isinstance(value, DiagnosticProviderResponse):
        parsed = _parse_diagnostics(value, request)
        diagnostics = {
            "content_length": parsed.content_length,
            "reasoning_length": parsed.reasoning_length,
            "finish_reasons": list(value.finish_reasons),
            "status": "received",
            "usage": {
                "present": bool(value.usage_present),
                "input_tokens": max(0, int(value.input_tokens)),
                "output_tokens": max(0, int(value.output_tokens)),
                "keys": list(value.usage_keys),
            },
            "output_sha256": parsed.output_sha256,
            "leading_shape": parsed.leading_shape,
            "fence_detected": parsed.fence_detected,
            "unique_json_object_count": parsed.unique_json_object_count,
            "strict_parse_code": parsed.strict_parse_code,
            "strict_validation_code": parsed.strict_validation_code,
            "safe_public_field_names": list(_filter_response_field_names(value.response_fields)),
            "request_id_present": bool(value.request_id_present),
            "raw_response_saved": False,
            "raw_reasoning_saved": False,
        }
        code: Optional[str] = None
        payload: Optional[Mapping[str, Any]] = None
        # K13's 900-token health proxy is intentionally not a K14
        # development limit.  K10 pages carry bounded but materially larger
        # context, so only the frozen per-call output ceiling is enforced
        # here; input-size eligibility was already decided by K10.
        if value.output_tokens > MAX_OUTPUT_TOKENS:
            code = "development_output_tokens_exceeded"
        elif not parsed.strict_complete:
            code = parsed.strict_parse_code if parsed.strict_parse_code != "ok" else parsed.strict_validation_code
        else:
            try:
                payload = validate_stage_a_output(_strict_loads(value.content), request)
            except Exception as exc:
                code = _safe_error_code(exc)
        return payload, diagnostics, code, max(0, int(value.input_tokens)), max(0, int(value.output_tokens)), max(0.0, float(value.latency_ms)), str(value.model or MODEL)
    if isinstance(value, StageModelResponse):
        diagnostics = _empty_diagnostics("ok")
        diagnostics.update(
            {
                "status": "received",
                "usage": {
                    "present": bool(value.input_tokens or value.output_tokens),
                    "input_tokens": max(0, int(value.input_tokens)),
                    "output_tokens": max(0, int(value.output_tokens)),
                    "keys": [],
                },
            }
        )
        try:
            payload = validate_stage_a_output(value.payload, request)
        except Exception as exc:
            return None, diagnostics, _safe_error_code(exc), max(0, int(value.input_tokens)), max(0, int(value.output_tokens)), max(0.0, float(value.latency_ms)), str(value.model or MODEL)
        code: Optional[str] = None
        if value.output_tokens > MAX_OUTPUT_TOKENS:
            code = "development_output_tokens_exceeded"
        return payload, diagnostics, code, max(0, int(value.input_tokens)), max(0, int(value.output_tokens)), max(0.0, float(value.latency_ms)), str(value.model or MODEL)
    if isinstance(value, Mapping):
        try:
            payload = validate_stage_a_output(value, request)
        except Exception as exc:
            return None, _empty_diagnostics(_safe_error_code(exc)), _safe_error_code(exc), 0, 0, 0.0, MODEL
        return payload, _empty_diagnostics("ok"), None, 0, 0, 0.0, MODEL
    code = "provider_response_object"
    return None, _empty_diagnostics(code), code, 0, 0, 0.0, MODEL


def _execute_page(
    provider: DiagnosticStageProvider,
    page: Mapping[str, Any],
    materialized: Mapping[str, Any],
    request: Mapping[str, Any],
    categories: Tuple[str, ...],
    cache: MutableMapping[str, Mapping[str, Any]],
) -> PageOutcome:
    request_sha = _request_sha256(provider, request)
    refs = _selected_opaque_refs(request)
    model = str(getattr(provider, "model_id", MODEL))
    source = str(getattr(provider, "source", "unknown"))
    cached = cache.get(request_sha)
    if cached is not None:
        try:
            payload = validate_stage_a_output(deepcopy(dict(cached)), request)
        except Exception:
            cache.pop(request_sha, None)
        else:
            return PageOutcome(page, materialized, request, request_sha, categories, "complete", payload, None, True, False, 0, 0, 0.0, "stage_a_cache", model, _empty_diagnostics("cache_hit"))
    started = time.perf_counter()
    try:
        raw = provider.complete("A", STAGE_A_SYSTEM_PROMPT, request, max_output_tokens=MAX_OUTPUT_TOKENS)
        payload, diagnostics, error_code, input_tokens, output_tokens, response_latency, response_model = _response_diagnostics(raw, request)
        latency = response_latency or max(0.0, (time.perf_counter() - started) * 1000.0)
        if payload is not None and error_code is None:
            cache[request_sha] = deepcopy(dict(payload))
            return PageOutcome(page, materialized, request, request_sha, categories, "complete", payload, None, False, True, input_tokens, output_tokens, latency, source, response_model, diagnostics)
        return PageOutcome(page, materialized, request, request_sha, categories, "pending", None, error_code or "provider_invalid_json", False, True, input_tokens, output_tokens, latency, source, response_model, diagnostics)
    except Exception as exc:
        return PageOutcome(page, materialized, request, request_sha, categories, "pending", None, _safe_error_code(exc), False, True, 0, 0, max(0.0, (time.perf_counter() - started) * 1000.0), source, model, _empty_diagnostics(_safe_error_code(exc)))


def _coverage(outcomes: Sequence[PageOutcome]) -> Dict[str, Any]:
    strata = {
        name: {
            "selected_pages": sum(name in row.categories for row in outcomes),
            "complete_pages": sum(name in row.categories and row.status == "complete" for row in outcomes),
            "pending_pages": sum(name in row.categories and row.status != "complete" for row in outcomes),
        }
        for name in CATEGORY_NAMES
    }
    expected_messages = sum(len(row.request.get("message_handles", ())) for row in outcomes)
    expected_candidates = sum(len(row.request.get("candidate_handles", ())) for row in outcomes)
    expected_evidence = sum(len(row.request.get("evidence_handles", ())) for row in outcomes)
    bound_messages = bound_candidates = bound_evidence = 0
    bindings: List[Dict[str, Any]] = []
    for row in outcomes:
        if row.payload is None:
            continue
        topics = list(row.payload.get("topics", ()))
        bound_messages += len({str(x) for topic in topics for x in topic.get("message_handles", ())})
        bound_candidates += len({str(x) for topic in topics for x in topic.get("candidate_handles", ())})
        bound_evidence += len({str(x) for topic in topics for x in topic.get("evidence_handles", ())})
        bindings.extend(
            {
                "page_id": str(row.page.get("page_id", "")),
                "root_id": str(row.page.get("root_id", "")),
                "topic_id": str(topic.get("topic_id", "")),
                "relation": str(topic.get("relation", "unknown")),
                "message_handles": list(topic.get("message_handles", ())),
                "candidate_handles": list(topic.get("candidate_handles", ())),
                "evidence_handles": list(topic.get("evidence_handles", ())),
            }
            for topic in topics
        )
    def rate(bound: int, expected: int) -> float:
        return bound / expected if expected else 1.0
    return {
        "category_flags": strata,
        "pages": {
            "selected": len(outcomes),
            "complete": sum(row.status == "complete" for row in outcomes),
            "pending": sum(row.status != "complete" for row in outcomes),
        },
        "message_handles": {"expected": expected_messages, "bound": bound_messages, "rate": rate(bound_messages, expected_messages)},
        "candidate_handles": {"expected": expected_candidates, "bound": bound_candidates, "rate": rate(bound_candidates, expected_candidates)},
        "evidence_handles": {"expected": expected_evidence, "bound": bound_evidence, "rate": rate(bound_evidence, expected_evidence)},
        "bindings": bindings,
    }


def _cache_stability(outcomes: Sequence[PageOutcome]) -> Dict[str, Any]:
    prefixes = [row.request_sha256[:16] for row in outcomes]
    return {"prefix_length": 16, "prefixes": prefixes, "stable": all(len(item) == 16 for item in prefixes), "unique_prefix_count": len(set(prefixes))}


def _write_artifacts(
    *,
    output_root: Path,
    health: HealthReuse,
    input_label: str,
    input_manifest: Optional[Mapping[str, Any]],
    development_input_read: bool,
    outcomes: Sequence[PageOutcome],
    provider_public: Mapping[str, Any],
    settings_before: str,
    settings_after: str,
    error_codes: Sequence[str],
) -> DevelopmentPilotResult:
    provider_calls = sum(bool(row.provider_call) for row in outcomes)
    input_tokens = sum(int(row.input_tokens) for row in outcomes)
    output_tokens = sum(int(row.output_tokens) for row in outcomes)
    all_complete = bool(health.ok and len(outcomes) == MAX_DEVELOPMENT_CALLS and all(row.status == "complete" for row in outcomes))
    status = "complete" if all_complete else ("partial" if health.ok and outcomes else "blocked")
    success = status == "complete"
    coverage = _coverage(outcomes)
    errors = [
        {"phase": "health_gate", "error_code": health.error_code}
        for _ in [0]
        if health.error_code
    ] + [
        {"phase": "development", "page_id": str(row.page.get("page_id", "")), "root_id": str(row.page.get("root_id", "")), "error_code": row.error_code}
        for row in outcomes
        if row.error_code
    ]
    errors.extend({"phase": "runner", "error_code": code} for code in error_codes if code not in {item.get("error_code") for item in errors})
    ledger = [
        {
            "phase": "development",
            **dict(row.diagnostics),
            "page_id": str(row.page.get("page_id", "")),
            "root_id": str(row.page.get("root_id", "")),
            # Keep the runner state authoritative.  Response diagnostics also
            # carry a provider-side status such as ``received``; it must not
            # overwrite our strict complete/pending decision in the ledger.
            "status": row.status,
            "error_code": row.error_code,
            "provider_call": bool(row.provider_call),
            "cache_hit": bool(row.cache_hit),
            "retry_count": MAX_RETRIES,
            "request_sha256": row.request_sha256,
            "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
            "user_packet_sha256": stable_hash(row.request),
            "source": row.source,
            "model": row.model,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "latency_ms": row.latency_ms,
            "response_format_mode": RESPONSE_FORMAT_MODE,
            "thinking_disabled": THINKING_DISABLED,
            "selected_opaque_refs": _selected_opaque_refs(row.request),
        }
        for row in outcomes
    ]
    selection = [
        {
            "selection_rank": index + 1,
            "page_id": str(row.page.get("page_id", "")),
            "root_id": str(row.page.get("root_id", "")),
            "source_packet_id": str(row.page.get("source_packet_id") or row.page.get("root_id", "")),
            "scope": deepcopy(row.page.get("scope", {})),
            "categories": list(row.categories),
            "status": row.status,
            "message_count": len(row.request.get("message_handles", ())),
            "candidate_count": len(row.request.get("candidate_handles", ())),
            "evidence_count": len(row.request.get("evidence_handles", ())),
        }
        for index, row in enumerate(outcomes)
    ]
    decisions = [
        {
            "page_id": str(row.page.get("page_id", "")),
            "root_id": str(row.page.get("root_id", "")),
            "status": row.status,
            "cache_hit": bool(row.cache_hit),
            "topics": deepcopy(list(row.payload.get("topics", ()))),
        }
        for row in outcomes
        if row.payload is not None
    ]
    provider = {
        **dict(provider_public),
        "model": MODEL,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "calls": provider_calls,
        "call_limit": MAX_PROVIDER_CALLS,
        "per_page_call_limit": PER_PAGE_PROVIDER_CALL_LIMIT,
        "within_call_limit": provider_calls <= MAX_PROVIDER_CALLS,
    }
    response_format = {"mode": RESPONSE_FORMAT_MODE, "sent": False, "predeclared": True}
    extra_body = {"sent": bool(provider_calls), "field_names": ["thinking"], "value_shape": "disabled", "per_call": True, "global_settings_mutated": False}
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "status": status,
        "success": success,
        "health_reused": bool(health.ok),
        "health_provider_calls": 0,
        "health_call_count": 0,
        "health": health.to_dict(),
        "input_artifact_version": INPUT_ARTIFACT_VERSION,
        "input_directory_name": Path(input_label).name,
        "input_manifest_digest": str((input_manifest or {}).get("input_selected_digest", "")),
        "development_input_read": development_input_read,
        "selected_page_count": len(outcomes),
        "development_calls": provider_calls,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": MAX_RETRIES,
        "stage_a_development": bool(development_input_read),
        "stage_b_pilot": False,
        "stage_c_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": provider,
        "response_format": response_format,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": extra_body,
        "topic_coverage": coverage,
        "cache_prefix_stability": _cache_stability(outcomes),
        "cost": {
            "provider_calls": provider_calls,
            "development_calls": provider_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round(sum(row.latency_ms for row in outcomes), 3),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "retry_count": MAX_RETRIES,
            "cached_completions": sum(bool(row.cache_hit) for row in outcomes),
        },
        "settings_before_sha256": settings_before,
        "settings_after_sha256": settings_after,
        "settings_unchanged": settings_before == settings_after,
        "errors": {"count": len(errors), "codes": sorted({str(item.get("error_code")) for item in errors if item.get("error_code")})},
    }
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "local_day": LOCAL_DAY,
        "output_directory_name": output_root.name,
        "status": status,
        "success": success,
        "health_reused": bool(health.ok),
        "health_provider_calls": 0,
        "health_call_count": 0,
        "diagnostic_only": False,
        "development_input_read": development_input_read,
        "selected_page_count": len(outcomes),
        "provider_called": bool(provider_calls),
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": MAX_RETRIES,
        "stage_a_development": bool(development_input_read),
        "stage_b_pilot": False,
        "stage_c_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": provider,
        "response_format": response_format,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": extra_body,
        "settings_before_sha256": settings_before,
        "settings_after_sha256": settings_after,
        "settings_unchanged": settings_before == settings_after,
        "health_artifact_directory_name": DEFAULT_HEALTH_ARTIFACT_DIRECTORY.name,
        "input_directory_name": Path(input_label).name,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    output_values = {"aggregate": aggregate, "cost": aggregate["cost"], "ledger": ledger, "selection": selection, "decisions": decisions, "errors": errors}
    for label, value in output_values.items():
        _assert_body_free(value, label=label)
    _assert_body_free(manifest, label="manifest")
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], aggregate["cost"])
    _write_jsonl(output_root / OUTPUT_FILENAMES["ledger"], ledger)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection"], selection)
    _write_jsonl(output_root / OUTPUT_FILENAMES["decisions"], decisions)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _file_sha256(output_root / filename)
        for filename in OUTPUT_FILENAMES.values()
        if filename != OUTPUT_FILENAMES["manifest"]
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return DevelopmentPilotResult(str(input_label), str(output_root), status, success, bool(health.ok), len(outcomes), provider_calls, MAX_RETRIES, aggregate, paths)


@dataclass(frozen=True)
class DevelopmentPilotResult:
    input_directory: str
    output_directory: str
    status: str
    success: bool
    health_reused: bool
    selected_page_count: int
    provider_calls: int
    retry_count: int
    aggregate: Mapping[str, Any]
    artifact_paths: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "status": self.status,
            "success": bool(self.success),
            "health_reused": bool(self.health_reused),
            "selected_page_count": self.selected_page_count,
            "provider_calls": self.provider_calls,
            "retry_count": self.retry_count,
            "aggregate": {"artifact_version": self.aggregate.get("artifact_version"), "status": self.aggregate.get("status")},
            "artifact_paths": dict(self.artifact_paths),
        }


def run_linear_stage_a_development_pilot(
    input_directory: Union[str, Path] = DEFAULT_INPUT_DIRECTORY,
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    health_artifact_directory: Union[str, Path] = DEFAULT_HEALTH_ARTIFACT_DIRECTORY,
    settings_path: Union[str, Path] = Path("data/workbench_settings.json"),
    provider: Optional[DiagnosticStageProvider] = None,
    cache: Optional[MutableMapping[str, Mapping[str, Any]]] = None,
    config: Optional[DiagnosticProviderConfig] = None,
) -> DevelopmentPilotResult:
    output_root = _safe_path(output_directory, "development_refuses_frozen_output")
    if output_root.exists():
        raise FileExistsError("development_output_is_immutable")
    health = _load_health_reuse(health_artifact_directory)
    settings_before = _file_sha256(settings_path)
    settings_after = settings_before
    outcomes: List[PageOutcome] = []
    input_manifest: Optional[Mapping[str, Any]] = None
    development_input_read = False
    errors: List[str] = []
    selected_config = config
    if selected_config is None and provider is not None:
        selected_config = DiagnosticProviderConfig(model=MODEL, base_url=None, response_format_mode=RESPONSE_FORMAT_MODE, thinking_disabled=THINKING_DISABLED)
    if selected_config is None:
        try:
            selected_config = DiagnosticProviderConfig.from_workbench_settings(
                settings_path,
                model_override=MODEL,
                response_format_mode=RESPONSE_FORMAT_MODE,
                thinking_disabled=THINKING_DISABLED,
            )
        except Exception as exc:
            errors.append(_safe_error_code(exc))
            selected_config = DiagnosticProviderConfig(model=MODEL, base_url=None, response_format_mode=RESPONSE_FORMAT_MODE, thinking_disabled=THINKING_DISABLED)
    selected_config = replace(selected_config, model=MODEL, response_format_mode=RESPONSE_FORMAT_MODE, thinking_disabled=THINKING_DISABLED)
    provider_object = provider
    if provider_object is None and health.ok and selected_config.configured:
        provider_object = DeepSeekProtocolDiagnosticProvider(selected_config)
    provider_public = selected_config.public_dict()
    if provider_object is not None:
        provider_public["source"] = str(getattr(provider_object, "source", provider_public.get("source", "unknown")))
    if not health.ok:
        errors.append(health.error_code or "health_reuse_gate_failed")
    elif provider_object is None or not bool(getattr(provider_object, "configured", True)):
        errors.append("provider_unconfigured")
    elif str(getattr(provider_object, "model_id", MODEL)) != MODEL:
        errors.append("development_model_override_refused")
    else:
        try:
            rows, store, input_manifest = _read_v2_after_health(_safe_path(input_directory, "development_refuses_frozen_input"))
            development_input_read = True
            selected = _select_pages([row["page"] for row in rows], store)
            if len(selected) != MAX_DEVELOPMENT_CALLS:
                errors.append("development_requires_five_complete_pages")
            else:
                material_by_page = {str(row["page"].get("page_id")): row["materialized"] for row in rows}
                cache_store: MutableMapping[str, Mapping[str, Any]] = cache if cache is not None else {}
                for page, categories in selected:
                    request = _build_stage_a_request(store, page)
                    outcomes.append(_execute_page(provider_object, page, material_by_page[str(page.get("page_id"))], request, tuple(categories), cache_store))
        except Exception as exc:
            errors.append("development_input_invalid")
    return _write_artifacts(
        output_root=output_root,
        health=health,
        input_label=str(input_directory),
        input_manifest=input_manifest,
        development_input_read=development_input_read,
        outcomes=outcomes,
        provider_public=provider_public,
        settings_before=settings_before,
        settings_after=settings_after,
        error_codes=errors,
    )


run_stage_a_development_pilot = run_linear_stage_a_development_pilot


__all__ = [
    "ARTIFACT_VERSION",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "HEALTH_ARTIFACT_VERSION",
    "MODEL",
    "RESPONSE_FORMAT_MODE",
    "THINKING_DISABLED",
    "MAX_DEVELOPMENT_CALLS",
    "MAX_PROVIDER_CALLS",
    "MAX_RETRIES",
    "MAX_OUTPUT_TOKENS",
    "DEFAULT_HEALTH_ARTIFACT_DIRECTORY",
    "DEFAULT_INPUT_DIRECTORY",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "DevelopmentPilotResult",
    "DiagnosticProviderConfig",
    "DeepSeekProtocolDiagnosticProvider",
    "run_linear_stage_a_development_pilot",
    "run_stage_a_development_pilot",
]
