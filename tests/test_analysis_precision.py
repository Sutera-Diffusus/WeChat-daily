from datetime import datetime, timedelta, timezone

from wechat_bridge.analysis import analyze_messages, build_ai_context, build_ai_dialogue_packets


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


def test_ai_context_backfills_high_density_chat_to_requested_limit():
    messages = [
        _item(
            "dense-%02d" % index,
            "高密度项目群",
            "请在今天确认第 %d 个接口迁移方案和负责人。" % index,
            index,
            True,
        )
        for index in range(40)
    ]

    candidates = build_ai_context(messages, max_items=30)

    assert len(candidates) == 30
    assert {item["chat_name"] for item in candidates} == {"高密度项目群"}


def test_ai_context_preserves_cross_chat_coverage_before_backfill():
    messages = [
        _item(
            "busy-%02d" % index,
            "高优先级项目群",
            "请在今天确认第 %d 个发布故障的修复方案。" % index,
            index,
            True,
        )
        for index in range(20)
    ]
    messages.append(
        _item(
            "quiet-01",
            "低频讨论群",
            "我认为模型效果需要结合真实任务评测，不能只看演示结果。",
            30,
            True,
        )
    )

    candidates = build_ai_context(messages, max_items=13)

    assert len(candidates) == 13
    assert "quiet-01" in {item["_source_message_id"] for item in candidates}
    assert sum(item["chat_name"] == "高优先级项目群" for item in candidates) == 12


def test_ai_context_backfill_keeps_priority_messages_within_total_budget():
    messages = [
        _item(
            "priority-%02d" % index,
            "单一项目群",
            "请在今天确认第 %d 个接口发布方案和负责人。" % index,
            index,
            True,
        )
        for index in range(30)
    ]
    priority_ids = {"priority-%02d" % index for index in range(20)}

    candidates = build_ai_context(
        messages,
        max_items=25,
        priority_message_ids=priority_ids,
    )

    selected_ids = {item["_source_message_id"] for item in candidates}
    assert len(candidates) == 25
    assert priority_ids <= selected_ids


def _ai_candidate(ref, chat, minute, content, domain_tags=()):
    return {
        "evidence_ref": ref,
        "chat_name": chat,
        "timestamp": (
            datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(minutes=minute)
        ).isoformat(),
        "content": content,
        "domain_tags": list(domain_tags),
    }


def test_ai_dialogue_packets_never_cross_chat_boundaries():
    candidates = [
        _ai_candidate("m-001", "甲群", 0, "接口发布方案已经确认"),
        _ai_candidate("m-002", "乙群", 1, "接口发布方案需要复核"),
    ]

    packets = build_ai_dialogue_packets(candidates)

    assert len(packets) == 2
    assert [{item["chat_name"] for item in packet["candidates"]} for packet in packets] == [
        {"甲群"}, {"乙群"}
    ]


def test_ai_dialogue_packets_split_when_inactivity_gap_exceeds_limit():
    candidates = [
        _ai_candidate("m-001", "项目群", 0, "接口发布方案已经确认"),
        _ai_candidate("m-002", "项目群", 46, "接口发布方案需要复核"),
    ]

    packets = build_ai_dialogue_packets(candidates, max_span_minutes=45)

    assert [packet["evidence_refs"] for packet in packets] == [["m-001"], ["m-002"]]


def test_ai_dialogue_packets_keep_long_but_continuous_conversation_together():
    candidates = [
        _ai_candidate("m-001", "项目群", 0, "接口发布方案已经确认"),
        _ai_candidate("m-002", "项目群", 30, "接口发布方案需要复核"),
        _ai_candidate("m-003", "项目群", 60, "接口发布方案补充了负责人"),
    ]

    packets = build_ai_dialogue_packets(candidates, max_span_minutes=45)

    assert [packet["evidence_refs"] for packet in packets] == [["m-001", "m-002", "m-003"]]


def test_ai_dialogue_packets_keep_close_domain_change_in_same_packet():
    candidates = [
        _ai_candidate("m-001", "技术群", 0, "Codex 额度重置", ("codex_service",)),
        _ai_candidate("m-002", "技术群", 1, "微信账号被限制", ("wechat",)),
    ]

    packets = build_ai_dialogue_packets(candidates)

    assert [packet["evidence_refs"] for packet in packets] == [["m-001", "m-002"]]


def test_ai_dialogue_packets_keep_distant_domain_change_within_span():
    candidates = [
        _ai_candidate("m-001", "技术群", 0, "Codex 额度重置", ("codex_service",)),
        _ai_candidate("m-002", "技术群", 11, "微信账号被限制", ("wechat",)),
    ]

    packets = build_ai_dialogue_packets(candidates)

    assert [packet["evidence_refs"] for packet in packets] == [["m-001", "m-002"]]


def test_ai_dialogue_packets_keep_close_topic_change_in_same_packet():
    candidates = [
        _ai_candidate("m-001", "技术群", 0, "接口发布方案已经确认"),
        _ai_candidate("m-002", "技术群", 5, "模型评测结果已经公布"),
    ]

    packets = build_ai_dialogue_packets(candidates)

    assert [packet["evidence_refs"] for packet in packets] == [["m-001", "m-002"]]


def test_ai_dialogue_packets_keep_distant_topic_change_within_span():
    candidates = [
        _ai_candidate("m-001", "技术群", 0, "接口发布方案已经确认"),
        _ai_candidate("m-002", "技术群", 11, "模型评测结果已经公布"),
    ]

    packets = build_ai_dialogue_packets(candidates)

    assert [packet["evidence_refs"] for packet in packets] == [["m-001", "m-002"]]


def test_ai_dialogue_packets_obey_hard_item_limit():
    candidates = [
        _ai_candidate("m-%03d" % index, "项目群", index, "接口发布方案第 %d 项" % index)
        for index in range(1, 8)
    ]

    packets = build_ai_dialogue_packets(candidates, max_items=3)

    assert [len(packet["candidates"]) for packet in packets] == [3, 3, 1]


def test_ai_dialogue_packets_cover_every_candidate_exactly_once_and_keep_global_refs():
    candidates = [
        _ai_candidate("m-004", "乙群", 4, "模型评测结果需要复核"),
        _ai_candidate("m-001", "甲群", 0, "接口发布方案已经确认"),
        _ai_candidate("m-003", "甲群", 2, "接口发布方案等待复核"),
        _ai_candidate("m-002", "甲群", 1, "接口发布方案补充了负责人"),
    ]

    packets = build_ai_dialogue_packets(candidates)
    flattened = [
        item["evidence_ref"]
        for packet in packets
        for item in packet["candidates"]
    ]

    assert sorted(flattened) == sorted(item["evidence_ref"] for item in candidates)
    assert len(flattened) == len(set(flattened)) == len(candidates)
    assert all(
        packet["evidence_refs"] == [item["evidence_ref"] for item in packet["candidates"]]
        for packet in packets
    )
    assert build_ai_dialogue_packets(candidates) == packets
