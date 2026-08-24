"""Data models shared by adapters, persistence and the reply service."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IncomingMessage:
    """Normalized message independent of the underlying WeChat adapter."""

    message_id: str
    chat_id: str
    chat_name: str
    sender_id: Optional[str]
    sender_name: Optional[str]
    message_type: str
    content: str
    timestamp: datetime
    is_self: Optional[bool]
    raw_message: Dict[str, Any] = field(default_factory=dict)
    adapter_name: str = "unknown"
    adapter_version: Optional[str] = None
    is_group: bool = False
    # Media is deliberately metadata-only in the first pass.  The adapter may
    # provide a discovered local cache path/name, but it must never pretend a
    # binary was decoded when WeChat has not exposed it.
    media_path: Optional[str] = None
    media_name: Optional[str] = None
    media_md5: Optional[str] = None
    # Identity provenance lets analysis distinguish a group nickname from a
    # low-confidence fallback without exposing internal ids to the UI/model.
    sender_name_source: Optional[str] = None
    sender_name_confidence: Optional[float] = None


@dataclass(frozen=True)
class SendResult:
    """Outcome of a send attempt.

    ``accepted`` means the adapter reported a successful send operation.
    ``confirmed`` is a best-effort UI/message-list confirmation and may be
    ``None`` when the adapter cannot perform one.
    """

    accepted: bool
    confirmed: Optional[bool]
    confirmation: str
    raw_response: Optional[str] = None
    error: Optional[str] = None
    sent_message_id: Optional[str] = None


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    adapter_name: str
    adapter_version: Optional[str]
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncRun:
    """Durable outcome of a date-range history synchronization run."""

    job_id: str
    range_start: str
    range_end: str
    scope: str
    status: str
    started_at: str
    seen: int = 0
    inserted: int = 0
    chat_count: int = 0
    error: Optional[str] = None
    finished_at: Optional[str] = None
    row_id: Optional[int] = None

    @property
    def state(self) -> str:
        """Alias for the in-memory status field used by the current API."""

        return self.status

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly representation for future API consumers."""

        return {
            "id": self.row_id,
            "job_id": self.job_id,
            "range": {"start": self.range_start, "end": self.range_end},
            "range_start": self.range_start,
            "range_end": self.range_end,
            "scope": self.scope,
            "status": self.status,
            "state": self.status,
            "seen": self.seen,
            "inserted": self.inserted,
            "chat_count": self.chat_count,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class ReplyDecision:
    should_reply: bool
    reason: str
    reply_text: Optional[str] = None


@dataclass(frozen=True)
class ReplyTask:
    task_id: int
    message_row_id: int
    message_id: str
    chat_id: str
    chat_name: str
    reply_text: str
    status: str
    attempts: int
    last_error: Optional[str]
