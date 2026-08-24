from datetime import datetime, timezone

from wechat_bridge.adapters.wechatauto_db import WeChatAutoDbAdapter


class FakeDb:
    db_dir = r"D:\WeChat\xwechat_files"
    account = "wxid_test_1234"
    unkeyed = []
    _keys = {"message/message_0.db": b"key"}

    def get_messages(self, user, limit=20):
        if user == "filehelper":
            return [
                {
                    "local_id": 1,
                    "sort_seq": 100,
                    "sender_id": 2,
                    "type": "文本",
                    "create_time": 1787227200,
                    "content": "old",
                }
            ]
        return []

    def get_sessions(self, limit=500):
        return []

    def search_contact(self, value):
        return []


class RefreshingFakeDb(FakeDb):
    def __init__(self):
        self.nickname_index_calls = 0
        self.sender_index_calls = 0
        self.current_nickname = "初始昵称"

    def _nickname_index(self):
        self.nickname_index_calls += 1
        return {"wxid_peer": self.current_nickname}

    def _sender_id_index(self):
        self.sender_index_calls += 1
        return {789: "wxid_peer"}

    def get_self_info(self):
        return {"nick_name": "测试账号"}

    def get_sessions(self, limit=500):
        return [{"username": "wxid_peer", "name": "对话对象", "message_count": 1}]

    def get_messages(self, user, limit=20):
        if user == "wxid_peer":
            return [
                {
                    "local_id": 3,
                    "sort_seq": 300,
                    "sender_id": 789,
                    "type": "文本",
                    "create_time": 1787227200,
                    "content": "历史消息",
                }
            ]
        return super().get_messages(user, limit=limit)


class FakeListener:
    def __init__(self, db, interval):
        self.db = db
        self.interval = interval
        self.callbacks = {}
        self.started = False
        self.stopped = False

    def add_listener(self, user, callback):
        self.callbacks[user] = callback

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def emit(self, user, message):
        self.callbacks[user](message, self)


class FakeResponse(dict):
    @property
    def is_success(self):
        return self.get("status") == "成功"


class FakeGui:
    def __init__(self):
        self.calls = []
        self._uia_tried = False
        self._uia = "would-be-uia"

    def send_msg(self, content, who, verify):
        self.calls.append((content, who, verify))
        return FakeResponse(status="成功", message="消息已发送并确认")


def make_adapter(db, listener, gui):
    return WeChatAutoDbAdapter(
        db_factory=lambda **kwargs: db,
        listener_factory=lambda db, interval: listener,
        gui_factory=lambda: gui,
    )


def test_local_db_adapter_normalizes_listener_messages_and_maps_filehelper():
    db = FakeDb()
    listener = FakeListener(db, 1.0)
    gui = FakeGui()
    adapter = make_adapter(db, listener, gui)
    received = []

    adapter.connect()
    assert adapter.health_check().ok is True
    adapter.start_receive(("文件传输助手",), received.append)
    listener.emit(
        "filehelper",
        {
            "local_id": 2,
            "sort_seq": 200,
            "sender_id": 7,
            "type": "文本",
            "create_time": 1787227200,
            "content": "hello",
        },
    )

    assert len(received) == 1
    assert received[0].message_id == "filehelper:2"
    assert received[0].chat_id == "filehelper"
    assert received[0].chat_name == "文件传输助手"
    assert received[0].message_type == "text"
    assert received[0].is_self is False
    assert received[0].timestamp == datetime.fromtimestamp(1787227200, timezone.utc)
    adapter.disconnect()
    assert listener.stopped is True


def test_local_db_adapter_send_uses_coordinate_path_and_requires_confirmation():
    db = FakeDb()
    listener = FakeListener(db, 1.0)
    gui = FakeGui()
    adapter = make_adapter(db, listener, gui)
    adapter.connect()
    adapter.start_receive(("文件传输助手",), lambda message: None)

    result = adapter.send_text("filehelper", "文件传输助手", "reply")

    assert result.accepted is True
    assert result.confirmed is True
    assert result.confirmation == "gui_send_confirmed"
    assert gui.calls == [("reply", "文件传输助手", False)]
    assert gui._uia_tried is True
    assert gui._uia is None
    adapter.disconnect()


def test_local_db_adapter_uses_status_for_self_detection_when_available():
    adapter = make_adapter(FakeDb(), FakeListener(FakeDb(), 1.0), FakeGui())

    outgoing = adapter._normalize_message(
        {
            "local_id": 10,
            "sender_id": 1,
            "_bridge_status": 2,
            "_bridge_shard": "message\\message_2.db",
            "type": "文本",
            "create_time": 1787227200,
            "content": "outgoing",
        },
        "filehelper",
        "文件传输助手",
    )
    incoming = adapter._normalize_message(
        {
            "local_id": 11,
            "sender_id": 2,
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227200,
            "content": "incoming",
        },
        "filehelper",
        "文件传输助手",
    )

    assert outgoing.is_self is True
    assert outgoing.message_id == "filehelper:message\\message_2.db:10"
    assert incoming.is_self is False


