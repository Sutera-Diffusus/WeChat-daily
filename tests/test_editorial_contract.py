from datetime import datetime, timedelta, timezone

from wechat_bridge.analysis import analyze_messages, is_editorial_title
from wechat_bridge.ai import OpenAIAnalysisGenerator


START = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _message(message_id, chat, content, minutes, *, sender="成员", self_sent=False, message_type="text", group=False):
    return {
        "message_id": message_id,
        "chat_id": chat,
        "chat_name": chat,
        "sender_name": "我" if self_sent else sender,
        "content": content,
        "timestamp": (START + timedelta(minutes=minutes)).isoformat(),
        "is_self": self_sent,
        "is_group": group,
        "message_type": message_type,
    }


def test_editorial_title_contract_rejects_raw_or_vague_titles():
    assert is_editorial_title("选课安排：信息在多方汇合")
    assert is_editorial_title("配置设计的变革")
    assert not is_editorial_title("相关讨论引关注")
    assert not is_editorial_title("但是我今天和老师聊明白了感觉豁然开朗")
    assert not is_editorial_title("需要确认")
    assert not is_editorial_title("GPT封号与成本风险受关注")
    assert not is_editorial_title("外部资源链接分享")
    assert not is_editorial_title("两个资源链接待确认")
    assert not is_editorial_title("这是一条超过二十四个字符的聊天摘要不应直接作为标题")


def test_ai_schema_carries_editorial_title_length_contract():
    title_schema = OpenAIAnalysisGenerator.schema["properties"]["findings"]["items"]["properties"]["title"]
    assert title_schema["minLength"] == 8
    assert title_schema["maxLength"] == 24


def test_local_event_headline_is_short_editorial_and_not_chat_excerpt():
    result = analyze_messages(
        [
            _message("course-1", "同学甲", "我把本学期选课时间和课程清单发你了，今晚一起确认。", 1, self_sent=True),
            _message("course-2", "教务群", "选课系统下午开放，专业课名额需要先确认。", 3, sender="周老师", group=True),
            _message("course-3", "辅导员", "关于选课安排，退补选截止时间是本周五。", 6, sender="辅导员"),
        ],
        START,
        START + timedelta(days=1),
    )
    assert result["event_briefs"]
    for event in result["event_briefs"]:
        assert is_editorial_title(event["title"]), event["title"]
        assert len(event["title"]) <= 24
        assert event["narrative"]
        assert event["why_it_matters"]
        assert event["message_ids"]


def test_full_census_accounts_for_unformed_greetings_questions_fragments_and_media():
    messages = [
        _message("greeting", "余宣", "晚上好，今天还顺利吗？", 1, sender="余宣"),
        _message("question", "刘锋学长", "请问选课系统几点开放？", 2, sender="刘锋学长"),
        _message("fragment", "秦昕老师", "那个老师的课", 3, sender="秦昕老师"),
        _message("media", "秦昕老师", "[语音]", 4, sender="秦昕老师", message_type="voice"),
        _message("image", "项目群", "[图片]", 5, sender="小周", message_type="image", group=True),
    ]
    result = analyze_messages(messages, START, START + timedelta(days=1))
    dynamic_ids = {
        message_id
        for item in result["unformed_dynamics"]
        for message_id in item["message_ids"]
    }
    event_ids = {
        message_id
        for event in result["event_briefs"]
        for message_id in event["message_ids"]
    }
    # The group image still passes the local census, but a context-free group
    # placeholder is intentionally suppressed from the editorial output.
    assert dynamic_ids | event_ids == {item["message_id"] for item in messages} - {"image"}
    assert result["summary"]["accounted_messages"] == len(messages)
    assert result["summary"]["suppressed_group_noise"] == 1
    assert result["unformed_dynamics"]
    summaries = "\n".join(item["summary"] for item in result["unformed_dynamics"])
    assert "余宣" in summaries
    assert "选课系统" in summaries
    assert "语音" in summaries
    assert "图片" not in summaries


def test_group_noise_is_suppressed_but_social_chat_events_are_kept():
    messages = [
        _message("noise-1", "技术群", "+1", 1, sender="甲", group=True),
        _message("noise-2", "技术群", "哈哈哈哈", 2, sender="乙", group=True),
        _message("noise-3", "技术群", "开团秒跟", 3, sender="丙", group=True),
        _message("life-1", "粗来丸", "周末一起吃饭吗？", 10, sender="小周", group=True),
        _message("life-2", "粗来丸", "可以，我下午都有时间", 12, sender="小秦", group=True),
    ]
    result = analyze_messages(messages, START, START + timedelta(days=1))
    summaries = "\n".join(item["summary"] for item in result["unformed_dynamics"])
    assert "粗来丸" in summaries
    assert "技术群" not in summaries
    assert result["summary"]["accounted_messages"] == 5
    assert result["summary"]["suppressed_group_noise"] == 3


def test_small_matters_describe_actual_event_instead_of_taxonomy_template():
    messages = [
        _message("down-1", "模型讨论群", "CLAUDE 怎么崩了啊", 1, sender="小周", group=True),
        _message("down-2", "模型讨论群", "opus fable都用不了，529了", 2, sender="小秦", group=True),
        _message("down-3", "模型讨论群", "恢复了我这里", 4, sender="小周", group=True),
    ]
    result = analyze_messages(messages, START, START + timedelta(days=1))
    summary = result["unformed_dynamics"][0]["summary"]
    assert "模型讨论群" in summary
    assert "Claude/Opus" in summary
    assert "已经恢复" in summary
    assert "交换了信息和看法" not in summary


def test_new_group_name_can_be_inferred_from_private_introduction():
    messages = [
        _message("intro", "董小炜", '我是群聊"秦老师和他的科研小伙伴"的董小炜', 1, sender="董小炜"),
        _message("poll-1", "50428006245@chatroom", "请大家尽可能多选能参加的时间", 5, sender="董小炜", group=True),
        _message("poll-2", "50428006245@chatroom", "我9月1号以后都可以", 7, sender="小李", group=True),
    ]
    result = analyze_messages(messages, START, START + timedelta(days=1))
    group_matter = next(item for item in result["unformed_dynamics"] if "poll-1" in item["message_ids"])
    assert group_matter["chats"][0]["chat_name"] == "秦老师和他的科研小伙伴"
    assert "@chatroom" not in group_matter["summary"]
