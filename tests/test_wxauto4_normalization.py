from datetime import datetime, timezone
from types import SimpleNamespace
import time

from wechat_bridge.adapters.wxauto4 import WxAuto4Adapter


def test_normalization_maps_common_wxauto4_fields():
    adapter = WxAuto4Adapter(client=object())
    raw = SimpleNamespace(
        id="42",
        content="hello",
        sender="Alice",
        sender_remark="Alice",
        type="text",
        attr="friend",
        timestamp=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
    )
    chat = SimpleNamespace(who="文件传输助手")
    message = adapter._normalize_message(raw, chat, None)
    assert message.message_id == "wxauto4:文件传输助手:42"
    assert message.chat_name == "文件传输助手"
    assert message.content == "hello"
    assert message.message_type == "text"
    assert message.is_self is False


def test_current_wheel_uses_polling_compatible_api_shape():
    class Client:
        GetSubWindow = object()
        GetAllMessage = object()

    assert WxAuto4Adapter(client=Client())._client is not None


def test_polling_fallback_skips_existing_history_and_emits_new_message():
    class Chat:
        def __init__(self):
            self.messages = []

        def ChatInfo(self):
            return {"chat_name": "文件传输助手"}

        def GetAllMessage(self):
            return list(self.messages)

    class Client:
        def __init__(self):
            self.chat = Chat()

        def IsOnline(self):
            return True

        def GetSubWindow(self, nickname):
            assert nickname == "文件传输助手"
            return self.chat

    client = Client()
    client.chat.messages.append(SimpleNamespace(id="old", content="旧消息", attr="friend"))
    adapter = WxAuto4Adapter(client=client, poll_interval=0.2)
    received = []
    adapter.start_receive(("文件传输助手",), received.append)
    try:
        time.sleep(0.35)
        assert received == []
        client.chat.messages.append(SimpleNamespace(id="new", content="新消息", attr="friend"))
        deadline = time.time() + 1.5
        while time.time() < deadline and not received:
            time.sleep(0.02)
        assert len(received) == 1
        assert received[0].content == "新消息"
    finally:
        adapter.stop_receive()
