import pytest

from wechat_bridge.store import SQLiteStore


def test_brief_feedback_is_hidden_metadata_and_can_be_revised():
    store = SQLiteStore(":memory:")
    try:
        first = store.save_brief_feedback("event:abc", "valuable")
        revised = store.save_brief_feedback("event:abc", "wrong_merge", "选课和购票不应合并")

        assert first["action"] == "valuable"
        assert revised["action"] == "wrong_merge"
        assert store.brief_feedback(["event:abc"]) == [revised]
    finally:
        store.close()


def test_brief_feedback_rejects_unknown_actions():
    store = SQLiteStore(":memory:")
    try:
        with pytest.raises(ValueError):
            store.save_brief_feedback("event:abc", "delete_messages")
    finally:
        store.close()


def test_manual_voice_correction_is_not_overwritten_by_later_asr():
    store = SQLiteStore(":memory:")
    try:
        corrected = store.save_voice_transcript(
            "voice:1", status="corrected", transcript="人工校对文本", manual=True
        )
        after_retry = store.save_voice_transcript(
            "voice:1", status="succeeded", transcript="错误的自动结果", provider="doubao_asr_v2"
        )

        assert corrected["status"] == "corrected"
        assert after_retry["transcript"] == "人工校对文本"
    finally:
        store.close()
