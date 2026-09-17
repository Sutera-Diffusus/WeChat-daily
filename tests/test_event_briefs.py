from datetime import datetime, timedelta, timezone

from wechat_bridge.analysis import analyze_messages


START = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _message(message_id, chat, content, minutes, *, self_sent=False, group=False, sender="联系人"):
    return {
        "message_id": message_id,
        "chat_id": chat,
        "chat_name": chat,
        "sender_name": "我" if self_sent else sender,
        "content": content,
        "timestamp": (START + timedelta(minutes=minutes)).isoformat(),
        "is_self": self_sent,
        "is_group": group,
        "message_type": "text",
    }


def test_fragmented_same_day_subject_merges_across_private_chats():
    result = analyze_messages(
        [
            _message("a", "小王", "我把本学期选课时间和课程清单发你了，今晚一起确认。", 10, self_sent=True),
            _message("b", "小李", "选课系统下午开放，我准备先确认专业课名额。", 45, sender="小李"),
            _message("c", "辅导员", "关于选课安排，退补选截止时间是本周五。", 80, sender="辅导员"),
        ],
        START,
        START + timedelta(days=1),
    )

    cross_chat = next(item for item in result["event_briefs"] if item["related_chat_count"] == 3)
    assert cross_chat["lane"] == "for_me"
    assert cross_chat["multi_attention"] is True
    assert "选课" in "".join(cross_chat["tags"])
    assert {item["chat_name"] for item in cross_chat["evidence"]} == {"小王", "小李", "辅导员"}
    assert all(item["statement"] and item["quote"] for item in cross_chat["evidence"])
    assert cross_chat["cluster_quality"]["merge_supported"] is True
    assert "共同主题标签" in cross_chat["merge_basis"]
    assert cross_chat["evidence_binding_rate"] == 1.0
    assert result["quality"]["cross_chat_event_count"] == 1
    assert result["quality"]["event_evidence_binding_rate"] == 1.0


def test_unrelated_messages_are_not_merged_just_because_they_share_time_words():
    result = analyze_messages(
        [
            _message("a", "同事", "今天确认选课系统里的专业课名额。", 10, sender="同事"),
            _message("b", "家人", "今天晚上购买高铁票，记得选择靠窗座位。", 12, sender="家人"),
        ],
        START,
        START + timedelta(days=1),
    )

    assert not any(item["related_chat_count"] == 2 for item in result["event_briefs"])


def test_cross_chat_merge_needs_more_than_one_ambiguous_shared_phrase():
    result = analyze_messages(
        [
            _message(
                "a",
                "工程群",
                "服务器部署遇到 quarterly-plan，今天需要修复故障。",
                10,
                group=True,
                sender="甲",
            ),
            _message(
                "b",
                "家人",
                "家庭装修预算采用 quarterly-plan，今天需要核对费用。",
                12,
                group=True,
                sender="乙",
            ),
        ],
        START,
        START + timedelta(days=1),
    )

    assert not any(item["related_chat_count"] == 2 for item in result["event_briefs"])
    assert result["quality"]["cross_chat_event_count"] == 0


def test_cross_chat_aliases_merge_into_one_event_with_canonical_evidence():
    result = analyze_messages(
        [
            _message(
                "risk-a",
                "项目群",
                "只读分析也可能被微信检测出来，不能当成安全保证。",
                10,
                group=True,
                sender="甲",
            ),
            _message(
                "risk-b",
                "项目私聊",
                "先把同步频率压到每天两三次，避免账号被封。",
                35,
                sender="乙",
            ),
            _message(
                "risk-c",
                "风控群",
                "这条链路不要自动发消息，封号风险不好玩。",
                60,
                group=True,
                sender="丙",
            ),
        ],
        START,
        START + timedelta(days=1),
    )

    event = next(item for item in result["event_briefs"] if item["related_chat_count"] == 3)
    assert set(event["message_ids"]) == {"risk-a", "risk-b", "risk-c"}
    assert "账号风控" in event["canonical_topics"]
    assert "账号风控" in event["cluster_quality"]["canonical_terms"]
    assert "同义或别名归一" in event["merge_basis"]


def test_hard_object_boundaries_split_wechat_risk_from_ai_resets():
    result = analyze_messages(
        [
            _message(
                "ai-1",
                "vibe",
                "GPT又不封号，而且不停重置，用中转站没有性价比啊",
                10,
                group=True,
                sender="w0ngpeng",
            ),
            _message(
                "wx-1",
                "vibe",
                "微信一个是分析，主要还是发消息吧，这都是风控的点",
                12,
                group=True,
                sender="张若彬",
            ),
            _message(
                "wx-2",
                "vibe",
                "微信风控很讨厌，而且没法合规，我已经让他们全都转到企业微信了",
                15,
                group=True,
                sender="在路上",
            ),
            _message(
                "ai-2",
                "deepthink",
                "CodeX重置了吗？为什么我刚才又重置了？",
                20,
                group=True,
                sender="🎧",
            ),
        ],
        START,
        START + timedelta(days=1),
    )

    events = result["event_briefs"]
    assert {frozenset(item["message_ids"]) for item in events} == {
        frozenset({"ai-1"}),
        frozenset({"wx-1", "wx-2"}),
        frozenset({"ai-2"}),
    }
    assert not any(
        {"ai_service", "wechat"}.issubset(set(item.get("domain_tags") or []))
        for item in events
    )
    assert any(item["title"].startswith("Codex重置") for item in events)


def test_group_hot_requires_multiple_people_and_stays_out_of_for_me():
    result = analyze_messages(
        [
            _message("a", "AI 群", "多模态模型评测要加入图像理解基准。", 10, group=True, sender="甲"),
            _message("b", "AI 群", "多模态模型评测还需要覆盖工具调用。", 15, group=True, sender="乙"),
            _message("c", "AI 群", "多模态模型评测最好记录延迟和成本。", 20, group=True, sender="丙"),
        ],
        START,
        START + timedelta(days=1),
    )

    event = next(item for item in result["event_briefs"] if item["group_hot"])
    assert event["lane"] == "trending"
    assert "群内热点" in event["tags"]
    assert event not in result["for_me"]
