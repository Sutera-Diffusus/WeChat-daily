"""Adapter contract for WeChat providers."""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Sequence

from ..models import HealthStatus, IncomingMessage, SendResult


MessageCallback = Callable[[IncomingMessage], None]


class AdapterError(RuntimeError):
    """A visible adapter failure; the service must not silently send."""


class WeChatAdapter(ABC):
    name = "unknown"

    @property
    @abstractmethod
    def version(self):
        raise NotImplementedError

    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> HealthStatus:
        raise NotImplementedError

    @abstractmethod
    def start_receive(self, chat_names: Sequence[str], callback: MessageCallback) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_receive(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_text(self, chat_id: str, chat_name: str, content: str) -> SendResult:
        raise NotImplementedError

    def send_image(self, chat_id: str, chat_name: str, path: str) -> SendResult:
        raise AdapterError("适配器不支持发送图片")

    def send_file(self, chat_id: str, chat_name: str, path: str) -> SendResult:
        raise AdapterError("适配器不支持发送文件")

    def get_chat_history(
        self,
        chat_id: str,
        chat_name: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        raise AdapterError("适配器不支持读取聊天历史")

    def list_message_chats(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return chats that have locally readable message history.

        This is intentionally optional.  A Hook or GUI adapter may only know
        about the chats it is currently listening to, while a local database
        adapter can expose the complete message inventory.
        """
        raise AdapterError("适配器不支持枚举消息会话")

    def get_history_range(
        self,
        start_at: Any,
        end_at: Any,
        chat_ids: Any = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Return normalized history in ``[start_at, end_at)``.

        The default adapter contract deliberately does not invent a fallback
        across chats.  Implementations that can provide a complete local
        export should override this method.
        """
        raise AdapterError("适配器不支持按时间范围读取历史")

    def list_accounts(self) -> List[Dict[str, Any]]:
        raise AdapterError("适配器不支持多账号枚举")
