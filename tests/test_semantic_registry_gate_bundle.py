"""Synthetic acceptance tests for Workstream A's reversible context path.

These tests use only invented public mappings.  They intentionally stop at
registration, semantic gating, fragments/claims and dialogue bundles; no
event, title, frontend or private/frozen input is involved.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from wechat_bridge.dialogue_bundle import (
    BundleClaim,
    BundleFragment,
    DialogueBundleBuilder,
    build_dialogue_bundles,
)
from wechat_bridge.semantic_gate import (
    CHANNEL_BACKGROUND,
    CHANNEL_COLD_RECOVERABLE,
    CHANNEL_IMMEDIATE,
    CHANNEL_PENDING_CONTEXT,
    SemanticGate,
)
from wechat_bridge.semantic_registry import (
    UNKNOWN,
    MessageRegistry,
)


def _message(
    message_id: str,
    text: str,
    *,
    account: str = "account-synthetic",
    chat: str = "chat-synthetic",
    speaker: str = "speaker-synthetic",
    sequence: int = 1,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "message_id": message_id,
        "account_id": account,
        "chat_id": chat,
        "speaker_id": speaker,
        "sender_name": "should-not-be-used-as-authority",
        "content": text,
        "message_type": "text",
        "sequence_in_chat": sequence,
        "time_offset_seconds": sequence,
        "split": "development",
    }
    value.update(extra)
    return value


def _fragment(
    fragment_id: str,
    message_id: str,
    text: str,
    *,
    account: str = "account-synthetic",
    chat: str = "chat-synthetic",
    speaker: str = "speaker-synthetic",
    subject: str = "unknown",
    object_id: str = "unknown",
    object_resolution: str = "unknown",
    state: str = "unknown",
    intent: str = "statement",
    fragment_type: str = "statement",
    actions: tuple[str, ...] = (),
    segment_id: str = "segment-synthetic",
    role: str = "substantive",
    is_opener: bool = False,
    is_silent: bool = False,
    reply_to_message_id: str | None = None,
    object_inherited_from_id: str | None = None,
    topic_shift: bool = False,
    state_evidence: str = "unknown",
) -> BundleFragment:
    return BundleFragment(
        fragment_id=fragment_id,
        message_id=message_id,
        account_id=account,
        chat_id=chat,
        segment_id=segment_id,
        text=text,
        span_start=0,
        span_end=len(text),
        role=role,
        fragment_type=fragment_type,
        speaker_id=speaker,
        mentioned_person_ids=(),
        subject_id=subject,
        subject_type="person" if subject != "unknown" else "unknown",
        object_id=object_id,
        object_resolution=object_resolution,
        object_inherited_from_id=object_inherited_from_id,
        state=state,
        state_evidence=state_evidence,
        closure_reason=state if state in {"resolved", "failed", "cancelled"} else "unknown",
        intent=intent,
        claim_role="question" if intent == "question" else "fact",
        modality="unknown",
        actions=actions,
        topic_shift=topic_shift,
        is_opener=is_opener,
        is_silent=is_silent,
        reply_to_message_id=reply_to_message_id,
    )


def test_registry_uses_metadata_authority_and_freezes_public_raw_reference():
    message = _message("m-authority", "synthetic body", speaker="speaker-authoritative")
    registry = MessageRegistry()
    entry = registry.register(message)

    assert entry.metadata.speaker_id == "speaker-authoritative"
    assert entry.metadata.account_id == "account-synthetic"
    assert entry.metadata.chat_id == "chat-synthetic"
    assert entry.raw_message_ref.get("content") == "synthetic body"
    with pytest.raises(TypeError):
        entry.raw_message_ref["content"] = "changed"  # type: ignore[index]

    message["content"] = "mutated after registration"
    assert entry.content == "synthetic body"
    exported = entry.to_dict()
    assert "content" not in exported
    assert "raw_message" not in exported
    assert len(entry.content_hash) == 64
    assert len(entry.metadata_hash) == 64
    assert len(entry.record_hash) == 64


def test_registry_does_not_guess_missing_metadata_from_display_fields():
    entry = MessageRegistry().register(
        {
            "message_id": "m-missing-metadata",
            "sender_name": "display-name-is-not-authority",
            "content": "synthetic body",
        }
    )
    assert entry.metadata.speaker_id == UNKNOWN
    assert entry.metadata.account_id == UNKNOWN
    assert entry.metadata.chat_id == UNKNOWN
    assert entry.metadata.metadata_complete is False


def test_registry_replay_is_idempotent_and_fragment_adapter_keeps_unknown_sequence():
    registry = MessageRegistry()
    message = {"message_id": "m-replay", "account_id": "a", "chat_id": "c", "content": "body"}
    first = registry.register(message)
    second = registry.register(dict(message))
    assert first is second
    assert len(registry) == 1

    builder = DialogueBundleBuilder(registry=registry)
    builder.add_fragment(
        _fragment("f-sequence-unknown", "m-fragment-only", "body", account="a", chat="c")
    )
    fragment_entry = registry.get("m-fragment-only")
    assert fragment_entry.metadata.sequence_in_chat is None


def test_registry_hash_and_cache_key_are_replayable_for_same_public_input():
    first = MessageRegistry().register(_message("m-stable", "same body"))
    second = MessageRegistry().register(_message("m-stable", "same body"))
    assert first.record_hash == second.record_hash
    assert first.cache_key == second.cache_key
    assert first.schema_version == second.schema_version


def test_gate_routes_four_channels_without_irreversible_drop():
    registry = MessageRegistry()
    gate = SemanticGate(registry)
    immediate = gate.route(registry.register(_message("m-immediate", "synthetic substantive")))
    pending = gate.route(
        registry.register(
            _message(
                "m-pending",
                "这个稍后确认",
                dialogue_role="context_only",
            )
        )
    )
    background = gate.route(registry.register(_message("m-greeting", "你好")))
    cold = gate.route(
        registry.register(
            _message(
                "m-cold",
                "historical recoverable note",
                source_mode="recovered",
                semantic_channel=CHANNEL_COLD_RECOVERABLE,
            )
        )
    )

    assert immediate.channel == CHANNEL_IMMEDIATE
    assert pending.channel == CHANNEL_PENDING_CONTEXT
    assert background.channel == CHANNEL_BACKGROUND
    assert cold.channel == CHANNEL_COLD_RECOVERABLE
    assert all(item.reversible for item in (immediate, pending, background, cold))
    assert gate.active_ids() == (
        "m-immediate",
        "m-pending",
        "m-greeting",
        "m-cold",
    )


def test_pending_context_can_be_reactivated_with_a_reversible_transition():
    registry = MessageRegistry()
    gate = SemanticGate(registry)
    entry = registry.register(_message("m-reactivate", "这个还要上下文", dialogue_role="context_only"))
    first = gate.route(entry)
    second = gate.reactivate("m-reactivate", channel=CHANNEL_IMMEDIATE, reason="new_context")

    assert first.channel == CHANNEL_PENDING_CONTEXT
    assert second.channel == CHANNEL_IMMEDIATE
    assert second.previous_channel == CHANNEL_PENDING_CONTEXT
    assert second.reversible is True
    assert gate.history("m-reactivate")[-1].channel == CHANNEL_IMMEDIATE


def test_bundle_keeps_two_fragments_from_one_message_and_one_claim_evidence():
    message = _message("m-multi", "first | second")
    fragments = [
        _fragment("f-one", "m-multi", "first", object_id="OBJECT_A", object_resolution="explicit"),
        _fragment("f-two", "m-multi", "second", object_id="OBJECT_B", object_resolution="explicit"),
    ]
    claims = [
        BundleClaim(
            claim_id="claim-one",
            fragment_id="f-one",
            message_id="m-multi",
            evidence_span=(0, 5),
            claim_type="fact",
        )
    ]
    result = build_dialogue_bundles([message], fragments=fragments, claims=claims)

    assert [item.fragment_id for item in result.fragments] == ["f-one", "f-two"]
    assert len(result.claims) == 1
    assert result.claims[0].evidence_span == (0, 5)
    assert result.claims[0].evidence_refs == ({"type": "fragment", "id": "f-one", "span": {"start": 0, "end": 5}},)
    assert all(item.message_id == "m-multi" for item in result.fragments)
    assert "events" not in result.to_dict()


def test_context_relations_require_semantic_support_and_block_cross_chat():
    left = _fragment("f-left", "m-left", "question", object_id="OBJECT_A", object_resolution="explicit", intent="question", fragment_type="question")
    right = _fragment("f-right", "m-right", "unrelated", object_id="OBJECT_B", object_resolution="explicit", state="resolved", state_evidence="explicit")
    cross_chat = replace(right, fragment_id="f-cross", message_id="m-cross", chat_id="other-chat")
    result = build_dialogue_bundles(
        [
            _message("m-left", "question", sequence=1),
            _message("m-right", "unrelated", sequence=2),
            _message("m-cross", "unrelated", chat="other-chat", sequence=3),
        ],
        fragments=[left, right, cross_chat],
    )

    assert not any(item.label in {"continues", "elaborates", "answers", "contrasts"} for item in result.relations)
    assert not any("m-cross" in item.source_message_ids and item.evidence_strength != "none" for item in result.relations)


def test_question_answer_can_skip_opener_and_context_bridge():
    question = _fragment("f-q", "m-q", "question", object_id="OBJECT_A", object_resolution="explicit", intent="question", fragment_type="question")
    opener = _fragment("f-opener", "m-opener", "你好", role="conversation_opener", fragment_type="conversation_opener", is_opener=True)
    bridge = _fragment("f-bridge", "m-bridge", "收到", role="context_only", fragment_type="acknowledgement")
    answer = _fragment("f-a", "m-a", "resolved", object_id="OBJECT_A", object_resolution="inherited", object_inherited_from_id="f-q", state="resolved", state_evidence="explicit", actions=("resolve",), fragment_type="answer", intent="answer")
    result = build_dialogue_bundles(
        [
            _message("m-q", "question", sequence=1),
            _message("m-opener", "你好", sequence=2),
            _message("m-bridge", "收到", sequence=3),
            _message("m-a", "resolved", sequence=4),
        ],
        fragments=[question, opener, bridge, answer],
    )
    direct = [item for item in result.relations if item.source_message_ids == ("m-q", "m-a")]
    assert any(item.label == "answers" for item in direct)
    assert not any(item.label == "answers" and item.source_message_ids == ("m-q", "m-opener") for item in result.relations)
    assert not any(item.label == "answers" and item.source_message_ids == ("m-q", "m-bridge") for item in result.relations)


def test_state_update_window_and_history_do_not_turn_silence_into_terminal_state():
    failed = _fragment("f-failed", "m-failed", "failed", object_id="OBJECT_A", object_resolution="explicit", state="failed", state_evidence="explicit")
    silent = _fragment("f-silent", "m-silent", "", is_silent=True, fragment_type="unknown")
    resolved = _fragment("f-resolved", "m-resolved", "resolved", object_id="OBJECT_A", object_resolution="inherited", object_inherited_from_id="f-failed", state="resolved", state_evidence="explicit")
    result = build_dialogue_bundles(
        [
            _message("m-failed", "failed", sequence=1),
            _message("m-silent", "", sequence=2, message_type="image"),
            _message("m-resolved", "resolved", sequence=3),
        ],
        fragments=[failed, silent, resolved],
    )

    assert any(item.subtype == "state_update" for item in result.relations)
    assert all(item.state != "resolved" for item in result.fragments if item.is_silent)
    assert all(item.state != "resolved" for item in result.fragments if item.is_opener)


def test_same_segment_and_time_only_do_not_create_relation():
    left = _fragment("f-time-left", "m-time-left", "left", segment_id="same", object_id="unknown")
    right = _fragment("f-time-right", "m-time-right", "right", segment_id="same", object_id="unknown")
    result = build_dialogue_bundles(
        [
            _message("m-time-left", "left", sequence=1, time_offset_seconds=10),
            _message("m-time-right", "right", sequence=2, time_offset_seconds=11),
        ],
        fragments=[left, right],
    )
    assert result.relations == ()


def test_forced_snapshot_preserves_open_boundary_and_hash_skeleton():
    builder = DialogueBundleBuilder()
    builder.add_fragment(
        _fragment("f-open", "m-open", "open", object_id="OBJECT_A", object_resolution="explicit")
    )
    snapshot = builder.snapshot(force=True, reason="window_limit")

    assert snapshot.bundles
    assert all(item.forced_snapshot is True for item in snapshot.bundles)
    assert all(item.open_boundary is True for item in snapshot.bundles)
    assert all(item.end_time_source == "unknown" for item in snapshot.bundles)
    assert snapshot.cache_key
    assert snapshot.input_hash
    assert snapshot.to_dict()["schema_version"] == "semantic_v2"


def test_ambiguous_local_history_can_emit_multiple_candidate_bundles_without_merging_claims():
    first = _fragment("f-a", "m-a", "A", subject="PERSON_P", object_id="OBJECT_A", object_resolution="explicit", actions=("check",))
    second = _fragment("f-b", "m-b", "B", subject="PERSON_P", object_id="OBJECT_B", object_resolution="explicit", actions=("check",))
    third = _fragment("f-c", "m-c", "follow-up", subject="PERSON_P", object_id="unknown", actions=("check",))
    result = build_dialogue_bundles(
        [
            _message("m-a", "A", sequence=1),
            _message("m-b", "B", sequence=2),
            _message("m-c", "follow-up", sequence=3),
        ],
        fragments=[first, second, third],
        claims=[
            BundleClaim("claim-c", "f-c", "m-c", (0, 9), "fact"),
        ],
    )

    candidates = [item for item in result.bundles if "f-c" in item.fragment_ids and item.scale == "local"]
    assert len(candidates) >= 2
    assert len(set(item.bundle_id for item in candidates)) == len(candidates)
    assert all("claim-c" in item.claim_ids for item in candidates)
    assert all(len(item.evidence_refs) == 1 for item in result.claims)


def test_public_serialization_contains_versions_and_no_event_layer():
    result = build_dialogue_bundles(
        [_message("m-serialize", "synthetic")],
        fragments=[_fragment("f-serialize", "m-serialize", "synthetic")],
    )
    payload = result.to_dict()
    assert payload["schema_version"] == "semantic_v2"
    assert payload["context_schema_version"] == "dialogue_context_v1"
    assert payload["pipeline_version"]
    assert payload["ruleset_version"]
    assert "events" not in payload
    assert all("raw_message" not in item for item in payload["registrations"])


def test_contract_projection_exposes_canonical_window_and_gate_fields():
    result = build_dialogue_bundles(
        [_message("m-contract", "synthetic")],
        fragments=[_fragment("f-contract", "m-contract", "synthetic")],
    )
    bundle = next(item for item in result.bundles if item.scale == "local")
    relationless = bundle.to_dict()
    assert relationless["window_scale"] == "W1"
    assert relationless["member_fragment_ids"] == ["f-contract"]
    assert relationless["status"] == "open"
    decision = result.gate_decisions[0].to_dict()
    assert decision["from_channel"] == "registered"
    assert decision["to_channel"] == decision["channel"]
    assert decision["budget_class"] in {"immediate", "deferred", "recovery"}


def test_silent_terminal_input_is_normalized_to_open_unknown_context():
    fragment = BundleFragment.from_mapping(
        {
            "fragment_id": "f-silent-terminal",
            "message_id": "m-silent-terminal",
            "account_id": "a",
            "chat_id": "c",
            "text": "",
            "is_silent": True,
            "state": "resolved",
            "state_evidence": "explicit",
            "closure_reason": "resolved",
        }
    )
    assert fragment.state == UNKNOWN
    assert fragment.closure_reason == UNKNOWN
    result = build_dialogue_bundles(
        [_message("m-silent-terminal", "", account="a", chat="c", message_type="image")],
        fragments=[fragment],
    )
    assert all(item.open_boundary for item in result.bundles)
