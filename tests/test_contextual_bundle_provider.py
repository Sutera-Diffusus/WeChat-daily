import json

from wechat_bridge.contextual_bundle_pipeline import (
    AIProviderConfig,
    BundleSemanticError,
    BundleSchemaError,
    OpenAIBundleModel,
    ProviderHealthResult,
    _provider_error_code,
    run_provider_health_check,
)
from wechat_bridge.contextual_bundle_pipeline_runner import run_development_shadow_pilot_v21


class _FakeHealthAdapter:
    source = "synthetic-provider"
    model_version = "synthetic-health-v1"

    def __init__(self):
        self.calls = []

    def health_check(self, **kwargs):
        self.calls.append(kwargs)
        return ProviderHealthResult(
            ok=True,
            status="available",
            source=self.source,
            model=self.model_version,
            request_sha256="synthetic-health-hash",
            input_tokens=17,
            output_tokens=4,
            max_input_tokens=kwargs["max_input_tokens"],
            max_output_tokens=kwargs["max_output_tokens"],
            latency_ms=1.5,
            config={"provider": self.source, "api_key_configured": True},
        )


class _BlockedHealthAdapter:
    source = "synthetic-provider"
    model_version = "synthetic-health-v1"

    def health_check(self, **kwargs):
        return ProviderHealthResult(
            ok=False,
            status="blocked",
            source=self.source,
            model=self.model_version,
            request_sha256="synthetic-health-hash",
            input_tokens=17,
            output_tokens=0,
            max_input_tokens=kwargs["max_input_tokens"],
            max_output_tokens=kwargs["max_output_tokens"],
            latency_ms=1.5,
            error_code="synthetic_health_failed",
            config={"provider": self.source, "api_key_configured": True},
        )


def test_health_probe_is_bounded_and_body_free_for_injected_fake():
    fake = _FakeHealthAdapter()
    result = run_provider_health_check(
        AIProviderConfig(provider="synthetic", model="synthetic-health-v1", api_key="synthetic-secret"),
        model=fake,
        max_input_tokens=500,
        max_output_tokens=100,
    )

    assert result.ok is True
    assert fake.calls[0]["max_input_tokens"] == 500
    assert fake.calls[0]["max_output_tokens"] == 100
    assert result.input_tokens <= 500
    assert result.output_tokens <= 100
    payload = result.to_dict()
    assert "synthetic-secret" not in json.dumps(payload)


def test_workbench_settings_factory_reuses_local_ai_shape_without_public_secret():
    class _Settings:
        def snapshot(self, include_secrets=False):
            assert include_secrets is True
            return {"ai": {"model": "synthetic-settings-model", "api_key": "synthetic-secret", "base_url": "https://synthetic.invalid/v1"}}

    config = AIProviderConfig.from_workbench_settings(_Settings())
    assert config.configured is True
    assert config.model == "synthetic-settings-model"
    assert config.to_dict()["api_key_configured"] is True
    assert "api_key" not in config.to_dict()
    assert "synthetic-secret" not in json.dumps(config.to_dict())


def test_workbench_settings_path_uses_existing_settings_loader_without_exposing_secret(tmp_path):
    path = tmp_path / "workbench_settings.json"
    path.write_text(
        json.dumps(
            {
                "ai": {
                    "model": "synthetic-settings-model",
                    "api_key": "synthetic-secret",
                    "base_url": "https://synthetic.invalid/v1",
                }
            }
        ),
        encoding="utf-8",
    )

    config = AIProviderConfig.from_workbench_settings_path(path)

    assert config.configured is True
    assert config.model == "synthetic-settings-model"
    assert config.base_url == "https://synthetic.invalid/v1"
    assert "synthetic-secret" not in json.dumps(config.public_dict())


def test_health_parser_accepts_one_fenced_json_object_with_prose():
    parsed = OpenAIBundleModel._parse_health_response(
        "The synthetic probe result is below:\n```json\n{\"ok\": true}\n```\n"
    )

    assert parsed == {"ok": True}


def test_health_parser_rejects_schema_coercion_and_wrapped_or_multiple_objects():
    invalid_responses = (
        '{"ok": "true"}',
        '{"ok": true, "extra": false}',
        '[{"ok": true}]',
        '{"outer": {"ok": true}}',
        '{"ok": true} and {"ok": false}',
    )

    for response in invalid_responses:
        try:
            OpenAIBundleModel._parse_health_response(response)
        except BundleSemanticError as exc:
            assert str(exc) in {
                "provider_health_response_not_json",
                "provider_health_schema_invalid",
                "provider_health_multiple_json_objects",
            }
        else:
            raise AssertionError("invalid synthetic health response was accepted")


def test_compatible_bundle_adapter_uses_json_mode_and_local_fixed_contract():
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return {"output_text": "{}"}

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    model = OpenAIBundleModel(
        AIProviderConfig(
            provider="openai",
            model="synthetic-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        )
    )
    model._client = _Client()
    model.encode_bundle(
        {
            "bundle_id": "bundle-synthetic",
            "chat_id": "chat-synthetic",
            "messages": [{"message_id": "message-synthetic"}],
        }
    )

    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 400
    assert "evidence_ids" in calls[0]["messages"][0]["content"]
    assert "synthetic-secret" not in json.dumps(calls[0])


def test_provider_error_taxonomy_is_stable_and_body_free():
    class BadRequestError(Exception):
        pass

    assert _provider_error_code(BadRequestError("provider body is private")) == "provider_protocol_error"
    assert _provider_error_code(BundleSchemaError("model response failed bundle validation")) == "schema_validation_failed"


def test_unconfigured_openai_health_probe_is_blocked_without_network():
    result = run_provider_health_check(
        AIProviderConfig(provider="openai", model="synthetic-model", api_key=None),
        max_input_tokens=500,
        max_output_tokens=100,
    )
    assert result.ok is False
    assert result.status == "blocked"
    assert result.error_code == "provider_not_configured"
    assert result.input_tokens <= 500
    assert result.output_tokens == 0


def test_v21_health_failure_writes_diagnostic_without_reading_messages(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    output = tmp_path / "v2_1_blocked"
    result = run_development_shadow_pilot_v21(
        development,
        output,
        provider_config=AIProviderConfig(provider="synthetic", model="synthetic", api_key="synthetic-secret"),
        health_model=_BlockedHealthAdapter(),
    )

    assert result.ok is False
    assert result.pilot is None
    assert result.health["error_code"] == "synthetic_health_failed"
    assert (output / "provider_health.private.json").is_file()
    assert not (output / "bundles.private.jsonl").exists()
    manifest = json.loads((output / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["development_input_read"] is False
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(manifest)
