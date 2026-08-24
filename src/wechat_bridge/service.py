"""Receive, persist, decide and send with explicit task state transitions."""

import logging
import queue
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence
from uuid import uuid4

from .adapters.base import AdapterError, WeChatAdapter
from .engine import ReplyPolicy
from .models import IncomingMessage, ReplyDecision, SendResult
from .store import SQLiteStore

logger = logging.getLogger("wechat_bridge")


class BridgeService:
    def __init__(
        self,
        adapter: WeChatAdapter,
        store: SQLiteStore,
        policy: ReplyPolicy,
        chat_names: Sequence[str],
        dry_run: bool = False,
        filehelper_only: bool = True,
        send_enabled: bool = True,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.policy = policy
        self.chat_names = tuple(chat_names)
        self.dry_run = dry_run
        self.filehelper_only = bool(filehelper_only)
        # Library callers may preserve the old explicit-send behavior, while
        # the CLI now passes False by default for the user's receive-only mode.
        self.send_enabled = bool(send_enabled)
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._started = False
        self._health_lock = threading.RLock()
        self._last_health = None
        self._last_health_at = 0.0
        self._last_sync_at = None
        self._sync_lock = threading.RLock()
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_status: Dict[str, Any] = {
            "state": "idle",
            "job_id": None,
            "range": None,
            "scope": "all",
            "seen": 0,
            "inserted": 0,
            "chat_count": 0,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def start(self) -> None:
        if self._started:
            return
        self.store.recover_in_flight_tasks()
        self.adapter.connect()
        health = self.adapter.health_check()
        with self._health_lock:
            self._last_health = health
            self._last_health_at = time.monotonic()
        if not health.ok:
            self.adapter.disconnect()
            raise AdapterError("适配器健康检查失败: %s" % health.message)
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="wechat-bridge-sender",
            daemon=True,
        )
        self._worker.start()
        try:
            self.adapter.start_receive(self.chat_names, self._on_message)
        except Exception:
            self._stop.set()
            if self._worker:
                self._worker.join(timeout=2.0)
            self.adapter.disconnect()
            raise
        for task_id in self.store.pending_task_ids():
            self._queue.put(task_id)
        self._started = True
        logger.info(
            "bridge started adapter=%s version=%s chats=%s dry_run=%s",
            self.adapter.name,
            self.adapter.version,
            ",".join(self.chat_names),
            self.dry_run,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        try:
            self.adapter.stop_receive()
        finally:
            if self._worker:
                self._worker.join(timeout=3.0)
            self.adapter.disconnect()
            self._started = False
            logger.info("bridge stopped")

    def pause(self) -> None:
        self._paused.set()
        logger.warning("automatic replies paused")

    def resume(self) -> None:
        self._paused.clear()
        logger.info("automatic replies resumed")

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def status_snapshot(self):
        """Return a small, JSON-friendly runtime status for local clients."""
        try:
            with self._health_lock:
                cached_health = self._last_health
            if cached_health is None:
                health = self.adapter.health_check()
                with self._health_lock:
                    self._last_health = health
                    self._last_health_at = time.monotonic()
            else:
                health = cached_health
            health_value = {
                "ok": health.ok,
                "adapter_name": health.adapter_name,
                "adapter_version": health.adapter_version,
                "message": health.message,
                "details": health.details,
            }
        except Exception as exc:
            health_value = {
                "ok": False,
                "adapter_name": self.adapter.name,
                "adapter_version": self.adapter.version,
                "message": str(exc),
                "details": {},
            }
        generator = self.policy.reply_generator
        ai_value = {
            "enabled": generator is not None,
            "preview_available": generator is not None,
            "provider": "openai" if generator is not None else None,
            "model": getattr(generator, "model", None),
            "configured": bool(getattr(generator, "api_key", None))
            if generator is not None
            else False,
        }
        return {
            "started": self.is_started,
            "receiving": self.is_started,
            "paused": self.is_paused,
            "dry_run": self.dry_run,
            "send_enabled": self.send_enabled,
            "receive_only": not self.send_enabled,
            "adapter": health_value,
            "chats": list(self.chat_names),
            "live_scope": "、".join(self.chat_names) or "—",
            "history_scope": "全部可读会话",
            "allowed_chats": list(self.policy.allowed_chats),
            "timezone": self.policy.timezone_name,
            "ai_enabled": self.policy.reply_generator is not None,
            "ai": ai_value,
            "filehelper_only": self.filehelper_only,
            "last_sync_at": self._last_sync_at,
            "sync": self.history_sync_status(),
            "rules": [
                {"name": rule.name, "enabled": rule.enabled}
                for rule in self.policy.rules
            ],
        }

    def sync_recent_history(self, limit: int = 100):
        """Import recent history without creating reply tasks or sending."""
        if not self._started:
            raise AdapterError("桥接服务尚未启动，无法同步历史消息")
        safe_limit = max(1, min(int(limit), 500))
        chat_name = self.chat_names[0] if self.chat_names else "文件传输助手"
        chat_id = "filehelper" if chat_name in ("文件传输助手", "filehelper") else chat_name
        if self.filehelper_only:
            chat_id = "filehelper"
            chat_name = "文件传输助手"
        raw_items = self.adapter.get_chat_history(chat_id, chat_name, safe_limit)
        inserted = self._ingest_history(raw_items, chat_id, chat_name)
        self._last_sync_at = datetime.now(timezone.utc)
        return {"seen": len(raw_items), "inserted": inserted, "chat_name": chat_name}

    def start_history_sync(
        self,
        start_at: datetime,
        end_at: datetime,
        scope: str = "all",
        limit: int = 50_000,
    ) -> Dict[str, Any]:
        """Start a date-range import in the background.

        History imports are intentionally independent of reply tasks.  The
        endpoint can therefore import a whole day/week without holding an HTTP
        request open while encrypted message shards are being read.
        """

        if not self._started:
            raise AdapterError("桥接服务尚未启动，无法同步历史消息")
        scope = str(scope or "all").strip().lower()
        if scope not in {"all", "filehelper"}:
            raise ValueError("scope 只能是 all 或 filehelper")
        safe_limit = max(1, min(int(limit), 200_000))
        with self._sync_lock:
            if self._sync_status.get("state") == "running":
                return dict(self._sync_status)
            job_id = uuid4().hex[:12]
            value = {
                "state": "running",
                "job_id": job_id,
                "range": {
                    "start": start_at.astimezone(timezone.utc).isoformat(),
                    "end": end_at.astimezone(timezone.utc).isoformat(),
                },
                "scope": scope,
                "limit": safe_limit,
                "seen": 0,
                "inserted": 0,
                "chat_count": 0,
                "error": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
            }
            self.store.create_sync_run(
                job_id=job_id,
                range_start=value["range"]["start"],
                range_end=value["range"]["end"],
                scope=scope,
                started_at=value["started_at"],
            )
            self._sync_status = value
            self._sync_thread = threading.Thread(
                target=self._run_history_sync,
                args=(job_id, start_at, end_at, scope, safe_limit),
                name="wechat-bridge-history-sync",
                daemon=True,
            )
            self._sync_thread.start()
            return dict(value)

    def history_sync_status(self) -> Dict[str, Any]:
        with self._sync_lock:
            return dict(self._sync_status)

    def _run_history_sync(
        self,
        job_id: str,
        start_at: datetime,
        end_at: datetime,
        scope: str,
        limit: int,
    ) -> None:
        raw_items = []
        inserted = 0
        try:
            chat_ids = ("filehelper",) if scope == "filehelper" else None
            raw_items = self.adapter.get_history_range(
                start_at,
                end_at,
                chat_ids=chat_ids,
                limit=limit,
            )
            inserted = self._ingest_history(raw_items, None, None)
            chat_count = len(
                {
                    self._history_chat_key(item)
                    for item in raw_items
                    if self._history_chat_key(item)
                }
            )
            now = datetime.now(timezone.utc)
            self.store.update_sync_run(
                job_id,
                status="succeeded",
                seen=len(raw_items),
                inserted=inserted,
                chat_count=chat_count,
                error=None,
                finished_at=now,
            )
            with self._sync_lock:
                if self._sync_status.get("job_id") == job_id:
                    self._sync_status.update(
                        {
                            "state": "succeeded",
                            "seen": len(raw_items),
                            "inserted": inserted,
                            "chat_count": chat_count,
                            "finished_at": now.isoformat(),
                        }
                    )
            self._last_sync_at = now
        except Exception as exc:
            logger.exception("历史范围同步失败 job=%s", job_id)
            finished_at = datetime.now(timezone.utc)
            try:
                self.store.update_sync_run(
                    job_id,
                    status="failed",
                    seen=len(raw_items),
                    inserted=inserted,
                    chat_count=len(
                        {
                            self._history_chat_key(item)
                            for item in raw_items
                            if self._history_chat_key(item)
                        }
                    ),
                    error=str(exc),
                    finished_at=finished_at,
                )
            except Exception:
                logger.exception("历史范围同步结果持久化失败 job=%s", job_id)
            with self._sync_lock:
                if self._sync_status.get("job_id") == job_id:
                    self._sync_status.update(
                        {
                            "state": "failed",
                            "error": str(exc),
                            "finished_at": finished_at.isoformat(),
                        }
                    )

    @staticmethod
    def _history_chat_key(item: Any) -> str:
        if isinstance(item, IncomingMessage):
            return str(item.chat_id or item.chat_name or "")
        if isinstance(item, dict):
            return str(item.get("chat_id") or item.get("chat_name") or "")
        return str(
            getattr(item, "chat_id", None)
            or getattr(item, "chat_name", None)
            or ""
        )

    def _ingest_history(
        self,
        raw_items: Sequence[Any],
        fallback_chat_id: Optional[str],
        fallback_chat_name: Optional[str],
    ) -> int:
        inserted = 0
        for raw in raw_items:
            value = dict(raw) if isinstance(raw, dict) else raw
            message = self._history_message(
                value,
                fallback_chat_id or str(getattr(value, "chat_id", "") or ""),
                fallback_chat_name or str(getattr(value, "chat_name", "") or ""),
            )
            result = self.store.ingest(
                message,
                ReplyDecision(False, "history_sync"),
                create_task=False,
            )
            if result.inserted:
                inserted += 1
        return inserted

    def _history_message(self, raw, chat_id: str, chat_name: str) -> IncomingMessage:
        if isinstance(raw, IncomingMessage):
            return raw
        value = dict(raw)
        timestamp = value.get("timestamp")
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp = None
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return IncomingMessage(
            message_id=str(value.get("message_id") or value.get("id") or "history:%s" % time.time_ns()),
            chat_id=str(value.get("chat_id") or chat_id),
            chat_name=str(value.get("chat_name") or chat_name),
            sender_id=value.get("sender_id"),
            sender_name=value.get("sender_name"),
            message_type=str(value.get("message_type") or "other"),
            content=str(value.get("content") or ""),
            timestamp=timestamp,
            is_self=value.get("is_self"),
            raw_message=value.get("raw_message") or {"history_sync": True},
            adapter_name=str(value.get("adapter_name") or self.adapter.name),
            adapter_version=value.get("adapter_version") or self.adapter.version,
            is_group=bool(value.get("is_group")),
            media_path=value.get("media_path"),
            media_name=value.get("media_name"),
            media_md5=value.get("media_md5"),
            sender_name_source=value.get("sender_name_source"),
            sender_name_confidence=value.get("sender_name_confidence"),
        )

    def retry_task(self, task_id: int) -> bool:
        task = self.store.get_task(int(task_id))
        if task is None or task.get("status") != "failed":
            return False
        if self.filehelper_only and task.get("chat_id") != "filehelper":
            return False
        if not self.store.retry_task(int(task_id)):
            return False
        if self._started and not self._paused.is_set():
            self._queue.put(int(task_id))
        return True

    def _on_message(self, message: IncomingMessage) -> None:
        self._last_sync_at = datetime.now(timezone.utc)
        if not self.send_enabled:
            # Receive-only mode still stores every live message selected by
            # the adapter, but does not generate reply tasks or call AI.
            decision = ReplyDecision(False, "send_disabled_by_operator")
        elif self.filehelper_only and message.chat_id != "filehelper":
            decision = ReplyDecision(False, "filehelper_only_test_scope")
        else:
            decision = self.policy.decide(message, time.monotonic())
        if decision.should_reply and decision.reply_text is None:
            context = self.store.recent_messages(
                chat_id=message.chat_id,
                limit=self.policy.context_limit,
            )
            try:
                reply_text = self.policy.generate_reply(message, context)
            except Exception as exc:
                logger.exception("AI reply generation failed message_id=%s", message.message_id)
                decision = ReplyDecision(False, "ai_generation_failed:%s" % exc)
            else:
                decision = ReplyDecision(True, decision.reason, reply_text)
        result = self.store.ingest(message, decision)
        logger.info(
            "message received id=%s chat=%s type=%s inserted=%s decision=%s",
            message.message_id,
            message.chat_name,
            message.message_type,
            result.inserted,
            decision.reason,
        )
        if result.inserted and result.task_id and decision.should_reply:
            self._queue.put(result.task_id)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process_task(int(task_id))
            except Exception:
                logger.exception("unexpected reply task failure task_id=%s", task_id)
                retryable = self.store.mark_failed(
                    int(task_id),
                    "unexpected_worker_exception",
                    max_attempts=self.policy.max_retries + 1,
                )
                if retryable and not self._stop.is_set():
                    self._queue.put(task_id)
            finally:
                self._queue.task_done()

    def _process_task(self, task_id: int) -> None:
        task = self.store.claim_pending_task(task_id)
        if task is None:
            return
        if self._paused.is_set():
            self.store.mark_skipped(task_id, "paused_before_send")
            return
        if self.filehelper_only and task.chat_id != "filehelper":
            self.store.mark_skipped(task_id, "filehelper_only_test_scope")
            logger.warning("task=%s blocked outside filehelper test scope", task_id)
            return
        if not self.send_enabled:
            self.store.mark_dry_run(task_id, "send_disabled_by_operator")
            logger.info("send-disabled task=%s chat=%s", task_id, task.chat_name)
            return
        if self.dry_run:
            self.store.mark_dry_run(task_id, "dry_run_no_message_sent")
            logger.info("dry-run task=%s chat=%s text=%r", task_id, task.chat_name, task.reply_text)
            return

        try:
            result = self.adapter.send_text(task.chat_id, task.chat_name, task.reply_text)
        except Exception as exc:
            result = SendResult(False, None, "adapter_exception", error=str(exc))
        if result.accepted:
            self.store.mark_succeeded(task_id, result)
            logger.info(
                "reply sent task=%s chat=%s confirmation=%s",
                task_id,
                task.chat_name,
                result.confirmation,
            )
            return

        error = result.error or "adapter rejected send"
        retryable = self.store.mark_failed(
            task_id,
            error,
            max_attempts=self.policy.max_retries + 1,
            result=result,
        )
        logger.error("reply failed task=%s retryable=%s error=%s", task_id, retryable, error)
        if retryable and not self._stop.is_set():
            time.sleep(min(2 ** max(0, task.attempts - 1), 5))
            if not self._stop.is_set():
                self._queue.put(task_id)
