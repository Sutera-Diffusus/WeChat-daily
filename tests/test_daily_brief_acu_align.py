import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.daily_brief_acu_align import AlignmentInputError, align_rows


def _row(acu_id, evidence, *, label="must", actor="用户", action="确认", obj="事项", status="confirmed", text=None):
    row = {
        "acu_id": acu_id,
        "atomic_content": text or acu_id,
        "evidence_message_ids": evidence,
        "actor": actor,
        "action": action,
        "object_or_recipient": obj,
        "status": status,
        "claim_type": "observed_action",
        "user_relevance": "direct",
        "inclusion_label": label,
        "exclusion_reason": None if label != "exclude" else "trivial",
        "uncertainty": [],
    }
    return row


def test_exact_evidence_alignment_reports_slot_and_label_disagreements():
    result = align_rows(
        [_row("a1", ["m1"], actor="用户", label="must")],
        [_row("b1", ["m1"], actor="宁江月", label="may", status="pending")],
    )

    assert result["summary"]["matched_pair_count"] == 1
    pair = result["matched_pairs"][0]
    assert pair["evidence_intersection"] == ["m1"]
    assert {item["field"] for item in pair["disagreements"]} == {
        "actor",
        "status",
        "inclusion_label",
    }
    assert result["policy"]["text_similarity_used"] is False
    assert result["policy"]["total_score"] is None


def test_same_text_without_direct_evidence_is_unmatched():
    result = align_rows(
        [_row("a1", ["m1"], text="同一内容")],
        [_row("b1", ["m2"], text="同一内容")],
    )

    assert result["matched_pairs"] == []
    assert result["ambiguous_many_to_many"] == []
    assert result["unmatched"]["round_a"][0]["reason"] == "no_direct_evidence_id_intersection"
    assert result["unmatched"]["round_b"][0]["reason"] == "no_direct_evidence_id_intersection"
    assert result["unmatched"]["round_a"][0]["row"]["atomic_content"] == "同一内容"


def test_many_to_many_is_retained_without_forced_assignment():
    result = align_rows(
        [_row("a1", ["m1"]), _row("a2", ["m1"], action="发送")],
        [_row("b1", ["m1"]), _row("b2", ["m1"], action="接收")],
    )

    assert result["matched_pairs"] == []
    assert len(result["ambiguous_many_to_many"]) == 1
    ambiguous = result["ambiguous_many_to_many"][0]
    assert ambiguous["round_a_acu_ids"] == ["a1", "a2"]
    assert ambiguous["round_b_acu_ids"] == ["b1", "b2"]
    assert len(ambiguous["candidate_pairs"]) == 4
    assert len(ambiguous["round_a_rows"]) == 2
    assert len(ambiguous["round_b_rows"]) == 2
    assert ambiguous["resolution"] == "unresolved"
    assert result["summary"]["unmatched_round_a_count"] == 0


def test_empty_evidence_is_unmatched_and_unknown_is_compared_semantically():
    result = align_rows(
        [_row("a1", [], actor=None, status=None)],
        [_row("b1", [], actor="unknown", status="待确认")],
    )

    assert result["matched_pairs"] == []
    assert result["unmatched"]["round_a"][0]["reason"] == "missing_direct_evidence_ids"
    assert result["unmatched"]["round_b"][0]["reason"] == "missing_direct_evidence_ids"


def test_canonical_rows_have_strict_label_and_duplicate_guards():
    with pytest.raises(AlignmentInputError, match="missing inclusion_label"):
        align_rows([{"acu_id": "a1", "evidence_message_ids": ["m1"]}], [])

    with pytest.raises(AlignmentInputError, match="duplicate round_a acu_id"):
        align_rows([_row("a1", ["m1"]), _row("a1", ["m2"])], [])


def test_legacy_aliases_are_loaded_by_cli_and_output_is_machine_readable(tmp_path):
    round_a = tmp_path / "round-a.jsonl"
    round_b = tmp_path / "round-b.jsonl"
    output = tmp_path / "alignment.json"
    round_a.write_text(
        json.dumps(
            {
                "acu_id": "a1",
                "decision": "must",
                "evidence_refs": ["m1"],
                "actor": "甲",
                "action": "确认",
                "object": "事项",
                "status": "confirmed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    round_b.write_text(
        json.dumps(_row("b1", ["m1"], actor="甲"), ensure_ascii=False),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "tools" / "daily_brief_acu_align.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--round-a",
            str(round_a),
            "--round-b",
            str(round_b),
            "--output",
            str(output),
            "--compact",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "daily_brief_acu_align.v1"
    assert payload["matched_pairs"][0]["evidence_intersection"] == ["m1"]
    assert payload["matched_pairs"][0]["disagreements"] == []
