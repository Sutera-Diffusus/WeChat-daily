"""Public synthetic tests for the C2.10 capacity-aware runner."""

from __future__ import annotations

import json
from pathlib import Path

from wechat_bridge.bundle_semantics import BUNDLE_SCHEMA_VERSION
from wechat_bridge.contextual_bundle_pipeline import AIProviderConfig, ProviderHealthResult
from wechat_bridge.contextual_bundle_pipeline_v2_9_runner import SchedulerInputs
from wechat_bridge.contextual_bundle_pipeline_v2_10_runner import (
    ARTIFACT_VERSION,
    CapacityInputs,
    OUTPUT_FILENAMES,
    _capacity_reasons,
    run_development_shadow_pilot_v210,
)
from wechat_bridge.semantic_wire import build_symbol_table
from wechat_bridge.semantic_wire_v3_compact import (
    assemble_compact_frame,
    compact_frame_exemplar,
    parse_compact_frame,
)


def _row(candidate_id: str, *, claim_count: int = 1) -> dict[str, object]:
    return {
        "decision_version": "contextual_bundle_scheduler_schema_v2_9",
        "candidate_id": candidate_id,
        "semantic_package_id": "package-" + candidate_id,
        "selection_status": "selected",
        "selection_reason": "selected_budget_rank",
        "selected_representative_id": candidate_id,
        "scheduled_for_encode": True,
        "scale": "local",
        "window_membership": ["local"],
        "channel": "pending_context",
        "chat_id": "chat-a",
        "source_message_count": 1,
        "fragment_count": 1,
        "claim_count": claim_count,
        "evidence_count": 1,
        "source_message_ids": ["message-a"],
        "activation_cues": [{"cue_type": "synthetic", "executable": True, "replay_key": "cue-a"}],
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
    message = {
        "split": "development",
        "local_day": "2026-08-25",
        "message_id": "message-a",
        "chat_id": "chat-a",
        "account_id": "account-a",
        "speaker_id": "person-a",
        "content": "synthetic content",
    }
    bundle = {"bundle_id": "candidate-a"}
    (source / "bundles.private.jsonl").write_text(json.dumps(bundle) + chr(10), encoding="utf-8")
    (source / "decisions.private.jsonl").write_text(json.dumps(bundle) + chr(10), encoding="utf-8")
    selected = _row("candidate-a")
    return SchedulerInputs(
        input_root=tmp_path / "development",
        source_root=source,
        scheduler_root=scheduler,
        messages=(message,),
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


class _FakeCompactModel:
    def __init__(self) -> None:
        self.calls = 0

    def encode_bundle(self, request):
        self.calls += 1
        table = build_symbol_table(request)
        frame = parse_compact_frame(
            compact_frame_exemplar(1),
            symbol_table=table,
            max_claims=8,
        )
        bundles = assemble_compact_frame(frame, request, symbol_table=table)
        return {
            "claims": bundles,
            "usage": {"prompt_tokens": 20, "completion_tokens": 20},
        }


def test_capacity_reasons_are_explicit_and_conservative():
    limits = {
        "max_messages_per_package": 8,
        "max_fragments_per_package": 8,
        "max_claims_per_package": 8,
        "max_evidence_handles_per_package": 8,
    }
    assert _capacity_reasons(_row("a", claim_count=8), limits) == []
    assert "max_claims_per_package_exceeded" in _capacity_reasons(_row("a", claim_count=9), limits)


def test_v210_runner_uses_compact_wire_and_preserves_capacity_cues(tmp_path, monkeypatch):
    checked = _checked_fixture(tmp_path)
    capacity = CapacityInputs(
        report={
            "artifact_version": "contextual_bundle_scheduler_v2_9_capacity_audit",
            "schema_version": "capacity_audit_schema_v2_9",
            "body_free": True,
            "provider_calls": 0,
            "provenance": {"frozen_read": False, "gold_loaded": False, "provider_invocations": 0},
            "recommendations": {"fourteen_call_current_selected_cap_applied_upper_bound_count": 1},
        },
        path=tmp_path / "capacity.private.json",
        sha256="capacity-hash",
        limits={
            "max_messages_per_package": 8,
            "max_fragments_per_package": 8,
            "max_claims_per_package": 8,
            "max_evidence_handles_per_package": 8,
        },
    )
    monkeypatch.setattr(
        "wechat_bridge.contextual_bundle_pipeline_v2_10_runner._validate_scheduler_inputs",
        lambda *args, **kwargs: checked,
    )
    monkeypatch.setattr(
        "wechat_bridge.contextual_bundle_pipeline_v2_10_runner._capacity_inputs",
        lambda *args, **kwargs: capacity,
    )
    output = tmp_path / "contextual_bundle_pipeline_v2_10"
    model = _FakeCompactModel()
    result = run_development_shadow_pilot_v210(
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
        max_retries=0,
    )
    assert model.calls == 1
    assert result.manifest["artifact_version"] == ARTIFACT_VERSION
    assert result.manifest["wire_schema_version"] == "semantic_wire_v3_compact"
    aggregate = json.loads((output / OUTPUT_FILENAMES["aggregate"]).read_text(encoding="utf-8"))
    assert aggregate["status_counts"]["complete"] == 1
    assert aggregate["counters"]["provider_request_attempts"] == 1
    assert aggregate["effective_encoded_message_count"] == 1
    assert aggregate["schema_evidence"]["schema_valid_count"] == 1
    assert aggregate["zero_tolerance"]["fallback_accepted"] == 0

