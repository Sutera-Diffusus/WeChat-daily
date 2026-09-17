"""Synthetic smoke tests for the K5 development material runner.

These tests do not open the real development/private split, frozen data, or a
provider.  They only prove the input guard, K2 builder reuse, deterministic
packet selection, and the body-free projection contract on a tiny fixture.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from wechat_bridge.context_packet_development_runner import (
    OUTPUT_FILENAMES,
    SurfaceSignals,
    _assert_body_free,
    _materialize_selection_cues,
    run_development_context_packet_material,
)
from wechat_bridge.context_packets import ContextPacketCache, build_context_packets
from wechat_bridge.contextual_bundle_pipeline_runner import _public_pipeline_messages
from wechat_bridge.dialogue_segments import ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER, ROLE_SUBSTANTIVE


def _write_synthetic_input(root: Path, count: int = 20) -> Path:
    root.mkdir(parents=True)
    rows = []
    texts = (
        "你好，最近怎么样？",
        "继续说一下 Linux 注册流程。",
        "邮箱还是收不到验证码。",
        "这个账号昨天又失败了。",
        "换个话题：中转站是否划算？",
        "我先记录一下，稍后再看。",
    )
    for index in range(count):
        rows.append(
            {
                "message_id": f"synthetic-{index:03d}",
                "account_id": "account-synthetic",
                "chat_id": f"chat-{index % 2}",
                "chat_type": "private",
                "speaker_id": f"person-{index % 3}",
                "direction": "incoming" if index % 2 else "outgoing",
                "message_type": "image" if index == count - 1 else "text",
                "content": "" if index == count - 1 else texts[index % len(texts)],
                "sequence_in_chat": index,
                "time_offset_seconds": index * 90,
                "dialogue_segment_id": f"segment-{index // 4}",
                "reply_to_message_id": None,
                "split": "development",
                "local_day": "2026-08-25",
            }
        )
    message_path = root / "messages.private.jsonl"
    raw = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    message_path.write_bytes(raw)
    manifest = {
        "artifact": "synthetic-p014-development",
        "split": "development",
        "status": "development",
        "local_day": "2026-08-25",
        "split_version": "wechat-2026-08-25-p014-evaluation-split-v1",
        "file_sha256": {"messages.private.jsonl": hashlib.sha256(raw).hexdigest()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return message_path


def test_k2_builder_reuse_is_local_and_replayable() -> None:
    rows = [
        {
            "message_id": "m1",
            "account_id": "a1",
            "chat_id": "c1",
            "speaker_id": "s1",
            "direction": "outgoing",
            "message_type": "text",
            "content": "你好，继续看 Linux。",
            "sequence_in_chat": 1,
            "time_offset_seconds": 1,
            "dialogue_segment_id": "d1",
            "split": "development",
            "local_day": "2026-08-25",
        },
        {
            "message_id": "m2",
            "account_id": "a1",
            "chat_id": "c1",
            "speaker_id": "s2",
            "direction": "incoming",
            "message_type": "text",
            "content": "邮箱收不到验证码？",
            "sequence_in_chat": 2,
            "time_offset_seconds": 2,
            "dialogue_segment_id": "d1",
            "split": "development",
            "local_day": "2026-08-25",
        },
    ]
    cache = ContextPacketCache()
    first = build_context_packets(
        _public_pipeline_messages(rows),
        window_size=8,
        max_candidates=3,
        max_packets=16,
        cache=cache,
    )
    replay = build_context_packets(
        _public_pipeline_messages(rows),
        window_size=8,
        max_candidates=3,
        max_packets=16,
        cache=cache,
    )
    assert first.packets
    assert len(first.packets) == len(replay.packets)
    assert [packet.packet_hash for packet in first.packets] == [packet.packet_hash for packet in replay.packets]
    assert replay.cache_hits >= first.cache_misses
    assert replay.cache_misses == 0


def test_synthetic_runner_writes_private_packets_and_body_free_projection(tmp_path: Path) -> None:
    input_root = tmp_path / "p014" / "development"
    _write_synthetic_input(input_root)
    output_root = tmp_path / "context_packet_development_v1"

    result = run_development_context_packet_material(
        input_root,
        output_root,
        selected_packet_count=4,
    )

    assert result.packet_count > 0
    assert result.selected_packet_count == 4
    assert result.manifest["development_input_read"] is True
    assert result.manifest["provider_calls"] == 0
    assert result.manifest["frozen_read"] is False
    assert result.aggregate["material_metrics"]["selected_packet_count"] == 4
    assert result.aggregate["material_metrics"]["activation_cues"]["coverage_rate"] == 1.0

    for key in ("manifest", "aggregate", "cost"):
        value = json.loads((output_root / OUTPUT_FILENAMES[key]).read_text(encoding="utf-8"))
        _assert_body_free(value, label=key)
    for key in ("errors", "selection_map", "audit_queue"):
        rows = [json.loads(line) for line in (output_root / OUTPUT_FILENAMES[key]).read_text(encoding="utf-8").splitlines() if line.strip()]
        _assert_body_free(rows, label=key)

    packet_rows = [json.loads(line) for line in (output_root / OUTPUT_FILENAMES["packets"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(packet_rows) == result.packet_count
    assert all(row.get("candidate_only") is True for row in packet_rows)
    assert all(row.get("open_boundary") is True for row in packet_rows)


def test_runner_rejects_selection_above_cap(tmp_path: Path) -> None:
    input_root = tmp_path / "p014" / "development"
    _write_synthetic_input(input_root, count=4)
    with pytest.raises(ValueError, match="selected_packet_count"):
        run_development_context_packet_material(
            input_root,
            tmp_path / "too_many",
            selected_packet_count=25,
        )


def _cue_fixture(rows: list[dict[str, object]], annotations: dict[str, dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    by_id = {str(row["message_id"]): row for row in rows}
    packet = {
        "source_message_ids": list(by_id),
        "primary_fragments": list(rows),
        "adjacent_context": [],
    }
    values, _ = _materialize_selection_cues(
        packet,
        by_id,
        {message_id: SurfaceSignals.for_row(row) for message_id, row in by_id.items()},
        annotations,
    )
    return values


def _cue_message(message_id: str, content: str, sequence: int) -> dict[str, object]:
    return {
        "message_id": message_id,
        "account_id": "cue-account",
        "chat_id": "cue-chat",
        "speaker_id": "speaker-%s" % (sequence % 2),
        "message_type": "text",
        "content": content,
        "sequence_in_chat": sequence,
        "dialogue_segment_id": "same-segment",
        "reply_to_message_id": None,
    }


def test_selection_cues_require_semantic_boundaries_and_independent_evidence() -> None:
    # Time/segment proximity and absent reply metadata alone do not create a
    # candidate.  A pure greeting without a later topic-bearing message also
    # has no boundary candidate.
    weak_rows = [_cue_message("w1", "你好", 1), _cue_message("w2", "稍后再看", 2)]
    weak_annotations = {
        "w1": {"role": ROLE_CONTEXT_ONLY, "topic_bearing": False, "greeting_only": True},
        "w2": {"role": ROLE_CONTEXT_ONLY, "topic_bearing": False},
    }
    weak = _cue_fixture(weak_rows, weak_annotations)
    assert not [row for row in weak["message_metadata"] if row.get("cue_kind") == "greeting_boundary"]
    assert weak["topic_transitions"] == []
    assert weak["candidate_competition"] == []
    assert weak["reply_status"] == []
    assert weak["pronoun_person_object_state"] == []

    # One continuation family (question -> textual follow-up) is not enough
    # for no-reply.  The second row below adds shared grounded text and typed
    # state/action cues, so it becomes a candidate-only status row.
    one_family_rows = [_cue_message("q1", "Linux 怎么办？", 1), _cue_message("q2", "另一个回答", 2)]
    one_family_annotations = {row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True} for row in one_family_rows}
    one_family = _cue_fixture(one_family_rows, one_family_annotations)
    assert one_family["reply_status"] == []

    two_family_rows = [
        _cue_message("q1", "Linux 怎么办？", 1),
        _cue_message("q2", "Linux 失败，需要修复", 2),
    ]
    two_family_annotations = {row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True} for row in two_family_rows}
    two_family = _cue_fixture(two_family_rows, two_family_annotations)
    assert two_family["reply_status"]
    assert set(two_family["reply_status"][0]["semantic_continuation_signals"]) >= {"question_answer", "shared_grounded_cue"}
    assert all(row["candidate_only"] and row["semantic_decision_pending"] and not row["strong_relation"] for rows in two_family.values() for row in rows)

    # An explicit competition cue with one grounded reference is insufficient;
    # two distinct grounded references are required.
    one_grounded_rows = [
        _cue_message("c1", "Option A vs option B", 1),
        _cue_message("c2", "Option A https://a.example", 2),
    ]
    one_grounded_annotations = {row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True} for row in one_grounded_rows}
    assert _cue_fixture(one_grounded_rows, one_grounded_annotations)["candidate_competition"] == []

    two_grounded_rows = one_grounded_rows + [_cue_message("c3", "Option B https://b.example", 3)]
    two_grounded_annotations = {row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True} for row in two_grounded_rows}
    competition = _cue_fixture(two_grounded_rows, two_grounded_annotations)["candidate_competition"]
    assert competition
    assert competition[0]["explicit_competition_cue"] is True
    assert competition[0]["grounded_recall_candidate_count"] >= 2


def test_audit_counterexamples_reject_ack_adversative_and_unbound_pronoun() -> None:
    # A genuine conversation opener followed by a substantive turn is the
    # positive greeting-boundary case.
    greeting_rows = [
        _cue_message("greet", "你好", 1),
        _cue_message("greet-topic", "项目上线状态", 2),
    ]
    greeting_annotations = {
        "greet": {"role": ROLE_CONVERSATION_OPENER, "topic_bearing": False},
        "greet-topic": {"role": ROLE_SUBSTANTIVE, "topic_bearing": True},
    }
    greeting_cues = _cue_fixture(greeting_rows, greeting_annotations)
    greeting_boundaries = [
        row for row in greeting_cues["message_metadata"] if row.get("cue_kind") == "greeting_boundary"
    ]
    assert len(greeting_boundaries) == 1
    assert greeting_boundaries[0]["message_refs"] == ["greet", "greet-topic"]

    # A segment-opening acknowledgement is context-only, not a greeting
    # boundary.  This is the concrete ``可以`` false-positive from the audit.
    ack_rows = [
        _cue_message("ack", "可以", 1),
        _cue_message("ack-topic", "项目上线状态", 2),
    ]
    ack_annotations = {
        "ack": {"role": ROLE_CONTEXT_ONLY, "topic_bearing": False, "greeting_only": True},
        "ack-topic": {"role": ROLE_SUBSTANTIVE, "topic_bearing": True},
    }
    ack_cues = _cue_fixture(ack_rows, ack_annotations)
    assert not [row for row in ack_cues["message_metadata"] if row.get("cue_kind") == "greeting_boundary"]

    # A normal adversative keeps one topic; it is not an explicit topic pivot.
    adversative_rows = [
        _cue_message("adv-left", "项目失败", 1),
        _cue_message("adv-mid", "但是需要修复", 2),
        _cue_message("adv-right", "项目已经恢复", 3),
    ]
    adversative_annotations = {
        row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True}
        for row in adversative_rows
    }
    adversative_cues = _cue_fixture(adversative_rows, adversative_annotations)
    assert adversative_cues["topic_transitions"] == []

    # An explicit pivot with topic-bearing turns on both sides is a positive
    # topic-shift case.
    pivot_rows = [
        _cue_message("pivot-left", "项目失败", 1),
        _cue_message("pivot-mid", "换个话题：旅行计划", 2),
        _cue_message("pivot-right", "旅行计划已经确认", 3),
    ]
    pivot_annotations = {
        row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True}
        for row in pivot_rows
    }
    pivot_cues = _cue_fixture(pivot_rows, pivot_annotations)
    assert len(pivot_cues["topic_transitions"]) == 1
    assert pivot_cues["topic_transitions"][0]["transition_message_ref"] == "pivot-mid"
    assert pivot_cues["topic_transitions"][0]["explicit_transition_cue"] is True

    # A pronoun-only turn and a pronoun/object cue in one message are both
    # insufficient.  A distinct grounded support message is required.
    pronoun_only_rows = [_cue_message("p-only", "这个", 1)]
    pronoun_only_annotations = {"p-only": {"role": ROLE_SUBSTANTIVE, "topic_bearing": True}}
    assert _cue_fixture(pronoun_only_rows, pronoun_only_annotations)["pronoun_person_object_state"] == []

    same_message_rows = [_cue_message("p-same", "这个项目失败", 1)]
    same_message_annotations = {"p-same": {"role": ROLE_SUBSTANTIVE, "topic_bearing": True}}
    assert _cue_fixture(same_message_rows, same_message_annotations)["pronoun_person_object_state"] == []

    bound_rows = [
        _cue_message("p-bound", "这个", 1),
        _cue_message("s-bound", "项目失败，需要修复", 2),
    ]
    bound_annotations = {row["message_id"]: {"role": ROLE_SUBSTANTIVE, "topic_bearing": True} for row in bound_rows}
    bound_cues = _cue_fixture(bound_rows, bound_annotations)
    assert bound_cues["pronoun_person_object_state"]
    assert bound_cues["pronoun_person_object_state"][0]["independent_grounded_support"] is True
    assert bound_cues["pronoun_person_object_state"][0]["person_cue_message_refs"] == ["p-bound"]
    assert bound_cues["pronoun_person_object_state"][0]["grounded_support_message_refs"] == ["s-bound"]
