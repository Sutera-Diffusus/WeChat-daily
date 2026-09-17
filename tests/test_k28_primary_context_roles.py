"""Synthetic K28 regressions for authoritative primary/context roles.

These tests use only in-memory public mappings.  They do not load a provider,
private message storage or frozen/development artifacts.
"""

from __future__ import annotations

from wechat_bridge.context_packets import build_context_packets
from wechat_bridge.compact_context_packets import (
    compact_context_packets,
    materialize_stage_packet,
)
from wechat_bridge.dialogue_bundle import build_dialogue_bundles
from wechat_bridge.dialogue_segments import is_context_only_text
from wechat_bridge.contextual_fragments import extract_context_fragments
from wechat_bridge.linear_stage_packets import (
    build_linear_stage_packets,
    materialize_stage_a,
    recover_linear_packet,
)


def _message(message_id: str, text: str, sequence: int, *, message_type: str = "text") -> dict[str, object]:
    return {
        "message_id": message_id,
        "account_id": "k28-account",
        "chat_id": "k28-chat",
        "speaker_id": "k28-speaker",
        "content": text,
        "message_type": message_type,
        "sequence_in_chat": sequence,
        "time_offset_seconds": float(sequence),
        "split": "synthetic",
    }


def _packet_primary_ids(result: object) -> set[str]:
    packets = getattr(result, "packets")
    return {
        str(row["message_id"])
        for packet in packets
        for row in packet.primary_fragments
    }


def _packet_adjacent_ids(result: object) -> set[str]:
    packets = getattr(result, "packets")
    return {
        str(row["message_id"])
        for packet in packets
        for row in packet.adjacent_context
    }


def _compact_provider_projection(result: object) -> tuple[set[str], set[str], set[str]]:
    """Return semantic primary/context/all IDs from the K6 provider view.

    K2 ``primary_fragments`` is the lossless retained view.  This helper
    deliberately crosses the compact materialization boundary before making
    role assertions, which is the boundary where provider-facing roles are
    allowed to differ from K2 retention.
    """

    compact = compact_context_packets(
        tuple(getattr(result, "packets")),
        max_input_token_proxy=20_000,
        max_messages=100,
        max_candidate_rows=100,
        max_evidence_refs=100,
    )
    primary: set[str] = set()
    context: set[str] = set()
    all_ids: set[str] = set()
    for packet in compact.packets:
        row = materialize_stage_packet(compact, packet, allow_over_capacity=True)
        primary.update(str(value) for value in row.get("primary_message_ids", ()) if value)
        context.update(str(value) for value in row.get("context_message_ids", ()) if value)
        all_ids.update(str(value) for value in row.get("message_ids", ()) if value)
    return primary, context, all_ids


def test_pure_confirmation_is_retained_in_k2_but_has_no_provider_primary() -> None:
    message = _message("confirm", "确认", 1)
    assert is_context_only_text(message["content"]) is True

    bundles = build_dialogue_bundles([message])
    assert bundles.fragments[0].role == "context_only"
    result = build_context_packets(dialogue_result=bundles)
    # K2 is the reversible source packet: a pure context turn is retained in
    # its authoritative primary-fragment view so recovery never loses it.
    assert result.packets
    assert _packet_primary_ids(result) == {"confirm"}
    assert result.dialogue_result.fragments[0].message_id == "confirm"
    provider_primary, provider_context, provider_all = _compact_provider_projection(result)
    assert provider_primary == set()
    assert "confirm" in provider_context
    assert "confirm" in provider_all


def test_confirmation_plus_new_topic_stays_substantive_and_keeps_ack_context() -> None:
    messages = [
        _message("confirm", "确认", 1),
        _message("topic", "确认项目上线状态", 2),
    ]
    result = build_context_packets(messages)

    # K2 keeps both source fragments, including the acknowledgement.
    assert _packet_primary_ids(result) == {"confirm", "topic"}
    assert "confirm" in _packet_adjacent_ids(result)
    roles = {fragment.message_id: fragment.role for fragment in result.dialogue_result.fragments}
    assert roles == {"confirm": "context_only", "topic": "substantive"}
    provider_primary, provider_context, provider_all = _compact_provider_projection(result)
    assert provider_primary == {"topic"}
    assert "confirm" in provider_context
    assert {"confirm", "topic"} <= provider_all


def test_greeting_plus_new_question_in_one_message_is_not_filtered() -> None:
    message = _message("mixed", "你好，接口什么时候恢复？", 1)
    result = build_context_packets([message])

    assert _packet_primary_ids(result) == {"mixed"}
    provider_primary, provider_context, provider_all = _compact_provider_projection(result)
    assert provider_primary == {"mixed"}
    assert "mixed" not in provider_context
    assert "mixed" in provider_all
    assert result.dialogue_result.fragments[0].role == "substantive"
    extracted = extract_context_fragments([message])
    assert [item.role for item in extracted.fragments] == ["conversation_opener", "substantive"]


def test_greeting_then_new_topic_keeps_greeting_as_adjacent_context() -> None:
    messages = [
        _message("greeting", "你好", 1),
        _message("shift", "换个话题，明天选课怎么办？", 2),
    ]
    result = build_context_packets(messages)

    assert _packet_primary_ids(result) == {"greeting", "shift"}
    assert "greeting" in _packet_adjacent_ids(result)
    provider_primary, provider_context, provider_all = _compact_provider_projection(result)
    assert provider_primary == {"shift"}
    assert "greeting" in provider_context
    assert {"greeting", "shift"} <= provider_all


def test_media_placeholder_is_context_and_does_not_displace_following_topic() -> None:
    messages = [
        _message("media", "", 1, message_type="image"),
        _message("topic", "项目状态已经恢复", 2),
    ]
    result = build_context_packets(messages)

    assert _packet_primary_ids(result) == {"media", "topic"}
    assert "media" in _packet_adjacent_ids(result)
    provider_primary, provider_context, provider_all = _compact_provider_projection(result)
    assert provider_primary == {"topic"}
    assert "media" in provider_context
    assert {"media", "topic"} <= provider_all


def test_linear_root_moves_pure_confirmation_but_preserves_mixed_and_unknown_rows() -> None:
    source = {
        "packet_id": "k28-linear",
        "account_id": "k28-account",
        "chat_id": "k28-chat",
        "primary_fragments": [
            {"message_id": "confirm", "account_id": "k28-account", "chat_id": "k28-chat", "content": "确认", "role": "substantive"},
            {"message_id": "mixed", "account_id": "k28-account", "chat_id": "k28-chat", "content": "确认项目上线状态", "role": "context_only", "fragment_type": "acknowledgement"},
            {"message_id": "unknown", "account_id": "k28-account", "chat_id": "k28-chat"},
        ],
        "adjacent_context": [],
    }
    store = build_linear_stage_packets((source,))
    root = store.roots[0]
    recovered = recover_linear_packet(store, root["root_id"])

    assert [row["message_id"] for row in recovered["primary_fragments"]] == ["mixed", "unknown"]
    assert [row["message_id"] for row in recovered["adjacent_context"]] == ["confirm"]
    assert recovered["adjacent_context"][0]["role"] == "context_only"
    stage_a = materialize_stage_a(store, root["page_refs"][0])
    stage_a_roles = {
        str(row["message_id"]): set(row.get("roles", ()))
        for row in stage_a["messages"]
    }
    assert stage_a_roles["confirm"] == {"adjacent"}
    assert stage_a_roles["mixed"] == {"primary"}
    assert stage_a_roles["unknown"] == {"primary"}
