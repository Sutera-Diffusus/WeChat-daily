from copy import deepcopy

import pytest

from wechat_bridge.dialogue_segments import (
    ROLE_CONTEXT_ONLY,
    ROLE_CONVERSATION_OPENER,
    ROLE_SUBSTANTIVE,
    is_greeting_only,
    segment_dialogues,
)


def _message(message_id, text, offset, *, chat_id="CHAT_SYNTHETIC", speaker_id="PERSON_A", **extra):
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "account_id": "ACCOUNT_SYNTHETIC",
        "speaker_id": speaker_id,
        "content": text,
        "time_offset_seconds": offset,
        "sequence_in_chat": extra.pop("sequence_in_chat", offset),
        "message_type": extra.pop("message_type", "text"),
        **extra,
    }


def test_greeting_opener_stays_with_new_topic_and_is_not_evidence():
    messages = [
        _message("M1", "\u4f60\u597d", 0, speaker_id="PERSON_A"),
        _message("M2", "GPT \u53c8\u91cd\u7f6e\u4e86", 30, speaker_id="PERSON_B"),
    ]

    result = segment_dialogues(messages)

    assert result.message_roles == {
        "M1": ROLE_CONVERSATION_OPENER,
        "M2": ROLE_SUBSTANTIVE,
    }
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.message_ids == ("M1", "M2")
    assert segment.opener_message_ids == ("M1",)
    assert segment.context_message_ids == ("M1",)
    assert segment.topic_bearing_message_ids == ("M2",)
    assert segment.evidence_eligible_message_ids == ("M2",)


def test_mixed_greeting_is_kept_as_substantive_message():
    messages = [_message("M1", "\u4f60\u597d\uff0c\u8bf7\u770b\u4e00\u4e0b API \u8fd4\u56de\u5931\u8d25", 0)]

    result = segment_dialogues(messages)

    assert is_greeting_only(messages[0]["content"]) is False
    assert result.message_roles["M1"] == ROLE_SUBSTANTIVE
    assert result.segments[0].opener_message_ids == ()
    assert result.segments[0].topic_bearing_message_ids == ("M1",)
    assert result.segments[0].evidence_eligible_message_ids == ("M1",)
    assert result.messages[0]["content"] == messages[0]["content"]


def test_pure_social_run_is_context_only_and_does_not_create_event_evidence():
    messages = [
        _message("M1", "\u8f9b\u82e6\u4e86", 0),
        _message("M2", "\u6700\u8fd1\u600e\u4e48\u6837\uff1f", 15, speaker_id="PERSON_B"),
        _message("M3", "OK", 30, speaker_id="PERSON_A"),
    ]

    result = segment_dialogues(messages)

    assert result.message_roles["M1"] == ROLE_CONVERSATION_OPENER
    assert result.message_roles["M2"] == ROLE_CONTEXT_ONLY
    assert result.message_roles["M3"] == ROLE_CONTEXT_ONLY
    assert result.segments[0].topic_bearing_message_ids == ()
    assert result.segments[0].evidence_eligible_message_ids == ()


def test_long_gap_splits_unless_current_turn_explicitly_replies():
    split_messages = [
        _message("M1", "\u8ba8\u8bba API \u90e8\u7f72", 0),
        _message("M2", "\u4eca\u5929\u9009\u8bfe\u600e\u4e48\u529e", 901),
    ]
    split_result = segment_dialogues(split_messages)
    assert len(split_result.segments) == 2
    assert split_result.segments[1].boundary_before == "max_gap_exceeded"

    reply_messages = [
        _message("M1", "\u8ba8\u8bba API \u90e8\u7f72", 0),
        _message("M2", "\u8fd8\u662f\u6309\u4e0a\u9762\u7684\u65b9\u6848", 901, reply_to_message_id="M1"),
    ]
    reply_result = segment_dialogues(reply_messages)
    assert len(reply_result.segments) == 1
    assert reply_result.segments[0].boundary_before == "initial_segment"


def test_chat_and_speaker_context_are_preserved_and_input_is_not_mutated():
    messages = [
        _message("M2", "\u91cd\u7f6e\u5bc6\u7801", 30, speaker_id="PERSON_B"),
        _message("M1", "\u4f60\u597d", 0, speaker_id="PERSON_A"),
        _message("M3", "\u4ef7\u683c\u591a\u5c11", 60, chat_id="CHAT_OTHER", speaker_id="PERSON_C"),
    ]
    original = deepcopy(messages)

    result = segment_dialogues(messages)

    assert messages == original
    assert {message_id for segment in result.segments for message_id in segment.message_ids} == {
        "M1",
        "M2",
        "M3",
    }
    assert len(result.segments) == 2
    by_chat = {segment.chat_id: segment for segment in result.segments}
    assert by_chat["CHAT_SYNTHETIC"].message_ids == ("M1", "M2")
    assert result.message_roles["M1"] == ROLE_CONVERSATION_OPENER


def test_result_supports_mapping_and_is_deterministic_under_input_shuffle():
    messages = [
        _message("M1", "hello", 0),
        _message("M2", "inspect the API failure", 20, speaker_id="PERSON_B"),
        _message("M3", "\u6362\u4e2a\u8bdd\u9898\uff0c\u660e\u5929\u9009\u8bfe", 500, speaker_id="PERSON_A"),
    ]
    first = segment_dialogues(messages)
    second = segment_dialogues(list(reversed(messages)))

    assert first["message_roles"] == second["message_roles"]
    assert [segment.to_dict() for segment in first.segments] == [
        segment.to_dict() for segment in second.segments
    ]
    assert first.to_dict()["segments"][0]["message_ids"] == ["M1", "M2"]


def test_invalid_inputs_fail_without_silent_data_loss():
    with pytest.raises(ValueError, match="duplicate message_id"):
        segment_dialogues([_message("M1", "hello", 0), _message("M1", "hi", 1)])
    with pytest.raises(ValueError, match="non-negative"):
        segment_dialogues([], max_gap_seconds=-1)
