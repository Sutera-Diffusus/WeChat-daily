"""Synthetic contract tests for the local ContextPacket v1 builder.

All inputs below are invented public mappings.  The packet layer is intentionally
tested as a reversible context projection, not as event/topic resolution.
"""

from __future__ import annotations

import json

from wechat_bridge.context_packets import (
    CONTEXT_PACKET_VERSION,
    ContextPacket,
    ContextPacketCache,
    build_context_packets,
)
from wechat_bridge.dialogue_bundle import BundleFragment


def _message(
    message_id: str,
    text: str,
    sequence: int,
    *,
    account: str = "account-synthetic",
    chat: str = "chat-synthetic",
    speaker: str = "speaker-synthetic",
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "message_id": message_id,
        "account_id": account,
        "chat_id": chat,
        "speaker_id": speaker,
        "content": text,
        "message_type": "text",
        "sequence_in_chat": sequence,
        "time_offset_seconds": float(sequence),
        "dialogue_segment_id": "segment-synthetic",
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
    people: tuple[str, ...] = (),
    object_id: str = "unknown",
    object_resolution: str = "unknown",
    state: str = "unknown",
    state_evidence: str = "unknown",
    intent: str = "statement",
    fragment_type: str = "statement",
    actions: tuple[str, ...] = (),
    role: str = "substantive",
    segment: str | None = "segment-synthetic",
    is_opener: bool = False,
    is_silent: bool = False,
    reply_to: str | None = None,
    inherited_from: str | None = None,
) -> BundleFragment:
    return BundleFragment(
        fragment_id=fragment_id,
        message_id=message_id,
        account_id=account,
        chat_id=chat,
        segment_id=segment,
        text=text,
        span_start=0,
        span_end=len(text),
        role=role,
        fragment_type=fragment_type,
        speaker_id=speaker,
        mentioned_person_ids=people,
        subject_id=subject,
        subject_type="person" if subject != "unknown" else "unknown",
        object_id=object_id,
        object_resolution=object_resolution,
        object_inherited_from_id=inherited_from,
        state=state,
        state_evidence=state_evidence,
        closure_reason=state if state in {"resolved", "failed", "cancelled"} else "unknown",
        intent=intent,
        claim_role="question" if intent == "question" else "fact",
        modality="unknown",
        actions=actions,
        is_opener=is_opener,
        is_silent=is_silent,
        reply_to_message_id=reply_to,
        source="synthetic",
    )


def _run(fragments: list[BundleFragment], messages: list[dict[str, object]] | None = None, **kwargs: object):
    if messages is None:
        messages = [
            _message(fragment.message_id, fragment.text, index + 1, account=fragment.account_id, chat=fragment.chat_id, speaker=fragment.speaker_id)
            for index, fragment in enumerate(fragments)
        ]
    return build_context_packets(messages, fragments=fragments, **kwargs)


def test_packet_has_authoritative_facts_and_fixed_dynamic_hashes_without_final_layers():
    fragment = _fragment("f-basic", "m-basic", "synthetic body", object_id="object-a", object_resolution="explicit")
    result = _run([fragment])
    packet = result.packets[0]

    assert packet.packet_version == CONTEXT_PACKET_VERSION
    assert packet.authoritative_facts[0]["speaker_id"] == "speaker-synthetic"
    assert packet.primary_fragments[0]["text_redacted"] == "synthetic body"
    assert packet.fixed_hash and packet.dynamic_hash and packet.packet_hash and packet.cache_key
    assert packet.fixed_part["fixed_part_version"]
    assert packet.dynamic_part["dynamic_part_version"]
    payload = packet.to_dict()
    assert payload["fixed_hash"] == packet.fixed_hash
    assert payload["dynamic_hash"] == packet.dynamic_hash
    assert "same_event" not in payload
    assert "final_topic" not in payload
    assert "events" not in payload
    assert "event_completeness_candidate" not in payload["primary_fragments"][0]


def test_one_fragment_can_appear_in_multiple_scale_packets_and_packet_result_is_iterable():
    first = _fragment("f-one", "m-one", "first", object_id="object-a", object_resolution="explicit")
    second = _fragment("f-two", "m-two", "second", object_id="object-a", object_resolution="explicit")
    result = _run([first, second])

    occurrences = sum("f-one" in packet.fragment_ids for packet in result)
    assert occurrences >= 2
    assert result.context_packets == result.packets
    assert result[0] == result.packets[0]


