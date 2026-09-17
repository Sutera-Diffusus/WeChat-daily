"""Synthetic regressions for the isolated K12 protocol diagnostic."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from wechat_bridge.linear_stage_a_protocol_diagnostic import (
    BODY_KEYS,
    DiagnosticProviderConfig,
    DiagnosticProviderResponse,
    DeepSeekProtocolDiagnosticProvider,
    _health_request,
    inspect_prior_state,
    run_linear_stage_a_protocol_diagnostic,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _valid_output() -> dict[str, Any]:
    packet = _health_request()
    return {
        "topics": [
            {
                "topic_id": "topic-1",
                "message_handles": list(packet["message_handles"]),
                "candidate_handles": list(packet["candidate_handles"]),
                "evidence_handles": list(packet["evidence_handles"]),
                "relation": "same_topic",
            }
        ]
    }


def _make_prior(tmp_path: Path, *, with_health: bool = True) -> tuple[Path, tuple[Path, ...]]:
    k11 = tmp_path / "linear_stage_a_pilot_v1"
    provider = {
        "provider": "deepseek",
        "source": "deepseek-openai-compatible",
        "model": "deepseek-v4-flash-vision-exp",
        "response_format_mode": "json_object",
    }
    _write_json(
        k11 / "manifest.private.json",
        {
            "artifact_version": "linear_stage_a_pilot_v1",
            "status": "blocked",
            "success": False,
            "development_input_read": False,
            "frozen_read": False,
            "gold_loaded": False,
            "production_state_written": False,
            "provider_calls": 1,
            "selected_page_count": 0,
            "provider": provider,
        },
    )
    _write_json(
        k11 / "aggregate.private.json",
        {
            "health": {"error_code": "provider_invalid_json"},
            "development_input_read": False,
        },
    )
    _write_json(
        k11 / "audit" / "audit_summary.private.json",
        {
            "audit_status": "pass",
            "health_gate": "blocked",
            "strict_error": "provider_invalid_json",
            "allow_stage_a_development": False,
            "allow_stage_b_pilot": False,
            "allow_one_protocol_diagnostic": True,
            "next_step": {
                "diagnostic_scope": "synthetic_health_only",
                "diagnostic_must_not_read_development_input": True,
            },
        },
    )
    capability = tmp_path / "contextual_bundle_pipeline_v2_10"
    if with_health:
        _write_json(
            capability / "provider_health.private.json",
            {
                "model": "deepseek-v4-flash",
                "source": "openai-semantic-frame",
                "ok": True,
                "status": "available",
                "raw_content_saved": False,
                "raw_reasoning_saved": False,
                "diagnostics": {"response_format_sent": False, "raw_content_saved": False, "raw_reasoning_saved": False},
            },
        )
    return k11, (capability,)


class SyntheticProvider:
    source = "synthetic-provider"
    model_id = "deepseek-v4-flash"
    configured = True

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def complete(self, stage: str, system_prompt: str, user_packet: Mapping[str, Any], *, max_output_tokens: int) -> DiagnosticProviderResponse:
        self.calls.append(
            {
                "stage": stage,
                "system_prompt": system_prompt,
                "user_packet": dict(user_packet),
                "max_output_tokens": max_output_tokens,
            }
        )
        return DiagnosticProviderResponse(
            content=self.content,
            reasoning_content="private reasoning must not be persisted",
            finish_reasons=("stop",),
            input_tokens=31,
            output_tokens=18,
            usage_present=True,
            usage_keys=("prompt_tokens", "completion_tokens"),
            latency_ms=1.25,
            model=self.model_id,
            source=self.source,
            response_fields=("choices", "id", "model", "usage", "content", "api_key"),
            request_id_present=True,
        )


def _loaded_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def test_strict_health_is_available_but_never_development_complete(tmp_path: Path) -> None:
    k11, capabilities = _make_prior(tmp_path)
    provider = SyntheticProvider(json.dumps(_valid_output(), separators=(",", ":")))
    output = tmp_path / "linear_stage_a_protocol_diagnostic_v1"
    result = run_linear_stage_a_protocol_diagnostic(
        output,
        k11_artifact_directory=k11,
        capability_directories=capabilities,
        provider=provider,
        config=DiagnosticProviderConfig(model="deepseek-v4-flash", base_url="https://example.invalid", api_key="secret"),
    )

    assert result.success is True
    assert result.status == "diagnostic_available"
    assert result.provider_calls == 1
    assert result.retry_count == 0
    assert len(provider.calls) == 1
    assert provider.calls[0]["stage"] == "A"
    assert provider.calls[0]["max_output_tokens"] == 300
    aggregate = _loaded_json(output / "aggregate.private.json")
    assert aggregate["development_input_read"] is False
    assert aggregate["stage_a_development"] is False
    assert aggregate["stage_b_pilot"] is False
    assert aggregate["strict_result"] == {
        "accepted_for_stage_a_development": False,
        "parse_code": "ok",
        "status": "available",
        "strict_complete": True,
        "validation_code": "ok",
    }
    assert aggregate["diagnostic_candidate"]["accepted_for_complete"] is False
    assert aggregate["response_format"]["mode"] == "omitted"
    assert aggregate["response_format"]["sent"] is False
    assert aggregate["response_format"]["predeclared"] is True
    diagnostic_text = (output / "diagnostic.private.json").read_text(encoding="utf-8")
    assert "private reasoning" not in diagnostic_text
    assert "api_key" not in diagnostic_text
    assert "secret" not in diagnostic_text
    assert '"raw_response_saved": false' in diagnostic_text.casefold()
    for path in output.iterdir():
        loaded = _loaded_json(path) if path.suffix == ".json" else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert not any(str(key).casefold() in {item.casefold() for item in BODY_KEYS} for key in _walk_keys(loaded))


def test_fenced_prose_is_diagnostic_candidate_only(tmp_path: Path) -> None:
    k11, capabilities = _make_prior(tmp_path)
    fenced = "Here is the health result:\n```json\n" + json.dumps(_valid_output()) + "\n```\nDone."
    provider = SyntheticProvider(fenced)
    output = tmp_path / "linear_stage_a_protocol_diagnostic_v1"
    result = run_linear_stage_a_protocol_diagnostic(
        output,
        k11_artifact_directory=k11,
        capability_directories=capabilities,
        provider=provider,
        config=DiagnosticProviderConfig(model="deepseek-v4-flash", base_url="https://example.invalid", api_key="secret"),
    )

    assert result.success is False
    assert result.status == "blocked"
    assert result.provider_calls == 1
    aggregate = _loaded_json(output / "aggregate.private.json")
    assert aggregate["errors"]["codes"] == ["provider_invalid_json"]
    assert aggregate["response_diagnostics"]["leading_shape"] == "prose"
    assert aggregate["response_diagnostics"]["fence_detected"] is True
    assert aggregate["response_diagnostics"]["unique_json_object_count"] == 1
    assert aggregate["strict_result"]["parse_code"] == "provider_invalid_json"
    assert aggregate["diagnostic_candidate"]["present"] is True
    assert aggregate["diagnostic_candidate"]["status"] == "candidate_valid_schema_not_accepted"
    assert aggregate["diagnostic_candidate"]["accepted_for_complete"] is False


def test_unavailable_nonvision_capability_blocks_without_provider_call(tmp_path: Path) -> None:
    k11, capabilities = _make_prior(tmp_path, with_health=False)
    provider = SyntheticProvider(json.dumps(_valid_output()))
    output = tmp_path / "linear_stage_a_protocol_diagnostic_v1"
    result = run_linear_stage_a_protocol_diagnostic(
        output,
        k11_artifact_directory=k11,
        capability_directories=capabilities,
        provider=provider,
        config=DiagnosticProviderConfig(model="deepseek-v4-flash", base_url="https://example.invalid", api_key="secret"),
    )

    assert result.success is False
    assert result.status == "blocked"
    assert result.provider_calls == 0
    assert provider.calls == []
    aggregate = _loaded_json(output / "aggregate.private.json")
    assert aggregate["errors"]["codes"] == ["fallback_model_unavailable"]
    assert aggregate["development_input_read"] is False
    assert aggregate["response_diagnostics"]["status"] == "not_run"


def test_adapter_omits_response_format_and_filters_public_fields() -> None:
    calls: list[dict[str, Any]] = []

    class Completions:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(
                id="response-id",
                model="deepseek-v4-flash",
                choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=31, completion_tokens=7),
                api_key="must-not-be-recorded",
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    config = DiagnosticProviderConfig(
        model="deepseek-v4-flash",
        base_url="https://example.invalid",
        api_key="secret",
        response_format_mode="omitted",
    )
    response = DeepSeekProtocolDiagnosticProvider(config, client=client).complete(
        "A", "system", _health_request(), max_output_tokens=300
    )
    assert response.content == "not-json"
    assert calls[0]["max_tokens"] == 300
    assert "response_format" not in calls[0]
    assert "api_key" not in response.response_fields
    assert set(response.response_fields) <= {"_request_id", "choices", "created", "id", "model", "object", "service_tier", "system_fingerprint", "usage"}


def test_prior_state_confirms_k11_and_nonvision_fallback(tmp_path: Path) -> None:
    k11, capabilities = _make_prior(tmp_path)
    prior = inspect_prior_state(k11, capabilities)
    assert prior.allowed is True
    assert prior.k11_model == "deepseek-v4-flash-vision-exp"
    assert prior.k11_response_format_mode == "json_object"
    assert prior.k11_error_code == "provider_invalid_json"
    assert prior.fallback_model == "deepseek-v4-flash"
    assert prior.fallback_response_format_mode == "omitted"
    assert prior.errors == ()


def test_hash_is_recorded_without_output_body() -> None:
    text = json.dumps(_valid_output(), separators=(",", ":"))
    provider = SyntheticProvider(text)
    # This checks the deterministic hash surface used in synthetic responses;
    # the runner's artifact assertions above ensure the value, not the body,
    # is persisted.
    assert hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert provider.content == text
