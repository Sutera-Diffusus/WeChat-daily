"""Synthetic edge-case regressions for greeting-aware dialogue segmentation.

This file is intentionally separate from the implementation smoke tests in
``test_dialogue_segments.py``.  It exercises the user-facing boundary that a
social opener must preserve the following topic, while opener/context rows
remain out of event evidence.  No private pilot data or legacy analysis path
is used.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from wechat_bridge.dialogue_segments import segment_dialogues

from synthetic_dialogue_fixtures import (
    context_only_around_topic,
    greeting_then_other_person_topic,
    greeting_then_same_person_question,
    greeting_with_substantive_content,
    long_gap,
    pure_greeting,
    topic_turn,
)


ROLE_OPENER = "conversation_opener"
ROLE_CONTEXT = "context_only"
ROLE_SUBSTANTIVE = "substantive"


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _roles(result: Any) -> dict[str, str]:
    return dict(_field(result, "message_roles"))


def _segments(result: Any) -> Sequence[Any]:
    return tuple(_field(result, "segments"))


def _segment_ids(segment: Any, name: str) -> list[str]:
    return [str(value) for value in _field(segment, name)]


def _union_segment_ids(result: Any, name: str) -> set[str]:
    return {
        message_id
        for segment in _segments(result)
        for message_id in _segment_ids(segment, name)
    }


def _only_segment(result: Any) -> Any:
    segments = _segments(result)
    assert len(segments) == 1
    return segments[0]


def _assert_message_accounting(result: Any, expected_ids: Iterable[str]) -> None:
    expected = list(expected_ids)
    actual = [
        message_id
        for segment in _segments(result)
        for message_id in _segment_ids(segment, "message_ids")
    ]
    assert actual == expected
    assert set(_roles(result)) == set(expected)


def test_pure_greetings_open_context_without_topic_or_evidence() -> None:
    messages = pure_greeting()
    result = segment_dialogues(messages)

    _assert_message_accounting(result, [item["message_id"] for item in messages])
    roles = _roles(result)
    assert roles["greet-1"] == ROLE_OPENER
    assert set(roles.values()) <= {ROLE_OPENER, ROLE_CONTEXT}
    assert ROLE_SUBSTANTIVE not in roles.values()
    assert _union_segment_ids(result, "topic_bearing_message_ids") == set()
    assert _union_segment_ids(result, "evidence_eligible_message_ids") == set()


def test_greeting_then_same_person_question_stays_in_one_segment() -> None:
    messages = greeting_then_same_person_question()
    result = segment_dialogues(messages)

    _assert_message_accounting(result, [item["message_id"] for item in messages])
    assert len(_segments(result)) == 1
    roles = _roles(result)
    assert roles == {
        "same-opener": ROLE_OPENER,
        "same-question": ROLE_SUBSTANTIVE,
    }
    segment = _only_segment(result)
    assert _segment_ids(segment, "topic_bearing_message_ids") == ["same-question"]
    assert _segment_ids(segment, "evidence_eligible_message_ids") == ["same-question"]


def test_greeting_then_other_speaker_topic_is_not_dropped() -> None:
    messages = greeting_then_other_person_topic()
    result = segment_dialogues(messages)

    _assert_message_accounting(result, [item["message_id"] for item in messages])
    assert len(_segments(result)) == 1
    roles = _roles(result)
    assert roles["other-opener"] == ROLE_OPENER
    assert roles["other-topic"] == ROLE_SUBSTANTIVE
    segment = _only_segment(result)
    assert _segment_ids(segment, "topic_bearing_message_ids") == ["other-topic"]
    assert _segment_ids(segment, "evidence_eligible_message_ids") == ["other-topic"]


def test_greeting_plus_substantive_content_in_one_message_is_not_filtered() -> None:
    messages = greeting_with_substantive_content()
    result = segment_dialogues(messages)

    _assert_message_accounting(result, ["mixed-opener-topic"])
    assert _roles(result) == {"mixed-opener-topic": ROLE_SUBSTANTIVE}
    segment = _only_segment(result)
    assert _segment_ids(segment, "topic_bearing_message_ids") == ["mixed-opener-topic"]
    assert _segment_ids(segment, "evidence_eligible_message_ids") == ["mixed-opener-topic"]


def test_long_time_gap_starts_a_new_segment_and_does_not_leak_context() -> None:
    messages = long_gap()
    result = segment_dialogues(messages, max_gap_seconds=600)

    _assert_message_accounting(result, [item["message_id"] for item in messages])
    segments = _segments(result)
    assert len(segments) == 2
    assert _segment_ids(segments[0], "message_ids") == ["gap-opener", "gap-first-topic"]
    assert _segment_ids(segments[1], "message_ids") == ["gap-second-topic"]
    assert _segment_ids(segments[0], "topic_bearing_message_ids") == ["gap-first-topic"]
    assert _segment_ids(segments[1], "topic_bearing_message_ids") == ["gap-second-topic"]
    assert _segment_ids(segments[0], "evidence_eligible_message_ids") == ["gap-first-topic"]
    assert _segment_ids(segments[1], "evidence_eligible_message_ids") == ["gap-second-topic"]


def test_clear_topic_turn_creates_a_new_segment() -> None:
    messages = topic_turn()
    result = segment_dialogues(messages, max_gap_seconds=600)

    _assert_message_accounting(result, [item["message_id"] for item in messages])
    segments = _segments(result)
    assert len(segments) == 2
    assert _segment_ids(segments[0], "message_ids") == ["turn-opener", "turn-first-topic"]
    assert _segment_ids(segments[1], "message_ids") == ["turn-second-topic"]
    assert _segment_ids(segments[0], "topic_bearing_message_ids") == ["turn-first-topic"]
    assert _segment_ids(segments[1], "topic_bearing_message_ids") == ["turn-second-topic"]
    assert _roles(result)["turn-opener"] == ROLE_OPENER
    assert _roles(result)["turn-first-topic"] == ROLE_SUBSTANTIVE
    assert _roles(result)["turn-second-topic"] == ROLE_SUBSTANTIVE


def test_context_messages_are_retained_but_never_event_evidence() -> None:
    messages = context_only_around_topic()
    result = segment_dialogues(messages)

    _assert_message_accounting(result, [item["message_id"] for item in messages])
    assert len(_segments(result)) == 1
    roles = _roles(result)
    assert roles == {
        "context-opener": ROLE_OPENER,
        "context-topic": ROLE_SUBSTANTIVE,
        "context-reaction": ROLE_CONTEXT,
    }
    segment = _only_segment(result)
    assert _segment_ids(segment, "topic_bearing_message_ids") == ["context-topic"]
    assert _segment_ids(segment, "evidence_eligible_message_ids") == ["context-topic"]
    assert "context-opener" not in _union_segment_ids(result, "evidence_eligible_message_ids")
    assert "context-reaction" not in _union_segment_ids(result, "evidence_eligible_message_ids")
