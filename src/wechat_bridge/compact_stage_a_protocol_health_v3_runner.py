"""One-shot real-provider adapter for the compact Stage-A v3 health check.

K23 deliberately kept :mod:`compact_stage_a_protocol_health_v3` synthetic
only.  This side-car is the narrowly scoped K24 execution boundary.  It may
send one synthetic request to the configured OpenAI-compatible provider, but
it never reads development/private chat data or frozen data and it never
persists a request, response, reasoning text, credential, or exception body.

The durable authorization ledger is opened before the provider call.  A
stable authorization id and authority root mean that a second output
directory or process cannot obtain a fresh slot.  The ledger reservation is
atomic and remains consumed even when the provider fails or returns an
invalid response.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from .compact_stage_a_protocol_v3 import (
    CACHE_VERSION,
    MAX_INPUT_TOKEN_PROXY,
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    PROTOCOL_VERSION,
    SYSTEM_PROMPT,
    TOPIC_LIMIT_RULE,
    build_compact_stage_a_request,
    canonical_json,
    measure_wire_size,
    parse_compact_stage_a_output,
    stable_hash,
    topic_limit_for_request,
)
from .compact_stage_a_protocol_health_v3 import build_synthetic_health_request
from .linear_stage_a_protocol_diagnostic import DiagnosticProviderConfig
from .persistent_call_budget import (
    AuthorizationBindingMismatch,
    CallAuthorizationLedger,
    CallBudgetExceeded,
    ReservationRejected,
)


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_VERSION = "compact_stage_a_protocol_health_v3"
REPORT_SCHEMA_VERSION = "compact_stage_a_protocol_health_report_v3"
RUNNER_SCHEMA_VERSION = "compact_stage_a_protocol_health_runner_v3_real"
MODEL_ID = "deepseek-v4-flash"
PROVIDER_ID = "openai-compatible"
SOURCE_ID = "deepseek-openai-compatible"
AUTHORIZATION_ID = "K23_COMPACT_STAGE_A_HEALTH_V3"
ARTIFACT_NAMESPACE = "compact-stage-a-health-v3"
RESPONSE_FORMAT_MODE = "omitted"
THINKING_DISABLED = True
MAX_PROVIDER_CALLS = 1
MAX_RETRIES = 0
HEALTH_MAX_OUTPUT_TOKENS = 400
DEFAULT_AUTHORITY_ROOT = ROOT / ".runtime" / "compact-stage-a-authorizations"
DEFAULT_SETTINGS_PATH = ROOT / "data" / "workbench_settings.json"
DEFAULT_ARTIFACT_DIRECTORY = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / "2026-08-25"
    / "compact_stage_a_protocol_health_v3"
)
OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "diagnostic": "diagnostic.private.json",
    "ledger": "ledger.private.jsonl",
    "errors": "errors.private.jsonl",
}

_FORBIDDEN_PATH_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "message",
        "messages",
        "prompt",
        "raw",
        "raw_response",
        "reasoning",
        "reasoning_content",
        "response",
        "text",
        "user_input",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "authorization_binding_mismatch",
        "authorization_call_budget_exhausted",
        "input_token_limit_exceeded",
        "output_token_limit_exceeded",
        "provider_error",
        "provider_invalid_json",
        "provider_response_shape",
        "provider_sdk_unavailable",
        "provider_unconfigured",
        "schema_validation_failed",
        "settings_mutated",
        "synthetic_request_invalid",
        "authorization_history_detected",
    }
)


class CompactStageAHealthV3ProviderError(RuntimeError):
    """Safe provider-boundary category; never contains provider text."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "provider_error"
        super().__init__(self.code)


@dataclass(frozen=True)
class ProviderResponse:
    """In-memory response metadata; ``content`` is never serialized."""

    content: str
    model: str
    source: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    finish_reason: str
    reasoning_length: int


