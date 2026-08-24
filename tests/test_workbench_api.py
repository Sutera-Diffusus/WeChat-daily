import http.client
import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

import pytest

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.engine import ReplyPolicy
from wechat_bridge.models import HealthStatus, IncomingMessage, ReplyDecision, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.store import SQLiteStore
from wechat_bridge.web import BridgeRequestHandler, start_dashboard_thread


class WorkbenchAdapter(WeChatAdapter):
    name = "workbench-test"

    @property
    def version(self):
        return "test"

    def connect(self):
        pass

    def disconnect(self):
        pass

    def health_check(self):
        return HealthStatus(True, self.name, self.version, "ok")

    def start_receive(self, chat_names, callback):
        self.callback = callback

    def stop_receive(self):
        self.callback = None

    def send_text(self, chat_id, chat_name, content):
        return SendResult(True, True, "confirmed")

    def get_chat_history(self, chat_id, chat_name="", limit=50):
        return []


def _message(
    message_id,
    timestamp,
    *,
    chat_id,
    chat_name,
    sender_id,
    sender_name,
    content,
    is_self=False,
    is_group=False,
    sender_name_source=None,
    message_type="text",
):
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_name=chat_name,
        sender_id=sender_id,
        sender_name=sender_name,
        message_type=message_type,
        content=content,
        timestamp=datetime(2026, 8, 21, timestamp, 0, tzinfo=timezone.utc),
        is_self=is_self,
        is_group=is_group,
        sender_name_source=sender_name_source,
        sender_name_confidence=1.0 if sender_name_source else None,
        raw_message={"content": content},
        adapter_name="workbench-test",
        adapter_version="test",
    )


@pytest.fixture
def running_workbench():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = WorkbenchAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定回复", ("文件传输助手",)),
            ("文件传输助手",),
            dry_run=True,
            filehelper_only=False,
            send_enabled=False,
        )
        fixture_messages = [
            _message(
                "m-1",
                1,
                chat_id="chat-a",
                chat_name="Alice",
                sender_id="alice-id",
                sender_name="Alice",
                sender_name_source="contact_remark",
                content="请今天确认项目报价并回复我？",
            ),
            _message(
                "m-2",
                2,
                chat_id="chat-a",
                chat_name="Alice",
                sender_id="me",
                sender_name="我",
                content="已收到",
                is_self=True,
            ),
            _message(
                "m-3",
                3,
                chat_id="chat-group",
                chat_name="研发群",
                sender_id="member-1",
                sender_name="小王",
                sender_name_source="group_nickname",
                content="项目故障，明天请安排处理？",
                is_group=True,
            ),
            _message(
                "m-4",
                4,
                chat_id="chat-a",
                chat_name="Alice",
                sender_id="wxid_alice",
                sender_name="wxid_alice",
                content="补充一下背景",
            ),
            _message(
                "m-5",
                5,
                chat_id="chat-b",
                chat_name="Bob",
                sender_id="bob-id",
                sender_name="Bob",
                sender_name_source="direct_chat_peer",
                content="晚点聊",
            ),
        ]
        for item in fixture_messages:
            store.ingest(item, ReplyDecision(False, "fixture"), create_task=False)
        service.start()
        server, _thread = start_dashboard_thread(service, port=0)
        try:
            yield server, store
        finally:
            server.shutdown()
            server.server_close()
            service.stop()
            store.close()


