"""Fully synthetic regression tests for the offline P0.1 semantic shadow path."""

from datetime import datetime, timedelta, timezone
import json

from wechat_bridge.semantic_pipeline import (
    P01_PIPELINE_VERSION,
    P01_RULESET_VERSION,
    RELATION_INSUFFICIENT_CONTEXT,
    RELATION_RELATED_EVENT,
    RELATION_SAME_EVENT,
    RELATION_SAME_TOPIC_ONLY,
    SHADOW_SOURCE,
    p01_result_to_legacy_preview,
    run_semantic_pipeline_p01,
)


START = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def _message(message_id, content, seconds, *, chat_id="synthetic-chat", sender_id="synthetic-a", **extra):
    value = {
        "data_origin": "synthetic",
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": "合成人员",
        "content": content,
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(),
        "message_type": "text",
        "is_group": True,
        "is_self": False,
    }
    value.update(extra)
    return value


def _claims(result, message_id):
    return [item for item in result.claims if item.message_id == message_id]


def _claim(result, message_id, action=None):
    values = _claims(result, message_id)
    if action is not None:
        values = [item for item in values if item.action == action]
    assert values
    return values[0]


def _decision(result, left_id, right_id):
    pair = {left_id, right_id}
    return next(
        item for item in result.pair_decisions
        if {item.left_claim_id, item.right_claim_id} == pair
    )


def test_p01_short_chinese_aliases_and_clause_claims_have_exact_spans():
    result = run_semantic_pipeline_p01(
        [
            _message("m-gpt", "GPT4 重置了。", 0),
            _message("m-db", "数据库备份今晚几点开始？", 30),
            _message("m-board", "项目看板打不开，而且报价太贵。", 60),
        ]
    )

    gpt = _claim(result, "m-gpt", "reset")
    assert gpt.target_entity_ids == ("ENTITY_GPT",)
    gpt_mention = next(
        item for item in result.mentions
        if item.message_id == "m-gpt" and item.normalized_id == "ENTITY_GPT"
    )
    assert gpt_mention.evidence_text == "GPT4"
    gpt_source = next(item for item in result.messages if item.message_id == "m-gpt")
    assert gpt_source.content[gpt_mention.span_start:gpt_mention.span_end] == "GPT4"

    schedule = _claim(result, "m-db", "schedule")
    assert schedule.claim_type == "question"
    assert schedule.target_entity_ids == ("object:backup",)
    assert schedule.evidence_span.evidence_text == "数据库备份今晚几点开始"

    board_claims = _claims(result, "m-board")
    assert {item.action for item in board_claims} == {"outage", "cost"}
    assert all(item.evidence_span.evidence_text for item in board_claims)
    for claim in board_claims:
        source = next(item for item in result.messages if item.message_id == claim.message_id)
        span = claim.evidence_span
        assert source.content[span.span_start:span.span_end] == span.evidence_text


def test_p01_emits_unknown_action_claim_per_eligible_substantive_clause():
    result = run_semantic_pipeline_p01(
        [_message("m-unknown", "这是一条合成的新事项。", 0)]
    )

    claims = _claims(result, "m-unknown")
    assert len(claims) == 1
    assert claims[0].action == "unknown"
    assert claims[0].claim_text == "这是一条合成的新事项"
    assert claims[0].evidence_span.evidence_text == claims[0].claim_text
    assert "core_entity_unknown" in claims[0].uncertainties


def test_p01_inherits_nearest_object_inside_segment_but_not_context_rows():
    messages = [
        _message("m-topic", "GPT 接口报错。", 0),
        _message("m-follow", "这个接口还是不行。", 30),
        _message(
            "m-context",
            "GPT 重置了。",
            60,
            dialogue_role="context_only",
            dialogue_evidence_eligible=True,
        ),
    ]
    result = run_semantic_pipeline_p01(messages)

    follow = _claim(result, "m-follow", "outage")
    assert follow.target_entity_ids == ("object:interface",)
    assert follow.context_message_ids == ()
    assert "explicit_continuation_cue" in follow.uncertainties
    assert not _claims(result, "m-context")
    assert all("m-context" not in event.source_message_ids for event in result.events)
    assert all(
        evidence.message_id != "m-context"
        for event in result.events
        for evidence in event.evidence_refs
    )


