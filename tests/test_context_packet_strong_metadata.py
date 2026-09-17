"""Small synthetic checks for upstream typed-strata materialization."""

from __future__ import annotations

from wechat_bridge.context_packets import build_context_packets
from wechat_bridge.dialogue_bundle import BundleFragment
from wechat_bridge.selection_strata import build_canonical_strata_metadata


def _message(message_id: str, text: str, sequence: int) -> dict[str, object]:
    return {
        "message_id": message_id,
        "account_id": "account-strong",
        "chat_id": "chat-strong",
        "speaker_id": "speaker-strong",
        "content": text,
        "message_type": "text",
        "sequence_in_chat": sequence,
        "split": "development",
    }


def _fragment(fragment_id: str, message_id: str, text: str, **extra: object) -> BundleFragment:
    value: dict[str, object] = {
        "fragment_id": fragment_id,
        "message_id": message_id,
        "account_id": "account-strong",
        "chat_id": "chat-strong",
        "segment_id": "segment-strong",
        "text": text,
        "span_start": 0,
        "span_end": len(text),
        "role": "substantive",
        "fragment_type": "statement",
        "speaker_id": "speaker-strong",
        "subject_id": "person-strong",
        "subject_type": "person",
        "mentioned_person_ids": ("person-strong",),
        "object_id": "object-strong",
        "object_resolution": "explicit",
        "object_evidence_refs": ({"type": "fragment", "id": fragment_id},),
        "state": "ongoing",
        "state_evidence": "explicit",
        "actions": ("review",),
        "intent": "statement",
    }
    value.update(extra)
    return BundleFragment(**value)


def test_typed_history_and_no_reply_are_materialized_only_with_local_evidence():
    left = _fragment("f-left", "m-left", "question", intent="question", fragment_type="question")
    right = _fragment("f-right", "m-right", "continuation", speaker_id="speaker-other")
    result = build_context_packets([_message("m-left", "question", 1), _message("m-right", "continuation", 2)], fragments=[left, right])
    packet = next(item for item in result if item.candidate_person_history)

    assert packet.candidate_person_history[0]["materialized_relation"] is True
    assert packet.candidate_object_history[0]["typed_evidence_fields"]
    assert packet.candidate_state_history[0]["scoped_evidence_ref"]["scope"] == {
        "account_id": "account-strong",
        "chat_id": "chat-strong",
    }
    assert packet.dynamic_part["pronoun_person_object_state"]
    reply = packet.dynamic_part["reply_status"]
    assert reply and reply[0]["continuation_signal_count"] >= 2
    strata = build_canonical_strata_metadata([packet.to_dict()])
    assert "pronoun_person_object_state" in strata["available_strata"]
    assert "no_reply" in strata["available_strata"]


def test_greeting_requires_typed_topic_followup_and_ack_only_stays_missing():
    opener = _fragment(
        "f-opener",
        "m-opener",
        "hello",
        role="conversation_opener",
        fragment_type="conversation_opener",
        is_opener=True,
        intent="greeting",
        subject_id="unknown",
        mentioned_person_ids=(),
        object_id="unknown",
        object_resolution="unknown",
        object_evidence_refs=(),
        state="unknown",
        state_evidence="unknown",
        actions=(),
    )
    topic = _fragment("f-topic", "m-topic", "review object", object_id="topic-object")
    ack = _fragment(
        "f-ack",
        "m-ack",
        "received",
        role="context_only",
        fragment_type="acknowledgement",
        subject_id="unknown",
        mentioned_person_ids=(),
        object_id="unknown",
        object_resolution="unknown",
        object_evidence_refs=(),
        state="unknown",
        state_evidence="unknown",
        actions=(),
        intent="acknowledgement",
    )
    messages = [_message("m-opener", "hello", 1), _message("m-topic", "review object", 2), _message("m-ack", "received", 3)]
    result = build_context_packets(messages, fragments=[opener, topic, ack])
    assert any(packet.dynamic_part["message_metadata"] for packet in result)
    assert all(not packet.dynamic_part["message_metadata"] for packet in build_context_packets([_message("m-ack", "received", 1)], fragments=[ack]))


def test_topic_transition_needs_explicit_marker_and_two_topic_bearing_endpoints():
    first = _fragment("f-first", "m-first", "first object")
    second = _fragment("f-second", "m-second", "new object", object_id="object-new", topic_shift=True)
    messages = [_message("m-first", "first object", 1), _message("m-second", "new object", 2)]
    result = build_context_packets(messages, fragments=[first, second])
    assert any(packet.dynamic_part["topic_transitions"] for packet in result)

    non_topic = _fragment(
        "f-non-topic",
        "m-non-topic",
        "boundary only",
        subject_id="unknown",
        mentioned_person_ids=(),
        object_id="unknown",
        object_resolution="unknown",
        object_evidence_refs=(),
        state="unknown",
        state_evidence="unknown",
        actions=(),
        topic_shift=True,
    )
    assert all(
        not packet.dynamic_part["topic_transitions"]
        for packet in build_context_packets([_message("m-non-topic", "boundary only", 1)], fragments=[non_topic])
    )
