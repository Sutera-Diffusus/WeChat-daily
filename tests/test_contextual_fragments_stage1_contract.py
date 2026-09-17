"""Independent synthetic acceptance tests for the Stage 1 context contract.

This file deliberately exercises the public contextual-fragment boundary only.
It does not use private message bodies, event clustering, title generation, or
frontend representations.  The assertions are intentionally independent from
``tests/test_contextual_fragments.py`` so that a duplicate implementation test
cannot make the acceptance result look healthy.
"""

from __future__ import annotations

from typing import Iterable

import pytest

from wechat_bridge.contextual_fragments import (
    LABEL_ANSWERS,
    LABEL_CONTINUES,
    LABEL_ELABORATES,
    ROLE_CONTEXT_ONLY,
    ROLE_CONVERSATION_OPENER,
    RESOLUTION_EXPLICIT,
    RESOLUTION_INHERITED,
    RESOLUTION_UNKNOWN,
    STATE_VALUES,
    extract_context_fragments,
)


CANONICAL_STATES = frozenset(
    {"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"}
)
CANONICAL_RELATION_LABELS = frozenset(
    {
        "continues",
        "elaborates",
        "answers",
        "contrasts",
        "topic_shift",
        "possibly_related",
        "insufficient",
    }
)
SEMANTIC_SIGNAL_CODES = frozenset(
    {
        "shared_explicit_or_inherited_object",
        "shared_action",
        "inherited_object",
        "question_then_turn",
        "state_change",
        "shared_state",
        "explicit_topic_shift",
        "contrast_marker",
    }
)


def _message(
    message_id: str,
    text: str,
    *,
    speaker: str = "synthetic-speaker-a",
    sequence: int | None = None,
    **extra: object,
) -> dict[str, object]:
    """Build a public, synthetic message fixture."""

    value: dict[str, object] = {
        "message_id": message_id,
        "account_id": "synthetic-account",
        "chat_id": "synthetic-chat",
        "speaker_id": speaker,
        "sender_name": speaker,
        "content": text,
        "message_type": "text",
    }
    if sequence is not None:
        value["sequence_in_chat"] = sequence
    value.update(extra)
    return value


def _segment(
    message_ids: Iterable[str],
    *,
    opener_ids: Iterable[str] = (),
    context_ids: Iterable[str] = (),
    substantive_ids: Iterable[str] | None = None,
    segment_id: str = "synthetic-segment",
) -> list[dict[str, object]]:
    ids = list(message_ids)
    return [
        {
            "segment_id": segment_id,
            "message_ids": ids,
            "opener_message_ids": list(opener_ids),
            "context_message_ids": list(context_ids),
            "substantive_message_ids": list(substantive_ids if substantive_ids is not None else ids),
        }
    ]


def _between(result, left_message_id: str, right_message_id: str):
    return [
        relation
        for relation in result.relations
        if relation.source_message_ids == (left_message_id, right_message_id)
    ]


def test_canonical_state_vocabulary_is_exactly_six_values():
    """The public state field uses the six frozen contract values."""

    assert STATE_VALUES == CANONICAL_STATES


@pytest.mark.parametrize(
    ("expected", "text"),
    [
        ("unknown", "这是一条没有状态证据的合成说明"),
        ("planned", "计划处理GPT"),
        ("ongoing", "GPT进行中"),
        ("resolved", "GPT已完成"),
        ("failed", "GPT失败了"),
        ("cancelled", "GPT已取消"),
    ],
)
def test_state_projection_uses_canonical_six_values(expected: str, text: str):
    result = extract_context_fragments([_message("state", text, sequence=1)])
    assert result.fragments[0].state == expected
    assert result.fragments[0].state in CANONICAL_STATES


def test_historical_is_a_temporal_qualifier_not_a_new_state():
    result = extract_context_fragments(
        [_message("historical", "历史上GPT失败了", sequence=1)]
    )
    fragment = result.fragments[0]
    assert fragment.state == "failed"
    assert fragment.temporal_qualifier == "historical"


