import json
from pathlib import Path

import pytest

from wechat_bridge.bundle_semantics import empty_bundle
from wechat_bridge.contextual_bundle_pipeline import (
    AIProviderConfig,
    ContextualBundlePipeline,
    PipelineConfigurationError,
)
from wechat_bridge.contextual_bundle_pipeline_runner import (
    OUTPUT_FILENAMES,
    run_development_shadow_pilot,
)


def _messages(count=3):
    return [
        {
            "message_id": "synthetic-message-%d" % index,
            "account_id": "synthetic-account",
            "chat_id": "synthetic-chat",
            "speaker_id": "synthetic-speaker-%d" % (index % 2),
            "message_type": "text",
            "sequence_in_chat": index,
            "timestamp": "2026-08-25T00:00:%02dZ" % index,
            "split": "development",
            "redacted_text": "synthetic body %d" % index,
        }
        for index in range(1, count + 1)
    ]


class _FakeModel:
    model_version = "synthetic-fake-v1"

    def __init__(self, failures=0):
        self.failures = failures
        self.requests = []

    def encode_bundle(self, request):
        self.requests.append(request)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic failure")
        return empty_bundle(
            request["bundle_id"],
            [item["message_id"] for item in request["messages"]],
            chat_id=request["chat_id"],
            status="complete",
            source="synthetic_fake",
        )


def _body_keys(value):
    keys = []
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).casefold()
            if lower in {"text", "content", "body", "raw", "redacted_text", "evidence_text"} or lower.endswith("_text"):
                keys.append(str(key))
            keys.extend(_body_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_body_keys(item))
    return keys


def test_disabled_mode_integrates_all_four_stages_without_model_calls():
    result = ContextualBundlePipeline(mode="disabled").run(_messages(), split="development")

    assert result.manifest["schema_version"] == "contextual_bundle_pipeline_v1"
    assert result.manifest["split"] == "development"
    assert result.manifest["frozen_read"] is False
    assert result.manifest["gold_loaded"] is False
    assert len(result.registry_snapshot["entry_hashes"]) == 3
    assert result.gate_snapshot["registry_version"] == 3
    assert result.dialogue_snapshot["bundle_count"] == len(result.bundles)
    assert result.cost["budget"]["calls_used"] == 0
    assert result.cost["fallback_count"] == len(result.decisions)
    assert all(item["status"] == "fallback" for item in result.decisions)
    assert not _body_keys(result.artifacts())


def test_fake_mode_is_budgeted_and_later_candidates_become_pending():
    model = _FakeModel()
    result = ContextualBundlePipeline(
        mode="fake",
        model=model,
        max_bundle_calls=1,
        max_retries=0,
    ).run(_messages(4), split="development")

    assert len(model.requests) == 1
    assert result.cost["budget"]["calls_used"] == 1
    assert result.cost["budget"]["calls_used"] <= 14
    assert any(item["status"] == "complete" for item in result.decisions)
    assert any(item["status"] == "pending" for item in result.decisions)
    assert any(item.get("error_code") == "bundle_call_budget_exhausted" for item in result.requests)


def test_fake_provider_failure_retries_with_explicit_retry_ledger():
    model = _FakeModel(failures=1)
    result = ContextualBundlePipeline(
        mode="fake",
        model=model,
        max_bundle_calls=2,
        max_retries=1,
    ).run(_messages(1), split="development")

    assert len(model.requests) == 2
    assert result.cost["budget"]["calls_used"] == 2
    assert result.cost["budget"]["retry_count"] == 1
    assert any(item.get("retry") is True for item in result.requests)
    assert any(item["status"] == "complete" for item in result.decisions)


def test_real_mode_without_credentials_is_blocked_and_uses_conservative_fallback():
    config = AIProviderConfig(model="synthetic-provider", api_key=None)
    result = ContextualBundlePipeline(mode="real", provider_config=config).run(_messages(1), split="development")

    assert result.manifest["provider_configured"] is False
    assert result.cost["blocked"] is True
    assert any(item["code"] == "provider_not_configured" for item in result.errors)
    assert all(item["status"] == "fallback" for item in result.decisions)
    assert result.cost["budget"]["calls_used"] == 0


def test_frozen_split_is_rejected_before_semantic_work():
    with pytest.raises(PipelineConfigurationError):
        ContextualBundlePipeline(mode="disabled").run(_messages(1), split="frozen")


def test_development_runner_writes_replayable_body_free_artifacts(tmp_path: Path):
    input_root = tmp_path / "development"
    input_root.mkdir()
    input_path = input_root / "messages.private.jsonl"
    input_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in _messages(2)),
        encoding="utf-8",
    )
    output_root = tmp_path / "contextual_bundle_pipeline_vsynthetic"

    pilot = run_development_shadow_pilot(input_root, output_root, mode="disabled")

    assert pilot.message_count == 2
    assert pilot.output_directory == str(output_root)
    assert set(OUTPUT_FILENAMES) == set(pilot.artifact_paths)
    assert all(Path(path).is_file() for path in pilot.artifact_paths.values())
    manifest = json.loads((output_root / OUTPUT_FILENAMES["manifest"]).read_text(encoding="utf-8"))
    aggregate = json.loads((output_root / OUTPUT_FILENAMES["aggregate"]).read_text(encoding="utf-8"))
    assert manifest["split"] == "development"
    assert manifest["gold_loaded"] is False
    assert manifest["frozen_read"] is False
    assert manifest["input_sha256"] != manifest["code_sha256"]
    assert aggregate["scoring"] == "N/A"
    assert not _body_keys(manifest)
    assert not _body_keys(aggregate)

    with pytest.raises(FileExistsError):
        run_development_shadow_pilot(input_root, output_root, mode="disabled")


def test_development_runner_rejects_frozen_named_input(tmp_path: Path):
    frozen = tmp_path / "frozen" / "development"
    frozen.mkdir(parents=True)
    (frozen / "messages.private.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen"):
        run_development_shadow_pilot(frozen, tmp_path / "out")
