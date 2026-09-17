"""Synthetic K9 runner smoke tests.

The fixtures are a tiny K5-shaped development artifact written under
``tmp_path``.  They never read the real private split, frozen data, K7 output,
or a provider.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from wechat_bridge.linear_stage_packet_development_runner import (
    OUTPUT_FILENAMES,
    run_linear_stage_packet_development,
    run_linear_stage_packet_development_from_mappings,
)


ACCOUNT = "account-k9-synthetic"
CHAT = "chat-k9-synthetic"


def _fragment(message_id: str, body: str, *, role: str = "substantive", fragment_type: str = "statement", sequence: int = 1) -> dict[str, Any]:
    return {
        "fragment_id": "fragment-%s" % message_id,
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "speaker_id": "speaker-%d" % (sequence % 2),
        "direction": "incoming" if sequence % 2 else "outgoing",
        "message_type": "text",
        "sequence_in_chat": sequence,
        "dialogue_segment_id": "segment-main" if role == "substantive" else "segment-social",
        "role": role,
        "fragment_type": fragment_type,
        "text_redacted": body,
        "content": body,
        "span": {"start": 0, "end": len(body)},
        "candidate_only": True,
    }


def _evidence(evidence_id: str, message_id: str, body: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "type": "span",
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "span": {"start": 0, "end": len(body)},
        "evidence_text": body,
    }


def _packet(packet_id: str, *, body_suffix: str = "") -> dict[str, Any]:
    question = _fragment("m-question-%s" % packet_id, "question%s" % body_suffix, fragment_type="question", sequence=1)
    answer = _fragment("m-answer-%s" % packet_id, "answer%s" % body_suffix, sequence=2)
    greeting = _fragment(
        "m-greeting-%s" % packet_id,
        "hello%s" % body_suffix,
        role="conversation_opener",
        fragment_type="conversation_opener",
        sequence=3,
    )
    acknowledgement = _fragment(
        "m-ack-%s" % packet_id,
        "received%s" % body_suffix,
        role="context_only",
        fragment_type="acknowledgement",
        sequence=4,
    )
    evidence = [
        _evidence("e-question-%s" % packet_id, question["message_id"], "question evidence%s" % body_suffix),
        _evidence("e-answer-%s" % packet_id, answer["message_id"], "answer evidence%s" % body_suffix),
        _evidence("e-greeting-%s" % packet_id, greeting["message_id"], "greeting evidence%s" % body_suffix),
        _evidence("e-ack-%s" % packet_id, acknowledgement["message_id"], "ack evidence%s" % body_suffix),
    ]
    return {
        "packet_id": packet_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "primary_fragments": [question, answer],
        "adjacent_context": [greeting, acknowledgement],
        "authoritative_facts": [
            {
                "message_id": row["message_id"],
                "account_id": ACCOUNT,
                "chat_id": CHAT,
                "speaker_id": row["speaker_id"],
                "sequence_in_chat": row["sequence_in_chat"],
                "metadata_authoritative": True,
            }
            for row in (question, answer, greeting, acknowledgement)
        ],
        "source_refs": [
            {"type": "message", "id": question["message_id"], "account_id": ACCOUNT, "chat_id": CHAT},
            {"type": "message", "id": answer["message_id"], "account_id": ACCOUNT, "chat_id": CHAT},
        ],
        "evidence_refs": evidence,
        "candidate_qa_links": [
            {
                "candidate_id": "candidate-%s" % packet_id,
                "left_message_id": question["message_id"],
                "right_message_id": answer["message_id"],
                "account_id": ACCOUNT,
                "chat_id": CHAT,
                "relation_label": "possibly_related",
                "candidate_reason": ["explicit_reply", "shared_object"],
                "candidate_only": True,
                "evidence_refs": evidence[:2],
            }
        ],
        "fixed_part": {"fixed_part_version": "k9-fixed-v1", "scope": {"account_id": ACCOUNT, "chat_id": CHAT}},
        "dynamic_part": {"dynamic_part_version": "k9-dynamic-v1", "status": "open"},
        "topics": [
            {
                "topic_id": "topic-main",
                "message_ids": [question["message_id"], answer["message_id"]],
                "candidate_ids": ["candidate-%s" % packet_id],
                "evidence_ids": [evidence[0]["evidence_id"], evidence[1]["evidence_id"]],
            }
        ],
        "open_boundary": True,
        "candidate_only": True,
    }


def _write_k5_artifact(root: Path, packets: list[Mapping[str, Any]]) -> Path:
    root.mkdir(parents=True)
    selection_rows = [
        {
            "selected": True,
            "selection_rank": index + 1,
            "packet_id": packet["packet_id"],
            "packet_hash": "source-hash-%d" % index,
            "message_count": 4,
            "candidate_count": 1,
            "evidence_count": 4,
        }
        for index, packet in enumerate(packets)
    ]
    raw_packets = "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for packet in packets).encode("utf-8")
    (root / "packets.private.jsonl").write_bytes(raw_packets)
    (root / "selection_map.private.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in selection_rows),
        encoding="utf-8",
    )
    manifest = {
        "artifact_version": "context_packet_development_v1",
        "runner_schema_version": "context_packet_development_runner_v1",
        "split": "development",
        "local_day": "2026-08-25",
        "status": "complete",
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "selection": {"selected_packet_count": len(packets), "selected_packet_limit": len(packets)},
        "input_sha256": hashlib.sha256(b"synthetic-development").hexdigest(),
    }
    (root / "manifest.private.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return root


def test_k9_runner_materializes_every_page_and_recovers_context(tmp_path: Path) -> None:
    input_root = _write_k5_artifact(tmp_path / "context_packet_development_v1", [_packet("p-one"), _packet("p-two", body_suffix="-two")])
    output_root = tmp_path / "linear_stage_packet_development_v1"

    result = run_linear_stage_packet_development(input_root, output_root, selected_packet_count=2)

    assert result.status == "complete"
    assert result.selected_packet_count == 2
    assert result.root_count == 2
    assert result.page_count == 2
    assert result.aggregate["metrics"]["roots_mapping_complete"] is True
    assert result.aggregate["metrics"]["page_formula"]["linear"] is True
    assert result.aggregate["metrics"]["stage_b"]["status"] == "N/A"
    assert result.aggregate["metrics"]["stage_c"]["status"] == "N/A"
    token_metrics = result.aggregate["metrics"]["stage_a_token_proxy"]
    user_token_metrics = result.aggregate["metrics"]["stage_a_user_token_proxy"]
    assert token_metrics["max"] <= 2000
    assert user_token_metrics["max"] <= 1600
    assert token_metrics["p50"] <= token_metrics["p95"]
    assert user_token_metrics["p50"] <= user_token_metrics["p95"]
    cost = json.loads((output_root / OUTPUT_FILENAMES["cost"]).read_text(encoding="utf-8"))
    assert cost["stage_a"]["input_token_proxy_p50"] == token_metrics["p50"]
    assert cost["stage_a"]["user_token_proxy_p95"] == user_token_metrics["p95"]
    assert result.aggregate["metrics"]["recovery_rates"]["source"]["rate"] == 1.0
    assert result.aggregate["metrics"]["recovery_rates"]["primary"]["rate"] == 1.0
    assert result.aggregate["metrics"]["recovery_rates"]["adjacent"]["rate"] == 1.0
    assert result.aggregate["metrics"]["recovery_rates"]["greeting"]["rate"] == 1.0
    assert result.aggregate["metrics"]["recovery_rates"]["ack"]["rate"] == 1.0
    assert result.aggregate["metrics"]["recovery_rates"]["evidence"]["rate"] == 1.0
    assert result.aggregate["metrics"]["replay"]["idempotent"] is True
    assert all(row["status"] == "complete" for row in result.materialized)
    assert all(row["stage_b_status"] == "N/A" and row["stage_c_status"] == "N/A" for row in result.materialized)

    for key in ("manifest", "aggregate", "cost"):
        data = json.loads((output_root / OUTPUT_FILENAMES[key]).read_text(encoding="utf-8"))
        encoded = json.dumps(data, ensure_ascii=False)
        assert '"content"' not in encoded
        assert '"text_redacted"' not in encoded
    selection = [json.loads(line) for line in (output_root / OUTPUT_FILENAMES["selection"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    errors = [line for line in (output_root / OUTPUT_FILENAMES["errors"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(selection) == 2
    assert not errors
    store_payload = json.loads((output_root / OUTPUT_FILENAMES["store"]).read_text(encoding="utf-8"))
    assert "question-two" in json.dumps(store_payload, ensure_ascii=False)
    recovery = [json.loads(line) for line in (output_root / OUTPUT_FILENAMES["recovery"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(recovery) == 2
    assert "question evidence" in json.dumps(recovery, ensure_ascii=False)


def test_k9_runner_keeps_pending_stage_a_open_snapshot(tmp_path: Path) -> None:
    packets = [_packet("p-pending")]
    # Make the local material exceed the synthetic budget without changing
    # the page handle limits; the core must retain an open snapshot.
    packets[0]["fixed_part"]["large_marker"] = "x" * 900
    input_root = _write_k5_artifact(tmp_path / "context_packet_development_v1", packets)
    result = run_linear_stage_packet_development(
        input_root,
        tmp_path / "linear_stage_packet_development_v1",
        selected_packet_count=1,
        capacity={"max_input_token_proxy": 64, "max_user_token_proxy": 32, "max_messages": 24, "max_candidate_rows": 64, "max_evidence_refs": 64},
    )

    assert result.status == "complete"
    assert result.pending_count == 1
    row = result.materialized[0]
    assert row["status"] == "pending"
    assert row["open_snapshot_ok"] is True
    assert row.get("open_snapshot_ref")
    assert result.aggregate["metrics"]["stage_a_budget"]["pending_snapshots_ok"] is True


def test_k9_mapping_boundary_runs_two_roots_without_reading_an_artifact(tmp_path: Path) -> None:
    packets = [_packet("mapping-one"), _packet("mapping-two")]
    refs = [
        {"packet_id": "mapping-two", "selection_rank": 2, "selected": True},
        {"packet_id": "mapping-one", "selection_rank": 1, "selected": True},
    ]
    result = run_linear_stage_packet_development_from_mappings(
        packets,
        refs,
        tmp_path / "linear_stage_packet_development_v1",
        selected_packet_count=2,
    )
    assert result.status == "complete"
    assert [row["source_packet_id"] for row in result.recovery] == ["mapping-one", "mapping-two"]
    assert result.page_count == result.aggregate["metrics"]["page_formula"]["expected_total"] == 2
    assert result.aggregate["metrics"]["replay"]["idempotent"] is True


def test_k10_v2_requires_twenty_complete_roots_and_unions_nested_evidence(tmp_path: Path) -> None:
    packets = [_packet("v2-%02d" % index) for index in range(20)]
    for packet in packets:
        # The two candidate-owned refs are the only expected evidence rows in
        # this regression; root-level evidence is deliberately absent.
        packet["evidence_refs"] = []
    refs = [
        {"packet_id": packet["packet_id"], "selection_rank": index + 1, "selected": True}
        for index, packet in enumerate(packets)
    ]
    output_root = tmp_path / "linear_stage_packet_development_v2"
    result = run_linear_stage_packet_development_from_mappings(
        packets,
        refs,
        output_root,
        selected_packet_count=20,
        output_version="v2",
    )

    assert result.status == "complete"
    assert result.root_count == result.selected_packet_count == 20
    assert result.pending_count == 0
    assert result.aggregate["artifact_version"] == "linear_stage_packet_development_v2"
    assert result.aggregate["runner_schema_version"] == "linear_stage_packet_development_runner_v2"
    assert result.aggregate["metrics"]["code_sha256"]["runner"]
    assert result.aggregate["metrics"]["recovery_rates"]["evidence"]["rate"] == 1.0
    assert result.aggregate["metrics"]["page_formula"]["linear"] is True
    assert result.aggregate["metrics"]["replay"]["idempotent"] is True
    assert all(row["status"] == "complete" for row in result.materialized)

    manifest = json.loads((output_root / OUTPUT_FILENAMES["manifest"]).read_text(encoding="utf-8"))
    assert manifest["artifact_version"] == "linear_stage_packet_development_v2"
    assert set(manifest["artifact_hashes"]) == set(OUTPUT_FILENAMES.values()) - {OUTPUT_FILENAMES["manifest"]}
