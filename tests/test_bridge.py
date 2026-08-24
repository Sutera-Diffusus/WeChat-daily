import time
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.engine import ReplyPolicy
from wechat_bridge.models import HealthStatus, IncomingMessage, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.store import SQLiteStore


class FakeAdapter(WeChatAdapter):
    name = "fake"

    def __init__(self):
        self.callback = None
        self.sent = []
        self.connected = False

    @property
    def version(self):
        return "test"

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def health_check(self):
        return HealthStatus(True, self.name, self.version, "ok")

    def start_receive(self, chat_names, callback):
        self.callback = callback

    def stop_receive(self):
        self.callback = None

    def send_text(self, chat_id, chat_name, content):
        self.sent.append((chat_id, chat_name, content))
        return SendResult(True, True, "test_confirmed", raw_response="ok")

    def emit(self, message):
        assert self.callback is not None
        self.callback(message)


def message(
    message_id="m1",
    chat_name="文件传输助手",
    is_self=False,
    message_type="text",
    sender_name="测试联系人",
    sender_name_source=None,
    sender_name_confidence=None,
):
    return IncomingMessage(
        message_id=message_id,
        chat_id="filehelper" if chat_name == "文件传输助手" else "fake:%s" % chat_name,
        chat_name=chat_name,
        sender_id="sender",
        sender_name=sender_name,
        message_type=message_type,
        content="你好",
        timestamp=datetime.now(timezone.utc),
        is_self=is_self,
        raw_message={"id": message_id, "content": "你好"},
        adapter_name="fake",
        adapter_version="test",
        sender_name_source=sender_name_source,
        sender_name_confidence=sender_name_confidence,
    )


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_duplicate_message_is_stored_once_and_sent_once():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = FakeAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定回复", ("文件传输助手",)),
            ("文件传输助手",),
        )
        service.start()
        try:
            incoming = message()
            adapter.emit(incoming)
            adapter.emit(incoming)
            assert wait_until(lambda: len(adapter.sent) == 1)
            assert store.count_messages() == 1
            assert store.count_tasks("succeeded") == 1
        finally:
            service.stop()
            store.close()


def test_self_unknown_and_non_allowlisted_messages_are_recorded_but_not_replied():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        policy = ReplyPolicy.from_values("固定回复", ("文件传输助手",))
        self_decision = policy.decide(message(message_id="self", is_self=True))
        unknown_decision = policy.decide(message(message_id="unknown", is_self=None))
        other_decision = policy.decide(message(message_id="other", chat_name="其他聊天"))
        assert self_decision.reason == "self_message"
        assert unknown_decision.reason == "self_state_unknown"
        assert other_decision.reason == "chat_not_allowlisted"
        store.ingest(message(message_id="self", is_self=True), self_decision)
        store.ingest(message(message_id="unknown", is_self=None), unknown_decision)
        store.ingest(message(message_id="other", chat_name="其他聊天"), other_decision)
        assert store.count_messages() == 3
        assert store.count_tasks("skipped") == 3
        store.close()


def test_pending_task_recovered_after_service_restart():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        policy = ReplyPolicy.from_values("固定回复", ("文件传输助手",))
        decision = policy.decide(message(message_id="restart"))
        result = store.ingest(message(message_id="restart"), decision)
        assert result.task_id is not None
        assert store.claim_pending_task(result.task_id) is not None

        adapter = FakeAdapter()
        service = BridgeService(adapter, store, policy, ("文件传输助手",))
        service.start()
        try:
            assert wait_until(lambda: len(adapter.sent) == 1)
            assert store.count_tasks("succeeded") == 1
        finally:
            service.stop()
            store.close()


def test_filehelper_only_gate_records_but_never_queues_other_chat():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = FakeAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定回复", ("其他聊天",)),
            ("其他聊天",),
            dry_run=False,
        )
        service.start()
        try:
            adapter.emit(message(message_id="other-scope", chat_name="其他聊天"))
            assert wait_until(lambda: store.count_tasks("skipped") == 1)
            assert adapter.sent == []
        finally:
            service.stop()
            store.close()


def test_duplicate_message_keeps_higher_confidence_identity():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        policy = ReplyPolicy.from_values("固定回复", ("文件传输助手",))

        reliable = message(
            sender_name="通讯录备注",
            sender_name_source="contact_remark",
            sender_name_confidence=0.98,
        )
        weak = message(
            sender_name="群成员",
            sender_name_source="unresolved",
            sender_name_confidence=0.10,
        )
        store.ingest(reliable, policy.decide(reliable), create_task=False)
        store.ingest(weak, policy.decide(weak), create_task=False)

        rows = store.recent_messages(limit=10)
        assert len(rows) == 1
        assert rows[0]["sender_name"] == "通讯录备注"
        assert rows[0]["sender_name_source"] == "contact_remark"
        assert rows[0]["sender_name_confidence"] == 0.98
        store.close()


def test_duplicate_message_refreshes_equal_confidence_alias():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        policy = ReplyPolicy.from_values("固定回复", ("文件传输助手",))
        old = message(
            sender_name="旧群昵称",
            sender_name_source="group_nickname_cached",
            sender_name_confidence=0.86,
        )
        new = message(
            sender_name="新群昵称",
            sender_name_source="group_nickname_cached",
            sender_name_confidence=0.86,
        )
        store.ingest(old, policy.decide(old), create_task=False)
        store.ingest(new, policy.decide(new), create_task=False)

        row = store.recent_messages(limit=1)[0]
        assert row["sender_name"] == "新群昵称"
        assert row["sender_name_source"] == "group_nickname_cached"
        assert row["sender_name_confidence"] == 0.86
        store.close()


def test_duplicate_message_rebinds_direct_peer_identity():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        policy = ReplyPolicy.from_values("固定回复", ("种博来",))
        old = message(
            chat_name="种博来",
            sender_name="杨家麒",
            sender_name_source="contact_remark",
            sender_name_confidence=0.98,
        )
        new = message(
            chat_name="种博来",
            sender_name="种博来",
            sender_name_source="direct_chat_peer",
            sender_name_confidence=1.0,
        )
        store.ingest(old, policy.decide(old), create_task=False)
        store.ingest(new, policy.decide(new), create_task=False)

        row = store.recent_messages(limit=1)[0]
        assert row["sender_name"] == "种博来"
        assert row["sender_name_source"] == "direct_chat_peer"
        assert row["sender_name_confidence"] == 1.0
        store.close()
