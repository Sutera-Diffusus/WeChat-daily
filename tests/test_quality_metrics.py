from datetime import datetime, timezone

from wechat_bridge.analysis import analyze_messages


def test_analysis_reports_unknown_capture_and_identity_media_quality_metrics():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    messages = [
        {
            "message_id": "quality-1",
            "chat_id": "chat-1",
            "chat_name": "项目群",
            "sender_name": "张三",
            "sender_name_source": "group_nickname",
            "sender_name_confidence": 0.95,
            "message_type": "text",
            "content": "请明天确认报价",
            "timestamp": start.isoformat(),
            "is_self": False,
        },
        {
            "message_id": "quality-2",
            "chat_id": "chat-1",
            "chat_name": "项目群",
            "sender_name": "李四",
            "sender_name_source": "group_nickname",
            "sender_name_confidence": 0.90,
            "message_type": "image",
            "content": "[图片]",
            "media_path": "C:/tmp/quality-2.jpg",
            "timestamp": (start.replace(hour=1)).isoformat(),
            "is_self": False,
        },
        {
            "message_id": "quality-3",
            "chat_id": "chat-1",
            "chat_name": "项目群",
            "sender_name": "王五",
            "sender_name_source": "group_nickname",
            "sender_name_confidence": 0.90,
            "message_type": "file",
            "content": "[文件]",
            "timestamp": (start.replace(hour=2)).isoformat(),
            "is_self": False,
        },
    ]

    result = analyze_messages(messages, start, start.replace(hour=3))
    quality = result["quality"]

    assert quality["capture_completeness"] is None
    assert quality["capture_completeness_state"] == "unknown"
    assert quality["identity_required"] == 3
    assert quality["identity_resolved"] == 3
    assert quality["identity_resolution_rate"] == 1.0
    assert quality["media_total"] == 2
    assert quality["media_with_path"] == 1
    assert quality["media_path_coverage"] == 0.5
    assert quality["media_file_open_rate"] is None
