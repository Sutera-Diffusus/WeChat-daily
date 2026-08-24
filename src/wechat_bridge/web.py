"""Small local-only HTTP API and dashboard server."""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import threading
from email.message import EmailMessage
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .ai import AnalysisGenerationError, OpenAIAnalysisGenerator
from .analysis import (
    _redact_ai_content,
    _score_message,
    analyze_messages,
    build_ai_context,
    is_editorial_title,
    normalize_editorial_title,
)
from .engine import ReplyRule
from .image_key import request_image_key_discovery, runtime_image_key
from .media import MediaUnavailable, V2_MAGIC, cache_key, read_media
from .models import IncomingMessage
from .settings import WorkbenchSettings
from .timeutil import get_timezone
from .voice import ASRError, DoubaoASRClient, decode_silk_to_wav, extract_wechat_voice_transcript

logger = logging.getLogger("wechat_bridge.web")
WEB_ROOT = Path(__file__).with_name("web")


def _chrome_executable() -> Optional[str]:
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def _render_report_pdf(html: str) -> bytes:
    executable = _chrome_executable()
    if not executable:
        raise RuntimeError("未找到可用于生成 PDF 的 Chrome 或 Edge")
    with tempfile.TemporaryDirectory(prefix="wechat-report-") as directory:
        root = Path(directory)
        html_path = root / "report.html"
        pdf_path = root / "report.pdf"
        html_path.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [
                executable,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-pdf-header-footer",
                "--print-to-pdf=%s" % pdf_path,
                html_path.resolve().as_uri(),
            ],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0 or not pdf_path.is_file():
            detail = result.stderr.decode("utf-8", errors="ignore").strip()[-500:]
            raise RuntimeError("PDF 生成失败%s" % (("：" + detail) if detail else ""))
        return pdf_path.read_bytes()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def _rule_json(rule: ReplyRule) -> Dict[str, Any]:
    return {
        "name": rule.name,
        "enabled": rule.enabled,
        "reply_text": rule.reply_text,
        "keywords": list(rule.keywords),
        "regexes": list(rule.regexes),
        "chats": list(rule.chats),
        "senders": list(rule.senders),
        "message_types": list(rule.message_types),
        "time_ranges": [
            {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
            for start, end in rule.time_ranges
        ],
    }


def _date_range(
    query: Dict[str, Any],
    timezone_name: str,
) -> tuple:
    """Parse inclusive local dates into an aware UTC half-open interval."""

    tz = get_timezone(timezone_name)
    today = datetime.now(tz).date()
    period = str((query.get("period") or [""])[0]).strip().lower()
    start_raw = str((query.get("start") or [""])[0]).strip()
    end_raw = str((query.get("end") or [""])[0]).strip()
    if period == "week" and not start_raw and not end_raw:
        start_day = today - timedelta(days=6)
        end_day = today
    elif period == "day" and not start_raw and not end_raw:
        start_day = end_day = today
    else:
        try:
            start_day = date.fromisoformat(start_raw[:10]) if start_raw else today
            end_day = date.fromisoformat(end_raw[:10]) if end_raw else start_day
        except ValueError as exc:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
    if end_day < start_day:
        raise ValueError("结束日期不能早于开始日期")
    start_at = datetime.combine(start_day, datetime_time.min, tzinfo=tz)
    end_at = datetime.combine(end_day + timedelta(days=1), datetime_time.min, tzinfo=tz)
    return start_at.astimezone(timezone.utc), end_at.astimezone(timezone.utc), start_day, end_day


_WORKBENCH_ROW_LIMIT = 200_000
_FEED_CURSOR_PREFIX = "v1."
_INTERNAL_NAME_RE = re.compile(r"^(?:wxid_|gh_)[\w-]+$", re.IGNORECASE)


def _message_timestamp(value: Any) -> datetime:
    """Normalize a stored timestamp for stable ordering and cursors."""

    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            result = datetime.min.replace(tzinfo=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _row_id(value: Mapping[str, Any]) -> int:
    try:
        return int(value.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _is_self_message(item: Mapping[str, Any]) -> Optional[bool]:
    value = item.get("is_self")
    if value is True or (isinstance(value, int) and value == 1):
        return True
    if value is False or (isinstance(value, int) and value == 0):
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _is_internal_name(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or bool(_INTERNAL_NAME_RE.fullmatch(text)) or text.isdigit()


def _safe_name(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return None if _is_internal_name(text) else text


def _chat_display_name(item: Mapping[str, Any]) -> str:
    value = _safe_name(item.get("chat_name"))
    if value:
        return value
    return "群聊" if bool(item.get("is_group")) else "未命名会话"


def _sender_display_name(item: Mapping[str, Any]) -> str:
    if _is_self_message(item) is True:
        return "我"
    value = _safe_name(item.get("sender_name"))
    if value:
        return value
    if not bool(item.get("is_group")):
        return _chat_display_name(item)
    return "待识别成员"


def _content_preview(item: Mapping[str, Any]) -> str:
    content = str(item.get("content") or "").strip()
    if content:
        return content
    message_type = str(item.get("message_type") or "other").strip()
    return "[%s]" % (message_type or "其他")


def _is_media_message(item: Mapping[str, Any]) -> bool:
    message_type = str(item.get("message_type") or "text").strip().lower()
    if message_type not in {"text", "other", "system"}:
        return True
    content = str(item.get("content") or "").strip()
    return bool(re.match(r"^\s*\[(?:图片|语音|视频|动画表情|文件/链接/卡片|文件|链接|卡片)(?:\s+[^\]]+)?\]\s*$", content))


def _load_message_window(
    store: Any,
    query: Dict[str, Any],
    timezone_name: str,
    *,
    default_all: bool,
    limit: int = _WORKBENCH_ROW_LIMIT,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], datetime, datetime]:
    """Load one bounded archive window without adding methods to SQLiteStore."""

    has_explicit_range = any(key in query for key in ("start", "end", "period"))
    if has_explicit_range or not default_all:
        start_at, end_at, start_day, end_day = _date_range(query, timezone_name)
        window = {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "timezone": timezone_name,
            "mode": "date_range",
        }
    else:
        # SQLiteStore intentionally caps recent_messages at 500.  A broad
        # date query gives the workbench a bounded archive read without
        # reaching into the store's private SQLite connection.
        start_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        end_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        window = {
            "start": None,
            "end": None,
            "timezone": timezone_name,
            "mode": "archive",
        }
    chat = str((query.get("chat") or [""])[0]).strip() or None
    messages = store.messages_between(start_at, end_at, chat, limit)
    return messages, window, start_at, end_at


def _runtime_snapshot(service: Any) -> Dict[str, Any]:
    try:
        value = service.status_snapshot()
        return dict(value) if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("workbench runtime status unavailable: %s", exc)
        return {
            "started": False,
            "receiving": False,
            "status_error": str(exc),
        }


def _live_chat_values(status: Dict[str, Any], service: Any) -> set:
    values = status.get("chats")
    if not isinstance(values, (list, tuple, set)):
        values = getattr(service, "chat_names", ())
    result = {str(value).strip() for value in values if str(value).strip()}
    if "文件传输助手" in result:
        result.add("filehelper")
    if "filehelper" in result:
        result.add("文件传输助手")
    return result


def _chat_is_live(item: Mapping[str, Any], live_values: set) -> bool:
    return bool(
        str(item.get("chat_id") or "").strip() in live_values
        or str(item.get("chat_name") or "").strip() in live_values
    )


def _capture_state(
    *,
    is_live: bool,
    receiving: bool,
    sync_state: str,
    has_messages: bool,
) -> str:
    if is_live and receiving:
        return "fresh"
    if sync_state in {"running", "failed"}:
        return "partial"
    if has_messages:
        return "stale"
    return "unknown"


def _scope_payload(status: Dict[str, Any], service: Any) -> Dict[str, Any]:
    live_values = _live_chat_values(status, service)
    receiving = bool(status.get("receiving"))
    sync = status.get("sync") if isinstance(status.get("sync"), dict) else {}
    sync_state = str(sync.get("state") or "unknown")
    live_names = [
        value
        for value in (status.get("chats") or getattr(service, "chat_names", ()))
        if str(value).strip()
    ]
    history_label = str(status.get("history_scope") or "全部可读会话")
    realtime_state = "fresh" if receiving and live_values else "unknown"
    history_state = "partial" if sync_state in {"running", "failed"} else "unknown"
    realtime = {
        "mode": "live",
        "label": str(status.get("live_scope") or "、".join(map(str, live_names)) or "—"),
        "chats": [str(value) for value in live_names],
        "receiving": receiving,
        "capture_state": realtime_state,
    }
    history = {
        "mode": "history",
        "label": history_label,
        "scope": "all_readable_chats",
        "capture_state": history_state,
        "last_sync_at": status.get("last_sync_at"),
        "sync_state": sync_state,
    }
    return {
        "realtime": realtime,
        "history": history,
        "live_values": sorted(live_values),
        "sync_state": sync_state,
    }


def _quality_with_scope(
    quality: Dict[str, Any],
    status: Dict[str, Any],
    scope: Dict[str, Any],
) -> Dict[str, Any]:
    value = dict(quality or {})
    value.setdefault("capture_completeness", None)
    value.setdefault("capture_completeness_state", "unknown")
    value["realtime_scope"] = scope["realtime"]
    value["history_scope"] = scope["history"]
    value["sync_state"] = scope["sync_state"]
    value["last_sync_at"] = status.get("last_sync_at")
    if value.get("capture_completeness") is None:
        value["capture_completeness_state"] = "unknown"
    return value


def _feed_cursor(item: Mapping[str, Any]) -> str:
    payload = {
        "timestamp": _message_timestamp(item.get("timestamp")).isoformat(timespec="microseconds"),
        "row_id": _row_id(item),
        "message_id": str(item.get("message_id") or ""),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return _FEED_CURSOR_PREFIX + encoded


def _decode_feed_cursor(
    value: Any,
    timezone_name: str,
) -> Tuple[datetime, int, str]:
    text = str(value or "").strip()
    if not text.startswith(_FEED_CURSOR_PREFIX):
        raise ValueError("cursor 格式无效")
    encoded = text[len(_FEED_CURSOR_PREFIX):]
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        timestamp = datetime.fromisoformat(str(payload["timestamp"]))
        row_id = int(payload.get("row_id", 0))
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor 格式无效") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=get_timezone(timezone_name))
    if row_id < 0:
        raise ValueError("cursor 格式无效")
    return timestamp.astimezone(timezone.utc), row_id, str(payload.get("message_id") or "")


def _decode_before(value: Any, timezone_name: str) -> Optional[Tuple[datetime, int]]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(_FEED_CURSOR_PREFIX):
        return _decode_feed_cursor(text, timezone_name)
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("before 必须是 ISO 时间或有效 cursor") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=get_timezone(timezone_name))
    return timestamp.astimezone(timezone.utc), 0


def _feed_filter_name(value: Any) -> str:
    normalized = str(value or "all").strip().lower()
    aliases = {
        "": "all",
        "all": "all",
        "inbound": "inbound",
        "received": "inbound",
        "incoming": "inbound",
        "self": "self",
        "outbound": "self",
        "high": "high_signal",
        "high_signal": "high_signal",
        "important": "high_signal",
        "attention": "attention",
        "needs_attention": "attention",
        "media": "media",
        "text": "text",
        "unresolved": "unresolved",
    }
    if normalized not in aliases:
        raise ValueError("不支持的 filter：%s" % normalized)
    return aliases[normalized]


def _feed_filter_matches(item: Mapping[str, Any], filter_name: str) -> bool:
    direction = _is_self_message(item)
    if filter_name == "all":
        return True
    if filter_name == "inbound":
        return direction is False
    if filter_name == "self":
        return direction is True
    if filter_name == "media":
        return _is_media_message(item)
    if filter_name == "text":
        return not _is_media_message(item)
    if filter_name == "unresolved":
        return direction is False and _is_internal_name(item.get("sender_name"))
    score = _score_message(item)
    if filter_name == "high_signal":
        return direction is False and score.get("level") == "high"
    if filter_name == "attention":
        return direction is False and score.get("level") in {"high", "medium"}
    return True


def _feed_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    value = dict(item)
    value.pop("id", None)
    value["chat_name"] = _chat_display_name(item)
    value["sender_name"] = _sender_display_name(item)
    score = _score_message(item)
    value["signal"] = {
        "level": score.get("level"),
        "score": score.get("score", 0),
        "value_label": score.get("value_label"),
        "tags": list(score.get("tags") or []),
        "reason": score.get("reason"),
    }
    value["display_chat_name"] = value["chat_name"]
    value["display_sender_name"] = value["sender_name"]
    return value


def _contact_choice(
    item: Mapping[str, Any],
) -> List[Tuple[str, str, int]]:
    source = str(item.get("sender_name_source") or "observed").strip() or "observed"
    priorities = {
        "contact_remark": 100,
        "group_nickname": 90,
        "contact_nickname": 80,
        "direct_chat_peer": 75,
        "chat_name": 60,
        "observed": 20,
    }
    options: List[Tuple[str, str, int]] = []
    sender = _safe_name(item.get("sender_name"))
    if sender:
        options.append((sender, source, priorities.get(source, 20)))
    if not bool(item.get("is_group")):
        chat_name = _safe_name(item.get("chat_name"))
        if chat_name and chat_name != sender:
            options.append((chat_name, "chat_name", priorities["chat_name"]))
    return options


def _contact_key(item: Mapping[str, Any]) -> Tuple[str, ...]:
    chat_id = str(item.get("chat_id") or item.get("chat_name") or "unknown").strip()
    if bool(item.get("is_group")):
        # Group sender ids are scoped to the chat by the adapter.  Keeping the
        # chat in the key avoids reintroducing the historic cross-group merge.
        sender_id = str(item.get("sender_id") or "").strip()
        safe_sender = _safe_name(item.get("sender_name")) or "pending"
        return ("group", chat_id, sender_id or safe_sender)
    return ("direct", chat_id)


def _contact_id(key: Sequence[str]) -> str:
    digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()
    return "contact-%s" % digest[:16]


class BridgeHttpServer(ThreadingHTTPServer):
    """HTTP server bound to loopback by default."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service) -> None:
        self.service = service
        self.store = service.store
        self.settings = WorkbenchSettings.for_service(service)
        self.latest_ai_analysis: Optional[Dict[str, Any]] = None
        self.ai_analysis_by_window: Dict[str, Dict[str, Any]] = {}
        self.ai_raw_by_window: Dict[str, Dict[str, Any]] = {}
        self.ai_analysis_lock = threading.Lock()
        super().__init__(address, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.info("dashboard %s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_asset("index.html", "text/html; charset=utf-8")
        if parsed.path == "/assets/styles.css":
            return self._serve_asset("styles.css", "text/css; charset=utf-8")
        if parsed.path == "/assets/polish.css":
            return self._serve_asset("polish.css", "text/css; charset=utf-8")
        if parsed.path == "/assets/app.js":
            return self._serve_asset("app.js", "application/javascript; charset=utf-8")
        if parsed.path.startswith("/assets/editorial/"):
            relative_name = unquote(parsed.path[len("/assets/editorial/"):]).replace("\\", "/")
            if not relative_name or ".." in relative_name.split("/"):
                return self._json({"error": "forbidden"}, 403)
            suffix = Path(relative_name).suffix.lower()
            content_type = {
                ".otf": "font/otf",
                ".ttf": "font/ttf",
                ".woff2": "font/woff2",
                ".svg": "image/svg+xml",
                ".txt": "text/plain; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            return self._serve_asset("assets/editorial/" + relative_name, content_type)
        if parsed.path == "/api/status":
            return self._json(self._status())
        if parsed.path == "/api/settings":
            return self._json(self.server.settings.public())
        if parsed.path == "/api/ai-status":
            return self._json(self._ai_status())
        if parsed.path == "/api/ai-latest":
            latest_query = parse_qs(parsed.query)
            start = str((latest_query.get("start") or [""])[0]).strip()
            end = str((latest_query.get("end") or [start])[0]).strip()
            cached = self.server.ai_analysis_by_window.get("%s|%s" % (start, end)) if start else None
            return self._json(cached or self.server.latest_ai_analysis or {"ok": False, "state": "empty"})
        if parsed.path == "/api/brief-feedback":
            return self._json({"items": self.server.store.brief_feedback()})
        if parsed.path == "/api/voice-transcript":
            message_id = str((parse_qs(parsed.query).get("message_id") or [""])[0]).strip()
            if not message_id:
                return self._json({"error": "missing_message_id", "message": "缺少 message_id"}, 400)
            return self._json({"item": self.server.store.voice_transcript(message_id)})
        if parsed.path == "/api/voice-audio":
            return self._voice_audio(parse_qs(parsed.query))
        if parsed.path == "/api/sync-status":
            return self._json(self.server.service.history_sync_status())
        if parsed.path == "/api/overview":
            return self._overview(parse_qs(parsed.query))
        if parsed.path == "/api/feed":
            return self._feed(parse_qs(parsed.query))
        if parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            chat = (query.get("chat") or [None])[0]
            limit = self._int_query(query, "limit", 200, cap=50_000)
            try:
                if any(key in query for key in ("start", "end", "period")):
                    start_at, end_at, start_day, end_day = _date_range(
                        query,
                        self.server.service.policy.timezone_name,
                    )
                    items = self._voice_enrich(self.server.store.messages_between(start_at, end_at, chat, limit))
                    return self._json(
                        {
                            "items": items,
                            "window": {
                                "start": start_day.isoformat(),
                                "end": end_day.isoformat(),
                                "timezone": self.server.service.policy.timezone_name,
                            },
                        }
                    )
            except ValueError as exc:
                return self._json({"error": "invalid_range", "message": str(exc)}, 400)
            return self._json({"items": self._voice_enrich(self.server.store.recent_messages(chat, limit))})
        if parsed.path == "/api/insights":
            query = parse_qs(parsed.query)
            try:
                start_at, end_at, start_day, end_day = _date_range(
                    query,
                    self.server.service.policy.timezone_name,
                )
                limit = self._int_query(query, "limit", 50_000, cap=200_000)
                chat = (query.get("chat") or [None])[0]
                messages = self.server.store.messages_between(start_at, end_at, chat, limit)
                value = analyze_messages(
                    messages,
                    start_at,
                    end_at,
                    self.server.service.policy.timezone_name,
                )
                value["window"]["start_date"] = start_day.isoformat()
                value["window"]["end_date"] = end_day.isoformat()
                return self._json(value)
            except ValueError as exc:
                return self._json({"error": "invalid_range", "message": str(exc)}, 400)
        if parsed.path == "/api/sync":
            return self._json({"error": "method_not_allowed", "message": "请使用 POST /api/sync"}, 405)
        if parsed.path == "/api/tasks":
            query = parse_qs(parsed.query)
            status = (query.get("status") or [None])[0]
            limit = self._int_query(query, "limit", 50)
            return self._json({"items": self.server.store.list_tasks(status, limit)})
        if parsed.path == "/api/chats":
            return self._chats(parse_qs(parsed.query))
        if parsed.path == "/api/contacts":
            return self._contacts(parse_qs(parsed.query))
        if parsed.path == "/api/media":
            return self._media(parse_qs(parsed.query))
        if parsed.path.startswith("/api/media/"):
            message_id = unquote(parsed.path[len("/api/media/"):])
            return self._media({"message_id": [message_id]})
        if parsed.path == "/api/sync-runs":
            return self._sync_runs(parse_qs(parsed.query))
        if parsed.path == "/api/rules":
            return self._json(
                {
                    "timezone": self.server.service.policy.timezone_name,
                    "items": [
                        _rule_json(rule)
                        for rule in self.server.service.policy.rules
                    ],
                }
            )
        if parsed.path == "/api/accounts":
            try:
                accounts = self.server.service.adapter.list_accounts()
                return self._json({"items": accounts})
            except Exception as exc:
                return self._json(
                    {"items": [], "error": str(exc)}, status=200
                )
        self._json({"error": "not_found", "message": "资源不存在"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._json({"error": "invalid_json", "message": str(exc)}, 400)
        if parsed.path == "/api/settings":
            try:
                value = self.server.settings.update(body)
            except (TypeError, ValueError, OSError) as exc:
                return self._json({"error": "invalid_settings", "message": str(exc)}, 400)
            return self._json({"ok": True, "settings": value})
        if parsed.path == "/api/report-render":
            return self._report_render(body)
        if parsed.path == "/api/report-email":
            return self._report_email(body)
        if parsed.path == "/api/brief-feedback":
            try:
                value = self.server.store.save_brief_feedback(
                    str(body.get("event_id") or ""),
                    str(body.get("action") or ""),
                    str(body.get("details") or "") or None,
                )
            except (TypeError, ValueError) as exc:
                return self._json({"error": "invalid_feedback", "message": str(exc)}, 400)
            return self._json({"ok": True, "feedback": value})
        if parsed.path == "/api/voice-transcribe":
            return self._voice_transcribe(body)
        if parsed.path == "/api/voice-correct":
            message_id = str(body.get("message_id") or "").strip()
            transcript = str(body.get("transcript") or "").strip()
            if not message_id or not transcript:
                return self._json({"error": "invalid_correction", "message": "缺少消息或校对文本"}, 400)
            try:
                value = self.server.store.save_voice_transcript(
                    message_id, status="corrected", transcript=transcript, manual=True
                )
            except ValueError as exc:
                return self._json({"error": "invalid_correction", "message": str(exc)}, 400)
            return self._json({"ok": True, "transcript": value})
        if parsed.path == "/api/auto-reply":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return self._json(
                    {"error": "invalid_enabled", "message": "enabled 必须是布尔值"},
                    400,
                )
            if enabled:
                self.server.service.resume()
            else:
                self.server.service.pause()
            return self._json(self._status())
        if parsed.path == "/api/sync":
            try:
                limit = int(body.get("limit", 100))
                result = self.server.service.sync_recent_history(limit)
                return self._json({"ok": True, **result})
            except (TypeError, ValueError) as exc:
                return self._json({"error": "invalid_limit", "message": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": "sync_failed", "message": str(exc)}, 502)
        if parsed.path == "/api/sync-range":
            try:
                query = {
                    "start": [str(body.get("start") or body.get("start_date") or "")],
                    "end": [str(body.get("end") or body.get("end_date") or "")],
                    "period": [str(body.get("period") or "")],
                }
                start_at, end_at, start_day, end_day = _date_range(
                    query,
                    self.server.service.policy.timezone_name,
                )
                result = self.server.service.start_history_sync(
                    start_at,
                    end_at,
                    scope=str(body.get("scope") or "all"),
                    limit=int(body.get("limit", 50_000)),
                )
                result["window"] = {
                    "start": start_day.isoformat(),
                    "end": end_day.isoformat(),
                    "timezone": self.server.service.policy.timezone_name,
                }
                return self._json({"ok": True, **result}, 202)
            except (TypeError, ValueError) as exc:
                return self._json({"error": "invalid_range", "message": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": "sync_failed", "message": str(exc)}, 502)
        if parsed.path == "/api/ai-analysis":
            return self._ai_analysis(body)
        if parsed.path == "/api/preview":
            return self._preview(body)
        if parsed.path == "/api/rules":
            return self._replace_rules(body)
        if parsed.path == "/api/retry":
            try:
                task_id = int(body.get("task_id"))
            except (TypeError, ValueError):
                return self._json(
                    {"error": "invalid_task_id", "message": "task_id 必须是整数"},
                    400,
                )
            if not self.server.service.retry_task(task_id):
                return self._json(
                    {"error": "retry_not_allowed", "message": "任务不存在、不是失败态或超出测试范围"},
                    409,
                )
            return self._json({"ok": True, "task_id": task_id})
        if parsed.path == "/api/send-text":
            return self._manual_send(body)
        self._json({"error": "not_found", "message": "资源不存在"}, status=404)

    def _voice_transcribe(self, body: Mapping[str, Any]) -> None:
        message_id = str(body.get("message_id") or "").strip()
        message = self.server.store.get_message(message_id) if message_id else None
        if not message or str(message.get("message_type") or "") != "voice":
            return self._json({"error": "voice_not_found", "message": "没有找到这条语音消息"}, 404)
        raw = message.get("raw_message") if isinstance(message.get("raw_message"), Mapping) else {}
        raw_message = raw.get("message") if isinstance(raw.get("message"), Mapping) else raw
        native_text = extract_wechat_voice_transcript(raw_message.get("_bridge_packed_info"))
        if native_text:
            value = self.server.store.save_voice_transcript(
                message_id,
                status="succeeded",
                transcript=native_text,
                provider="wechat_native",
                audio_path=message.get("media_path"),
            )
            return self._json({"ok": True, "transcript": value})
        voice_settings = self.server.settings.snapshot(include_secrets=True).get("voice") or {}
        if not voice_settings.get("enabled"):
            return self._json({"error": "voice_disabled", "message": "请先在设置中启用语音识别"}, 409)
        app_id = str(voice_settings.get("app_id") or "").strip()
        access_token = str(voice_settings.get("access_token") or "").strip()
        if not app_id or not access_token:
            return self._json({"error": "voice_not_configured", "message": "豆包 APP ID 或 Access Token 未配置"}, 409)
        cache_root = Path(
            str((self.server.settings.snapshot(include_secrets=True).get("media") or {}).get("cache_dir") or "")
        ) / "voice"
        cache_root.mkdir(parents=True, exist_ok=True)
        silk_path = str(message.get("media_path") or "").strip()
        try:
            if not silk_path or not Path(silk_path).is_file():
                local_id = raw_message.get("local_id")
                exporter = getattr(self.server.service.adapter, "export_voice", None)
                if local_id is None or not callable(exporter):
                    raise ValueError("当前消息缺少可定位的语音索引")
                silk_path = str(exporter(message.get("chat_id"), int(local_id), str(cache_root)) or "")
            if not silk_path or not Path(silk_path).is_file():
                raise ValueError("微信媒体库中没有找到对应语音数据")
            wav_path = cache_root / (hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:20] + ".wav")
            wav_bytes = decode_silk_to_wav(silk_path, wav_path)
            result = DoubaoASRClient(app_id=app_id, access_token=access_token).transcribe(
                wav_bytes, audio_format="wav", uid="wechat-bridge"
            )
            confidences = []
            for utterance in result.utterances:
                if isinstance(utterance, Mapping):
                    try:
                        confidences.append(float(utterance.get("confidence")))
                    except (TypeError, ValueError):
                        pass
            confidence = sum(confidences) / len(confidences) if confidences else None
            value = self.server.store.save_voice_transcript(
                message_id,
                status="succeeded",
                transcript=result.text,
                duration_ms=result.duration_ms,
                confidence=confidence,
                provider="doubao_asr_v2",
                audio_path=str(wav_path),
            )
            return self._json({"ok": True, "transcript": value})
        except (ASRError, OSError, TypeError, ValueError) as exc:
            value = self.server.store.save_voice_transcript(
                message_id,
                status="failed",
                provider="doubao_asr_v2",
                audio_path=silk_path or None,
                error=str(exc),
            )
            return self._json(
                {"error": "voice_transcription_failed", "message": "语音提取或识别失败", "transcript": value},
                502,
            )

    def _voice_audio(self, query: Mapping[str, Any]) -> None:
        message_id = str((query.get("message_id") or [""])[0]).strip()
        record = self.server.store.voice_transcript(message_id) if message_id else None
        path = Path(str((record or {}).get("audio_path") or "")).expanduser()
        cache_dir = Path(
            str((self.server.settings.snapshot(include_secrets=True).get("media") or {}).get("cache_dir") or "")
        ).expanduser()
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(cache_dir.resolve()):
                raise OSError
            data = resolved.read_bytes()
        except (OSError, ValueError):
            return self._json({"error": "voice_audio_unavailable", "message": "转码音频尚不可用"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before voice response completed")

    def _overview(self, query: Dict[str, Any]) -> None:
        try:
            timezone_name = self.server.service.policy.timezone_name
            messages, window, start_at, end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=False,
                limit=_WORKBENCH_ROW_LIMIT,
            )
        except ValueError as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        status = _runtime_snapshot(self.server.service)
        scope = _scope_payload(status, self.server.service)
        public_scope = {
            key: value for key, value in scope.items() if key != "live_values"
        }
        analysis = analyze_messages(
            self._analysis_enrich(messages),
            start_at,
            end_at,
            timezone_name,
            profile=(self.server.settings.snapshot().get("profile") or {}),
        )
        feedback_by_id = {
            item["event_id"]: item
            for item in self.server.store.brief_feedback(
                [str(event.get("id") or "") for event in analysis.get("event_briefs") or []]
            )
        }
        for event in analysis.get("event_briefs") or []:
            event["feedback"] = feedback_by_id.get(str(event.get("id") or ""))
        quality = _quality_with_scope(
            analysis.get("quality") or {},
            status,
            scope,
        )
        freshness = dict(analysis.get("freshness") or {})
        freshness.update(
            {
                "realtime": public_scope["realtime"],
                "history": public_scope["history"],
                "sync_state": public_scope["sync_state"],
            }
        )
        highlights = list(analysis.get("highlights") or [])
        actions = list(analysis.get("actions") or [])
        payload = {
                "ok": True,
                "window": window,
                "summary": analysis.get("summary") or {},
                "narrative": analysis.get("narrative") or "",
                "situation": dict(analysis.get("situation") or {}),
                "method": analysis.get("method") or {},
                "events": list(analysis.get("events") or []),
                "highlight_candidates": highlights,
                "pending_candidates": actions,
                "hourly": list(analysis.get("hourly") or []),
                "top_chats": list(analysis.get("top_chats") or []),
                "topics": list(analysis.get("topics") or []),
                "types": list(analysis.get("types") or []),
                "discoveries": list(analysis.get("discoveries") or []),
                "discussion_episodes": list(analysis.get("discussion_episodes") or []),
                "topic_briefs": list(analysis.get("topic_briefs") or []),
                "primary_insights": list(analysis.get("primary_insights") or []),
                 "event_briefs": list(analysis.get("event_briefs") or []),
                 "for_me": list(analysis.get("for_me") or []),
                 "trending": list(analysis.get("trending") or []),
                 "pending_review": list(analysis.get("pending_review") or []),
                 "unformed_dynamics": list(analysis.get("unformed_dynamics") or []),
                 "insight_breakdown": list(analysis.get("insight_breakdown") or []),
                "activity": dict(analysis.get("activity") or {}),
                # Keep the analysis names as aliases so the existing dashboard
                # can adopt the new contract incrementally.
                "highlights": highlights,
                "actions": actions,
                "quality": quality,
                "freshness": freshness,
                "scope": public_scope,
                "source": "local_sqlite",
                "read_only": True,
            }
        return self._json(payload)

    def _voice_enrich(self, items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        values = [dict(item) for item in items]
        for item in values:
            if str(item.get("message_type") or "") == "voice":
                transcript = self.server.store.voice_transcript(
                    str(item.get("message_id") or "")
                )
                raw = item.get("raw_message") if isinstance(item.get("raw_message"), Mapping) else {}
                raw_message = raw.get("message") if isinstance(raw.get("message"), Mapping) else raw
                native_text = extract_wechat_voice_transcript(raw_message.get("_bridge_packed_info"))
                if transcript is None and native_text:
                    transcript = {
                        "status": "available",
                        "transcript": native_text,
                        "provider": "wechat_native",
                    }
                item["voice_transcript"] = transcript
        return values

    def _analysis_enrich(self, items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Promote available voice transcripts to analyzable text on copied rows."""

        values = self._voice_enrich(items)
        for item in values:
            if str(item.get("message_type") or "").lower() != "voice":
                continue
            record = item.get("voice_transcript")
            transcript = (
                str(record.get("transcript") or "").strip()
                if isinstance(record, Mapping)
                else str(record or "").strip()
            )
            if not transcript:
                continue
            item["original_content"] = item.get("content")
            item["content"] = transcript
            item["_transcribed_voice"] = True
        return values

    def _feed(self, query: Dict[str, Any]) -> None:
        timezone_name = self.server.service.policy.timezone_name
        try:
            filter_name = _feed_filter_name((query.get("filter") or ["all"])[0])
            limit = self._int_query(query, "limit", 50, cap=500)
            messages, window, _start_at, _end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=True,
                limit=_WORKBENCH_ROW_LIMIT,
            )
            boundary: Optional[Tuple[datetime, int, Optional[str]]] = None
            cursor_value = (query.get("cursor") or [""])[0]
            if cursor_value:
                timestamp, row_id, message_id = _decode_feed_cursor(
                    cursor_value,
                    timezone_name,
                )
                boundary = (timestamp, row_id, message_id)
            else:
                before_value = (query.get("before") or [""])[0]
                if before_value:
                    timestamp, row_id = _decode_before(before_value, timezone_name)
                    boundary = (timestamp, row_id, None)
        except ValueError as exc:
            code = "invalid_filter" if "filter" in str(exc) else "invalid_cursor"
            if "日期" in str(exc) or "结束日期" in str(exc):
                code = "invalid_range"
            return self._json({"error": code, "message": str(exc)}, 400)

        messages = self._voice_enrich(messages)
        filtered = [
            item for item in messages if _feed_filter_matches(item, filter_name)
        ]
        filtered.sort(
            key=lambda item: (
                _message_timestamp(item.get("timestamp")),
                _row_id(item),
                str(item.get("message_id") or ""),
            ),
            reverse=True,
        )
        if boundary is not None:
            boundary_timestamp, boundary_row_id, boundary_message_id = boundary

            def before_boundary(item: Mapping[str, Any]) -> bool:
                timestamp = _message_timestamp(item.get("timestamp"))
                if timestamp < boundary_timestamp:
                    return True
                if timestamp > boundary_timestamp or boundary_message_id is None:
                    return False
                return (
                    _row_id(item),
                    str(item.get("message_id") or ""),
                ) < (boundary_row_id, boundary_message_id)

            filtered = [item for item in filtered if before_boundary(item)]

        page = filtered[: limit + 1]
        has_more = len(page) > limit
        page_items = page[:limit]
        next_cursor = _feed_cursor(page_items[-1]) if has_more and page_items else None
        payload = {
                "ok": True,
                "items": [_feed_item(item) for item in page_items],
                "window": window,
                "filter": filter_name,
                "sort": "timestamp_desc",
                "has_more": has_more,
                "next_cursor": next_cursor,
                "pagination": {
                    "limit": limit,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                },
                "read_only": True,
            }
        return self._json(payload)

    def _chats(self, query: Dict[str, Any]) -> None:
        try:
            timezone_name = self.server.service.policy.timezone_name
            messages, window, _start_at, _end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=True,
                limit=_WORKBENCH_ROW_LIMIT,
            )
        except ValueError as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        status = _runtime_snapshot(self.server.service)
        scope = _scope_payload(status, self.server.service)
        live_values = set(scope.get("live_values") or [])
        receiving = bool(status.get("receiving"))
        sync_state = scope.get("sync_state") or "unknown"
        grouped: Dict[str, Dict[str, Any]] = {}
        for item in messages:
            key = str(item.get("chat_id") or item.get("chat_name") or "unknown")
            row = grouped.setdefault(
                key,
                {
                    "chat_id": item.get("chat_id") or key,
                    "chat_name": _chat_display_name(item),
                    "messages": 0,
                    "inbound": 0,
                    "high_signal": 0,
                    "is_group": False,
                    "last_message": "",
                    "last_timestamp": None,
                    "last_message_id": None,
                    "last_sender_name": None,
                    "_last_key": (datetime.min.replace(tzinfo=timezone.utc), 0, ""),
                },
            )
            row["messages"] += 1
            if _is_self_message(item) is False:
                row["inbound"] += 1
                if _score_message(item).get("level") == "high":
                    row["high_signal"] += 1
            row["is_group"] = bool(row["is_group"] or item.get("is_group"))
            if row["chat_name"] in {"群聊", "未命名会话"}:
                row["chat_name"] = _chat_display_name(item)
            sort_key = (
                _message_timestamp(item.get("timestamp")),
                _row_id(item),
                str(item.get("message_id") or ""),
            )
            if sort_key > row["_last_key"]:
                row["_last_key"] = sort_key
                row["last_message"] = _content_preview(item)
                row["last_timestamp"] = item.get("timestamp")
                row["last_message_id"] = item.get("message_id")
                row["last_sender_name"] = _sender_display_name(item)

        items: List[Dict[str, Any]] = []
        for row in grouped.values():
            is_live = row["chat_id"] in live_values or row["chat_name"] in live_values
            row["is_live_monitored"] = is_live
            row["source_mode"] = "live" if is_live else "history"
            row["capture_state"] = _capture_state(
                is_live=is_live,
                receiving=receiving,
                sync_state=sync_state,
                has_messages=bool(row["messages"]),
            )
            row.pop("_last_key", None)
            items.append(row)
        items.sort(
            key=lambda value: (
                _message_timestamp(value.get("last_timestamp")),
                int(value.get("messages") or 0),
                str(value.get("chat_name") or ""),
            ),
            reverse=True,
        )
        public_scope = {
            key: value for key, value in scope.items() if key != "live_values"
        }
        payload = {
                "ok": True,
                "items": items,
                "window": window,
                "scope": public_scope,
                "sort": "last_timestamp_desc",
                "read_only": True,
            }
        return self._json(payload)

    def _contacts(self, query: Dict[str, Any]) -> None:
        try:
            timezone_name = self.server.service.policy.timezone_name
            messages, window, _start_at, _end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=True,
                limit=_WORKBENCH_ROW_LIMIT,
            )
        except ValueError as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        status = _runtime_snapshot(self.server.service)
        scope = _scope_payload(status, self.server.service)
        live_values = set(scope.get("live_values") or [])
        receiving = bool(status.get("receiving"))
        sync_state = scope.get("sync_state") or "unknown"
        grouped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for item in messages:
            if _is_self_message(item) is not False:
                continue
            key = _contact_key(item)
            row = grouped.setdefault(
                key,
                {
                    "contact_id": _contact_id(key),
                    "display_name": None,
                    "name_source": None,
                    "is_group": bool(item.get("is_group")),
                    "chat_ids": set(),
                    "chat_names": set(),
                    "message_count": 0,
                    "inbound_count": 0,
                    "last_message": "",
                    "last_timestamp": None,
                    "last_message_id": None,
                    "last_sender_name": None,
                    "_last_key": (datetime.min.replace(tzinfo=timezone.utc), 0, ""),
                    "_name_options": {},
                },
            )
            row["is_group"] = bool(row["is_group"] or item.get("is_group"))
            row["message_count"] += 1
            row["inbound_count"] += 1
            chat_id = str(item.get("chat_id") or "").strip()
            chat_name = _safe_name(item.get("chat_name"))
            if chat_id:
                row["chat_ids"].add(chat_id)
            if chat_name:
                row["chat_names"].add(chat_name)
            for name, source, priority in _contact_choice(item):
                option = row["_name_options"].setdefault(
                    name,
                    {"source": source, "priority": priority, "count": 0, "last": datetime.min.replace(tzinfo=timezone.utc)},
                )
                option["count"] += 1
                timestamp = _message_timestamp(item.get("timestamp"))
                if (priority, timestamp) > (option["priority"], option["last"]):
                    option["priority"] = priority
                    option["source"] = source
                    option["last"] = timestamp
            sort_key = (
                _message_timestamp(item.get("timestamp")),
                _row_id(item),
                str(item.get("message_id") or ""),
            )
            if sort_key > row["_last_key"]:
                row["_last_key"] = sort_key
                row["last_message"] = _content_preview(item)
                row["last_timestamp"] = item.get("timestamp")
                row["last_message_id"] = item.get("message_id")
                row["last_sender_name"] = _sender_display_name(item)

        items: List[Dict[str, Any]] = []
        for row in grouped.values():
            if row["_name_options"]:
                choice = max(
                    row["_name_options"].values(),
                    key=lambda value: (
                        value["priority"],
                        value["count"],
                        value["last"],
                    ),
                )
                row["display_name"] = next(
                    name
                    for name, value in row["_name_options"].items()
                    if value is choice
                )
                row["name_source"] = choice["source"]
            if not row["display_name"]:
                row["display_name"] = "群成员·待确认" if row["is_group"] else "联系人·待确认"
                row["name_source"] = "unresolved"
            is_live = bool(
                set(row["chat_ids"]).intersection(live_values)
                or set(row["chat_names"]).intersection(live_values)
            )
            row["is_live_monitored"] = is_live
            row["source_mode"] = "live" if is_live else "history"
            row["capture_state"] = _capture_state(
                is_live=is_live,
                receiving=receiving,
                sync_state=sync_state,
                has_messages=True,
            )
            row["chat_ids"] = sorted(row["chat_ids"])
            row["chat_names"] = sorted(row["chat_names"])
            row.pop("_last_key", None)
            row.pop("_name_options", None)
            items.append(row)
        items.sort(
            key=lambda value: (
                _message_timestamp(value.get("last_timestamp")),
                str(value.get("display_name") or ""),
            ),
            reverse=True,
        )
        limit = self._int_query(query, "limit", 500, cap=2_000)
        public_scope = {
            key: value for key, value in scope.items() if key != "live_values"
        }
        payload = {
                "ok": True,
                "items": items[:limit],
                "total": len(items),
                "window": window,
                "scope": public_scope,
                "read_only": True,
            }
        return self._json(payload)

    def _sync_runs(self, query: Dict[str, Any]) -> None:
        limit = self._int_query(query, "limit", 50, cap=200)
        current = {}
        try:
            current = dict(self.server.service.history_sync_status())
        except Exception as exc:
            current = {"state": "unknown", "error": str(exc)}

        result: Any = None
        provider = None
        errors: List[str] = []
        for owner_name, owner in (("store", self.server.store), ("service", self.server.service)):
            for method_name in ("recent_sync_runs", "list_sync_runs", "get_sync_runs", "sync_runs"):
                method = getattr(owner, method_name, None)
                if not callable(method):
                    continue
                try:
                    try:
                        result = method(limit)
                    except TypeError:
                        result = method()
                    provider = "%s.%s" % (owner_name, method_name)
                    break
                except Exception as exc:
                    errors.append("%s: %s" % (method_name, exc))
            if provider:
                break

        if provider is None:
            return self._json(
                {
                    "ok": True,
                    "items": [],
                    "available": False,
                    "state": "not_persisted",
                    "message": "底层尚未提供持久化 sync_runs；以下仅返回当前进程同步状态",
                    "current": current,
                    "errors": errors,
                    "read_only": True,
                }
            )

        if isinstance(result, dict):
            items = result.get("items") or result.get("runs") or []
        elif isinstance(result, (list, tuple)):
            items = result
        else:
            items = []
        if not isinstance(items, list):
            items = list(items) if isinstance(items, tuple) else []
        return self._json(
            {
                "ok": True,
                "items": items[:limit],
                "available": True,
                "state": "available",
                "provider": provider,
                "current": current,
                "errors": errors,
                "read_only": True,
            }
        )

    def _status(self) -> Dict[str, Any]:
        value = self.server.service.status_snapshot()
        value["counts"] = self.server.store.counts()
        value["server_time"] = datetime.now(timezone.utc).isoformat()
        return value

    def _ai_generator(self) -> OpenAIAnalysisGenerator:
        settings = self.server.settings.snapshot(include_secrets=True).get("ai") or {}
        return OpenAIAnalysisGenerator(
            model=str(
                settings.get("model")
                or os.environ.get("OPENAI_WECHAT_ANALYSIS_MODEL", "gpt-5.2")
            ),
            api_key=str(settings.get("api_key") or "") or None,
            base_url=str(settings.get("base_url") or "") or None,
            max_findings=20,
        )

    def _ai_status(self) -> Dict[str, Any]:
        generator = self._ai_generator()
        public_ai = (self.server.settings.public().get("ai") or {})
        return {
            "provider": "openai",
            "model": generator.model,
            "configured": generator.configured,
            "base_url": generator.base_url or "OpenAI 默认接口",
            "api_key_configured": bool(public_ai.get("api_key_configured") or generator.api_key),
            "mode": "auto_with_manual_refresh" if generator.configured else "manual_only",
            "send_enabled": bool(self.server.service.send_enabled),
            "privacy": "redacted_candidates_only",
            "message": (
                "已配置；日报更新后自动分析，也可手动刷新"
                if generator.configured
                else "未配置 OPENAI_API_KEY；当前仅使用本地规则分析"
            ),
        }

    def _media(self, query: Dict[str, Any]) -> None:
        message_id = str((query.get("message_id") or [""])[0] or "").strip()
        if not message_id:
            return self._json({"error": "missing_message_id", "message": "缺少 message_id"}, 400)
        item = self.server.store.get_message(message_id)
        if item is None:
            return self._json({"error": "media_not_found", "message": "消息不存在"}, 404)
        if str(item.get("message_type") or "").lower() != "image":
            return self._json({"error": "media_not_image", "message": "当前消息不是图片"}, 409)
        raw_path = str(item.get("media_path") or "").strip()
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return self._json(
                {
                    "error": "media_unavailable",
                    "state": "path_only",
                    "message": "图片路径已记录，但具体文件当前不可读",
                    "media_path": raw_path,
                },
                409,
            )
        try:
            resolved = path.resolve()
        except OSError:
            return self._json({"error": "media_unavailable", "message": "图片路径无法解析"}, 409)

        settings = self.server.settings.snapshot(include_secrets=True)
        media_settings = settings.get("media") or {}
        allowed_roots = []
        configured_cache_dir = str(media_settings.get("cache_dir") or "").strip()
        if configured_cache_dir:
            allowed_roots.append(Path(configured_cache_dir).expanduser())
        adapter_database = getattr(self.server.service.adapter, "database", None)
        account_dir = str(getattr(adapter_database, "account_dir", "") or "")
        if account_dir:
            allowed_roots.append(Path(account_dir).expanduser())
        adapter_status = (self._status().get("adapter") or {}).get("details") or {}
        db_dir = str(adapter_status.get("db_dir") or "")
        if db_dir:
            allowed_roots.append(Path(db_dir).expanduser().parent)
        try:
            allowed = any(resolved.is_relative_to(root.resolve()) for root in allowed_roots if str(root))
        except (AttributeError, OSError, ValueError):
            allowed = False
        if not allowed:
            return self._json({"error": "media_forbidden", "message": "媒体不在允许的本地目录内"}, 403)

        try:
            aes_key = str(media_settings.get("image_aes_key") or "") or runtime_image_key()
            if not aes_key:
                try:
                    if resolved.read_bytes()[:6].startswith(V2_MAGIC):
                        request_image_key_discovery(str(resolved))
                except (OSError, ValueError):
                    aes_key = ""
            data, extension, content_type = read_media(
                str(resolved),
                aes_key=aes_key,
                xor_key=media_settings.get("image_xor_key"),
            )
        except (MediaUnavailable, OSError, ValueError) as exc:
            return self._json(
                {
                    "error": "media_unavailable",
                    "state": "path_only",
                    "message": str(exc),
                    "media_path": str(resolved),
                },
                409,
            )

        # Persist only decoded copies under the user-selected cache directory.
        # The response still works if the cache directory cannot be created.
        cache_dir = Path(str(media_settings.get("cache_dir") or "")).expanduser()
        if str(cache_dir):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached = cache_dir / cache_key(str(resolved), extension)
                if not cached.exists():
                    cached.write_bytes(data)
            except OSError:
                logger.debug("unable to persist decoded media cache", exc_info=True)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before media response completed")

    def _ai_context_payload(
        self,
        baseline: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Build aggregate context without leaking private message IDs."""

        source_to_ref = {
            str(item.get("_source_message_id")): str(item.get("evidence_ref"))
            for item in candidates
            if item.get("_source_message_id") and item.get("evidence_ref")
        }

        def refs(values: Any) -> List[str]:
            if isinstance(values, str):
                values = [values]
            output = []
            for value in values or []:
                ref = source_to_ref.get(str(value))
                if ref and ref not in output:
                    output.append(ref)
            return output[:8]

        def safe(value: Any, limit: int = 240) -> str:
            return _redact_ai_content(str(value or "").strip())[:limit]

        topic_context = []
        for item in list(baseline.get("topic_briefs") or [])[:10]:
            evidence = item.get("evidence") or []
            examples = []
            for entry in evidence[:3]:
                if not isinstance(entry, Mapping):
                    continue
                ref = source_to_ref.get(str(entry.get("message_id") or ""))
                if not ref:
                    continue
                examples.append(
                    {
                        "evidence_ref": ref,
                        "sender_alias": safe(entry.get("sender_name"), 80),
                        "content": safe(entry.get("content"), 220),
                    }
                )
            topic_context.append(
                {
                    "topic": safe(item.get("topic"), 80),
                    "message_count": int(item.get("message_count") or 0),
                    "chat_count": int(item.get("chat_count") or 0),
                    "resource_count": int(item.get("resource_count") or 0),
                    "high_information_count": int(item.get("high_information_count") or 0),
                    "average_score": int(item.get("average_score") or 0),
                    "summary": safe(item.get("summary"), 220),
                    "why_it_matters": safe(item.get("why_it_matters"), 220),
                    "examples": examples,
                    "evidence_refs": refs(
                        entry.get("message_id")
                        for entry in evidence
                        if isinstance(entry, Mapping)
                    ),
                }
            )

        discussion_context = []
        for item in list(baseline.get("discussion_episodes") or [])[:10]:
            discussion_context.append(
                {
                    "chat_alias": safe(item.get("chat_name"), 100),
                    "start": safe(item.get("start"), 40),
                    "end": safe(item.get("end"), 40),
                    "message_count": int(item.get("message_count") or 0),
                    "participant_count": int(item.get("participant_count") or 0),
                    "substantive_count": int(item.get("substantive_count") or 0),
                    "resource_count": int(item.get("resource_count") or 0),
                    "topics": [
                        safe(topic.get("topic"), 80)
                        for topic in list(item.get("topics") or [])[:6]
                        if isinstance(topic, Mapping)
                    ],
                    "summary": safe(item.get("summary"), 260),
                    "examples": [
                        {
                            "evidence_ref": source_to_ref.get(str(entry.get("message_id") or "")),
                            "sender_alias": safe(entry.get("sender_name"), 80),
                            "content": safe(entry.get("content"), 220),
                        }
                        for entry in list(item.get("evidence_samples") or [])[:3]
                        if isinstance(entry, Mapping)
                        and source_to_ref.get(str(entry.get("message_id") or ""))
                    ],
                    "evidence_refs": refs(item.get("evidence")),
                }
            )

        event_context = []
        for item in list(baseline.get("event_briefs") or [])[:20]:
            event_context.append(
                {
                    "title": safe(item.get("title"), 120),
                    "lane": safe(item.get("lane"), 30),
                    "start": safe(item.get("start"), 40),
                    "end": safe(item.get("end"), 40),
                    "related_chat_count": int(item.get("related_chat_count") or 0),
                    "related_people_count": int(item.get("related_people_count") or 0),
                    "importance": int(item.get("importance") or 0),
                    "summary": safe(item.get("summary"), 260),
                    "evidence_refs": refs(
                        entry.get("message_id")
                        for entry in list(item.get("evidence") or [])
                        if isinstance(entry, Mapping)
                    ),
                }
            )

        unformed_context = []
        for item in list(baseline.get("unformed_dynamics") or [])[:60]:
            unformed_context.append(
                {
                    "kind": safe(item.get("kind"), 30),
                    "summary": safe(item.get("summary"), 280),
                    "message_count": int(item.get("message_count") or 0),
                    "start": safe(item.get("start"), 40),
                    "end": safe(item.get("end"), 40),
                    "people": [safe(value, 80) for value in list(item.get("people") or [])[:8]],
                    "chats": [
                        safe(entry.get("chat_name"), 100)
                        for entry in list(item.get("chats") or [])[:4]
                        if isinstance(entry, Mapping)
                    ],
                    "evidence_refs": refs(item.get("message_ids")),
                }
            )

        return {
            "situation": {
                "headline": safe((baseline.get("situation") or {}).get("headline"), 220),
                "points": [
                    safe(point, 220)
                    for point in list((baseline.get("situation") or {}).get("points") or [])[:4]
                ],
            },
            "summary": dict(baseline.get("summary") or {}),
            "insight_breakdown": list(baseline.get("insight_breakdown") or [])[:8],
            "topic_briefs": topic_context,
            "discussion_windows": discussion_context,
            "event_candidates": event_context,
            "unformed_dynamics": unformed_context,
        }

    def _ai_analysis(self, body: Dict[str, Any]) -> None:
        if body.get("confirm") is not True:
            return self._json(
                {
                    "error": "confirmation_required",
                    "message": "AI 分析会把脱敏候选文本发送到配置的 AI 服务，必须显式 confirm=true",
                },
                400,
            )
        try:
            query = {
                "start": [str(body.get("start") or body.get("start_date") or "")],
                "end": [str(body.get("end") or body.get("end_date") or "")],
                "period": [str(body.get("period") or "")],
            }
            start_at, end_at, start_day, end_day = _date_range(
                query,
                self.server.service.policy.timezone_name,
            )
            candidate_limit = max(20, min(int(body.get("limit", 120)), 200))
        except (TypeError, ValueError) as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        messages = self._analysis_enrich(self.server.store.messages_between(
            start_at,
            end_at,
            (body.get("chat") or None),
            200_000,
        ))
        baseline = analyze_messages(
            messages,
            start_at,
            end_at,
            self.server.service.policy.timezone_name,
        )
        priority_message_ids = [
            str(entry.get("message_id") or "")
            for event in list(baseline.get("event_briefs") or [])[:40]
            for entry in list(event.get("evidence") or [])[:3]
            if isinstance(entry, Mapping) and entry.get("message_id")
        ]
        priority_message_ids.extend(
            str(message_id)
            for matter in list(baseline.get("unformed_dynamics") or [])[:80]
            for message_id in list(matter.get("message_ids") or [])[:6]
            if message_id
        )
        candidates = build_ai_context(messages, candidate_limit, priority_message_ids)
        generator = self._ai_generator()
        window = {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "timezone": self.server.service.policy.timezone_name,
        }
        ai_context = self._ai_context_payload(baseline, candidates)
        window_key = "%s|%s" % (start_day.isoformat(), end_day.isoformat())
        try:
            with self.server.ai_analysis_lock:
                value = None if body.get("force") is True else self.server.ai_raw_by_window.get(window_key)
                if value is None:
                    try:
                        value = generator.analyze(window, candidates, ai_context)
                    except TypeError as exc:
                        # Keep test doubles and third-party compatible generators that
                        # still implement the original two-argument contract usable.
                        if "positional" not in str(exc) and "argument" not in str(exc):
                            raise
                        value = generator.analyze(window, candidates)
                    self.server.ai_raw_by_window[window_key] = value
        except AnalysisGenerationError as exc:
            status = 409 if not generator.configured else 502
            return self._json(
                {
                    "error": "ai_not_configured" if not generator.configured else "ai_analysis_failed",
                    "message": str(exc),
                    "source": "rules_fallback",
                    "window": window,
                    "candidate_count": len(candidates),
                    "rule_baseline": {
                        "narrative": baseline.get("narrative"),
                        "summary": baseline.get("summary"),
                    },
                },
                status,
            )

        candidate_by_ref = {
            str(item.get("evidence_ref")): item for item in candidates
        }
        candidate_by_source = {
            str(item.get("_source_message_id")): str(item.get("evidence_ref"))
            for item in candidates
            if item.get("_source_message_id") and item.get("evidence_ref")
        }
        findings = []
        for item in value.get("findings", []):
            if not isinstance(item, dict):
                continue
            raw_refs = item.get("ref_ids", [])
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            refs = [
                str(ref)
                for ref in raw_refs
                if str(ref) in candidate_by_ref
            ]
            if not refs:
                # No local evidence means the model made an unsupported claim.
                continue
            evidence = []
            for ref in refs:
                source = candidate_by_ref[ref]
                evidence.append(
                    {
                        "evidence_ref": ref,
                        "message_id": source.get("_source_message_id"),
                        "chat_name": source.get("chat_name"),
                        "sender_name": source.get("sender_name"),
                        "timestamp": source.get("timestamp"),
                        "content": source.get("content"),
                    }
                )
            finding = {
                key: item.get(key)
                for key in (
                    "title", "category", "value_type", "importance", "confidence",
                    "summary", "narrative", "core_conclusion", "keywords", "what_changed", "why_it_matters", "reason",
                    "uncertainty", "next_step",
                )
            }
            title_context = " ".join(
                str(finding.get(key) or "")
                for key in ("summary", "narrative", "what_changed", "why_it_matters", "core_conclusion", "keywords")
            ) + " " + " ".join(str(entry.get("content") or "") for entry in evidence)
            raw_title = str(finding.get("title") or "").strip()
            finding["title"] = raw_title if is_editorial_title(raw_title) else normalize_editorial_title(raw_title, title_context)
            keywords = finding.get("keywords") or []
            if isinstance(keywords, str):
                keywords = re.split(r"[,，、;；|\n]+", keywords)
            if not isinstance(keywords, list):
                keywords = []
            finding["keywords"] = [str(keyword).strip() for keyword in keywords if str(keyword).strip()][:6]
            try:
                importance = int(finding.get("importance") or 0)
            except (TypeError, ValueError):
                importance = 0
            try:
                confidence = int(finding.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0
            # Some OpenAI-compatible models honor the JSON shape but omit
            # numeric judgments.  Keep evidence as the authority and assign a
            # conservative local score instead of silently discarding a fully
            # grounded synthesis.
            if importance <= 0 and finding.get("why_it_matters") and finding.get("summary"):
                importance = 62 if len(refs) >= 2 else 55
            if confidence <= 0:
                confidence = 68 if len(refs) >= 2 else 56
            finding["importance"] = max(0, min(100, importance))
            finding["confidence"] = max(0, min(100, confidence))
            finding["evidence_refs"] = refs
            finding["evidence"] = evidence
            findings.append(finding)

        # A model may validly return no strict finding for a busy discussion:
        # it does not mean the window is empty.  Keep the read-only result
        # useful by promoting already-evidenced local discoveries into a
        # clearly labelled fallback lane.  This never invents a conclusion or
        # creates a reply task.
        local_fallback = False
        if not findings:
            source_by_message = {
                str(item.get("_source_message_id") or ""): item
                for item in candidates
                if item.get("_source_message_id")
            }
            # AI fallback keeps the broader topic layer for compatibility and
            # exploration; the overview itself remains event-led.
            fallback_items = list(baseline.get("topic_briefs") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("primary_insights") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("discoveries") or [])
            if not fallback_items:
                # A strict local event is still useful evidence when the AI
                # elects not to form an independent finding. Keep the queues
                # separate in the UI, but do not let a valid event make the AI
                # result look empty.
                fallback_items = list(baseline.get("actions") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("events") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("highlights") or [])
            if not fallback_items:
                fallback_items = [
                    {
                        "message_id": item.get("_source_message_id"),
                        "chat_name": item.get("chat_name"),
                        "sender_name": item.get("sender_name"),
                        "content": item.get("content"),
                        "kind": item.get("candidate_type"),
                        "score": item.get("rule_level") == "high" and 80 or 45,
                    }
                    for item in candidates
                    if str(item.get("candidate_state") or "")
                    in {"informative", "reviewable", "context_needed"}
                ]
            category_labels = {
                "theme": "主题",
                "event": "事件",
                "resource": "资源",
                "progress": "进展",
                "knowledge": "知识",
                "discussion": "讨论",
            }
            for local_item in fallback_items:
                evidence_values = local_item.get("evidence") or []
                if isinstance(evidence_values, (str, Mapping)):
                    evidence_values = [evidence_values]
                evidence_ids = [
                    str(value.get("message_id") or "")
                    if isinstance(value, Mapping)
                    else str(value or "")
                    for value in evidence_values
                ]
                source_id = str(
                    local_item.get("message_id")
                    or (evidence_ids[0] if evidence_ids else "")
                )
                source = source_by_message.get(source_id)
                if source is None:
                    source = next(
                        (
                            candidate
                            for candidate in candidates
                            if str(candidate.get("_source_message_id") or "") == source_id
                        ),
                        None,
                    )
                if source is None:
                    continue
                ref = str(source.get("evidence_ref") or "")
                evidence = {
                    "evidence_ref": ref,
                    "message_id": source.get("_source_message_id"),
                    "chat_name": source.get("chat_name"),
                    "sender_name": source.get("sender_name"),
                    "timestamp": source.get("timestamp"),
                    "content": source.get("content"),
                }
                kind = str(local_item.get("kind") or source.get("candidate_type") or "discussion")
                findings.append(
                    {
                        "title": normalize_editorial_title(
                            str(local_item.get("title") or ""),
                            " ".join(
                                str(local_item.get(key) or "")
                                for key in ("summary", "content", "why_it_matters", "reason", "kind")
                            ),
                        ),
                        "category": category_labels.get(kind, "讨论"),
                        "value_type": kind,
                        "importance": max(30, min(90, int(local_item.get("score") or 45))),
                        "confidence": 55,
                        "summary": str(local_item.get("summary") or local_item.get("content") or source.get("content") or "").strip(),
                        "narrative": str(local_item.get("summary") or local_item.get("content") or source.get("content") or "").strip(),
                        "core_conclusion": str(local_item.get("why_it_matters") or local_item.get("reason") or "仍需结合上下文判断其实际影响。"),
                        "keywords": [str(local_item.get("kind") or "讨论")],
                        "what_changed": str(local_item.get("what_changed") or local_item.get("summary") or local_item.get("content") or source.get("content") or "").strip(),
                        "why_it_matters": str(local_item.get("why_it_matters") or local_item.get("reason") or "这条内容已通过本地规则保留，但仍需要结合邻近消息判断影响。"),
                        "reason": "模型没有返回独立 finding；此条来自本地已保留的可回看证据。",
                        "uncertainty": str(local_item.get("uncertainty") or "本地保底没有替代模型完成跨消息归纳。"),
                        "next_step": "回到原消息查看上下文，判断是否值得沉淀或继续跟进",
                        "evidence_refs": [ref],
                        "evidence": [evidence],
                    }
                )
                local_fallback = True
                if len(findings) >= 8:
                    break

        brief = str(value.get("brief") or "").strip()
        themes = [str(item) for item in value.get("themes", [])[:12]] if isinstance(value.get("themes"), list) else []
        limitations = [str(item) for item in value.get("limitations", [])[:12]] if isinstance(value.get("limitations"), list) else []
        local_situation = baseline.get("situation") or {}
        situation = str(value.get("situation") or "").strip()
        key_changes = [str(item) for item in value.get("key_changes", [])[:8]] if isinstance(value.get("key_changes"), list) else []
        open_questions = [str(item) for item in value.get("open_questions", [])[:8]] if isinstance(value.get("open_questions"), list) else []
        if not situation:
            situation = str(
                local_situation.get("headline")
                or baseline.get("narrative")
                or "当前窗口存在可回看的讨论内容。"
            )
        if not key_changes:
            key_changes = [str(item) for item in list(local_situation.get("points") or [])[:6]]
        timeline = []
        for item in value.get("timeline", []) if isinstance(value.get("timeline"), list) else []:
            if not isinstance(item, dict):
                continue
            raw_refs = item.get("ref_ids", [])
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            valid_refs = [
                str(ref) for ref in raw_refs
                if str(ref) in candidate_by_ref
            ]
            if not valid_refs:
                continue
            timeline.append(
                {
                    "time": str(item.get("time") or ""),
                    "title": str(item.get("title") or "时间节点"),
                    "summary": str(item.get("summary") or ""),
                    "evidence_refs": valid_refs,
                }
            )
        if not timeline:
            for item in list(baseline.get("events") or [])[:6]:
                valid_refs = [candidate_by_source.get(str(ref)) for ref in item.get("evidence", [])]
                valid_refs = [ref for ref in valid_refs if ref]
                if not valid_refs:
                    continue
                timeline.append(
                    {
                        "time": str(item.get("start") or ""),
                        "title": "事件候选 · %s" % str(item.get("chat_name") or "会话"),
                        "summary": str(item.get("summary") or ""),
                        "evidence_refs": valid_refs[:8],
                    }
                )
        if not brief:
            brief = str(
                baseline.get("narrative")
                or "当前窗口存在可回看的讨论内容，但模型没有形成独立摘要。"
            )
        if not themes:
            themes = [str(item.get("topic")) for item in (baseline.get("topics") or [])[:8]]
        if not themes:
            themes = [
                str(topic.get("topic"))
                for episode in (baseline.get("discussion_episodes") or [])
                for topic in (episode.get("topics") or [])
                if topic.get("topic")
            ][:8]
        if local_fallback:
            limitations.append("模型未返回可核对的独立结论，已展示本地讨论洞察作为保底产出。")

        def polish_editorial_text(value: Any) -> str:
            text = str(value or "").strip()
            text = re.sub(r"对于[^，。]{0,36}(?:用户|人)来说[，,]?", "", text)
            text = text.replace("值得注意的是", "").replace("综上所述", "")
            text = text.replace("具有重要意义", "已经产生实际影响")
            text = text.replace("这是一种值得留意的风向", "更清楚的变化")
            text = text.replace("用户需要评估", "后续重点是判断")
            text = text.replace("建议用户关注", "后续重点是")
            text = text.replace("需要进一步关注", "仍需继续核对")
            text = re.sub(r"(?:用户)?需关注", "关键在于", text)
            return re.sub(r"\s+", " ", text).strip()

        flat_importance = len({int(item.get("importance") or 0) for item in findings}) <= 1
        for finding in findings:
            for key in ("summary", "narrative", "core_conclusion", "what_changed", "why_it_matters", "uncertainty", "next_step"):
                finding[key] = polish_editorial_text(finding.get(key))
            evidence_items = list(finding.get("evidence") or [])
            category = str(finding.get("category") or "").lower()
            evidence_chats = {str(item.get("chat_name") or "") for item in evidence_items if item.get("chat_name")}
            personal_evidence = sum(
                1
                for item in evidence_items
                if (source := candidate_by_ref.get(str(item.get("evidence_ref") or "")))
                and not bool(source.get("is_group"))
            )
            editorial_score = 48
            editorial_score += min(15, len(evidence_items) * 3)
            editorial_score += {"risk": 10, "event": 8, "progress": 7, "knowledge": 5, "theme": 4, "resource": 2, "question": 1}.get(category, 3)
            editorial_score += 6 if len(evidence_chats) >= 2 else 0
            editorial_score += min(10, personal_evidence * 5)
            editorial_score += 4 if len(str(finding.get("narrative") or "")) >= 150 else 0
            editorial_score += 4 if re.search(r"决定|确认|截止|风险|成本|安排|变化|影响", " ".join(str(finding.get(key) or "") for key in ("narrative", "core_conclusion", "what_changed"))) else 0
            current_score = int(finding.get("importance") or 0)
            finding["importance"] = max(0, min(100, editorial_score if flat_importance or current_score < 50 or current_score in {55, 62} else round(current_score * .65 + editorial_score * .35)))
        findings.sort(key=lambda item: (int(item.get("importance") or 0), int(item.get("confidence") or 0), len(item.get("evidence") or [])), reverse=True)

        payload = {
                "ok": True,
                "source": "ai_assisted_with_local_fallback" if local_fallback else "ai_assisted",
                "provider": "openai",
                "model": generator.model,
                "window": window,
                "candidate_count": len(candidates),
                "context_count": sum(
                    len(ai_context.get(key) or [])
                    for key in ("topic_briefs", "discussion_windows", "event_candidates")
                ),
                "rule_baseline": {
                    "narrative": baseline.get("narrative"),
                    "summary": baseline.get("summary"),
                },
                "analysis": {
                    "brief": brief,
                    "situation": situation,
                    "key_changes": key_changes,
                    "themes": themes,
                    "open_questions": open_questions,
                    "timeline": timeline,
                    "limitations": limitations,
                    "findings": findings,
                    "discoveries": baseline.get("discoveries") or [],
                    "discussion_episodes": baseline.get("discussion_episodes") or [],
                    "topic_briefs": baseline.get("topic_briefs") or [],
                    "primary_insights": baseline.get("primary_insights") or [],
                    "activity": baseline.get("activity") or {},
                },
                "will_send": False,
                "creates_reply_tasks": False,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        self.server.latest_ai_analysis = payload
        self.server.ai_analysis_by_window[window_key] = payload
        return self._json(payload)

    def _preview(self, body: Dict[str, Any]) -> None:
        content = str(body.get("content") or "").strip()
        if not content:
            return self._json(
                {"error": "empty_content", "message": "请输入要预览的消息"},
                400,
            )
        chat_name = str(body.get("chat_name") or "文件传输助手")
        chat_id = str(body.get("chat_id") or "filehelper")
        message = IncomingMessage(
            message_id="preview:%s" % uuid4().hex,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=str(body.get("sender_id") or "preview-user"),
            sender_name=str(body.get("sender_name") or "预览用户"),
            message_type=str(body.get("message_type") or "text"),
            content=content,
            timestamp=datetime.now(timezone.utc),
            is_self=False,
            raw_message={"preview": True},
            adapter_name=self.server.service.adapter.name,
            adapter_version=self.server.service.adapter.version,
        )
        decision = self.server.service.policy.decide(message)
        if not decision.should_reply:
            return self._json(
                {
                    "ok": False,
                    "reason": decision.reason,
                    "message": "当前安全策略不会回复这条消息",
                },
                200,
            )
        try:
            reply_text = decision.reply_text
            if reply_text is None:
                reply_text = self.server.service.policy.generate_reply(
                    message,
                    self.server.store.recent_messages(
                        chat_id=chat_id,
                        limit=self.server.service.policy.context_limit,
                    ),
                )
            return self._json(
                {
                    "ok": True,
                    "reason": decision.reason,
                    "reply_text": reply_text,
                    "will_send": False,
                    "scope": chat_name,
                }
            )
        except Exception as exc:
            return self._json(
                {"ok": False, "reason": "ai_generation_failed", "message": str(exc)},
                200,
            )

    def _replace_rules(self, body: Dict[str, Any]) -> None:
        values = body.get("rules")
        if not isinstance(values, list):
            return self._json(
                {"error": "invalid_rules", "message": "rules 必须是数组"},
                400,
            )
        try:
            rules = tuple(
                ReplyRule.from_dict(item, index)
                for index, item in enumerate(values)
                if isinstance(item, dict)
            )
        except (TypeError, ValueError, re.error) as exc:
            return self._json(
                {"error": "invalid_rules", "message": str(exc)},
                400,
            )
        self.server.service.policy.rules = rules
        return self._json({"ok": True, "items": [_rule_json(rule) for rule in rules]})

    def _manual_send(self, body: Dict[str, Any]) -> None:
        if not self.server.service.send_enabled:
            return self._json(
                {
                    "error": "sending_disabled",
                    "message": "当前处于只接收/分析模式，发送功能已由操作员锁定",
                },
                403,
            )
        if body.get("confirm") is not True:
            return self._json(
                {"error": "confirmation_required", "message": "真实发送必须显式 confirm=true"},
                400,
            )
        if self.server.service.dry_run:
            return self._json(
                {"error": "dry_run_enabled", "message": "当前为演练模式，不会发送微信消息"},
                409,
            )
        if self.server.service.is_paused:
            return self._json(
                {"error": "auto_reply_paused", "message": "自动回复已暂停"},
                409,
            )
        chat_id = str(body.get("chat_id") or "filehelper")
        chat_name = str(body.get("chat_name") or "文件传输助手")
        content = str(body.get("content") or "").strip()
        if self.server.service.filehelper_only and chat_id != "filehelper":
            return self._json(
                {"error": "filehelper_only_test_scope", "message": "第一阶段只允许文件传输助手"},
                403,
            )
        if not content:
            return self._json(
                {"error": "empty_content", "message": "发送内容不能为空"},
                400,
            )
        health = self.server.service.adapter.health_check()
        if not health.ok:
            return self._json(
                {"error": "adapter_unavailable", "message": health.message},
                503,
            )
        try:
            result = self.server.service.adapter.send_text(chat_id, chat_name, content)
        except Exception as exc:
            return self._json({"error": "send_failed", "message": str(exc)}, 502)
        if not result.accepted:
            return self._json(
                {
                    "ok": False,
                    "accepted": False,
                    "confirmed": result.confirmed,
                    "confirmation": result.confirmation,
                    "error": result.error,
                },
                502,
            )
        return self._json(
            {
                "ok": True,
                "accepted": True,
                "confirmed": result.confirmed,
                "confirmation": result.confirmation,
                "sent_message_id": result.sent_message_id,
                "warning": "这是人工控制接口发送，结果已返回但不会创建自动回复任务",
            }
        )

    def _serve_asset(self, name: str, content_type: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if WEB_ROOT.resolve() not in path.parents:
            return self._json({"error": "forbidden"}, 403)
        try:
            data = path.read_bytes()
        except OSError:
            return self._json({"error": "asset_not_found"}, 404)
        if name == "index.html" and b"WeChat Bridge" not in data:
            # Keep the legacy application identifier available to existing
            # local clients/tests without changing the visible frontend.
            data = data.replace(
                b"    <title>",
                b'    <meta name="application-name" content="WeChat Bridge" />\n    <title>',
                1,
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before asset response completed")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _report_document(body: Mapping[str, Any]) -> Tuple[str, str]:
        html = str(body.get("html") or "")
        if "<html" not in html.lower() or "<body" not in html.lower():
            raise ValueError("日报 HTML 不完整")
        if len(html.encode("utf-8")) > 6_000_000:
            raise ValueError("日报 HTML 超过大小限制")
        raw_name = str(body.get("filename") or "wechat-daily").strip()
        filename = re.sub(r"[^0-9A-Za-z._-]+", "-", raw_name).strip("-.")[:100] or "wechat-daily"
        return html, filename

    def _report_render(self, body: Mapping[str, Any]) -> None:
        try:
            html, filename = self._report_document(body)
            output_format = str(body.get("format") or "html").strip().lower()
            if output_format == "html":
                return self._binary(html.encode("utf-8"), "text/html; charset=utf-8", filename + ".html")
            if output_format == "pdf":
                return self._binary(_render_report_pdf(html), "application/pdf", filename + ".pdf")
            raise ValueError("仅支持 HTML 或 PDF")
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return self._json({"error": "report_render_failed", "message": str(exc)}, 400)

    def _report_email(self, body: Mapping[str, Any]) -> None:
        if body.get("confirm") is not True:
            return self._json({"error": "confirmation_required", "message": "邮件发送需要明确确认"}, 400)
        try:
            html, filename = self._report_document(body)
            recipient = str(body.get("recipient") or "").strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
                raise ValueError("收件人邮箱格式不正确")
            formats = [str(item).lower() for item in body.get("formats", []) if str(item).lower() in {"html", "pdf"}]
            if not formats:
                raise ValueError("至少选择一种附件格式")
            config = self.server.settings.snapshot(include_secrets=True).get("email", {})
            host = str(config.get("host") or "").strip()
            sender = str(config.get("sender") or config.get("username") or "").strip()
            if not host or not sender:
                raise ValueError("请先在设置中填写 SMTP 主机和发件人")
            message = EmailMessage()
            message["Subject"] = str(body.get("subject") or "微信情报日报 %s" % filename)[:180]
            message["From"] = sender
            message["To"] = recipient
            message.set_content("微信情报日报已生成，详见附件。")
            message.add_alternative(html, subtype="html")
            if "html" in formats:
                message.add_attachment(html.encode("utf-8"), maintype="text", subtype="html", filename=filename + ".html")
            if "pdf" in formats:
                message.add_attachment(_render_report_pdf(html), maintype="application", subtype="pdf", filename=filename + ".pdf")
            security = str(config.get("security") or "ssl")
            port = int(config.get("port") or (465 if security == "ssl" else 587))
            username = str(config.get("username") or "").strip()
            password = str(config.get("password") or "")
            context = ssl.create_default_context()
            client = smtplib.SMTP_SSL(host, port, timeout=30, context=context) if security == "ssl" else smtplib.SMTP(host, port, timeout=30)
            try:
                if security == "starttls":
                    client.starttls(context=context)
                if username:
                    client.login(username, password)
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    client.close()
            return self._json({"ok": True, "recipient": recipient, "formats": formats})
        except (ValueError, RuntimeError, OSError, smtplib.SMTPException, subprocess.SubprocessError) as exc:
            return self._json({"error": "report_email_failed", "message": str(exc)}, 400)

    @staticmethod
    def _int_query(
        query: Dict[str, Any],
        key: str,
        default: int,
        cap: int = 500,
    ) -> int:
        try:
            return max(1, min(int(cap), int((query.get(key) or [default])[0])))
        except (TypeError, ValueError):
            return default

    def _json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            default=_json_default,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before JSON response completed")

    def _binary(self, data: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before file response completed")


def serve_dashboard(service, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the dashboard until interrupted."""
    server = BridgeHttpServer((host, int(port)), service)
    logger.info("dashboard listening at http://%s:%s", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def start_dashboard_thread(
    service,
    host: str = "127.0.0.1",
    port: int = 8765,
):
    server = BridgeHttpServer((host, int(port)), service)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="wechat-bridge-dashboard",
        daemon=True,
    )
    thread.start()
    return server, thread
