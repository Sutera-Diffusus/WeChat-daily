"""HTTP adapter for a locally injected WeChat Hook service.

The first provider profile follows aixed/WeChat-Hook's HTTP shape:

* ``GET /QueryDB/status`` for login health;
* ``POST /SendTextMsg`` with ``wxidorgid`` and ``msg`` for text sending;
* a callback URL receiving ``D0003`` message events.

This module only talks to an already-running local HTTP service. It does not
download, inject or replace a DLL.
"""

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..models import HealthStatus, IncomingMessage, SendResult, utc_now
from .base import AdapterError, MessageCallback, WeChatAdapter

logger = logging.getLogger("wechat_bridge.hook_http")


class HookHttpAdapter(WeChatAdapter):
    """Adapter for a local HTTP Hook endpoint and callback receiver."""

    name = "hook_http"

    _MESSAGE_TYPES = {
        1: "text",
        3: "image",
        34: "voice",
        42: "personal_card",
        43: "video",
        47: "emoji",
        48: "location",
        49: "link_or_file",
        10000: "system",
        10002: "recalled",
    }

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:30001",
        callback_host: str = "127.0.0.1",
        callback_port: int = 30000,
        callback_path: str = "/wechat/",
        callback_advertise_host: Optional[str] = None,
        status_path: str = "/QueryDB/status",
        send_path: str = "/SendTextMsg",
        timeout: float = 3.0,
        target_client_version: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.callback_host = callback_host
        self.callback_port = int(callback_port)
        self.callback_path = self._normalize_path(callback_path)
        self.callback_advertise_host = callback_advertise_host or callback_host
        self.status_path = self._normalize_path(status_path)
        self.send_path = self._normalize_path(send_path)
        self.timeout = max(0.2, float(timeout))
        self.target_client_version = target_client_version
        self._connected = False
        self._callback: Optional[MessageCallback] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None
        self._actual_callback_url: Optional[str] = None

    @property
    def version(self) -> str:
        target = self.target_client_version or "unknown-client"
        return "hook-http/target-unverified:%s" % target

    @property
    def callback_url(self) -> Optional[str]:
        """URL to pass to the Hook's ``CallBackURL`` parameter."""
        return self._actual_callback_url

    def connect(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AdapterError("Hook base URL 无效: %s" % self.base_url)
        self._connected = True

    def disconnect(self) -> None:
        self.stop_receive()
        self._connected = False

    def health_check(self) -> HealthStatus:
        if not self._connected:
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="Hook HTTP 适配器尚未连接",
            )
        try:
            payload = self._request_json("GET", self.status_path)
        except AdapterError as exc:
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="Hook HTTP 健康检查失败: %s" % exc,
                details={"base_url": self.base_url},
            )

        login_value = payload.get("IsLogin") if isinstance(payload, dict) else None
        logged_in = login_value in (1, "1", True, "true", "True")
        details = {
            "base_url": self.base_url,
            "status_path": self.status_path,
            "raw_status": payload,
        }
        if self.target_client_version:
            details["target_client_version"] = self.target_client_version
        if not logged_in:
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="Hook 服务可访问，但未确认微信已登录",
                details=details,
            )
        return HealthStatus(
            ok=True,
            adapter_name=self.name,
            adapter_version=self.version,
            message="Hook 服务在线且微信已登录",
            details=details,
        )

    def start_receive(self, chat_names: Sequence[str], callback: MessageCallback) -> None:
        del chat_names  # Policy filtering happens after normalization.
        if not self._connected:
            raise AdapterError("必须先 connect() 才能启动 Hook 回调接收")
        if self._server is not None:
            raise AdapterError("Hook 回调接收已经启动")
        self._callback = callback
        try:
            server = ThreadingHTTPServer(
                (self.callback_host, self.callback_port),
                _HookCallbackHandler,
            )
        except OSError as exc:
            raise AdapterError(
                "无法监听 Hook 回调地址 %s:%s: %s"
                % (self.callback_host, self.callback_port, exc)
            ) from exc
        server.adapter = self  # type: ignore[attr-defined]
        self._server = server
        actual_port = int(server.server_address[1])
        self._actual_callback_url = "http://%s:%s%s" % (
            self.callback_advertise_host,
            actual_port,
            self.callback_path,
        )
        self._server_thread = threading.Thread(
            target=server.serve_forever,
            name="wechat-hook-callback",
            daemon=True,
        )
        self._server_thread.start()
        logger.info(
            "Hook callback listening at %s; configure the Hook DLL CallBackURL to this URL",
            self._actual_callback_url,
        )

    def stop_receive(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2.0)
        self._server_thread = None
        self._actual_callback_url = None
        self._callback = None

    def send_text(self, chat_id: str, chat_name: str, content: str) -> SendResult:
        del chat_name
        if not self._connected:
            raise AdapterError("Hook HTTP 适配器未连接，拒绝发送")
        if not chat_id:
            return SendResult(False, False, "missing_chat_id", error="Hook 发送需要 wxid")
        if not content:
            return SendResult(False, False, "empty_content", error="消息内容为空")
        try:
            response = self._request_json(
                "POST",
                self.send_path,
                {"wxidorgid": chat_id, "msg": content},
            )
        except AdapterError as exc:
            return SendResult(False, False, "http_error", error=str(exc))

        accepted = self._response_accepted(response)
        raw_response = self._json_text(response)
        if accepted:
            return SendResult(
                accepted=True,
                confirmed=None,
                confirmation="hook_ret_0_unverified",
                raw_response=raw_response,
            )
        return SendResult(
            accepted=False,
            confirmed=False,
            confirmation="hook_rejected",
            raw_response=raw_response,
            error="Hook 服务未返回 ret=0 成功结果",
        )

    def _handle_callback(self, payload: Any) -> None:
        try:
            message = self._normalize_event(payload)
            if message is not None and self._callback is not None:
                self._callback(message)
        except Exception as exc:
            self._last_error = "Hook 回调解析失败: %s" % exc
            logger.exception(self._last_error)

    def _normalize_event(self, payload: Any) -> Optional[IncomingMessage]:
        if not isinstance(payload, dict) or payload.get("type") != "D0003":
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        chat_id = data.get("fromWxid") or data.get("wxid") or data.get("toWxid")
        if not chat_id:
            return None
        chat_id = str(chat_id)
        from_type = self._as_int(data.get("fromType"))
        mapped_chat_name = {"filehelper": "文件传输助手"}.get(chat_id)
        chat_name = str(
            mapped_chat_name
            or data.get("fromName")
            or data.get("chatName")
            or chat_id
        )
        sender_id = data.get("finalFromWxid") or data.get("fromWxid")
        sender_name = data.get("finalFromName") or data.get("senderName")
        source = data.get("msgSource")
        is_self = None if source is None else self._as_int(source) == 1
        msg_type_value = self._as_int(data.get("msgType"))
        message_type = self._MESSAGE_TYPES.get(msg_type_value, "other")
        content = data.get("msg") or ""
        content = str(content)
        timestamp = self._parse_timestamp(data.get("timestamp") or payload.get("timestamp"))
        raw_id = data.get("msgId") or data.get("msgid") or data.get("id")
        if raw_id is None:
            raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            raw_id = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        return IncomingMessage(
            message_id="%s:%s" % (chat_id, raw_id),
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=str(sender_id) if sender_id is not None else None,
            sender_name=str(sender_name) if sender_name is not None else None,
            message_type=message_type,
            content=content,
            timestamp=timestamp,
            is_self=is_self,
            raw_message=payload,
            adapter_name=self.name,
            adapter_version=self.version,
        )

    def _request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + self._normalize_path(path)
        body = None
        headers = {"Accept": "application/json", "User-Agent": "local-wechat-bridge/0.1"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = int(response.status)
                text = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AdapterError("Hook HTTP %s %s: %s %s" % (method, url, exc.code, detail)) from exc
        except URLError as exc:
            raise AdapterError("Hook HTTP 无法连接 %s: %s" % (url, exc.reason)) from exc
        except OSError as exc:
            raise AdapterError("Hook HTTP 请求失败 %s: %s" % (url, exc)) from exc
        if status < 200 or status >= 300:
            raise AdapterError("Hook HTTP 返回状态码 %s: %s" % (status, text))
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AdapterError("Hook HTTP 返回不是合法 JSON: %s" % text[:300]) from exc

    @staticmethod
    def _response_accepted(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        if "ret" in response:
            return HookHttpAdapter._as_int(response.get("ret")) == 0
        if "code" in response:
            return HookHttpAdapter._as_int(response.get("code")) == 0
        if "success" in response:
            return bool(response.get("success"))
        return False

    @staticmethod
    def _json_text(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _normalize_path(path: str) -> str:
        value = str(path or "/")
        return value if value.startswith("/") else "/" + value

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            try:
                number = float(text)
                return HookHttpAdapter._parse_timestamp(number)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
                except ValueError:
                    pass
        return utc_now()


class _HookCallbackHandler(BaseHTTPRequestHandler):
    """Minimal callback endpoint; the Hook receives a fast JSON response."""

    server: ThreadingHTTPServer

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        adapter = getattr(self.server, "adapter", None)
        expected_path = getattr(adapter, "callback_path", "/wechat/")
        if self.path.rstrip("/") != expected_path.rstrip("/"):
            self._write_json(404, {"code": 404, "msg": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(400, {"code": 400, "msg": "invalid json: %s" % exc})
            return
        if adapter is not None:
            adapter._handle_callback(payload)
        self._write_json(200, {"code": 200, "msg": "ok"})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._write_json(200, {"code": 200, "msg": "callback ready"})

    def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("Hook callback: " + format, *args)
