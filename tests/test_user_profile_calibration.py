from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechat_bridge.user_profile_calibration import build_sample_manifest, generate_calibration_site


def _fixture() -> dict:
    return {
        "review": {
            "sample_units": [
                {
                    "chat_type": "group",
                    "chat_ref": "chat-1",
                    "unit_ref": "unit-1",
                    "content_line_extraction": {"status": "complete", "content_line_count": 2},
                    "content_line_candidates": [
                        {
                            "content_line_ref": "line-1",
                            "title": "项目方案讨论",
                            "summary_candidate": "讨论下一步计划和风险。",
                            "importance_candidate": "high",
                            "claim_type": "decision",
                            "external_verification_status": "not_performed",
                            "support_message_ids": ["MESSAGE_001"],
                            "uncertainties": ["时间未明确"],
                        },
                        {
                            "content_line_ref": "line-2",
                            "title": "轻松话题",
                            "summary_candidate": "分享一个有趣的梗。",
                            "importance_candidate": "low",
                            "claim_type": "opinion",
                            "external_verification_status": "not_required",
                            "support_message_ids": ["MESSAGE_002"],
                        },
                    ],
                }
            ]
        }
    }


def test_manifest_is_body_free_and_single_date() -> None:
    manifest = build_sample_manifest(_fixture(), "fixture.json")
    assert manifest["body_free"] is True
    assert manifest["date_coverage"] == ["2026-08-25"]
    assert manifest["card_count"] == 2
    assert manifest["sample_count"] == 2
    assert manifest["algorithm_version"] == "user-profile-calibration-v3"
    assert manifest["maximum_comparisons"] == 1
    assert manifest["source_card_count"] == 2
    assert manifest["collapsed_card_count"] == 0
    assert manifest["sample_id"].startswith("user-profile-calibration-v3|")
    assert manifest["contains_original_chat_body"] is False
    assert set(manifest["cards"][0]) == {
        "card_id", "unit_ref", "chat_ref", "title", "summary", "date",
        "importance", "claim_type", "value_dimension_candidates", "topic_family",
    }
    serialized = json.dumps(manifest, ensure_ascii=False).lower()
    for forbidden in ("message_body", "raw_body", "sender_name", "api_key", "secret"):
        assert forbidden not in serialized


def test_generator_writes_offline_adaptive_calibrator(tmp_path: Path) -> None:
    source = tmp_path / "reconstruction.json"
    source.write_text(json.dumps(_fixture(), ensure_ascii=False), encoding="utf-8")
    paths = generate_calibration_site(source, tmp_path / "site")
    assert set(paths) == {"index", "manifest", "readme"}
    html = paths["index"].read_text(encoding="utf-8")
    assert "connect-src 'none'" in html
    assert "localStorage" in html
    assert "function nextPair()" in html
    assert "if(usedPair(a.card_id,b.card_id))continue" in html
    assert "if(!unresolved.size)return null" in html
    assert "s.exposure[a.card_id]||0)>0" in html
    assert "titleOverlap(a.title,b.title)" in html
    assert "wechat-user-profile-calibration-v3" in html
    assert "sampleId!==manifest.sample_id" in html
    assert "function coverageGaps()" in html
    assert "每张卡只出现一次" in html
    assert "explicit_non_preference" in html
    assert "都重要" in html and "都不重要" in html
    assert "无法判断" in html and "跳过" in html
    assert "撤销上一题" in html and "导出 JSON" in html
    assert "原因选择" in html and "取舍归纳" in html and "证据不足" in html
    assert "证据次数" in html and "置信度：" in html and "适用范围" in html
    assert "选择记录引用" in html
    assert "data-disable" in html and "data-delete" in html
    assert "conclusion_status" in html and "deleted" in html and "disabled" in html
    assert "点击和停留时间不会参与判断" in html
    assert "当前样本仅来自 2026-08-25" in html
    lowered = html.lower()
    assert "fetch(" not in lowered and "xmlhttprequest" not in lowered
    assert "websocket" not in lowered and "<script src=" not in lowered


def test_failed_units_are_excluded() -> None:
    fixture = _fixture()
    fixture["review"]["sample_units"].append(
        {
            "unit_ref": "failed-unit",
            "content_line_extraction": {"status": "failed"},
            "content_line_candidates": [{"content_line_ref": "bad", "title": "不应出现"}],
        }
    )
    manifest = build_sample_manifest(fixture, "fixture.json")
    assert [card["card_id"] for card in manifest["cards"]] == ["line-1", "line-2"]


def test_embedded_manifest_escapes_script_breakout(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["review"]["sample_units"][0]["content_line_candidates"][0]["summary_candidate"] = (
        "</script><script>alert(1)</script>"
    )
    source = tmp_path / "reconstruction.json"
    source.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    html = generate_calibration_site(source, tmp_path / "site")["index"].read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html


def test_generic_participant_word_does_not_imply_important_people() -> None:
    fixture = _fixture()
    fixture["review"]["sample_units"][0]["content_line_candidates"][0]["title"] = "参与者讨论"
    fixture["review"]["sample_units"][0]["content_line_candidates"][0]["summary_candidate"] = "参与者交换观点。"
    manifest = build_sample_manifest(fixture, "fixture.json")
    assert "people" not in manifest["cards"][0]["value_dimension_candidates"]


def test_requires_two_content_lines() -> None:
    with pytest.raises(ValueError, match="at least two"):
        build_sample_manifest({"review": {"sample_units": []}}, "empty.json")


def test_duplicate_semantic_cards_are_collapsed() -> None:
    fixture = _fixture()
    duplicate = dict(fixture["review"]["sample_units"][0]["content_line_candidates"][0])
    duplicate["content_line_ref"] = "line-duplicate"
    fixture["review"]["sample_units"][0]["content_line_candidates"].append(duplicate)
    manifest = build_sample_manifest(fixture, "fixture.json")
    assert manifest["source_card_count"] == 3
    assert manifest["card_count"] == 2
    assert manifest["collapsed_card_count"] == 1
