import json
from pathlib import Path

import pytest

from wechat_bridge.daily_brief_selector import (
    DEFAULT_QUERY,
    SelectionFailure,
    evaluate_candidates,
    join_codebook_by_evidence,
    select_candidates,
    write_selection_artifacts,
)


def _candidate(
    candidate_id="c-1",
    *,
    label="must",
    status="confirmed",
    relevance="direct",
    evidence=("m-1",),
    title="活动安排已确认",
):
    return {
        "candidate_id": candidate_id,
        "title": title,
        "atomic_content": "活动安排已经确认，用户需要据此行动。",
        "actor": "组织方",
        "action": "确认安排",
        "object_or_recipient": "活动安排",
        "status": status,
        "claim_type": "observed_action",
        "user_relevance": relevance,
        "inclusion_label": label,
        "exclusion_reason": "trivial" if label == "exclude" else None,
        "evidence_message_ids": list(evidence),
        "evidence_bindings": [{
            "statement": "活动安排已经确认",
            "message_ids": list(evidence),
        }],
        "uncertainty": [],
    }


def test_selector_uses_codebook_without_length_cap_or_word_clustering():
    candidates = [
        _candidate("must", title="相同词语但事项一"),
        _candidate("may", label="may", evidence=("m-2",), title="相同词语但事项二"),
        _candidate("excluded", label="exclude", evidence=("m-3",), title="相同词语但事项三"),
    ]

    result = select_candidates(candidates, query=DEFAULT_QUERY, allowed_evidence_ids={"m-1", "m-2", "m-3"})

    assert result["status"] == "ok"
    assert [row["candidate_id"] for row in result["selected"]] == ["must", "may"]
    assert result["audit"]["selected_count"] == 2
    assert result["policy"]["fixed_output_count"] is False
    assert result["policy"]["top_k"] is False
    assert result["policy"]["word_overlap_clustering"] is False
    assert any(row["reason"] == "codebook_exclude" for row in result["rejected"])


def test_unknown_status_is_preserved_and_never_promoted_to_complete():
    result = select_candidates(
        [_candidate("unknown", status="unknown")],
        allowed_evidence_ids={"m-1"},
    )
    row = result["selected"][0]
    assert row["status"] == "unknown"
    assert row["completion_inference_forbidden"] is True
    assert "status_unknown" in row["uncertainty"]


def test_canonical_acu_without_bindings_gets_one_safe_atomic_binding():
    acu = _candidate("acu-1", evidence=("m-1",), title="ACU 原子内容")
    acu.pop("evidence_bindings")
    result = select_candidates([acu], allowed_evidence_ids={"m-1"})
    row = result["selected"][0]
    assert row["evidence_bindings"] == [{
        "statement": acu["atomic_content"],
        "message_ids": ["m-1"],
    }]


def test_semantic_raw_candidate_without_bindings_stays_rejected():
    raw = _candidate("line-raw", evidence=("m-1",), title="语义候选")
    raw.pop("evidence_bindings")
    raw.pop("atomic_content")
    raw["summary_candidate"] = "语义模型摘要"
    raw["support_message_ids"] = ["m-1"]
    result = evaluate_candidates([raw], allowed_evidence_ids={"m-1"})
    assert result["status"] == "failed"
    assert result["rejected"][0]["reason"] == "missing_direct_evidence_binding"


def test_missing_label_or_direct_binding_cannot_become_formal_body():
    missing_label = _candidate("unlabelled")
    missing_label.pop("inclusion_label")
    missing_binding = _candidate("unbound", evidence=("m-2",))
    missing_binding.pop("evidence_bindings")
    missing_binding.pop("atomic_content")
    result = evaluate_candidates(
        [missing_label, missing_binding],
        allowed_evidence_ids={"m-1", "m-2"},
    )
    assert result["status"] == "failed"
    assert result["failure_reason"] == "no_qualified_candidates"
    assert {row["reason"] for row in result["rejected"]} == {
        "missing_codebook_label",
        "missing_direct_evidence_binding",
    }


