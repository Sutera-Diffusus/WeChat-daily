import time
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.engine import ReplyPolicy
from wechat_bridge.models import HealthStatus, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.store import SQLiteStore


class HistoryAdapter(WeChatAdapter):
    name = "sync-test"

    def __init__(self, items=None, error=None):
        self.items = list(items or [])
        self.error = error
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
        return SendResult(True, True, "test_confirmed")

    def get_history_range(self, start_at, end_at, chat_ids=None, limit=50_000):
        if self.error is not None:
            raise self.error
        return list(self.items)


def history_item(message_id, chat_id, chat_name):
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "sender_id": "sender-1",
        "sender_name": "测试联系人",
        "message_type": "text",
        "content": "历史消息",
        "timestamp": "2026-08-21T04:00:00+00:00",
        "is_self": False,
        "raw_message": {"message_id": message_id},
    }


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_sync_runs_are_persisted_and_queryable_after_reopen():
    with TemporaryDirectory() as tmp:
        db_path = tmp + "/bridge.db"
        store = SQLiteStore(db_path)
        store.create_sync_run(
            job_id="job-1",
            range_start="2026-08-20T00:00:00+00:00",
            range_end="2026-08-21T00:00:00+00:00",
            scope="all",
            started_at="2026-08-21T01:00:00+00:00",
        )
        store.update_sync_run(
            "job-1",
            status="succeeded",
            seen=12,
            inserted=9,
            chat_count=3,
            error=None,
            finished_at="2026-08-21T01:00:05+00:00",
        )
        store.close()

        reopened = SQLiteStore(db_path)
        run = reopened.get_sync_run("job-1")
        assert run is not None
        assert run.status == "succeeded"
        assert run.range_start == "2026-08-20T00:00:00+00:00"
        assert run.range_end == "2026-08-21T00:00:00+00:00"
        assert run.seen == 12
        assert run.inserted == 9
        assert run.chat_count == 3
        assert run.error is None
        assert run.finished_at == "2026-08-21T01:00:05+00:00"
        assert reopened.recent_sync_runs(limit=1)[0].job_id == "job-1"
        reopened.close()


def test_service_persists_successful_history_sync_and_keeps_runtime_status():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = HistoryAdapter(
            [
                history_item("history-1", "chat-a", "会话 A"),
                history_item("history-2", "chat-b", "会话 B"),
            ]
        )
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定回复", ("文件传输助手",)),
            ("文件传输助手",),
            send_enabled=False,
        )
        service.start()
        try:
            started = service.start_history_sync(
                datetime(2026, 8, 20, tzinfo=timezone.utc),
                datetime(2026, 8, 21, tzinfo=timezone.utc),
                scope="all",
            )
            assert started["state"] == "running"
            assert wait_until(
                lambda: service.history_sync_status()["state"] == "succeeded"
            )

            current = service.history_sync_status()
            run = store.get_sync_run(started["job_id"])
            assert run is not None
            assert run.status == "succeeded"
            assert run.seen == 2
            assert run.inserted == 2
            assert run.chat_count == 2
            assert run.job_id == current["job_id"]
            assert current["state"] == "succeeded"
        finally:
            service.stop()
            store.close()


def test_service_persists_failed_history_sync_with_error():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = HistoryAdapter(error=RuntimeError("history source unavailable"))
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定回复", ("文件传输助手",)),
            ("文件传输助手",),
            send_enabled=False,
        )
        service.start()
        try:
            started = service.start_history_sync(
                datetime(2026, 8, 20, tzinfo=timezone.utc),
                datetime(2026, 8, 21, tzinfo=timezone.utc),
                scope="filehelper",
            )
            assert wait_until(
                lambda: service.history_sync_status()["state"] == "failed"
            )

            run = store.get_sync_run(started["job_id"])
            assert run is not None
            assert run.status == "failed"
            assert run.scope == "filehelper"
            assert run.error == "history source unavailable"
            assert run.finished_at is not None
        finally:
            service.stop()
            store.close()
