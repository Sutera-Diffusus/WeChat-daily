from datetime import datetime, timedelta, timezone

from wechat_bridge.analysis import analyze_messages, build_ai_context


def _item(message_id, chat_id, content, minute, is_group=False):
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_name": chat_id,
        "content": content,
        "timestamp": (datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat(),
        "is_self": False,
        "message_type": "text",
        "is_group": is_group,
    }


def test_isolated_decision_fragments_do_not_enter_pending_queue():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    messages = [
        _item("m1", "群聊", "需要你研究一下怎么用", 0),
        _item("m2", "群聊", "我嫌烦就改成拍一拍了", 1),
        _item("m3", "群聊", "改成糖醋里脊了", 2),
        _item("m4", "技术群", "改成 session 取就好了，不用挂 statusline", 60),
        _item("m5", "技术群", "我直接把 search toolcall 改成了聚合搜索", 120),
    ]

    result = analyze_messages(messages, start, start + timedelta(days=1))

    assert result["actions"] == []
    assert result["quality"]["context_needed_count"] >= 2
    assert result["quality"]["filtered_low_value_count"] >= 2
    assert {item["message_id"] for item in result["suppressed_candidates"]} >= {"m1", "m2", "m3", "m4"}


def test_nearby_context_can_complete_an_objectless_request():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    messages = [
        _item("context", "项目群", "这个接口的文档已经发在群里了", 0),
        _item("request", "项目群", "需要你研究一下怎么用", 3),
    ]

    result = analyze_messages(messages, start, start + timedelta(days=1))

    assert [item["message_id"] for item in result["actions"]] == ["request"]
    assert result["actions"][0]["reason"] == "请求对象由同一会话的邻近上下文补足"


def test_contextual_risk_clause_is_not_promoted_as_a_standalone_highlight():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    messages = [
        _item("fact", "种博来", "额度异常账户使用了工具把订阅额度转换成 API 流量", 0),
        _item("clause", "种博来", "至于那些没有使用工具还说额度异常的用户", 1),
    ]

    result = analyze_messages(messages, start, start + timedelta(days=1))

    assert [item["message_id"] for item in result["highlights"]] == ["fact"]
    assert result["quality"]["context_needed_count"] == 1


def test_discussion_and_resource_lanes_preserve_non_actionable_value():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    messages = [
        _item("d1", "AI 群", "我认为 agent harness 的关键不是多开几个模型，而是把工具边界和评测指标固定下来。", 0, True),
        _item("d2", "AI 群", "这篇文档把多模态 token 预算和图像分辨率的关系讲得很清楚 https://example.com/vision", 1, True),
        _item("d3", "AI 群", "模型效果需要结合真实任务评测，不能只看 demo。", 2, True),
        _item("d4", "AI 群", "哈哈哈", 3, True),
        _item("d5", "AI 群", "又开始讨论了", 4, True),
        _item("d6", "AI 群", "这个方向值得继续观察", 5, True),
    ]

    result = analyze_messages(messages, start, start + timedelta(days=1))

    assert result["actions"] == []
    assert result["summary"]["substantive"] >= 2
    assert result["summary"]["resources"] >= 1
    assert result["discoveries"]
    assert result["discussion_episodes"]
    assert result["situation"]["headline"]
    assert result["topic_briefs"]
    assert result["topic_briefs"][0]["evidence"]
    assert result["primary_insights"]
    assert result["activity"]["hourly"][8]["substantive"] >= 2
    candidates = build_ai_context(messages)
    assert any(item["candidate_state"] == "informative" for item in candidates)


def test_event_time_range_is_chronological():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    result = analyze_messages(
        [
            _item("e1", "项目群", "请明天确认方案", 20),
            _item("e2", "项目群", "项目风险需要排查", 0),
        ],
        start,
        start + timedelta(days=1),
    )
    assert result["events"]
    assert result["events"][0]["start"] <= result["events"][0]["end"]


def test_busy_group_without_topic_is_not_presented_as_an_insight_episode():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    messages = [
        _item("noise-%s" % index, "生活群", "今天手机没信号，晚点再聊。", index, True)
        for index in range(7)
    ]

    result = analyze_messages(messages, start, start + timedelta(days=1))

    assert result["discussion_episodes"] == []


def test_ai_context_keeps_same_sender_fragments_in_chronological_context():
    messages = [
        _item("f1", "私聊", "选课系统中午开放", 0),
        _item("f2", "私聊", "那个老师的课", 1),
        _item("f3", "私聊", "名额只剩两个了", 2),
        _item("f4", "私聊", "请下午三点前确认选课方案", 3),
    ]

    candidates = build_ai_context(messages, priority_message_ids={"f4"})
    target = next(item for item in candidates if item["_source_message_id"] == "f4")

    assert [item["content"] for item in target["context"]] == [
        "选课系统中午开放",
        "那个老师的课",
        "名额只剩两个了",
    ]
    assert all(item["same_sender"] is True for item in target["context"])


def test_ai_context_can_analyze_substantive_messages_sent_by_me():
    message = _item("self-1", "同学", "我已经整理了选课冲突，下午三点前需要确认最终课程。", 0)
    message["is_self"] = True
    message["sender_name"] = "本地账号"

    candidates = build_ai_context([message], priority_message_ids={"self-1"})

    assert len(candidates) == 1
    assert candidates[0]["sender_name"] == "我"
    assert candidates[0]["is_self"] is True
