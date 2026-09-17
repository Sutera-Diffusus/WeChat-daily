"""Synthetic contract tests for the K24 real-provider side-car.

These tests inject an in-memory model and never contact a provider.  The
production K24 run is separately recorded in the private health artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from wechat_bridge import compact_stage_a_protocol_health_v3_runner as runner


def _settings(path: Path) -> bytes:
    value = {
        "ai": {
            "base_url": "https://example.invalid",
            "model": "deepseek-v4-flash-vision-exp",
            "api_key": "synthetic-only",
        }
    }
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return raw


class _FakeModel:
    model_id = "deepseek-v4-flash"
    source = "synthetic"
    configured = True
    calls: list[dict[str, Any]] = []

    def __init__(self, _config: Any) -> None:
        self.calls = []

    def complete(
        self,
        system_prompt: str,
        request: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Mapping[str, Any],
    ) -> runner.ProviderResponse:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "request": dict(request),
                "max_output_tokens": max_output_tokens,
                "extra_body": dict(extra_body),
            }
        )
        primary = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
        context = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "c"]
        content = json.dumps(
            {
                "topics": [
                    {
                        "topic_id": "t1",
                        "primary_message_ids": primary,
                        "context_message_ids": context,
                        "uncertainty": "unknown",
                    }
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return runner.ProviderResponse(
            content=content,
            model=self.model_id,
            source=self.source,
            input_tokens=120,
            output_tokens=25,
            latency_ms=2.0,
            finish_reason="stop",
            reasoning_length=0,
        )


def test_runner_is_one_shot_and_persists_body_free_ledger(tmp_path: Path, monkeypatch: Any) -> None:
    settings_path = tmp_path / "settings.json"
    before = _settings(settings_path)
    fake_instances: list[_FakeModel] = []

    def factory(config: Any) -> _FakeModel:
        model = _FakeModel(config)
        fake_instances.append(model)
        return model

    monkeypatch.setattr(runner, "OpenAICompatibleCompactStageAHealthV3Model", factory)
    result = runner.run_compact_stage_a_protocol_health_v3_real(
        tmp_path / "artifact",
        settings_path=settings_path,
        authority_root=tmp_path / "authority",
    )
    assert result["success"] is True
    assert result["provider_calls"] == 1
    assert len(fake_instances) == 1
    assert fake_instances[0].calls[0]["max_output_tokens"] == 400
    assert fake_instances[0].calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert settings_path.read_bytes() == before
    assert len(result["aggregate"]["authorization_ledger"]["status_counts"]) == 1
    assert result["aggregate"]["authorization_ledger"]["status_counts"] == {"complete": 1}

    artifact = tmp_path / "artifact"
    manifest = json.loads((artifact / "manifest.private.json").read_text(encoding="utf-8"))
    ledger_rows = [
        json.loads(line)
        for line in (artifact / "ledger.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(ledger_rows) == 1
    assert set(ledger_rows[0]) == {
        "reservation_id",
        "authorization_id",
        "ordinal",
        "request_sha256",
        "unit_ref_sha256",
        "attempt",
        "provider",
        "model",
        "protocol",
        "status",
        "error_code",
        "input_tokens",
        "output_tokens",
        "latency_ms",
    }
    assert manifest["response_format_sent"] is False
    assert manifest["thinking_disabled"] is True


def test_health_adapter_extracts_nested_sdk_response_with_compact_contract() -> None:
    calls: list[dict[str, Any]] = []

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return type(
                "Response",
                (),
                {
                    "model": "deepseek-v4-flash",
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type(
                                    "Message",
                                    (),
                                    {
                                        "content": '{"topics":[]}',
                                        "reasoning_content": "hidden-synthetic-reasoning",
                                    },
                                )(),
                            },
                        )()
                    ],
                    "usage": type("Usage", (), {"prompt_tokens": 11, "completion_tokens": 13})(),
                },
            )()

    config = runner.DiagnosticProviderConfig(
        model="deepseek-v4-flash",
        base_url="https://example.invalid",
        api_key="synthetic-only",
        timeout_seconds=9.0,
        response_format_mode="omitted",
        thinking_disabled=True,
    )
    adapter = runner.OpenAICompatibleCompactStageAHealthV3Model(
        config,
    )
    adapter._client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
    )()
    result = adapter.complete(
        "synthetic-system",
        {"packet": "synthetic"},
        max_output_tokens=400,
        extra_body={"thinking": {"type": "disabled"}},
    )

    assert len(calls) == 1
    request = calls[0]
    assert request["model"] == "deepseek-v4-flash"
    assert request["messages"] == [
        {"role": "system", "content": "synthetic-system"},
        {"role": "user", "content": '{"packet":"synthetic"}'},
    ]
    assert request["max_tokens"] == 400
    assert request["temperature"] == 0
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "response_format" not in request
    assert result.content == '{"topics":[]}'
    assert result.input_tokens == 11
    assert result.output_tokens == 13
    assert result.finish_reason == "stop"
    assert result.reasoning_length == len("hidden-synthetic-reasoning")


def test_same_authorization_cannot_replay_in_a_new_output_directory(tmp_path: Path, monkeypatch: Any) -> None:
    settings_path = tmp_path / "settings.json"
    _settings(settings_path)
    instances: list[_FakeModel] = []

    def factory(config: Any) -> _FakeModel:
        model = _FakeModel(config)
        instances.append(model)
        return model

    monkeypatch.setattr(runner, "OpenAICompatibleCompactStageAHealthV3Model", factory)
    authority = tmp_path / "authority"
    first = runner.run_compact_stage_a_protocol_health_v3_real(
        tmp_path / "first",
        settings_path=settings_path,
        authority_root=authority,
    )
    second = runner.run_compact_stage_a_protocol_health_v3_real(
        tmp_path / "second",
        settings_path=settings_path,
        authority_root=authority,
    )
    assert first["provider_calls"] == 1
    assert second["provider_calls"] == 0
    assert second["error_code"] == "authorization_history_detected"
    assert len(instances) == 2
    assert instances[0].calls
    assert instances[1].calls == []
    assert second["aggregate"]["authorization_preflight"]["zero_history"] is False