def test_no_qualified_candidates_raise_and_write_explicit_failure(tmp_path: Path):
    with pytest.raises(SelectionFailure) as exc_info:
        select_candidates([_candidate("excluded", label="exclude")])
    assert exc_info.value.result["status"] == "failed"
    paths = write_selection_artifacts(exc_info.value.result, tmp_path)
    assert (tmp_path / "selection_summary.json").exists()
    assert (tmp_path / "selection_failure.json").exists()
    assert "claims" not in paths
    persisted = json.loads((tmp_path / "selection_failure.json").read_text(encoding="utf-8"))
    assert persisted["policy"]["local_fallback_used"] is False


def test_codebook_join_uses_exact_evidence_intersection_not_title_overlap():
    candidates = [
        _candidate("line-1", evidence=("m-1",), title="模型成本"),
        _candidate("line-2", evidence=("m-9",), title="模型成本"),
    ]
    decisions = [
        {"acu_id": "acu-1", "evidence_message_ids": ["m-1"], "inclusion_label": "must"},
        {"acu_id": "acu-2", "evidence_message_ids": ["m-2"], "inclusion_label": "must"},
    ]
    joined = join_codebook_by_evidence(candidates, decisions)
    assert joined["line-1"]["acu_id"] == "acu-1"
    assert joined["line-2"]["join_error"] == "no_direct_evidence_intersection"


def test_ambiguous_evidence_join_is_not_forced():
    candidate = _candidate("line-1", evidence=("m-1",), title="两个事项标题完全相同")
    decisions = [
        {"acu_id": "acu-1", "evidence_message_ids": ["m-1", "m-2"], "inclusion_label": "must"},
        {"acu_id": "acu-2", "evidence_message_ids": ["m-1", "m-3"], "inclusion_label": "must"},
    ]
    result = evaluate_candidates([candidate], codebook=decisions, allowed_evidence_ids={"m-1"})
    assert result["status"] == "failed"
    assert result["rejected"][0]["reason"] == "ambiguous_direct_evidence_intersection"


def test_explicit_candidate_id_codebook_join_is_supported_without_text_matching():
    candidate = _candidate("line-1", evidence=("m-1",), title="标题不参与对齐")
    result = select_candidates(
        [candidate],
        codebook={"line-1": "must"},
        allowed_evidence_ids={"m-1"},
    )
    assert result["status"] == "ok"
    assert result["selected"][0]["inclusion_label"] == "must"


def test_manual_codebook_input_is_marked_oracle_not_production():
    result = select_candidates(
        [_candidate("line-1")],
        codebook={"line-1": "must"},
        allowed_evidence_ids={"m-1"},
    )
    assert result["oracle"] == {
        "manual_codebook": True,
        "automatic_inclusion_labeler": False,
        "production": False,
        "reason": "explicit_manual_codebook_or_acu_input",
    }
    assert result["formal_briefing_allowed"] is False


def test_semantic_line_without_annotation_decision_fails_closed():
    reconstruction = {
        "messages": [{"source_message_id": "m-1"}],
        "review": {
            "sample_units": [{
                "unit_ref": "unit-1",
                "content_line_candidates": [{
                    "content_line_ref": "line-1",
                    "title": "一个候选",
                    "summary_candidate": "只有语义抽取，没有代码本标注。",
                    "support_message_ids": ["m-1"],
                    "evidence_bindings": [{"statement": "候选", "message_ids": ["m-1"]}],
                    "claim_type": "chat_report",
                }],
            }],
        },
    }
    result = evaluate_candidates(
        [
            {
                **reconstruction["review"]["sample_units"][0]["content_line_candidates"][0],
                "source_unit_ref": "unit-1",
            }
        ],
        allowed_evidence_ids={"m-1"},
    )
    assert result["status"] == "failed"
    assert result["rejected"][0]["reason"] == "missing_codebook_label"
