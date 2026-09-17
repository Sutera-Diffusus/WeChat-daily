"""Fully synthetic dialogue fixtures for the semantic dialogue contract.

These fixtures intentionally contain no exported WeChat records, private
identifiers, or copied message text.  They exercise only the public input
shape expected by :func:`wechat_bridge.dialogue_segments.segment_dialogues`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


DATA_ORIGIN = "synthetic"
START = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def message(
    message_id: str,
    content: str,
    seconds: int,
    *,
    speaker_id: str = "person-a",
    speaker_name: str = "合成人甲",
    chat_id: str = "chat-synthetic",
    reply_to_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one deterministic synthetic text message."""

    return {
        "data_origin": DATA_ORIGIN,
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": speaker_id,
        "sender_name": speaker_name,
        "content": content,
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(),
        "reply_to_message_id": reply_to_message_id,
        "message_type": "text",
        "is_group": False,
        "is_self": speaker_id == "self",
    }


def pure_greeting() -> List[Dict[str, Any]]:
    """A social-only exchange with no topic to promote as evidence."""

    return [
        message("greet-1", "你好", 0),
        message("greet-2", "辛苦了", 45),
        message("greet-3", "最近怎么样？", 90),
    ]


def greeting_then_same_person_question() -> List[Dict[str, Any]]:
    return [
        message("same-opener", "你好", 0),
        message("same-question", "我想问一下，项目看板为什么打不开？", 50),
    ]


def greeting_then_other_person_topic() -> List[Dict[str, Any]]:
    return [
        message("other-opener", "辛苦了", 0),
        message(
            "other-topic",
            "数据库备份今晚几点开始？",
            55,
            speaker_id="person-b",
            speaker_name="合成人乙",
        ),
    ]


def greeting_with_substantive_content() -> List[Dict[str, Any]]:
    return [
        message("mixed-opener-topic", "你好，我想确认项目看板的发布状态。", 0),
    ]


def long_gap() -> List[Dict[str, Any]]:
    return [
        message("gap-opener", "你好", 0),
        message("gap-first-topic", "项目看板今天打不开，需要排查。", 45),
        message("gap-second-topic", "课程选修截止时间是什么？", 3600),
    ]


def topic_turn() -> List[Dict[str, Any]]:
    return [
        message("turn-opener", "你好", 0),
        message("turn-first-topic", "服务器接口异常，需要排查。", 45),
        message(
            "turn-second-topic",
            "对了，课程选修截止时间是什么？",
            90,
            speaker_id="person-b",
            speaker_name="合成人乙",
        ),
    ]


def context_only_around_topic() -> List[Dict[str, Any]]:
    return [
        message("context-opener", "你好", 0),
        message("context-topic", "项目看板今天打不开，需要排查。", 45),
        message("context-reaction", "收到", 90),
    ]
