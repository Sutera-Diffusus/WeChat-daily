"""Minimal synthetic regressions for the one-shot K13 health probe."""

from __future__ import annotations

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
)
from wechat_bridge.linear_stage_a_protocol_health_v2 import (
    run_linear_stage_a_protocol_health_v2,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _make_prior(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    k11 = tmp_path / "linear_stage_a_pilot_v1"
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
            "provider": {
                "model": "deepseek-v4-flash-vision-exp",
                "response_format_mode": "json_object",
            },
        },
    )
    _write_json(
        k11 / "aggregate.private.json",
        {"health": {"error_code": "provider_invalid_json"}, "development_input_read": False},
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
    _write_json(
        capability / "provider_health.private.json",
        {
            "model": "deepseek-v4-flash",
            "source": "openai-semantic-frame",
            "ok": True,
            "status": "available",
            "raw_content_saved": False,
            "raw_reasoning_saved": False,
            "diagnostics": {
                "response_format_sent": False,
                "raw_content_saved": False,
                "raw_reasoning_saved": False,
            },
        },
    )
    return k11, (capability,)


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


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def test_adapter_sends_only_per_call_thinking_disable_and_does_not_mutate_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"ai": {"base_url": "https://example.invalid", "api_key": "secret"}}) + "\n",
        encoding="utf-8",
    )
    before = settings.read_bytes()
    config = DiagnosticProviderConfig.from_workbench_settings(
        settings,
        model_override="deepseek-v4-flash",
        response_format_mode="omitted",
        thinking_disabled=True,
    )
    calls: list[dict[str, Any]] = []

    class Completions:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(
                id="synthetic-id",
                model="deepseek-v4-flash",
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=9, completion_tokens=4),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    response = DeepSeekProtocolDiagnosticProvider(config, client=client).complete(
        "A", "system", _health_request(), max_output_tokens=400
    )

    assert settings.read_bytes() == before
    assert calls[0]["max_tokens"] == 400
    assert "response_format" not in calls[0]
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert config.public_dict()["thinking_disabled"] is True
    assert "api_key" not in config.public_dict()
    assert response.content == "{}"


class _SyntheticProvider:
    source = "synthetic-provider"
    model_id = "deepseek-v4-flash"
    configured = True

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> DiagnosticProviderResponse:
        assert stage == "A"
        assert system_prompt
        assert user_packet["page_id"] == "SYNTHETIC_HEALTH_PAGE"
        assert max_output_tokens == 400
        return DiagnosticProviderResponse(
            content="prefix\n```json\n" + json.dumps(_valid_output()) + "\n```\nsuffix",
            reasoning_content="private reasoning must not be persisted",
            finish_reasons=("stop",),
            input_tokens=31,
            output_tokens=18,
            usage_present=True,
            usage_keys=("prompt_tokens", "completion_tokens"),
            latency_ms=1.25,
            model=self.model_id,
            source=self.source,
            response_fields=("choices", "content", "reasoning", "api_key"),
            request_id_present=True,
        )


def test_runner_keeps_fenced_candidate_diagnostic_only_and_artifact_body_free(tmp_path: Path) -> None:
    k11, capabilities = _make_prior(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text('{"ai":{"api_key":"secret"}}\n', encoding="utf-8")
    before = settings.read_bytes()
    output = tmp_path / "linear_stage_a_protocol_health_v2"

    result = run_linear_stage_a_protocol_health_v2(
        output,
        k11_artifact_directory=k11,
        capability_directories=capabilities,
        settings_path=settings,
        provider=_SyntheticProvider(),
        config=DiagnosticProviderConfig(
            model="deepseek-v4-flash",
            base_url="https://example.invalid",
            api_key="secret",
            response_format_mode="omitted",
        ),
    )

    assert settings.read_bytes() == before
    assert result.provider_calls == 1
    assert result.retry_count == 0
    assert result.success is False
    aggregate = json.loads((output / "aggregate.private.json").read_text(encoding="utf-8"))
    assert aggregate["status"] == "blocked"
    assert aggregate["development_input_read"] is False
    assert aggregate["stage_a_development"] is False
    assert aggregate["stage_b_pilot"] is False
    assert aggregate["thinking_disabled"] is True
    assert aggregate["extra_body"]["global_settings_mutated"] is False
    assert aggregate["response_format"]["mode"] == "omitted"
    assert aggregate["strict_result"]["parse_code"] == "provider_invalid_json"
    assert aggregate["diagnostic_candidate"]["accepted_for_complete"] is False
    assert aggregate["response_diagnostics"]["fence_detected"] is True
    assert aggregate["response_diagnostics"]["raw_response_saved"] is False
    assert aggregate["response_diagnostics"]["raw_reasoning_saved"] is False

    for path in output.iterdir():
        if path.suffix == ".json":
            loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        else:
            loaded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert not any(str(key).casefold() in {item.casefold() for item in BODY_KEYS} for key in _walk_keys(loaded))
        text = path.read_text(encoding="utf-8")
        assert "private reasoning" not in text
        assert "secret" not in text
