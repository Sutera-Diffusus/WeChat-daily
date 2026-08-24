"""SQLite persistence for messages, reply tasks and send attempts."""

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .models import IncomingMessage, ReplyDecision, ReplyTask, SendResult, SyncRun


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IngestResult:
    inserted: bool
    message_row_id: Optional[int]
    task_id: Optional[int]
    reason: str


_UNSET = object()
_SYNC_RUN_STATUSES = frozenset({"running", "succeeded", "failed"})


def _sync_time_text(value: Optional[Union[str, datetime]]) -> str:
    if value is None:
        return _now_text()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


class SQLiteStore:
    """Small thread-safe SQLite store.

    A single connection is protected by a re-entrant lock. WAL is enabled
    when SQLite supports it, so future readers do not unnecessarily block the
    receive/worker path.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    adapter_version TEXT,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    sender_id TEXT,
                    sender_name TEXT,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    is_self INTEGER,
                    is_group INTEGER NOT NULL DEFAULT 0,
                    media_path TEXT,
                    media_name TEXT,
                    media_md5 TEXT,
                    sender_name_source TEXT,
                    sender_name_confidence REAL,
                    raw_message TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE(adapter_name, message_id)
                );

                CREATE TABLE IF NOT EXISTS reply_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_row_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending', 'sending', 'succeeded', 'failed', 'skipped', 'dry_run'
                    )),
                    reply_text TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    sent_message_id TEXT,
                    confirmation TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(message_row_id) REFERENCES messages(id)
                );

                CREATE TABLE IF NOT EXISTS send_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    confirmation TEXT,
                    raw_response TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES reply_tasks(id)
                );

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    range_start TEXT NOT NULL,
                    range_end TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'running', 'succeeded', 'failed'
                    )),
                    seen INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0,
                    chat_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS brief_feedback (
                    event_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL CHECK(action IN (
                        'valuable', 'not_valuable', 'wrong_merge', 'missing_context'
                    )),
                    details TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS voice_transcripts (
                    message_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'succeeded', 'failed', 'corrected')),
                    transcript TEXT,
                    duration_ms INTEGER,
                    confidence REAL,
                    provider TEXT,
                    audio_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_received_at
                    ON messages(received_at);
                CREATE INDEX IF NOT EXISTS idx_reply_tasks_status
                    ON reply_tasks(status);
                CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at
                    ON sync_runs(started_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_sync_runs_status_started_at
                    ON sync_runs(status, started_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_brief_feedback_updated_at
                    ON brief_feedback(updated_at DESC);
                """
            )
            columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(messages)")
            }
            if "is_group" not in columns:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN is_group INTEGER NOT NULL DEFAULT 0"
                )
            for column in ("media_path", "media_name", "media_md5"):
                if column not in columns:
                    self._conn.execute(
                        "ALTER TABLE messages ADD COLUMN %s TEXT" % column
                    )
            if "sender_name_source" not in columns:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN sender_name_source TEXT"
                )
            if "sender_name_confidence" not in columns:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN sender_name_confidence REAL"
                )
            self._conn.commit()

    def save_brief_feedback(
        self,
        event_id: str,
        action: str,
        details: Optional[str] = None,
    ) -> Dict[str, Any]:
        event_id = str(event_id or "").strip()
        action = str(action or "").strip()
        if not event_id:
            raise ValueError("event_id 不能为空")
        if action not in {"valuable", "not_valuable", "wrong_merge", "missing_context"}:
            raise ValueError("不支持的评价动作")
        now = _now_text()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO brief_feedback(event_id, action, details, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    action=excluded.action,
                    details=excluded.details,
                    updated_at=excluded.updated_at
                """,
                (event_id, action, str(details or "").strip() or None, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT event_id, action, details, created_at, updated_at FROM brief_feedback WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return dict(row)

    def brief_feedback(self, event_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if event_ids:
                clean = [str(value) for value in event_ids if str(value).strip()]
                if not clean:
                    return []
                placeholders = ",".join("?" for _ in clean)
                rows = self._conn.execute(
                    "SELECT event_id, action, details, created_at, updated_at FROM brief_feedback "
                    "WHERE event_id IN (%s) ORDER BY updated_at DESC" % placeholders,
                    clean,
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT event_id, action, details, created_at, updated_at FROM brief_feedback "
                    "ORDER BY updated_at DESC LIMIT 200"
                ).fetchall()
        return [dict(row) for row in rows]

    def save_voice_transcript(
        self,
        message_id: str,
        *,
        status: str,
        transcript: Optional[str] = None,
        duration_ms: Optional[int] = None,
        confidence: Optional[float] = None,
        provider: Optional[str] = None,
        audio_path: Optional[str] = None,
        error: Optional[str] = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        message_id = str(message_id or "").strip()
        if not message_id:
            raise ValueError("message_id 不能为空")
        normalized_status = "corrected" if manual else str(status or "").strip()
        if normalized_status not in {"pending", "succeeded", "failed", "corrected"}:
            raise ValueError("无效的语音转写状态")
        now = _now_text()
        with self._lock:
            existing = self._conn.execute(
                "SELECT status, created_at FROM voice_transcripts WHERE message_id=?", (message_id,)
            ).fetchone()
            if existing is not None and existing["status"] == "corrected" and not manual:
                row = self._conn.execute(
                    "SELECT * FROM voice_transcripts WHERE message_id=?", (message_id,)
                ).fetchone()
                return dict(row)
            created_at = existing["created_at"] if existing is not None else now
            self._conn.execute(
                """
                INSERT INTO voice_transcripts(
                    message_id, status, transcript, duration_ms, confidence,
                    provider, audio_path, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    status=excluded.status, transcript=excluded.transcript,
                    duration_ms=excluded.duration_ms, confidence=excluded.confidence,
                    provider=excluded.provider, audio_path=excluded.audio_path,
                    error=excluded.error, updated_at=excluded.updated_at
                """,
                (
                    message_id, normalized_status, str(transcript or "").strip() or None,
                    duration_ms, confidence, provider, audio_path,
                    str(error or "").strip()[:500] or None, created_at, now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM voice_transcripts WHERE message_id=?", (message_id,)
            ).fetchone()
        return dict(row)

    def voice_transcript(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM voice_transcripts WHERE message_id=?", (str(message_id),)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _sync_run_from_row(row: sqlite3.Row) -> SyncRun:
        return SyncRun(
            row_id=int(row["id"]),
            job_id=str(row["job_id"]),
            range_start=str(row["range_start"]),
            range_end=str(row["range_end"]),
            scope=str(row["scope"]),
            status=str(row["status"]),
            started_at=str(row["started_at"]),
            seen=int(row["seen"]),
            inserted=int(row["inserted"]),
            chat_count=int(row["chat_count"]),
            error=row["error"],
            finished_at=row["finished_at"],
        )

    def create_sync_run(
        self,
        job_id: str,
        range_start: Union[str, datetime],
        range_end: Union[str, datetime],
        scope: str,
        started_at: Optional[Union[str, datetime]] = None,
    ) -> SyncRun:
        """Create a durable ``running`` history synchronization record."""

        job_id = str(job_id).strip()
        scope = str(scope).strip()
        if not job_id:
            raise ValueError("job_id 不能为空")
        if not scope:
            raise ValueError("scope 不能为空")
        started_text = _sync_time_text(started_at)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sync_runs (
                    job_id, range_start, range_end, scope, status,
                    seen, inserted, chat_count, error, started_at, finished_at
                ) VALUES (?, ?, ?, ?, 'running', 0, 0, 0, NULL, ?, NULL)
                """,
                (
                    job_id,
                    _sync_time_text(range_start),
                    _sync_time_text(range_end),
                    scope,
                    started_text,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sync_runs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("sync_runs 记录创建后无法读取")
            return self._sync_run_from_row(row)

    def update_sync_run(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        seen: Optional[int] = None,
        inserted: Optional[int] = None,
        chat_count: Optional[int] = None,
        error: Any = _UNSET,
        finished_at: Any = _UNSET,
    ) -> Optional[SyncRun]:
        """Update a durable sync record and return its current value.

        Omitted fields retain their previous values.  Passing ``error=None``
        explicitly clears an earlier error, which is used on success.
        """

        if status is not None and status not in _SYNC_RUN_STATUSES:
            raise ValueError("无效的同步状态: %s" % status)
        values: List[Any] = []
        assignments: List[str] = []
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        for column, value in (
            ("seen", seen),
            ("inserted", inserted),
            ("chat_count", chat_count),
        ):
            if value is not None:
                numeric_value = int(value)
                if numeric_value < 0:
                    raise ValueError("%s 不能为负数" % column)
                assignments.append("%s=?" % column)
                values.append(numeric_value)
        if error is not _UNSET:
            assignments.append("error=?")
            values.append(None if error is None else str(error))
        if finished_at is not _UNSET:
            assignments.append("finished_at=?")
            values.append(None if finished_at is None else _sync_time_text(finished_at))

        job_id = str(job_id)
        with self._lock:
            if assignments:
                values.append(job_id)
                self._conn.execute(
                    "UPDATE sync_runs SET %s WHERE job_id=?"
                    % ", ".join(assignments),
                    tuple(values),
                )
                self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sync_runs WHERE job_id=?", (job_id,)
            ).fetchone()
            return self._sync_run_from_row(row) if row is not None else None

    def get_sync_run(self, job_id: str) -> Optional[SyncRun]:
        """Return one durable sync record by job id."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sync_runs WHERE job_id=?", (str(job_id),)
            ).fetchone()
            return self._sync_run_from_row(row) if row is not None else None

    def recent_sync_runs(self, limit: int = 20) -> List[SyncRun]:
        """Return the newest persisted sync records first."""

        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM sync_runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
            return [self._sync_run_from_row(row) for row in rows]

    def recover_in_flight_tasks(self) -> int:
        """Return tasks left in ``sending`` to ``pending`` after a restart."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reply_tasks SET status='pending', updated_at=? WHERE status='sending'",
                (_now_text(),),
            )
            self._conn.commit()
            return cur.rowcount

    def pending_task_ids(self) -> List[int]:
        """Return pending reply IDs so a restarted service can resume them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM reply_tasks WHERE status='pending' ORDER BY id"
            ).fetchall()
            return [int(row[0]) for row in rows]

    def ingest(
        self,
        message: IncomingMessage,
        decision: ReplyDecision,
        create_task: bool = True,
    ) -> IngestResult:
        raw = json.dumps(message.raw_message, ensure_ascii=False, default=str)
        is_self = None if message.is_self is None else int(message.is_self)
        task_status = "pending" if decision.should_reply else "skipped"
        reply_text = decision.reply_text if decision.should_reply else None
        last_error = None if decision.should_reply else decision.reason
        now = _now_text()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages (
                        message_id, adapter_name, adapter_version, chat_id, chat_name,
                        sender_id, sender_name, message_type, content, timestamp,
                        is_self, is_group, media_path, media_name, media_md5,
                        sender_name_source, sender_name_confidence,
                        raw_message, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.message_id,
                        message.adapter_name,
                        message.adapter_version,
                        message.chat_id,
                        message.chat_name,
                        message.sender_id,
                        message.sender_name,
                        message.message_type,
                        message.content,
                        message.timestamp.isoformat(),
                        is_self,
                        int(message.is_group),
                        message.media_path,
                        message.media_name,
                        message.media_md5,
                        message.sender_name_source,
                        message.sender_name_confidence,
                        raw,
                        now,
                    ),
                )
                if cur.rowcount == 0:
                    # A historical re-scan can discover better identity or
                    # media metadata for an already stored row.  Keep the
                    # dedupe guarantee, but allow that enrichment to land.
                    existing = self._conn.execute(
                        """
                        SELECT sender_name, sender_name_confidence
                        FROM messages
                        WHERE adapter_name=? AND message_id=?
                        """,
                        (message.adapter_name, message.message_id),
                    ).fetchone()
                    identity_update = False
                    if (
                        existing is not None
                        and message.sender_name
                        and message.sender_name_confidence is not None
                    ):
                        old_name = str(existing["sender_name"] or "")
                        old_confidence = existing["sender_name_confidence"]
                        try:
                            new_confidence = float(message.sender_name_confidence)
                            old_confidence_value = (
                                None
                                if old_confidence is None
                                else float(old_confidence)
                            )
                        except (TypeError, ValueError):
                            new_confidence = None
                            old_confidence_value = None
                        identity_update = bool(
                            new_confidence is not None
                            and (
                                old_confidence_value is None
                                or new_confidence > old_confidence_value
                                or (
                                    new_confidence == old_confidence_value
                                    and str(message.sender_name) != old_name
                                )
                            )
                        )
                    self._conn.execute(
                        """
                        UPDATE messages
                        SET chat_name=CASE WHEN ? <> '' THEN ? ELSE chat_name END,
                            sender_name=CASE WHEN ? THEN COALESCE(NULLIF(?, ''), sender_name) ELSE sender_name END,
                            content=CASE WHEN ? <> '' THEN ? ELSE content END,
                            media_path=COALESCE(NULLIF(?, ''), media_path),
                            media_name=COALESCE(NULLIF(?, ''), media_name),
                            media_md5=COALESCE(NULLIF(?, ''), media_md5),
                            sender_name_source=CASE WHEN ? THEN COALESCE(NULLIF(?, ''), sender_name_source) ELSE sender_name_source END,
                            sender_name_confidence=CASE WHEN ? THEN ? ELSE sender_name_confidence END,
                            is_group=CASE WHEN ? THEN 1 ELSE is_group END
                        WHERE adapter_name=? AND message_id=?
                        """,
                        (
                            message.chat_name or "",
                            message.chat_name or "",
                            int(identity_update),
                            message.sender_name or "",
                            message.content or "",
                            message.content or "",
                            message.media_path or "",
                            message.media_name or "",
                            message.media_md5 or "",
                            int(identity_update),
                            message.sender_name_source or "",
                            int(identity_update),
                            message.sender_name_confidence,
                            int(message.is_group),
                            message.adapter_name,
                            message.message_id,
                        ),
                    )
                    self._conn.commit()
                    return IngestResult(False, None, None, "duplicate_message")

                message_row_id = int(cur.lastrowid)
                if not create_task:
                    self._conn.commit()
                    return IngestResult(True, message_row_id, None, decision.reason)
                task_cur = self._conn.execute(
                    """
                    INSERT INTO reply_tasks (
                        message_row_id, status, reply_text, last_error,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (message_row_id, task_status, reply_text, last_error, now, now),
                )
                task_id = int(task_cur.lastrowid)
                self._conn.commit()
                return IngestResult(True, message_row_id, task_id, decision.reason)
            except Exception:
                self._conn.rollback()
                raise

    def claim_pending_task(self, task_id: int) -> Optional[ReplyTask]:
        with self._lock:
            now = _now_text()
            cur = self._conn.execute(
                """
                UPDATE reply_tasks
                SET status='sending', attempts=attempts + 1, updated_at=?
                WHERE id=? AND status='pending'
                """,
                (now, task_id),
            )
            if cur.rowcount == 0:
                self._conn.commit()
                return None
            row = self._conn.execute(
                """
                SELECT t.id AS task_id, t.message_row_id, m.message_id,
                       m.chat_id, m.chat_name, t.reply_text, t.status,
                       t.attempts, t.last_error
                FROM reply_tasks t
                JOIN messages m ON m.id=t.message_row_id
                WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
            self._conn.commit()
            if row is None or row["reply_text"] is None:
                return None
            return ReplyTask(
                task_id=int(row["task_id"]),
                message_row_id=int(row["message_row_id"]),
                message_id=str(row["message_id"]),
                chat_id=str(row["chat_id"]),
                chat_name=str(row["chat_name"]),
                reply_text=str(row["reply_text"]),
                status=str(row["status"]),
                attempts=int(row["attempts"]),
                last_error=row["last_error"],
            )

    def mark_skipped(self, task_id: int, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reply_tasks SET status='skipped', last_error=?, updated_at=? WHERE id=?",
                (reason, _now_text(), task_id),
            )
            self._conn.commit()

    def mark_dry_run(self, task_id: int, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reply_tasks SET status='dry_run', confirmation=?, updated_at=? WHERE id=?",
                (detail, _now_text(), task_id),
            )
            self._conn.commit()

    def mark_succeeded(self, task_id: int, result: SendResult) -> None:
        with self._lock:
            now = _now_text()
            self._conn.execute(
                """
                UPDATE reply_tasks
                SET status='succeeded', last_error=NULL, sent_message_id=?,
                    confirmation=?, updated_at=?
                WHERE id=?
                """,
                (result.sent_message_id, result.confirmation, now, task_id),
            )
            attempts = self._conn.execute(
                "SELECT attempts FROM reply_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            self._conn.execute(
                """
                INSERT INTO send_attempts (
                    task_id, attempt_number, status, error, confirmation,
                    raw_response, created_at
                ) VALUES (?, ?, 'succeeded', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempts,
                    result.error,
                    result.confirmation,
                    result.raw_response,
                    now,
                ),
            )
            self._conn.commit()

    def mark_failed(
        self,
        task_id: int,
        error: str,
        max_attempts: int,
        result: Optional[SendResult] = None,
    ) -> bool:
        """Record failure and return whether the task remains retryable."""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM reply_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            attempts = int(row[0])
            retryable = attempts < max_attempts
            status = "pending" if retryable else "failed"
            now = _now_text()
            confirmation = result.confirmation if result else None
            raw_response = result.raw_response if result else None
            self._conn.execute(
                """
                UPDATE reply_tasks
                SET status=?, last_error=?, confirmation=?, updated_at=?
                WHERE id=?
                """,
                (status, error, confirmation, now, task_id),
            )
            self._conn.execute(
                """
                INSERT INTO send_attempts (
                    task_id, attempt_number, status, error, confirmation,
                    raw_response, created_at
                ) VALUES (?, ?, 'failed', ?, ?, ?, ?)
                """,
                (task_id, attempts, error, confirmation, raw_response, now),
            )
            self._conn.commit()
            return retryable

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT t.*, m.message_id, m.chat_id, m.chat_name, m.content
                FROM reply_tasks t JOIN messages m ON m.id=t.message_row_id
                WHERE t.id=?
                """,
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

    def retry_task(self, task_id: int) -> bool:
        """Move an exhausted task back to pending for an explicit retry."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE reply_tasks
                SET status='pending', last_error=NULL, updated_at=?
                WHERE id=? AND status='failed'
                """,
                (_now_text(), int(task_id)),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def recent_messages(
        self,
        chat_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent normalized messages in chronological order."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            if chat_id:
                rows = self._conn.execute(
                    """
                    SELECT id, message_id, adapter_name, adapter_version,
                           chat_id, chat_name, sender_id, sender_name,
                           message_type, content, timestamp, is_self, is_group,
                           media_path, media_name, media_md5,
                           sender_name_source, sender_name_confidence,
                           raw_message, received_at
                    FROM messages
                    WHERE chat_id=? OR chat_name=?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (chat_id, chat_id, safe_limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT id, message_id, adapter_name, adapter_version,
                           chat_id, chat_name, sender_id, sender_name,
                           message_type, content, timestamp, is_self, is_group,
                           media_path, media_name, media_md5,
                           sender_name_source, sender_name_confidence,
                           raw_message, received_at
                    FROM messages
                    ORDER BY id DESC LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            values = [dict(row) for row in reversed(rows)]
            for value in values:
                value["is_self"] = (
                    None if value["is_self"] is None else bool(value["is_self"])
                )
                value["is_group"] = bool(value.get("is_group"))
                try:
                    value["raw_message"] = json.loads(value["raw_message"])
                except (TypeError, ValueError):
                    pass
            return values

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Return one archived message by its dedupe identifier."""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, message_id, adapter_name, adapter_version,
                       chat_id, chat_name, sender_id, sender_name,
                       message_type, content, timestamp, is_self, is_group,
                       media_path, media_name, media_md5,
                       sender_name_source, sender_name_confidence,
                       raw_message, received_at
                FROM messages
                WHERE message_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (str(message_id),),
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["is_self"] = (
                None if value["is_self"] is None else bool(value["is_self"])
            )
            value["is_group"] = bool(value.get("is_group"))
            try:
                value["raw_message"] = json.loads(value["raw_message"])
            except (TypeError, ValueError):
                pass
            return value

    def messages_between(
        self,
        start_at: datetime,
        end_at: datetime,
        chat_id: Optional[str] = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Return messages in the half-open interval ``[start_at, end_at)``."""

        safe_limit = max(1, min(int(limit), 200_000))
        start_text = start_at.astimezone(timezone.utc).isoformat()
        end_text = end_at.astimezone(timezone.utc).isoformat()
        with self._lock:
            params: List[Any] = [start_text, end_text]
            chat_sql = ""
            if chat_id:
                chat_sql = " AND (chat_id=? OR chat_name=?)"
                params.extend([chat_id, chat_id])
            params.append(safe_limit)
            rows = self._conn.execute(
                """
                SELECT id, message_id, adapter_name, adapter_version,
                       chat_id, chat_name, sender_id, sender_name,
                       message_type, content, timestamp, is_self, is_group,
                       media_path, media_name, media_md5,
                       sender_name_source, sender_name_confidence,
                       raw_message, received_at
                FROM messages
                WHERE timestamp >= ? AND timestamp < ?
                """
                + chat_sql
                + " ORDER BY timestamp ASC, id ASC LIMIT ?",
                tuple(params),
            ).fetchall()
            values = [dict(row) for row in rows]
            for value in values:
                value["is_self"] = (
                    None if value["is_self"] is None else bool(value["is_self"])
                )
                value["is_group"] = bool(value.get("is_group"))
                try:
                    value["raw_message"] = json.loads(value["raw_message"])
                except (TypeError, ValueError):
                    pass
            return values

    def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return reply tasks with their source message fields."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            params = []
            where = ""
            if status:
                where = "WHERE t.status=?"
                params.append(status)
            params.append(safe_limit)
            rows = self._conn.execute(
                f"""
                SELECT t.id AS task_id, t.message_row_id, t.status,
                       t.reply_text, t.attempts, t.last_error,
                       t.sent_message_id, t.confirmation,
                       t.created_at, t.updated_at,
                       m.message_id, m.chat_id, m.chat_name,
                       m.sender_id, m.sender_name, m.message_type,
                       m.content, m.timestamp, m.media_path, m.media_name,
                       m.media_md5, m.sender_name_source,
                       m.sender_name_confidence
                FROM reply_tasks t
                JOIN messages m ON m.id=t.message_row_id
                {where}
                ORDER BY t.id DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def counts(self) -> Dict[str, int]:
        """Return compact counts for a dashboard status card."""
        with self._lock:
            messages = int(
                self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            )
            tasks = int(
                self._conn.execute("SELECT COUNT(*) FROM reply_tasks").fetchone()[0]
            )
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM reply_tasks GROUP BY status"
            ).fetchall()
            result = {"messages": messages, "tasks": tasks}
            result.update({"tasks_%s" % row[0]: int(row[1]) for row in rows})
            return result

    def count_messages(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def count_tasks(self, status: Optional[str] = None) -> int:
        with self._lock:
            if status is None:
                return int(self._conn.execute("SELECT COUNT(*) FROM reply_tasks").fetchone()[0])
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM reply_tasks WHERE status=?", (status,)
                ).fetchone()[0]
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
