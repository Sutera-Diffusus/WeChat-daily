import http.client
import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

import pytest

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.ai import OpenAIReplyGenerator, ReplyGenerationError
from wechat_bridge.engine import ReplyPolicy, ReplyRule
from wechat_bridge.models import HealthStatus, IncomingMessage, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.store import SQLiteStore
from wechat_bridge.web import start_dashboard_thread


class FakeAdapter(WeChatAdapter):
    name = "fake-web"

    def __init__(self):
        self.callback = None

    @property
    def version(self):
        return "test"

    def connect(self):
        pass

    def disconnect(self):
        self.callback = None

    def health_check(self):
        return HealthStatus(True, self.name, self.version, "ok")

    def start_receive(self, chat_names, callback):
        self.callback = callback

    def stop_receive(self):
        self.callback = None

    def send_text(self, chat_id, chat_name, content):
        return SendResult(True, True, "confirmed")

    def get_chat_history(self, chat_id, chat_name="", limit=50):
        return [incoming("历史同步消息")]


def incoming(content="请问现在方便吗？"):
    return IncomingMessage(
        message_id="web-1",
        chat_id="filehelper",
        chat_name="文件传输助手",
        sender_id="someone",
        sender_name="测试用户",
        message_type="text",
        content=content,
        timestamp=datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc),
        is_self=False,
        raw_message={"content": content},
        adapter_name="fake-web",
        adapter_version="test",
    )


def test_rule_matches_keyword_and_shanghai_time():
    rule = ReplyRule.from_dict(
        {
            "name": "工作时间问候",
            "keywords": ["方便"],
            "time_ranges": ["11:00-13:00"],
            "reply": "你好",
        },
        0,
    )
    assert rule.matches(incoming(), "Asia/Shanghai") is True
    assert rule.matches(
        incoming("请问现在方便吗？"), "UTC"
    ) is False


def test_ai_provider_refuses_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ReplyGenerationError, match="OPENAI_API_KEY"):
        OpenAIReplyGenerator(api_key="").generate(incoming())


def test_dashboard_exposes_safe_status_preview_and_pause():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = FakeAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定预览", ("文件传输助手",)),
            ("文件传输助手",),
            dry_run=True,
        )
        service.start()
        server = None
        try:
            server, thread = start_dashboard_thread(service, port=0)
            port = server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert "WeChat Bridge" in page
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/assets/editorial/SourceHanSerifSC-Heavy.otf")
            response = connection.getresponse()
            font_payload = response.read()
            assert response.status == 200
            assert response.getheader("Content-Type").startswith("font/otf")
            assert len(font_payload) > 1_000_000
            connection.request(
                "POST",
                "/api/report-render",
                body=json.dumps({"format": "html", "filename": "daily", "html": "<!doctype html><html><body>日报</body></html>"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            report = response.read().decode("utf-8")
            assert response.status == 200
            assert response.getheader("Content-Disposition") == 'attachment; filename="daily.html"'
            assert "日报" in report
            connection.request(
                "POST",
                "/api/report-email",
                body=json.dumps({"recipient": "reader@example.com", "formats": ["html"], "html": "<html><body>日报</body></html>"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            email_error = json.loads(response.read().decode("utf-8"))
            assert response.status == 400
            assert email_error["error"] == "confirmation_required"
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            status = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert status["dry_run"] is True
            assert status["allowed_chats"] == ["文件传输助手"]

            connection.request(
                "POST",
                "/api/sync",
                body=json.dumps({"limit": 10}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            sync = json.loads(response.read().decode("utf-8"))
            assert sync["inserted"] == 1
            assert store.count_tasks() == 0

            connection.request(
                "POST",
                "/api/preview",
                body=json.dumps({"content": "测试预览"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            preview = json.loads(response.read().decode("utf-8"))
            assert preview["ok"] is True
            assert preview["will_send"] is False

            connection.request(
                "POST",
                "/api/auto-reply",
                body=json.dumps({"enabled": False}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            paused = json.loads(response.read().decode("utf-8"))
            assert paused["paused"] is True
            connection.close()
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            service.stop()
            store.close()