def _request(server, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_overview_contains_candidates_quality_and_scope(running_workbench):
    server, _store = running_workbench
    status, value = _request(
        server,
        "GET",
        "/api/overview?start=2026-08-21&end=2026-08-21",
    )
    assert status == 200
    assert value["summary"]["messages"] == 5
    assert value["events"]
    assert value["highlight_candidates"]
    assert value["pending_candidates"]
    assert value["scope"]["realtime"]["mode"] == "live"
    assert value["scope"]["history"]["mode"] == "history"
    assert "analysis_coverage" in value["quality"]
    assert "capture_completeness_state" in value["quality"]
    assert "discoveries" in value
    assert "discussion_episodes" in value
    assert "situation" in value
    assert "topic_briefs" in value
    assert "primary_insights" in value
    assert "unformed_dynamics" in value
    assert all("summary" in item and "message_ids" in item for item in value["unformed_dynamics"])
    assert all(len(str(item.get("title") or "")) <= 24 for item in value["event_briefs"])
    assert len(value["hourly"]) == 24
    assert "chat_activity" in value["activity"]
    assert value["read_only"] is True


def test_voice_transcript_is_included_in_overview_analysis(running_workbench):
    server, store = running_workbench
    voice = _message(
        "voice-course-1",
        6,
        chat_id="chat-teacher",
        chat_name="秦昕老师",
        sender_id="teacher-qin",
        sender_name="秦昕老师",
        sender_name_source="contact_remark",
        message_type="voice",
        content="[语音]",
    )
    store.ingest(voice, ReplyDecision(False, "fixture"), create_task=False)
    transcript = "选课方面，组织中的人工智能属于必修课，其他课程应结合研究方向决定。"
    store.save_voice_transcript(
        voice.message_id,
        status="succeeded",
        transcript=transcript,
        duration_ms=18000,
        confidence=0.96,
        provider="wechat_native",
    )

    status, value = _request(
        server,
        "GET",
        "/api/overview?start=2026-08-21&end=2026-08-21",
    )

    assert status == 200
    assert value["summary"]["voice_total"] == 1
    assert value["summary"]["voice_transcribed"] == 1
    assert value["summary"]["voice_transcript_coverage"] == 1.0
    evidence_text = json.dumps(value, ensure_ascii=False)
    assert "选课" in evidence_text
    assert "秦昕老师" in evidence_text


def test_feed_is_reverse_chronological_and_cursor_paginates(running_workbench):
    server, _store = running_workbench
    path = "/api/feed?start=2026-08-21&end=2026-08-21&limit=2"
    status, first = _request(server, "GET", path)
    assert status == 200
    assert [item["message_id"] for item in first["items"]] == ["m-5", "m-4"]
    assert first["sort"] == "timestamp_desc"
    assert first["has_more"] is True
    assert first["next_cursor"]

    status, second = _request(
        server,
        "GET",
        path + "&cursor=" + first["next_cursor"],
    )
    assert status == 200
    assert [item["message_id"] for item in second["items"]] == ["m-3", "m-2"]
    assert set(item["message_id"] for item in first["items"]).isdisjoint(
        item["message_id"] for item in second["items"]
    )

    status, filtered = _request(
        server,
        "GET",
        "/api/feed?start=2026-08-21&end=2026-08-21&filter=high_signal&limit=10",
    )
    assert status == 200
    assert {item["message_id"] for item in filtered["items"]} == {"m-1", "m-3"}


def test_chats_are_enriched_and_high_signal_is_computed(running_workbench):
    server, _store = running_workbench
    status, value = _request(
        server,
        "GET",
        "/api/chats?start=2026-08-21&end=2026-08-21",
    )
    assert status == 200
    chats = {item["chat_id"]: item for item in value["items"]}
    assert chats["chat-a"]["last_message"] == "补充一下背景"
    assert chats["chat-a"]["last_timestamp"].startswith("2026-08-21T04:00")
    assert chats["chat-a"]["high_signal"] == 1
    assert chats["chat-group"]["is_group"] is True
    assert chats["chat-group"]["capture_state"] == "stale"


def test_contacts_deduplicate_and_never_use_wxid_as_display_name(running_workbench):
    server, _store = running_workbench
    status, value = _request(server, "GET", "/api/contacts")
    assert status == 200
    names = [item["display_name"] for item in value["items"]]
    assert "Alice" in names
    assert names.count("Alice") == 1
    assert all(not name.lower().startswith(("wxid_", "gh_")) for name in names)
    assert all(name not in {"alice-id", "bob-id"} for name in names)


def test_sync_runs_are_persisted_and_send_stays_locked(running_workbench):
    server, _store = running_workbench
    status, value = _request(server, "GET", "/api/sync-runs")
    assert status == 200
    assert value["available"] is True
    assert value["state"] == "available"
    assert value["items"] == []
    assert value["current"]["state"] == "idle"

    status, send = _request(
        server,
        "POST",
        "/api/send-text",
        {"chat_id": "filehelper", "content": "不能发送"},
    )
    assert status == 403
    assert send["error"] == "sending_disabled"


def test_ai_analysis_keeps_local_insights_when_model_returns_no_findings(
    running_workbench, monkeypatch
):
    server, store = running_workbench
    message = IncomingMessage(
        message_id="informative-1",
        chat_id="chat-group",
        chat_name="研发群",
        sender_id="member-2",
        sender_name="小李",
        message_type="text",
        content="我认为 agent harness 的关键是把工具边界和评测指标讲清楚，方便团队后续复用和比较。",
        timestamp=datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc),
        is_self=False,
        is_group=True,
        sender_name_source="group_nickname",
        sender_name_confidence=0.95,
        raw_message={"content": "本地测试"},
        adapter_name="workbench-test",
        adapter_version="test",
    )
    store.ingest(message, ReplyDecision(False, "fixture"), create_task=False)
    for extra in (
        IncomingMessage(
            message_id="informative-2",
            chat_id="chat-group",
            chat_name="研发群",
            sender_id="member-3",
            sender_name="小周",
            message_type="text",
            content="相比只看待办，知识解释和资源链接也应该单独保留。",
            timestamp=datetime(2026, 8, 21, 6, 5, tzinfo=timezone.utc),
            is_self=False,
            is_group=True,
            sender_name_source="group_nickname",
            sender_name_confidence=0.95,
            raw_message={"content": "本地测试"},
            adapter_name="workbench-test",
            adapter_version="test",
        ),
        IncomingMessage(
            message_id="informative-3",
            chat_id="chat-group",
            chat_name="研发群",
            sender_id="member-3",
            sender_name="小周",
            message_type="text",
            content="这类讨论可以帮助我们回看方案脉络和技术依据。",
            timestamp=datetime(2026, 8, 21, 6, 8, tzinfo=timezone.utc),
            is_self=False,
            is_group=True,
            sender_name_source="group_nickname",
            sender_name_confidence=0.95,
            raw_message={"content": "本地测试"},
            adapter_name="workbench-test",
            adapter_version="test",
        ),
    ):
        store.ingest(extra, ReplyDecision(False, "fixture"), create_task=False)

    class EmptyFindingGenerator:
        model = "fake-analysis-model"
        configured = True

        def analyze(self, window, candidates):
            assert candidates
            return {"brief": "", "themes": [], "findings": [], "limitations": []}

    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: EmptyFindingGenerator(),
    )
    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {"start": "2026-08-21", "end": "2026-08-21", "confirm": True},
    )

    assert status == 200
    assert value["source"] == "ai_assisted_with_local_fallback"
    assert value["analysis"]["findings"]
    assert any(item["category"] == "主题" for item in value["analysis"]["findings"])
    assert value["analysis"]["findings"][0]["what_changed"]
    assert value["analysis"]["discoveries"]
    assert value["analysis"]["discussion_episodes"]
