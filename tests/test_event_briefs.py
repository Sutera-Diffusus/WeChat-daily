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
