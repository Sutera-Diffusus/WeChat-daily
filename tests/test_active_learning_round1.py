import json

from src.wechat_bridge.active_learning_round1 import (
    SOURCE,
    _debug_result_projection,
    _manifest,
    _render_review,
    _review_message_text,
    _safe_message_dom,
)


def _sample_package():
    messages = [
        {"id": "m1", "message_id": "m1", "chat_id": "chat", "timestamp": "2026-08-14T00:00:00", "message_type": "text", "content": "上下文" , "role": "context"},
        {"id": "m2", "message_id": "m2", "chat_id": "chat", "timestamp": "2026-08-14T00:01:00", "message_type": "text", "content": "核心", "role": "primary"},
        {"id": "m3", "message_id": "m3", "chat_id": "chat", "timestamp": "2026-08-14T00:02:00", "message_type": "text", "content": "后文", "role": "context"},
        {"id": "m4", "message_id": "m4", "chat_id": "chat", "timestamp": "2026-08-14T00:03:00", "message_type": "text", "content": "补充", "role": "context"},
        {"id": "m5", "message_id": "m5", "chat_id": "chat", "timestamp": "2026-08-14T00:04:00", "message_type": "text", "content": "结尾", "role": "context"},
    ]
    return {
        "package_id": "m2",
        "chat_id": "chat",
        "messages": messages,
        "core": {"message_ids": ["m2"]},
        "necessary": {"message_ids": ["m1", "m3", "m4", "m5"]},
        "recoverable": {"message_refs": [f"sqlite:messages:m{i}" for i in range(1, 6)]},
        "window_start_ref": "sqlite:messages:m1",
        "core_message_refs": ["sqlite:messages:m2"],
    }


def _payload():
    return {
        "config": {"dates": ["2026-08-14"], "packages_per_date": 1, "persistent_cap": 24, "retry": 0},
        "provider": {"id": "openai-compatible", "model": "test"},
        "authorization": {"calls_used": 1},
        "provider_calls": 1,
        "provider_calls_total": 1,
        "results": [{
            "date": "2026-08-14", "package_id": "m2", "status": "complete", "provider_call": True,
            "topic": "1", "evidence": "1", "unknown": 0, "error": "", "error_code": "",
            "output_token_budget": 8192, "output_tokens": 55, "input_tokens": 33, "token": 88,
            "message_count": 5, "topics": [{"topic_id": "t1", "label": "测试", "evidence_aliases": ["m2"]}],
            "people": [], "objects": [], "states": [], "overall_uncertainties": [], "evidence_refs": ["sqlite:messages:m2"],
            "speech_mode": {"value": "serious", "evidence_aliases": ["m2"]}, "intent": {"value": "inform", "evidence_aliases": ["m2"]},
            "core": {"message_ids": ["m2"]}, "window_start_ref": "sqlite:messages:m1", "core_message_refs": ["sqlite:messages:m2"],
            "recoverable_context": {"refs": [f"sqlite:messages:m{i}" for i in range(1, 6)], "body_free": True},
        }],
        "by_date": {"2026-08-14": []}, "daily_summary": {}, "manifest": {}, "scope": {}, "recoverable_context_summary": {},
    }


def test_projection_is_body_free_and_keeps_configured_budget():
    selection = {"debug_dates": ["2026-08-14"], "blind_dates": ["2026-08-19"], "debug_count": 1, "blind_count": 4}
    value = _debug_result_projection(_payload(), selection)
    assert value["manifest"]["sampling_unit"] == "episode_chunk"
    assert value["results"][0]["output_token_budget"] == 8192
    encoded = json.dumps(value, ensure_ascii=False)
    assert "上下文" not in encoded
    assert "reasoning" not in encoded


def test_review_contains_human_edit_fields_and_export_controls():
    value = _debug_result_projection(_payload(), {"debug_dates": ["2026-08-14"], "blind_dates": [], "debug_count": 1, "blind_count": 0})
    page = _render_review(value, {"2026-08-14": [_sample_package()]})
    for field in ("topic", "evidence", "person", "object", "state", "tone", "intent", "notes"):
        assert f'data-field="{field}"' in page
    assert "导出 human_labels.json" in page
    assert "localStorage" in page
    assert "核心" in page
    assert "黄色消息是模型指定的核心主消息" in page


def test_manifest_defaults_to_8192_and_records_round_gates(tmp_path, monkeypatch):
    monkeypatch.delenv("CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS", raising=False)
    for name in ("selection_manifest.json", "debug_results.json", "errors.json", "review.html", "human_labels.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    selection = {"debug_dates": ["2026-08-14"], "blind_dates": ["2026-08-19"], "excluded_dates": [], "blind_count": 4}
    value = _manifest(selection, _payload(), tmp_path)
    assert value["source"] == SOURCE
    assert value["output_budget"]["configured_tokens"] == 8192
    assert value["output_budget"]["effective_tokens"] == 8192
    assert value["persistent_call_limit"] == 24
    assert value["per_package_call_limit"] == 1
    assert value["retry_count"] == 0
    assert value["accuracy"] is False
    assert value["stage_b"] is False and value["stage_c"] is False
    assert value["production_blocked"] is True


def test_review_evidence_checkboxes_and_pending_correct_gate():
    payload = _payload()
    pending = dict(payload["results"][0])
    pending.update({"package_id": "pending", "status": "pending", "provider_call": False, "error_code": "semantic_invalid_json", "error": "semantic_invalid_json", "evidence_refs": []})
    payload["results"] = [payload["results"][0], pending]
    value = _debug_result_projection(payload, {"debug_dates": ["2026-08-14"], "blind_dates": [], "debug_count": 2, "blind_count": 0})
    first = _sample_package()
    second = dict(first)
    second["package_id"] = "pending"
    page = _render_review(value, {"2026-08-14": [first, second]})
    assert page.count('data-corrected-evidence="true"') == 10
    assert page.count('data-model-correct="true"') == 2
    assert page.count('data-model-correct="true" disabled') == 1
    assert page.count('data-save-card="true"') == 2
    assert "需人工补全/模型未通过" in page
    assert "corrected_evidence_refs" in page or "data-evidence-ref" in page


def test_review_text_keeps_leading_spaces_before_html_escape():
    value = "  <原文>\n下一行"
    assert _review_message_text({"content": value}) == value
    assert _safe_message_dom(value).startswith("  &lt;原文&gt;\n")