def test_p01_same_event_requires_same_message_reply_shared_instance_or_continuation():
    no_reply = run_semantic_pipeline_p01(
        [
            _message("m-one", "GPT 重置了。", 0),
            _message("m-two", "GPT 重置了。", 30, sender_id="synthetic-b"),
        ]
    )
    first = _claim(no_reply, "m-one", "reset")
    second = _claim(no_reply, "m-two", "reset")
    decision = _decision(no_reply, first.claim_id, second.claim_id)
    assert decision.relation == RELATION_RELATED_EVENT
    assert decision.relation != RELATION_SAME_EVENT
    assert "missing_strong_event_linkage" in decision.uncertainties

    reply = run_semantic_pipeline_p01(
        [
            _message("m-reply-one", "GPT 重置了。", 0),
            _message("m-reply-two", "GPT 重置了。", 7200, reply_to_message_id="m-reply-one"),
        ]
    )
    assert _decision(
        reply,
        _claim(reply, "m-reply-one", "reset").claim_id,
        _claim(reply, "m-reply-two", "reset").claim_id,
    ).relation == RELATION_SAME_EVENT

    shared = run_semantic_pipeline_p01(
        [
            _message("m-instance-one", "GPT 接口状态500。", 0, shared_instance_id="incident-a"),
            _message("m-instance-two", "GPT 接口状态500。", 7200, shared_instance_id="incident-a"),
        ]
    )
    assert _decision(
        shared,
        _claim(shared, "m-instance-one", "outage").claim_id,
        _claim(shared, "m-instance-two", "outage").claim_id,
    ).relation == RELATION_SAME_EVENT


def test_p01_mnl_keeps_different_object_action_cards_separate():
    result = run_semantic_pipeline_p01(
        [_message("m-mixed", "项目看板打不开，而且报价太贵。", 0)]
    )
    outage = _claim(result, "m-mixed", "outage")
    cost = _claim(result, "m-mixed", "cost")
    decision = _decision(result, outage.claim_id, cost.claim_id)
    assert decision.relation == RELATION_SAME_TOPIC_ONLY
    assert decision.must_not_link is True
    assert "CORE_OBJECT_CONFLICT" in decision.must_not_link_reason_codes
    assert "ACTION_TYPE_CONFLICT" in decision.must_not_link_reason_codes
    assert len(result.events) == 2
    assert all(
        set(event.claim_ids) != {outage.claim_id, cost.claim_id}
        for event in result.events
    )


def test_p01_candidate_blocks_use_segment_or_explicit_linkage_not_family_only():
    result = run_semantic_pipeline_p01(
        [
            _message("m-a", "GPT 重置了。", 0, chat_id="chat-a", dialogue_segment_id="segment-a"),
            _message("m-b", "Codex 重置了。", 30, chat_id="chat-a", dialogue_segment_id="segment-a"),
            _message("m-c", "GPT 重置了。", 0, chat_id="chat-a", dialogue_segment_id="segment-b"),
            _message("m-d", "Codex 重置了。", 0, chat_id="chat-c", dialogue_segment_id="segment-c"),
        ]
    )
    a = _claim(result, "m-a", "reset")
    b = _claim(result, "m-b", "reset")
    c = _claim(result, "m-c", "reset")
    d = _claim(result, "m-d", "reset")
    pairs = {
        frozenset((item.left_claim_id, item.right_claim_id)): item
        for item in result.candidate_pairs
    }
    assert frozenset((a.claim_id, b.claim_id)) in pairs
    assert frozenset((a.claim_id, c.claim_id)) not in pairs
    # Same AI family alone is not a cross-block candidate.
    assert frozenset((a.claim_id, d.claim_id)) not in pairs


def test_p01_presentation_is_exact_projection_and_preview_is_shadow_only():
    result = run_semantic_pipeline_p01(
        [
            _message("m-one", "GPT 重置了。", 0),
            _message("m-two", "这个又重置了。", 30),
        ]
    )
    assert result.pipeline_version == P01_PIPELINE_VERSION
    assert result.ruleset_version == P01_RULESET_VERSION
    assert result.source == SHADOW_SOURCE
    for card in result.presentations:
        event = next(item for item in result.events if item.event_id == card.event_id)
        assert card.evidence_refs == event.evidence_refs
        assert set(card.source_message_ids) == set(event.source_message_ids)
        if card.presentation_role != "do_not_display":
            assert card.supported_claim_ids == event.claim_ids
            assert all(sentence.claim_ids and sentence.message_ids for sentence in card.sentences)
    preview = p01_result_to_legacy_preview(result)
    assert preview["source"] == SHADOW_SOURCE
    assert preview["production_connected"] is False
    assert preview["read_only"] is True
    assert preview["candidate_pairs"]
    json.dumps(preview, ensure_ascii=False)


def test_p01_is_deterministic_under_input_shuffle():
    messages = [
        _message("m-1", "你好", 0),
        _message("m-2", "GPT4 重置了。", 20),
        _message("m-3", "这个接口还是不行。", 40),
    ]
    first = run_semantic_pipeline_p01(messages)
    second = run_semantic_pipeline_p01(list(reversed(messages)))
    assert first.to_dict() == second.to_dict()


