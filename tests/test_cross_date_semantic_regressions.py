"""Offline contract tests for the human-review semantic upgrades."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.wechat_bridge.cross_date_experiment import (
    HUMAN_LABELED_REGRESSION_MARKER,
    _normalize_semantic_output,
    run_experiment,
    write_audit_summary,
    write_outputs,
)
from tests.synthetic_cross_date_semantic_regressions import (
    CONTAMINATION_MARKER,
    SAMPLE_COUNT,
    SYNTHETIC_CROSS_DATE_SEMANTIC_REGRESSIONS,
    synthetic_cross_date_regression_manifest,
)


def _model_input(text1: str, text2: str = "SYNTHETIC_CONTEXT"):
    return [
        {"alias": "m1", "speaker": "synthetic-a", "time": "t1", "text": text1, "role": "primary"},
        {"alias": "m2", "speaker": "synthetic-b", "time": "t2", "text": text2, "role": "context"},
    ]


def _new_result(**overrides):
    result = {
        "no_topic": False,
        "information_value": "substantive",
        "no_topic_evidence_aliases": [],
        "topics": [{
            "topic_id": "t1",
            "label": "synthetic topic",
            "primary_aliases": ["m1"],
            "context_aliases": ["m2"],
            "uncertainty": "low",
            "evidence_aliases": ["m1"],
        }],
        "people": [],
        "objects": [],
        "states": [],
        "speech_mode": {"value": "unknown", "evidence_aliases": []},
        "intent": {"value": "unknown", "evidence_aliases": []},
        "overall_uncertainties": [],
    }
    result.update(overrides)
    return result


def test_fixture_is_exactly_15_contaminated_and_not_accuracy_releaseable():
    assert len(SYNTHETIC_CROSS_DATE_SEMANTIC_REGRESSIONS) == SAMPLE_COUNT == 15
    assert all(row["sample_status"] == CONTAMINATION_MARKER for row in SYNTHETIC_CROSS_DATE_SEMANTIC_REGRESSIONS)
    manifest = synthetic_cross_date_regression_manifest()
    assert manifest["sample_status"] == HUMAN_LABELED_REGRESSION_MARKER
    assert manifest["accuracy_release_allowed"] is False


def test_no_topic_allows_empty_topics_and_binds_trigger_evidence():
    normalized = _normalize_semantic_output(
        _new_result(
            no_topic=True,
            information_value="none",
            topics=[],
            no_topic_evidence_aliases=["m1"],
        ),
        _model_input("SYNTHETIC_GREETING_FILLER"),
    )
    assert normalized["topics"] == []
    assert normalized["no_topic"] is True
    assert normalized["information_value"] == "none"
    assert normalized["no_topic_evidence_aliases"] == ["m1"]


def test_known_claim_cannot_use_unrelated_evidence_in_new_schema():
    with pytest.raises(ValueError, match="semantic_claim_evidence_not_specific"):
        _normalize_semantic_output(
            _new_result(
                topics=[{
                    "topic_id": "t1", "label": "quota usage",
                    "primary_aliases": ["m1"], "context_aliases": [],
                    "uncertainty": "low", "evidence_aliases": ["m2"],
                }],
            ),
            _model_input("SYNTHETIC_CORE_OTHER", "SYNTHETIC_UNRELATED"),
        )


def test_object_keeps_exact_compound_phrase_and_rejects_adjacent_split():
    normalized = _normalize_semantic_output(
        _new_result(
            topics=[{
                "topic_id": "t1", "label": "compound object",
                "primary_aliases": ["m1"], "context_aliases": [],
                "uncertainty": "low", "evidence_aliases": ["m1"],
            }],
            objects=[{
                "name_or_unknown": "SYNTHETIC_COMPOUND_OBJECT",
                "role": "product",
                "exact_noun_phrase": "SYNTHETIC_COMPOUND_OBJECT",
                "span": {"alias": "m1", "start": 0, "end": 25},
                "evidence_aliases": ["m1"],
            }],
        ),
        _model_input("SYNTHETIC_COMPOUND_OBJECT"),
    )
    assert normalized["objects"][0]["exact_noun_phrase"] == "SYNTHETIC_COMPOUND_OBJECT"
    assert normalized["objects"][0]["span"]["alias"] == "m1"

    with pytest.raises(ValueError, match="semantic_object_phrase_split"):
        _normalize_semantic_output(
            _new_result(
                topics=[{
                    "topic_id": "t1", "label": "SYNTHETIC_A",
                    "primary_aliases": ["m1"], "context_aliases": [],
                    "uncertainty": "low", "evidence_aliases": ["m1"],
                }],
                objects=[
                    {"name_or_unknown": "SYNTHETIC_A", "role": "part", "exact_noun_phrase": "SYNTHETIC_A", "span": {"alias": "m1", "start": 0, "end": 12}, "evidence_aliases": ["m1"]},
                    {"name_or_unknown": "SYNTHETIC_B", "role": "part", "exact_noun_phrase": "SYNTHETIC_B", "span": {"alias": "m1", "start": 12, "end": 23}, "evidence_aliases": ["m1"]},
                ],
            ),
            _model_input("SYNTHETIC_A_SYNTHETIC_B"),
        )


def test_speech_mode_and_intent_allow_unknown_and_require_specific_evidence():
    normalized = _normalize_semantic_output(
        _new_result(
            speech_mode={"value": "unknown", "evidence_aliases": []},
            intent={"value": "unknown", "evidence_aliases": []},
        ),
        _model_input("SYNTHETIC_UNKNOWN"),
    )
    assert normalized["speech_mode"]["value"] == "unknown"
    assert normalized["intent"]["value"] == "unknown"

    with pytest.raises(ValueError, match="semantic_speech_claim_evidence_not_specific"):
        _normalize_semantic_output(
            _new_result(speech_mode={"value": "teasing", "evidence_aliases": ["m2"]}),
            _model_input("SYNTHETIC_CORE_TEASING", "SYNTHETIC_UNRELATED"),
        )


def test_human_review_json_is_loaded_into_renderer_and_old_flags_survive(tmp_path: Path):
    payload = run_experiment(
        # A body-free fixture keeps this test offline and avoids the provider.
        __import__("src.wechat_bridge.cross_date_experiment", fromlist=["ExperimentConfig"]).ExperimentConfig(
            dates=("2026-08-20",), packages_per_date=1, persistent_cap=1,
            source="synthetic-human-review", authorization_id="synthetic-human-review",
        ),
        {"2026-08-20": [{"id": "p1"}]},
        provider=lambda *_args, **_kwargs: {"status": "ok"},
        authority_root=tmp_path / "ledger",
    )
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "human_review.json").write_text(json.dumps({
        "reviews": {
            "2026-08-20:p1": {
                "flags": {"topic_error": True, "evidence": True},
                "notes": "synthetic reviewer note",
            },
        },
        "legacy": {"relevance": True},
    }, ensure_ascii=False), encoding="utf-8")
    _json_path, html_path = write_outputs(payload, tmp_path / "out")
    page = html_path.read_text(encoding="utf-8")
    assert "主题错误" in page
    assert "synthetic reviewer note" in page
    assert "旧：证据" in page
    assert "human_review.json" in page
    assert "fetch(humanReviewFilename" in page


def test_audit_summary_records_external_human_review_hash(tmp_path: Path):
    from src.wechat_bridge.cross_date_experiment import ExperimentConfig

    payload = run_experiment(
        ExperimentConfig(
            dates=("2026-08-20",), packages_per_date=1, persistent_cap=1,
            source="synthetic-audit-human-review", authorization_id="synthetic-audit-human-review",
        ),
        {"2026-08-20": [{"id": "p1"}]},
        provider=lambda *_args, **_kwargs: {"status": "ok"},
        authority_root=tmp_path / "ledger",
    )
    output = tmp_path / "out"
    write_outputs(payload, output)
    (output / "human_review.json").write_text(json.dumps({"reviews": {"2026-08-20:p1": {"labels": ["无有效信息"], "notes": "n"}}}, ensure_ascii=False), encoding="utf-8")
    # Rebuilding the audit after the user file appears is the explicit
    # presentation-only refresh path; no provider/ledger call is involved.
    audit_path = write_audit_summary(payload, output, authority_root=tmp_path / "ledger")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["human_review"]["status"] == "loaded"
    assert audit["human_review"]["loaded_rows"] == 1
    assert audit["artifacts"]["human_review_sha256"]