def test_question_answer_candidate_keeps_object_overlap_and_is_not_final_decision():
    question = _fragment(
        "f-question",
        "m-question",
        "question",
        object_id="object-a",
        object_resolution="explicit",
        intent="question",
        fragment_type="question",
    )
    answer = _fragment(
        "f-answer",
        "m-answer",
        "answer",
        speaker="speaker-other",
        object_id="object-a",
        object_resolution="explicit",
        intent="answer",
        fragment_type="answer",
        state="ongoing",
        state_evidence="explicit",
    )
    result = _run([question, answer])

    assert any(packet.candidate_qa_links for packet in result)
    assert any(packet.candidate_object_history for packet in result)
    candidate = next(packet.candidate_qa_links[0] for packet in result if packet.candidate_qa_links)
    assert candidate["candidate_only"] is True
    assert candidate["semantic_support"]
    assert candidate["time_is_weak_only"] is True


def test_person_object_and_state_history_candidates_are_separate_reversible_views():
    first = _fragment(
        "f-history-a",
        "m-history-a",
        "history a",
        subject="person-a",
        people=("person-b",),
        object_id="object-a",
        object_resolution="explicit",
        state="planned",
        state_evidence="explicit",
        actions=("check",),
    )
    second = _fragment(
        "f-history-b",
        "m-history-b",
        "history b",
        subject="person-a",
        people=("person-b",),
        object_id="object-a",
        object_resolution="explicit",
        state="ongoing",
        state_evidence="explicit",
        actions=("check",),
    )
    result = _run([first, second])

    assert any(packet.candidate_person_history for packet in result)
    assert any(packet.candidate_object_history for packet in result)
    assert any(packet.candidate_state_history for packet in result)
    assert all(item.get("candidate_only") is True for packet in result for item in packet.candidate_state_history)


def test_cross_chat_is_hard_boundary_even_with_shared_object_people_time_and_segment():
    left = _fragment("f-chat-a", "m-chat-a", "left", chat="chat-a", object_id="object-shared", object_resolution="explicit", people=("person-shared",))
    right = _fragment("f-chat-b", "m-chat-b", "right", chat="chat-b", object_id="object-shared", object_resolution="explicit", people=("person-shared",))
    result = _run(
        [left, right],
        [
            _message("m-chat-a", "left", 1, chat="chat-a"),
            _message("m-chat-b", "right", 2, chat="chat-b"),
        ],
    )

    for packet in result:
        assert packet.chat_id in {"chat-a", "chat-b"}
        assert all(item["chat_id"] == packet.chat_id for item in packet.primary_fragments)
        assert all(item["chat_id"] == packet.chat_id for item in packet.adjacent_context)
        assert not any(
            {item.get("left_message_id"), item.get("right_message_id")} == {"m-chat-a", "m-chat-b"}
            for values in (
                packet.candidate_qa_links,
                packet.candidate_person_history,
                packet.candidate_object_history,
                packet.candidate_state_history,
            )
            for item in values
        )


def test_time_and_same_segment_are_context_reasons_not_sufficient_candidates():
    left = _fragment("f-weak-a", "m-weak-a", "left", object_id="unknown", segment="segment-same")
    right = _fragment("f-weak-b", "m-weak-b", "right", object_id="unknown", segment="segment-same")
    result = _run(
        [left, right],
        [
            _message("m-weak-a", "left", 1),
            _message("m-weak-b", "right", 2),
        ],
    )

    assert all(not packet.candidate_qa_links for packet in result)
    assert all(not packet.candidate_person_history for packet in result)
    assert all(not packet.candidate_object_history for packet in result)
    assert any("same_segment_weak" in packet.candidate_reason for packet in result)
    assert any("time_proximity_weak" in packet.candidate_reason for packet in result)
    assert any("time_weak_only" in packet.uncertainties for packet in result)


