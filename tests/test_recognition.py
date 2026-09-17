import json
from datetime import datetime, timedelta, timezone

from wechat_bridge.analysis import analyze_messages, build_ai_context, _build_context_windows


START = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _message(
    message_id,
    chat,
    content,
    minutes,
    *,
    sender="联系人",
    group=True,
    message_type="text",
):
    return {
        "message_id": message_id,
        "chat_id": chat,
        "chat_name": chat,
        "sender_name": sender,
        "content": content,
        "timestamp": (START + timedelta(minutes=minutes)).isoformat(),
        "is_self": False,
        "is_group": group,
        "message_type": message_type,
    }


def test_noise_is_layered_without_removing_raw_message_accounting():
    messages = [
        _message("reaction-1", "模型群", "哈哈哈哈", 0, sender="甲"),
        _message("reaction-2", "模型群", "+1", 1, sender="乙"),
        _message("ack-1", "模型群", "收到", 2, sender="丙"),
        _message("greeting-1", "模型群", "早上好", 3, sender="丁"),
        _message("media-1", "模型群", "[图片]", 4, sender="戊", message_type="image"),
        _message("content-1", "模型群", "这个方案的问题在于接口会超时，明天需要排查。", 5, sender="己"),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))

    noise_by_type = {item["kind"]: item["count"] for item in result["quality"]["noise_by_type"]}
    assert result["summary"]["accounted_messages"] == len(messages)
    assert result["quality"]["noise_total"] == 5
    assert noise_by_type["reaction"] == 2
    assert noise_by_type["acknowledgement"] == 1
    assert noise_by_type["greeting"] == 1
    assert noise_by_type["media_placeholder"] == 1
    surfaced_ids = {
        item.get("message_id")
        for item in result["discoveries"] + result["actions"]
    }
    assert not surfaced_ids.intersection({"reaction-1", "reaction-2", "ack-1", "greeting-1", "media-1"})
    assert {item["message_id"] for item in result["noise_samples"]} >= {
        "reaction-1", "reaction-2", "ack-1", "greeting-1", "media-1",
    }


def test_context_window_does_not_import_adjacent_parallel_topic():
    messages = [
        _message("model-1", "混合群", "模型部署的日志已经上传，接口返回 500。", 0, sender="甲"),
        _message("course-1", "混合群", "选课安排的截止时间是周五，大家记得看教务系统。", 1, sender="乙"),
        _message("generic-request", "混合群", "请确认一下", 2, sender="丙"),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))

    suppressed = next(item for item in result["suppressed_candidates"] if item["message_id"] == "generic-request")
    assert suppressed["candidate_state"] == "context_needed"
    assert suppressed["context_message_ids"] == []
    assert result["actions"] == []


def test_repeated_phrase_with_group_reactions_is_a_cautious_meme_candidate():
    messages = [
        _message("meme-1", "复古群", "未来古法", 0, sender="甲"),
        _message("meme-r1", "复古群", "哈哈哈哈", 1, sender="乙"),
        _message("meme-2", "复古群", "未来古法", 2, sender="丙"),
        _message("meme-r2", "复古群", "笑死", 3, sender="乙"),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))

    assert len(result["meme_candidates"]) == 1
    candidate = result["meme_candidates"][0]
    assert candidate["kind"] == "inside_joke"
    assert candidate["occurrence_count"] == 2
    assert candidate["participant_count"] == 2
    assert candidate["reaction_count"] == 2
    assert set(candidate["message_ids"]) == {"meme-1", "meme-2"}
    assert set(candidate["reaction_message_ids"]) == {"meme-r1", "meme-r2"}
    assert len(candidate["evidence"]) == 4
    assert all(item["content"] for item in candidate["evidence"])
    assert result["actions"] == []
    assert {item["kind"] for item in result["discoveries"]} == {"meme"}

    ai_context = build_ai_context(messages)
    assert any(item["candidate_type"] == "meme" for item in ai_context)
    assert not any(item["content"] in {"哈哈哈哈", "笑死"} for item in ai_context)
    json.dumps(result, ensure_ascii=False)


def test_repeated_acknowledgements_and_operational_requests_are_not_memes():
    messages = [
        _message("ack-1", "工作群", "收到", 0, sender="甲"),
        _message("ack-2", "工作群", "收到", 1, sender="乙"),
        _message("task-1", "工作群", "请明天确认报价", 2, sender="甲"),
        _message("task-2", "工作群", "请明天确认报价", 3, sender="乙"),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))

    assert result["meme_candidates"] == []


def test_old_explicit_reply_reopens_only_the_referenced_thread():
    messages = [
        _message("old-quote", "混合群", "请明天确认报价。", 0, sender="甲"),
        _message("inserted-topic", "混合群", "模型部署的日志已经上传，服务返回 500。", 120, sender="乙"),
        _message(
            "old-reply",
            "混合群",
            "请明天确认这个报价。",
            121,
            sender="丙",
        ),
    ]
    messages[-1]["reply_to_message_id"] = "old-quote"

    windows = _build_context_windows(messages)
    assert [item["message_id"] for item in windows[id(messages[-1])]] == ["old-quote"]

    result = analyze_messages(messages, START, START + timedelta(days=1))
    reply_action = next(item for item in result["actions"] if item["message_id"] == "old-reply")
    assert reply_action["context_message_ids"] == ["old-quote"]
    assert reply_action["context_relations"] == [{"message_id": "old-quote", "relation": "挖坟回复"}]
    assert reply_action["revived_thread"] is True
    assert reply_action["explicit_thread"] is True


