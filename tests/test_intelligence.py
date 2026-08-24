import http.client
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.analysis import analyze_messages, build_ai_context
from wechat_bridge.ai import OpenAIAnalysisGenerator
from wechat_bridge.engine import ReplyPolicy
from wechat_bridge.models import HealthStatus, IncomingMessage, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.store import SQLiteStore
from wechat_bridge.web import start_dashboard_thread


def _message(message_id, content, timestamp, is_self=False):
    return IncomingMessage(
        message_id=message_id,
        chat_id="filehelper",
        chat_name="文件传输助手",
        sender_id="sender",
        sender_name="测试联系人",
        message_type="text",
        content=content,
        timestamp=timestamp,
        is_self=is_self,
        raw_message={"content": content},
        adapter_name="range-test",
        adapter_version="test",
    )


class RangeAdapter(WeChatAdapter):
    name = "range-test"

    def __init__(self):
        self.callback = None
        self.sent = []

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
        self.sent.append(content)
        return SendResult(True, True, "confirmed")

    def get_history_range(self, start_at, end_at, chat_ids=None, limit=50_000):
        return [
            _message(
                "range-1",
                "请明天确认报价",
                datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc),
            ).__dict__,
            _message(
                "range-2",
                "收到",
                datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc),
                is_self=True,
            ).__dict__,
        ]


def wait_until(predicate, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_analysis_is_explainable_and_keeps_evidence_ids():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    items = [
        {"message_id": "m1", "chat_id": "c1", "chat_name": "项目群", "content": "请明天确认报价", "timestamp": start.isoformat(), "is_self": False},
        {"message_id": "m2", "chat_id": "c1", "chat_name": "项目群", "content": "好的", "timestamp": (start + timedelta(minutes=2)).isoformat(), "is_self": False},
    ]
    value = analyze_messages(items, start, start + timedelta(days=1))
    assert value["summary"]["messages"] == 2
    assert value["actions"][0]["evidence"] == "m1"
    assert value["highlights"][0]["message_id"] == "m1"
    assert value["method"]["name"] == "rules_v1"


def test_range_sync_is_read_only_and_does_not_create_reply_tasks():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = RangeAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("不会发送", ("文件传输助手",)),
            ("文件传输助手",),
            dry_run=False,
            send_enabled=False,
        )
        service.start()
        try:
            start = datetime(2026, 8, 21, tzinfo=timezone.utc)
            service.start_history_sync(start, start + timedelta(days=1), limit=100)
            assert wait_until(lambda: service.history_sync_status()["state"] == "succeeded")
            assert service.history_sync_status()["inserted"] == 2
            assert store.count_messages() == 2
            assert store.count_tasks() == 0
            assert adapter.sent == []
        finally:
            service.stop()
            store.close()


def test_receive_only_http_hard_blocks_manual_send():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = RangeAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("不会发送", ("文件传输助手",)),
            ("文件传输助手",),
            dry_run=False,
            send_enabled=False,
        )
        service.start()
        server = None
        try:
            server, thread = start_dashboard_thread(service, port=0)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            connection.request(
                "POST",
                "/api/send-text",
                body=json.dumps({"chat_id": "filehelper", "content": "不会发出", "confirm": True}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            value = json.loads(response.read().decode("utf-8"))
            assert response.status == 403
            assert value["error"] == "sending_disabled"
            assert adapter.sent == []
            connection.close()
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            service.stop()
            store.close()


def test_ai_analysis_is_manual_and_falls_back_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = RangeAdapter()
        policy = ReplyPolicy.from_values("不会发送", ("文件传输助手",))
        service = BridgeService(
            adapter,
            store,
            policy,
            ("文件传输助手",),
            dry_run=False,
            send_enabled=False,
        )
        service.start()
        server = None
        try:
            server, _thread = start_dashboard_thread(service, port=0)
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=3
            )
            connection.request("GET", "/api/ai-status")
            status = connection.getresponse()
            status_value = json.loads(status.read().decode("utf-8"))
            assert status.status == 200
            assert status_value["configured"] is False
            assert status_value["mode"] == "manual_only"

            connection.request(
                "POST",
                "/api/ai-analysis",
                body=json.dumps(
                    {
                        "start": "2026-08-21",
                        "end": "2026-08-21",
                        "confirm": True,
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            value = json.loads(response.read().decode("utf-8"))
            assert response.status == 409
            assert value["error"] == "ai_not_configured"
            assert value["source"] == "rules_fallback"
            assert value["candidate_count"] == 0
            assert service.send_enabled is False
            connection.close()
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            service.stop()
            store.close()


def test_ai_context_is_bounded_and_does_not_include_raw_payload():
    items = [
        {
            "message_id": "internal-1",
            "chat_id": "wxid_private",
            "chat_name": "项目群",
            "sender_name": "群内昵称",
            "sender_name_confidence": 0.96,
            "is_group": True,
            "is_self": False,
            "message_type": "text",
            "content": "请明天确认方案，原始路径 C:\\private\\file.txt",
            "timestamp": "2026-08-21T01:00:00+00:00",
            "raw_message": {"secret": "不要发送"},
        }
    ]
    candidates = build_ai_context(items, max_items=120)
    assert len(candidates) == 1
    assert candidates[0]["evidence_ref"] == "m-001"
    assert candidates[0]["sender_name"] == "群内昵称"
    assert "raw_message" not in candidates[0]
    assert "message_id" not in candidates[0]
    assert "wxid_private" not in candidates[0]
    assert "C:\\private" not in candidates[0]["content"]
    assert "[本地路径]" in candidates[0]["content"]


def test_ai_generator_uses_structured_output_without_raw_identifiers(monkeypatch):
    calls = {}

    class FakeResponses:
        def create(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "brief": "发现一项待确认事项。",
                        "themes": ["项目"],
                        "findings": [
                            {
                                "ref_ids": ["m-001"],
                                "title": "需要确认",
                                "category": "待办",
                                "importance": 80,
                                "confidence": 70,
                                "summary": "消息明确提出确认请求。",
                                "reason": "引用 m-001",
                                "next_step": "人工核对原消息",
                            }
                        ],
                        "limitations": [],
                    },
                    ensure_ascii=False,
                )
            )

    class FakeOpenAI:
        def __init__(self, api_key):
            calls["api_key"] = api_key
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    generator = OpenAIAnalysisGenerator(api_key="test-key")
    result = generator.analyze(
        {"start": "2026-08-21", "end": "2026-08-21"},
        [
            {
                "evidence_ref": "m-001",
                "timestamp": "2026-08-21T01:00:00+00:00",
                "chat_name": "项目群",
                "sender_name": "群成员",
                "is_group": True,
                "content": "请明天确认方案 [内部标识]",
                "rule_signals": ["待办"],
                "rule_level": "high",
                "_source_message_id": "internal-message-id",
                "raw_message": {"secret": "不要发送"},
            }
        ],
    )
    assert result["findings"][0]["ref_ids"] == ["m-001"]
    assert calls["store"] is False
    assert calls["text"]["format"]["type"] == "json_schema"
    assert "internal-message-id" not in calls["input"]
    assert "不要发送" not in calls["input"]
