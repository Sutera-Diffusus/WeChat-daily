"""wxauto4 adapter with listener and polling compatibility paths."""

import hashlib
import importlib.metadata
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from ..models import HealthStatus, IncomingMessage, SendResult, utc_now
from .base import AdapterError, MessageCallback, WeChatAdapter


class WxAuto4Adapter(WeChatAdapter):
    name = "wxauto4"

    def __init__(self, client: Any = None, poll_interval: float = 1.0) -> None:
        self._client = client
        self._poll_interval = max(0.2, poll_interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[MessageCallback] = None
        self._chat_names = []
        self._chat_windows: Dict[str, Any] = {}
        self._mode = "not_started"
        self._last_error: Optional[str] = None
        self._version = self._detect_package_version()

    @property
    def version(self) -> Optional[str]:
        return self._version

    @staticmethod
    def _detect_package_version() -> Optional[str]:
        try:
            return importlib.metadata.version("wxauto4")
        except importlib.metadata.PackageNotFoundError:
            return None

    def connect(self) -> None:
        if self._client is not None:
            return
        try:
            from wxauto4 import WeChat
        except Exception as exc:
            raise AdapterError(
                "无法导入 wxauto4；请在当前 Python 环境安装固定版本 wxauto4。"
            ) from exc
        try:
            try:
                self._client = WeChat(ads=False)
            except TypeError:
                # Older wxauto4 builds do not expose the ads keyword.
                self._client = WeChat()
        except Exception as exc:
            raise AdapterError(
                "无法连接微信客户端。请先启动并登录微信，再重试；"
                "不会在连接失败时继续发送。原因: %s" % exc
            ) from exc

    def disconnect(self) -> None:
        self.stop_receive()
        self._chat_windows.clear()
        # Do not close or kill the user's WeChat process here.
        self._client = None

    def health_check(self) -> HealthStatus:
        if self._client is None:
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="wxauto4 未连接微信客户端",
            )
        details: Dict[str, Any] = {"receive_mode": self._mode}
        try:
            online_method = getattr(self._client, "IsOnline", None)
            online = bool(online_method()) if callable(online_method) else True
            path = getattr(self._client, "path", None)
            if path:
                details["wechat_path"] = str(path)
            if self._last_error:
                details["last_error"] = self._last_error
            return HealthStatus(
                ok=online,
                adapter_name=self.name,
                adapter_version=self.version,
                message="微信客户端在线" if online else "微信客户端不在线",
                details=details,
            )
        except Exception as exc:
            details["error"] = str(exc)
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="wxauto4 健康检查失败",
                details=details,
            )

    def start_receive(self, chat_names: Sequence[str], callback: MessageCallback) -> None:
        if self._client is None:
            raise AdapterError("必须先 connect() 才能开始接收消息")
        names = [str(name) for name in chat_names if str(name).strip()]
        if not names:
            raise AdapterError("至少需要一个监听聊天对象")
        self._chat_names = names
        self._callback = callback
        self._stop.clear()

        listener = getattr(self._client, "AddListenChat", None)
        if callable(listener):
            self._mode = "callback"

            def on_message(raw_message: Any, chat: Any) -> None:
                try:
                    normalized = self._normalize_message(raw_message, chat, None)
                    callback(normalized)
                except Exception as exc:
                    self._last_error = "消息回调解析失败: %s" % exc

            try:
                listener(names[0] if len(names) == 1 else names, on_message)
                return
            except Exception as exc:
                self._last_error = "wxauto4 回调监听启动失败: %s" % exc
                raise AdapterError(self._last_error) from exc

        # wxauto4 41.1.2 exposes GetSubWindow/GetAllMessage but not the
        # documented AddListenChat API. Poll only the explicitly allowlisted
        # chats as a compatibility fallback.
        self._mode = "polling"
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="wechat-bridge-receiver",
            daemon=True,
        )
        self._thread.start()

    def stop_receive(self) -> None:
        self._stop.set()
        stopper = getattr(self._client, "StopListening", None)
        if callable(stopper):
            try:
                stopper()
            except Exception as exc:
                self._last_error = "wxauto4 停止监听失败: %s" % exc
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(2.0, self._poll_interval + 1.0))
        self._thread = None
        self._mode = "stopped"

    def send_text(self, chat_id: str, chat_name: str, content: str) -> SendResult:
        del chat_id  # wxauto4 routes by the chat window/name.
        if self._client is None:
            raise AdapterError("wxauto4 未连接，拒绝发送")
        if not content:
            return SendResult(False, None, "not_sent", error="消息内容为空")

        chat = self._chat_windows.get(chat_name)
        if chat is None:
            chat = self._get_chat_window(chat_name)
            self._chat_windows[chat_name] = chat
        try:
            send_method = getattr(chat, "SendMsg", None)
            if not callable(send_method):
                raise AdapterError("目标聊天窗口没有 SendMsg 方法")
            if chat is self._client:
                response = send_method(content, chat_name)
            else:
                response = send_method(content)
        except Exception as exc:
            return SendResult(False, None, "send_exception", error=str(exc))

        accepted = self._response_accepted(response)
        raw_response = self._safe_response_text(response)
        if not accepted:
            return SendResult(
                False,
                False,
                "api_rejected_or_unconfirmed",
                raw_response=raw_response,
                error="wxauto4 未返回明确成功结果",
            )

        confirmed = self._confirm_visible_message(chat, content)
        confirmation = "message_visible" if confirmed else "api_response_unverified"
        return SendResult(
            True,
            confirmed,
            confirmation,
            raw_response=raw_response,
        )

    def _poll_loop(self) -> None:
        seen: Dict[str, set] = {name: set() for name in self._chat_names}
        initialized = set()
        while not self._stop.is_set():
            for chat_name in self._chat_names:
                if self._stop.is_set():
                    break
                try:
                    chat = self._chat_windows.get(chat_name)
                    if chat is None:
                        chat = self._get_chat_window(chat_name)
                        self._chat_windows[chat_name] = chat
                    raw_messages = list(chat.GetAllMessage())
                    if chat_name not in initialized:
                        seen[chat_name].update(
                            self._raw_fingerprint(raw, chat_name) for raw in raw_messages
                        )
                        initialized.add(chat_name)
                        continue
                    for raw_message in raw_messages:
                        fingerprint = self._raw_fingerprint(raw_message, chat_name)
                        if fingerprint in seen[chat_name]:
                            continue
                        seen[chat_name].add(fingerprint)
                        normalized = self._normalize_message(raw_message, chat, chat_name)
                        if self._callback:
                            self._callback(normalized)
                except Exception as exc:
                    self._chat_windows.pop(chat_name, None)
                    self._last_error = "轮询 %s 失败: %s" % (chat_name, exc)
            self._stop.wait(self._poll_interval)

    def _get_chat_window(self, chat_name: str) -> Any:
        get_subwindow = getattr(self._client, "GetSubWindow", None)
        if callable(get_subwindow):
            return get_subwindow(chat_name)
        chat_with = getattr(self._client, "ChatWith", None)
        if callable(chat_with):
            chat_with(chat_name)
            return self._client
        raise AdapterError("wxauto4 没有 GetSubWindow/ChatWith，无法读取聊天")

    def _normalize_message(
        self, raw_message: Any, chat: Any = None, default_chat: Optional[str] = None
    ) -> IncomingMessage:
        chat_name = self._chat_name(chat) or default_chat or "未知聊天"
        content = self._value(raw_message, "content", "")
        content = "" if content is None else str(content)
        message_type = self._value(raw_message, "type", "message_type", default="text")
        message_type = str(message_type or "text")
        sender_name = self._value(raw_message, "sender_name", "sender_remark", "sender")
        sender_name = None if sender_name is None else str(sender_name)
        sender_id = self._value(raw_message, "sender_id", "sender_wxid")
        sender_id = None if sender_id is None else str(sender_id)
        is_self = self._is_self(raw_message, sender_name)
        timestamp = self._parse_timestamp(
            self._value(raw_message, "timestamp", "time", "datetime")
        )
        chat_id_value = self._value(chat, "chat_id", "wxid", "id")
        if not chat_id_value:
            chat_info = self._call_chat_info(chat)
            chat_id_value = chat_info.get("chat_id") or chat_info.get("wxid")
        chat_id = str(chat_id_value or "%s:%s" % (self.name, chat_name))
        adapter_message_id = self._value(raw_message, "message_id", "msg_id", "id")
        if adapter_message_id is not None:
            message_id = "%s:%s" % (chat_id, adapter_message_id)
        else:
            message_id = self._raw_fingerprint(raw_message, chat_name, timestamp)
        raw = self._safe_raw_message(raw_message)
        return IncomingMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender_name,
            message_type=message_type,
            content=content,
            timestamp=timestamp,
            is_self=is_self,
            raw_message=raw,
            adapter_name=self.name,
            adapter_version=self.version,
        )

    def _raw_fingerprint(
        self, raw_message: Any, chat_name: str, timestamp: Optional[datetime] = None
    ) -> str:
        raw_id = self._value(raw_message, "message_id", "msg_id", "id")
        if raw_id is not None:
            return "%s:%s" % (chat_name, raw_id)
        parts = [
            chat_name,
            str(self._value(raw_message, "sender_id", "sender_wxid", "sender") or ""),
            str(self._value(raw_message, "type", "message_type") or "text"),
            str(self._value(raw_message, "content", "") or ""),
            str(self._value(raw_message, "timestamp", "time", "datetime") or timestamp or ""),
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _value(obj: Any, *names: str, default: Any = None) -> Any:
        if obj is None:
            return default
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj[name]
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if value is not None:
                return value
        return default

    def _chat_name(self, chat: Any) -> Optional[str]:
        if isinstance(chat, str):
            return chat
        value = self._value(chat, "who", "chat_name", "name")
        if value:
            return str(value)
        info = self._call_chat_info(chat)
        value = info.get("chat_name") or info.get("name")
        return str(value) if value else None

    def _call_chat_info(self, chat: Any) -> Dict[str, Any]:
        method = getattr(chat, "ChatInfo", None) if chat is not None else None
        if not callable(method):
            return {}
        try:
            value = method()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _is_self(self, raw_message: Any, sender_name: Optional[str]) -> Optional[bool]:
        value = self._value(raw_message, "is_self")
        if isinstance(value, bool):
            return value
        attr = str(self._value(raw_message, "attr", "direction", default="")).lower()
        if attr in {"self", "out", "outgoing", "自己"}:
            return True
        if attr in {"friend", "in", "incoming", "对方"}:
            return False
        if sender_name in {"我", "自己"}:
            return True
        return None

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            text = value.strip().replace("Z", "+00:00")
            dt = None
            for candidate in (text, text.replace("年", "-").replace("月", "-").replace("日", "")):
                try:
                    dt = datetime.fromisoformat(candidate)
                    break
                except ValueError:
                    pass
            if dt is None:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M"):
                    try:
                        dt = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        pass
            if dt is None:
                dt = utc_now()
        else:
            dt = utc_now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _safe_raw_message(self, raw_message: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key in (
            "id", "message_id", "msg_id", "content", "sender", "sender_name",
            "sender_remark", "sender_id", "type", "message_type", "attr",
            "direction", "hash", "time", "timestamp", "is_self",
        ):
            value = self._value(raw_message, key)
            if value is not None:
                result[key] = self._json_safe(value)
        raw_value = self._value(raw_message, "raw")
        if raw_value is not None and isinstance(raw_value, (str, int, float, bool, dict, list)):
            result["raw"] = self._json_safe(raw_value)
        return result

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(v) for v in value]
        return str(value)

    @classmethod
    def _response_accepted(cls, response: Any) -> bool:
        if response is None:
            return False
        if isinstance(response, bool):
            return response
        is_success = getattr(response, "is_success", None)
        if is_success is not None:
            try:
                return bool(is_success)
            except Exception:
                return False
        if isinstance(response, dict):
            if "success" in response:
                return bool(response["success"])
            status = str(response.get("status", "")).lower()
            return status in {"成功", "success", "ok", "sent"}
        return False

    @classmethod
    def _safe_response_text(cls, response: Any) -> Optional[str]:
        try:
            return json.dumps(cls._json_safe(response), ensure_ascii=False, default=str)
        except Exception:
            return str(response)

    def _confirm_visible_message(self, chat: Any, content: str) -> Optional[bool]:
        getter = getattr(chat, "GetAllMessage", None)
        if not callable(getter):
            return None
        try:
            messages = list(getter())[-8:]
            for raw_message in reversed(messages):
                if str(self._value(raw_message, "content", "") or "") != content:
                    continue
                if self._is_self(raw_message, self._value(raw_message, "sender")) is True:
                    return True
            return False
        except Exception:
            return None