@pytest.mark.parametrize(
    "text",
    [
        "GPT还在处理中",  # collection window starts in the middle
        "GPT开始处理了",  # only a semantic start cue is visible
        "GPT已经完成了",  # only a semantic end cue is visible
    ],
)
def test_missing_fragment_boundaries_remain_unknown(text: str):
    result = extract_context_fragments(
        [
            _message(
                "boundary",
                text,
                sequence=2,
                time_offset_seconds=120,
                timestamp="2026-08-25T09:02:00+08:00",
            )
        ]
    )
    fragment = result.fragments[0]

    # Message ordering time is not semantic event start/end evidence.
    assert fragment.time_offset_seconds == 120.0
    assert fragment.start_time is None
    assert fragment.end_time is None
    assert fragment.start_time_source == RESOLUTION_UNKNOWN
    assert fragment.end_time_source == RESOLUTION_UNKNOWN


def test_no_reply_can_support_an_answer_when_two_semantic_signals_exist():
    result = extract_context_fragments(
        [
            _message("question", "GPT怎么恢复？", speaker="a", sequence=1),
            _message("answer", "已经恢复了", speaker="b", sequence=2),
        ]
    )
    answer_relations = _between(result, "question", "answer")
    relation = next(item for item in answer_relations if item.relation == LABEL_ANSWERS)

    assert relation.explicit_reply_present is False
    # same segment and clock proximity are not semantic evidence.  A no-reply
    # answer must still expose at least two independent semantic signals.
    semantic_signals = set(relation.supporting_signals) & SEMANTIC_SIGNAL_CODES
    assert len(semantic_signals) >= 2
    assert relation.time_evidence in {"none", "weak"}


@pytest.mark.parametrize(
    ("bridge_text", "bridge_role"),
    [
        ("你好", ROLE_CONVERSATION_OPENER),
        ("收到", ROLE_CONTEXT_ONLY),
        ("我先记一下", ROLE_CONTEXT_ONLY),
    ],
)
def test_answer_can_skip_opener_context_only_or_inserted_bridge(
    bridge_text: str, bridge_role: str
):
    messages = [
        _message("question", "GPT怎么恢复？", speaker="a", sequence=1),
        _message(
            "bridge",
            bridge_text,
            speaker="b",
            sequence=2,
            dialogue_role=bridge_role,
        ),
        _message("answer", "已经恢复了", speaker="b", sequence=3),
    ]
    segments = _segment(
        ["question", "bridge", "answer"],
        opener_ids=["bridge"] if bridge_role == ROLE_CONVERSATION_OPENER else (),
        context_ids=["bridge"] if bridge_role == ROLE_CONTEXT_ONLY else (),
        substantive_ids=["question", "answer"],
    )
    result = extract_context_fragments(messages, segments)

    bridge = next(item for item in result.fragments if item.message_id == "bridge")
    assert bridge.role == bridge_role
    assert bridge.intent in {"greeting", "acknowledgement"}

    # The direct edge must survive a non-substantive/interposed fragment even
    # though no message supplies reply_to_message_id.
    direct = _between(result, "question", "answer")
    relation = next(item for item in direct if item.relation == LABEL_ANSWERS)
    assert relation.explicit_reply_present is False
    answer = next(item for item in result.fragments if item.message_id == "answer")
    assert answer.object_resolution == RESOLUTION_INHERITED
    assert answer.object_inherited_from_id

    # A segment marker alone cannot turn the bridge into a question answer.
    assert not any(item.relation == LABEL_ANSWERS for item in _between(result, "question", "bridge"))


def test_greeting_then_new_topic_does_not_inherit_old_object_or_close_old_state():
    messages = [
        _message("old", "GPT失败了", sequence=1),
        _message("greeting", "你好", sequence=2, dialogue_role=ROLE_CONVERSATION_OPENER),
        _message("new", "明天选课怎么办？", sequence=3),
    ]
    segments = _segment(
        ["old", "greeting", "new"],
        opener_ids=["greeting"],
        substantive_ids=["old", "new"],
    )
    result = extract_context_fragments(messages, segments)
    old, greeting, new = result.fragments

    assert greeting.role == ROLE_CONVERSATION_OPENER
    assert old.state == "failed"
    assert new.object_resolution == RESOLUTION_EXPLICIT
    assert new.object_id != old.object_id
    assert new.context_message_ids == ()
    assert not any(
        item.relation in {LABEL_ANSWERS, LABEL_CONTINUES, LABEL_ELABORATES}
        for item in _between(result, "greeting", "new")
    )
    assert all(item.relation != "resolved" for item in result.relations)


