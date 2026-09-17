"""Synthetic Workstream I scheduler contract checks.

All candidates are invented structured metadata.  No provider, private
artifact, event layer, or frontend is used here.
"""

from __future__ import annotations

from wechat_bridge.contextual_bundle_scheduler_v2_9 import (
    schedule_bundle_candidates,
)


def _candidate(
    bundle_id,
    *,
    scale="local",
    channel="immediate",
    chat_id="chat-a",
    messages=("message-a",),
    fragments=("fragment-a",),
    claims=("claim-a",),
    evidence=("evidence-a",),
    information_value="unknown",
    event_completeness="unknown",
    uncertainties=(),
    semantic_bundle=None,
    candidate_bundle_ids=(),
):
    return {
        "bundle_id": bundle_id,
        "scale": scale,
        "channel": channel,
        "chat_id": chat_id,
        "source_message_ids": list(messages),
        "fragment_ids": list(fragments),
        "claim_ids": list(claims),
        "evidence_refs": [{"id": item, "type": "fragment"} for item in evidence],
        "information_value": information_value,
        "event_completeness": event_completeness,
        "uncertainties": list(uncertainties),
        "semantic_bundle": semantic_bundle or {},
        "candidate_bundle_ids": list(candidate_bundle_ids),
    }


def test_scheduler_deduplicates_semantic_package_but_preserves_windows():
    candidates = [
        _candidate("micro-a", scale="micro"),
        _candidate("turn-a", scale="turn"),
        _candidate("local-a", scale="local"),
        _candidate("session-a", scale="session"),
        _candidate(
            "other-chat",
            chat_id="chat-b",
            messages=("message-b",),
            fragments=("fragment-b",),
            claims=("claim-b",),
            evidence=("evidence-b",),
        ),
    ]
    result = schedule_bundle_candidates(candidates, max_provider_calls=2)
    assert result.metrics["candidate_count"] == 5
    assert result.metrics["decision_count"] == 5
    assert result.metrics["unique_semantic_package_count"] == 2
    assert result.metrics["selected_count"] == 2
    assert result.metrics["provider_calls"] == 0
    assert result.metrics["encoded_bundle_count"] == 0
    selected = [item for item in result.decisions if item["selection_status"] == "selected"]
    assert len(selected) == 2
    package = next(item for item in selected if item["semantic_package_candidate_count"] == 4)
    assert set(package["window_membership"]) == {"micro", "turn", "local", "session"}
    duplicates = [item for item in result.decisions if item["selection_reason"] == "duplicate_semantic_package"]
    assert len(duplicates) == 3


def test_pending_and_cold_candidates_always_have_replayable_activation_cues():
    candidates = [
        _candidate(
            "pending",
            channel="pending_context",
            uncertainties=("object_unknown", "state_unknown"),
            semantic_bundle={
                "claim_type": "question",
                "subject": {"id": "person-a"},
            },
        ),
        _candidate(
            "cold",
            channel="cold_recoverable",
            scale="cold",
            messages=("message-c",),
            fragments=("fragment-c",),
            claims=(),
            evidence=(),
            candidate_bundle_ids=("prior-window",),
        ),
    ]
    first = schedule_bundle_candidates(candidates, max_provider_calls=1)
    second = schedule_bundle_candidates(candidates, max_provider_calls=1)
    assert first.metrics["activation_cue_eligible_count"] == 2
    assert first.metrics["activation_cue_zero_count"] == 0
    assert first.metrics["activation_cue_coverage_rate"] == 1.0
    for left, right in zip(first.decisions, second.decisions):
        if left["channel"] in {"pending_context", "cold_recoverable"}:
            assert left["activation_cues"]
            assert all(cue["executable"] is True for cue in left["activation_cues"])
            assert left["activation_cues"] == right["activation_cues"]


def test_scheduler_prioritizes_structured_slots_and_keeps_time_zero_weight():
    candidates = [
        _candidate(
            "dense",
            chat_id="chat-dense",
            information_value="high",
            event_completeness="sufficient",
            semantic_bundle={
                "subject": {"id": "person-a"},
                "object": [{"id": "object-a"}],
                "action": [{"label": "confirm"}],
                "state": "ongoing",
                "claim_type": "question",
                "evidence": [{"id": "evidence-a"}],
            },
        ),
        _candidate(
            "thin",
            chat_id="chat-thin",
            information_value="unknown",
            event_completeness="unknown",
            messages=("message-z",),
            fragments=("fragment-z",),
            claims=(),
            evidence=(),
        ),
    ]
    result = schedule_bundle_candidates(candidates, max_provider_calls=1)
    assert result.decisions[0]["candidate_id"] == "dense"
    assert result.decisions[0]["selection_status"] == "selected"
    assert result.decisions[0]["score_breakdown"]["time"] == 0
    assert result.decisions[0]["score_breakdown"]["same_segment"] == 0
    assert result.metrics["time_signal_weight"] == 0
    assert result.metrics["same_segment_signal_weight"] == 0
    assert result.metrics["estimated_message_coverage_count"] == 1
    assert result.metrics["compression_ratio"] == 2.0