def test_group_identity_is_scoped_and_media_reuses_group_nickname():
    adapter = make_adapter(FakeDb(), FakeListener(FakeDb(), 1.0), FakeGui())
    # The global SenderName2Id table deliberately points to another contact.
    # A group prefix is stronger evidence and must also cover media rows that
    # do not carry the prefix themselves.
    adapter._sender_index = {789: "wxid_wrong"}
    adapter._contact_names = {"wxid_wrong": "错误联系人"}

    text = adapter._normalize_message(
        {
            "local_id": 20,
            "sender_id": 789,
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227200,
            "content": "pzc163:\n正文",
        },
        "group-a@chatroom",
        "群聊 A",
    )
    media = adapter._normalize_message(
        {
            "local_id": 21,
            "sender_id": 789,
            "_bridge_status": 3,
            "type": "图片",
            "create_time": 1787227201,
            "content": "[图片]",
        },
        "group-a@chatroom",
        "群聊 A",
    )
    other_group = adapter._normalize_message(
        {
            "local_id": 22,
            "sender_id": 789,
            "_bridge_status": 3,
            "type": "图片",
            "create_time": 1787227202,
            "content": "[图片]",
        },
        "group-b@chatroom",
        "群聊 B",
    )

    assert text.sender_name == "pzc163"
    assert media.sender_name == "pzc163"
    assert media.sender_name_source == "group_nickname"
    assert media.sender_name_confidence == 0.96
    assert other_group.sender_name == "待识别成员"
    assert other_group.sender_name_source == "unresolved"
    assert other_group.sender_name_confidence == 0.0


def test_group_wxid_prefix_resolves_contact_display_name():
    adapter = make_adapter(FakeDb(), FakeListener(FakeDb(), 1.0), FakeGui())
    adapter._contact_names = {"wxid_exact": "真实昵称"}
    message = adapter._normalize_message(
        {
            "local_id": 30,
            "sender_id": 1427,
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227200,
            "content": "wxid_exact:\n正文",
        },
        "group-a@chatroom",
        "群聊 A",
    )

    assert message.sender_name == "真实昵称"


def test_group_nickname_beats_ordinary_contact_nickname_but_remark_wins():
    adapter = make_adapter(FakeDb(), FakeListener(FakeDb(), 1.0), FakeGui())
    adapter._contact_names = {"wxid_member": "通讯录昵称"}
    adapter._contact_remarks = {"wxid_member": "通讯录备注"}
    adapter._display_identity_users = {"群内昵称": "wxid_member"}

    group_nickname = adapter._normalize_message(
        {
            "local_id": 40,
            "sender_id": 100,
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227200,
            "content": "群内昵称:\n正文",
        },
        "group-a@chatroom",
        "群聊 A",
    )
    assert group_nickname.sender_name == "通讯录备注"
    assert group_nickname.sender_name_source == "contact_remark"

    adapter._contact_remarks = {}
    group_nickname_without_remark = adapter._normalize_message(
        {
            "local_id": 41,
            "sender_id": 100,
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227201,
            "content": "群内昵称:\n正文",
        },
        "group-b@chatroom",
        "群聊 B",
    )
    assert group_nickname_without_remark.sender_name == "群内昵称"
    assert group_nickname_without_remark.sender_name_source == "group_nickname"


def test_direct_identity_uses_chat_peer_not_global_numeric_sender_map():
    adapter = make_adapter(FakeDb(), FakeListener(FakeDb(), 1.0), FakeGui())
    adapter._sender_index = {2: "wxid_wrong_contact"}
    adapter._contact_names = {
        "wxid_chat_peer": "种博来",
        "wxid_wrong_contact": "杨家麒",
    }
    message = adapter._normalize_message(
        {
            "local_id": 50,
            "sender_id": 2,
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227200,
            "content": "对话内容",
        },
        "wxid_chat_peer",
        "种博来",
    )

    assert message.sender_name == "种博来"
    assert message.sender_name_source == "direct_chat_peer"
    assert message.sender_name_confidence == 1.0


def test_identity_indexes_refresh_before_history_and_receive():
    db = RefreshingFakeDb()
    listener = FakeListener(db, 1.0)
    adapter = make_adapter(db, listener, FakeGui())
    received = []

    adapter.connect()
    connect_calls = db.nickname_index_calls
    db.current_nickname = "更新后的昵称"

    history = adapter.get_chat_history("wxid_peer", "对话对象", limit=10)
    range_history = adapter.get_history_range(
        datetime.fromtimestamp(1787227199, timezone.utc),
        datetime.fromtimestamp(1787227201, timezone.utc),
        chat_ids=("wxid_peer",),
    )

    assert db.nickname_index_calls > connect_calls
    assert db.sender_index_calls >= 3
    assert history[0]["sender_name"] == "更新后的昵称"
    assert range_history[0]["sender_name"] == "更新后的昵称"

    before_receive = db.nickname_index_calls
    adapter.start_receive(("文件传输助手",), received.append)
    listener.emit(
        "filehelper",
        {
            "local_id": 4,
            "sort_seq": 400,
            "sender_id": 7,
            "type": "文本",
            "create_time": 1787227200,
            "content": "收到",
        },
    )

    assert db.nickname_index_calls > before_receive
    assert len(received) == 1
    adapter.disconnect()


def test_unknown_identity_never_leaks_wxid_and_reports_anomaly_metadata():
    adapter = make_adapter(FakeDb(), FakeListener(FakeDb(), 1.0), FakeGui())
    message = adapter._normalize_message(
        {
            "local_id": 60,
            "sender_id": "wxid_not_in_contacts",
            "_bridge_status": 3,
            "type": "文本",
            "create_time": 1787227200,
            "content": "正文",
        },
        "unknown-group@chatroom",
        "未知群聊",
    )

    assert message.sender_name == "待识别成员"
    assert message.sender_name_source == "unresolved"
    assert message.sender_name_confidence == 0.0
    assert "wxid" not in message.sender_name.lower()
