"""Synthetic P0.2 relation/blocking regressions.

These cases deliberately exercise scope and evidence semantics, not any
private development vocabulary or frozen annotation rows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from wechat_bridge.semantic_pipeline import (
    RELATION_SAME_EVENT,
    legacy_messages_to_v2,
    run_semantic_pipeline_p02,
)


START = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def _message(
    message_id,
    content,
    seconds=0,
    *,
    account_id="synthetic-account",
    chat_id="synthetic-chat",
    block_id="block-a",
    segment_id="segment-a",
    position_in_block=None,
    **extra,
):
    value = {
        "data_origin": "synthetic",
        "message_id": message_id,
        "account_id": account_id,
        "chat_id": chat_id,
        "sender_id": "synthetic-sender",
        "sender_name": "合成人员",
        "content": content,
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(),
        "message_type": "text",
        "is_group": True,
        "is_self": False,
        "block_id": block_id,
        "dialogue_segment_id": segment_id,
    }
    if position_in_block is not None:
        value["position_in_block"] = position_in_block
    value.update(extra)
    return value


def _claim(result, message_id):
    values = [item for item in result.claims if item.message_id == message_id]
    assert len(values) == 1
    return values[0]


def _decision(result, left_message_id, right_message_id):
    claim_ids = {
        _claim(result, left_message_id).claim_id,
        _claim(result, right_message_id).claim_id,
    }
    return next(
        item
        for item in result.pair_decisions
        if {item.left_claim_id, item.right_claim_id} == claim_ids
    )


def test_p02_candidate_scope_is_hard_and_same_event_key_is_not_an_input():
    result = run_semantic_pipeline_p02(
        [
            _message("m-a", "GPT 重置了。", 0, block_id="block-a", segment_id="segment-a"),
            _message("m-b", "GPT 重置了。", 30, block_id="block-b", segment_id="segment-b"),
            _message(
                "m-reply",
                "GPT 重置了。",
                60,
                block_id="block-c",
                segment_id="segment-c",
                reply_to_message_id="m-a",
            ),
            _message(
                "m-other-chat",
                "GPT 重置了。",
                90,
                chat_id="other-chat",
                block_id="block-d",
                segment_id="segment-d",
                reply_to_message_id="m-a",
            ),
            _message(
                "m-alias-a",
                "GPT 重置了。",
                120,
                block_id="block-e",
                segment_id="segment-e",
                shared_instance_id="fixture-label-must-not-authorize",
            ),
            _message(
                "m-alias-b",
                "GPT 重置了。",
                150,
                block_id="block-f",
                segment_id="segment-f",
                shared_instance_id="fixture-label-must-not-authorize",
            ),
        ]
    )

    candidate_messages = {
        frozenset(item.source_message_ids): item
        for item in result.candidate_pairs
    }
    assert frozenset({"m-a", "m-b"}) not in candidate_messages
    assert frozenset({"m-a", "m-reply"}) in candidate_messages
    assert "explicit_reply" in candidate_messages[frozenset({"m-a", "m-reply"})].blocking_reasons
    assert frozenset({"m-a", "m-other-chat"}) not in candidate_messages
    assert frozenset({"m-alias-a", "m-alias-b"}) not in candidate_messages

    mapped = legacy_messages_to_v2(
        [_message("m-leak", "GPT 重置了。", same_event_key="fixture-label")],
        scope_boundaries=True,
    )
    assert mapped[0].explicit_instance_id is None


@pytest.mark.parametrize(
    ("left", "right", "marker", "left_extra", "right_extra"),
    [
        ("GPT 重置了。", "Codex 重置了。", "CORE_OBJECT_CONFLICT", {}, {}),
        ("GPT 重置了。", "GPT 价格太贵。", "ACTION_TYPE_CONFLICT", {}, {}),
        ("GPT 怎么重置？", "GPT 建议重置。", "INTENT_CONFLICT", {}, {}),
        (
            "GPT 重置了。",
            "GPT 重置了。",
            "ATTRIBUTION_CONFLICT",
            {"attribution": "direct"},
            {"attribution": "quote"},
        ),
    ],
)
def test_p02_global_conflicts_veto_same_event_even_with_reply(
    left, right, marker, left_extra, right_extra
):
    result = run_semantic_pipeline_p02(
        [
            _message("m-left", left, 0, **left_extra),
            _message(
                "m-right",
                right,
                30,
                reply_to_message_id="m-left",
                **right_extra,
            ),
        ]
    )
    decision = _decision(result, "m-left", "m-right")
    assert decision.relation != RELATION_SAME_EVENT
    assert decision.must_not_link is True
    assert marker in decision.must_not_link_reason_codes


def test_p02_continuation_is_local_support_only():
    result = run_semantic_pipeline_p02(
        [
            _message("m-first", "GPT 重置了。", 0, position_in_block=0),
            _message("m-follow", "这个 GPT 又重置了。", 30, position_in_block=1),
        ]
    )
    candidate = result.candidate_pairs[0]
    decision = _decision(result, "m-first", "m-follow")
    assert "explicit_continuation" in candidate.blocking_reasons
    assert "explicit_continuation" in decision.supporting_slots
    assert "explicit_reply" not in candidate.blocking_reasons
    assert "shared_instance" not in candidate.blocking_reasons
    assert decision.relation != RELATION_SAME_EVENT

    cross_block = run_semantic_pipeline_p02(
        [
            _message("m-first", "GPT 重置了。", 0, block_id="block-a", segment_id="segment-a"),
            _message(
                "m-follow",
                "这个 GPT 又重置了。",
                30,
                block_id="block-b",
                segment_id="segment-b",
            ),
        ]
    )
    assert not cross_block.candidate_pairs


def test_p02_instance_keys_require_context_and_allow_only_verified_observable_exception():
    known_object = run_semantic_pipeline_p02(
        [
            _message("m-one", "GPT 地址是 https://synthetic.test/item。", 0, block_id="block-a"),
            _message("m-two", "GPT 地址是 https://synthetic.test/item。", 30, block_id="block-b"),
        ]
    )
    first = _claim(known_object, "m-one")
    second = _claim(known_object, "m-two")
    assert first.explicit_instance_id
    assert first.explicit_instance_id == second.explicit_instance_id
    assert "explicit_shared_instance" in known_object.candidate_pairs[0].blocking_reasons
    assert _decision(known_object, "m-one", "m-two").relation == RELATION_SAME_EVENT

    different_object = run_semantic_pipeline_p02(
        [
            _message("m-one", "GPT 地址是 https://synthetic.test/item。", 0),
            _message("m-two", "Codex 地址是 https://synthetic.test/item。", 30),
        ]
    )
    assert _decision(different_object, "m-one", "m-two").relation != RELATION_SAME_EVENT
    assert _decision(different_object, "m-one", "m-two").must_not_link is True

    unknown_observable = run_semantic_pipeline_p02(
        [
            _message("m-one", "地址是 https://synthetic.test/item。", 0, block_id="block-a"),
            _message("m-two", "地址是 https://synthetic.test/item。", 30, block_id="block-b"),
        ]
    )
    assert _decision(unknown_observable, "m-one", "m-two").relation == RELATION_SAME_EVENT

    sentinel = run_semantic_pipeline_p02(
        [
            _message("m-one", "GPT 重置了。", 0, block_id="block-a", explicit_instance_id="unknown"),
            _message("m-two", "GPT 重置了。", 30, block_id="block-b", explicit_instance_id="unknown"),
        ]
    )
    assert not sentinel.candidate_pairs