def test_ambiguous_request_is_not_promoted_by_a_shared_generic_verb():
    messages = [
        _message("task", "工作群", "请明天确认报价。", 0, sender="甲"),
        _message("vague", "工作群", "请确认一下。", 2, sender="丙"),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))
    assert [item["message_id"] for item in result["actions"]] == ["task"]
    suppressed = next(item for item in result["suppressed_candidates"] if item["message_id"] == "vague")
    assert suppressed["candidate_state"] == "context_needed"
    assert suppressed["expression_status"] == "fragment"
    assert suppressed["context_message_ids"] == []


def test_context_backed_colloquial_reference_can_be_reviewed_without_silent_completion():
    messages = [
        _message("incident", "技术群", "接口返回 500，需要排查。", 0, sender="甲"),
        _message("followup", "技术群", "这个接口要怎么处理？", 1, sender="乙"),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))
    action = next(item for item in result["actions"] if item["message_id"] == "followup")
    assert action["context_message_ids"] == ["incident"]
    assert action["expression_status"] == "context_dependent"
    assert action["expression_confidence"] >= 80
    assert "自动补全" not in action["reason"]


def test_generic_fragment_does_not_jump_over_an_unrelated_strong_topic():
    messages = [
        _message("old-topic", "混合群", "请明天确认报价。", 0, sender="甲"),
        _message("inserted-topic", "混合群", "模型部署的日志已经上传，服务返回 500。", 1, sender="乙"),
        _message("fragment", "混合群", "继续处理一下。", 2, sender="丙"),
    ]

    windows = _build_context_windows(messages)
    fragment_context_ids = [item["message_id"] for item in windows[id(messages[-1])]]
    assert "old-topic" not in fragment_context_ids
    assert fragment_context_ids == ["inserted-topic"]


def test_many_to_few_funnel_keeps_every_input_row_and_exposes_reversible_layers():
    messages = [
        _message("noise", "混合群", "哈哈哈", 0, sender="甲"),
        _message("action", "混合群", "请明天确认报价。", 1, sender="乙"),
        _message("fragment", "混合群", "请确认一下。", 2, sender="丙"),
        _message("archive", "混合群", "最近在整理项目资料，先放这里。", 3, sender="丁"),
    ]
    messages[-1].pop("message_id")

    result = analyze_messages(messages, START, START + timedelta(days=1))
    ledger = result["retention_ledger"]
    invariant = result["funnel"]["invariant"]

    assert len(ledger) == len(messages)
    assert invariant["all_rows_retained"] is True
    assert invariant["missing_row_ids"] == []
    assert invariant["extra_row_ids"] == []
    assert {row["row_id"] for row in ledger} == {"noise", "action", "fragment", "row:3"}
    assert {row["layer"] for row in ledger} >= {
        "suppressed_noise",
        "first_screen",
        "context_pending",
    }
    assert result["summary"]["retention_ledger_count"] == len(messages)
    assert result["summary"]["all_rows_retained"] is True
    assert result["quality"]["retention_invariant_passed"] is True


def test_topic_detail_layer_preserves_platforms_and_failure_branches_from_context_rows():
    messages = [
        _message(
            "github-rule",
            "技术群",
            "Linux.do 要求 GitHub 老号登录，但拒绝 QQ 邮箱注册的账号。",
            0,
            sender="甲",
        ),
        _message(
            "github-loop",
            "技术群",
            "重新注册仍提示已注册，重置密码又收不到邮件。",
            1,
            sender="乙",
        ),
        _message(
            "github-impact",
            "技术群",
            "V2EX 和 linuxsb 也遇到类似情况，登录注册形成死循环，可能错失技术情报。",
            2,
            sender="丙",
        ),
    ]

    result = analyze_messages(messages, START, START + timedelta(days=1))
    topic = next(item for item in result["topic_briefs"] if item["topic"] == "账号与平台")
    detail = topic["detail"]

    assert set(topic["source_message_ids"]) == {"github-rule", "github-loop", "github-impact"}
    assert set(topic["entities"]) >= {"GitHub", "Linux.do", "V2EX", "linuxsb", "QQ邮箱"}
    assert set(topic["participants"]) >= {"甲", "乙", "丙"}
    assert any("甲" in value and "Linux.do" in value for value in topic["speaker_points"])
    assert any(item["sender_name"] == "乙" and "收不到邮件" in item["quote"] for item in topic["attributions"])
    assert topic["evidence_count"] == 3
    assert len(detail["timeline"]) == 3
    assert any("已注册" in value for value in detail["failure_points"])
    assert any("收不到邮件" in value for value in detail["failure_points"])
    assert any("错失技术情报" in value for value in detail["impact"])
    assert "Linux.do" in topic["summary"]
    assert "V2EX" in topic["detail_summary"]
    assert "其他讨论" not in {item["topic"] for item in result["topic_briefs"]}
