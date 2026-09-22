import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.daily_brief_eval import EvalInputError, evaluate, load_candidate_json, load_gold_jsonl


def _gold():
    return [
        {
            "acu_id": "a1",
            "text": "已确认交接",
            "inclusion_label": "must",
            "actor": "宁江月",
            "action": "交接",
            "object_or_recipient": "团组织关系转接工作",
            "status": "confirmed",
            "evidence_message_ids": ["m1"],
        },
        {
            "acu_id": "a2",
            "text": "可复用方法",
            "inclusion_label": "may",
            "actor": "我",
            "action": "记录",
            "object_or_recipient": "方法",
            "status": "unknown",
            "evidence_message_ids": ["m2"],
        },
        {
            "acu_id": "a3",
            "text": "普通名单",
            "inclusion_label": "exclude",
            "actor": "群成员",
            "action": "发送",
            "object_or_recipient": "名单",
            "status": "reported",
            "evidence_message_ids": ["m3"],
        },
    ]


def test_evaluate_reports_each_dimension_without_length_cap():
    result = evaluate(
        _gold(),
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "acu_ids": ["a1"],
                    "actor": "宁江月",
                    "action": "交接",
                    "object_or_recipient": "团组织关系转接工作",
                    "status": "confirmed",
                    "evidence_message_ids": ["m1"],
                },
                {
                    "claim_id": "c2",
                    "acu_ids": ["a1"],
                    "actor": "宁江月",
                    "action": "交接",
                    "object_or_recipient": "团组织关系转接工作",
                    "status": "confirmed",
                    "evidence_message_ids": ["m1"],
                },
                {
                    "claim_id": "c3",
                    "acu_ids": ["a3"],
                    "actor": "群成员",
                    "action": "发送",
                    "object_or_recipient": "名单",
                    "status": "reported",
                    "evidence_message_ids": ["m3"],
                },
                # An unknown binding is an auditable false positive, not an
                # excuse to silently drop this report claim from precision.
                {"claim_id": "c4", "acu_ids": ["not-in-gold"], "evidence_message_ids": ["m99"]},
                # An unbound claim is likewise counted as an unmatched unit.
                {"claim_id": "c5", "evidence_message_ids": []},
            ]
        },
    )

    assert result["acu_precision"] == pytest.approx(2 / 5)
    assert result["acu_recall"] == pytest.approx(1 / 2)
    assert result["must_recall"] == 1.0
    assert result["trivial_inclusion_rate"] == pytest.approx(1 / 5)
    assert result["redundancy_rate"] == pytest.approx(1 / 4)
    assert result["unsupported_claim_rate"] == pytest.approx(2 / 5)
    assert result["slot_accuracy"] == {
        "actor": 1.0,
        "action": 1.0,
        "object_or_recipient": 1.0,
        "status": 1.0,
    }
    assert result["counts"]["candidate_claims"] == 5
    assert result["counts"]["duplicate_acu_references"] == 1
    assert result["counts"]["unmatched_claims"] == 1
    assert len(result["per_claim"]) == 5


def test_slot_unknown_and_evidence_are_scored_explicitly():
    gold = [
        {
            "acu_id": "a1",
            "inclusion_label": "must",
            "actor": None,
            "action": "确认",
            "object_or_recipient": "事项",
            "status": "待确认",
            "evidence_message_ids": ["m1", "m2"],
        }
    ]
    result = evaluate(
        gold,
        {
            "claims": [
                {
                    "acu_id": "a1",  # singular alias is accepted
                    "actor": "unknown",
                    "action": "确认",
                    "object_or_recipient": "事项",
                    "status": None,
                    "evidence_message_ids": ["m2", "unrelated"],
                }
            ]
        },
    )
    assert result["slot_accuracy"] == {
        "actor": 1.0,
        "action": 1.0,
        "object_or_recipient": 1.0,
        "status": 1.0,
    }
    assert result["unsupported_claim_rate"] == 0.0


def test_empty_inputs_return_null_ratios_instead_of_fake_zeros():
    result = evaluate([], {"claims": []})

    for key in (
        "acu_precision",
        "acu_recall",
        "must_recall",
        "trivial_inclusion_rate",
        "redundancy_rate",
        "unsupported_claim_rate",
    ):
        assert result[key] is None
    assert result["slot_accuracy"] == {
        "actor": None,
        "action": None,
        "object_or_recipient": None,
        "status": None,
    }


def test_loaders_validate_jsonl_identity_and_candidate_schema(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text(
        json.dumps(_gold()[0], ensure_ascii=False) + "\n\n" + json.dumps(_gold()[1], ensure_ascii=False),
        encoding="utf-8",
    )
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps({"claims": []}), encoding="utf-8")

    assert len(load_gold_jsonl(gold_path)) == 2
    assert load_candidate_json(candidate_path) == {"claims": []}

    duplicate_path = tmp_path / "duplicate.jsonl"
    duplicate_path.write_text(
        json.dumps(_gold()[0], ensure_ascii=False) + "\n" + json.dumps(_gold()[0], ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(EvalInputError, match="duplicate gold acu_id"):
        load_gold_jsonl(duplicate_path)

    missing_claims = tmp_path / "missing.json"
    missing_claims.write_text("{}", encoding="utf-8")
    with pytest.raises(EvalInputError, match="missing claims array"):
        load_candidate_json(missing_claims)


def test_cli_emits_machine_readable_metrics(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in _gold()), encoding="utf-8")
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(
        json.dumps({"claims": [{"acu_ids": ["a1"], "evidence_message_ids": ["m1"]}]}), encoding="utf-8"
    )

    script = Path(__file__).parents[1] / "tools" / "daily_brief_eval.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--gold", str(gold_path), "--candidate", str(candidate_path), "--compact"],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["schema_version"] == "daily_brief_eval.v1"
    assert output["must_recall"] == 1.0
    assert completed.stderr == ""


def test_legacy_field_aliases_remain_accepted():
    result = evaluate(
        [{
            "acu_id": "legacy-a1", "decision": "must", "actor": "甲",
            "action": "确认", "object": "事项", "status": "done",
            "evidence_refs": ["legacy-m1"],
        }],
        {"claims": [{
            "acu_ids": ["legacy-a1"], "actor": "甲", "action": "确认",
            "object": "事项", "status": "done", "evidence_refs": ["legacy-m1"],
        }]},
    )
    assert result["must_recall"] == 1.0
    assert result["slot_accuracy"]["object_or_recipient"] == 1.0
    assert result["unsupported_claim_rate"] == 0.0