class OpenAICompatibleCompactStageAHealthV3Model:
    """Lazy, no-retry OpenAI-compatible adapter for one health call."""

    source = SOURCE_ID

    def __init__(self, config: DiagnosticProviderConfig) -> None:
        self.config = config
        self.model_id = MODEL_ID
        self.configured = bool(config.api_key)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.config.api_key:
            raise CompactStageAHealthV3ProviderError("provider_unconfigured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise CompactStageAHealthV3ProviderError("provider_sdk_unavailable") from exc
        kwargs: Dict[str, Any] = {
            "api_key": self.config.api_key,
            # The K24 authorization is one provider request with no retry.
            "max_retries": 0,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.timeout_seconds is not None:
            kwargs["timeout"] = self.config.timeout_seconds
        try:
            self._client = OpenAI(**kwargs)
        except TypeError as exc:
            # A client which cannot honor max_retries=0 is not safe for this
            # authorization.  Do not silently fall back to a retrying client.
            raise CompactStageAHealthV3ProviderError("provider_sdk_unavailable") from exc
        return self._client

    @staticmethod
    def _value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(key, default)
        return getattr(value, key, default)

    def complete(
        self,
        system_prompt: str,
        request: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Mapping[str, Any],
    ) -> ProviderResponse:
        client = self._get_client()
        # The request intentionally contains no response_format field.  The
        # only provider extension is the explicit per-call thinking disable.
        payload: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_json(request)},
            ],
            "max_tokens": int(max_output_tokens),
            "temperature": 0,
        }
        if extra_body:
            payload["extra_body"] = dict(extra_body)
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(**payload)
        except CompactStageAHealthV3ProviderError:
            raise
        except Exception as exc:
            raise CompactStageAHealthV3ProviderError("provider_error") from exc
        elapsed = max(0.0, (time.perf_counter() - started) * 1000.0)

        choices = self._value(response, "choices", ())
        if not isinstance(choices, (list, tuple)) or not choices:
            raise CompactStageAHealthV3ProviderError("provider_response_shape")
        first = choices[0]
        message = self._value(first, "message", None)
        content = self._value(message, "content", "")
        if not isinstance(content, str):
            raise CompactStageAHealthV3ProviderError("provider_response_shape")
        reasoning = self._value(message, "reasoning_content", "")
        if not isinstance(reasoning, str):
            reasoning = ""
        finish = self._value(first, "finish_reason", "unknown")
        finish_reason = str(finish or "unknown")[:80]

        usage = self._value(response, "usage", None)
        input_tokens = self._value(usage, "prompt_tokens", None)
        if input_tokens is None:
            input_tokens = self._value(usage, "input_tokens", 0)
        output_tokens = self._value(usage, "completion_tokens", None)
        if output_tokens is None:
            output_tokens = self._value(usage, "output_tokens", 0)
        try:
            input_count = max(0, int(input_tokens or 0))
            output_count = max(0, int(output_tokens or 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageAHealthV3ProviderError("provider_response_shape") from exc
        response_model = self._value(response, "model", self.model_id)
        return ProviderResponse(
            content=content,
            model=str(response_model or self.model_id),
            source=self.source,
            input_tokens=input_count,
            output_tokens=output_count,
            latency_ms=elapsed,
            finish_reason=finish_reason,
            reasoning_length=len(reasoning),
        )


def _safe_output_path(path: Union[str, Path]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in resolved.parts):
        raise ValueError("health_v3_refuses_frozen_output")
    if str(resolved) in {"", "."}:
        raise ValueError("health_v3_output_required")
    return resolved


def _safe_authority_root(path: Union[str, Path]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in resolved.parts):
        raise ValueError("health_v3_refuses_frozen_authority")
    return resolved


def _file_sha256(path: Union[str, Path]) -> str:
    candidate = Path(path)
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return ""


def _safe_error(value: Any) -> str:
    text = str(value or "provider_error")
    return text if text in _SAFE_ERROR_CODES else "provider_error"


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_latency(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _output_sha256(content: Optional[str]) -> str:
    if not isinstance(content, str):
        return ""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _body_free(value: Any) -> bool:
    """Recursively reject accidental body-bearing persisted values."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).casefold()
            is_ref = key.endswith(("_ref", "_refs", "_handle", "_handles"))
            if key in _BODY_KEYS and child not in (None, "", [], {}, ()) and not is_ref:
                return False
            if not _body_free(child):
                return False
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(_body_free(item) for item in value)
    return True


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    text = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    path.write_text((text + "\n") if text else "", encoding="utf-8")


def _request_scope(request: Mapping[str, Any]) -> Dict[str, str]:
    scope = request.get("s")
    if not isinstance(scope, Mapping):
        raise ValueError("synthetic_request_invalid")
    account = scope.get("a")
    chat = scope.get("c")
    if type(account) is not str or type(chat) is not str or not account or not chat:
        raise ValueError("synthetic_request_invalid")
    return {"account_id": account, "chat_id": chat}


def _classify_response(
    response: Optional[ProviderResponse],
    request: Mapping[str, Any],
) -> tuple[Optional[Mapping[str, Any]], str, str, Optional[str]]:
    """Validate in memory and return parsed result plus safe categories."""

    if response is None:
        return None, "not_run", "not_run", "provider_error"
    if response.model != MODEL_ID:
        return None, "not_run", "not_run", "provider_response_shape"
    if response.input_tokens > MAX_INPUT_TOKEN_PROXY:
        return None, "not_run", "not_run", "input_token_limit_exceeded"
    if response.output_tokens > MAX_OUTPUT_TOKENS or response.finish_reason not in {"", "stop"}:
        return None, "not_run", "not_run", "output_token_limit_exceeded"
    try:
        parsed = parse_compact_stage_a_output(response.content, request)
    except Exception as exc:
        code = str(getattr(exc, "code", "provider_invalid_json"))
        invalid_json = {
            "invalid_json",
            "json_not_text",
            "duplicate_json_key",
            "nonstandard_json_number",
        }
        if code in invalid_json:
            return None, "provider_invalid_json", "not_run", "provider_invalid_json"
        return None, "ok", "schema_validation_failed", "schema_validation_failed"
    return parsed, "ok", "ok", None


def _response_diagnostics(
    response: Optional[ProviderResponse],
    parse_code: str,
    validation_code: str,
    parsed: Optional[Mapping[str, Any]],
    topic_limit: int,
    error_code: Optional[str],
) -> Dict[str, Any]:
    return {
        "content_length": len(response.content) if response else 0,
        "output_sha256": _output_sha256(response.content if response else None),
        "input_tokens": _safe_nonnegative_int(response.input_tokens if response else 0),
        "output_tokens": _safe_nonnegative_int(response.output_tokens if response else 0),
        "latency_ms": _safe_latency(response.latency_ms if response else 0.0),
        "finish_reason": str(response.finish_reason if response else "not_run")[:80],
        "reasoning_length": _safe_nonnegative_int(response.reasoning_length if response else 0),
        "strict_parse_code": parse_code,
        "strict_validation_code": validation_code,
        "strict_complete": bool(parsed is not None and error_code is None),
        "topic_count": len(parsed.get("topics", ())) if isinstance(parsed, Mapping) else 0,
        "topic_limit": int(topic_limit),
        "error_code": error_code,
    }


def run_compact_stage_a_protocol_health_v3_real(
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    settings_path: Union[str, Path] = DEFAULT_SETTINGS_PATH,
    authority_root: Union[str, Path] = DEFAULT_AUTHORITY_ROOT,
) -> Dict[str, Any]:
    """Execute exactly one authorized synthetic health request.

    The return value is body-free.  A successful provider response is accepted
    only after the local v3 parser/validator passes.  No retry branch exists.
    """

    output_root = _safe_output_path(output_directory)
    if output_root.exists():
        raise FileExistsError("health_v3_output_is_immutable")
    settings_file = Path(settings_path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in settings_file.parts):
        raise ValueError("health_v3_refuses_frozen_settings")

    request = build_synthetic_health_request()
    request_sha256 = stable_hash(request)
    system_prompt_sha256 = stable_hash(SYSTEM_PROMPT)
    request_stats = measure_wire_size(request)
    topic_limit = topic_limit_for_request(request)
    request_scope = _request_scope(request)
    settings_before = _file_sha256(settings_file)

    config = DiagnosticProviderConfig.from_workbench_settings(
        settings_file,
        model_override=MODEL_ID,
        response_format_mode=RESPONSE_FORMAT_MODE,
        response_format_rationale="K24 compact Stage-A v3; response_format omitted",
        thinking_disabled=THINKING_DISABLED,
    )
    # Public configuration is metadata only; the API key stays in memory.
    config_public = config.public_dict()
    provider_model = OpenAICompatibleCompactStageAHealthV3Model(config)

    ledger = CallAuthorizationLedger.for_authorization(
        _safe_authority_root(authority_root),
        authorization_id=AUTHORIZATION_ID,
        max_calls=MAX_PROVIDER_CALLS,
        provider=PROVIDER_ID,
        model=MODEL_ID,
        protocol=PROTOCOL_VERSION,
        settings_sha256=settings_before,
        scope=request_scope,
        input_sha256=request_sha256,
        artifact_namespace=ARTIFACT_NAMESPACE,
    )
    preflight = ledger.snapshot()
    zero_history = bool(
        preflight.get("calls_used") == 0
        and preflight.get("reservation_count") == 0
        and preflight.get("rejection_count") == 0
    )

    provider_calls = 0
    retry_count = 0
    reservation: Any = None
    response: Optional[ProviderResponse] = None
    parsed: Optional[Mapping[str, Any]] = None
    parse_code = "not_run"
    validation_code = "not_run"
    error_code: Optional[str] = None

    if not zero_history:
        error_code = "authorization_history_detected"
    elif request_stats.http_token_proxy > MAX_INPUT_TOKEN_PROXY:
        error_code = "input_token_limit_exceeded"
    elif not config.configured:
        error_code = "provider_unconfigured"
    else:
        try:
            # This is the only point at which the authorization is consumed;
            # it commits before the provider adapter is entered.
            reservation = ledger.reserve(
                request_sha256=request_sha256,
                unit_ref="synthetic-health-v3",
                attempt=0,
                provider=PROVIDER_ID,
                model=MODEL_ID,
                protocol=PROTOCOL_VERSION,
                settings_sha256=settings_before,
                scope=request_scope,
                input_sha256=request_sha256,
                artifact_namespace=ARTIFACT_NAMESPACE,
                input_tokens_estimate=request_stats.http_token_proxy,
            )
        except CallBudgetExceeded:
            error_code = "authorization_call_budget_exhausted"
        except ReservationRejected as exc:
            error_code = _safe_error(getattr(exc, "code", "provider_error"))
        except AuthorizationBindingMismatch:
            error_code = "authorization_binding_mismatch"
        if reservation is not None:
            provider_calls = 1
            try:
                ledger.mark_started(reservation)
                response = provider_model.complete(
                    SYSTEM_PROMPT,
                    request,
                    max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                parsed, parse_code, validation_code, error_code = _classify_response(response, request)
            except CompactStageAHealthV3ProviderError as exc:
                error_code = _safe_error(exc.code)
            except Exception:
                # No exception message is allowed across this boundary.
                error_code = "provider_error"

    settings_after = _file_sha256(settings_file)
    if settings_before != settings_after and provider_calls:
        error_code = "settings_mutated"
    error_code = _safe_error(error_code) if error_code else None
    strict_complete = bool(response is not None and parsed is not None and error_code is None)
    if reservation is not None:
        input_tokens = _safe_nonnegative_int(response.input_tokens if response else 0)
        output_tokens = _safe_nonnegative_int(response.output_tokens if response else 0)
        latency_ms = _safe_latency(response.latency_ms if response else 0.0)
        try:
            if strict_complete:
                ledger.mark_complete(
                    reservation,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                )
            else:
                ledger.mark_failed(
                    reservation,
                    error_code=error_code or "provider_error",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                )
        except Exception:
            # The reservation remains consumed; do not retry or overwrite it.
            if strict_complete:
                strict_complete = False
                error_code = "provider_error"

    status = "available" if strict_complete else "blocked"
    response_diag = _response_diagnostics(
        response,
        parse_code,
        validation_code,
        parsed,
        topic_limit,
        error_code,
    )
    response_diag["strict_complete"] = strict_complete
    strict_result = {
        "strict_parse_code": parse_code,
        "strict_validation_code": validation_code,
        "strict_complete": strict_complete,
        "accepted_for_complete": strict_complete,
        "topic_count": response_diag["topic_count"],
        "topic_limit": topic_limit,
        "diagnostic_candidate_separate": True,
    }
    diagnostic_candidate = {
        "present": response is not None,
        "json_object_candidate": bool(response and response.content.lstrip().startswith("{")),
        "candidate_sha256": _output_sha256(response.content if response else None),
        "schema_valid": parsed is not None,
        "accepted_for_complete": False,
        "diagnostic_only": True,
    }
    ledger_snapshot = ledger.snapshot()
    # Keep the exported reservation rows byte-for-byte equivalent to the
    # durable ledger's body-free ``records()`` projection.  Additional
    # request/response diagnostics live in aggregate/diagnostic; adding them
    # here would invalidate ``ledger_rows_sha256`` and break replay audits.
    ledger_rows = [dict(raw_row) for raw_row in ledger.records()]
    for raw_row in ledger.rejections():
        ledger_rows.append(
            {
                "record_type": "preflight_rejection",
                "authorization_id": raw_row.get("authorization_id", AUTHORIZATION_ID),
                "request_sha256": raw_row.get("request_sha256", ""),
                "unit_ref_sha256": raw_row.get("unit_ref_sha256", ""),
                "attempt": raw_row.get("attempt", 0),
                "error_code": _safe_error(raw_row.get("code", "provider_error")),
            }
        )
    authorization = {
        "authorization_id": AUTHORIZATION_ID,
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "protocol": PROTOCOL_VERSION,
        "scope_sha256": stable_hash(request_scope),
        "settings_sha256": settings_before,
        "input_sha256": request_sha256,
        "artifact_namespace": ARTIFACT_NAMESPACE,
        "max_calls": MAX_PROVIDER_CALLS,
        "preflight_zero_history": zero_history,
    }
    errors = (
        [
            {
                "phase": "synthetic_health_v3",
                "error_code": error_code,
                "provider_calls": provider_calls,
                "authorization_id": AUTHORIZATION_ID,
            }
        ]
        if error_code
        else []
    )
    provider_public = {
        **dict(config_public),
        "id": PROVIDER_ID,
        "model": MODEL_ID,
        "source": str(response.source if response else SOURCE_ID),
        "calls": provider_calls,
        "call_limit": MAX_PROVIDER_CALLS,
        "retry_count": retry_count,
        "max_retries": 0,
    }
    request_public = {
        "message_count": sum(row["k"] == "m" for row in request["h"]),
        "candidate_count": sum(row["k"] == "c" for row in request["h"]),
        "primary_count": topic_limit,
        "topic_limit": topic_limit,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "request_sha256": request_sha256,
        "system_prompt_sha256": system_prompt_sha256,
        "user_packet_sha256": request_sha256,
        "input_token_proxy": request_stats.http_token_proxy,
        "input_token_proxy_limit": MAX_INPUT_TOKEN_PROXY,
    }
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "status": status,
        "success": strict_complete,
        "health_complete": strict_complete,
        "diagnostic_only": True,
        "synthetic_only": True,
        "production_blocked": True,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": retry_count,
        "development_input_read": False,
        "private_input_read": False,
        "development_calls": 0,
        "stage_a_development": False,
        "stage_b_pilot": False,
        "stage_c_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": provider_public,
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": {
            "sent": bool(provider_calls),
            "field_names": ["thinking"],
            "value_shape": "disabled",
            "per_call": True,
            "global_settings_mutated": False,
        },
        "authorization": authorization,
        "authorization_ledger": ledger_snapshot,
        "authorization_preflight": {
            "zero_history": zero_history,
            "calls_used_before": preflight.get("calls_used", 0),
            "reservation_count_before": preflight.get("reservation_count", 0),
            "rejection_count_before": preflight.get("rejection_count", 0),
        },
        "request": request_public,
        "strict_result": strict_result,
        "diagnostic_candidate": diagnostic_candidate,
        "response_diagnostics": response_diag,
        "settings_before_sha256": settings_before,
        "settings_after_sha256": settings_after,
        "settings_unchanged": bool(settings_before and settings_before == settings_after),
        "errors": {"count": len(errors), "codes": sorted({row["error_code"] for row in errors})},
    }
    cost = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "provider_calls": provider_calls,
        "retry_count": retry_count,
        "input_tokens": response_diag["input_tokens"],
        "output_tokens": response_diag["output_tokens"],
        "latency_ms": response_diag["latency_ms"],
        "input_token_proxy": request_stats.http_token_proxy,
        "input_token_proxy_limit": MAX_INPUT_TOKEN_PROXY,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "cache_hit": False,
        "authorization_calls_used": ledger_snapshot.get("calls_used", provider_calls),
        "authorization_calls_remaining": ledger_snapshot.get("calls_remaining", 0),
    }
    diagnostic = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "strict_result": strict_result,
        "diagnostic_candidate": diagnostic_candidate,
        "response_diagnostics": response_diag,
        "provider": {
            "model": MODEL_ID,
            "source": provider_public["source"],
            "latency_ms": response_diag["latency_ms"],
        },
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "thinking_disabled": THINKING_DISABLED,
    }
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "local_day": "2026-08-25",
        "split": "synthetic",
        "status": status,
        "success": strict_complete,
        "health_complete": strict_complete,
        "diagnostic_only": True,
        "synthetic_only": True,
        "production_blocked": True,
        "provider_called": provider_calls > 0,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": retry_count,
        "development_input_read": False,
        "private_input_read": False,
        "development_calls": 0,
        "stage_a_development": False,
        "stage_b_pilot": False,
        "stage_c_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": provider_public,
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": aggregate["extra_body"],
        "authorization": authorization,
        "authorization_ledger": ledger_snapshot,
        "settings_before_sha256": settings_before,
        "settings_after_sha256": settings_after,
        "settings_unchanged": bool(settings_before and settings_before == settings_after),
        "output_files": dict(OUTPUT_FILENAMES),
    }
    outputs: Dict[str, Any] = {
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": diagnostic,
        "ledger": ledger_rows,
        "errors": errors,
    }
    if not all(_body_free(value) for value in outputs.values()):
        raise AssertionError("health_v3_body_free_violation")

    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_json(output_root / OUTPUT_FILENAMES["diagnostic"], diagnostic)
    _write_jsonl(output_root / OUTPUT_FILENAMES["ledger"], ledger_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _file_sha256(output_root / filename)
        for key, filename in OUTPUT_FILENAMES.items()
        if key != "manifest"
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    return {
        "output_directory": str(output_root),
        "status": status,
        "success": strict_complete,
        "health_complete": strict_complete,
        "provider_calls": provider_calls,
        "retry_count": retry_count,
        "error_code": error_code,
        "artifact_paths": {
            key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()
        },
        "aggregate": aggregate,
    }


run_stage_a_compact_health_v3_real = run_compact_stage_a_protocol_health_v3_real
run_compact_stage_a_health_probe_v3_real = run_compact_stage_a_protocol_health_v3_real


__all__ = [
    "ARTIFACT_VERSION",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "MODEL_ID",
    "AUTHORIZATION_ID",
    "MAX_PROVIDER_CALLS",
    "MAX_RETRIES",
    "RESPONSE_FORMAT_MODE",
    "THINKING_DISABLED",
    "DEFAULT_AUTHORITY_ROOT",
    "DEFAULT_SETTINGS_PATH",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "OpenAICompatibleCompactStageAHealthV3Model",
    "run_compact_stage_a_protocol_health_v3_real",
    "run_stage_a_compact_health_v3_real",
    "run_compact_stage_a_health_probe_v3_real",
]


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--settings-path", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--authority-root", type=Path, default=DEFAULT_AUTHORITY_ROOT)
    args = parser.parse_args(argv)
    result = run_compact_stage_a_protocol_health_v3_real(
        args.output_directory,
        settings_path=args.settings_path,
        authority_root=args.authority_root,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "success": result["success"],
                "health_complete": result["health_complete"],
                "provider_calls": result["provider_calls"],
                "retry_count": result["retry_count"],
                "error_code": result["error_code"],
                "output_directory": result["output_directory"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
