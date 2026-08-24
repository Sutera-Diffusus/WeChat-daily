"""Current WeChat 4.x adapter backed by local DB reads and GUI sends.

``wechatauto-replica`` knows the current Windows WeChat 4.x storage format:
messages are read from the encrypted ``xwechat_files`` databases, while text
sends use the desktop client.  The package's optional UIA hot-activation path
is disabled by default; the default send path is coordinate/OCR.  Callers may
explicitly opt into the known one-byte accessibility switch when needed.

The dependency is imported lazily so Hook and wxauto4 users can still run the
project without importing this provider.
"""

import hashlib
import importlib.metadata
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..models import HealthStatus, IncomingMessage, SendResult, utc_now
from .base import AdapterError, MessageCallback, WeChatAdapter

logger = logging.getLogger("wechat_bridge.wechatauto_db")


def _message_table(user: str) -> str:
    return "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest()


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """Read a sqlite.Row value without requiring every schema column."""

    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return default


def _chat_rows(
    db: Any,
    user: str,
    since_seq: Optional[int] = None,
    limit_per_shard: int = 500,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read one chat from every encrypted message shard.

    ``wechatauto-replica`` exposes a convenient single-shard query, but a
    current WeChat account can keep the same ``Msg_<md5>`` table in multiple
    ``message_*.db`` files.  The bridge needs the union so a newly written
    message is not hidden behind an older shard.
    """

    table = _message_table(user)
    rows: List[Dict[str, Any]] = []
    descending = since_seq is None and start_ts is None and end_ts is None
    for shard in db._message_dbs():
        conn = db._open(shard)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            columns = {
                str(item[1])
                for item in conn.execute('PRAGMA table_info("%s")' % table)
            }
            has_status = "status" in columns
            status_sql = ", status" if has_status else ""
            server_sql = ", server_id" if "server_id" in columns else ""
            filters = []
            query_args = []
            if since_seq is not None:
                filters.append("sort_seq > ?")
                query_args.append(int(since_seq))
            if start_ts is not None:
                filters.append("create_time >= ?")
                query_args.append(int(start_ts))
            if end_ts is not None:
                filters.append("create_time < ?")
                query_args.append(int(end_ts))
            where_sql = (" WHERE " + " AND ".join(filters)) if filters else ""
            # Range imports need chronological order; live polling needs the
            # newest rows first only when it is discovering a chat snapshot.
            order_sql = "DESC" if descending else "ASC"
            sql = (
                'SELECT local_id, local_type%s, real_sender_id, create_time, '
                'message_content, source, packed_info_data, sort_seq%s '
                'FROM "%s"%s ORDER BY sort_seq %s LIMIT ?'
                % (server_sql, status_sql, table, where_sql, order_sql)
            )
            query_args.append(max(1, int(limit_per_shard)))
            try:
                db_rows = conn.execute(sql, tuple(query_args)).fetchall()
            except sqlite3.DatabaseError:
                # A future schema may omit one of the optional metadata
                # columns.  The core message fields are enough to receive.
                sql = (
                    'SELECT local_id, local_type%s, real_sender_id, create_time, '
                    'message_content, source, packed_info_data, sort_seq '
                    'FROM "%s"%s ORDER BY sort_seq %s LIMIT ?'
                    % (server_sql, table, where_sql, order_sql)
                )
                db_rows = conn.execute(sql, tuple(query_args)).fetchall()
            for row in db_rows:
                try:
                    message = dict(db._msg_row_to_dict(row))
                except Exception:
                    content = _row_value(row, "message_content", "")
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="replace")
                    message = {
                        "local_id": _row_value(row, "local_id"),
                        "type": _row_value(row, "local_type"),
                        "sender_id": _row_value(row, "real_sender_id"),
                        "create_time": _row_value(row, "create_time"),
                        "content": str(content or ""),
                        "sort_seq": _row_value(row, "sort_seq", 0),
                    }
                message["_bridge_shard"] = shard
                message["_bridge_status"] = _row_value(row, "status")
                message["_bridge_server_id"] = _row_value(row, "server_id")
                message["_bridge_packed_info"] = _row_value(row, "packed_info_data")
                message["_bridge_source"] = _row_value(row, "source")
                rows.append(message)
        finally:
            conn.close()
    rows.sort(
        key=lambda item: (
            int(item.get("sort_seq") or 0),
            int(item.get("local_id") or 0),
            str(item.get("_bridge_shard") or ""),
        )
    )
    if descending:
        rows.reverse()
    return rows


def _latest_sort_seq(db: Any, user: str) -> int:
    """Return the newest sequence across all shards for one chat."""

    table = _message_table(user)
    latest = 0
    for shard in db._message_dbs():
        conn = db._open(shard)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            value = conn.execute(
                'SELECT MAX(sort_seq) FROM "%s"' % table
            ).fetchone()[0]
            if value is not None:
                latest = max(latest, int(value))
        finally:
            conn.close()
    return latest


def _all_chat_rows_in_range(
    db: Any,
    start_ts: int,
    end_ts: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Read a date range with one pass over each message shard.

    Calling ``_chat_rows`` once per contact is correct but very expensive on a
    large account because each call reopens every encrypted shard.  The full
    archive path scans the table inventory once per shard instead.
    """

    rows: List[Dict[str, Any]] = []
    safe_limit = max(1, int(limit))
    for shard in db._message_dbs():
        conn = db._open(shard)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for table_row in tables:
                table = str(table_row[0])
                columns = {
                    str(item[1])
                    for item in conn.execute('PRAGMA table_info("%s")' % table)
                }
                if "create_time" not in columns:
                    continue
                status_sql = ", status" if "status" in columns else ""
                server_sql = ", server_id" if "server_id" in columns else ""
                sql = (
                    'SELECT local_id, local_type%s, real_sender_id, create_time, '
                    'message_content, source, packed_info_data, sort_seq%s '
                    'FROM "%s" WHERE create_time >= ? AND create_time < ? '
                    'ORDER BY create_time ASC, sort_seq ASC'
                    % (server_sql, status_sql, table)
                )
                try:
                    db_rows = conn.execute(sql, (int(start_ts), int(end_ts))).fetchall()
                except sqlite3.DatabaseError:
                    sql = (
                        'SELECT local_id, local_type%s, real_sender_id, create_time, '
                        'message_content, source, packed_info_data, sort_seq '
                        'FROM "%s" WHERE create_time >= ? AND create_time < ? '
                        'ORDER BY create_time ASC, sort_seq ASC' % (server_sql, table)
                    )
                    db_rows = conn.execute(
                        sql,
                        (int(start_ts), int(end_ts)),
                    ).fetchall()
                for row in db_rows:
                    try:
                        message = dict(db._msg_row_to_dict(row))
                    except Exception:
                        content = _row_value(row, "message_content", "")
                        if isinstance(content, bytes):
                            content = content.decode("utf-8", errors="replace")
                        message = {
                            "local_id": _row_value(row, "local_id"),
                            "type": _row_value(row, "local_type"),
                            "sender_id": _row_value(row, "real_sender_id"),
                            "create_time": _row_value(row, "create_time"),
                            "content": str(content or ""),
                            "sort_seq": _row_value(row, "sort_seq", 0),
                        }
                    message["_bridge_shard"] = shard
                    message["_bridge_status"] = _row_value(row, "status")
                    message["_bridge_md5"] = table[4:]
                    message["_bridge_server_id"] = _row_value(row, "server_id")
                    message["_bridge_packed_info"] = _row_value(row, "packed_info_data")
                    message["_bridge_source"] = _row_value(row, "source")
                    rows.append(message)
                    if len(rows) >= safe_limit:
                        break
                if len(rows) >= safe_limit:
                    break
        finally:
            conn.close()
        if len(rows) >= safe_limit:
            break
    rows.sort(
        key=lambda item: (
            int(item.get("create_time") or 0),
            int(item.get("sort_seq") or 0),
            int(item.get("local_id") or 0),
        )
    )
    return rows[:safe_limit]


class _MultiShardDbListener:
    """Small read-only listener that merges all message shards."""

    def __init__(
        self,
        db: Any,
        interval: float,
        before_poll: Optional[Callable[[], None]] = None,
        db_lock: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.interval = max(0.2, float(interval))
        self.before_poll = before_poll
        self.db_lock = db_lock or threading.RLock()
        self.callbacks: Dict[str, List[Callable[..., None]]] = {}
        self._watermark: Dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def add_listener(self, user: str, callback: Callable[..., None]) -> None:
        self.callbacks.setdefault(user, []).append(callback)
        if user not in self._watermark:
            with self.db_lock:
                self._watermark[user] = _latest_sort_seq(self.db, user)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="wechat-bridge-db-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval * 3))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            with self.db_lock:
                if self.before_poll is not None:
                    try:
                        self.before_poll()
                    except Exception:
                        # Identity refresh is enrichment. A transient contact DB
                        # read failure must not stop message reception.
                        logger.exception("接收前刷新微信身份索引失败")
                for user, callbacks in tuple(self.callbacks.items()):
                    try:
                        since = self._watermark.get(user, 0)
                        messages = _chat_rows(self.db, user, since_seq=since)
                        if not messages:
                            continue
                        self._watermark[user] = max(
                            int(message.get("sort_seq") or since)
                            for message in messages
                        )
                        for message in messages:
                            for callback in tuple(callbacks):
                                callback(message, self)
                    except Exception:
                        logger.exception("跨分片消息轮询失败 user=%s", user)
            self._stop.wait(self.interval)


class WeChatAutoDbAdapter(WeChatAdapter):
    """Read current 4.x messages from DB and send through the visible client."""

    name = "wechatauto_db_gui"

    _DISPLAY_TO_USER = {
        "文件传输助手": "filehelper",
        "filehelper": "filehelper",
    }
    _MESSAGE_TYPES = {
        "文本": "text",
        "图片": "image",
        "语音": "voice",
        "视频": "video",
        "动画表情": "emoji",
        "位置": "location",
        "文件/链接/卡片": "link_or_file",
        "系统消息": "system",
    }

    def __init__(
        self,
        db_dir: Optional[str] = None,
        account: Optional[str] = None,
        poll_interval: float = 1.0,
        gui_hwnd: Optional[int] = None,
        verify_send: bool = True,
        allow_ui_hot_activation: bool = False,
        db_factory: Optional[Callable[..., Any]] = None,
        listener_factory: Optional[Callable[..., Any]] = None,
        gui_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.db_dir = db_dir
        self.account = account
        self.poll_interval = max(0.2, float(poll_interval))
        self.gui_hwnd = gui_hwnd
        self.verify_send = bool(verify_send)
        self.allow_ui_hot_activation = bool(allow_ui_hot_activation)
        self._db_factory = db_factory
        self._listener_factory = listener_factory
        self._gui_factory = gui_factory
        self._db: Any = None
        self._listener: Any = None
        self._gui: Any = None
        self._connected = False
        self._chat_display_names: Dict[str, str] = {}
        self._contact_names: Dict[str, str] = {}
        self._contact_remarks: Dict[str, str] = {}
        self._sender_index: Dict[int, str] = {}
        # real_sender_id is not globally stable across chatrooms.  Keep
        # nickname fallbacks scoped to (chatroom, sender_id).
        self._group_sender_names: Dict[
            Tuple[str, str], Tuple[str, str, float]
        ] = {}
        self._group_sender_identities: Dict[Tuple[str, str], str] = {}
        self._display_identity_users: Dict[str, str] = {}
        self._self_display_name = "我"
        self._media_downloader: Any = None
        self._media_path_cache: Dict[Tuple[str, str, str], Optional[str]] = {}
        self._last_error: Optional[str] = None
        # wechatauto's decrypted cache is mutable even though callers only
        # read it.  Serialise listener, identity and archive reads so two
        # threads cannot rebuild/merge the same cached SQLite file at once.
        self._db_lock = threading.RLock()

    @property
    def version(self) -> Optional[str]:
        try:
            package_version = importlib.metadata.version("wechatauto-replica")
        except importlib.metadata.PackageNotFoundError:
            package_version = "unknown"
        return "wechatauto-replica:%s" % package_version

    @property
    def database(self) -> Any:
        """Expose the read-only provider for diagnostics and tests."""

        return self._db

    def export_voice(self, chat_id: str, local_id: int, save_dir: str) -> Optional[str]:
        """Export one encrypted-database voice blob as a local SILK file."""

        if self._media_downloader is None:
            raise AdapterError("当前数据库适配器没有可用的媒体下载器")
        try:
            return self._media_downloader.download_voice(
                str(chat_id), int(local_id), str(save_dir)
            )
        except Exception as exc:
            self._last_error = "语音提取失败: %s" % exc
            raise AdapterError("无法从微信媒体库提取这条语音") from exc

    def connect(self) -> None:
        if self._connected and self._db is not None:
            return
        try:
            if self._db_factory is not None:
                self._db = self._db_factory(
                    db_dir=self.db_dir,
                    account=self.account,
                )
            else:
                from wechatauto import WeChatDB

                kwargs: Dict[str, Any] = {}
                if self.db_dir:
                    kwargs["db_dir"] = self.db_dir
                if self.account:
                    kwargs["account"] = self.account
                self._db = WeChatDB(**kwargs)
        except ImportError as exc:
            raise AdapterError(
                "无法导入 wechatauto-replica；请先执行 pip install -e ."
            ) from exc
        except Exception as exc:
            self._db = None
            raise AdapterError(
                "无法读取当前微信本地数据库；请确认微信已登录并保持打开。原因: %s"
                % exc
            ) from exc
        if self._db is None:
            raise AdapterError("wechatauto 数据库适配器未创建数据库对象")
        self._refresh_identity_indexes()
        try:
            from wechatauto import MediaDownloader

            self._media_downloader = MediaDownloader(self._db)
        except Exception:
            # Media paths are optional enrichment.  Message reception must not
            # fail just because the provider's media helper is unavailable.
            self._media_downloader = None
        self._connected = True

    def disconnect(self) -> None:
        self.stop_receive()
        self._gui = None
        self._db = None
        self._chat_display_names.clear()
        self._contact_names.clear()
        self._contact_remarks.clear()
        self._sender_index.clear()
        self._group_sender_names.clear()
        self._group_sender_identities.clear()
        self._display_identity_users.clear()
        self._self_display_name = "我"
        self._media_downloader = None
        self._media_path_cache.clear()
        self._connected = False

    def health_check(self) -> HealthStatus:
        if not self._connected or self._db is None:
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="wechatauto 数据库适配器尚未连接",
            )
        unkeyed = list(getattr(self._db, "unkeyed", ()) or ())
        details = {
            "db_dir": str(getattr(self._db, "db_dir", self.db_dir or "")),
            "account": str(getattr(self._db, "account", self.account or "")),
            "keyed_database_count": len(getattr(self._db, "_keys", {}) or {}),
            "unkeyed_databases": unkeyed,
            "receive_mode": "local_db_listener",
            "send_mode": (
                "uia_or_coordinate_ocr"
                if self.allow_ui_hot_activation
                else "coordinate_ocr_no_uia_hot_activation"
            ),
        }
        if self._last_error:
            details["last_error"] = self._last_error
        if unkeyed:
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="微信数据库已定位，但仍有数据库无法解密",
                details=details,
            )
        try:
            # This is a read-only probe and also catches a stale account/key.
            self._db.get_messages("filehelper", limit=1)
        except Exception as exc:
            details["probe_error"] = str(exc)
            return HealthStatus(
                ok=False,
                adapter_name=self.name,
                adapter_version=self.version,
                message="微信数据库读取探测失败: %s" % exc,
                details=details,
            )
        return HealthStatus(
            ok=True,
            adapter_name=self.name,
            adapter_version=self.version,
            message="当前微信 4.x 本地数据库可读",
            details=details,
        )

    def start_receive(self, chat_names: Sequence[str], callback: MessageCallback) -> None:
        if not self._connected or self._db is None:
            raise AdapterError("必须先 connect() 才能开始本地数据库监听")
        names = [str(name).strip() for name in chat_names if str(name).strip()]
        if not names:
            raise AdapterError("至少需要一个监听聊天对象")
        if self._listener is not None:
            raise AdapterError("本地数据库监听已经启动")

        # Contact and group-member indexes are mutable while WeChat is open;
        # loading them only during connect() leaves later scans with stale
        # names and stale group membership mappings.
        self._refresh_identity_indexes()
        resolved = []
        for name in names:
            user, display_name = self._resolve_chat(name)
            resolved.append((user, display_name))
            self._chat_display_names[user] = display_name

        try:
            if self._listener_factory is not None:
                listener = self._listener_factory(
                    self._db,
                    interval=self.poll_interval,
                )
            else:
                listener = _MultiShardDbListener(
                    self._db,
                    interval=self.poll_interval,
                    before_poll=self._refresh_identity_indexes,
                    db_lock=self._db_lock,
                )
            for user, display_name in resolved:
                listener.add_listener(
                    user,
                    self._make_message_callback(
                        user,
                        display_name,
                        callback,
                        refresh_before_message=self._listener_factory is not None,
                    ),
                )
            listener.start()
            self._listener = listener
        except Exception as exc:
            self._listener = None
            self._last_error = str(exc)
            raise AdapterError("启动本地数据库监听失败: %s" % exc) from exc
        logger.info(
            "local DB listener started users=%s interval=%s",
            ",".join(user for user, _ in resolved),
            self.poll_interval,
        )

    def stop_receive(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                logger.exception("停止本地数据库监听失败")

    def send_text(self, chat_id: str, chat_name: str, content: str) -> SendResult:
        if not self._connected or self._db is None:
            raise AdapterError("wechatauto 数据库适配器未连接，拒绝发送")
        if not content or not content.strip():
            return SendResult(False, False, "empty_content", error="消息内容为空")

        user = str(chat_id or "").strip()
        display_name = str(chat_name or "").strip()
        if user in self._chat_display_names:
            display_name = self._chat_display_names[user]
        if not display_name:
            display_name = self._display_name_for_user(user) or user
        if not user:
            user = self._user_for_display(display_name) or display_name
        if not display_name:
            return SendResult(False, False, "missing_chat", error="缺少目标聊天")

        before_seq: Optional[int] = None
        if self.verify_send and hasattr(self._db, "_message_dbs"):
            try:
                before_seq = _latest_sort_seq(self._db, user)
            except Exception:
                logger.debug("发送前读取消息水位失败", exc_info=True)

        try:
            gui = self._get_gui()
            response = gui.send_msg(
                content,
                display_name,
                # The provider's built-in verifier assumes sender_id == 2
                # and queries only its first message shard.  The bridge owns
                # a cross-shard, status-aware verifier below instead.
                verify=False,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return SendResult(
                False,
                False,
                "gui_exception",
                error="微信界面发送异常: %s" % exc,
            )

        raw_response = self._response_text(response)
        success = bool(getattr(response, "is_success", False))
        if not success and isinstance(response, dict):
            success = response.get("status") == "成功"
        if isinstance(response, dict):
            response_message = str(response.get("message") or "")
        else:
            response_message = str(getattr(response, "message", "") or "")
        if not success:
            return SendResult(
                False,
                False,
                "gui_send_rejected",
                raw_response=raw_response,
                error=response_message or "wechatauto 未确认发送成功",
            )
        sent_message_id = None
        confirmed = None
        if self.verify_send:
            sent_message_id = self._wait_for_sent_message_id(
                user,
                content,
                after_seq=before_seq,
            )
            confirmed = sent_message_id is not None
            # A test double or another provider may have performed its own
            # confirmation.  The real wechatauto response is intentionally
            # called with verify=False and therefore cannot take this branch.
            if not confirmed and "确认" in response_message:
                confirmed = True
            if not confirmed:
                return SendResult(
                    False,
                    False,
                    "gui_send_unconfirmed",
                    raw_response=raw_response,
                    error="界面已操作，但跨分片本地数据库未确认自己消息",
                )
        return SendResult(
            True,
            confirmed,
            "gui_send_confirmed" if confirmed else "gui_send_accepted",
            raw_response=raw_response,
            sent_message_id=sent_message_id,
        )

    def send_image(self, chat_id: str, chat_name: str, path: str) -> SendResult:
        return self._send_attachment(chat_id, chat_name, path, "image")

    def send_file(self, chat_id: str, chat_name: str, path: str) -> SendResult:
        return self._send_attachment(chat_id, chat_name, path, "file")

    def get_chat_history(
        self,
        chat_id: str,
        chat_name: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self._db_lock:
            return self._get_chat_history_unlocked(chat_id, chat_name, limit)

    def _get_chat_history_unlocked(
        self,
        chat_id: str,
        chat_name: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        if not self._connected or self._db is None:
            raise AdapterError("wechatauto 数据库适配器未连接")
        self._refresh_identity_indexes()
        user = str(chat_id or "").strip() or self._user_for_display(chat_name or "")
        if not user:
            raise AdapterError("缺少聊天对象")
        if hasattr(self._db, "_message_dbs") and hasattr(self._db, "_open"):
            rows = _chat_rows(self._db, user, limit_per_shard=max(1, int(limit)))
        else:
            rows = list(self._db.get_messages(user, limit=max(1, int(limit))))
        self._prepare_group_identity_cache(rows, user)
        return [self._normalize_message(row, user, chat_name or user).__dict__ for row in reversed(rows)]

    def list_message_chats(self, limit: int = 500) -> List[Dict[str, Any]]:
        """List every local message table known by the current account."""

        with self._db_lock:
            return self._list_message_chats_unlocked(limit)

    def _list_message_chats_unlocked(self, limit: int = 500) -> List[Dict[str, Any]]:
        """List chats while holding the provider cache lock."""

        if not self._connected or self._db is None:
            raise AdapterError("wechatauto 数据库适配器未连接")
        self._refresh_identity_indexes()
        safe_limit = max(1, min(int(limit), 2_000))
        try:
            rows = list(self._db.list_message_chats())
        except sqlite3.DatabaseError as exc:
            logger.warning(
                "微信会话索引副本异常，改用消息分片与会话表重建索引: %s",
                exc,
            )
            rows = self._message_chats_from_shards(safe_limit)
        except AttributeError:
            rows = []
            try:
                sessions = list(self._db.get_sessions(limit=safe_limit))
            except Exception as exc:
                raise AdapterError("无法枚举微信消息会话: %s" % exc) from exc
            for value in sessions:
                item = dict(value)
                user = str(
                    item.get("username")
                    or item.get("user_name")
                    or item.get("chat_id")
                    or ""
                ).strip()
                if not user:
                    continue
                rows.append(
                    {
                        "username": user,
                        "name": str(
                            item.get("name")
                            or item.get("nickname")
                            or item.get("remark")
                            or user
                        ),
                        "message_count": int(item.get("message_count") or 0),
                    }
                )
        normalized = []
        for value in rows[:safe_limit]:
            item = dict(value)
            user = str(
                item.get("username")
                or item.get("user_name")
                or item.get("chat_id")
                or ""
            ).strip()
            if not user:
                continue
            is_group = user.endswith("@chatroom")
            raw_name = (
                item.get("name")
                or item.get("nickname")
                or item.get("remark")
                or self._display_name_for_user(user)
            )
            display_name = self._clean_identity(raw_name)
            if not display_name:
                display_name = (
                    "群聊" if is_group else
                    ("文件传输助手" if user == "filehelper" else "未命名联系人")
                )
            normalized.append(
                {
                    "chat_id": user,
                    "chat_name": display_name,
                    "message_count": int(item.get("message_count") or 0),
                    "is_group": is_group,
                }
            )
        if not any(item["chat_id"] == "filehelper" for item in normalized):
            normalized.append(
                {
                    "chat_id": "filehelper",
                    "chat_name": "文件传输助手",
                    "message_count": 0,
                    "is_group": False,
                }
            )
        normalized.sort(key=lambda item: (-item["message_count"], item["chat_name"]))
        return normalized[:safe_limit]

    def _message_chats_from_shards(self, limit: int) -> List[Dict[str, Any]]:
        """Rebuild the md5 chat index without relying on a fragile cache join."""

        candidates: Dict[str, str] = {
            user: self._contact_remarks.get(user)
            or self._contact_names.get(user)
            or user
            for user in set(self._contact_names) | set(self._contact_remarks)
        }
        candidates["filehelper"] = "文件传输助手"
        try:
            for raw in self._db.get_sessions(limit=max(500, int(limit))):
                item = dict(raw)
                user = str(
                    item.get("username")
                    or item.get("user_name")
                    or item.get("chat_id")
                    or ""
                ).strip()
                if user:
                    candidates[user] = str(
                        item.get("remark")
                        or item.get("name")
                        or item.get("nickname")
                        or candidates.get(user)
                        or user
                    )
        except Exception:
            logger.debug("从微信会话表补齐聊天索引失败", exc_info=True)
        md5_to_user = {
            _message_table(user)[4:]: user
            for user in candidates
        }
        counts: Dict[str, int] = {}
        if not hasattr(self._db, "_message_dbs") or not hasattr(self._db, "_open"):
            return [
                {"username": user, "name": name, "message_count": 0}
                for user, name in candidates.items()
            ]
        for shard in self._db._message_dbs():
            conn = self._db._open(shard)
            try:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
                for table_row in tables:
                    table = str(table_row[0])
                    md5 = table[4:]
                    try:
                        count = int(conn.execute('SELECT count(*) FROM "%s"' % table).fetchone()[0])
                    except sqlite3.DatabaseError:
                        continue
                    counts[md5] = counts.get(md5, 0) + count
            finally:
                conn.close()
        rows = []
        for md5, count in counts.items():
            user = md5_to_user.get(md5, md5)
            rows.append(
                {
                    "username": user,
                    "name": candidates.get(user, user),
                    "message_count": count,
                }
            )
        return rows

    def get_history_range(
        self,
        start_at: datetime,
        end_at: datetime,
        chat_ids: Optional[Sequence[str]] = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Read all readable chats in a half-open timestamp range.

        This path is read-only.  It never touches the GUI and is separate from
        the live receive listener and all send methods.
        """

        with self._db_lock:
            return self._get_history_range_unlocked(
                start_at, end_at, chat_ids=chat_ids, limit=limit
            )

    def _get_history_range_unlocked(
        self,
        start_at: datetime,
        end_at: datetime,
        chat_ids: Optional[Sequence[str]] = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        if not self._connected or self._db is None:
            raise AdapterError("wechatauto 数据库适配器未连接")
        start_value = start_at.astimezone(timezone.utc)
        end_value = end_at.astimezone(timezone.utc)
        start_ts = int(start_value.timestamp())
        end_ts = int(end_value.timestamp())
        safe_limit = max(1, min(int(limit), 200_000))
        wanted = {str(value).strip() for value in (chat_ids or ()) if str(value).strip()}
        chats = self.list_message_chats(limit=2_000)
        chat_labels = {item["chat_id"]: item["chat_name"] for item in chats}
        md5_to_user = {
            _message_table(item["chat_id"])[4:]: item["chat_id"]
            for item in chats
        }
        output: List[Dict[str, Any]] = []
        try:
            if hasattr(self._db, "_message_dbs") and hasattr(self._db, "_open"):
                rows = _all_chat_rows_in_range(
                    self._db,
                    start_ts,
                    end_ts,
                    safe_limit,
                )
                rows_by_user: Dict[str, List[Dict[str, Any]]] = {}
                for row in rows:
                    md5 = str(row.get("_bridge_md5") or "")
                    user = md5_to_user.get(md5, md5)
                    if wanted and user not in wanted:
                        continue
                    rows_by_user.setdefault(user, []).append(row)
                for user, user_rows in rows_by_user.items():
                    self._prepare_group_identity_cache(user_rows, user)
                    display_name = str(chat_labels.get(user) or user)
                    output.extend(
                        self._normalize_message(row, user, display_name).__dict__
                        for row in user_rows
                    )
            else:
                per_chat_limit = max(500, safe_limit)
                for item in chats:
                    user = item["chat_id"]
                    if wanted and user not in wanted:
                        continue
                    rows = list(self._db.get_messages(user, limit=per_chat_limit))
                    rows = [
                        row
                        for row in rows
                        if start_ts <= int(float(row.get("create_time") or 0)) < end_ts
                    ]
                    self._prepare_group_identity_cache(rows, user)
                    for row in rows:
                        output.append(
                            self._normalize_message(
                                row,
                                user,
                                str(item.get("chat_name") or user),
                            ).__dict__
                        )
        except Exception:
            logger.exception("读取微信历史范围失败")
        output.sort(key=lambda value: (value["timestamp"], value["message_id"]))
        return output

    def list_accounts(self) -> List[Dict[str, Any]]:
        try:
            from wechatauto import list_accounts

            return [dict(item) for item in list_accounts(self.db_dir)]
        except Exception as exc:
            raise AdapterError("无法枚举微信账号: %s" % exc) from exc

    def _send_attachment(
        self,
        chat_id: str,
        chat_name: str,
        path: str,
        kind: str,
    ) -> SendResult:
        if not self._connected or self._db is None:
            raise AdapterError("wechatauto 数据库适配器未连接，拒绝发送")
        if not path or not str(path).strip():
            return SendResult(False, False, "missing_path", error="附件路径为空")
        user = str(chat_id or "").strip()
        display_name = str(chat_name or "").strip()
        if not user:
            user = self._user_for_display(display_name) or display_name
        if not display_name:
            display_name = self._display_name_for_user(user) or user
        if not user or not display_name:
            return SendResult(False, False, "missing_chat", error="缺少目标聊天")
        try:
            gui = self._get_gui()
            method = getattr(gui, "send_image" if kind == "image" else "send_file")
            response = method(str(path), display_name, verify=False)
        except Exception as exc:
            self._last_error = str(exc)
            return SendResult(False, False, "gui_attachment_exception", error=str(exc))
        raw_response = self._response_text(response)
        success = bool(getattr(response, "is_success", False))
        if isinstance(response, dict):
            success = success or response.get("status") == "成功"
        if not success:
            return SendResult(
                False,
                False,
                "gui_attachment_rejected",
                raw_response=raw_response,
                error="微信未确认%s发送成功" % ("图片" if kind == "image" else "文件"),
            )
        return SendResult(
            True,
            None,
            "gui_attachment_accepted",
            raw_response=raw_response,
        )

    def _get_gui(self) -> Any:
        if self._gui is not None:
            return self._gui
        if self._gui_factory is not None:
            if self.gui_hwnd is None:
                gui = self._gui_factory()
            else:
                gui = self._gui_factory(hwnd=self.gui_hwnd)
        else:
            from wechatauto.guia import WeChatGUI

            kwargs = {} if self.gui_hwnd is None else {"hwnd": self.gui_hwnd}
            gui = WeChatGUI(**kwargs)
        # UIA hot activation writes one known Qt accessibility state byte in
        # the running Weixin process.  It is opt-in; the default provider
        # remains the coordinate/OCR path and does not modify that process.
        if not self.allow_ui_hot_activation and hasattr(gui, "_uia_tried"):
            gui._uia_tried = True
            gui._uia = None
        if hasattr(gui, "_cached_db"):
            gui._cached_db = self._db
        self._gui = gui
        return gui

    def _resolve_chat(self, value: str) -> Tuple[str, str]:
        user = self._DISPLAY_TO_USER.get(value, value)
        if user == "filehelper":
            return user, "文件传输助手"

        sessions = self._db.get_sessions(limit=500)
        for session in sessions:
            username = str(session.get("username") or "")
            if username == value:
                display = self._clean_identity(
                    session.get("remark")
                    or session.get("nickname")
                    or session.get("nick_name")
                    or session.get("name")
                )
                return username, display or self._display_name_for_user(username) or "待识别成员"

        hits = self._db.search_contact(value)
        if hits:
            first = hits[0]
            username = str(first.get("username") or "")
            if username:
                display = self._clean_identity(
                    first.get("remark")
                    or first.get("nick_name")
                    or value
                ) or "待识别成员"
                return username, display
        raise AdapterError(
            "无法把聊天名称解析为微信号: %s；请使用会话名称或微信号" % value
        )

    def _user_for_display(self, display_name: str) -> Optional[str]:
        if display_name in self._DISPLAY_TO_USER:
            return self._DISPLAY_TO_USER[display_name]
        for user, display in self._chat_display_names.items():
            if display == display_name:
                return user
        try:
            hits = self._db.search_contact(display_name)
        except Exception:
            return None
        return str(hits[0].get("username")) if hits else None

    def _display_name_for_user(self, user: str) -> Optional[str]:
        if user == "filehelper":
            return "文件传输助手"
        for value in (
            self._chat_display_names.get(user),
            self._contact_remarks.get(user),
            self._contact_names.get(user),
        ):
            display = self._clean_identity(value)
            if display:
                return display
        return None

    def _make_message_callback(
        self,
        user: str,
        display_name: str,
        callback: MessageCallback,
        refresh_before_message: bool = False,
    ) -> Callable[..., None]:
        def on_message(raw: Dict[str, Any], *_args: Any) -> None:
            try:
                if refresh_before_message:
                    self._refresh_identity_indexes()
                callback(self._normalize_message(raw, user, display_name))
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("本地数据库消息标准化失败")

        return on_message

    @staticmethod
    def _clean_identity(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if not text or text.lower().startswith(("wxid_", "gh_")):
            return None
        if text.isdigit() or len(text) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return None
        return text

    def _refresh_identity_indexes(self) -> None:
        """Refresh mutable contact and group-member indexes before reads."""
        with self._db_lock:
            self._load_identity_indexes()

    def _load_identity_indexes(self) -> None:
        """Load contact and group-member indexes from the read-only DB.

        WeChat stores a group's sender as a small integer in the message
        table.  Resolving it through ``message_resource`` and ``contact`` is
        what keeps the UI human-readable without leaking a wxid. The numeric
        index is retained as provider metadata only; it is never used as a
        global sender-to-person mapping because real_sender_id is scoped to a
        chatroom.
        """

        self._contact_names.clear()
        self._contact_remarks.clear()
        self._sender_index.clear()
        if self._db is None:
            return
        try:
            raw_names = self._db._nickname_index()
            for key, value in raw_names.items():
                user = str(key or "").strip()
                display = self._clean_identity(value)
                if user and display:
                    self._contact_names[user] = display
        except Exception:
            logger.debug("加载微信昵称索引失败", exc_info=True)
        try:
            raw_sender_index = self._db._sender_id_index()
            self._sender_index.update(
                {int(key): str(value) for key, value in raw_sender_index.items()}
            )
        except Exception:
            logger.debug("加载群成员索引失败", exc_info=True)
        try:
            for rel, path, _ in getattr(self._db, "_db_files", ()):
                if os.path.basename(path) != "contact.db":
                    continue
                conn = self._db._open(rel)
                try:
                    for row in conn.execute(
                        "SELECT username, nick_name, remark FROM contact"
                    ):
                        user = str(row[0] or "").strip()
                        remark = self._clean_identity(row[2])
                        nickname = self._clean_identity(row[1])
                        if user and remark:
                            self._contact_remarks[user] = remark
                        if user and (remark or nickname):
                            self._contact_names[user] = remark or nickname  # type: ignore[assignment]
                finally:
                    conn.close()
                break
        except Exception:
            logger.debug("加载通讯录备注失败", exc_info=True)
        # Reverse lookup is intentionally kept only for unique display names.
        # Ambiguous nicknames must not be used to guess a person.
        self._display_identity_users.clear()
        ambiguous = set()
        for user in set(self._contact_names) | set(self._contact_remarks):
            for value in (
                self._contact_remarks.get(user),
                self._contact_names.get(user),
            ):
                identity = self._clean_identity(value)
                if not identity:
                    continue
                if identity in ambiguous:
                    continue
                previous = self._display_identity_users.get(identity)
                if previous is not None and previous != user:
                    self._display_identity_users.pop(identity, None)
                    ambiguous.add(identity)
                else:
                    self._display_identity_users[identity] = user
        # Re-evaluate cached group-local identities after a refresh so a
        # newly added/changed friend remark takes precedence immediately.
        for key, identity in tuple(self._group_sender_identities.items()):
            detail = self._group_identity_detail(identity)
            if detail[0]:
                self._group_sender_names[key] = (
                    str(detail[0]), detail[1], detail[2]
                )
            else:
                self._group_sender_names.pop(key, None)
        try:
            self_info = self._db.get_self_info()
            self._self_display_name = (
                self._clean_identity(self_info.get("remark"))
                or self._clean_identity(self_info.get("nick_name"))
                or "我"
            )
        except Exception:
            self._self_display_name = "我"

    @staticmethod
    def _group_prefix(content: str) -> Tuple[Optional[str], str]:
        # Group messages commonly arrive as ``群昵称:\n正文``.  Only strip a
        # prefix when a newline follows it, so ordinary URLs and prose remain
        # untouched.
        match = re.match(r"^\s*([^:\r\n]{1,80})\s*:\s*\r?\n(.*)$", content, re.S)
        if not match:
            return None, content
        prefix = str(match.group(1) or "").strip()
        if not prefix:
            return None, match.group(2).strip()
        # Keep wxid_/gh_ prefixes as identifiers. They are not suitable for
        # display by themselves, but can be resolved through contact.db.
        if prefix.lower().startswith(("wxid_", "gh_")):
            return prefix, match.group(2).strip()
        cleaned = WeChatAutoDbAdapter._clean_identity(prefix)
        if cleaned is None:
            return None, match.group(2).strip()
        return cleaned, match.group(2).strip()

    def _contact_identity_detail(self, identity: Any) -> Tuple[Optional[str], str, float]:
        value = str(identity or "").strip()
        if not value:
            return None, "unresolved", 0.0
        remark = self._clean_identity(self._contact_remarks.get(value))
        if remark:
            return remark, "contact_remark", 0.98
        name = self._clean_identity(self._contact_names.get(value))
        if name:
            return name, "contact_nickname", 0.90
        mapped_user = self._display_identity_users.get(value)
        if mapped_user:
            display = (
                self._clean_identity(self._contact_remarks.get(mapped_user))
                or self._clean_identity(self._contact_names.get(mapped_user))
            )
            if display:
                source = (
                    "contact_remark"
                    if self._contact_remarks.get(mapped_user)
                    else "contact_nickname"
                )
                return display, source, 0.90 if source == "contact_nickname" else 0.98
        return None, "unresolved", 0.0

    def _contact_display_for_identity(self, identity: Any) -> Optional[str]:
        return self._contact_identity_detail(identity)[0]

    def _resolve_group_identity(self, identity: Any) -> Optional[str]:
        """Resolve a group prefix without consulting another group's id map."""

        return self._group_identity_detail(identity)[0]

    def _group_identity_detail(self, identity: Any) -> Tuple[Optional[str], str, float]:
        """Resolve group identity with remark > group nickname > nickname."""

        value = str(identity or "").strip()
        if not value:
            return None, "unresolved", 0.0
        # WeChat ids are identifiers, so they can be resolved to a contact
        # nickname/remark. A human-readable group prefix must be preserved;
        # only an exact contact remark is allowed to override it.
        if value.lower().startswith(("wxid_", "gh_")):
            return self._contact_identity_detail(value)
        direct_remark = self._clean_identity(self._contact_remarks.get(value))
        if direct_remark:
            return direct_remark, "contact_remark", 0.98
        mapped_user = self._display_identity_users.get(value)
        if mapped_user:
            remark = self._clean_identity(self._contact_remarks.get(mapped_user))
            if remark:
                return remark, "contact_remark", 0.98
        cleaned = self._clean_identity(value)
        if cleaned:
            return cleaned, "group_nickname", 0.96
        return None, "unresolved", 0.0

    def _remember_group_identity(
        self,
        user: str,
        sender_raw: Any,
        group_identity: Any,
    ) -> None:
        if not str(user).endswith("@chatroom"):
            return
        sender_key = str(sender_raw or "").strip()
        if not sender_key:
            return
        identity_key = str(group_identity or "").strip()
        if not identity_key:
            return
        cache_key = (str(user), sender_key)
        self._group_sender_identities[cache_key] = identity_key
        detail = self._group_identity_detail(identity_key)
        if detail[0]:
            self._group_sender_names[cache_key] = (
                str(detail[0]), detail[1], detail[2]
            )
        else:
            self._group_sender_names.pop(cache_key, None)

    def _learn_group_identity(self, raw: Dict[str, Any], user: str) -> None:
        if not str(user).endswith("@chatroom"):
            return
        content = raw.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        prefix, _ = self._group_prefix(content)
        if prefix:
            self._remember_group_identity(user, raw.get("sender_id"), prefix)

    def _prepare_group_identity_cache(
        self,
        rows: Sequence[Dict[str, Any]],
        user: str,
    ) -> None:
        for row in rows:
            self._learn_group_identity(row, user)

    def _sender_identity(
        self,
        sender_raw: Any,
        user: str,
        group_nickname: Optional[str],
        is_self: Optional[bool],
        chat_display_name: Optional[str] = None,
    ) -> Tuple[str, str, float]:
        if is_self is True:
            return "我", "self", 1.0
        is_group = str(user).endswith("@chatroom")
        if is_group:
            # Per-message group prefix is the strongest evidence. A wxid
            # prefix is resolved through contact.db; a normal prefix is kept
            # as the group nickname. Neither path consults a global numeric
            # sender map.
            group_detail = self._group_identity_detail(group_nickname)
            group_display = group_detail[0]
            if group_display:
                self._remember_group_identity(user, sender_raw, group_nickname)
                return group_display, group_detail[1], group_detail[2]
            remembered = self._group_sender_names.get(
                (str(user), str(sender_raw or "").strip())
            )
            if remembered:
                if isinstance(remembered, tuple):
                    return remembered
                # Compatibility for adapters/tests that populated the old
                # display-only cache shape.
                return str(remembered), "group_nickname_cached", 0.86
            sender_identity = str(sender_raw or "").strip()
            if not sender_identity.lower().startswith(("wxid_", "gh_")):
                sender_identity = self._clean_identity(sender_identity) or ""
            resolved, source, confidence = self._contact_identity_detail(sender_identity)
            if resolved:
                return resolved, source, confidence
            # A numeric sender id without group-local evidence is unsafe to
            # resolve through the global SenderName2Id table.
            return "待识别成员", "unresolved", 0.0
        # In a direct chat, the chat user itself is the only stable peer
        # identity.  ``real_sender_id`` is a small numeric value and the
        # global SenderName2Id table can point at an unrelated contact (it is
        # especially unsafe after group messages).  Never use that table for
        # one-to-one messages.
        resolved, _source, _confidence = self._contact_identity_detail(user)
        if resolved:
            return resolved, "direct_chat_peer", 1.0
        chat_display = self._clean_identity(chat_display_name) or self._clean_identity(
            self._chat_display_names.get(user)
        )
        if chat_display:
            return chat_display, "direct_chat_peer", 0.95
        return "待识别成员", "unresolved", 0.0

    def _sender_display_name(
        self,
        sender_raw: Any,
        user: str,
        group_nickname: Optional[str],
        is_self: Optional[bool],
        chat_display_name: Optional[str] = None,
    ) -> str:
        return self._sender_identity(
            sender_raw,
            user,
            group_nickname,
            is_self,
            chat_display_name,
        )[0]

    def _media_metadata(
        self,
        raw: Dict[str, Any],
        user: str,
        message_type: str,
        content: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if message_type not in {"image", "link_or_file", "voice", "video"}:
            return None, None, None
        blobs = [
            raw.get("media_path"), raw.get("path"), raw.get("file_path"),
            raw.get("local_path"), raw.get("media_name"), raw.get("file_name"),
            raw.get("filename"), raw.get("title"), raw.get("packed_info"),
            raw.get("_bridge_packed_info"), raw.get("_bridge_source"),
            raw.get("content"), content,
        ]
        text_blobs = []
        for value in blobs:
            if isinstance(value, bytes):
                text_blobs.append(value.decode("utf-8", errors="replace"))
            elif value is not None:
                text_blobs.append(str(value))
        joined = "\n".join(text_blobs)
        md5_match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", joined)
        media_md5 = md5_match.group(1).lower() if md5_match else None
        media_name = None
        for value in (
            raw.get("media_name"), raw.get("file_name"), raw.get("filename"),
            raw.get("title"),
        ):
            candidate = self._clean_identity(value)
            if candidate:
                media_name = candidate
                break
        media_path = None
        for value in (
            raw.get("media_path"), raw.get("path"), raw.get("file_path"),
            raw.get("local_path"),
        ):
            candidate = str(value or "").strip()
            if candidate and (os.path.exists(candidate) or ":\\" in candidate or candidate.startswith("/")):
                media_path = candidate
                break

        account_dir = str(getattr(self._db, "account_dir", "") or "")
        if not media_path and account_dir:
            chat_hash = hashlib.md5(str(user).encode("utf-8")).hexdigest()
            month = ""
            try:
                month = time.strftime("%Y-%m", time.localtime(float(raw.get("create_time") or 0)))
            except (TypeError, ValueError, OverflowError):
                month = ""
            candidates: List[str] = []
            directory = ""
            if message_type == "image":
                root = os.path.join(account_dir, "msg", "attach", chat_hash)
                if month:
                    root = os.path.join(root, month, "Img")
                directory = root
                if media_md5:
                    candidates = [os.path.join(root, media_md5 + suffix) for suffix in (".dat", "_t.dat", "_h.dat")]
            elif message_type == "video":
                root = os.path.join(account_dir, "msg", "video")
                if month:
                    root = os.path.join(root, month)
                directory = root
                if media_md5:
                    candidates = [os.path.join(root, media_md5 + ".mp4")]
            elif message_type == "link_or_file":
                root = os.path.join(account_dir, "msg", "file")
                if month:
                    root = os.path.join(root, month)
                directory = root
                if media_name:
                    candidates = [os.path.join(root, media_name)]
            if candidates:
                cache_key = (str(user), message_type, candidates[0])
                if cache_key in self._media_path_cache:
                    media_path = self._media_path_cache[cache_key]
                else:
                    media_path = next((candidate for candidate in candidates if os.path.isfile(candidate)), None)
                    if not media_path:
                        # A media row can legitimately lack its md5/name in
                        # the decrypted message table. Expose the exact
                        # month/type cache directory as an honest fallback;
                        # the UI labels it as a directory, not as the file.
                        directory = os.path.dirname(candidates[0])
                        if os.path.isdir(directory):
                            media_path = directory
                            media_name = media_name or "微信缓存目录（未定位具体文件）"
                    self._media_path_cache[cache_key] = media_path
            elif directory and os.path.isdir(directory):
                media_path = directory
                media_name = media_name or "微信缓存目录（未定位具体文件）"
        return media_path, media_name, media_md5

    def _normalize_message(
        self,
        raw: Dict[str, Any],
        user: str,
        display_name: str,
    ) -> IncomingMessage:
        local_id = raw.get("local_id")
        sort_seq = raw.get("sort_seq")
        shard = raw.get("_bridge_shard")
        if shard and local_id is not None:
            # local_id values can repeat in different message shards.
            identity = "%s:%s" % (shard, local_id)
        else:
            identity = local_id if local_id is not None else sort_seq
        if identity is None:
            identity = hashlib.sha256(repr(sorted(raw.items())).encode()).hexdigest()
        sender_raw = raw.get("sender_id")
        status_raw = raw.get("_bridge_status")
        if status_raw is not None:
            try:
                # WeChat 4.x records outgoing rows with status=2.  This is
                # chat/shard safe; real_sender_id is not a global direction
                # flag and can vary between message shards.
                is_self = int(status_raw) == 2
            except (TypeError, ValueError):
                is_self = None
        else:
            try:
                sender_number = int(sender_raw)
            except (TypeError, ValueError):
                sender_number = None
            is_self = None if sender_number is None else sender_number == 2
        message_type = self._MESSAGE_TYPES.get(str(raw.get("type") or ""), "other")
        content = raw.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        is_group = str(user).endswith("@chatroom")
        group_nickname, content = self._group_prefix(content) if is_group else (None, content)
        safe_chat_name = self._clean_identity(display_name) or self._display_name_for_user(user)
        if not safe_chat_name:
            safe_chat_name = "群聊" if is_group else "待识别成员"
        sender_name, sender_name_source, sender_name_confidence = self._sender_identity(
            sender_raw,
            user,
            group_nickname,
            is_self,
            display_name,
        )
        media_path, media_name, media_md5 = self._media_metadata(
            raw,
            user,
            message_type,
            content,
        )
        return IncomingMessage(
            message_id="%s:%s" % (user, identity),
            chat_id=user,
            chat_name=safe_chat_name,
            sender_id=str(sender_raw) if sender_raw is not None else None,
            sender_name=sender_name,
            message_type=message_type,
            content=content,
            timestamp=self._timestamp(raw.get("create_time")),
            is_self=is_self,
            raw_message={"user": user, "message": raw},
            adapter_name=self.name,
            adapter_version=self.version,
            is_group=is_group,
            media_path=media_path,
            media_name=media_name,
            media_md5=media_md5,
            sender_name_source=sender_name_source,
            sender_name_confidence=sender_name_confidence,
        )

    def _find_sent_message_id(
        self,
        user: str,
        content: str,
        after_seq: Optional[int] = None,
    ) -> Optional[str]:
        try:
            if hasattr(self._db, "_message_dbs") and hasattr(self._db, "_open"):
                for message in _chat_rows(
                    self._db,
                    user,
                    since_seq=None,
                    limit_per_shard=100,
                ):
                    if message.get("content") != content:
                        continue
                    if after_seq is not None:
                        try:
                            if int(message.get("sort_seq") or 0) <= after_seq:
                                continue
                        except (TypeError, ValueError):
                            continue
                    status = message.get("_bridge_status")
                    if status is not None and int(status) != 2:
                        continue
                    value = message.get("local_id")
                    shard = message.get("_bridge_shard") or "unknown-shard"
                    return "%s:%s:%s" % (user, shard, value)
            else:
                for message in self._db.get_messages(user, limit=10):
                    if message.get("sender_id") == 2 and message.get("content") == content:
                        value = message.get("local_id")
                        return str(value) if value is not None else None
        except Exception:
            logger.debug("发送回读 ID 查询失败", exc_info=True)
        return None

    def _wait_for_sent_message_id(
        self,
        user: str,
        content: str,
        after_seq: Optional[int] = None,
        timeout: float = 8.0,
    ) -> Optional[str]:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            sent_id = self._find_sent_message_id(
                user,
                content,
                after_seq=after_seq,
            )
            if sent_id is not None:
                return sent_id
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return utc_now()

    @staticmethod
    def _response_text(response: Any) -> str:
        if isinstance(response, dict):
            status = response.get("status")
            message = response.get("message")
            return "%s: %s" % (status, message)
        return str(response)
