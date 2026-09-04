"""Public contract tests for the provider-free conversation reconstruction prototype."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechat_bridge.context_reconstruction import (
    SCHEMA_VERSION,
    assert_body_free,
    load_jsonl_messages,
    reconstruct_context,
    render_review_html,
    write_review_artifacts,
)
from wechat_bridge.semantic_content_lines import (
    ContentLineProtocolError,
    enrich_review_content_lines,
    validate_content_lines,
)


class _ContentLineModel:
    model_id = "synthetic-semantic-model"
    source = "synthetic"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def complete(self, stage: str, system_prompt: str, user_packet: dict[str, object], *, max_output_tokens: int) -> dict[str, object]:
        self.calls.append({"stage": stage, "system_prompt": system_prompt, "user_packet": user_packet, "max_output_tokens": max_output_tokens})
        return self.payload


def _message(
    message_id: str,
    text: str,
    timestamp: str,
    *,
    chat_id: str = "chat-direct",
    chat_type: str = "direct",
    speaker_id: str = "person-a",
    speaker_name: str = "合成人甲",
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "message_id": message_id,
        "account_id": "account-synthetic",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "speaker_id": speaker_id,
        "speaker_name": speaker_name,
        "timestamp": timestamp,
        "content": text,
        "message_type": "text",
        "split": "development",
    }
    row.update(extra)
    return row


def test_reconstruct_context_is_body_free_and_keeps_direct_group_scope_separate() -> None:
    rows = [
        _message("d1", "同一个词，问一下项目安排？", "2026-08-24T23:58:00+08:00"),
        _message("d2", "继续讨论同一个词，明天确认。", "2026-08-25T00:05:00+08:00", speaker_id="person-b", speaker_name="合成人乙", reply_to_message_id="d1"),
        _message("g1", "同一个词，群里分享方案。", "2026-08-25T09:00:00+08:00", chat_id="chat-group", chat_type="group", is_group=True, member_count=3, speaker_id="person-c", speaker_name="合成人丙"),
        _message("g2", "收到，讨论一下？", "2026-08-25T09:01:00+08:00", chat_id="chat-group", chat_type="group", is_group=True, member_count=3, speaker_id="person-d", speaker_name="合成人丁"),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["provider_used"] is False
    assert result["provider_calls"] == 0
    assert result["frozen_read"] is False
    assert {chat["chat_type"] for chat in result["chats"]} == {"direct", "group"}
    assert len(result["episodes"]) == 2
    assert len(result["threads"]) == 2
    assert result["counts"]["cross_day_episode_count"] == 1
    assert any(view["thread_refs"] for view in result["views"].values())
    assert_body_free(result)
    assert all(key not in {"content", "text", "body", "message_text", "raw_message"} for row in result["messages"] for key in row)


def test_cross_day_reply_is_one_open_episode_and_silence_does_not_resolve_it() -> None:
    rows = [
        _message("m1", "项目看板需要排查", "2026-08-25T23:59:00+08:00"),
        _message("m2", "我明天继续看", "2026-08-26T09:00:00+08:00", speaker_id="person-b", reply_to_message_id="m1"),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-26")

    episode = result["episodes"][0]
    assert len(result["episodes"]) == 1
    assert episode["cross_day_candidate"] is True
    assert episode["start_boundary"]["status"] == "open"
    assert episode["end_boundary"]["status"] == "open"
    assert "resolved" not in json.dumps(episode, ensure_ascii=False)
    assert any(edge["relation"] == "explicit_reply" for edge in episode["continuity_edges"])
    assert result["views"]["today"]["thread_refs"]
    assert result["views"]["yesterday"]["thread_refs"]


def test_signals_are_candidates_and_layers_allow_unclassified_and_chitchat_flow() -> None:
    rows = [
        _message("greet", "你好", "2026-08-25T09:00:00+08:00"),
        _message("ask", "请问项目什么时候发布？", "2026-08-25T09:01:00+08:00", speaker_id="person-b"),
        _message("empty", "[图片]", "2026-08-25T09:02:00+08:00", speaker_id="person-a", message_type="image"),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    labels = {signal["label"] for segment in result["segments"] for signal in segment["interaction_signals"]}
    assert {"greeting", "question", "request"} <= labels
    assert all(signal["candidate_only"] is True and signal["final_semantics"] is None for segment in result["segments"] for signal in segment["interaction_signals"])
    assert any(segment["topic_layer"] == "chitchat_flow" for segment in result["segments"])
    assert any("topic_unclassified" in segment["uncertainties"] for segment in result["segments"])
    assert any(segment["explicit_matters"] for segment in result["segments"])


def test_unavailable_media_is_visible_but_not_semantic_evidence() -> None:
    rows = [
        _message("image", "[图片]", "2026-08-25T09:00:00+08:00", message_type="image", media_state="unavailable"),
        _message("voice", "[语音]", "2026-08-25T09:01:00+08:00", message_type="audio"),
        _message("file", "[文件]", "2026-08-25T09:02:00+08:00", message_type="file", extracted_text="可核验的文件摘要"),
        _message("url", "https://example.test/a", "2026-08-25T09:03:00+08:00", message_type="link", link_resolved=False),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    media = {row["source_message_id"]: row["media"] for row in result["messages"]}
    assert media["image"]["status"] == "unavailable"
    assert media["image"]["semantic_evidence_eligible"] is False
    assert media["voice"]["missing_reason"] == "transcription_not_available"
    assert media["file"]["semantic_evidence_eligible"] is True
    assert media["url"]["resolved"] is False
    image_segment = next(segment for segment in result["segments"] if segment["message_ref"] == next(row["message_ref"] for row in result["messages"] if row["source_message_id"] == "image"))
    assert image_segment["evidence_refs"] == []
    assert "media_content_unavailable" in image_segment["uncertainties"]


def test_media_derived_text_enters_candidate_recall_with_explicit_provenance() -> None:
    rows = [
        _message(
            "file-derived",
            "[文件]",
            "2026-08-25T09:00:00+08:00",
            message_type="file",
            extracted_text="接口恢复时间需要确认",
        ),
        _message(
            "audio-derived",
            "[语音]",
            "2026-08-25T09:01:00+08:00",
            message_type="audio",
            transcript="请确认接口恢复时间",
            speaker_id="person-b",
        ),
        _message(
            "audio-verified-redacted",
            "[语音]",
            "2026-08-25T09:02:00+08:00",
            message_type="audio",
            media_state="redacted_transcript",
            redacted_text="请确认接口恢复时间",
            speaker_id="person-c",
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")
    message_refs = {row["source_message_id"]: row["message_ref"] for row in result["messages"]}

    for source_id, source_field in (
        ("file-derived", "extracted_text"),
        ("audio-derived", "transcript"),
        ("audio-verified-redacted", "redacted_text"),
    ):
        segment = next(item for item in result["segments"] if item["message_ref"] == message_refs[source_id])
        assert segment["media"]["semantic_evidence_eligible"] is True
        assert segment["media"]["semantic_text_source"] == source_field
        assert segment["span_basis"] == source_field
        assert segment["content_refs"][0]["source_field"] == source_field
        assert segment["content_refs"][0]["content_status"] == "available"
        assert segment["content_refs"][0]["evidence_refs"] == [message_refs[source_id]]
        assert any(signal["label"] in {"question", "request", "discussion"} for signal in segment["interaction_signals"])


def test_soft_topic_marker_keeps_a_shared_object_in_one_episode() -> None:
    rows = [
        _message("same-object-a", "接口进度今天确认。", "2026-08-25T09:00:00+08:00"),
        _message(
            "same-object-b",
            "对了，继续看接口进度。",
            "2026-08-25T09:01:00+08:00",
            speaker_id="person-b",
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    assert len(result["episodes"]) == 1
    assert result["episodes"][0]["continuity_edges"][0]["topic_shift_cue"] is False


def test_soft_topic_marker_still_splits_when_the_new_object_has_no_overlap() -> None:
    rows = [
        _message("new-object-a", "接口进度今天确认。", "2026-08-25T09:00:00+08:00"),
        _message(
            "new-object-b",
            "对了，电影什么时候上映？",
            "2026-08-25T09:01:00+08:00",
            speaker_id="person-b",
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    assert len(result["episodes"]) == 2
    assert any("topic_shift_candidate" in episode["boundary_reasons"] for episode in result["episodes"])


def test_review_writer_escapes_bodies_and_manifest_stays_body_free(tmp_path: Path) -> None:
    rows = [_message("m1", "<script>alert('x')</script> 你好", "2026-08-25T09:00:00+08:00")]
    result = reconstruct_context(rows, reference_date="2026-08-25")
    paths = write_review_artifacts(result, tmp_path, source_messages=rows)

    html = paths["review_html"].read_text(encoding="utf-8")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    reconstruction = json.loads(paths["reconstruction"].read_text(encoding="utf-8"))
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "<script>alert" not in html
    assert manifest["body_free"] is True
    assert manifest["provider_used"] is False
    assert_body_free(manifest)
    assert_body_free(reconstruction)
    assert "<script>" not in json.dumps(manifest, ensure_ascii=False)
    assert paths["manifest"].exists() and paths["reconstruction"].exists()


def test_development_loader_refuses_frozen_paths_and_rows(tmp_path: Path) -> None:
    frozen_dir = tmp_path / "frozen_test"
    frozen_dir.mkdir()
    frozen_path = frozen_dir / "messages.jsonl"
    frozen_path.write_text(json.dumps({"message_id": "x", "split": "development"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen"):
        load_jsonl_messages(frozen_path)


def test_cross_time_same_course_flow_links_internal_episodes_without_merging_them() -> None:
    rows = [
        _message(
            "course-morning",
            "选课方案今天先确认两个名额。",
            "2026-08-25T09:00:00+08:00",
        ),
        _message(
            "course-evening",
            "选课方案继续确认，明天再提交。",
            "2026-08-25T18:00:00+08:00",
            speaker_id="person-b",
            conversation_boundary=True,
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    assert len(result["episodes"]) == 2
    flows = [flow for flow in result["conversation_flows"] if len(flow.get("episode_refs") or ()) == 2]
    assert len(flows) == 1
    assert flows[0]["internal_episode_boundaries_preserved"] is True
    flow_refs = {thread.get("flow_ref") for thread in result["threads"]}
    assert len(flow_refs) == 1
    assert flow_refs != {None}


def test_short_group_gets_review_context_envelope_without_changing_core_episode() -> None:
    rows = [
        _message(
            "group-lead-in",
            "先说接口进度，大家早。",
            "2026-08-25T09:00:00+08:00",
            chat_id="chat-group-context",
            chat_type="group",
            is_group=True,
            member_count=4,
        ),
        _message(
            "group-core",
            "请问接口什么时候可以确认？",
            "2026-08-25T09:01:00+08:00",
            chat_id="chat-group-context",
            chat_type="group",
            is_group=True,
            member_count=4,
            conversation_boundary=True,
        ),
        _message(
            "group-follow-up",
            "收到，我继续跟进。",
            "2026-08-25T09:02:00+08:00",
            chat_id="chat-group-context",
            chat_type="group",
            is_group=True,
            member_count=4,
            speaker_id="person-b",
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")

    core_ref = next(row["message_ref"] for row in result["messages"] if row["source_message_id"] == "group-core")
    thread = next(thread for thread in result["threads"] if core_ref in thread["message_refs"])
    envelope = next(item for item in result["review_context_envelopes"] if item["thread_ref"] == thread["thread_ref"])
    assert envelope["core_message_refs"] == thread["message_refs"]
    assert envelope["core_message_count"] == len(thread["message_refs"])
    assert envelope["context_message_count"] >= 1
    lead_in_ref = next(row["message_ref"] for row in result["messages"] if row["source_message_id"] == "group-lead-in")
    assert lead_in_ref in envelope["context_message_refs"]
    assert envelope["safety_cap_is_not_semantic_boundary"] is True


def test_local_signal_density_is_not_information_value() -> None:
    result = reconstruct_context([
        _message("density", "请确认选课安排，明天提交。", "2026-08-25T09:00:00+08:00"),
    ], reference_date="2026-08-25")

    information = result["threads"][0]["information_value"]
    assert information["label"] == "unknown"
    assert information["score"] is None
    assert information["status"] == "unknown_pending_model"
    assert information["signal_density_score"] is not None
    assert "local_signal_density_is_not_information_value" in information["uncertainties"]


def test_parallel_domain_and_subscription_strands_do_not_merge_by_time() -> None:
    rows = [
        _message(
            "domain-1",
            "请帮忙购买 domain 注册。",
            "2026-08-25T09:00:00+08:00",
            chat_id="parallel-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            speaker_id="person-domain",
        ),
        _message(
            "subscription-1",
            "subscription 订阅套餐怎么续？",
            "2026-08-25T09:00:01+08:00",
            chat_id="parallel-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            speaker_id="person-subscription",
        ),
        _message(
            "domain-2",
            "domain 购买价格需要确认。",
            "2026-08-25T09:00:02+08:00",
            chat_id="parallel-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            speaker_id="person-domain",
        ),
        _message(
            "subscription-2",
            "subscription 订阅到期时间请确认。",
            "2026-08-25T09:00:03+08:00",
            chat_id="parallel-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            speaker_id="person-subscription",
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")
    by_ref = {row["message_ref"]: row for row in result["messages"]}
    episode_source_sets = [
        {by_ref[ref]["source_message_id"] for ref in episode["message_refs"]}
        for episode in result["episodes"]
    ]
    assert len(episode_source_sets) == 2
    assert {"domain-1", "domain-2"} in episode_source_sets
    assert {"subscription-1", "subscription-2"} in episode_source_sets
    assert any("parallel_topic_strand_candidate" in episode["boundary_reasons"] for episode in result["episodes"])


def test_review_sample_units_are_disjoint_and_merge_direct_flow_card() -> None:
    rows = [
        _message("course-a", "选课方案今天确认。", "2026-08-25T09:00:00+08:00", speaker_id="course-a"),
        _message("course-b", "选课方案晚上继续确认。", "2026-08-25T18:00:00+08:00", speaker_id="course-b", conversation_boundary=True),
    ]
    for index in range(5):
        rows.append(_message(
            f"group-{index}",
            f"群聊候选讨论对象 {index}。",
            f"2026-08-25T0{index + 1}:00:00+08:00",
            chat_id="review-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            speaker_id=f"group-speaker-{index}",
        ))
    result = reconstruct_context(rows, reference_date="2026-08-25")
    units = result["review"]["sample_units"]
    displayed = [set(unit.get("display_message_refs") or ()) for unit in units]
    assert all(not (left & right) for index, left in enumerate(displayed) for right in displayed[index + 1:])
    direct_units = [unit for unit in units if unit.get("chat_type") == "direct"]
    assert len(direct_units) == 1
    assert len(direct_units[0].get("episode_refs") or ()) == 2
    assert result["review"]["sample_audit"]["direct_card_count"] == 1


def test_review_card_keeps_ai_workflow_and_forum_registration_as_separate_content_lines() -> None:
    rows = [
        _message("unclear-1", "没必要用。", "2026-08-25T09:00:00+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
        _message("unclear-2", "现在已经不用这个了。", "2026-08-25T09:00:10+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
        _message("ai-1", "Sol 跑了两个小时，速度太慢。", "2026-08-25T09:00:20+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
        _message("ai-2", "可以用 subagent 提速吗？", "2026-08-25T09:00:30+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
        _message("ai-3", "Superpowers 可以卸载。", "2026-08-25T09:00:40+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
        _message("ai-4", "这个工具会限制模型能力。", "2026-08-25T09:00:50+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
        _message("forum-image", "[图片]", "2026-08-25T09:01:00+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5, message_type="image"),
        _message("forum-1", "这论坛注册流程怎么这么抽象。", "2026-08-25T09:01:10+08:00", chat_id="multi-line-group", chat_type="group", is_group=True, member_count=5),
    ]
    base = reconstruct_context(rows, reference_date="2026-08-25")
    assert all(not unit.get("content_line_candidates") for unit in base["review"]["sample_units"])
    model = _ContentLineModel({"content_lines": [
        {
            "title": "工具性能与代理工作流",
            "summary": "讨论运行速度、代理提速、工具卸载及能力限制。",
            "evidence_bindings": [
                {"statement": "Sol 运行耗时较长。", "message_ids": ["ai-1"]},
                {"statement": "群内讨论了代理提速、工具卸载和能力限制。", "message_ids": ["ai-2", "ai-3", "ai-4"]},
            ],
            "primary_message_ids": ["ai-1", "ai-2", "ai-3", "ai-4"],
            "context_message_ids": ["unclear-1", "unclear-2"],
            "importance": "medium",
            "key_topic_candidate": True,
            "importance_reasons": ["sustained_discussion"],
            "claim_type": "mixed",
            "external_verification": "recommended",
            "uncertainties": [],
        },
        {
            "title": "注册流程体验",
            "summary": "反馈注册流程存在阻碍；图片不可用，具体卡点未知。",
            "evidence_bindings": [{"statement": "有人反馈论坛注册流程体验不佳。", "message_ids": ["forum-1"]}],
            "primary_message_ids": ["forum-1"],
            "context_message_ids": ["forum-image"],
            "importance": "low",
            "key_topic_candidate": False,
            "importance_reasons": ["brief_side_topic"],
            "claim_type": "reported_experience",
            "external_verification": "not_applicable",
            "uncertainties": ["unavailable_image"],
        },
    ]})
    result = enrich_review_content_lines(base, rows, model)
    unit = next(unit for unit in result["review"]["sample_units"] if "forum-1" in unit.get("core_message_ids", ()))
    lines = {line["title"]: line for line in unit["content_line_candidates"]}

    assert set(lines) == {"工具性能与代理工作流", "注册流程体验"}
    assert lines["工具性能与代理工作流"]["key_topic_candidate"] is True
    assert lines["工具性能与代理工作流"]["importance_candidate"] == "medium"
    assert set(lines["工具性能与代理工作流"]["support_message_ids"]) == {"ai-1", "ai-2", "ai-3", "ai-4"}
    assert set(lines["工具性能与代理工作流"]["context_message_ids"]) == {"unclear-1", "unclear-2"}
    assert lines["注册流程体验"]["key_topic_candidate"] is False
    assert lines["注册流程体验"]["importance_candidate"] == "low"
    assert lines["注册流程体验"]["support_message_ids"] == ["forum-1"]
    assert lines["注册流程体验"]["context_message_ids"] == ["forum-image"]
    assert lines["注册流程体验"]["claim_status"] == "chat_evidence_only"
    assert lines["注册流程体验"]["external_verification_status"] == "not_performed"
    assert result["provider_calls"] == 1


def test_short_subscription_exchange_has_topic_but_is_not_promoted_to_key_topic() -> None:
    rows = [
        _message("quota-1", "200 美元订阅额度用完了？", "2026-08-25T10:00:00+08:00", chat_id="short-topic-group", chat_type="group", is_group=True, member_count=3),
        _message("quota-2", "可以去中转站充值续用。", "2026-08-25T10:00:10+08:00", chat_id="short-topic-group", chat_type="group", is_group=True, member_count=3),
        _message("quota-3", "也可以等额度刷新。", "2026-08-25T10:00:20+08:00", chat_id="short-topic-group", chat_type="group", is_group=True, member_count=3),
    ]
    base = reconstruct_context(rows, reference_date="2026-08-25")
    model = _ContentLineModel({"content_lines": [{
        "title": "额度耗尽后的续用方案",
        "summary": "讨论额度用完后的充值和等待刷新方案。",
        "evidence_bindings": [{"statement": "群内讨论了充值续用和等待额度刷新。", "message_ids": ["quota-1", "quota-2", "quota-3"]}],
        "primary_message_ids": ["quota-1", "quota-2", "quota-3"],
        "context_message_ids": [],
        "importance": "low",
        "key_topic_candidate": True,
        "importance_reasons": ["brief_exchange"],
        "claim_type": "mixed",
        "external_verification": "recommended",
        "uncertainties": [],
    }]})
    result = enrich_review_content_lines(base, rows, model)
    unit = next(unit for unit in result["review"]["sample_units"] if "quota-1" in unit.get("core_message_ids", ()))
    line = unit["content_line_candidates"][0]

    assert line["topic_detected"] is True
    assert line["evidence_message_count"] == 3
    assert line["importance_candidate"] == "low"
    assert line["key_topic_candidate"] is False
    assert line["key_topic_status"] == "not_promoted"
    assert line["key_topic_reason_codes"] == ["brief_exchange"]


def test_semantic_review_has_no_default_card_cap() -> None:
    rows = [
        _message(
            f"message-{index}",
            f"第 {index} 个聊天提出一项独立问题。",
            f"2026-08-25T{index:02d}:00:00+08:00",
            chat_id=f"chat-{index}",
        )
        for index in range(8)
    ]
    base = reconstruct_context(rows, reference_date="2026-08-25")

    class DynamicModel:
        model_id = "dynamic-semantic-model"
        source = "synthetic"

        def complete(self, stage, system_prompt, user_packet, *, max_output_tokens):
            message_id = user_packet["messages"][0]["message_id"]
            return {"content_lines": [{
                "title": "独立问题待处理",
                "summary": "该聊天提出了一项独立问题，处理结果尚未出现。",
                "evidence_bindings": [{"statement": "聊天中出现一项问题。", "message_ids": [message_id]}],
                "primary_message_ids": [message_id],
                "context_message_ids": [],
                "importance": "low",
                "key_topic_candidate": False,
                "importance_reasons": ["brief_exchange"],
                "claim_type": "question",
                "external_verification": "not_applicable",
                "uncertainties": ["outcome_unknown"],
            }]}

    result = enrich_review_content_lines(base, rows, DynamicModel())

    assert len(base["review"]["sample_units"]) == 8
    assert result["provider_calls"] == 8
    assert result["review"]["semantic_content_line_extraction"]["skipped_by_card_limit_count"] == 0
    assert all(unit["content_line_candidates"] for unit in result["review"]["sample_units"])


def test_content_line_contract_rejects_unbound_evidence_and_mechanical_editorial_copy() -> None:
    base = {
        "title": "工具调整带来新变化",
        "summary": "工具已经调整，后续影响仍待观察。",
        "evidence_bindings": [{"statement": "工具已经调整。", "message_ids": ["m1"]}],
        "primary_message_ids": ["m1", "m2"],
        "context_message_ids": [],
        "importance": "medium",
        "key_topic_candidate": False,
        "importance_reasons": ["reported_change"],
        "claim_type": "chat_report",
        "external_verification": "recommended",
        "uncertainties": [],
    }
    with pytest.raises(ContentLineProtocolError, match="unbound_primary"):
        validate_content_lines({"content_lines": [base]}, allowed_message_ids=["m1", "m2"])

    mechanical = dict(base)
    mechanical["primary_message_ids"] = ["m1"]
    mechanical["summary"] = "这不是一次普通调整，而是工作方式的全面重塑。"
    with pytest.raises(ContentLineProtocolError, match="mechanical_editorial_template"):
        validate_content_lines({"content_lines": [mechanical]}, allowed_message_ids=["m1"])


def test_context_envelope_does_not_pull_unrelated_tool_and_application_turns() -> None:
    rows = [
        _message(
            "core-topic",
            "请讨论 deploy pipeline 的接口问题。",
            "2026-08-25T09:00:00+08:00",
            chat_id="pollution-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            conversation_boundary=True,
        ),
        _message(
            "claude-noise",
            "Claude 版本怎么重置？",
            "2026-08-25T09:01:00+08:00",
            chat_id="pollution-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            conversation_boundary=True,
        ),
        _message(
            "gpt-noise",
            "GPT 重置之后再看。",
            "2026-08-25T09:02:00+08:00",
            chat_id="pollution-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            conversation_boundary=True,
        ),
        _message(
            "app-noise",
            "应用开发继续推进。",
            "2026-08-25T09:03:00+08:00",
            chat_id="pollution-group",
            chat_type="group",
            is_group=True,
            member_count=4,
            conversation_boundary=True,
        ),
    ]
    result = reconstruct_context(rows, reference_date="2026-08-25")
    core_ref = next(row["message_ref"] for row in result["messages"] if row["source_message_id"] == "core-topic")
    thread = next(thread for thread in result["threads"] if core_ref in thread["message_refs"])
    envelope = next(item for item in result["review_context_envelopes"] if item["thread_ref"] == thread["thread_ref"])
    context_ids = {
        row["source_message_id"]
        for row in result["messages"]
        if row["message_ref"] in set(envelope["context_message_refs"])
    }
    assert not context_ids & {"claude-noise", "gpt-noise", "app-noise"}
    assert envelope["time_proximity_alone_is_insufficient"] is True