def test_p01_candidates_cover_every_claim_combination_in_one_segment():
    """Blocking must not discard secondary clauses from either source row."""

    result = run_semantic_pipeline_p01(
        [
            _message(
                "multi-left",
                "GPT重置了，而且Codex重置了。",
                0,
                dialogue_segment_id="shared-segment",
            ),
            _message(
                "multi-right",
                "GPT价格太贵，而且Codex价格太贵。",
                30,
                dialogue_segment_id="shared-segment",
            ),
        ]
    )
    left = _claims(result, "multi-left")
    right = _claims(result, "multi-right")
    assert len(left) == len(right) == 2
    expected_cross_message_pairs = {
        frozenset((one.claim_id, two.claim_id))
        for one in left
        for two in right
    }
    actual_pairs = {
        frozenset((item.left_claim_id, item.right_claim_id))
        for item in result.candidate_pairs
    }
    assert expected_cross_message_pairs <= actual_pairs


def test_p01_unknown_identity_or_action_cannot_merge_even_on_reply():
    cases = (
        [
            _message("unknown-entity-a", "这是一条新的事项。", 0),
            _message(
                "unknown-entity-b",
                "这个事项也是这样。",
                10,
                reply_to_message_id="unknown-entity-a",
            ),
        ],
        [
            _message("unknown-action-a", "GPT这是一个说明。", 0),
            _message(
                "unknown-action-b",
                "GPT这个说明也是这样。",
                10,
                reply_to_message_id="unknown-action-a",
            ),
        ],
    )
    for messages in cases:
        result = run_semantic_pipeline_p01(messages)
        assert result.pair_decisions
        assert all(item.relation == RELATION_INSUFFICIENT_CONTEXT for item in result.pair_decisions)
        assert all(item.relation != RELATION_SAME_EVENT for item in result.pair_decisions)
        assert all(not event.claim_ids or len(event.claim_ids) == 1 for event in result.events)


def test_p01_scope_blocks_cross_chat_and_cross_block_default_but_reply_is_related():
    same_label = [
        _message(
            "scope-a",
            "GPT重置了。",
            0,
            chat_id="chat-one",
            dialogue_segment_id="same-label",
        ),
        _message(
            "scope-b",
            "GPT重置了。",
            0,
            chat_id="chat-two",
            dialogue_segment_id="same-label",
        ),
    ]
    isolated = run_semantic_pipeline_p01(same_label)
    assert not isolated.candidate_pairs
    scoped_segments = {item.dialogue_segment_id for item in isolated.claims}
    assert len(scoped_segments) == 2
    assert all("account=default" in value and "chat=" in value for value in scoped_segments)

    result = run_semantic_pipeline_p01(
        [
            _message(
                "block-a",
                "GPT重置了。",
                0,
                dialogue_segment_id="shared-segment",
                block_id="block-one",
            ),
            _message(
                "block-b",
                "GPT重置了。",
                10,
                dialogue_segment_id="shared-segment",
                block_id="block-two",
            ),
            _message(
                "block-c",
                "GPT重置了。",
                20,
                dialogue_segment_id="shared-segment",
                block_id="block-two",
                reply_to_message_id="block-a",
            ),
        ]
    )
    a = _claim(result, "block-a", "reset")
    b = _claim(result, "block-b", "reset")
    c = _claim(result, "block-c", "reset")
    pair_keys = {
        frozenset((item.left_claim_id, item.right_claim_id)): item
        for item in result.candidate_pairs
    }
    assert frozenset((a.claim_id, b.claim_id)) not in pair_keys
    reply_key = frozenset((a.claim_id, c.claim_id))
    assert reply_key in pair_keys
    assert _decision(result, a.claim_id, c.claim_id).relation == RELATION_RELATED_EVENT
    assert _decision(result, a.claim_id, c.claim_id).must_not_link is True


def test_p01_max_gap_and_boundary_provenance_are_effective():
    messages = [
        _message(
            "boundary-a",
            "GPT重置了。",
            0,
            account_id="account-one",
            dialogue_segment_id="provided-segment",
            block_id="provided-block",
        ),
        _message(
            "boundary-b",
            "GPT重置了。",
            120,
            account_id="account-one",
            dialogue_segment_id="provided-segment",
            block_id="provided-block",
        ),
    ]
    supplied = run_semantic_pipeline_p01(messages)
    assert len(supplied.dialogue_segments) == 1
    segment_id = supplied.claims[0].dialogue_segment_id
    block_id = supplied.claims[0].block_id
    assert segment_id and "account=account-one" in segment_id and "chat=synthetic-chat" in segment_id
    assert block_id and "account=account-one" in block_id and "chat=synthetic-chat" in block_id
    for item in supplied.claims:
        assert segment_id in item.provenance.input_ids
        assert block_id in item.provenance.input_ids
    assert segment_id in supplied.candidate_pairs[0].provenance.input_ids
    assert block_id in supplied.candidate_pairs[0].provenance.input_ids

    split = run_semantic_pipeline_p01(
        [_message("gap-a", "GPT重置了。", 0), _message("gap-b", "GPT重置了。", 120)],
        max_gap_seconds=10,
    )
    assert len(split.dialogue_segments) == 2
    assert not split.candidate_pairs
