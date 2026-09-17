import json
from pathlib import Path

from wechat_bridge.bundle_semantics import BUNDLE_SCHEMA_VERSION
from wechat_bridge.contextual_bundle_pipeline import AIProviderConfig
from wechat_bridge.contextual_bundle_pipeline_v2_9_runner import SchedulerInputs
from wechat_bridge.contextual_bundle_pipeline_v2_10_runner import CapacityInputs
from wechat_bridge.contextual_bundle_pipeline_v2_11_runner import (
    ARTIFACT_VERSION,
    OUTPUT_FILENAMES,
    run_development_shadow_pilot_v211,
)
from wechat_bridge.semantic_wire import build_symbol_table
from wechat_bridge.semantic_wire_v2_11_tsv import (
    assemble_tsv_frame,
    parse_tsv_frame,
    tsv_frame_exemplar,
)
from wechat_bridge.semantic_wire_v3_compact import build_compact_request


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
    selected = {
        "candidate_id": "candidate-a",
        "semantic_package_id": "package-candidate-a",
        "selection_status": "selected",
        "selection_reason": "selected_budget_rank",
        "selected_representative_id": "candidate-a",
        "scheduled_for_encode": True,
        "scale": "local",
        "channel": "pending_context",
        "chat_id": "chat-a",
        "source_message_count": 1,
        "fragment_count": 1,
        "claim_count": 1,
        "evidence_count": 1,
        "source_message_ids": ["message-a"],
        "activation_cues": [{"cue_type": "synthetic", "executable": True, "replay_key": "cue-a"}],
        "activation_cue_replayable": True,
    }
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


class _FakeTsvModel:
    def __init__(self):
        self.calls = 0

    def encode_bundle(self, request):
        self.calls += 1
        table = build_symbol_table(request)
        frame = parse_tsv_frame(tsv_frame_exemplar(), symbol_table=table, max_claims=8)
        bundles = assemble_tsv_frame(frame, request, symbol_table=table)
        return {
            "claims": bundles,
            "usage": {"prompt_tokens": 20, "completion_tokens": 20},
        }


def test_v211_runner_records_no_health_and_blocks_unscored_protocol(tmp_path, monkeypatch):
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
        "wechat_bridge.contextual_bundle_pipeline_v2_11_runner._validate_scheduler_inputs",
        lambda *args, **kwargs: checked,
    )
    monkeypatch.setattr(
        "wechat_bridge.contextual_bundle_pipeline_v2_11_runner._capacity_inputs",
        lambda *args, **kwargs: capacity,
    )
    model = _FakeTsvModel()
    output = tmp_path / "contextual_bundle_pipeline_v2_11"
    result = run_development_shadow_pilot_v211(
        checked.input_root,
        checked.source_root,
        checked.scheduler_root,
        output,
        provider_config=AIProviderConfig(model="deepseek-v4-flash", api_key="synthetic"),
        model=model,
        max_provider_calls=1,
    )
    assert model.calls == 1
    assert result.manifest["artifact_version"] == ARTIFACT_VERSION
    assert result.manifest["provider_incompatible"] is True
    assert result.manifest["production_blocked"] is True
    assert result.manifest["health_probe_performed"] is False
    aggregate = json.loads((output / OUTPUT_FILENAMES["aggregate"]).read_text(encoding="utf-8"))
    assert aggregate["schema_complete_rate"] == 1.0
    assert aggregate["semantic_audit"]["passed"] == "N/A"
    assert aggregate["production_blocked"] is True
    health = json.loads((output / OUTPUT_FILENAMES["provider_health"]).read_text(encoding="utf-8"))
    assert health["health_probe_calls"] == 0
