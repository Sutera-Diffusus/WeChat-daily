import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import Request, urlopen

from wechat_bridge.adapters.hook_http import HookHttpAdapter


class FakeHookHandler(BaseHTTPRequestHandler):
    events = []
    sent = []

    def do_GET(self):  # noqa: N802
        if self.path == "/QueryDB/status":
            self._write(200, {"IsLogin": 1, "hWeixin": 123})
        else:
            self._write(404, {"ret": 1})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/SendTextMsg":
            self.sent.append(payload)
            self._write(200, {"ret": 0, "retmsg": "success"})
        else:
            self._write(404, {"ret": 1})

    def _write(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_hook_health_send_and_callback_normalization():
    FakeHookHandler.events = []
    FakeHookHandler.sent = []
    hook_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHookHandler)
    hook_thread = threading.Thread(target=hook_server.serve_forever, daemon=True)
    hook_thread.start()
    hook_port = hook_server.server_address[1]
    adapter = HookHttpAdapter(
        base_url="http://127.0.0.1:%s" % hook_port,
        callback_port=0,
        target_client_version="4.1.12.26",
    )
    received = []
    try:
        adapter.connect()
        assert adapter.health_check().ok is True
        adapter.start_receive(("文件传输助手",), received.append)
        event = {
            "type": "D0003",
            "data": {
                "msgId": "m-1",
                "msgType": 1,
                "msgSource": 0,
                "fromType": 1,
                "fromWxid": "filehelper",
                "fromName": "filehelper",
                "msg": "hello",
                "timestamp": 1787227200000,
            },
        }
        request = Request(
            adapter.callback_url,
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
        deadline = time.time() + 1.0
        while time.time() < deadline and not received:
            time.sleep(0.01)
        assert len(received) == 1
        assert received[0].chat_id == "filehelper"
        assert received[0].chat_name == "文件传输助手"
        assert received[0].is_self is False
        assert received[0].content == "hello"

        result = adapter.send_text("filehelper", "文件传输助手", "reply")
        assert result.accepted is True
        assert result.confirmed is None
        assert FakeHookHandler.sent == [{"wxidorgid": "filehelper", "msg": "reply"}]
    finally:
        adapter.disconnect()
        hook_server.shutdown()
        hook_server.server_close()
        hook_thread.join(timeout=2)
