"""Public synthetic tests for the scheduler-integrated v2.9 runner.

These tests never open a repository artifact. They exercise the immutable
selection/attempt ledger with invented identifiers and a dependency-injected
model.
"""

from __future__ import annotations

import json
from pathlib import Path

from wechat_bridge.bundle_semantics import BUNDLE_SCHEMA_VERSION, empty_bundle
from wechat_bridge.contextual_bundle_pipeline import AIProviderConfig, ProviderHealthResult
from wechat_bridge.contextual_bundle_pipeline_v2_9_runner import (
    ARTIFACT_VERSION,
    OUTPUT_FILENAMES,
    SchedulerInputs,
    _error_bucket,
    _health_from_capability_artifact,
    run_development_shadow_pilot_v29,
)


def _cue(label: str) -> dict[str, object]:
    return {
        "cue_type": "synthetic",
        "cue_key": label,
        "executable": True,
        "replay_key": "replay-" + label,
    }


def _scheduler_row(candidate_id: str, *, selected: bool) -> dict[str, object]:
    return {
        "decision_version": "contextual_bundle_scheduler_schema_v2_9",
        "candidate_id": candidate_id,
        "semantic_package_id": "package-" + candidate_id,
        "selection_status": "selected" if selected else "not_selected",
        "selection_reason": "selected_budget_rank" if selected else "scheduler_budget_exhausted",
        "selected_representative_id": candidate_id,
        "scheduled_for_encode": selected,
        "scale": "local",
        "window_membership": ["local"],
        "channel": "pending_context",
        "chat_id": "chat-a",
        "source_message_count": 1,
        "source_message_ids": ["message-" + candidate_id],
        "activation_cues": [_cue(candidate_id)],
        "activation_cue_replayable": True,
    }


def _checked_fixture(tmp_path: Path) -> SchedulerInputs:
    source = tmp_path / "contextual_bundle_pipeline_v2_8"
    scheduler = tmp_path / "contextual_bundle_scheduler_v2_9"
    source.mkdir()
    scheduler.mkdir()
    for filename, value in (
        ("registry.private.json", {}),
        ("gate.private.json", {}),
        ("snapshots.private.json", {}),
        ("relations.private.jsonl", ""),
        ("open_context_snapshots.private.jsonl", ""),
    ):
        (source / filename).write_text(
            json.dumps(value) if not isinstance(value, str) else value,
            encoding="utf-8",
        )
    messages = (
        {
            "split": "development",
            "local_day": "2026-08-25",
            "message_id": "message-candidate-a",
            "chat_id": "chat-a",
            "speaker_id": "person-a",
            "content": "synthetic content",
        },
    )
    bundle = {"bundle_id": "candidate-a"}
    (source / "bundles.private.jsonl").write_text(json.dumps(bundle) + "\n", encoding="utf-8")
    (source / "decisions.private.jsonl").write_text(json.dumps(bundle) + "\n", encoding="utf-8")
    selected = _scheduler_row("candidate-a", selected=True)
    return SchedulerInputs(
        input_root=tmp_path / "development",
        source_root=source,
        scheduler_root=scheduler,
        messages=messages,
        raw_input=b"synthetic",
        source_manifest={},
        source_bundles=(bundle,),
        source_decisions=(bundle,),
        scheduler_manifest={},
        scheduler_coverage={},
        scheduler_provenance={},
        scheduler_decisions=(selected,),
        input_sha256="input-hash",
        scheduler_code_sha256="scheduler-hash",
        source_file_hashes={},
        selected_rows=(selected,),
    )


class _FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    def encode_bundle(self, request):
        self.calls += 1
        return empty_bundle(
            request["bundle_id"],
            [item["message_id"] for item in request["messages"]],
            chat_id=request["chat_id"],
            status="complete",
            source="fake",
            schema_version=BUNDLE_SCHEMA_VERSION,
        )


def test_v29_runner_keeps_selected_attempts_separate_from_decisions(tmp_path, monkeypatch):
    checked = _checked_fixture(tmp_path)
    monkeypatch.setattr(
        "wechat_bridge.contextual_bundle_pipeline_v2_9_runner._validate_scheduler_inputs",
        lambda *args, **kwargs: checked,
    )
    output = tmp_path / "contextual_bundle_pipeline_v2_9"
    model = _FakeModel()
    result = run_development_shadow_pilot_v29(
        checked.input_root,
        checked.source_root,
        checked.scheduler_root,
        output,
        provider_config=AIProviderConfig(model="deepseek-v4-flash", api_key="synthetic"),
        provider_health=ProviderHealthResult(
            True,
            "available",
            "synthetic",
            "deepseek-v4-flash",
            "health-hash",
            10,
            4,
            500,
            100,
            1.0,
        ),
        model=model,
        max_provider_calls=1,
        max_retries=1,
    )
    assert model.calls == 1
    assert result.manifest["artifact_version"] == ARTIFACT_VERSION
    aggregate = json.loads((output / OUTPUT_FILENAMES["aggregate"]).read_text(encoding="utf-8"))
    assert aggregate["candidate_decision_count"] == 1
    assert aggregate["selected_package_count"] == 1
    assert aggregate["counters"]["provider_request_attempts"] == 1
    assert aggregate["counters"]["candidate_decisions"] == 1
    assert aggregate["counters"]["successful_model_outputs"] == 1
    assert aggregate["activation_cues"]["preservation_rate"] == 1.0
    assert aggregate["zero_tolerance"]["fallback_accepted"] == 0


def test_v29_error_buckets_are_body_free_and_conservative():
    assert _error_bucket("evidence_span_out_of_bounds") == "evidence"
    assert _error_bucket("semantic_wire_schema_invalid") == "schema"
    assert _error_bucket("input_token_limit_exceeded") == "token_input_limit"
    assert _error_bucket("provider_protocol_error") == "provider_protocol"


def test_prior_capability_health_can_be_reused_without_a_new_probe(tmp_path):
    source = tmp_path / "contextual_bundle_provider_semantic_frame_v1"
    source.mkdir()
    path = source / "provider_capabilities.private.json"
    path.write_text(
        json.dumps(
            {
                "frozen_read": False,
                "gold_loaded": False,
                "candidates": [
                    {
                        "model_id": "deepseek-v4-pro",
                        "health_ok": True,
                        "health_status": "available",
                        "health_input_tokens": 12,
                        "health_output_tokens": 8,
                        "health_latency_ms": 1.0,
                        "semantic_frame_confirmed": True,
                        "json_object_confirmed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    health = _health_from_capability_artifact(path, model="deepseek-v4-pro")
    assert health["ok"] is True
    assert health["model"] == "deepseek-v4-pro"
    assert health["request_sha256"] == "N/A"
