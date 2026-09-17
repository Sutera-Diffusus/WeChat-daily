"""Synthetic K7 development compact-runner smoke tests.

The fixture is K5-shaped and local only: no real split, frozen input, or
provider is touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from wechat_bridge.context_packet_compact_development_runner import (
    OUTPUT_FILENAMES,
    run_compact_context_packet_development,
)


def _packet(index: int) -> dict:
    account, chat = "a-k7", "c-k7"
    message_id = f"m-{index}"
    fragment_id = f"f-{index}"
    body = "你好，收到后继续看这个对象。" if index == 1 else "收到，继续看这个对象。"
    fragment = {
        "fragment_id": fragment_id,
        "message_id": message_id,
        "account_id": account,
        "chat_id": chat,
        "text_redacted": body,
        "content": body,
        "span": {"start": 0, "end": len(body)},
        "role": "conversation_opener" if index == 1 else "context_only",
        "fragment_type": "conversation_opener" if index == 1 else "acknowledgement",
        "speaker_id": "s1",
        "subject_id": "person-k7",
        "object_id": "object-k7",
        "object_resolution": "explicit",
        "candidate_only": True,
        "evidence_refs": [{"evidence_id": "e-shared", "type": "span", "message_id": message_id, "fragment_id": fragment_id, "account_id": account, "chat_id": chat, "span": {"start": 0, "end": 4}}],
    }
    candidate = {
        "candidate_id": "candidate-shared",
        "left_message_id": message_id,
        "right_message_id": message_id,
        "account_id": account,
        "chat_id": chat,
        "relation_label": "candidate_only",
        "candidate_reason": ["explicit_reply"],
        "strong_relation": False,
        "candidate_only": True,
        "evidence_refs": [{"evidence_id": "e-shared", "type": "span", "message_id": message_id, "account_id": account, "chat_id": chat, "span": {"start": 0, "end": 4}}],
    }
    return {
        "packet_id": f"K5_PACKET_{index}",
        "context_packet_id": f"K5_PACKET_{index}",
        "packet_version": "k5-synthetic-v1",
        "account_id": account,
        "chat_id": chat,
        "scope": {"account_id": account, "chat_id": chat},
        "anchor_fragment_ids": [fragment_id],
        "claim_ids": [f"claim-{index}"],
        "primary_fragments": [fragment],
        "adjacent_context": [fragment],
        "authoritative_facts": [{"message_id": message_id, "account_id": account, "chat_id": chat, "speaker_id": "s1", "message_type": "text", "metadata_authoritative": True}],
        "candidate_qa_links": [candidate],
        "candidate_person_history": [],
        "candidate_object_history": [candidate],
        "candidate_state_history": [],
        "evidence_refs": [{"evidence_id": "e-shared", "type": "span", "message_id": message_id, "account_id": account, "chat_id": chat, "span": {"start": 0, "end": 4}}],
        "source_refs": [{"type": "message", "id": message_id, "message_id": message_id, "account_id": account, "chat_id": chat}],
        "open_thread_candidates": [{"candidate_id": f"open-{index}", "thread_id": f"thread-{index}", "account_id": account, "chat_id": chat, "open_boundary": True, "candidate_only": True}],
        "activation_cues": [{"replay_key": f"cue-{index}", "cue_type": "question_follow_up", "message_ids": [message_id]}],
        "candidate_reason": ["explicit_reply"],
        "uncertainties": ["semantic_pending"],
        "boundary": {"start": {"message_id": message_id}, "end": {"message_id": message_id}},
        "fixed_hash": f"fixed-{index}",
        "dynamic_hash": f"dynamic-{index}",
        "packet_hash": f"source-{index}",
        "open_boundary": True,
    }


def _write_input(root: Path) -> None:
    root.mkdir()
    packets = [_packet(1), _packet(2)]
    packet_lines = "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n" for packet in packets).encode("utf-8")
    (root / "packets.private.jsonl").write_bytes(packet_lines)
    selection_lines = "".join(json.dumps({"selected": True, "selection_rank": index, "packet_id": packet["packet_id"]}, sort_keys=True) + "\n" for index, packet in enumerate(packets, 1))
    (root / "selection_map.private.jsonl").write_text(selection_lines, encoding="utf-8")
    (root / "manifest.private.json").write_text(
        json.dumps({"artifact_version": "context_packet_development_v1", "split": "development", "local_day": "2026-08-25", "frozen_read": False, "provider_called": False}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_synthetic_k7_compact_runner_materializes_bounded_envelopes(tmp_path: Path) -> None:
    source = tmp_path / "context_packet_development_v1"
    output = tmp_path / "context_packet_compact_development_v1"
    _write_input(source)

    result = run_compact_context_packet_development(source, output, selected_packet_count=2)

    assert result.status == "complete"
    assert result.root_count == 2
    assert result.pending_count == 0
    assert result.aggregate["metrics"]["materialized_token_proxy"]["max"] <= 2000
    assert result.aggregate["metrics"]["recovery_rates"]["primary"]["rate"] == 1.0
    assert result.aggregate["metrics"]["recovery_rates"]["open_snapshot"]["rate"] == 1.0
    assert set(result.artifact_paths) == set(OUTPUT_FILENAMES)
    for key in ("manifest", "aggregate", "cost"):
        assert (output / OUTPUT_FILENAMES[key]).is_file()
    assert (output / OUTPUT_FILENAMES["store"]).is_file()


def test_synthetic_k7_selection_must_be_explicit(tmp_path: Path) -> None:
    source = tmp_path / "context_packet_development_v1"
    _write_input(source)
    # The runner is allowed to run a smaller synthetic selection, but cannot
    # silently compact an unselected K5 packet.
    selection = source / "selection_map.private.jsonl"
    rows = [json.loads(line) for line in selection.read_text(encoding="utf-8").splitlines()]
    rows[1]["selected"] = False
    selection.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    try:
        run_compact_context_packet_development(source, tmp_path / "out", selected_packet_count=2)
    except ValueError as exc:
        assert "selected packet refs" in str(exc)
    else:
        raise AssertionError("runner accepted a non-selected packet")
