"""Synthetic-only health facade for the K22 Stage-A v3 wire.

The default call is deliberately no-call and writes a blocked diagnostic only
when a caller explicitly asks it to write an artifact.  A model may be
injected by an offline synthetic test; this module has no HTTP client and does
not read development, private, or frozen data.  It exists so future health
experiments have a v3 default path rather than silently reusing the v2 short
key protocol.  It must not be used as authorization for development, Stage B,
Stage C, or production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Union

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
)


ARTIFACT_VERSION = "compact_stage_a_protocol_health_v3"
REPORT_SCHEMA_VERSION = "compact_stage_a_protocol_health_report_v3"
RUNNER_SCHEMA_VERSION = "compact_stage_a_protocol_health_runner_v3"
MODEL_ID = "deepseek-v4-flash"
PROVIDER_ID = "openai-compatible"
SOURCE_ID = "synthetic"
RESPONSE_FORMAT_MODE = "omitted"
THINKING_DISABLED = True
MAX_PROVIDER_CALLS = 1
MAX_RETRIES = 0
HEALTH_MAX_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
DEFAULT_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/compact_stage_a_protocol_health_v3"
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
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CompactStageAHealthV3Model(Protocol):
    model_id: str
    source: str
    configured: bool

    def complete(
        self,
        system_prompt: str,
        request: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Mapping[str, Any],
    ) -> Any:
        ...


@dataclass(frozen=True)
class CompactStageAHealthV3Response:
    content: str
    model: str = MODEL_ID
    source: str = SOURCE_ID
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    reasoning_length: int = 0


@dataclass
class FakeCompactStageAHealthV3Model:
    """Small in-memory model used only by synthetic tests."""

    content: Optional[str] = None
    model_id: str = MODEL_ID
    source: str = SOURCE_ID
    configured: bool = True
    input_tokens: int = 64
    output_tokens: int = 20
    latency_ms: float = 1.0
    finish_reason: str = "stop"
    reasoning_length: int = 0
    calls: list[Dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def valid_content(request: Mapping[str, Any]) -> str:
        primary = [str(row["i"]) for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
        context = [str(row["i"]) for row in request["h"] if row["k"] == "m" and row["r"] == "c"]
        return canonical_json(
            {
                "topics": [
                    {
                        "topic_id": "t1",
                        "primary_message_ids": primary,
                        "context_message_ids": context,
                        "uncertainty": "certain",
                    }
                ]
            }
        )

    def complete(
        self,
        system_prompt: str,
        request: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Mapping[str, Any],
    ) -> CompactStageAHealthV3Response:
        self.calls.append(
            {
                "system_prompt_sha256": stable_hash(system_prompt),
                "request_sha256": stable_hash(request),
                "max_output_tokens": int(max_output_tokens),
                "extra_body_field_names": sorted(str(key) for key in extra_body),
            }
        )
        return CompactStageAHealthV3Response(
            content=self.content if self.content is not None else self.valid_content(request),
            model=self.model_id,
            source=self.source,
            input_tokens=int(self.input_tokens),
            output_tokens=int(self.output_tokens),
            latency_ms=float(self.latency_ms),
            finish_reason=self.finish_reason,
            reasoning_length=int(self.reasoning_length),
        )


FakeStageAHealthV3Model = FakeCompactStageAHealthV3Model
SyntheticCompactStageAHealthV3Model = FakeCompactStageAHealthV3Model


@dataclass(frozen=True)
class CompactStageAHealthV3RunResult:
    output_directory: str
    status: str
    success: bool
    strict_complete: bool
    provider_calls: int
    retry_count: int
    error_code: Optional[str]
    artifact_paths: Mapping[str, str]
    aggregate: Mapping[str, Any]

    @property
    def health_ok(self) -> bool:
        return self.strict_complete

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_directory": self.output_directory,
            "status": self.status,
            "success": self.success,
            "strict_complete": self.strict_complete,
            "provider_calls": self.provider_calls,
            "retry_count": self.retry_count,
            "error_code": self.error_code,
            "artifact_paths": dict(self.artifact_paths),
        }


HealthRunResult = CompactStageAHealthV3RunResult


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_output_path(path: Union[str, Path]) -> Path:
    result = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in result.parts):
        raise ValueError("health_v3_refuses_frozen_output")
    return result


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    text = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows
    )
    path.write_text((text + "\n") if text else "", encoding="utf-8")


def _synthetic_request() -> Dict[str, Any]:
    scope = {"account_id": "account-k22-synthetic", "chat_id": "chat-k22-synthetic"}
    messages = [
        {
            "message_handle": f"{scope['account_id']}/{scope['chat_id']}|message|M001",
            "text": "synthetic health primary",
            "role": "primary",
        },
        {
            "message_handle": f"{scope['account_id']}/{scope['chat_id']}|message|M002",
            "text": "synthetic health context",
            "role": "context",
        },
    ]
    candidates = [
        {
            "candidate_handle": f"{scope['account_id']}/{scope['chat_id']}|candidate|C001",
            "relation": "continuity",
        }
    ]
    return build_compact_stage_a_request(scope, messages, candidates)


def build_synthetic_health_request() -> Dict[str, Any]:
    return _synthetic_request()


def _coerce_response(raw: Any, model: Any) -> CompactStageAHealthV3Response:
    if isinstance(raw, CompactStageAHealthV3Response):
        return raw
    if not isinstance(raw, Mapping) or type(raw.get("content")) is not str:
        raise ValueError("provider_response_shape")
    try:
        return CompactStageAHealthV3Response(
            content=raw["content"],
            model=str(raw.get("model", getattr(model, "model_id", MODEL_ID))),
            source=str(raw.get("source", getattr(model, "source", SOURCE_ID))),
            input_tokens=int(raw.get("input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
            latency_ms=float(raw.get("latency_ms", 0.0)),
            finish_reason=str(raw.get("finish_reason", "stop")),
            reasoning_length=int(raw.get("reasoning_length", raw.get("reasoning_content_length", 0))),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("provider_response_metadata") from exc


def _body_free(value: Any) -> bool:
    """Check only the synthetic marker; no provider response is persisted."""

    return "K22_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED" not in canonical_json(value)


def run_compact_stage_a_protocol_health_v3(
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    model: Optional[CompactStageAHealthV3Model] = None,
    provider: Optional[CompactStageAHealthV3Model] = None,
    request: Optional[Mapping[str, Any]] = None,
) -> CompactStageAHealthV3RunResult:
    """Run at most one explicitly injected synthetic health exchange.

    With no injected model this function performs zero model calls and writes
    a blocked, body-free artifact.  There is no retry, no development input,
    and no path that can invoke a real provider.
    """

    if model is not None and provider is not None and model is not provider:
        raise ValueError("health_v3_model_provider_alias_mismatch")
    model_object = model if model is not None else provider
    output_root = _safe_output_path(output_directory)
    if output_root.exists():
        raise FileExistsError("health_v3_output_is_immutable")
    request_value = dict(request) if request is not None else _synthetic_request()
    request_stats = measure_wire_size(request_value)
    request_hash = stable_hash(request_value)
    provider_calls = 0
    error_code: Optional[str] = None
    response: Optional[CompactStageAHealthV3Response] = None
    parsed: Optional[Mapping[str, Any]] = None
    parse_code = "not_run"
    validation_code = "not_run"

    if model_object is None:
        error_code = "synthetic_model_required"
    elif not bool(getattr(model_object, "configured", True)):
        error_code = "provider_unconfigured"
    elif request_stats.http_token_proxy > MAX_INPUT_TOKEN_PROXY:
        error_code = "health_input_token_proxy_exceeded"
    else:
        provider_calls = 1
        try:
            raw = model_object.complete(
                SYSTEM_PROMPT,
                request_value,
                max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
                extra_body={"thinking": {"type": "disabled"}},
            )
            response = _coerce_response(raw, model_object)
            if response.input_tokens < 0 or response.output_tokens < 0 or response.latency_ms < 0:
                error_code = "provider_response_metadata"
            elif response.input_tokens > MAX_INPUT_TOKEN_PROXY:
                error_code = "input_token_limit_exceeded"
            elif response.output_tokens > MAX_OUTPUT_TOKENS or response.finish_reason not in {"", "stop"}:
                error_code = "output_token_limit_exceeded"
            else:
                try:
                    parsed = parse_compact_stage_a_output(response.content, request_value)
                    parse_code = "ok"
                    validation_code = "ok"
                except Exception as exc:
                    error_code = str(getattr(exc, "code", "provider_invalid_json"))
                    parse_code = "provider_invalid_json" if error_code == "invalid_json" else "ok"
                    validation_code = "not_run" if parse_code != "ok" else error_code
        except Exception:
            error_code = "provider_error"

    strict_complete = bool(response is not None and parsed is not None and error_code is None)
    status = "available" if strict_complete else "blocked"
    result_diagnostics: Dict[str, Any] = {
        "content_length": len(response.content) if response else 0,
        "output_sha256": _sha256_bytes(response.content.encode("utf-8")) if response else "",
        "input_tokens": max(0, int(response.input_tokens)) if response else 0,
        "output_tokens": max(0, int(response.output_tokens)) if response else 0,
        "latency_ms": max(0.0, float(response.latency_ms)) if response else 0.0,
        "finish_reason": str(response.finish_reason) if response else "not_run",
        "reasoning_length": max(0, int(response.reasoning_length)) if response else 0,
        "strict_parse_code": parse_code,
        "strict_validation_code": validation_code,
        "strict_complete": strict_complete,
    }
    diagnostic_candidate = {
        "present": bool(response),
        "schema_valid": bool(parsed),
        "accepted_for_complete": False,
        "diagnostic_only": True,
    }
    strict_result = {
        "strict_parse_code": parse_code,
        "strict_validation_code": validation_code,
        "strict_complete": strict_complete,
        "accepted_for_complete": strict_complete,
        "diagnostic_candidate_separate": True,
    }
    errors = ([{"phase": "synthetic_health", "error_code": error_code}] if error_code else [])
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "status": status,
        "success": strict_complete,
        "synthetic_only": True,
        "diagnostic_only": True,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": 0,
        "development_input_read": False,
        "private_input_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "request": {
            "message_count": sum(row["k"] == "m" for row in request_value["h"]),
            "candidate_count": sum(row["k"] == "c" for row in request_value["h"]),
            "request_sha256": request_hash,
            "input_token_proxy": request_stats.http_token_proxy,
            "input_token_proxy_limit": MAX_INPUT_TOKEN_PROXY,
        },
        "strict_result": strict_result,
        "diagnostic_candidate": diagnostic_candidate,
        "response_diagnostics": result_diagnostics,
        "errors": {"count": len(errors), "codes": sorted({row["error_code"] for row in errors})},
    }
    cost = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "provider_calls": provider_calls,
        "retry_count": 0,
        "input_tokens": result_diagnostics["input_tokens"],
        "output_tokens": result_diagnostics["output_tokens"],
        "latency_ms": result_diagnostics["latency_ms"],
        "input_token_proxy": request_stats.http_token_proxy,
        "input_token_proxy_limit": MAX_INPUT_TOKEN_PROXY,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "cache_version": CACHE_VERSION,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
    }
    diagnostic = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "strict_result": strict_result,
        "diagnostic_candidate": diagnostic_candidate,
        "response_diagnostics": result_diagnostics,
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
    }
    ledger = [
        {
            "status": "complete" if strict_complete else ("failed" if provider_calls else "not_run"),
            "provider_call": provider_calls == 1,
            "request_sha256": request_hash,
            "output_sha256": result_diagnostics["output_sha256"],
            "error_code": error_code,
        }
    ] if provider_calls else []
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "local_day": "2026-08-25",
        "split": "synthetic",
        "status": status,
        "success": strict_complete,
        "diagnostic_only": True,
        "synthetic_only": True,
        "provider_called": provider_calls > 0,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": 0,
        "development_input_read": False,
        "private_input_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": {
            "id": PROVIDER_ID,
            "model": str(getattr(response, "model", MODEL_ID)),
            "source": str(getattr(response, "source", SOURCE_ID)),
        },
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    outputs: Dict[str, Any] = {
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": diagnostic,
        "ledger": ledger,
        "errors": errors,
    }
    if not all(_body_free(value) for value in outputs.values()):
        raise AssertionError("health_v3_body_free_violation")
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_json(output_root / OUTPUT_FILENAMES["diagnostic"], diagnostic)
    _write_jsonl(output_root / OUTPUT_FILENAMES["ledger"], ledger)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _sha256_bytes((output_root / filename).read_bytes())
        for key, filename in OUTPUT_FILENAMES.items()
        if key != "manifest"
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return CompactStageAHealthV3RunResult(
        output_directory=str(output_root),
        status=status,
        success=strict_complete,
        strict_complete=strict_complete,
        provider_calls=provider_calls,
        retry_count=0,
        error_code=error_code,
        artifact_paths=paths,
        aggregate=aggregate,
    )


run_stage_a_compact_health_v3 = run_compact_stage_a_protocol_health_v3
run_compact_stage_a_health_v3 = run_compact_stage_a_protocol_health_v3
run_compact_stage_a_health_probe_v3 = run_compact_stage_a_protocol_health_v3
build_health_request_v3 = build_synthetic_health_request


class CompactStageAProtocolHealthV3Runner:
    def __init__(self, output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY, **kwargs: Any) -> None:
        self.output_directory = output_directory
        self._kwargs = dict(kwargs)

    def run(
        self,
        *,
        model: Optional[CompactStageAHealthV3Model] = None,
        provider: Optional[CompactStageAHealthV3Model] = None,
        **kwargs: Any,
    ) -> CompactStageAHealthV3RunResult:
        options = dict(self._kwargs)
        options.update(kwargs)
        return run_compact_stage_a_protocol_health_v3(
            self.output_directory, model=model, provider=provider, **options
        )


CompactStageAHealthV3Runner = CompactStageAProtocolHealthV3Runner

__all__ = [
    "ARTIFACT_VERSION",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "PROTOCOL_VERSION",
    "PROMPT_VERSION",
    "CACHE_VERSION",
    "MODEL_ID",
    "PROVIDER_ID",
    "SOURCE_ID",
    "RESPONSE_FORMAT_MODE",
    "THINKING_DISABLED",
    "MAX_PROVIDER_CALLS",
    "MAX_RETRIES",
    "HEALTH_MAX_OUTPUT_TOKENS",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "OUTPUT_FILENAMES",
    "CompactStageAHealthV3Model",
    "CompactStageAHealthV3Response",
    "FakeCompactStageAHealthV3Model",
    "FakeStageAHealthV3Model",
    "SyntheticCompactStageAHealthV3Model",
    "CompactStageAHealthV3RunResult",
    "HealthRunResult",
    "build_synthetic_health_request",
    "build_health_request_v3",
    "run_compact_stage_a_protocol_health_v3",
    "run_stage_a_compact_health_v3",
    "run_compact_stage_a_health_v3",
    "run_compact_stage_a_health_probe_v3",
    "CompactStageAProtocolHealthV3Runner",
    "CompactStageAHealthV3Runner",
]
