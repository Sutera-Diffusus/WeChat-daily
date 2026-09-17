"""K13 one-shot synthetic Stage-A protocol health probe.

The K12 diagnostic established that the configured vision model with JSON mode
was not a usable protocol path.  K13 repeats exactly one synthetic health
request on the already-reviewed non-vision ``deepseek-v4-flash`` path, with
``response_format`` omitted and ``thinking_disabled`` sent explicitly per
call.  The request is still validated against the unchanged small Stage-A
schema; a fence or prose wrapper is diagnostic evidence only.

No development input is accepted by this API.  Response text/reasoning exists
only while the provider call is being classified and is never written.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Union

from .linear_stage_a_protocol_diagnostic import (
    DEFAULT_CAPABILITY_DIRECTORIES,
    DEFAULT_K11_ARTIFACT_DIRECTORY,
    DEFAULT_SETTINGS_PATH,
    FALLBACK_MODEL,
    HEALTH_MAX_INPUT_PROXY,
    STAGE_A_SYSTEM_PROMPT,
    DiagnosticProviderConfig,
    DiagnosticProviderResponse,
    DiagnosticRunResult,
    DiagnosticStageProvider,
    DeepSeekProtocolDiagnosticProvider,
    _assert_body_free,
    _candidate_result_dict,
    _coerce_provider_response,
    _empty_diagnostics,
    _health_request,
    _parse_diagnostics,
    _response_diagnostic_dict,
    _safe_error_code,
    _selected_opaque_refs,
    _strict_result_dict,
    _token_proxy,
    _write_json,
    _write_jsonl,
    inspect_prior_state,
    stable_hash,
)


ARTIFACT_VERSION = "linear_stage_a_protocol_health_v2"
REPORT_SCHEMA_VERSION = "linear_stage_a_protocol_health_report_v2"
RUNNER_SCHEMA_VERSION = "linear_stage_a_protocol_health_runner_v2"
HEALTH_MAX_OUTPUT_TOKENS = 400
MAX_PROVIDER_CALLS = 1
MAX_RETRIES = 0
RESPONSE_FORMAT_MODE = "omitted"
THINKING_DISABLED = True
DEFAULT_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_a_protocol_health_v2"
)
OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "diagnostic": "diagnostic.private.json",
    "ledger": "ledger.private.jsonl",
    "errors": "errors.private.jsonl",
}


def _file_sha256(path: Union[str, Path]) -> str:
    candidate = Path(path)
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return ""


def _safe_path(path: Union[str, Path]) -> Path:
    result = Path(path).expanduser().resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in result.parts):
        raise ValueError("health_refuses_frozen_path")
    return result


def _rationale() -> str:
    return (
        "Predeclared omitted response_format: the reviewed non-vision "
        "deepseek-v4-flash health path succeeded with response_format_sent=false; "
        "K11 vision-exp json_object health failed as provider_invalid_json. "
        "thinking_disabled is sent explicitly per call via extra_body."
    )


def _safe_extra_body_metadata(provider_called: bool) -> Dict[str, Any]:
    return {
        "sent": bool(provider_called),
        "field_names": ["thinking"],
        "value_shape": "disabled",
        "per_call": True,
        "global_settings_mutated": False,
    }


def _write_health_artifact(
    *,
    output_directory: Path,
    prior: Any,
    config_public: Mapping[str, Any],
    request: Mapping[str, Any],
    response: Optional[DiagnosticProviderResponse],
    parsed: Any,
    provider_calls: int,
    error_code: Optional[str],
    settings_before_sha256: str,
    settings_after_sha256: str,
) -> DiagnosticRunResult:
    health_ok = bool(parsed.strict_complete and error_code is None)
    status = "available" if health_ok else "blocked"
    source = str((response.source if response else config_public.get("source")) or "unknown")
    model = str((response.model if response else config_public.get("model")) or FALLBACK_MODEL)
    latency = max(0.0, float(response.latency_ms if response else 0.0))
    input_tokens = max(0, int(response.input_tokens if response else 0))
    output_tokens = max(0, int(response.output_tokens if response else 0))
    request_sha256 = stable_hash(
        {
            "runner_schema_version": RUNNER_SCHEMA_VERSION,
            "stage_schema_version": "stage_a_topic_map_small_v1",
            "stage": "A",
            "model": model,
            "source": source,
            "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
            "user_packet_sha256": stable_hash(request),
        }
    )
    diagnostic = _response_diagnostic_dict(response, parsed)
    diagnostic.update(
        {
            "model": model,
            "source": source,
            "latency_ms": latency,
            "thinking_disabled": THINKING_DISABLED,
            "response_format_mode": RESPONSE_FORMAT_MODE,
        }
    )
    strict_result = _strict_result_dict(parsed)
    candidate_result = _candidate_result_dict(parsed)
    ledger = {
        "phase": "synthetic_protocol_health",
        "page_id": str(request["page_id"]),
        "root_id": str(request["root_id"]),
        "status": status,
        "error_code": error_code,
        "provider_call": bool(provider_calls),
        "provider_calls": provider_calls,
        "retry_count": MAX_RETRIES,
        "request_sha256": request_sha256,
        "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
        "user_packet_sha256": stable_hash(request),
        "source": source,
        "model": model,
        "latency_ms": latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "content_length": diagnostic["content_length"],
        "reasoning_length": diagnostic["reasoning_length"],
        "finish_reasons": diagnostic["finish_reasons"],
        "output_sha256": diagnostic["output_sha256"],
        "leading_shape": diagnostic["leading_shape"],
        "fence_detected": diagnostic["fence_detected"],
        "unique_json_object_count": diagnostic["unique_json_object_count"],
        "strict_parse_code": diagnostic["strict_parse_code"],
        "strict_validation_code": diagnostic["strict_validation_code"],
        "safe_public_field_names": diagnostic["safe_public_field_names"],
        "thinking_disabled": THINKING_DISABLED,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "cache_hit": False,
        "selected_opaque_refs": _selected_opaque_refs(request),
    }
    errors = (
        [
            {
                "phase": "synthetic_protocol_health",
                "error_code": error_code,
                "model": model,
                "source": source,
            }
        ]
        if error_code
        else []
    )
    provider = {
        **dict(config_public),
        "model": model,
        "source": source,
        "calls": provider_calls,
        "call_limit": MAX_PROVIDER_CALLS,
        "within_call_limit": provider_calls <= MAX_PROVIDER_CALLS,
        "thinking_disabled": THINKING_DISABLED,
    }
    response_format = {
        "mode": RESPONSE_FORMAT_MODE,
        "sent": False,
        "predeclared": True,
        "rationale": _rationale(),
    }
    cost = {
        "provider_calls": provider_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency,
        "retry_count": MAX_RETRIES,
        "max_output_tokens": HEALTH_MAX_OUTPUT_TOKENS,
        "input_token_proxy": _token_proxy(request, STAGE_A_SYSTEM_PROMPT),
        "input_token_proxy_limit": HEALTH_MAX_INPUT_PROXY,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
    }
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "status": status,
        "success": health_ok,
        "diagnostic_only": True,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "diagnostic_call_count": provider_calls,
        "retry_count": MAX_RETRIES,
        "development_input_read": False,
        "development_calls": 0,
        "stage_a_development": False,
        "stage_b_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": provider,
        "response_format": response_format,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": _safe_extra_body_metadata(bool(provider_calls)),
        "prior_state": prior.to_dict(),
        "health_request": {
            "page_id": str(request["page_id"]),
            "root_id": str(request["root_id"]),
            "input_token_proxy": _token_proxy(request, STAGE_A_SYSTEM_PROMPT),
            "input_token_proxy_limit": HEALTH_MAX_INPUT_PROXY,
            "max_output_tokens": HEALTH_MAX_OUTPUT_TOKENS,
            "request_sha256": request_sha256,
            "user_packet_sha256": stable_hash(request),
        },
        "strict_result": strict_result,
        "diagnostic_candidate": candidate_result,
        "response_diagnostics": diagnostic,
        "cost": cost,
        "settings_before_sha256": settings_before_sha256,
        "settings_after_sha256": settings_after_sha256,
        "settings_unchanged": bool(settings_before_sha256 and settings_before_sha256 == settings_after_sha256),
        "errors": {"count": len(errors), "codes": sorted({str(item["error_code"]) for item in errors})},
    }
    diagnostic_file = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "strict_result": strict_result,
        "diagnostic_candidate": candidate_result,
        "response_diagnostics": diagnostic,
        "provider": {"model": model, "source": source, "latency_ms": latency},
        "thinking_disabled": THINKING_DISABLED,
        "response_format_mode": RESPONSE_FORMAT_MODE,
    }
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "local_day": "2026-08-25",
        "output_directory_name": output_directory.name,
        "status": status,
        "success": health_ok,
        "diagnostic_only": True,
        "provider_called": bool(provider_calls),
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "diagnostic_call_count": provider_calls,
        "retry_count": MAX_RETRIES,
        "development_input_read": False,
        "development_calls": 0,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": provider,
        "response_format": response_format,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": _safe_extra_body_metadata(bool(provider_calls)),
        "settings_before_sha256": settings_before_sha256,
        "settings_after_sha256": settings_after_sha256,
        "settings_unchanged": bool(settings_before_sha256 and settings_before_sha256 == settings_after_sha256),
        "prior_k11_artifact_directory_name": "linear_stage_a_pilot_v1",
        "fallback_model": FALLBACK_MODEL,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    outputs = {
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": diagnostic_file,
        "ledger": [ledger],
        "errors": errors,
    }
    for label, value in outputs.items():
        _assert_body_free(value, label=label)
    _assert_body_free(manifest, label="manifest")
    output_directory.mkdir(parents=True, exist_ok=False)
    _write_json(output_directory / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_directory / OUTPUT_FILENAMES["cost"], cost)
    _write_json(output_directory / OUTPUT_FILENAMES["diagnostic"], diagnostic_file)
    _write_jsonl(output_directory / OUTPUT_FILENAMES["ledger"], outputs["ledger"])
    _write_jsonl(output_directory / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _file_sha256(output_directory / filename)
        for key, filename in OUTPUT_FILENAMES.items()
        if key != "manifest"
    }
    _write_json(output_directory / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_directory / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return DiagnosticRunResult(
        output_directory=str(output_directory),
        status=status,
        success=health_ok,
        provider_calls=provider_calls,
        retry_count=MAX_RETRIES,
        health_ok=health_ok,
        aggregate=aggregate,
        artifact_paths=paths,
    )


def run_linear_stage_a_protocol_health_v2(
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    k11_artifact_directory: Union[str, Path] = DEFAULT_K11_ARTIFACT_DIRECTORY,
    capability_directories: Sequence[Union[str, Path]] = DEFAULT_CAPABILITY_DIRECTORIES,
    settings_path: Union[str, Path] = DEFAULT_SETTINGS_PATH,
    provider: Optional[DiagnosticStageProvider] = None,
    config: Optional[DiagnosticProviderConfig] = None,
    clock: Optional[Callable[[], float]] = None,
) -> DiagnosticRunResult:
    """Perform at most one synthetic health request and persist metadata only."""

    output_root = _safe_path(output_directory)
    if output_root.exists():
        raise FileExistsError("health_output_is_immutable")
    prior = inspect_prior_state(k11_artifact_directory, capability_directories)
    request = _health_request()
    settings_before = _file_sha256(settings_path)
    selected_model = prior.fallback_model or FALLBACK_MODEL
    selected_config = config or DiagnosticProviderConfig.from_workbench_settings(
        settings_path,
        model_override=selected_model,
        response_format_mode=RESPONSE_FORMAT_MODE,
        response_format_rationale=_rationale(),
        thinking_disabled=THINKING_DISABLED,
    )
    # K13 is deliberately fixed to the reviewed text model, omitted response
    # format, and explicit per-call thinking disable.  Any injected config is
    # normalized in memory; WorkbenchSettings is never changed.
    selected_config = replace(
        selected_config,
        model=FALLBACK_MODEL,
        response_format_mode=RESPONSE_FORMAT_MODE,
        response_format_rationale=_rationale(),
        thinking_disabled=THINKING_DISABLED,
    )
    config_public = selected_config.public_dict()
    provider_object: Optional[DiagnosticStageProvider] = provider
    if provider_object is None and prior.allowed and selected_config.configured:
        provider_object = DeepSeekProtocolDiagnosticProvider(selected_config, clock=clock)
    provider_calls = 0
    response: Optional[DiagnosticProviderResponse] = None
    parsed = _empty_diagnostics("not_run")
    error_code: Optional[str] = None
    input_proxy = _token_proxy(request, STAGE_A_SYSTEM_PROMPT)
    if input_proxy > HEALTH_MAX_INPUT_PROXY:
        error_code = "health_input_proxy_exceeded"
    elif not prior.allowed:
        error_code = prior.errors[0] if prior.errors else "prior_gate_blocked"
    elif provider_object is None or not bool(getattr(provider_object, "configured", True)):
        error_code = "provider_unconfigured"
    elif str(getattr(provider_object, "model_id", FALLBACK_MODEL)) != FALLBACK_MODEL:
        error_code = "diagnostic_model_override_refused"
    else:
        provider_calls = 1
        try:
            raw = provider_object.complete(
                "A",
                STAGE_A_SYSTEM_PROMPT,
                request,
                max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
            )
            response = _coerce_provider_response(raw, provider_object)
            parsed = _parse_diagnostics(response, request)
            if response.input_tokens > HEALTH_MAX_INPUT_PROXY:
                error_code = "health_input_tokens_exceeded"
            elif response.output_tokens > HEALTH_MAX_OUTPUT_TOKENS:
                error_code = "health_output_tokens_exceeded"
            elif not parsed.strict_complete:
                error_code = parsed.strict_parse_code if parsed.strict_parse_code != "ok" else parsed.strict_validation_code
        except Exception as exc:
            error_code = _safe_error_code(exc)
            parsed = _empty_diagnostics(error_code)
    settings_after = _file_sha256(settings_path)
    return _write_health_artifact(
        output_directory=output_root,
        prior=prior,
        config_public=config_public,
        request=request,
        response=response,
        parsed=parsed,
        provider_calls=provider_calls,
        error_code=error_code,
        settings_before_sha256=settings_before,
        settings_after_sha256=settings_after,
    )


run_stage_a_protocol_health_v2 = run_linear_stage_a_protocol_health_v2


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--k11-artifact-directory", type=Path, default=DEFAULT_K11_ARTIFACT_DIRECTORY)
    parser.add_argument("--settings-path", type=Path, default=DEFAULT_SETTINGS_PATH)
    args = parser.parse_args(argv)
    result = run_linear_stage_a_protocol_health_v2(
        args.output_directory,
        k11_artifact_directory=args.k11_artifact_directory,
        settings_path=args.settings_path,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "success": result.success,
                "health_ok": result.health_ok,
                "provider_calls": result.provider_calls,
                "retry_count": result.retry_count,
                "output_directory": result.output_directory,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


__all__ = [
    "ARTIFACT_VERSION",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "HEALTH_MAX_OUTPUT_TOKENS",
    "MAX_PROVIDER_CALLS",
    "MAX_RETRIES",
    "RESPONSE_FORMAT_MODE",
    "THINKING_DISABLED",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "DiagnosticProviderConfig",
    "DiagnosticProviderResponse",
    "DeepSeekProtocolDiagnosticProvider",
    "DiagnosticRunResult",
    "run_linear_stage_a_protocol_health_v2",
    "run_stage_a_protocol_health_v2",
]


if __name__ == "__main__":
    raise SystemExit(main())
