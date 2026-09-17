import json

from wechat_bridge.bundle_semantics import empty_bundle
from wechat_bridge.contextual_bundle_pipeline import AIProviderConfig, ProviderHealthResult
from wechat_bridge.contextual_bundle_pipeline_runner import (
    CAPABILITY_OVERRIDE_RUNNER_SCHEMA_VERSION,
    SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION,
    SEMANTIC_FRAME_FINAL_RUNNER_SCHEMA_VERSION,
    SEMANTIC_FRAME_AGGREGATE_RUNNER_SCHEMA_VERSION,
    SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION,
    run_development_shadow_pilot_v23,
    run_development_shadow_pilot_v24,
    run_development_shadow_pilot_v25,
    run_development_shadow_pilot_v26,
    run_development_shadow_pilot_v27,
)
from wechat_bridge.contextual_bundle_provider_capabilities import (
    BUNDLE_HEALTH_CAPABILITY_SCHEMA_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    SEMANTIC_FRAME_CAPABILITY_PROTOCOL,
    extract_model_ids,
    probe_provider_capabilities,
    probe_semantic_frame_bundle_capabilities,
    probe_semantic_frame_capabilities,
    select_model_candidates,
    write_bundle_health_artifact,
    write_capability_artifact,
)


class _FakeModels:
    def __init__(self, values):
        self.values = values

    def list(self):
        return {"data": self.values, "private_body": "must-not-be-retained"}


class _FakeClient:
    def __init__(self, values):
        self.models = _FakeModels(values)


class _FakeHealth:
    source = "synthetic-provider"

    def __init__(self, model, successful_models):
        self.model_version = model
        self.successful_models = successful_models
        self.calls = []

    def health_check(self, **kwargs):
        self.calls.append(kwargs)
        ok = self.model_version in self.successful_models
        return ProviderHealthResult(
            ok=ok,
            status="available" if ok else "blocked",
            source=self.source,
            model=self.model_version,
            request_sha256="synthetic-health-hash-%s" % self.model_version,
            input_tokens=31,
            output_tokens=7 if ok else 0,
            max_input_tokens=kwargs["max_input_tokens"],
            max_output_tokens=kwargs["max_output_tokens"],
            latency_ms=1.0,
            error_code=None if ok else "synthetic_health_failed",
            config={"api_key_configured": True},
        )

    def bundle_health_check(self, **kwargs):
        self.calls.append(kwargs)
        ok = self.model_version in self.successful_models
        return ProviderHealthResult(
            ok=ok,
            status="available" if ok else "blocked",
            source=self.source,
            model=self.model_version,
            request_sha256="synthetic-bundle-health-hash-%s" % self.model_version,
            input_tokens=41,
            output_tokens=23 if ok else 0,
            max_input_tokens=kwargs["max_input_tokens"],
            max_output_tokens=kwargs["max_output_tokens"],
            latency_ms=1.0,
            error_code=None if ok else "synthetic_bundle_health_failed",
            config={"api_key_configured": True},
            diagnostics={"probe_kind": "synthetic_bundle", "response_format_sent": False},
        )


def test_model_selection_excludes_vision_and_prioritizes_configured_text_model():
    rows = select_model_candidates(
        ["qwen-chat", "configured-text", "configured-text", "deepseek-vision-exp", "embedding-small"],
        configured_model="configured-text",
    )

    assert [row["model_id"] for row in rows] == ["configured-text", "qwen-chat"]
    assert all("vision" not in row["model_id"] for row in rows)


def test_capability_probe_is_bounded_body_free_and_selects_first_success():
    model_values = [
        {"id": "deepseek-v4-flash-vision-exp", "private": "ignored"},
        {"id": "qwen-chat"},
        {"id": "gpt-text"},
        {"id": "deepseek-chat"},
        {"id": "qwen-chat"},
    ]
    config = AIProviderConfig(
        model="deepseek-v4-flash-vision-exp",
        api_key="synthetic-secret",
        base_url="https://synthetic.invalid/v1",
    )
    health_adapters = []

    def health_factory(candidate_config):
        adapter = _FakeHealth(candidate_config.model, {"deepseek-chat"})
        health_adapters.append(adapter)
        return adapter

    result = probe_provider_capabilities(
        config,
        client_factory=lambda _config: _FakeClient(model_values),
        health_model_factory=health_factory,
        max_candidates=3,
        max_health_calls=3,
    )

    assert result.ok is True
    assert result.selected_model == "deepseek-chat"
    assert len(result.health_calls) == 3
    assert all(call["max_input_tokens"] == 500 for call in result.health_calls)
    assert all(call["max_output_tokens"] == 100 for call in result.health_calls)
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "synthetic-secret" not in payload
    assert "must-not-be-retained" not in payload
    assert result.to_dict()["development_read"] is False
    assert result.to_dict()["frozen_read"] is False