def test_object_ellipsis_inherits_only_a_unique_antecedent():
    unique = extract_context_fragments(
        [
            _message("unique-first", "GPT失败了", sequence=1),
            _message("unique-next", "后来恢复了", sequence=2),
        ]
    )
    first, next_fragment = unique.fragments
    assert first.object_resolution == RESOLUTION_EXPLICIT
    assert next_fragment.object_resolution == RESOLUTION_INHERITED
    assert next_fragment.object_inherited_from_id == first.objects[0].ref_id
    assert next_fragment.object_evidence_refs
    assert next_fragment.context_message_ids == ("unique-first",)

    competing = extract_context_fragments(
        [
            _message("competing-first", "GPT和Codex都失败了", sequence=1),
            _message("competing-next", "它后来恢复了", sequence=2),
        ]
    )
    ambiguous = competing.fragments[1]
    assert len(competing.fragments[0].objects) >= 2
    assert ambiguous.object_resolution == RESOLUTION_UNKNOWN
    assert ambiguous.object_id == "unknown"
    assert ambiguous.object_inherited_from_id is None
    assert ambiguous.object_evidence_refs == ()


def test_speaker_mentioned_subject_and_object_are_separate_slots():
    result = extract_context_fragments(
        [_message("roles", "张三说GPT失败了", speaker="speaker-a", sequence=1)]
    )
    fragment = result.fragments[0]

    assert fragment.speaker.role == "speaker"
    assert fragment.speaker_id == "speaker-a"
    assert fragment.subject.role == "subject"
    assert fragment.subject.surface_text == "张三"
    assert fragment.subject.resolution == RESOLUTION_EXPLICIT
    assert fragment.mentioned_persons
    assert all(person.role == "mentioned" for person in fragment.mentioned_persons)
    assert fragment.speaker.actor_id != fragment.subject.actor_id
    assert fragment.object_resolution == RESOLUTION_EXPLICIT
    assert fragment.object_id != fragment.subject.actor_id
    assert fragment.objects[0].resolution == RESOLUTION_EXPLICIT


def test_silence_is_unknown_and_never_a_resolution_signal():
    result = extract_context_fragments(
        [
            _message("failed", "GPT失败了", sequence=1),
            _message("silent", "", sequence=2),
        ]
    )
    silent = result.fragments[1]
    assert silent.is_silent is True
    assert silent.state == "unknown"
    assert silent.state_evidence == RESOLUTION_UNKNOWN
    assert silent.closure_reason == "unknown"
    assert silent.states == ()
    assert silent.information_value == "none"
    assert silent.event_completeness == "not_applicable"
    assert all(item.state != "resolved" for item in silent.states)


def test_same_segment_and_time_cannot_create_an_unsafe_relation():
    result = extract_context_fragments(
        [
            _message(
                "question",
                "GPT怎么恢复？",
                sequence=1,
                time_offset_seconds=0,
            ),
            _message(
                "unrelated",
                "明天选课安排已确定",
                sequence=2,
                time_offset_seconds=1,
            ),
        ],
        _segment(["question", "unrelated"], substantive_ids=["question", "unrelated"]),
    )

    # The only common facts here are segment membership and clock proximity;
    # neither may be promoted to answers/continues/elaborates.
    between = _between(result, "question", "unrelated")
    assert not any(
        item.relation in {LABEL_ANSWERS, LABEL_CONTINUES, LABEL_ELABORATES}
        for item in between
    )
    assert all(item.evidence_strength in {"weak", "none"} for item in between)


def test_relation_labels_are_canonical_and_have_typed_evidence():
    result = extract_context_fragments(
        [
            _message("q", "GPT怎么恢复？", sequence=1),
            _message("a", "已经恢复了", sequence=2),
        ]
    )
    assert result.relations
    for relation in result.relations:
        assert relation.relation in CANONICAL_RELATION_LABELS
        assert relation.source_message_ids == ("q", "a")
        assert relation.evidence_refs
        assert relation.provenance.get("time_is_weak_only") is True


def test_information_value_and_event_completeness_are_independent_axes():
    result = extract_context_fragments(
        [
            _message("opener", "你好", sequence=1),
            _message("substantive", "我说GPT失败了", sequence=2),
        ],
        _segment(
            ["opener", "substantive"],
            opener_ids=["opener"],
            substantive_ids=["substantive"],
        ),
    )
    opener, substantive = result.fragments
    assert opener.information_value == "low"
    assert opener.event_completeness == "not_applicable"
    assert substantive.information_value == "high"
    assert substantive.event_completeness == "sufficient"