def test_greeting_ack_and_silence_are_retained_as_context_but_never_semantic_links():
    substantive = _fragment("f-substantive", "m-substantive", "substantive", object_id="object-a", object_resolution="explicit")
    opener = _fragment("f-opener", "m-opener", "你好", role="conversation_opener", fragment_type="conversation_opener", is_opener=True)
    ack = _fragment("f-ack", "m-ack", "收到", role="context_only", fragment_type="acknowledgement")
    silent = _fragment("f-silent", "m-silent", "", is_silent=True, fragment_type="unknown")
    result = _run(
        [substantive, opener, ack, silent],
        [
            _message("m-substantive", "substantive", 1),
            _message("m-opener", "你好", 2),
            _message("m-ack", "收到", 3),
            _message("m-silent", "", 4, message_type="image"),
        ],
    )

    seen_primary = {item["fragment_id"] for packet in result for item in packet.primary_fragments}
    assert {"f-opener", "f-ack", "f-silent"} <= seen_primary
    for packet in result:
        for values in (
            packet.candidate_qa_links,
            packet.candidate_person_history,
            packet.candidate_object_history,
            packet.candidate_state_history,
        ):
            assert not any(item["left_fragment_id"] in {"f-opener", "f-ack", "f-silent"} or item["right_fragment_id"] in {"f-opener", "f-ack", "f-silent"} for item in values)


def test_unknown_scope_only_allows_same_message_and_keeps_unknown_uncertainty():
    first = _fragment("f-unknown-a", "m-unknown-a", "first", account="unknown", chat="unknown", object_id="object-a", object_resolution="explicit")
    second = _fragment("f-unknown-b", "m-unknown-b", "second", account="unknown", chat="unknown", object_id="object-a", object_resolution="explicit")
    result = _run(
        [first, second],
        [
            {"message_id": "m-unknown-a", "content": "first", "speaker_id": "speaker-a"},
            {"message_id": "m-unknown-b", "content": "second", "speaker_id": "speaker-b"},
        ],
    )
    assert all(not packet.candidate_object_history for packet in result)
    assert all("scope_unknown" in packet.uncertainties for packet in result)


def test_authoritative_registry_projection_does_not_copy_private_or_mutable_fields():
    fragment = _fragment("f-public", "m-public", "public", object_id="object-a", object_resolution="explicit")
    message = _message(
        "m-public",
        "public",
        1,
        raw_text="private should not cross registry",
        private_identity="private should not cross registry",
    )
    result = _run([fragment], [message])
    message["content"] = "mutated after registration"
    payload = result.to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert "private should not cross registry" not in text
    assert "private_identity" not in text
    assert "raw_message" not in payload["packets"][0]["authoritative_facts"][0]
    assert result.packets[0].authoritative_facts[0]["content_hash"]


def test_content_addressed_cache_replays_and_persists_without_changing_packet_hash():
    fragment = _fragment("f-cache", "m-cache", "cache", object_id="object-a", object_resolution="explicit")
    cache = ContextPacketCache()
    first = _run([fragment], cache=cache)
    second = _run([fragment], cache=cache)

    assert first.cache_misses == len(first.packets)
    assert first.cache_hits == 0
    assert second.cache_hits == len(second.packets)
    assert second.cache_misses == 0
    assert [item.packet_hash for item in first] == [item.packet_hash for item in second]
    restored = ContextPacketCache.loads(cache.dumps())
    replay = _run([fragment], cache=restored)
    assert replay.cache_hits == len(replay.packets)
    assert restored.to_dict()["entry_count"] == len(cache)


def test_fixed_and_dynamic_parts_are_separately_hashable_and_version_changes_identity():
    first = _fragment("f-fixed", "m-fixed", "fixed", object_id="object-a", object_resolution="explicit")
    second = _fragment("f-neighbour", "m-neighbour", "neighbour", object_id="unknown")
    one = _run([first])
    two = _run([first, second])
    one_anchor = next(item for item in one if item.anchor_fragment_id == "f-fixed" and item.anchor_bundle_id.endswith(""))
    two_anchor = next(item for item in two if item.anchor_fragment_id == "f-fixed")
    assert one_anchor.fixed_hash == two_anchor.fixed_hash
    assert one_anchor.packet_hash != two_anchor.packet_hash
    changed = _run([first], packet_version="context_packet_v1_alt")
    assert changed.packets[0].packet_hash != one.packets[0].packet_hash


def test_terminal_input_remains_an_open_candidate_projection():
    fragment = _fragment(
        "f-terminal-input",
        "m-terminal-input",
        "terminal candidate",
        object_id="object-a",
        object_resolution="explicit",
        state="resolved",
        state_evidence="explicit",
    )
    result = _run([fragment])
    assert result.packets
    assert all(item["open_boundary"] is True for packet in result for item in packet.open_thread_candidates)
    assert all("closed" not in item for packet in result for item in packet.open_thread_candidates)
    assert all("same_event" not in packet.to_dict() for packet in result)