def test_semantic_frame_capability_probe_uses_frame_protocol_and_two_call_cap():
    config = AIProviderConfig(model="configured-text", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    adapters = []

    def health_factory(candidate_config):
        adapter = _FakeHealth(candidate_config.model, {"deepseek-chat"})
        adapters.append(adapter)
        return adapter

    result = probe_semantic_frame_capabilities(
        config,
        client_factory=lambda _config: _FakeClient([{"id": "qwen-chat"}, {"id": "deepseek-chat"}, {"id": "image-vision"}]),
        health_model_factory=health_factory,
        max_candidates=2,
        max_health_calls=2,
    )

    assert result.protocol == SEMANTIC_FRAME_CAPABILITY_PROTOCOL
    assert result.ok is True
    assert result.selected_model == "deepseek-chat"
    assert len(result.health_calls) == 2
    assert all(row["semantic_frame_confirmed"] is not None for row in result.candidates)


def test_semantic_frame_bundle_capability_probe_uses_bundle_health_and_two_call_cap():
    config = AIProviderConfig(model="configured-text", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    result = probe_semantic_frame_bundle_capabilities(
        config,
        client_factory=lambda _config: _FakeClient([{"id": "configured-text"}, {"id": "deepseek-chat"}, {"id": "image-vision"}]),
        health_model_factory=lambda candidate_config: _FakeHealth(candidate_config.model, {"configured-text"}),
        max_candidates=2,
        max_health_calls=2,
        thinking_disabled=True,
    )

    assert result.protocol == "semantic_frame_v1_bundle_health"
    assert result.ok is True
    assert result.selected_model == "configured-text"
    assert len(result.health_calls) == 2
    assert result.selected_health is not None
    assert result.selected_health.diagnostics["probe_kind"] == "synthetic_bundle"
    assert all(row["semantic_frame_confirmed"] is False for row in result.candidates)
    assert "synthetic-secret" not in json.dumps(result.to_dict(), ensure_ascii=False)


def test_capability_artifact_is_versioned_and_raw_response_free(tmp_path):
    config = AIProviderConfig(model="synthetic-text", api_key="synthetic-secret")
    result = probe_provider_capabilities(
        config,
        client_factory=lambda _config: _FakeClient([{"id": "synthetic-text"}]),
        health_model_factory=lambda candidate_config: _FakeHealth(candidate_config.model, {"synthetic-text"}),
    )
    paths = write_capability_artifact(result, tmp_path / "capability_v1")

    matrix = json.loads((tmp_path / "capability_v1" / "provider_capabilities.private.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "capability_v1" / "manifest.private.json").read_text(encoding="utf-8"))
    assert matrix["schema_version"] == CAPABILITY_SCHEMA_VERSION
    assert manifest["raw_response_saved"] is False
    assert manifest["development_read"] is False
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(matrix)
    assert set(paths) == {"matrix", "errors", "manifest"}


def test_direct_bundle_health_artifact_is_bounded_and_body_free(tmp_path):
    config = AIProviderConfig(model="synthetic-text", api_key="synthetic-secret")
    health = ProviderHealthResult(
        ok=False,
        status="blocked",
        source="synthetic-provider",
        model="synthetic-text",
        request_sha256="synthetic-health-hash",
        input_tokens=41,
        output_tokens=23,
        max_input_tokens=500,
        max_output_tokens=400,
        latency_ms=1.0,
        error_code="semantic_frame_field_count",
        config={"api_key_configured": True},
        diagnostics={"missing_field_names": ["metadata"], "raw_content_saved": False},
    )
    paths = write_bundle_health_artifact(
        config,
        (health, health),
        tmp_path / "bundle_health_v2_6",
        errors=({"stage": "health", "code": "provider_incompatible", "body": "must-not-be-retained"},),
    )

    matrix = json.loads((tmp_path / "bundle_health_v2_6" / "provider_capabilities.private.json").read_text(encoding="utf-8"))
    assert matrix["schema_version"] == BUNDLE_HEALTH_CAPABILITY_SCHEMA_VERSION
    assert len(matrix["health_calls"]) == 2
    assert matrix["development_read"] is False
    assert matrix["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(matrix)
    assert "must-not-be-retained" not in json.dumps(matrix)
    assert set(paths) == {"matrix", "errors", "manifest"}


class _FakeBundleModel:
    model_version = "override-text"

    def encode_bundle(self, request):
        return empty_bundle(
            request["bundle_id"],
            [item["message_id"] for item in request["messages"]],
            chat_id=request["chat_id"],
            status="complete",
            source="model",
        )


def test_v23_uses_successful_capability_health_as_explicit_override_without_new_probe(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    (development / "messages.private.jsonl").write_text(
        json.dumps(
            {
                "split": "development",
                "local_day": "2026-08-25",
                "message_id": "message-synthetic",
                "chat_id": "chat-synthetic",
                "speaker_id": "speaker-synthetic",
                "redacted_text": "synthetic",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = AIProviderConfig(
        model="override-text",
        api_key="synthetic-secret",
        base_url="https://synthetic.invalid/v1",
    )
    health = ProviderHealthResult(
        ok=True,
        status="available",
        source="synthetic-provider",
        model="override-text",
        request_sha256="synthetic-health-hash",
        input_tokens=31,
        output_tokens=7,
        max_input_tokens=500,
        max_output_tokens=100,
        latency_ms=1.0,
        config={"api_key_configured": True},
    )

    result = run_development_shadow_pilot_v23(
        development,
        tmp_path / "v2_3",
        provider_config=config,
        provider_health=health,
        model=_FakeBundleModel(),
    )

    assert result.ok is True
    manifest = json.loads((tmp_path / "v2_3" / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["runner_schema_version"] == CAPABILITY_OVERRIDE_RUNNER_SCHEMA_VERSION
    assert manifest["provider_model_override"]["model"] == "override-text"
    assert manifest["provider_model_override"]["global_settings_mutated"] is False
    assert manifest["provider_health_reused"] is True
    assert manifest["development_input_read"] is True
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(manifest)


def test_v24_uses_semantic_frame_override_and_reuses_successful_health(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    (development / "messages.private.jsonl").write_text(
        json.dumps(
            {
                "split": "development",
                "local_day": "2026-08-25",
                "message_id": "message-synthetic",
                "chat_id": "chat-synthetic",
                "speaker_id": "speaker-synthetic",
                "redacted_text": "synthetic",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = AIProviderConfig(model="override-text", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    health = ProviderHealthResult(
        ok=True,
        status="available",
        source="synthetic-provider",
        model="override-text",
        request_sha256="synthetic-health-hash",
        input_tokens=31,
        output_tokens=7,
        max_input_tokens=500,
        max_output_tokens=100,
        latency_ms=1.0,
        config={"api_key_configured": True},
    )

    result = run_development_shadow_pilot_v24(
        development,
        tmp_path / "v2_4",
        provider_config=config,
        provider_health=health,
        model=_FakeBundleModel(),
    )

    assert result.ok is True
    manifest = json.loads((tmp_path / "v2_4" / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["runner_schema_version"] == SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION
    assert manifest["provider_model_override"]["protocol"] == "semantic_frame_v1"
    assert manifest["provider_health_reused"] is True
    assert manifest["development_input_read"] is True
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(manifest)


def test_v25_uses_compact_bundle_health_override_without_global_settings_or_new_probe(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    (development / "messages.private.jsonl").write_text(
        json.dumps(
            {
                "split": "development",
                "local_day": "2026-08-25",
                "message_id": "message-synthetic",
                "chat_id": "chat-synthetic",
                "speaker_id": "speaker-synthetic",
                "redacted_text": "synthetic",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = AIProviderConfig(model="override-text", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    health = ProviderHealthResult(
        ok=True,
        status="available",
        source="synthetic-provider",
        model="override-text",
        request_sha256="synthetic-bundle-health-hash",
        input_tokens=41,
        output_tokens=23,
        max_input_tokens=500,
        max_output_tokens=100,
        latency_ms=1.0,
        error_code=None,
        config={"api_key_configured": True},
        diagnostics={"probe_kind": "synthetic_bundle", "response_format_sent": False},
    )

    result = run_development_shadow_pilot_v25(
        development,
        tmp_path / "v2_5",
        provider_config=config,
        provider_health=health,
        capability_artifact=tmp_path / "capability_v2_5",
        thinking_disabled=True,
        model=_FakeBundleModel(),
    )

    assert result.ok is True
    manifest = json.loads((tmp_path / "v2_5" / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["runner_schema_version"] == SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION
    assert manifest["provider_model_override"]["protocol"] == "semantic_frame_v1"
    assert manifest["provider_model_override"]["thinking_disabled"] is True
    assert manifest["provider_health_probe"] == "synthetic_bundle"
    assert manifest["provider_health_reused"] is True
    assert manifest["response_format_sent"] is False
    assert manifest["compact_input_chars_limit"] == 1800
    assert manifest["development_input_read"] is True
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(manifest)


def test_v26_normalizes_exhausted_health_failures_to_provider_incompatible(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    (development / "messages.private.jsonl").write_text("not-read\n", encoding="utf-8")
    config = AIProviderConfig(model="override-text", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    health = ProviderHealthResult(
        ok=False,
        status="blocked",
        source="synthetic-provider",
        model="override-text",
        request_sha256="synthetic-bundle-health-hash",
        input_tokens=125,
        output_tokens=92,
        max_input_tokens=500,
        max_output_tokens=400,
        latency_ms=1.0,
        error_code="semantic_frame_field_count",
        config={"api_key_configured": True},
        diagnostics={
            "probe_kind": "synthetic_bundle",
            "finish_reasons": ["stop"],
            "missing_field_names": ["metadata"],
            "raw_content_saved": False,
        },
    )

    result = run_development_shadow_pilot_v26(
        development,
        tmp_path / "v2_6",
        provider_config=config,
        provider_health=health,
        thinking_disabled=True,
    )

    assert result.ok is False
    assert result.health["error_code"] == "provider_incompatible"
    assert result.diagnostic["provider_incompatible"] is True
    assert result.diagnostic["provider_attempts_exhausted"] is True
    manifest = json.loads((tmp_path / "v2_6" / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["runner_schema_version"] == SEMANTIC_FRAME_FINAL_RUNNER_SCHEMA_VERSION
    assert manifest["provider_incompatible"] is True
    assert manifest["development_input_read"] is False
    assert manifest["frozen_read"] is False
    assert manifest["provider_health"]["diagnostics"]["underlying_error_code"] == "semantic_frame_field_count"
    assert "synthetic-secret" not in json.dumps(manifest)


def test_v27_reuses_successful_health_and_records_safe_error_taxonomy_version(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    (development / "messages.private.jsonl").write_text(
        json.dumps(
            {
                "split": "development",
                "local_day": "2026-08-25",
                "message_id": "message-synthetic",
                "chat_id": "chat-synthetic",
                "speaker_id": "speaker-synthetic",
                "redacted_text": "synthetic",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = AIProviderConfig(model="override-text", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    health = ProviderHealthResult(
        ok=True,
        status="available",
        source="synthetic-provider",
        model="override-text",
        request_sha256="synthetic-bundle-health-hash",
        input_tokens=41,
        output_tokens=23,
        max_input_tokens=500,
        max_output_tokens=400,
        latency_ms=1.0,
        error_code=None,
        config={"api_key_configured": True},
        diagnostics={"probe_kind": "synthetic_bundle", "response_format_sent": False},
    )

    result = run_development_shadow_pilot_v27(
        development,
        tmp_path / "v2_7",
        provider_config=config,
        provider_health=health,
        capability_artifact=tmp_path / "capability_v2_6",
        model=_FakeBundleModel(),
    )

    assert result.ok is True
    manifest = json.loads((tmp_path / "v2_7" / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["runner_schema_version"] == SEMANTIC_FRAME_AGGREGATE_RUNNER_SCHEMA_VERSION
    assert manifest["provider_health_reused"] is True
    assert manifest["provider_health_probe"] == "synthetic_bundle_compact_final_reused"
    assert manifest["error_taxonomy_version"] == "semantic_frame_safe_taxonomy_v1"
    assert manifest["provider_model_override"]["source"] == "semantic_frame_schema_focus_v2_7"
    assert manifest["development_input_read"] is True
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(manifest)


def test_extract_model_ids_discards_non_ids_and_deduplicates():
    assert extract_model_ids({"data": [{"id": "text-a"}, {"id": "text-a"}, {"id": ""}, {"private": "body"}]}) == ("text-a",)
