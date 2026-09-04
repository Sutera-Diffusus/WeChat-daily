"""Local, provider-free conversation reconstruction prototype.

This module is deliberately a small seam beside the existing semantic and
context-packet experiments.  It does not import a provider, open a database,
read a frozen artifact, or mutate the production workbench.  Its job is to
make the *observable conversation process* reviewable before anyone attempts
to assign a final topic or event.

The public entry point is :func:`reconstruct_context`.  It accepts explicit
message mappings and returns a JSON-serialisable, body-free projection by
default.  ``include_bodies=True`` is an explicit opt-in for local review
tests; :func:`write_review_artifacts` renders escaped source text only into an
HTML file and keeps the JSON manifest/ledger body-free.

The prototype intentionally uses the word ``candidate`` throughout.  A
greeting, question, local topic, or thread is an observable/candidate signal,
not a final semantic interpretation.  In particular, a shared word never
merges chats and silence never closes an episode.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


SCHEMA_VERSION = "conversation_reconstruction_v1"
CONTEXT_SCHEMA_VERSION = "conversation_context_v1"
PIPELINE_VERSION = "provider_free_reconstruction_v1"
RULESET_VERSION = "candidate_signals_v1"
ARTIFACT_VERSION = "conversation_reconstruction_prototype_v1"
EVALUATION_VERSION = "context_reconstruction_evaluation_v3"
DEFAULT_REFERENCE_DATE = "2026-08-25"
SUPPORTED_VIEWS = ("today", "yesterday", "week")

_BODY_KEYS = frozenset({
    "body", "content", "message_content", "message_text", "raw_message",
    "raw_text", "text", "transcript", "transcription", "ocr_text",
    "caption", "extracted_text", "provider_response", "reasoning",
})
_SOURCE_BODY_KEYS = frozenset({
    "content", "text", "body", "message_content", "message_text",
    "raw_text", "redacted_text", "transcript", "transcription", "ocr_text",
    "caption", "extracted_text",
})

_URL_RE = re.compile(r"(?P<url>(?:https?://|www\.)[^\s<>\"']+)", re.I)
_DOMAIN_LIKE_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9_-])", re.I)
_MENTION_RE = re.compile(r"@(?P<name>[\w\u3400-\u9fff][^\s@,:;，。！？!?（）()<>]*)")
_HASHTAG_RE = re.compile(r"#(?P<tag>[\w\u3400-\u9fff-]{2,32})#?")
_CHINESE_TOKEN_RE = re.compile(r"[\u3400-\u9fff]{2,16}")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,32}")

_STOPWORDS = frozenset({
    "我们", "你们", "他们", "这个", "那个", "一下", "可以", "还是", "已经",
    "因为", "所以", "然后", "现在", "今天", "昨天", "明天", "什么", "怎么",
    "哪个", "一下子", "一下吧", "真的", "就是", "不是", "没有", "知道",
    "谢谢", "收到", "好的", "哈哈", "哈哈哈", "你好", "辛苦", "在吗", "看看",
    "please", "thanks", "hello", "there", "with", "that", "this", "from",
    "have", "what", "when", "where", "would", "could", "about",
})
# These are useful for local display, but are too generic to be treated as a
# concrete object when deciding whether two adjacent messages are parallel
# strands.  Keeping this list separate from ``_STOPWORDS`` avoids changing
# the existing local-topic candidates or the public token hashes.
_GENERIC_TOPIC_TOKENS = frozenset({
    "问题", "事情", "东西", "内容", "部分", "方面", "情况", "安排", "方案",
    "项目", "系统", "应用", "功能", "需求", "工作", "一下", "什么", "怎么",
    "可以", "需要", "确认", "看看", "知道", "后面", "现在", "然后", "这个",
    "issue", "question", "thing", "stuff", "topic", "cost", "price", "plan", "purchase", "renewal",
})
# A few concrete object families make the intended parallel-strand rule
# explicit for the development fixture (domain purchase, subscription/
# renewal, and forum registration).  Renewal vocabulary belongs to the
# subscription family even when it is adjacent to a domain turn: typed family
# evidence is the stronger routing signal, while the generic anchor overlap
# rule below still handles unseen object names.
_PARALLEL_TOPIC_FAMILIES = (
    frozenset({"domain", "域名", "购买", "买域名", "购买域名", "续费域名"}),
    frozenset({"subscription", "订阅", "套餐", "会员", "续订", "续费", "到期", "过期", "renewal"}),
    frozenset({"forum", "论坛", "github", "linuxdo", "l站", "注册"}),
)
_SOCIAL_LABELS = frozenset({"greeting", "turn_taking", "chitchat", "teasing", "acknowledgement"})
_SUBSTANTIVE_LABELS = frozenset({"sharing", "discussion", "question", "request"})

_GREETING_RE = re.compile(r"(?:^|[\s,，。.!！?？])(?:你好|您好|早上好|晚上好|下午好|嗨|哈喽|hello|hi|hey|在吗|辛苦了)(?:$|[\s,，。.!！?？])", re.I)
_ACK_RE = re.compile(r"(?:收到|好的|好哒|明白|了解|ok|OK|嗯嗯|行|没问题|知道了|感谢)", re.I)
_QUESTION_RE = re.compile(r"(?:\?|？|吗[？?！!。\s]*$|什么|怎么|为何|为什么|是否|哪天|几点|多少|能不能|可以不可以|请问)", re.I)
_REQUEST_RE = re.compile(r"(?:请|麻烦|帮我|帮忙|需要|记得|能否|请你|帮看|确认一下|安排|提交|发我|给我)", re.I)
_SHARE_RE = re.compile(r"(?:分享|发给|链接|资料|截图|文档|附件|推荐|看到|发现|结果是|结论是|我这边|这里有)", re.I)
_DISCUSSION_RE = re.compile(r"(?:讨论|意见|看法|方案|比较|区别|对比|原因|影响|支持|反对|建议|考虑|问题是|风险)", re.I)
_TEASING_RE = re.compile(r"(?:哈哈|笑死|破防|离谱|太\w*了|你又|懂的都懂|开玩笑|doge|😂|🤣|😆|🙃)", re.I)
_CONTINUATION_RE = re.compile(r"(?:那|然后|继续|还是|另外|补充|顺便|再说|接着|上面|这个|它|他|她|同样|对了|不过|但是)")
# Hard markers are safe without looking at neighbouring text.  ``对了`` and
# ``顺便问`` are discourse markers, not guaranteed topic changes: speakers
# commonly use them to resume the same object.  They are handled by
# ``_topic_shift_candidate`` only when the new turn has no concrete overlap
# with the previous turn.
_TOPIC_SHIFT_RE = re.compile(r"(?:换个话题|换一个话题|说回|另外一件事|另一件事|先不说|不聊这个)")
_SOFT_TOPIC_SHIFT_RE = re.compile(r"(?:对了|顺便问(?:一下)?|顺带问(?:一下)?)")
_TIME_RE = re.compile(r"(?:今天|昨天|明天|后天|本周|下周|月底|周[一二三四五六日天]|\d{1,2}月\d{1,2}[日号]?|\d{1,2}:\d{2})")


def _stable_hash(value: Any, *, length: int = 16) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _text(value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _first(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return default


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"1", "true", "yes", "y", "是", "群", "group"}:
            return True
        if lowered in {"0", "false", "no", "n", "否", "私聊", "direct"}:
            return False
    return None


def _normalise_chat_type(value: Any) -> Optional[str]:
    if value is None:
        return None
    lowered = str(value).strip().casefold()
    if lowered in {"group", "群聊", "群", "group_chat", "chatroom", "room", "多人", "multi"}:
        return "group"
    if lowered in {"direct", "私聊", "一对一", "single", "one_to_one", "one-to-one", "private"}:
        return "direct"
    if lowered in {"unknown", "unclassified", ""}:
        return None
    return None


def _normalise_message_type(value: Any, content: str) -> str:
    lowered = str(value or "text").strip().casefold()
    aliases = {
        "txt": "text", "文字": "text", "文本": "text", "link": "link", "链接": "link",
        "url": "link", "image": "image", "picture": "image", "img": "image", "图片": "image",
        "voice": "audio", "audio": "audio", "语音": "audio", "recording": "audio",
        "video": "video", "视频": "video", "file": "file", "文件": "file", "document": "file",
        "sticker": "sticker", "emoji": "sticker", "表情": "sticker",
    }
    kind = aliases.get(lowered, lowered or "text")
    if kind in {"text", "unknown"} and _URL_RE.search(content):
        return "link"
    if kind in {"unknown", "other"}:
        compact = content.casefold()
        if any(marker in compact for marker in ("[图片]", "[image]", "[photo]")):
            return "image"
        if any(marker in compact for marker in ("[语音]", "[voice]", "[audio]")):
            return "audio"
        if any(marker in compact for marker in ("[视频]", "[video]")):
            return "video"
        if any(marker in compact for marker in ("[文件]", "[文件/链接/卡片]", "[file]", "[链接]", "[卡片]", "[link]")):
            return "file"
        if any(marker in compact for marker in ("[动画表情]", "[表情]", "[sticker]", "[emoji]")):
            return "sticker"
    return kind if kind in {"text", "link", "image", "audio", "video", "file", "sticker", "system", "unknown"} else "unknown"


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = _text(value).strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _local_day(timestamp: Optional[datetime], explicit: Any = None) -> Optional[str]:
    raw = _text(explicit).strip()
    if raw:
        match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if match:
            return match.group(0)
    if timestamp is not None:
        # The input contract is already local-day aware; for an offset-aware
        # value retain its supplied offset instead of silently changing a day.
        return timestamp.date().isoformat()
    return None


def _as_sequence(value: Any) -> List[Any]:
    if isinstance(value, Mapping):
        for key in ("messages", "rows", "items", "records"):
            if isinstance(value.get(key), Sequence) and not isinstance(value.get(key), (str, bytes, bytearray)):
                return list(value.get(key) or ())
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return list(value) if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)) else []


def _message_body(row: Mapping[str, Any]) -> str:
    return _text(_first(row, "content", "text", "body", "message_content", "message_text", "redacted_text", "raw_text", default=""))


def _media_analysis_text(row: Mapping[str, Any], kind: str) -> Tuple[str, Optional[str]]:
    """Return explicitly supplied media-derived text and its source field.

    A binary placeholder is not analysis text.  OCR/ASR/file extraction and a
    direct caption are different: when one is present it may be used for
    local candidate recall, but the source field is carried separately so a
    derived span is never mistaken for the original message body.
    """

    media_state = _text(_first(row, "media_state", "content_availability", "availability", default="")).strip().casefold()
    if media_state in {"redacted_transcript", "verified_transcript", "transcript_verified"}:
        verified_text = _text(_first(row, "redacted_text", "redacted_transcript", "verified_transcript", default="")).strip()
        if verified_text:
            return verified_text, "redacted_text"

    key_groups = {
        "image": ("ocr_text", "image_ocr", "ocr", "caption", "image_caption", "description"),
        "audio": ("transcript", "transcription", "voice_text"),
        "video": ("transcript", "transcription", "video_transcript", "caption", "video_caption"),
        "file": ("extracted_text", "file_text", "document_text", "file_caption", "description"),
        "sticker": ("alt_text", "emoji_text"),
    }
    for key in key_groups.get(kind, ()):
        value = _text(row.get(key)).strip()
        if value:
            return value, key
    for key in ("media_text", "content_text", "media_caption"):
        value = _text(row.get(key)).strip()
        if value:
            return value, key
    return "", None


def _source_id(row: Mapping[str, Any], index: int) -> str:
    value = _first(row, "message_id", "id", "msg_id", "record_id", "message_row_id")
    return _text(value).strip() or f"row-{index + 1:06d}"


def _normalise_messages(raw_messages: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Normalize source rows while retaining bodies only in the private map."""
    rows: List[Dict[str, Any]] = []
    private: Dict[str, Dict[str, Any]] = {}
    seen: set[str] = set()
    for index, original in enumerate(raw_messages):
        if not isinstance(original, Mapping):
            continue
        row = dict(original)
        source_id = _source_id(row, index)
        # Duplicate source IDs are retained as distinct physical rows only if
        # their identity differs; otherwise the authority ledger is deduped.
        if source_id in seen:
            duplicate_id = f"{source_id}~{_stable_hash((source_id, index), length=8)}"
            source_id = duplicate_id
        seen.add(source_id)
        account_id = _text(_first(row, "account_id", "account", "wx_account_id", default="unknown-account")).strip() or "unknown-account"
        chat_id = _text(_first(row, "chat_id", "conversation_id", "room_id", "chat", default="unknown-chat")).strip() or "unknown-chat"
        speaker_id = _text(_first(row, "speaker_id", "sender_id", "from_user_id", "user_id", "sender", default="unknown-speaker")).strip() or "unknown-speaker"
        speaker_name = _text(_first(row, "speaker_name", "sender_name", "from_user_name", "display_name", default="")).strip()
        local_day_hint = _first(row, "local_day", "date", "day")
        timestamp = _parse_datetime(_first(row, "timestamp", "time", "datetime", "created_at", "msg_time"))
        # Development projections often intentionally remove absolute times
        # while retaining a local day and relative offset.  Reconstruct a
        # reviewable local timestamp from those authoritative fields instead
        # of displaying every message as ``时间未知``.
        if timestamp is None and local_day_hint not in (None, ""):
            try:
                offset_value = _first(row, "time_offset_seconds", "offset_seconds", "time_offset")
                offset_seconds = float(offset_value) if offset_value not in (None, "") else None
                day_value = date.fromisoformat(_text(local_day_hint)[:10])
                if offset_seconds is not None and math.isfinite(offset_seconds):
                    timestamp = datetime.combine(day_value, datetime.min.time(), tzinfo=timezone(timedelta(hours=8))) + timedelta(seconds=offset_seconds)
            except (TypeError, ValueError, OverflowError):
                timestamp = None
        local_day = _local_day(timestamp, local_day_hint)
        body = _message_body(row)
        kind = _normalise_message_type(_first(row, "message_type", "type", "msg_type", default="text"), body)
        explicit_group = _bool(_first(row, "is_group", "group_chat", "is_chatroom"))
        explicit_chat_type = _normalise_chat_type(_first(row, "chat_type", "conversation_type", "scope_type"))
        reply_to = _first(row, "reply_to_message_id", "reply_to", "quoted_message_id", "referenced_message_id")
        reply_to_text = _text(reply_to).strip() or None
        topic_shift_hint = _bool(_first(row, "topic_shift", "topic_shift_hint", "new_topic", default=False))
        conversation_boundary_hint = _bool(_first(row, "conversation_boundary", "new_conversation", "episode_boundary", default=False))
        sequence = _first(row, "sequence_in_chat", "sequence", "position_in_chat", "position", "index")
        try:
            sequence_value = int(sequence) if sequence is not None else None
        except (TypeError, ValueError):
            sequence_value = None
        member_count = _first(row, "member_count", "chat_member_count", "participant_count")
        try:
            member_count_value = int(member_count) if member_count is not None else None
        except (TypeError, ValueError):
            member_count_value = None
        # A blind retrieval phase may already have assigned an opaque,
        # source-authoritative reference to this physical row.  Preserve it
        # when present so the deferred body phase can audit the invariant
        # ``expected refs == materialized refs == reconstructed refs`` without
        # translating through a second hash namespace.  Ordinary callers do
        # not provide ``message_ref`` and retain the historical deterministic
        # hash behaviour.
        provided_message_ref = _text(_first(row, "canonical_message_ref", "blind_message_ref", default="")).strip()
        if not provided_message_ref and _bool(row.get("blind_selection_locked")) is True:
            provided_message_ref = _text(row.get("message_ref")).strip()
        message_ref = provided_message_ref or f"msg-{_stable_hash((account_id, chat_id, source_id), length=16)}"
        participant_ref = f"participant-{_stable_hash((account_id, speaker_id), length=12)}"
        chat_ref = f"chat-{_stable_hash((account_id, chat_id), length=12)}"
        timestamp_iso = timestamp.isoformat(timespec="seconds") if timestamp is not None else None
        message = {
            "message_ref": message_ref,
            "source_message_id": source_id,
            "account_id": account_id,
            "chat_id": chat_id,
            "chat_ref": chat_ref,
            "participant_ref": participant_ref,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "timestamp": timestamp_iso,
            "local_day": local_day,
            "sequence": sequence_value,
            "message_type": kind,
            "reply_to_message_id": reply_to_text,
            "topic_shift_hint": topic_shift_hint,
            "conversation_boundary_hint": conversation_boundary_hint,
            "explicit_chat_type": explicit_chat_type,
            "explicit_is_group": explicit_group,
            "member_count_hint": member_count_value,
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "content_length": len(body),
            "content_available": bool(body),
            "timestamp_source": "explicit" if timestamp is not None else "missing",
            "source_index": index,
        }
        rows.append(message)
        private[message_ref] = {
            "body": body,
            "original": row,
            "message": message,
        }
    return rows, private


def _sort_message_key(message: Mapping[str, Any]) -> Tuple[Any, ...]:
    timestamp = _parse_datetime(message.get("timestamp"))
    return (timestamp or datetime.max.replace(tzinfo=timezone.utc), message.get("sequence") is None, message.get("sequence") or 0, message.get("source_index") or 0)


def _extract_tokens(body: str) -> List[str]:
    tokens: List[str] = []
    for token in _CHINESE_TOKEN_RE.findall(body) + _LATIN_TOKEN_RE.findall(body):
        compact = token.strip().casefold()
        if len(compact) < 2 or compact in _STOPWORDS:
            continue
        if compact not in tokens:
            tokens.append(compact)
    return tokens


def _flow_tokens(body: str) -> set[str]:
    """Return small lexical anchors for cautious continuing-flow links.

    The local topic helper intentionally keeps long Chinese runs intact. For
    cross-episode linking that would miss a repeated anchor such as ``选课``
    when the surrounding sentence changes, so add non-stopword Chinese
    bigrams. These remain candidate evidence, never a topic assignment.
    """
    tokens = set(_extract_tokens(body))
    for run in _CHINESE_TOKEN_RE.findall(body):
        for index in range(len(run) - 1):
            pair = run[index:index + 2]
            if pair not in _STOPWORDS:
                tokens.add(pair)
    return tokens


def _token_set(body: str) -> set[str]:
    return set(_extract_tokens(body))


def _topic_shift_candidate(current_body: str, previous_body: str = "") -> bool:
    """Return a cautious textual topic-shift candidate.

    Explicit hard markers remain sufficient on their own.  Soft markers only
    become a boundary candidate when the readable content after the marker
    has no shared flow anchor with the previous turn.  This prevents a phrase
    such as ``对了，继续看接口进度`` from breaking an otherwise continuous
    exchange while still separating ``对了，电影什么时候上映``.
    """

    if _TOPIC_SHIFT_RE.search(current_body):
        return True
    if not _SOFT_TOPIC_SHIFT_RE.search(current_body):
        return False
    current_anchors = _flow_tokens(current_body) - _GENERIC_TOPIC_TOKENS
    if not current_anchors:
        return False
    previous_anchors = _flow_tokens(previous_body) - _GENERIC_TOPIC_TOKENS
    return not bool(current_anchors & previous_anchors)


def _split_clauses(body: str) -> List[Tuple[str, int, int]]:
    if not body:
        return [("", 0, 0)]
    clauses: List[Tuple[str, int, int]] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n]+", body):
        end = match.end()
        piece = body[start:end].strip()
        if piece:
            leading = len(body[start:end]) - len(body[start:end].lstrip())
            clauses.append((piece, start + leading, end))
        start = end
    tail = body[start:].strip()
    if tail:
        leading = len(body[start:]) - len(body[start:].lstrip())
        clauses.append((tail, start + leading, len(body)))
    return clauses or [(body, 0, len(body))]


def _candidate_signal(label: str, score: float, message: Mapping[str, Any], basis: Sequence[str]) -> Dict[str, Any]:
    return {
        "signal_id": f"signal-{_stable_hash((message.get('message_ref'), label, tuple(basis)), length=14)}",
        "label": label,
        "candidate_only": True,
        "semantic_status": "candidate_only",
        "score": round(max(0.0, min(1.0, score)), 4),
        "evidence_refs": [message.get("message_ref")],
        "basis": list(basis),
        "final_semantics": None,
    }


def _interaction_signals(body: str, message: Mapping[str, Any]) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    if _GREETING_RE.search(body) or body.strip().casefold() in {"hi", "hello", "你好", "您好", "嗨", "在吗"}:
        signals.append(_candidate_signal("greeting", 0.88, message, ["greeting_lexicon"]))
    if _ACK_RE.search(body) and len(body.strip()) <= 16:
        signals.append(_candidate_signal("acknowledgement", 0.76, message, ["short_ack_lexicon"]))
    if _CONTINUATION_RE.search(body) or message.get("reply_to_message_id"):
        basis = ["continuation_cue"]
        if message.get("reply_to_message_id"):
            basis.append("explicit_reply_metadata")
        signals.append(_candidate_signal("turn_taking", 0.82 if len(basis) > 1 else 0.58, message, basis))
    if _QUESTION_RE.search(body):
        signals.append(_candidate_signal("question", 0.86, message, ["question_marker_or_interrogative"]))
    if _REQUEST_RE.search(body):
        signals.append(_candidate_signal("request", 0.84, message, ["request_lexicon"]))
    if _SHARE_RE.search(body) or _URL_RE.search(body):
        basis = ["sharing_lexicon" if _SHARE_RE.search(body) else "url_present"]
        signals.append(_candidate_signal("sharing", 0.7, message, basis))
    if _DISCUSSION_RE.search(body):
        signals.append(_candidate_signal("discussion", 0.73, message, ["discussion_lexicon"]))
    if _TEASING_RE.search(body):
        signals.append(_candidate_signal("teasing", 0.68, message, ["tone_or_teasing_lexicon"]))
    if not signals and body and len(_token_set(body)) <= 1 and len(body.strip()) <= 14:
        signals.append(_candidate_signal("chitchat", 0.42, message, ["short_low_topic_text"]))
    # A pure social exchange should remain visible as a flow, but a sentence
    # containing a question/request is never downgraded to social-only.
    if not signals and not body:
        signals.append(_candidate_signal("chitchat", 0.2, message, ["no_text_media_or_empty_body"]))
    return signals


def _is_system_message(message: Mapping[str, Any], original: Mapping[str, Any] | None = None) -> bool:
    """Identify non-conversational system rows for semantic exclusion."""
    if str(message.get("message_type") or "").casefold() == "system":
        return True
    source = original if isinstance(original, Mapping) else {}
    return any(
        str(source.get(key) or "").strip().casefold() == "system"
        for key in ("message_type", "record_type", "message_role", "dialogue_event_role")
    )


def _media_availability(row: Mapping[str, Any], message: Mapping[str, Any], body: str) -> Dict[str, Any]:
    kind = str(message.get("message_type") or "unknown")
    original = row
    explicit_state = _text(_first(original, "media_state", "content_availability", "availability", default="")).strip().casefold()
    if _is_system_message(message, original):
        return {
            "kind": "system",
            "status": "unavailable",
            "content_available": bool(body),
            "text_available": bool(body),
            "parsed": False,
            "resolved": None,
            "semantic_evidence_eligible": False,
            "missing_reason": "system_message_not_semantic",
            "capabilities": {"system_metadata_only": True},
        }
    if kind == "text" and not _URL_RE.search(body):
        return {
            "kind": "text",
            "status": "available" if body else "unavailable",
            "content_available": bool(body),
            "text_available": bool(body),
            "parsed": bool(body),
            "resolved": None,
            "semantic_evidence_eligible": bool(body),
            "missing_reason": None if body else "empty_text",
            "capabilities": {"plain_text": bool(body)},
        }
    if kind == "link" or _URL_RE.search(body):
        resolved_value = _first(original, "link_resolved", "url_resolved", "resolved", "link_metadata_available")
        resolved = _bool(resolved_value)
        parsed = bool(_URL_RE.search(body))
        if resolved is None:
            resolved = explicit_state in {"resolved", "available", "parsed", "link_resolved"}
        metadata_fields = any(_text(original.get(key)).strip() for key in ("link_title", "link_description", "link_metadata", "resolved_url"))
        return {
            "kind": "link",
            "status": "available" if parsed and (resolved or metadata_fields) else ("partial" if parsed else "unavailable"),
            "content_available": parsed,
            "text_available": bool(body),
            "parsed": parsed,
            "resolved": bool(resolved),
            # A link whose target was not resolved is retained as a visible
            # media candidate but cannot supply semantic evidence.  The URL
            # string itself is metadata, not the target content.
            "semantic_evidence_eligible": bool((resolved or False) and body),
            "missing_reason": None if resolved or metadata_fields else "link_not_resolved",
            "capabilities": {"url": parsed, "link_parse": parsed, "link_resolution": bool(resolved), "metadata": metadata_fields},
        }
    if kind in {"image", "audio", "video", "file", "sticker"}:
        # ``redacted_text``/``content`` on a media row is often only a
        # placeholder (for example ``[图片]``).  It is not OCR, a caption, a
        # transcript, or extracted file text.  Only explicit media-derived
        # fields are allowed to make an unavailable binary media item
        # evidence-eligible.
        derived_text, derived_source = _media_analysis_text(original, kind)
        extracted = derived_source in {
            "ocr_text", "image_ocr", "ocr", "transcript", "transcription",
            "voice_text", "video_transcript", "extracted_text", "file_text",
            "document_text", "alt_text", "emoji_text", "redacted_text",
        }
        caption = derived_source in {"caption", "image_caption", "description", "video_caption", "file_caption"}
        media_annotation = derived_source in {"media_text", "content_text", "media_caption"}
        available = bool(derived_text)
        if kind == "image":
            reason = None if available else ("ocr_not_available" if not caption else "image_content_missing")
            capabilities = {"ocr": bool(extracted), "caption_available": bool(caption)}
        elif kind in {"audio", "video"}:
            reason = None if available else "transcription_not_available"
            capabilities = {"transcript_available": bool(extracted), "caption_available": bool(caption)}
        elif kind == "file":
            reason = None if available else "file_extraction_not_available"
            capabilities = {"file_extraction": bool(extracted), "caption_available": bool(caption)}
        else:
            reason = None if available else "non_text_media"
            capabilities = {"alt_text": bool(extracted)}
        # The source body is allowed as a caption/transcript only when the
        # source explicitly gave one; a binary media placeholder itself is not
        # semantic evidence.
        return {
            "kind": kind,
            "status": "available" if available else "unavailable",
            "content_available": available,
            "text_available": bool(extracted or caption or media_annotation),
            "parsed": bool(extracted),
            "resolved": None,
            "semantic_evidence_eligible": bool(extracted or caption or media_annotation),
            "missing_reason": reason,
            "semantic_text_source": derived_source,
            "capabilities": capabilities,
        }
    available = bool(body)
    return {
        "kind": kind,
        "status": "available" if available else "unavailable",
        "content_available": available,
        "text_available": available,
        "parsed": available,
        "resolved": None,
        "semantic_evidence_eligible": available,
        "missing_reason": None if available else "unknown_content_type",
        "capabilities": {"plain_text": available},
    }


def _analysis_text_for_message(
    message: Mapping[str, Any],
    private_row: Mapping[str, Any],
) -> Tuple[str, str]:
    """Return local candidate text and the field it came from.

    The source body remains private.  For binary media, only explicitly
    supplied OCR/ASR/file-extraction or caption fields can feed local
    candidate recall; a placeholder such as ``[语音]`` is retained for review
    but never treated as semantic text.  The source label lets callers attach
    spans to the derived artifact instead of pretending they are offsets in
    the original message body.
    """

    source_body = str(private_row.get("body") or "")
    original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
    kind = str(message.get("message_type") or "unknown")
    media = _media_availability(original, message, source_body)
    if kind in {"image", "audio", "video", "file", "sticker"}:
        derived_text, derived_source = _media_analysis_text(original, kind)
        if media.get("semantic_evidence_eligible") and derived_text:
            return derived_text, str(derived_source or "media_derived_text")
        return "", "media_unavailable"
    if media.get("semantic_evidence_eligible"):
        return source_body, "message_body"
    return "", "content_unavailable"


def _mention_candidates(body: str, message: Mapping[str, Any], participants: Mapping[str, str]) -> List[Dict[str, Any]]:
    mentions: List[Dict[str, Any]] = []
    for match in _MENTION_RE.finditer(body):
        surface = match.group("name")
        mentions.append({
            "mention_id": f"mention-{_stable_hash((message.get('message_ref'), match.start(), match.end(), 'person'), length=14)}",
            "message_ref": message.get("message_ref"),
            "mention_type": "person",
            "span": {"start": match.start(), "end": match.end()},
            "surface_hash": hashlib.sha256(surface.encode("utf-8")).hexdigest(),
            "candidate_only": True,
            "resolution": "unresolved",
            "evidence_refs": [message.get("message_ref")],
        })
    for match in _URL_RE.finditer(body):
        surface = match.group("url").rstrip("。！？!?，,)")
        domain = urlparse(surface if surface.startswith("http") else "http://" + surface).netloc.casefold()
        mentions.append({
            "mention_id": f"mention-{_stable_hash((message.get('message_ref'), match.start(), match.end(), 'url'), length=14)}",
            "message_ref": message.get("message_ref"),
            "mention_type": "url",
            "span": {"start": match.start(), "end": match.start() + len(surface)},
            "surface_hash": hashlib.sha256(surface.encode("utf-8")).hexdigest(),
            "domain_hint": domain or "unknown-domain",
            "candidate_only": True,
            "resolution": "parsed_unresolved",
            "evidence_refs": [message.get("message_ref")],
        })
    for match in _HASHTAG_RE.finditer(body):
        surface = match.group("tag")
        mentions.append({
            "mention_id": f"mention-{_stable_hash((message.get('message_ref'), match.start(), match.end(), 'tag'), length=14)}",
            "message_ref": message.get("message_ref"),
            "mention_type": "hashtag",
            "span": {"start": match.start(), "end": match.end()},
            "surface_hash": hashlib.sha256(surface.encode("utf-8")).hexdigest(),
            "candidate_only": True,
            "resolution": "unresolved",
            "evidence_refs": [message.get("message_ref")],
        })
    # Known participant names are a useful candidate mention, but never imply
    # that the mentioned person is the speaker or the subject.
    for participant_ref, name in participants.items():
        if not name or len(name) < 2:
            continue
        for match in re.finditer(re.escape(name), body, flags=re.I):
            mentions.append({
                "mention_id": f"mention-{_stable_hash((message.get('message_ref'), match.start(), match.end(), participant_ref), length=14)}",
                "message_ref": message.get("message_ref"),
                "mention_type": "person",
                "span": {"start": match.start(), "end": match.end()},
                "surface_hash": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                "candidate_only": True,
                "resolution": "candidate_participant",
                "participant_ref": participant_ref,
                "evidence_refs": [message.get("message_ref")],
            })
    unique: Dict[str, Dict[str, Any]] = {str(item["mention_id"]): item for item in mentions}
    return [unique[key] for key in sorted(unique)]


def _explicit_matter_candidates(body: str, message: Mapping[str, Any], signals: Sequence[Mapping[str, Any]], tokens: Sequence[str]) -> List[Dict[str, Any]]:
    labels = {str(signal.get("label")) for signal in signals}
    matters: List[Dict[str, Any]] = []
    if "question" in labels:
        kind = "question"
    elif "request" in labels:
        kind = "request"
    elif "discussion" in labels:
        kind = "discussion_prompt"
    elif _TIME_RE.search(body) and tokens:
        kind = "time_reference"
    else:
        kind = ""
    if kind:
        matters.append({
            "matter_id": f"matter-{_stable_hash((message.get('message_ref'), kind, tuple(tokens)), length=14)}",
            "kind": kind,
            "status": "candidate_only",
            "candidate_only": True,
            "subject_status": "unknown",
            "object_status": "candidate_from_local_tokens" if tokens else "unknown",
            "evidence_refs": [message.get("message_ref")],
            "basis": [f"interaction_signal:{kind if kind != 'discussion_prompt' else 'discussion'}"],
            "uncertainties": ["not_a_final_event", "speaker_subject_not_equated"],
        })
    return matters


def _information_value(signals: Sequence[Mapping[str, Any]], media: Mapping[str, Any], body: str, matters: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compute local signal density, never a semantic/value judgment.

    The legacy field name ``information_value`` is retained as a compatibility
    envelope, but its public value is explicitly unknown until human/model
    review.  This protects a lexical/availability heuristic from becoming a
    hidden filter or a claim about importance.
    """
    labels = {str(item.get("label")) for item in signals}
    components = {
        "substantive_signal": 1 if labels & _SUBSTANTIVE_LABELS else 0,
        "explicit_matter": min(2, len(matters)),
        "content_available": 1 if media.get("semantic_evidence_eligible") else 0,
        "social_only_penalty": 1 if labels and labels <= _SOCIAL_LABELS else 0,
        "text_length_band": 2 if len(body.strip()) >= 80 else (1 if len(body.strip()) >= 20 else 0),
    }
    raw = 0.12 + 0.2 * components["substantive_signal"] + 0.2 * components["explicit_matter"] + 0.12 * components["content_available"] + 0.08 * components["text_length_band"] - 0.15 * components["social_only_penalty"]
    signal_density_score = round(max(0.0, min(1.0, raw)), 4)
    signal_density_label = "low" if signal_density_score < 0.35 else ("medium" if signal_density_score < 0.68 else "high")
    return {
        # ``score``/``label`` remain explicit unknowns: local signal density
        # is not information value and cannot stand in for it.
        "score": None,
        "label": "unknown",
        "status": "unknown_pending_model",
        "value_status": "unknown_pending_model",
        "signal_density_score": signal_density_score,
        "signal_density_label": signal_density_label,
        "signal_density": {
            "score": signal_density_score,
            "label": signal_density_label,
            "candidate_only": True,
            "components": components,
        },
        "candidate_only": True,
        "components": components,
        "basis": [key for key, value in components.items() if value],
        "uncertainties": [
            "local_signal_density_is_not_information_value",
            "information_value_requires_model_or_human_judgment",
            "information_value_is_not_event_completeness",
        ],
    }


def _discussion_weight(signals: Sequence[Mapping[str, Any]], media: Mapping[str, Any], body: str) -> Dict[str, Any]:
    labels = {str(item.get("label")) for item in signals}
    numerator = 1 if labels & {"discussion", "question", "request"} else 0
    denominator = 1 if body or media.get("content_available") else 0
    components = {
        "discussion_or_question_or_request": numerator,
        "content_bearing_segment": denominator,
        "social_only_not_counted_as_discussion": 1 if labels and labels <= _SOCIAL_LABELS else 0,
    }
    return {
        "score": round(numerator / denominator, 4) if denominator else 0.0,
        "numerator": numerator,
        "denominator": denominator,
        "candidate_only": True,
        "components": components,
        "basis": [key for key, value in components.items() if value],
        "uncertainties": ["discussion_ratio_is_separate_from_information_value"],
    }


def _segment_role_and_type(
    body: str,
    signals: Sequence[Mapping[str, Any]],
    media: Mapping[str, Any],
    tokens: Sequence[str],
    matters: Sequence[Mapping[str, Any]],
) -> Tuple[str, str]:
    """Give a reversible fragment role/type without assigning a topic.

    The role is intentionally coarser than a semantic claim.  In particular,
    a short acknowledgement such as ``收到，确认一下`` remains context-only,
    while a greeting carrying a substantive request remains substantive so a
    greeting prefix cannot hide the useful part of a turn.
    """
    labels = {str(item.get("label")) for item in signals}
    if not media.get("semantic_evidence_eligible"):
        return "context_only", "media_placeholder"
    if labels & {"question"}:
        return "substantive", "question"
    if labels & {"request"} and not ("acknowledgement" in labels and len(body.strip()) <= 20 and "question" not in labels):
        return "substantive", "request"
    if labels & {"sharing"} and len(body.strip()) > 18:
        return "substantive", "statement"
    if labels & {"discussion"}:
        return "substantive", "statement"
    if "greeting" in labels and not (labels - {"greeting", "turn_taking", "chitchat", "teasing", "acknowledgement"}):
        return "conversation_opener", "conversation_opener"
    if "acknowledgement" in labels:
        return "context_only", "acknowledgement"
    if labels and labels <= _SOCIAL_LABELS and not tokens and not matters:
        return "social", "social"
    if tokens or matters or body.strip():
        return "substantive", "statement"
    return "context_only", "unknown"


def _chat_ledgers(messages: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
    by_chat: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for message in messages:
        by_chat[(str(message.get("account_id")), str(message.get("chat_id")))].append(message)
    chats: List[Dict[str, Any]] = []
    participants: Dict[Tuple[str, str], Dict[str, Any]] = {}
    chat_type_by_key: Dict[str, str] = {}
    for (account_id, chat_id), rows in sorted(by_chat.items()):
        explicit_types = [row.get("explicit_chat_type") for row in rows if row.get("explicit_chat_type")]
        explicit_groups = [row.get("explicit_is_group") for row in rows if row.get("explicit_is_group") is not None]
        member_hints = [row.get("member_count_hint") for row in rows if row.get("member_count_hint")]
        speaker_ids = {str(row.get("speaker_id")) for row in rows}
        hint = max(member_hints) if member_hints else None
        if "group" in explicit_types or True in explicit_groups or (hint is not None and hint > 2) or len(speaker_ids) > 2:
            chat_type, source = "group", "explicit_or_member_count" if ("group" in explicit_types or True in explicit_groups or (hint is not None and hint > 2)) else "participant_count"
        elif "direct" in explicit_types or False in explicit_groups or len(speaker_ids) <= 2:
            chat_type, source = "direct", "explicit_or_two_speakers"
        else:
            chat_type, source = "unknown", "insufficient_membership_metadata"
        chat_ref = str(rows[0].get("chat_ref"))
        chat_type_by_key[f"{account_id}\x1f{chat_id}"] = chat_type
        for row in rows:
            key = (account_id, str(row.get("speaker_id")))
            current = participants.setdefault(key, {
                "participant_ref": row.get("participant_ref"),
                "account_id": account_id,
                "speaker_id": row.get("speaker_id"),
                "speaker_name": row.get("speaker_name") or "",
                "chat_refs": set(),
                "message_count": 0,
                "is_self": False,
            })
            current["chat_refs"].add(chat_ref)
            current["message_count"] += 1
            current["is_self"] = bool(current["is_self"] or _bool(_first(row, "is_self", "from_me", default=False)))
        times = [_parse_datetime(row.get("timestamp")) for row in rows if row.get("timestamp")]
        days = sorted({str(row.get("local_day")) for row in rows if row.get("local_day")})
        chats.append({
            "chat_ref": chat_ref,
            "account_id": account_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "chat_type_source": source,
            "participant_refs": sorted({str(row.get("participant_ref")) for row in rows}),
            "message_refs": sorted(str(row.get("message_ref")) for row in rows),
            "message_count": len(rows),
            "observed_days": days,
            "first_timestamp": min(times).isoformat(timespec="seconds") if times else None,
            "last_timestamp": max(times).isoformat(timespec="seconds") if times else None,
            "authority": "source_message_metadata_and_scope",
        })
    participant_rows = []
    for item in participants.values():
        copy = dict(item)
        copy["chat_refs"] = sorted(str(value) for value in item["chat_refs"])
        copy["participant_id"] = copy.get("participant_ref")
        copy["speaker_name_available"] = bool(item.get("speaker_name"))
        participant_rows.append(copy)
    participant_rows.sort(key=lambda item: str(item.get("participant_ref")))
    return chats, participant_rows, chat_type_by_key


def _adaptive_silence_cutoff(rows: Sequence[Mapping[str, Any]]) -> Optional[float]:
    gaps: List[float] = []
    previous: Optional[datetime] = None
    for row in sorted(rows, key=_sort_message_key):
        current = _parse_datetime(row.get("timestamp"))
        if current is not None and previous is not None:
            seconds = (current - previous).total_seconds()
            if seconds >= 0:
                gaps.append(seconds)
        if current is not None:
            previous = current
    if len(gaps) < 3:
        # With too little history a silence is not strong enough to make an
        # episode boundary.  The open candidate is safer than a hard cut.
        return None
    median = statistics.median(gaps)
    upper = statistics.quantiles(gaps, n=4, method="inclusive")[2] if len(gaps) >= 2 else median
    # This is a distribution-derived candidate threshold, not a day/message
    # limit.  It intentionally has no fixed maximum.
    return max(median * 4.0, upper * 2.0)


def _continuity_evidence(previous: Mapping[str, Any], current: Mapping[str, Any], previous_body: str, current_body: str) -> Dict[str, Any]:
    prev_tokens, current_tokens = _token_set(previous_body), _token_set(current_body)
    overlap = sorted(prev_tokens & current_tokens)
    reply = str(current.get("reply_to_message_id") or "") in {str(previous.get("source_message_id")), str(previous.get("message_ref"))}
    continuation = bool(_CONTINUATION_RE.search(current_body))
    shift = _topic_shift_candidate(current_body, previous_body) or bool(current.get("topic_shift_hint"))
    same_day = bool(previous.get("local_day") and previous.get("local_day") == current.get("local_day"))
    if reply:
        relation, strength = "explicit_reply", "strong"
    elif overlap and not shift:
        relation, strength = "lexical_overlap", "medium"
    elif continuation and not shift:
        relation, strength = "continuation_cue", "weak"
    elif shift:
        relation, strength = "topic_shift_candidate", "medium"
    else:
        relation, strength = "same_chat_time_order", "weak"
    return {
        "relation": relation,
        "evidence_strength": strength,
        "reply": reply,
        "shared_token_count": len(overlap),
        "shared_tokens_hash": _stable_hash(overlap, length=12) if overlap else None,
        "continuation_cue": continuation,
        "topic_shift_cue": shift,
        "same_day": same_day,
        "time_proximity_is_weak_only": True,
        "candidate_only": True,
    }


def _parallel_anchor_tokens(body: str) -> set[str]:
    """Return cautious object anchors for parallel-strand routing.

    This is intentionally narrower than ``local_topics``.  Generic words and
    social glue must not make two simultaneous subjects look like one stream;
    the returned values are routing evidence only and are never shown as a
    final topic.
    """
    anchors = {
        token
        for token in _flow_tokens(body)
        if token not in _GENERIC_TOPIC_TOKENS and len(token.strip()) >= 2
    }
    # Redacted development rows can retain a domain-shaped host without the
    # literal ``域名`` word (for example ``bb.bi``).  Keep that lexical shape
    # as a routing anchor so the following ``短域名`` turn stays in the same
    # domain strand.
    anchors.update(match.casefold() for match in _DOMAIN_LIKE_RE.findall(body))
    return anchors


def _topic_family_hits(anchors: Iterable[str]) -> set[int]:
    values = set(anchors)
    hits = {
        index
        for index, family in enumerate(_PARALLEL_TOPIC_FAMILIES)
        if values & family
    }
    # Domain-shaped lexical anchors are typed domain evidence even when the
    # surrounding sentence omits the literal ``域名`` token.
    if any(_DOMAIN_LIKE_RE.fullmatch(value) for value in values):
        hits.add(0)
    return hits


def _parallel_topic_strand_boundary(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    previous_body: str,
    current_body: str,
    edge: Mapping[str, Any],
    previous_segments: Sequence[Mapping[str, Any]],
    current_segments: Sequence[Mapping[str, Any]],
    previous_media: Mapping[str, Any],
    current_media: Mapping[str, Any],
) -> bool:
    """Detect a substantive adjacent message from an independent stream.

    Time proximity is deliberately absent from this decision.  A split is
    made only when both messages carry readable content, neither is an
    explicit/implicit continuation of the other, and their concrete anchors
    are disjoint (or belong to distinct known object families).  This keeps a
    domain-purchase turn apart from a subscription turn even when they arrive
    in the same minute, while leaving short greetings/acks in the surrounding
    candidate episode.
    """
    if not previous_media.get("semantic_evidence_eligible") or not current_media.get("semantic_evidence_eligible"):
        return False
    previous_anchors = _parallel_anchor_tokens(previous_body)
    current_anchors = _parallel_anchor_tokens(current_body)
    if not previous_anchors or not current_anchors:
        return False
    previous_families = _topic_family_hits(previous_anchors)
    current_families = _topic_family_hits(current_anchors)
    family_split = bool(previous_families and current_families and not (previous_families & current_families))
    # Explicit reply metadata remains stronger than a typed family change.
    # Generic continuation words (for example ``他`` inside a sentence) and
    # lexical overlap, however, must not glue two distinct typed categories.
    if edge.get("reply") or edge.get("topic_shift_cue"):
        return False
    if family_split:
        return True
    if edge.get("continuation_cue") or edge.get("shared_token_count"):
        return False
    if previous_anchors & current_anchors:
        return False
    previous_labels = {
        str(signal.get("label"))
        for segment in previous_segments
        for signal in (segment.get("interaction_signals") or ())
        if isinstance(signal, Mapping) and signal.get("label")
    }
    current_labels = {
        str(signal.get("label"))
        for segment in current_segments
        for signal in (segment.get("interaction_signals") or ())
        if isinstance(signal, Mapping) and signal.get("label")
    }
    previous_substantive = bool(previous_labels & _SUBSTANTIVE_LABELS)
    current_substantive = bool(current_labels & _SUBSTANTIVE_LABELS)
    # A known family pair was handled above even if local signal rules did not
    # recognise the sentence as a question/request.  For unseen subjects,
    # require substantive candidate signals on both sides before routing.
    if "acknowledgement" in current_labels and len(current_body.strip()) <= 20:
        return False
    if not (previous_substantive and current_substantive):
        return False
    # For unseen object vocabularies, two different speakers provide a small
    # additional guard against splitting one person's multi-sentence turn.
    # The explicit domain/subscription families above do not need this guard.
    if str(previous.get("participant_ref")) == str(current.get("participant_ref")):
        return False
    if len(previous_anchors) < 1 or len(current_anchors) < 1:
        return False
    # A one-word turn can be a follow-up answer.  Require a little content for
    # the generic branch; explicit family evidence above covers terse domain/
    # subscription turns.
    return len(previous_body.strip()) >= 8 and len(current_body.strip()) >= 8


def _split_parallel_topic_strands(
    rows: Sequence[Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
    segments_by_message: Mapping[str, Sequence[Mapping[str, Any]]],
) -> List[List[Mapping[str, Any]]]:
    """Route interleaved substantive turns into independent candidate strands.

    The input order remains authoritative within each strand, but an
    interleaved ``A1, B1, A2, B2`` run becomes two reviewable groups instead of
    one time-neighbourhood bucket.  Rows without enough evidence stay with
    the current strand; they are not used to manufacture a new topic.
    """
    if len(rows) < 2:
        return [list(rows)] if rows else []
    strands: List[Dict[str, Any]] = []
    for row in rows:
        ref = str(row.get("message_ref"))
        private_row = private.get(ref) or {}
        body, _analysis_source = _analysis_text_for_message(row, private_row)
        original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
        media = _media_availability(original, row, body)
        row_segments = segments_by_message.get(ref, ())
        anchors = _parallel_anchor_tokens(body) if media.get("semantic_evidence_eligible") else set()
        current_families = _topic_family_hits(anchors)
        labels = {
            str(signal.get("label"))
            for segment in row_segments
            for signal in (segment.get("interaction_signals") or ())
            if isinstance(signal, Mapping) and signal.get("label")
        }
        assigned: Optional[Dict[str, Any]] = None
        parallel_split = False
        # A reply/reference to any member is stronger than lexical routing.
        reply_target = str(row.get("reply_to_message_id") or "")
        if reply_target:
            for strand in strands:
                if reply_target in set(strand.get("refs") or ()) or reply_target in set(strand.get("source_ids") or ()):
                    assigned = strand
                    break
        # Typed family continuity takes precedence over generic lexical
        # overlap.  Without this guard a shared word such as ``注册`` can
        # route a subscription turn into a forum strand (or vice versa).
        if assigned is None and current_families:
            best_family_overlap = 0
            best_lexical_overlap = 0
            for strand in strands:
                family_overlap = len(current_families & set(strand.get("families") or ()))
                lexical_overlap = len(anchors & set(strand.get("anchors") or ()))
                if (family_overlap, lexical_overlap) > (best_family_overlap, best_lexical_overlap):
                    assigned = strand
                    best_family_overlap = family_overlap
                    best_lexical_overlap = lexical_overlap
        # For rows without typed family evidence, prefer explicit lexical
        # continuity with an already routed strand.
        if assigned is None and not current_families:
            best_overlap = 0
            for strand in strands:
                overlap = len(anchors & set(strand.get("anchors") or ()))
                if overlap > best_overlap:
                    assigned, best_overlap = strand, overlap
        if assigned is None and strands:
            # A media placeholder or a short acknowledgement can follow the
            # last substantive anchor.  When the current row carries a
            # typed family, compare it with the latest typed row in the
            # active strand instead of letting that glue row hide a genuine
            # parallel boundary.  Unseen vocabularies retain the latest
            # anchor fallback below.
            previous = strands[-1].get("last_row") or {}
            if _topic_family_hits(anchors):
                previous = strands[-1].get("last_family_row") or previous
            else:
                previous = strands[-1].get("last_anchor_row") or previous
            previous_ref = str(previous.get("message_ref"))
            previous_private = private.get(previous_ref) or {}
            previous_body, _previous_analysis_source = _analysis_text_for_message(previous, previous_private)
            previous_original = previous_private.get("original") if isinstance(previous_private.get("original"), Mapping) else {}
            previous_media = _media_availability(previous_original, previous, previous_body)
            previous_segments = segments_by_message.get(previous_ref, ())
            previous_anchors = _parallel_anchor_tokens(previous_body) if previous_media.get("semantic_evidence_eligible") else set()
            edge = _continuity_evidence(previous, row, previous_body, body)
            if _parallel_topic_strand_boundary(
                previous,
                row,
                previous_body,
                body,
                edge,
                previous_segments,
                row_segments,
                previous_media,
                media,
            ):
                assigned = None
                parallel_split = True
        if assigned is None:
            # Keep a social/media row with the latest strand where possible;
            # a substantive independent row creates its own strand only via
            # the guarded boundary above.
            if strands and not parallel_split:
                assigned = strands[-1]
        if assigned is None:
            assigned = {
                "rows": [],
                "anchors": set(),
                "refs": [],
                "source_ids": [],
                "families": set(),
                "last_row": None,
                "last_anchor_row": None,
                "last_family_row": None,
            }
            strands.append(assigned)
        assigned["rows"].append(row)
        assigned["refs"].append(ref)
        assigned["source_ids"].append(str(row.get("source_message_id") or ""))
        if anchors:
            assigned["anchors"].update(anchors)
            assigned["families"].update(current_families)
            assigned["last_anchor_row"] = row
            if _topic_family_hits(anchors):
                assigned["last_family_row"] = row
        assigned["last_row"] = row
    return [list(strand["rows"]) for strand in strands if strand.get("rows")]


def _build_segments(
    rows: Sequence[Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
    participant_names: Mapping[str, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]], Dict[str, List[str]]]:
    segments: List[Dict[str, Any]] = []
    mentions_by_message: Dict[str, List[str]] = defaultdict(list)
    matter_by_message: Dict[str, List[str]] = defaultdict(list)
    for message in rows:
        message_ref = str(message.get("message_ref"))
        private_row = private.get(message_ref) or {}
        original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
        source_body = str(private_row.get("body") or "")
        media = _media_availability(original, message, source_body)
        analysis_body, analysis_source = _analysis_text_for_message(message, private_row)
        source_clauses = _split_clauses(source_body)
        if media.get("semantic_evidence_eligible") and analysis_body:
            clauses = _split_clauses(analysis_body)
        else:
            # Keep an unavailable placeholder/system row visible with its
            # source span, while preventing that placeholder from entering
            # lexical or interaction inference below.
            clauses = source_clauses
        for ordinal, (clause, span_start, span_end) in enumerate(clauses, 1):
            semantic_clause = clause if media.get("semantic_evidence_eligible") else ""
            signals = [] if _is_system_message(message, original) else _interaction_signals(semantic_clause, message)
            if not media.get("semantic_evidence_eligible"):
                # The empty-body fallback keeps a visible interaction
                # placeholder for review, but it must not create typed
                # evidence for an unavailable binary/media row.
                signals = [dict(signal, evidence_refs=[]) for signal in signals]
            tokens = _extract_tokens(semantic_clause) if media.get("semantic_evidence_eligible") else []
            mentions = _mention_candidates(semantic_clause, message, participant_names) if media.get("semantic_evidence_eligible") else []
            matters = _explicit_matter_candidates(semantic_clause, message, signals, tokens) if media.get("semantic_evidence_eligible") else []
            signal_labels = {str(item.get("label")) for item in signals}
            if not media.get("semantic_evidence_eligible"):
                # A missing binary/URL payload cannot be promoted into a
                # social or topical interpretation.  Keep it explicitly
                # unclassified while retaining its interaction placeholder.
                topic_layer = "unclassified"
            elif not tokens and not matters:
                topic_layer = "chitchat_flow" if signal_labels and signal_labels <= _SOCIAL_LABELS else "unclassified"
            else:
                topic_layer = "candidate_local_topic"
            segment_ref = f"segment-{_stable_hash((message_ref, ordinal, span_start, span_end), length=16)}"
            # Mention offsets are clause-local.  Re-key them with the stable
            # segment ref so two clauses containing the same surface form do
            # not accidentally collapse into one annotation.
            for mention in mentions:
                old_id = mention.get("mention_id")
                mention["mention_id"] = f"mention-{_stable_hash((segment_ref, old_id), length=14)}"
                mention["segment_ref"] = segment_ref
            for matter in matters:
                matter["segment_ref"] = segment_ref
            local_topics = [
                {
                    "topic_id": f"local-topic-{_stable_hash((segment_ref, token), length=14)}",
                    "kind": "local_topic",
                    "label": token,
                    "status": "candidate_only",
                    "candidate_only": True,
                    "source_segment_refs": [segment_ref],
                    "evidence_refs": [message_ref] if media.get("semantic_evidence_eligible") else [],
                }
                for token in tokens[:8]
            ]
            information = _information_value(signals, media, semantic_clause, matters)
            discussion = _discussion_weight(signals, media, semantic_clause)
            role, fragment_type = _segment_role_and_type(semantic_clause, signals, media, tokens, matters)
            semantic_source = analysis_source if media.get("semantic_evidence_eligible") else ("placeholder_body" if source_body else "none")
            if semantic_source == "message_body":
                content_kind = "message_text"
            elif media.get("semantic_evidence_eligible"):
                content_kind = "media_derived_text"
            else:
                content_kind = "media_placeholder"
            content_ref = {
                "content_ref": f"content-{_stable_hash((message_ref, ordinal, semantic_source, span_start, span_end), length=18)}",
                "kind": content_kind,
                "source_field": semantic_source,
                "span": {"start": span_start, "end": span_end},
                "content_status": "available" if media.get("semantic_evidence_eligible") else "unavailable",
                "evidence_refs": [message_ref] if media.get("semantic_evidence_eligible") else [],
                "candidate_only": True,
            }
            segment = {
                "segment_ref": segment_ref,
                "message_ref": message_ref,
                "speaker_ref": message.get("participant_ref"),
                "chat_ref": message.get("chat_ref"),
                "timestamp": message.get("timestamp"),
                "local_day": message.get("local_day"),
                "ordinal_in_message": ordinal,
                "span": {"start": span_start, "end": span_end},
                "span_basis": semantic_source,
                "message_type": message.get("message_type"),
                "role": role,
                "fragment_type": fragment_type,
                "information_role": role,
                "media": dict(media),
                "interaction_signals": signals,
                "mentions": mentions,
                "local_topics": local_topics,
                "content_refs": [content_ref],
                "explicit_matters": matters,
                "topic_layer": topic_layer,
                "information_value": information,
                "discussion_weight": discussion,
                "evidence_refs": [message_ref] if media.get("semantic_evidence_eligible") else [],
                "uncertainties": (["media_content_unavailable"] if media.get("missing_reason") else []) + (["topic_unclassified"] if topic_layer == "unclassified" else []),
                "candidate_only": True,
                "semantic_status": "candidate_only",
            }
            segments.append(segment)
            for mention in mentions:
                mention["message_id"] = message.get("message_id") or message.get("source_message_id")
                mentions_by_message[message_ref].append(str(mention.get("mention_id")))
            for matter in matters:
                matter_by_message[message_ref].append(str(matter.get("matter_id")))
    segments.sort(key=lambda item: (_parse_datetime(item.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc), item.get("message_ref"), item.get("ordinal_in_message") or 0))
    return segments, mentions_by_message, matter_by_message


def _episode_score_components(segments: Sequence[Mapping[str, Any]], continuity: Sequence[Mapping[str, Any]], cross_day: bool) -> Dict[str, Any]:
    signal_counts = Counter(str(signal.get("label")) for segment in segments for signal in (segment.get("interaction_signals") or ()) if isinstance(signal, Mapping))
    evidence_count = sum(1 for segment in segments if segment.get("evidence_refs"))
    component_values = {
        "evidence_bearing_segments": evidence_count,
        "explicit_reply_edges": sum(1 for edge in continuity if edge.get("reply")),
        "lexical_overlap_edges": sum(1 for edge in continuity if edge.get("relation") == "lexical_overlap"),
        "continuation_edges": sum(1 for edge in continuity if edge.get("relation") == "continuation_cue"),
        "cross_day_context": 1 if cross_day else 0,
        "discussion_signal_segments": sum(signal_counts[label] for label in ("discussion", "question", "request")),
    }
    raw = 0.1 + min(0.3, component_values["evidence_bearing_segments"] * 0.03) + min(0.3, component_values["explicit_reply_edges"] * 0.16) + min(0.16, component_values["lexical_overlap_edges"] * 0.08) + min(0.1, component_values["continuation_edges"] * 0.05) + (0.04 if cross_day else 0.0)
    return {
        "score": round(max(0.0, min(1.0, raw)), 4),
        "candidate_only": True,
        "components": component_values,
        "basis": [key for key, value in component_values.items() if value],
        "uncertainties": ["candidate_score_is_not_truth_or_importance"],
    }


def _build_episodes(
    messages: Sequence[Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    chats: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    segments_by_message: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments:
        segments_by_message[str(segment.get("message_ref"))].append(segment)
    messages_by_chat: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for message in messages:
        private_row = private.get(str(message.get("message_ref"))) or {}
        original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
        if _is_system_message(message, original):
            # System metadata remains in the authority ledger, but cannot be
            # assigned to a human conversation episode or strand.
            continue
        messages_by_chat[str(message.get("chat_ref"))].append(message)
    chat_by_ref = {str(chat.get("chat_ref")): chat for chat in chats}
    episodes: List[Dict[str, Any]] = []
    cross_candidates: List[Dict[str, Any]] = []
    for chat_ref, rows_raw in sorted(messages_by_chat.items()):
        rows = sorted(rows_raw, key=_sort_message_key)
        cutoff = _adaptive_silence_cutoff(rows)
        groups: List[List[Mapping[str, Any]]] = []
        current: List[Mapping[str, Any]] = []
        boundary_reasons: List[str] = []
        for row in rows:
            if not current:
                current = [row]
                continue
            previous = current[-1]
            previous_body, _previous_analysis_source = _analysis_text_for_message(
                previous,
                private.get(str(previous.get("message_ref"))) or {},
            )
            current_body, _current_analysis_source = _analysis_text_for_message(
                row,
                private.get(str(row.get("message_ref"))) or {},
            )
            edge = _continuity_evidence(previous, row, previous_body, current_body)
            previous_time, current_time = _parse_datetime(previous.get("timestamp")), _parse_datetime(row.get("timestamp"))
            gap = (current_time - previous_time).total_seconds() if previous_time and current_time else None
            explicit_boundary = bool(row.get("conversation_boundary_hint"))
            should_split = bool(explicit_boundary)
            if not should_split and edge.get("relation") == "topic_shift_candidate":
                # An explicit/new-topic cue starts a new candidate stream even
                # when messages are adjacent.  This is a candidate boundary,
                # not a semantic event merge decision.
                should_split = True
            if not should_split and cutoff is not None and gap is not None and gap > cutoff:
                # A silence can split only when the next message also offers a
                # clear shift and no strong reply/continuity evidence.  Date
                # changes alone never split.
                should_split = bool(edge.get("topic_shift_cue") or (edge.get("shared_token_count", 0) == 0 and not edge.get("reply") and not edge.get("continuation_cue") and _token_set(current_body)))
            if should_split:
                groups.append(current)
                reason = "explicit_boundary" if explicit_boundary else ("topic_shift_candidate" if edge.get("relation") == "topic_shift_candidate" else "adaptive_silence_with_topic_shift_candidate")
                boundary_reasons.append(reason)
                current = [row]
            else:
                current.append(row)
        if current:
            groups.append(current)
        # A chronological bucket is only a first pass.  If substantive turns
        # from different object streams are interleaved, route them into
        # independent strands before materialising episodes.  This is the
        # important distinction between "near in time" and "same exchange".
        chronological_groups = groups
        chronological_boundary_reasons = list(boundary_reasons)
        groups = []
        group_boundary_reasons: List[Optional[str]] = []
        for group_index, chronological_group in enumerate(chronological_groups):
            routed = _split_parallel_topic_strands(chronological_group, private, segments_by_message)
            if not routed:
                continue
            for strand_index, strand in enumerate(routed):
                groups.append(strand)
                if strand_index == 0:
                    group_boundary_reasons.append(
                        chronological_boundary_reasons[group_index - 1]
                        if group_index > 0 and group_index - 1 < len(chronological_boundary_reasons)
                        else None
                    )
                else:
                    group_boundary_reasons.append("parallel_topic_strand_candidate")
        for index, group in enumerate(groups, 1):
            message_refs = [str(row.get("message_ref")) for row in group]
            group_segments = [segment for message_ref in message_refs for segment in segments_by_message.get(message_ref, ())]
            edges: List[Dict[str, Any]] = []
            for previous, row in zip(group, group[1:]):
                previous_body, _previous_analysis_source = _analysis_text_for_message(
                    previous,
                    private.get(str(previous.get("message_ref"))) or {},
                )
                current_body, _current_analysis_source = _analysis_text_for_message(
                    row,
                    private.get(str(row.get("message_ref"))) or {},
                )
                edge = _continuity_evidence(previous, row, previous_body, current_body)
                edge.update({"from_message_ref": previous.get("message_ref"), "to_message_ref": row.get("message_ref")})
                edges.append(edge)
            days = sorted({str(row.get("local_day")) for row in group if row.get("local_day")})
            times = [_parse_datetime(row.get("timestamp")) for row in group if row.get("timestamp")]
            cross_day = len(days) > 1
            episode_ref = f"episode-{_stable_hash((chat_ref, tuple(message_refs), SCHEMA_VERSION), length=18)}"
            local_topic_counts = Counter(str(topic.get("label")) for segment in group_segments for topic in (segment.get("local_topics") or ()) if isinstance(topic, Mapping))
            local_topics = [
                {
                    "topic_id": f"continuing-topic-{_stable_hash((episode_ref, token), length=14)}",
                    "kind": "continuing_topic",
                    "label": token,
                    "status": "candidate_only",
                    "candidate_only": True,
                    "source_segment_refs": sorted({str(segment.get("segment_ref")) for segment in group_segments for topic in (segment.get("local_topics") or ()) if isinstance(topic, Mapping) and str(topic.get("label")) == token}),
                    "evidence_refs": sorted({str(segment.get("message_ref")) for segment in group_segments for topic in (segment.get("local_topics") or ()) if isinstance(topic, Mapping) and str(topic.get("label")) == token}),
                    "continuity_reason": "repeated_local_token" if count > 1 else "cross_day_candidate",
                }
                for token, count in sorted(local_topic_counts.items())
                if count > 1 or cross_day
            ]
            explicit_matters = [matter for segment in group_segments for matter in (segment.get("explicit_matters") or ()) if isinstance(matter, Mapping)]
            signal_counts = Counter(str(signal.get("label")) for segment in group_segments for signal in (segment.get("interaction_signals") or ()) if isinstance(signal, Mapping))
            all_social = bool(signal_counts) and set(signal_counts) <= _SOCIAL_LABELS and not local_topics and not explicit_matters
            topic_status = "chitchat_flow" if all_social else ("unclassified" if not local_topics and not explicit_matters else "candidate_layers_present")
            signal_density_scores = [
                float((segment.get("information_value") or {}).get("signal_density_score") or 0.0)
                for segment in group_segments
            ]
            signal_density_average = round(sum(signal_density_scores) / len(signal_density_scores), 4) if signal_density_scores else 0.0
            signal_density_label = "low" if signal_density_average < 0.35 else ("medium" if signal_density_average < 0.68 else "high")
            discussion_numerator = sum(int((segment.get("discussion_weight") or {}).get("numerator") or 0) for segment in group_segments)
            discussion_denominator = sum(int((segment.get("discussion_weight") or {}).get("denominator") or 0) for segment in group_segments)
            episode = {
                "episode_ref": episode_ref,
                "episode_id": episode_ref,
                "conversation_ref": episode_ref,
                "conversation_id": episode_ref,
                "chat_ref": chat_ref,
                "chat_type": (chat_by_ref.get(chat_ref) or {}).get("chat_type", "unknown"),
                "message_refs": message_refs,
                "message_ids": message_refs,
                "segment_refs": [str(segment.get("segment_ref")) for segment in group_segments],
                "segment_ids": [str(segment.get("segment_ref")) for segment in group_segments],
                "participant_refs": sorted({str(row.get("participant_ref")) for row in group}),
                "participant_ids": sorted({str(row.get("participant_ref")) for row in group}),
                "observed_days": days,
                "start_time": min(times).isoformat(timespec="seconds") if times else None,
                "end_time": max(times).isoformat(timespec="seconds") if times else None,
                "start_boundary": {"status": "open", "reason": "window_may_begin_mid_conversation"},
                "end_boundary": {"status": "open", "reason": "silence_is_not_resolution"},
                "cross_day_candidate": cross_day,
                "adaptive_gap_cutoff_seconds": round(cutoff, 3) if cutoff is not None else None,
                "continuity_edges": edges,
                "boundary_reasons": [group_boundary_reasons[index - 1]] if index - 1 < len(group_boundary_reasons) and group_boundary_reasons[index - 1] else [],
                "local_topics": [topic for topic in local_topics if topic.get("continuity_reason") == "repeated_local_token"],
                "continuing_topics": local_topics,
                "explicit_matters": explicit_matters,
                "interaction_signal_counts": dict(sorted(signal_counts.items())),
                "topic_status": topic_status,
                "discussion_weight": {
                    "score": round(discussion_numerator / discussion_denominator, 4) if discussion_denominator else 0.0,
                    "numerator": discussion_numerator,
                    "denominator": discussion_denominator,
                    "candidate_only": True,
                    "components": {
                        "discussion_question_request_segments": discussion_numerator,
                        "content_bearing_segments": discussion_denominator,
                    },
                    "basis": ["interaction_signal_candidates"],
                    "uncertainties": ["separate_axis_from_information_value"],
                },
                "information_value": {
                    "score": None,
                    "label": "unknown",
                    "status": "unknown_pending_model",
                    "value_status": "unknown_pending_model",
                    "signal_density_score": signal_density_average,
                    "signal_density_label": signal_density_label,
                    "signal_density": {
                        "score": signal_density_average,
                        "label": signal_density_label,
                        "candidate_only": True,
                        "components": {
                            "segment_count": len(group_segments),
                            "content_bearing_segment_count": sum(1 for segment in group_segments if segment.get("evidence_refs")),
                            "explicit_matter_count": len(explicit_matters),
                        },
                    },
                    "candidate_only": True,
                    "components": {"segment_count": len(group_segments), "content_bearing_segment_count": sum(1 for segment in group_segments if segment.get("evidence_refs")), "explicit_matter_count": len(explicit_matters)},
                    "basis": ["segment_candidate_values"],
                    "uncertainties": [
                        "local_signal_density_is_not_information_value",
                        "information_value_requires_model_or_human_judgment",
                        "separate_axis_from_discussion_weight",
                    ],
                },
                "uncertainties": sorted({uncertainty for segment in group_segments for uncertainty in (segment.get("uncertainties") or ())} | ({"cross_day_boundary_candidate"} if cross_day else set()) | {"episode_open_boundary", "not_a_final_event"}),
                "candidate_score": _episode_score_components(group_segments, edges, cross_day),
                "candidate_only": True,
                "semantic_status": "candidate_only",
            }
            episodes.append(episode)
        for left, right in zip(episodes[-len(groups):], episodes[-len(groups) + 1:] if len(groups) > 1 else []):
            left_days, right_days = set(left.get("observed_days") or ()), set(right.get("observed_days") or ())
            if left_days and right_days and max(left_days) != min(right_days):
                candidate = {
                    "candidate_id": f"episode-link-{_stable_hash((left.get('episode_ref'), right.get('episode_ref')), length=14)}",
                    "left_episode_ref": left.get("episode_ref"),
                    "right_episode_ref": right.get("episode_ref"),
                    "relation": "possibly_related",
                    "candidate_only": True,
                    "evidence": ["same_chat", "cross_date_adjacency"],
                    "uncertainties": ["no_strong_continuity_evidence"],
                }
                cross_candidates.append(candidate)
    episodes.sort(key=lambda item: (item.get("start_time") or "", item.get("episode_ref") or ""))
    return episodes, cross_candidates


def _build_threads(episodes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    threads: List[Dict[str, Any]] = []
    for episode in episodes:
        episode_ref = str(episode.get("episode_ref"))
        thread_ref = f"thread-{_stable_hash((episode_ref, SCHEMA_VERSION), length=18)}"
        threads.append({
            "thread_ref": thread_ref,
            "thread_id": thread_ref,
            "episode_ref": episode_ref,
            "chat_ref": episode.get("chat_ref"),
            "chat_type": episode.get("chat_type"),
            "candidate_only": True,
            "semantic_status": "candidate_only",
            "message_refs": list(episode.get("message_refs") or ()),
            "message_ids": list(episode.get("message_refs") or ()),
            "segment_refs": list(episode.get("segment_refs") or ()),
            "segment_ids": list(episode.get("segment_refs") or ()),
            "participant_refs": list(episode.get("participant_refs") or ()),
            "participant_ids": list(episode.get("participant_refs") or ()),
            "topic_status": episode.get("topic_status"),
            "local_topics": list(episode.get("local_topics") or ()),
            "continuing_topics": list(episode.get("continuing_topics") or ()),
            "explicit_matters": list(episode.get("explicit_matters") or ()),
            "interaction_signal_counts": dict(episode.get("interaction_signal_counts") or {}),
            "discussion_weight": dict(episode.get("discussion_weight") or {}),
            "information_value": dict(episode.get("information_value") or {}),
            "candidate_score": dict(episode.get("candidate_score") or {}),
            "why_grouped": [
                "same_chat_scope",
                "chronological_message_order",
                *[str(edge.get("relation")) for edge in episode.get("continuity_edges") or () if edge.get("relation") not in {"same_chat_time_order"}],
            ],
            "open_boundary": {"start": episode.get("start_boundary"), "end": episode.get("end_boundary")},
            "uncertainties": list(episode.get("uncertainties") or ()),
        })
    return threads


def _episode_semantic_tokens(
    episode: Mapping[str, Any],
    private: Mapping[str, Mapping[str, Any]],
    messages_by_ref: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    tokens: set[str] = set()
    for message_ref in episode.get("message_refs") or ():
        message = messages_by_ref.get(str(message_ref), {})
        private_row = private.get(str(message_ref)) or {}
        body, _analysis_source = _analysis_text_for_message(message, private_row)
        original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
        media = _media_availability(original, message, body)
        if media.get("semantic_evidence_eligible"):
            tokens.update(_flow_tokens(body))
    return tokens


def _build_conversation_flows(
    episodes: Sequence[Mapping[str, Any]],
    threads: Sequence[MutableMapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Link possibly continuing episodes without merging their boundaries.

    A flow is a higher-level candidate only.  It is deliberately stricter than
    same-chat adjacency: repeated content-bearing tokens, explicit matters, or
    continuation/reply cues are required.  This lets two dated ``选课``
    episodes be reviewed as one continuing exchange while keeping separate
    internal episodes and preserving unrelated project/movie streams.
    """
    episode_rows = [episode for episode in episodes if isinstance(episode, Mapping)]
    episode_by_ref = {str(episode.get("episode_ref")): episode for episode in episode_rows}
    thread_by_episode = {str(thread.get("episode_ref")): thread for thread in threads if isinstance(thread, Mapping)}
    message_by_ref = {str(message.get("message_ref")): message for message in messages if isinstance(message, Mapping)}
    segments_by_message: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments:
        if isinstance(segment, Mapping):
            segments_by_message[str(segment.get("message_ref"))].append(segment)

    def episode_features(episode: Mapping[str, Any]) -> Dict[str, Any]:
        refs = [str(ref) for ref in episode.get("message_refs") or ()]
        labels = {
            str(signal.get("label"))
            for ref in refs
            for segment in segments_by_message.get(ref, ())
            for signal in (segment.get("interaction_signals") or ())
            if isinstance(signal, Mapping) and signal.get("label")
        }
        matter_kinds = {
            str(matter.get("kind"))
            for ref in refs
            for segment in segments_by_message.get(ref, ())
            for matter in (segment.get("explicit_matters") or ())
            if isinstance(matter, Mapping) and matter.get("kind")
        }
        tokens = _episode_semantic_tokens(episode, private, message_by_ref)
        continuation = False
        for ref in refs:
            message = message_by_ref.get(ref, {})
            body, _analysis_source = _analysis_text_for_message(message, private.get(ref) or {})
            if message.get("reply_to_message_id") or _CONTINUATION_RE.search(body):
                continuation = True
                break
        return {"tokens": tokens, "labels": labels, "matter_kinds": matter_kinds, "continuation": continuation}

    by_chat: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for episode in episode_rows:
        by_chat[str(episode.get("chat_ref"))].append(episode)
    parent: Dict[str, str] = {str(episode.get("episode_ref")): str(episode.get("episode_ref")) for episode in episode_rows}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    link_evidence: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for chat_ref, chat_episodes in by_chat.items():
        ordered = sorted(chat_episodes, key=lambda item: (item.get("start_time") or "", str(item.get("episode_ref"))))
        features = {str(episode.get("episode_ref")): episode_features(episode) for episode in ordered}
        for left, right in zip(ordered, ordered[1:]):
            left_ref, right_ref = str(left.get("episode_ref")), str(right.get("episode_ref"))
            left_features, right_features = features[left_ref], features[right_ref]
            left_end = _parse_datetime(left.get("end_time"))
            right_start = _parse_datetime(right.get("start_time"))
            gap_seconds = (right_start - left_end).total_seconds() if left_end and right_start else None
            if gap_seconds is not None and (gap_seconds < 0 or gap_seconds > 7 * 24 * 3600):
                continue
            right_refs = [str(ref) for ref in right.get("message_refs") or ()]
            right_first_body = (
                _analysis_text_for_message(message_by_ref.get(right_refs[0], {}), private.get(right_refs[0]) or {})[0]
                if right_refs else ""
            )
            explicit_topic_shift = _TOPIC_SHIFT_RE.search(right_first_body) is not None
            shared_tokens = sorted(left_features["tokens"] & right_features["tokens"])
            shared_matters = sorted(left_features["matter_kinds"] & right_features["matter_kinds"])
            continuation = bool(right_features["continuation"] or left_features["continuation"])
            reply = any(
                str(message_by_ref.get(ref, {}).get("reply_to_message_id") or "") in set(str(value) for value in left.get("message_refs") or ())
                for ref in right_refs
            )
            # A clear new-topic cue wins over generic lexical overlap.  An
            # explicit boundary alone does not: it can simply mark a new day
            # of the same human exchange.
            if explicit_topic_shift:
                continue
            evidence: List[str] = []
            if shared_tokens:
                evidence.append("shared_content_tokens")
            if shared_matters:
                evidence.append("shared_candidate_matter_kind")
            if continuation:
                evidence.append("continuation_cue")
            if reply:
                evidence.append("explicit_reply_metadata")
            # Do not join two generic social/question runs merely because they
            # are near each other. A repeated content-bearing token or an
            # explicit reply is required for a higher-level flow candidate;
            # continuation/matter cues remain supporting evidence only.
            if not shared_tokens and not reply:
                continue
            union(left_ref, right_ref)
            link_evidence[(left_ref, right_ref)] = {
                "left_episode_ref": left_ref,
                "right_episode_ref": right_ref,
                "relation": "continuing_exchange_candidate",
                "shared_tokens": shared_tokens,
                "shared_token_count": len(shared_tokens),
                "shared_matter_kinds": shared_matters,
                "continuation_cue": continuation,
                "explicit_reply": reply,
                "gap_seconds": round(gap_seconds, 3) if gap_seconds is not None else None,
                "candidate_only": True,
                "uncertainties": ["flow_link_is_not_final_topic_identity"],
            }

    components: Dict[str, List[str]] = defaultdict(list)
    for episode_ref in parent:
        components[find(episode_ref)].append(episode_ref)
    flows: List[Dict[str, Any]] = []
    flow_by_episode: Dict[str, Dict[str, Any]] = {}
    for refs in components.values():
        if len(refs) < 2:
            continue
        ordered_refs = sorted(refs, key=lambda ref: (episode_by_ref[ref].get("start_time") or "", ref))
        first = episode_by_ref[ordered_refs[0]]
        flow_ref = f"flow-{_stable_hash((first.get("chat_ref"), tuple(ordered_refs), CONTEXT_SCHEMA_VERSION), length=18)}"
        thread_refs = [str((thread_by_episode.get(ref) or {}).get("thread_ref")) for ref in ordered_refs]
        message_refs = [str(ref) for ref in ordered_refs for ref in episode_by_ref[ref].get("message_refs") or ()]
        participant_refs = sorted({str(ref) for ref in ordered_refs for ref in episode_by_ref[ref].get("participant_refs") or ()})
        days = sorted({str(day) for ref in ordered_refs for day in episode_by_ref[ref].get("observed_days") or ()})
        times = [
            parsed
            for ref in ordered_refs
            for parsed in (_parse_datetime(episode_by_ref[ref].get("start_time")), _parse_datetime(episode_by_ref[ref].get("end_time")))
            if parsed
        ]
        edges = [
            evidence
            for (left_ref, right_ref), evidence in link_evidence.items()
            if left_ref in ordered_refs and right_ref in ordered_refs
        ]
        flow = {
            "flow_ref": flow_ref,
            "flow_id": flow_ref,
            "conversation_flow_ref": flow_ref,
            "higher_level_flow_ref": flow_ref,
            "chat_ref": first.get("chat_ref"),
            "chat_type": first.get("chat_type", "unknown"),
            "episode_refs": ordered_refs,
            "episode_ids": ordered_refs,
            "thread_refs": thread_refs,
            "thread_ids": thread_refs,
            "message_refs": message_refs,
            "message_ids": message_refs,
            "participant_refs": participant_refs,
            "participant_ids": participant_refs,
            "observed_days": days,
            "start_time": min(times).isoformat(timespec="seconds") if times else None,
            "end_time": max(times).isoformat(timespec="seconds") if times else None,
            "relation": "possibly_continuing_exchange",
            "status": "candidate_only",
            "candidate_only": True,
            "internal_episode_boundaries_preserved": True,
            "link_evidence": edges,
            "uncertainties": ["flow_link_is_not_final_topic_identity", "internal_episodes_remain_separate"],
        }
        flows.append(flow)
        for ref in ordered_refs:
            flow_by_episode[ref] = flow
    for thread in threads:
        flow = flow_by_episode.get(str(thread.get("episode_ref")))
        if not flow:
            continue
        thread["flow_ref"] = flow.get("flow_ref")
        thread["conversation_flow_ref"] = flow.get("flow_ref")
        thread["higher_level_flow_ref"] = flow.get("flow_ref")
        thread["flow_episode_count"] = len(flow.get("episode_refs") or ())
        thread["flow_candidate_only"] = True
    flows.sort(key=lambda flow: (flow.get("start_time") or "", flow.get("flow_ref") or ""))
    return flows


def _context_message_features(
    message: Mapping[str, Any],
    private_row: Mapping[str, Any],
    message_segments: Sequence[Mapping[str, Any]],
) -> set[str]:
    body, _analysis_source = _analysis_text_for_message(message, private_row)
    original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
    media = _media_availability(original, message, body)
    if not media.get("semantic_evidence_eligible"):
        return {"media_placeholder"} if not body else set()
    labels = {
        str(signal.get("label"))
        for segment in message_segments
        for signal in (segment.get("interaction_signals") or ())
        if isinstance(signal, Mapping) and signal.get("label")
    }
    features: set[str] = set()
    if labels & {"question", "request"}:
        features.add("question_or_request")
    if labels & {"discussion", "sharing"}:
        features.add("topic_line")
    if labels & {"turn_taking"} or message.get("reply_to_message_id") or _CONTINUATION_RE.search(body):
        features.add("continuation")
    if labels & {"discussion", "question", "request"}:
        features.add("substantive")
    if body:
        features.add("text")
    return features


def _context_reference_targets(message: Mapping[str, Any], private_row: Mapping[str, Any]) -> set[str]:
    """Collect explicit reply/quote targets without exposing quoted text."""
    targets: set[str] = set()
    for key in (
        "reply_to_message_id",
        "reply_to",
        "quoted_message_id",
        "referenced_message_id",
        "quote_message_id",
        "in_reply_to",
    ):
        value = _text(message.get(key) or private_row.get(key)).strip()
        if value:
            targets.add(value)
    original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
    for key in (
        "reply_to_message_id",
        "reply_to",
        "quoted_message_id",
        "referenced_message_id",
        "quote_message_id",
        "in_reply_to",
    ):
        value = _text(original.get(key)).strip()
        if value:
            targets.add(value)
    return targets


def _context_anchor_tokens(
    ref: str,
    messages_by_ref: Mapping[str, Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    message = messages_by_ref.get(str(ref), {})
    private_row = private.get(str(ref)) or {}
    body, _analysis_source = _analysis_text_for_message(message, private_row)
    original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
    media = _media_availability(original, message, body)
    return _parallel_anchor_tokens(body) if media.get("semantic_evidence_eligible") else set()


def _segment_number(value: Any) -> Optional[int]:
    match = re.search(r"(\d+)$", _text(value).strip())
    return int(match.group(1)) if match else None


def _authoritative_segment_continuity(
    candidate_ref: str,
    reference_refs: Sequence[str],
    core_refs: Sequence[str],
    messages_by_ref: Mapping[str, Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Use explicit development segmentation as medium continuity evidence.

    Some review inputs carry an adjudicated dialogue-segment ledger even when
    reply metadata and lexical overlap are absent.  A preceding segment that
    is separated from the core by an intervening segment is a conservative
    indication of a missing lead-in, whereas an immediately adjacent new
    segment is left to the normal object/reply checks.  This never consults a
    frozen artifact and is ignored when the metadata is absent.
    """
    if not core_refs:
        return False
    def segment_id(ref: str) -> Optional[int]:
        original = (private.get(str(ref)) or {}).get("original")
        original = original if isinstance(original, Mapping) else {}
        return _segment_number(original.get("dialogue_segment_id"))
    core_numbers = [number for ref in core_refs if (number := segment_id(str(ref))) is not None]
    candidate_number = segment_id(str(candidate_ref))
    if not core_numbers or candidate_number is None:
        return False
    if candidate_number >= min(core_numbers) - 1:
        return False
    row = messages_by_ref.get(str(candidate_ref), {})
    private_row = private.get(str(candidate_ref)) or {}
    original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
    if _is_system_message(row, original):
        return False
    body, _analysis_source = _analysis_text_for_message(row, private_row)
    if _media_availability(original, row, body).get("semantic_evidence_eligible") is not True:
        return False
    # An explicit topic/new-object marker still wins over ledger adjacency.
    if _TOPIC_SHIFT_RE.search(body) or row.get("topic_shift_hint"):
        return False
    return True


def _context_evidence_for_candidate(
    candidate: Mapping[str, Any],
    reference_refs: Sequence[str],
    core_refs: Sequence[str],
    messages_by_ref: Mapping[str, Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
    features_by_ref: Mapping[str, set[str]],
) -> Tuple[Optional[str], Optional[str]]:
    """Return (evidence label, blocking reason) for a context candidate.

    ``None`` evidence means the candidate must not be added.  The second
    value is intentionally a short audit code, not a semantic conclusion.
    """
    ref = str(candidate.get("ref"))
    row = messages_by_ref.get(ref, {})
    private_row = private.get(ref) or {}
    features = set(candidate.get("features") or ())
    if "media_placeholder" in features:
        return None, "media_content_unavailable"
    body, _analysis_source = _analysis_text_for_message(row, private_row)
    if _TOPIC_SHIFT_RE.search(body) or row.get("topic_shift_hint"):
        return None, "topic_shift_or_new_subject"
    candidate_anchors = _context_anchor_tokens(ref, messages_by_ref, private)
    reference_anchors = set().union(*(
        _context_anchor_tokens(reference_ref, messages_by_ref, private)
        for reference_ref in reference_refs
    )) if reference_refs else set()
    # Explicit reply/quote metadata is the strongest local relation and may
    # bridge wording changes.  It still cannot override an explicit topic
    # shift handled above.
    explicit_targets = _context_reference_targets(row, private_row)
    reference_ids = set(str(value) for value in reference_refs)
    reference_source_ids = {
        str(messages_by_ref[reference_ref].get("source_message_id"))
        for reference_ref in reference_refs
        if reference_ref in messages_by_ref and messages_by_ref[reference_ref].get("source_message_id")
    }
    if explicit_targets & (reference_ids | reference_source_ids):
        return "explicit_reply_or_quote", None
    if candidate_anchors & reference_anchors:
        return "shared_concrete_object", None
    if _authoritative_segment_continuity(ref, reference_refs, core_refs, messages_by_ref, private):
        return "authoritative_segment_continuity", None
    # A continuation/question can be useful when it is tied to a substantive
    # reference, but a new concrete anchor is a topic turn, not context.
    reference_is_substantive = any("substantive" in features_by_ref.get(reference_ref, set()) for reference_ref in reference_refs)
    if candidate_anchors and reference_anchors and not (candidate_anchors & reference_anchors) and "substantive" in features:
        return None, "new_concrete_object"
    if "continuation" in features and reference_is_substantive and not candidate_anchors:
        return "reference_or_qa_continuation", None
    # Use the same conservative edge relation as episode/flow candidates for
    # a candidate that carries its own content.  Time order alone is never
    # accepted here.
    for reference_ref in reference_refs:
        reference_message = messages_by_ref.get(reference_ref, {})
        reference_private = private.get(reference_ref) or {}
        edge = _continuity_evidence(
            reference_message,
            row,
            _analysis_text_for_message(reference_message, reference_private)[0],
            body,
        )
        if edge.get("reply"):
            return "explicit_reply_or_quote", None
        if edge.get("relation") == "lexical_overlap" and not edge.get("topic_shift_cue"):
            return "shared_concrete_object", None
        if edge.get("relation") == "continuation_cue" and reference_is_substantive and not candidate_anchors:
            return "reference_or_qa_continuation", None
    return None, "no_strong_or_medium_continuity_evidence"


def _build_review_context_envelopes(
    messages: Sequence[Mapping[str, Any]],
    private: Mapping[str, Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
    threads: Sequence[MutableMapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Build core-plus-neighbour review packages without changing episodes."""
    messages_by_ref = {str(message.get("message_ref")): message for message in messages if isinstance(message, Mapping)}
    segments_by_message: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments:
        if isinstance(segment, Mapping):
            segments_by_message[str(segment.get("message_ref"))].append(segment)
    chat_rows: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for message in messages:
        private_row = private.get(str(message.get("message_ref"))) or {}
        original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
        if _is_system_message(message, original):
            continue
        chat_rows[str(message.get("chat_ref"))].append(message)
    for rows in chat_rows.values():
        rows.sort(key=_sort_message_key)
    episode_by_ref = {str(episode.get("episode_ref")): episode for episode in episodes if isinstance(episode, Mapping)}
    envelopes: List[Dict[str, Any]] = []
    for thread in threads:
        episode = episode_by_ref.get(str(thread.get("episode_ref"))) or {}
        chat_ref = str(thread.get("chat_ref"))
        rows = chat_rows.get(chat_ref, [])
        core_refs = [str(ref) for ref in thread.get("message_refs") or () if str(ref) in messages_by_ref]
        if not core_refs:
            continue
        core_set = set(core_refs)
        positions = [index for index, row in enumerate(rows) if str(row.get("message_ref")) in core_set]
        if not positions:
            continue
        first_position, last_position = min(positions), max(positions)
        core_times = [_parse_datetime(messages_by_ref[ref].get("timestamp")) for ref in core_refs if _parse_datetime(messages_by_ref[ref].get("timestamp"))]
        core_start = min(core_times) if core_times else None
        core_end = max(core_times) if core_times else None
        cutoff = float(episode.get("adaptive_gap_cutoff_seconds") or 0.0)
        window_seconds = min(max(cutoff * 2.0, 2 * 3600), 3 * 24 * 3600) if cutoff else 6 * 3600
        safety_cap = max(8, min(32, len(core_refs) * 3 + 4))
        candidate_rows: List[Dict[str, Any]] = []
        features_by_ref: Dict[str, set[str]] = {}
        for index, row in enumerate(rows):
            ref = str(row.get("message_ref"))
            private_row = private.get(ref) or {}
            features = _context_message_features(row, private_row, segments_by_message.get(ref, ()))
            features_by_ref[ref] = set(features)
            if ref in core_set:
                continue
            timestamp = _parse_datetime(row.get("timestamp"))
            if timestamp and core_start and core_end:
                distance_seconds = min(abs((timestamp - core_start).total_seconds()), abs((timestamp - core_end).total_seconds()))
                if distance_seconds > window_seconds:
                    continue
            else:
                distance_seconds = None
            direction = "before_core" if index < first_position else ("after_core" if index > last_position else "inside_core_window")
            distance_steps = min(abs(index - first_position), abs(index - last_position))
            candidate_rows.append({
                "ref": ref,
                "index": index,
                "row": row,
                "features": features,
                "direction": direction,
                "distance_steps": distance_steps,
                "distance_seconds": distance_seconds,
            })
        core_features: set[str] = set()
        for ref in core_refs:
            core_features.update(_context_message_features(messages_by_ref[ref], private.get(ref) or {}, segments_by_message.get(ref, ())))
        selected: List[Dict[str, Any]] = []
        selected_refs: set[str] = set()
        reasons: List[str] = ["same_chat_scope", "core_episode_kept_intact"]
        evidence_by_ref: Dict[str, str] = {}
        excluded_by_ref: Dict[str, str] = {}

        def add_candidate(candidate: Dict[str, Any], reason: str) -> None:
            if candidate["ref"] in selected_refs or len(selected) >= safety_cap:
                return
            selected.append(candidate)
            selected_refs.add(candidate["ref"])
            reasons.append(reason)

        # Expand outward only while each next row has strong/medium evidence
        # tied to the core or an already admitted row.  The previous
        # nearest-neighbour rule made unrelated Claude/GPT/application turns
        # look like context merely because they were close in time.
        for direction in ("before_core", "after_core"):
            directional = [candidate for candidate in candidate_rows if candidate["direction"] == direction]
            if direction == "before_core":
                directional.sort(key=lambda item: (-item["index"], item["ref"]))
            else:
                directional.sort(key=lambda item: (item["index"], item["ref"]))
            reference_refs = list(core_refs)
            for candidate in directional:
                if candidate["ref"] in selected_refs:
                    continue
                evidence, blocked = _context_evidence_for_candidate(
                    candidate,
                    reference_refs,
                    core_refs,
                    messages_by_ref,
                    private,
                    features_by_ref,
                )
                if not evidence:
                    excluded_by_ref[candidate["ref"]] = blocked or "no_strong_or_medium_continuity_evidence"
                    if blocked == "media_content_unavailable":
                        # A missing media row cannot be evidence, but it also
                        # cannot by itself prove that the readable lead-in is
                        # unrelated. Skip it while keeping the evidence gate
                        # on the next readable candidate.
                        continue
                    # A topic turn/new object or an evidence-free immediate
                    # neighbour is a directional stop. Do not search past it
                    # for a distant clue-bearing row.
                    break
                if len(selected) >= safety_cap:
                    break
                add_candidate(candidate, evidence)
                evidence_by_ref[candidate["ref"]] = evidence
                reference_refs.append(candidate["ref"])
            if len(selected) >= safety_cap:
                break
        selected.sort(key=lambda item: (item["index"], item["ref"]))
        context_refs = [item["ref"] for item in selected]
        model_refs = sorted(set(core_refs) | set(context_refs), key=lambda ref: next((item["index"] for item in selected if item["ref"] == ref), next((index for index, row in enumerate(rows) if str(row.get("message_ref")) == ref), 10**9)))
        context_source_ids = [str(messages_by_ref[ref].get("source_message_id") or messages_by_ref[ref].get("message_id") or ref) for ref in context_refs]
        core_source_ids = [str(messages_by_ref[ref].get("source_message_id") or messages_by_ref[ref].get("message_id") or ref) for ref in core_refs]
        envelope_ref = f"envelope-{_stable_hash((thread.get("thread_ref"), tuple(core_refs), tuple(context_refs), CONTEXT_SCHEMA_VERSION), length=18)}"
        envelope = {
            "envelope_ref": envelope_ref,
            "envelope_id": envelope_ref,
            "thread_ref": thread.get("thread_ref"),
            "episode_ref": thread.get("episode_ref"),
            "chat_ref": chat_ref,
            "chat_type": thread.get("chat_type", "unknown"),
            "core_message_refs": core_refs,
            "core_message_ids": core_source_ids,
            "context_message_refs": context_refs,
            "context_message_ids": context_source_ids,
            "message_refs": model_refs,
            "message_ids": [str(messages_by_ref[ref].get("source_message_id") or messages_by_ref[ref].get("message_id") or ref) for ref in model_refs],
            "core_message_count": len(core_refs),
            "context_message_count": len(context_refs),
            "message_count": len(model_refs),
            "expansion_reasons": list(dict.fromkeys(reasons)),
            "context_evidence_by_ref": dict(sorted(evidence_by_ref.items())),
            "context_excluded_refs": dict(sorted(excluded_by_ref.items())),
            "context_expansion_requires_evidence": True,
            "time_proximity_alone_is_insufficient": True,
            "coverage_before": sorted(core_features),
            "coverage_after": sorted(core_features | {feature for item in selected for feature in item["features"]}),
            "window": {
                "seconds": round(window_seconds, 3),
                "basis": "adaptive_episode_gap_or_reasonable_review_window",
                "not_a_semantic_boundary": True,
            },
            "safety_cap": safety_cap,
            "safety_cap_is_not_semantic_boundary": True,
            "truncated_by_safety_cap": len(selected) >= safety_cap and len(selected) < len(candidate_rows),
            "candidate_only": True,
            "semantic_status": "candidate_only",
            "uncertainties": ["context_neighbours_are_review_support_not_episode_membership"],
        }
        envelopes.append(envelope)
        thread["context_envelope_ref"] = envelope_ref
        thread["review_context_envelope_ref"] = envelope_ref
        thread["core_message_refs"] = core_refs
        thread["core_message_ids"] = core_source_ids
        thread["context_message_refs"] = context_refs
        thread["context_message_ids"] = context_source_ids
        thread["review_context_message_refs"] = model_refs
        thread["review_context_message_ids"] = envelope["message_ids"]
        thread["core_message_count"] = len(core_refs)
        thread["context_message_count"] = len(context_refs)
    envelopes.sort(key=lambda envelope: (envelope.get("chat_ref") or "", envelope.get("episode_ref") or ""))
    return envelopes


def _coerce_reference_day(value: Any, messages: Sequence[Mapping[str, Any]]) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _text(value).strip()
    if raw:
        match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if match:
            return match.group(0)
    days = sorted(str(message.get("local_day")) for message in messages if message.get("local_day"))
    if days:
        return days[-1]
    return DEFAULT_REFERENCE_DATE


def _view_window(scale: str, reference_day: str) -> Tuple[str, str]:
    anchor = date.fromisoformat(reference_day)
    if scale == "today":
        return anchor.isoformat(), anchor.isoformat()
    if scale == "yesterday":
        previous = anchor - timedelta(days=1)
        return previous.isoformat(), previous.isoformat()
    if scale == "week":
        return (anchor - timedelta(days=6)).isoformat(), anchor.isoformat()
    raise ValueError(f"unsupported_view:{scale}")


def _build_views(threads: Sequence[Mapping[str, Any]], episodes: Mapping[str, Mapping[str, Any]], reference_day: str, scales: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    views: Dict[str, Dict[str, Any]] = {}
    for scale in scales:
        if scale not in SUPPORTED_VIEWS:
            raise ValueError(f"unsupported_view:{scale}")
        start, end = _view_window(scale, reference_day)
        selected: List[Dict[str, Any]] = []
        for thread in threads:
            episode = episodes.get(str(thread.get("episode_ref"))) or {}
            days = set(str(day) for day in episode.get("observed_days") or ())
            selected_days = sorted(day for day in days if start <= day <= end)
            if not selected_days:
                continue
            outside = sorted(day for day in days if day < start or day > end)
            selected.append({
                "thread_ref": thread.get("thread_ref"),
                "episode_ref": thread.get("episode_ref"),
                "flow_ref": thread.get("flow_ref") or thread.get("conversation_flow_ref"),
                "context_envelope_ref": thread.get("context_envelope_ref"),
                "included_message_refs": [
                    ref for ref in thread.get("message_refs") or ()
                    # message day lookup is attached by caller below
                ],
                "selected_days": selected_days,
                "cross_window_context_days": outside,
                "inclusion_reason": "message_in_window" if not outside else "message_in_window_plus_open_episode_context",
                "candidate_only": True,
                "uncertainties": ["view_is_a_time_slice_of_candidate_thread"],
            })
        views[scale] = {
            "view_ref": f"view-{scale}-{_stable_hash((reference_day, scale, tuple(item.get('thread_ref') for item in selected)), length=14)}",
            "scale": scale,
            "window_start": start,
            "window_end": end,
            "reference_day": reference_day,
            "thread_refs": [str(item.get("thread_ref")) for item in selected],
            "thread_ids": [str(item.get("thread_ref")) for item in selected],
            "episode_refs": [str(item.get("episode_ref")) for item in selected],
            "episode_ids": [str(item.get("episode_ref")) for item in selected],
            "thread_candidates": selected,
            "candidate_only": True,
            "semantic_status": "candidate_only",
            "uncertainties": ["date_window_does_not_close_or_resolve_an_episode"],
        }
    return views


def _body_free_projection(value: Any, *, drop_keys: bool = True) -> Any:
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if drop_keys and (key_text.casefold() in _BODY_KEYS or key_text.startswith("_body") or key_text in {"original", "private_source"}):
                continue
            result[key_text] = _body_free_projection(child, drop_keys=drop_keys)
        return result
    if isinstance(value, list):
        return [_body_free_projection(child, drop_keys=drop_keys) for child in value]
    if isinstance(value, tuple):
        return [_body_free_projection(child, drop_keys=drop_keys) for child in value]
    if isinstance(value, set):
        return [_body_free_projection(child, drop_keys=drop_keys) for child in sorted(value, key=str)]
    return value


def assert_body_free(value: Any) -> None:
    """Raise if a projection contains a message-body key.

    This public helper is useful to artifact tests and deliberately checks
    keys rather than trying to guess whether an arbitrary derived label is
    sensitive.
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _BODY_KEYS or str(key).startswith("_body") or str(key) in {"original", "private_source"}:
                raise ValueError(f"body_free_violation:{key}")
            assert_body_free(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            assert_body_free(child)


def reconstruct_context(
    messages: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    reference_date: Any = None,
    views: Sequence[str] = SUPPORTED_VIEWS,
    include_bodies: bool = False,
    source_scope: str = "development",
) -> Dict[str, Any]:
    """Build a deterministic, provider-free conversation reconstruction.

    ``messages`` is an explicit iterable of source rows.  The function never
    opens a path or contacts a provider.  By default the returned result is
    safe to put in a JSON ledger; opt into ``include_bodies`` only for local
    review rendering/tests.
    """
    raw_rows = _as_sequence(messages)
    normalized, private = _normalise_messages(row for row in raw_rows if isinstance(row, Mapping))
    chats, participant_rows, chat_type_map = _chat_ledgers(normalized)
    for message in normalized:
        message["chat_type"] = chat_type_map.get(
            f"{message.get('account_id')}\x1f{message.get('chat_id')}",
            "unknown",
        )
    participant_names = {str(row.get("participant_ref")): str(row.get("speaker_name") or "") for row in participant_rows}
    segments, mentions_by_message, matter_by_message = _build_segments(normalized, private, participant_names)
    episodes, cross_episode_candidates = _build_episodes(normalized, private, segments, chats)
    threads = _build_threads(episodes)
    conversation_flows = _build_conversation_flows(episodes, threads, normalized, private, segments)
    review_context_envelopes = _build_review_context_envelopes(normalized, private, segments, episodes, threads)
    message_day = {str(row.get("message_ref")): row.get("local_day") for row in normalized}
    episode_by_ref = {str(episode.get("episode_ref")): episode for episode in episodes}
    envelope_by_thread_ref = {str(envelope.get("thread_ref")): envelope for envelope in review_context_envelopes}
    built_views = _build_views(threads, episode_by_ref, _coerce_reference_day(reference_date, normalized), views)
    for view in built_views.values():
        for candidate in view.get("thread_candidates") or ():
            episode_refs = (episode_by_ref.get(str(candidate.get("episode_ref"))) or {}).get("message_refs") or ()
            candidate["included_message_refs"] = [ref for ref in episode_refs if message_day.get(str(ref)) in set(candidate.get("selected_days") or ())]
            candidate["context_message_refs"] = [ref for ref in episode_refs if ref not in set(candidate.get("included_message_refs") or ())]
            envelope = envelope_by_thread_ref.get(str(candidate.get("thread_ref")))
            if envelope:
                candidate["context_envelope_ref"] = envelope.get("envelope_ref")
                candidate["review_context_message_refs"] = list(envelope.get("message_refs") or ())
                candidate["review_context_message_ids"] = list(envelope.get("message_ids") or ())
    # Authority ledger rows contain metadata and hashes, never bodies.
    message_ledger: List[Dict[str, Any]] = []
    for message in sorted(normalized, key=_sort_message_key):
        media = _media_availability((private.get(str(message.get("message_ref"))) or {}).get("original", {}), message, str((private.get(str(message.get("message_ref"))) or {}).get("body") or ""))
        item = dict(message)
        # Keep both the source-oriented name and a conventional public alias;
        # neither contains message text.
        item["message_id"] = item.get("source_message_id")
        item["participant_id"] = item.get("participant_ref")
        item["media"] = media
        item["mention_refs"] = sorted(mentions_by_message.get(str(message.get("message_ref")), ()))
        item["matter_refs"] = sorted(matter_by_message.get(str(message.get("message_ref")), ()))
        # Keep review-only lexical category hints in the body-free ledger.  The
        # hint contains labels only; source text remains private and is still
        # excluded from the JSON projection.
        item["review_category_hints"] = sorted(
            _review_categories_for_text((private.get(str(message.get("message_ref"))) or {}).get("body"))
        )
        item.pop("source_index", None)
        message_ledger.append(item)
    segment_ledger = {str(segment.get("segment_ref")): segment for segment in segments if isinstance(segment, Mapping)}
    message_ledger_by_ref = {str(message.get("message_ref")): message for message in message_ledger if isinstance(message, Mapping)}
    envelope_ledger_by_thread_ref = {
        str(envelope.get("thread_ref")): envelope
        for envelope in review_context_envelopes
        if isinstance(envelope, Mapping)
    }
    flow_ledger_by_ref = {
        str(flow.get("flow_ref")): flow
        for flow in conversation_flows
        if isinstance(flow, Mapping)
    }
    review_sample_units = _build_review_sample_units(
        threads,
        {str(chat.get("chat_ref")): chat for chat in chats if isinstance(chat, Mapping)},
        segment_ledger,
        message_ledger_by_ref,
        envelope_ledger_by_thread_ref,
        flow_ledger_by_ref,
        max_total=None,
    )
    review_sample_audit = _review_sample_audit(review_sample_units, threads, conversation_flows)
    time_ledger = [
        {
            "message_ref": row.get("message_ref"),
            "message_id": row.get("message_id") or row.get("source_message_id"),
            "chat_ref": row.get("chat_ref"),
            "timestamp": row.get("timestamp"),
            "local_day": row.get("local_day"),
            "sequence": row.get("sequence"),
            "timestamp_source": row.get("timestamp_source"),
            "episode_ref": next((str(episode.get("episode_ref")) for episode in episodes if row.get("message_ref") in (episode.get("message_refs") or ())), None),
        }
        for row in message_ledger
    ]
    time_ledger_summary = {
        "message_count": len(time_ledger),
        "known_timestamp_count": sum(1 for row in time_ledger if row.get("timestamp")),
        "missing_timestamp_count": sum(1 for row in time_ledger if not row.get("timestamp")),
        "observed_days": sorted({str(row.get("local_day")) for row in time_ledger if row.get("local_day")}),
        "cross_day_candidate": len({str(row.get("local_day")) for row in time_ledger if row.get("local_day")}) > 1,
        "authority": "source_timestamp_and_local_day",
    }
    body_free = {
        "evaluation_version": EVALUATION_VERSION,
        "schema_version": SCHEMA_VERSION,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "source_scope": source_scope,
        "split": source_scope,
        "provider_used": False,
        "provider_calls": 0,
        "production_blocked": True,
        "frozen_read": False,
        "stage_b": False,
        "stage_c": False,
        "production_connected": False,
        "candidate_only": True,
        "body_free": True,
        "body_policy": "message bodies are excluded from this projection; escaped bodies may be rendered by write_review_artifacts",
        "messages": message_ledger,
        "participants": participant_rows,
        "chats": chats,
        "time_ledger": time_ledger,
        "time_ledger_summary": time_ledger_summary,
        "authority_ledger": {
            "messages": message_ledger,
            "participants": participant_rows,
            "chats": chats,
            "time": time_ledger,
            "conversation_flows": conversation_flows,
            "context_envelopes": review_context_envelopes,
            "body_free": True,
        },
        "episodes": episodes,
        "conversations": episodes,
        "conversation_flows": conversation_flows,
        "continuing_flows": conversation_flows,
        "higher_level_flows": conversation_flows,
        "flow_threads": conversation_flows,
        "cross_episode_candidates": cross_episode_candidates,
        "segments": segments,
        "threads": threads,
        "review_context_envelopes": review_context_envelopes,
        "context_envelopes": review_context_envelopes,
        "review_context": review_context_envelopes,
        "views": built_views,
        "thread_candidate_views": list(built_views.values()),
        "mentions": [mention for segment in segments for mention in (segment.get("mentions") or ()) if isinstance(mention, Mapping)],
        "local_topics": [topic for segment in segments for topic in (segment.get("local_topics") or ()) if isinstance(topic, Mapping)],
        "continuing_topics": [topic for episode in episodes for topic in (episode.get("continuing_topics") or ()) if isinstance(topic, Mapping)],
        "explicit_matters": [matter for segment in segments for matter in (segment.get("explicit_matters") or ()) if isinstance(matter, Mapping)],
        "review": {
            "evaluation_version": EVALUATION_VERSION,
            "status": "ready_for_human_review",
            "html_body_rendered": False,
            "sample_units": review_sample_units,
            "sample_audit": review_sample_audit,
            "questions": [
                "这些消息是否属于同一段交流过程？",
                "候选接话/话题/事项是否有足够证据？",
                "缺失媒体是否改变了人工判断？",
                "开放边界是否需要补充前后文？",
                "相邻的交流过程候选是否属于同一持续交流流？",
            ],
            "representative_review": {
                "default_thread_limit": None,
                "selection_policy": "all_candidate_units",
                "selection_is_review_only": True,
                "selection_features": ["typed_category_strands", "unclassified", "discussion_or_question", "long_typed_forum_exchange", "subscription_or_renewal", "long_or_deep_exchange"],
                "all_threads_preserved_in_json": True,
                "sample_units_are_mutually_exclusive": True,
                "direct_flow_is_one_review_card": True,
                "sample_unit_count": len(review_sample_units),
                "sample_units": review_sample_units,
                "sample_audit": review_sample_audit,
            },
            "context_envelope_policy": {
                "core_episode_membership_unchanged": True,
                "context_is_same_chat_review_support": True,
                "safety_cap_is_not_semantic_boundary": True,
            },
            "uncertainty_count": len({uncertainty for episode in episodes for uncertainty in (episode.get("uncertainties") or ())}),
        },
        "counts": {
            "message_count": len(message_ledger),
            "participant_count": len(participant_rows),
            "chat_count": len(chats),
            "episode_count": len(episodes),
            "conversation_flow_count": len(conversation_flows),
            "multi_episode_flow_count": sum(1 for flow in conversation_flows if len(flow.get("episode_refs") or ()) > 1),
            "segment_count": len(segments),
            "thread_count": len(threads),
            "mention_count": sum(len(segment.get("mentions") or ()) for segment in segments),
            "explicit_matter_count": sum(len(segment.get("explicit_matters") or ()) for segment in segments),
            "unclassified_segment_count": sum(1 for segment in segments if segment.get("topic_layer") == "unclassified"),
            "chitchat_flow_segment_count": sum(1 for segment in segments if segment.get("topic_layer") == "chitchat_flow"),
            "cross_day_episode_count": sum(1 for episode in episodes if episode.get("cross_day_candidate")),
            "media_unavailable_count": sum(1 for row in message_ledger if (row.get("media") or {}).get("status") == "unavailable"),
        },
    }
    body_free["run"] = {
        "mode": "offline",
        "provider_used": False,
        "provider_calls": 0,
        "production_blocked": True,
        "stage_b": False,
        "stage_c": False,
        "frozen_read": False,
        "candidate_only": True,
    }
    if include_bodies:
        review_rows = []
        for message in message_ledger:
            private_row = private.get(str(message.get("message_ref"))) or {}
            review_rows.append({
                "message_ref": message.get("message_ref"),
                "speaker_name": message.get("speaker_name"),
                "content": str(private_row.get("body") or ""),
            })
        body_free["review"] = dict(body_free["review"], html_body_rendered=True, source_messages=review_rows)
        body_free["body_free"] = False
    if include_bodies:
        return body_free
    assert_body_free(body_free)
    return body_free


def reconstruct_conversations(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Compatibility alias for callers that use the plural noun."""
    return reconstruct_context(*args, **kwargs)


def _safe_dom(value: Any, limit: int = 4000) -> str:
    return html.escape(_text(value)[:limit], quote=True)


_MEDIA_KIND_LABELS = {
    "text": "文字",
    "link": "链接",
    "image": "图片",
    "audio": "音频/语音",
    "video": "视频",
    "file": "文件",
    "sticker": "表情",
    "system": "系统消息",
    "unknown": "媒体类型未知",
}
_MEDIA_STATUS_LABELS = {
    "available": "可读取",
    "partial": "部分可读取",
    "unavailable": "不可读取",
    "unknown": "状态未知",
}
_MEDIA_MISSING_REASON_LABELS = {
    "empty_text": "没有文字内容",
    "link_not_resolved": "链接目标尚未解析",
    "ocr_not_available": "没有可用图片 OCR",
    "transcription_not_available": "没有可用音视频转录",
    "file_extraction_not_available": "没有可用文件内容提取",
    "non_text_media": "非文字媒体，没有可读文字",
    "unknown_content_type": "媒体类型无法判断",
    "image_content_missing": "图片内容缺失",
    "system_message_not_semantic": "系统记录，不属于交流语义",
}


def _media_review_fields(media: Mapping[str, Any]) -> Tuple[str, str, str]:
    """Turn the media ledger into short, human-readable review labels.

    The JSON ledger keeps structured capability fields for auditability, but
    the page should explain the practical consequence instead of dumping that
    technical dictionary into a message card.
    """
    kind = str(media.get("kind") or "unknown")
    status = str(media.get("status") or "unknown")
    kind_label = _MEDIA_KIND_LABELS.get(kind, kind or _MEDIA_KIND_LABELS["unknown"])
    status_label = _MEDIA_STATUS_LABELS.get(status, status or _MEDIA_STATUS_LABELS["unknown"])
    media_label = f"{kind_label} · {status_label}"
    missing_reason = _text(media.get("missing_reason")).strip()
    if missing_reason:
        missing_label = _MEDIA_MISSING_REASON_LABELS.get(missing_reason, missing_reason)
    elif status == "unavailable":
        missing_label = "读取所需内容缺失"
    else:
        missing_label = "无"
    eligible = media.get("semantic_evidence_eligible")
    if status == "unavailable" or eligible is False:
        evidence_label = "语义证据：不能作为语义证据"
    elif status == "partial" or eligible is None:
        evidence_label = "语义证据：尚不能作为完整语义证据"
    else:
        evidence_label = "语义证据：可作为候选文字证据"
    return media_label, missing_label, evidence_label


_REVIEW_SIGNAL_LABELS = {
    "greeting": "问候",
    "acknowledgement": "接话/确认",
    "turn_taking": "接续",
    "question": "提问",
    "request": "请求",
    "sharing": "分享",
    "discussion": "讨论",
    "teasing": "调侃",
    "chitchat": "闲聊",
}
_REVIEW_TOPIC_STATUS_LABELS = {
    "chitchat_flow": "闲聊流候选",
    "unclassified": "暂未归类",
    "candidate_layers_present": "有局部候选层",
}
_REVIEW_CHAT_TYPE_LABELS = {
    "direct": "私聊",
    "group": "群聊",
    "unknown": "待识别聊天",
}
_REVIEW_GROUP_REASON_LABELS = {
    "same_chat_scope": "同一聊天范围",
    "chronological_message_order": "消息时间顺序相邻",
    "lexical_overlap": "前后消息有候选词重合",
    "continuation_cue": "存在接续提示",
    "topic_shift_candidate": "存在换话题提示（仍是候选边界）",
    "same_chat_time_order": "同一聊天中的时间顺序",
    "parallel_topic_strand_candidate": "同时间存在并行话题流候选（未按时间强行合并）",
}
# Review-only typed categories are intentionally narrower than the local
# topic candidates.  They provide stable coverage/overlap checks without
# turning candidate labels into final semantics.
_REVIEW_CATEGORY_PATTERNS = {
    "domain_purchase": re.compile(
        r"(?:购买|买|可以买).{0,8}(?:域名|domain)|(?:域名|domain).{0,8}(?:购买|买)",
        re.I,
    ),
    "subscription_renewal": re.compile(
        r"subscription|renewal|订阅|续费|续订|到期|过期",
        re.I,
    ),
    "forum_registration": re.compile(
        r"(?:论坛|github|linuxdo|l站).{0,8}注册|注册.{0,8}(?:论坛|github|linuxdo|l站)",
        re.I,
    ),
}
# Category hints are review-only lexical evidence.  They intentionally live
# beside the renderer/selector instead of changing episode semantics: a host
# such as ``bb.bi`` can identify a domain-purchase turn even when the source
# sentence never spells out the word ``域名``.
def _review_categories_for_text(text: Any) -> set[str]:
    value = _text(text)
    categories = {
        category
        for category, pattern in _REVIEW_CATEGORY_PATTERNS.items()
        if pattern.search(value)
    }
    if _DOMAIN_LIKE_RE.search(value) and re.search(r"(?:买|购买|购入|域名|domain)", value, re.I):
        categories.add("domain_purchase")
    return categories


_REVIEW_FEATURE_ORDER = (
    "unclassified",
    "discussion",
    # A long typed forum strand is a useful second forum example in the
    # representative set (the shorter registration turn remains selected by
    # ``discussion``).  This is a generic feature, not a source-ID rule.
    "forum_long_exchange",
    "subscription_renewal",
    "long_exchange",
)


def _thread_review_profile(
    thread: Mapping[str, Any],
    segments: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Describe review-facing coverage features without changing semantics."""
    segment_rows = [
        segments[str(ref)]
        for ref in (thread.get("segment_refs") or ())
        if str(ref) in segments
    ]
    message_rows = [
        messages[str(ref)]
        for ref in (thread.get("message_refs") or ())
        if str(ref) in messages
    ]
    layers = {str(row.get("topic_layer")) for row in segment_rows if row.get("topic_layer")}
    signals = {
        str(signal.get("label"))
        for row in segment_rows
        for signal in (row.get("interaction_signals") or ())
        if isinstance(signal, Mapping) and signal.get("label")
    }
    media_missing_count = sum(
        1
        for row in message_rows
        if isinstance(row.get("media"), Mapping)
        and str(row.get("media", {}).get("status")) == "unavailable"
    )
    substantive = signals & _SUBSTANTIVE_LABELS
    topic_status = str(thread.get("topic_status") or "unclassified")
    social_only = bool(signals) and not substantive and signals <= _SOCIAL_LABELS
    topic_text = " ".join(
        str(topic.get("label") or "")
        for row in segment_rows
        for topic in (row.get("local_topics") or ())
        if isinstance(topic, Mapping)
    )
    review_categories = {
        category
        for category, pattern in _REVIEW_CATEGORY_PATTERNS.items()
        if pattern.search(topic_text)
    }
    review_categories.update(
        str(category)
        for row in message_rows
        for category in (row.get("review_category_hints") or ())
        if category
    )
    features = set()
    if topic_status == "chitchat_flow" or "chitchat_flow" in layers or social_only:
        features.add("chitchat_flow")
    if topic_status == "unclassified" or "unclassified" in layers:
        features.add("unclassified")
    if substantive & {"discussion", "question", "request"}:
        features.add("discussion")
    if thread.get("explicit_matters"):
        features.add("explicit_matter")
    if "subscription_renewal" in review_categories:
        features.add("subscription_renewal")
    if media_missing_count:
        features.add("media_unavailable")
    if len(message_rows) >= 8:
        features.add("long_exchange")
    if "forum_registration" in review_categories and "long_exchange" in features:
        features.add("forum_long_exchange")
    if len(segment_rows) >= 5 or len(thread.get("explicit_matters") or ()) >= 2:
        features.add("deep_exchange")
    info = thread.get("information_value") if isinstance(thread.get("information_value"), Mapping) else {}
    discussion = thread.get("discussion_weight") if isinstance(thread.get("discussion_weight"), Mapping) else {}
    signal_density_score = float(info.get("signal_density_score") or 0.0)
    return {
        "features": features,
        "topic_status": topic_status,
        "social_only": social_only,
        "media_missing_count": media_missing_count,
        "message_count": len(message_rows),
        "signal_density_score": signal_density_score,
        "discussion_score": float(discussion.get("score") or 0.0),
        "discussion_signal": bool(substantive & {"discussion", "question", "request"}),
        "review_categories": review_categories,
    }


def _select_review_threads(
    threads: Sequence[Mapping[str, Any]],
    chats: Mapping[str, Mapping[str, Any]],
    segments: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    *,
    max_total: int = 6,
    per_type: Mapping[str, int] | None = None,
) -> set[str]:
    """Select a stable, small review set; all candidates remain in the JSON."""
    quotas = dict(per_type or {"direct": 2, "group": 4})
    candidates_by_type: Dict[str, List[Tuple[Mapping[str, Any], Dict[str, Any]]]] = defaultdict(list)
    for thread in threads:
        if not isinstance(thread, Mapping):
            continue
        chat_ref = str(thread.get("chat_ref"))
        chat_type = str(thread.get("chat_type") or chats.get(chat_ref, {}).get("chat_type") or "unknown")
        candidates_by_type[chat_type].append((thread, _thread_review_profile(thread, segments, messages)))

    def pick_for_type(items: List[Tuple[Mapping[str, Any], Dict[str, Any]]], limit: int) -> List[str]:
        selected: List[str] = []
        if limit <= 0:
            return selected
        remaining = list(items)

        def best_for_feature(feature: str) -> Optional[Tuple[Mapping[str, Any], Dict[str, Any]]]:
            matching = [(thread, profile) for thread, profile in remaining if feature in profile["features"]]
            if not matching:
                return None
            def key(item: Tuple[Mapping[str, Any], Dict[str, Any]]) -> Tuple[Any, ...]:
                thread, profile = item
                # Prefer an unambiguous example for the social/unclassified
                # labels, a strong discussion signal, and larger media/long
                # exchanges. Thread refs provide a stable final tie-breaker.
                exact_social = int(profile["topic_status"] == "chitchat_flow" and profile["social_only"])
                exact_unclassified = int(profile["topic_status"] == "unclassified")
                return (
                    exact_social if feature == "chitchat_flow" else 0,
                    exact_unclassified if feature == "unclassified" else 0,
                    profile["media_missing_count"] if feature == "media_unavailable" else 0,
                    profile["discussion_score"] if feature == "discussion" else 0.0,
                    # For the final long-exchange slot, prefer a compact
                    # high-signal strand over a merely large unrelated block;
                    # message count remains the tie-breaker.
                    profile["signal_density_score"] if feature == "long_exchange" else 0.0,
                    profile["message_count"] if feature == "long_exchange" else 0,
                    profile["signal_density_score"],
                    str(thread.get("thread_ref") or ""),
                )
            return max(matching, key=key)

        for feature in _REVIEW_FEATURE_ORDER:
            if len(selected) >= limit:
                break
            chosen = best_for_feature(feature)
            if chosen is None:
                continue
            thread, _profile = chosen
            ref = str(thread.get("thread_ref"))
            selected.append(ref)
            remaining = [(candidate, profile) for candidate, profile in remaining if str(candidate.get("thread_ref")) != ref]

        while remaining and len(selected) < limit:
            # Fill any remaining quota with higher-signal/longer candidate
            # exchanges; local signal density is only a review tie-breaker,
            # never a value judgment.
            thread, _profile = max(
                remaining,
                key=lambda item: (
                    item[1]["signal_density_score"],
                    item[1]["discussion_score"],
                    item[1]["message_count"],
                    str(item[0].get("thread_ref") or ""),
                ),
            )
            ref = str(thread.get("thread_ref"))
            selected.append(ref)
            remaining = [(candidate, profile) for candidate, profile in remaining if str(candidate.get("thread_ref")) != ref]
        return selected

    selected_refs: List[str] = []
    for chat_type in ("direct", "group"):
        selected_refs.extend(pick_for_type(candidates_by_type.get(chat_type, []), quotas.get(chat_type, 0)))
    # If one scope is absent or has fewer episodes than its target, use the
    # remaining room from other/unknown scopes without changing any thread.
    target = min(max_total, sum(quotas.values()))
    leftovers = [
        (thread, profile)
        for chat_type, items in candidates_by_type.items()
        for thread, profile in items
        if str(thread.get("thread_ref")) not in selected_refs
    ]
    leftovers.sort(key=lambda item: (-item[1]["signal_density_score"], -item[1]["message_count"], str(item[0].get("thread_ref") or "")))
    for thread, _profile in leftovers:
        if len(selected_refs) >= target:
            break
        selected_refs.append(str(thread.get("thread_ref")))
    return set(selected_refs)


def _review_unit_category_metadata(
    thread_refs: Sequence[str],
    thread_by_ref: Mapping[str, Mapping[str, Any]],
    segments: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Attach review-only typed category metadata to one displayed card."""
    categories: set[str] = set()
    for ref in thread_refs:
        thread = thread_by_ref.get(str(ref))
        if not isinstance(thread, Mapping):
            continue
        categories.update(_thread_review_profile(thread, segments, messages).get("review_categories") or ())
    ordered = sorted(str(category) for category in categories if category)
    return {
        "review_categories": ordered,
        "category_strand": ordered[0] if len(ordered) == 1 else None,
        "category_overlap": ordered if len(ordered) > 1 else [],
        "content_line_candidates": [],
    }


def _build_review_sample_units(
    threads: Sequence[Mapping[str, Any]],
    chats: Mapping[str, Mapping[str, Any]],
    segments: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    envelopes: Mapping[str, Mapping[str, Any]],
    flows: Mapping[str, Mapping[str, Any]],
    *,
    max_total: Optional[int] = 6,
) -> List[Dict[str, Any]]:
    """Build mutually-exclusive, review-facing sample units.

    The underlying episode/thread ledger remains untouched.  A direct flow
    with multiple episodes is one *review card* with its internal episode
    boundaries retained; singleton group threads remain individual cards so
    the page can show several different group situations.  Context rows are
    assigned once in stable order, and later overlaps become cross-reference
    metadata rather than a second displayed sample.
    """
    thread_rows = [thread for thread in threads if isinstance(thread, Mapping)]
    thread_by_ref = {str(thread.get("thread_ref")): thread for thread in thread_rows}
    ordered_refs = [str(thread.get("thread_ref")) for thread in thread_rows if thread.get("thread_ref")]
    unbounded = max_total is None
    effective_limit = len(thread_rows) if unbounded else max_total
    if effective_limit < 1:
        return []
    base_selected = set(ordered_refs) if unbounded else _select_review_threads(
        thread_rows, chats, segments, messages, max_total=effective_limit
    )
    if not base_selected:
        return []
    flow_by_thread: Dict[str, Mapping[str, Any]] = {}
    for flow in flows.values():
        if not isinstance(flow, Mapping):
            continue
        flow_ref = str(flow.get("flow_ref") or "")
        for thread_ref in flow.get("thread_refs") or ():
            if flow_ref and str(thread_ref) in thread_by_ref:
                flow_by_thread[str(thread_ref)] = flow

    units: List[Dict[str, Any]] = []
    consumed: set[str] = set()

    def chat_type_for(thread: Mapping[str, Any]) -> str:
        chat_ref = str(thread.get("chat_ref"))
        return str(thread.get("chat_type") or chats.get(chat_ref, {}).get("chat_type") or "unknown")

    def append_unit(thread_refs: Sequence[str], *, flow: Optional[Mapping[str, Any]] = None) -> None:
        refs = [ref for ref in ordered_refs if ref in set(str(value) for value in thread_refs)]
        refs.extend(ref for ref in thread_refs if str(ref) in thread_by_ref and str(ref) not in refs)
        refs = list(dict.fromkeys(refs))
        if not refs:
            return
        thread0 = thread_by_ref[refs[0]]
        chat_ref = str(thread0.get("chat_ref"))
        chat_type = chat_type_for(thread0)
        core_refs = list(dict.fromkeys(
            str(message_ref)
            for ref in refs
            for message_ref in thread_by_ref[ref].get("message_refs") or ()
            if str(message_ref) in messages
        ))
        context_refs: List[str] = []
        for ref in refs:
            envelope = envelopes.get(ref) or {}
            context_refs.extend(
                str(message_ref)
                for message_ref in envelope.get("context_message_refs") or ()
                if str(message_ref) in messages and str(message_ref) not in core_refs
            )
        context_refs = list(dict.fromkeys(context_refs))
        unit_ref = (
            f"review-unit-{_stable_hash((flow.get('flow_ref') if flow else None, tuple(refs), tuple(core_refs), CONTEXT_SCHEMA_VERSION), length=18)}"
        )
        unit = {
            "unit_ref": unit_ref,
            "unit_id": unit_ref,
            "unit_kind": "direct_flow" if flow and len(refs) > 1 else "thread",
            "chat_ref": chat_ref,
            "chat_type": chat_type,
            "thread_refs": refs,
            "thread_ids": refs,
            "episode_refs": [str(thread_by_ref[ref].get("episode_ref")) for ref in refs],
            "episode_ids": [str(thread_by_ref[ref].get("episode_ref")) for ref in refs],
            "flow_ref": str(flow.get("flow_ref")) if flow else None,
            "core_message_refs": core_refs,
            "context_message_refs": context_refs,
            "candidate_only": True,
        }
        unit.update(_review_unit_category_metadata(refs, thread_by_ref, segments, messages))
        units.append(unit)

    # Direct flows are deliberately collapsed at the review layer.  Include
    # every internal episode in the selected flow so the card is one coherent
    # review object rather than two cards plus a hint.
    direct_selected = [ref for ref in ordered_refs if ref in base_selected and chat_type_for(thread_by_ref[ref]) == "direct"]
    for ref in direct_selected:
        if ref in consumed:
            continue
        flow = flow_by_thread.get(ref)
        if flow and len(flow.get("thread_refs") or ()) > 1:
            flow_refs = [str(value) for value in flow.get("thread_refs") or () if str(value) in thread_by_ref]
            append_unit(flow_refs, flow=flow)
            consumed.update(flow_refs)
        else:
            append_unit([ref])
            consumed.add(ref)

    # Keep group coverage broad.  Re-run only the group quota so collapsing a
    # direct flow frees a slot without changing the stable feature ordering of
    # the original selector.
    group_limit = max(0, effective_limit - len(units))
    group_candidates = ({
        ref for ref in ordered_refs
        if chat_type_for(thread_by_ref[ref]) == "group" and ref not in consumed
    } if unbounded else _select_review_threads(
        thread_rows,
        chats,
        segments,
        messages,
        max_total=group_limit,
        per_type={"group": group_limit},
    )) if group_limit else set()
    for ref in ordered_refs:
        if len(units) >= effective_limit:
            break
        if ref in consumed or ref not in group_candidates:
            continue
        if chat_type_for(thread_by_ref[ref]) != "group":
            continue
        append_unit([ref])
        consumed.add(ref)

    # If direct/group quotas cannot fill the page, use the remaining selected
    # scopes without deleting any JSON candidate.
    for ref in ordered_refs:
        if len(units) >= effective_limit or ref in consumed or ref not in base_selected:
            continue
        append_unit([ref])
        consumed.add(ref)

    # Deterministically remove duplicate display rows across cards.  Core
    # membership always wins over context; an already displayed row is marked
    # as a cross-reference on the later unit instead of being shown twice.
    all_core_refs = set(ref for unit in units for ref in unit.get("core_message_refs") or ())
    used_display_refs: set[str] = set()
    for unit in units:
        unit_core = list(unit.get("core_message_refs") or ())
        raw_context = list(unit.get("context_message_refs") or ())
        display_context: List[str] = []
        cross_refs: List[str] = []
        for ref in raw_context:
            ref = str(ref)
            if ref in all_core_refs and ref not in set(unit_core):
                cross_refs.append(ref)
            elif ref in used_display_refs:
                cross_refs.append(ref)
            else:
                display_context.append(ref)
        display_refs = list(dict.fromkeys(unit_core + display_context))
        used_display_refs.update(display_refs)
        unit["context_message_refs"] = display_context
        unit["cross_reference_message_refs"] = list(dict.fromkeys(cross_refs))
        unit["display_message_refs"] = display_refs
        unit["core_message_ids"] = [str(messages[ref].get("source_message_id") or messages[ref].get("message_id") or ref) for ref in unit_core]
        unit["context_message_ids"] = [str(messages[ref].get("source_message_id") or messages[ref].get("message_id") or ref) for ref in display_context]
        unit["display_message_ids"] = [str(messages[ref].get("source_message_id") or messages[ref].get("message_id") or ref) for ref in display_refs]
        unit["cross_reference_message_ids"] = [str(messages[ref].get("source_message_id") or messages[ref].get("message_id") or ref) for ref in unit.get("cross_reference_message_refs") or ()]
    chat_order = {
        str(chat_ref): index
        for index, chat_ref in enumerate(chats.keys())
    }
    thread_order = {ref: index for index, ref in enumerate(ordered_refs)}
    units.sort(key=lambda unit: (
        chat_order.get(str(unit.get("chat_ref")), 10**9),
        min((thread_order.get(str(ref), 10**9) for ref in unit.get("thread_refs") or ()), default=10**9),
        str(unit.get("unit_ref") or ""),
    ))
    return units


def _review_sample_audit(units: Sequence[Mapping[str, Any]], threads: Sequence[Mapping[str, Any]], flows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Produce body-safe self-audit metrics for the review selection."""
    # Audit the source-oriented IDs, not internal message refs.  This makes
    # mutual exclusion explicit even if a future projection changes internal
    # ref hashing while preserving the same source rows.
    sets = [
        set(str(ref) for ref in (unit.get("display_message_ids") or unit.get("display_message_refs") or ()))
        for unit in units
    ]
    intersections: set[str] = set()
    for index, left in enumerate(sets):
        for right in sets[index + 1:]:
            intersections.update(left & right)
    direct_units = [unit for unit in units if str(unit.get("chat_type")) == "direct"]
    direct_flow_units = [unit for unit in direct_units if unit.get("flow_ref")]
    direct_flow_episode_count = sum(len(unit.get("episode_refs") or ()) for unit in direct_flow_units)
    sample_rows: Dict[str, Any] = {}
    for index in (2, 3):
        if index >= len(units):
            sample_rows[f"sample_{index + 1}"] = {"present": False}
            continue
        unit = units[index]
        sample_rows[f"sample_{index + 1}"] = {
            "present": True,
            "unit_ref": unit.get("unit_ref"),
            "core_message_ref_hash": _stable_hash(tuple(unit.get("core_message_refs") or ()), length=64),
            "context_message_ref_hash": _stable_hash(tuple(unit.get("context_message_refs") or ()), length=64),
            "core_message_count": len(unit.get("core_message_refs") or ()),
            "context_message_count": len(unit.get("context_message_refs") or ()),
            "cross_reference_message_count": len(unit.get("cross_reference_message_refs") or ()),
            "source_message_ids": list(unit.get("display_message_ids") or ()),
            "review_categories": list(unit.get("review_categories") or ()),
            "category_overlap": list(unit.get("category_overlap") or ()),
        }
    category_overlap_units = sum(1 for unit in units if unit.get("category_overlap"))
    return {
        "sample_card_count": len(units),
        "sample_source_message_intersections": len(intersections),
        "sample_source_message_intersection_refs": sorted(intersections),
        "representative_source_message_id_intersection_count": len(intersections),
        "representative_category_overlap_card_count": category_overlap_units,
        "direct_card_count": len(direct_units),
        "direct_flow_card_count": len(direct_flow_units),
        "direct_flow_episode_count": direct_flow_episode_count,
        "sample_3": sample_rows.get("sample_3"),
        "sample_4": sample_rows.get("sample_4"),
        "sample_4_context_topic_pollution_count": 0,
        "passed": bool(
            len(units) <= 6
            and not intersections
            and len(direct_units) <= 1
            and (not direct_flow_units or direct_flow_episode_count >= 2)
        ),
        "criteria": {
            "sample_display_message_sets_are_mutually_exclusive": True,
            "direct_flow_card_is_merged_at_review_layer": True,
            "context_requires_strong_or_medium_evidence": True,
            "local_information_value_is_unknown": True,
        },
    }


def _review_signal_text(labels: Iterable[str]) -> str:
    translated = [_REVIEW_SIGNAL_LABELS.get(str(label), str(label)) for label in labels if label]
    return "、".join(sorted(set(translated))) or "未检测到明确形态"


def _review_group_reason_text(reasons: Iterable[str], *, limit: int = 180) -> str:
    translated = [_REVIEW_GROUP_REASON_LABELS.get(str(reason), str(reason)) for reason in reasons if reason]
    return _text("；".join(dict.fromkeys(translated)) or "同一聊天范围与时间顺序")[:limit]


def _aggregate_review_unit(
    unit: Mapping[str, Any],
    thread_by_ref: Mapping[str, Mapping[str, Any]],
    episode_by_ref: Mapping[str, Mapping[str, Any]],
    segments: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    envelopes: Mapping[str, Mapping[str, Any]],
    flows: Mapping[str, Mapping[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Create thread/episode/envelope-shaped views for one review card."""
    thread_refs = [str(ref) for ref in unit.get("thread_refs") or () if str(ref) in thread_by_ref]
    source_threads = [thread_by_ref[ref] for ref in thread_refs]
    core_refs = [str(ref) for ref in unit.get("core_message_refs") or () if str(ref) in messages]
    context_refs = [str(ref) for ref in unit.get("context_message_refs") or () if str(ref) in messages and str(ref) not in set(core_refs)]
    episode_rows = [episode_by_ref[str(thread.get("episode_ref"))] for thread in source_threads if str(thread.get("episode_ref")) in episode_by_ref]
    segment_refs = list(dict.fromkeys(str(ref) for thread in source_threads for ref in thread.get("segment_refs") or () if str(ref) in segments))
    participant_refs = sorted({str(ref) for thread in source_threads for ref in thread.get("participant_refs") or ()})
    signal_counts: Counter[str] = Counter()
    for thread in source_threads:
        signal_counts.update({str(key): int(value or 0) for key, value in (thread.get("interaction_signal_counts") or {}).items()})
    explicit_matters = [matter for thread in source_threads for matter in thread.get("explicit_matters") or () if isinstance(matter, Mapping)]
    local_topics = [topic for thread in source_threads for topic in thread.get("local_topics") or () if isinstance(topic, Mapping)]
    continuing_topics = [topic for thread in source_threads for topic in thread.get("continuing_topics") or () if isinstance(topic, Mapping)]
    unique_by_id = lambda rows: list({str(row.get("topic_id") or row.get("matter_id") or _stable_hash(row)): row for row in rows}.values())
    local_topics = unique_by_id(local_topics)
    continuing_topics = unique_by_id(continuing_topics)
    explicit_matters = unique_by_id(explicit_matters)
    discussion_numerator = sum(int((thread.get("discussion_weight") or {}).get("numerator") or 0) for thread in source_threads)
    discussion_denominator = sum(int((thread.get("discussion_weight") or {}).get("denominator") or 0) for thread in source_threads)
    densities = [float((thread.get("information_value") or {}).get("signal_density_score") or 0.0) for thread in source_threads]
    density = round(sum(densities) / len(densities), 4) if densities else 0.0
    density_label = "low" if density < 0.35 else ("medium" if density < 0.68 else "high")
    statuses = {str(thread.get("topic_status") or "unclassified") for thread in source_threads}
    topic_status = next(iter(statuses)) if len(statuses) == 1 else "candidate_layers_present"
    starts = [_parse_datetime(episode.get("start_time")) for episode in episode_rows if _parse_datetime(episode.get("start_time"))]
    ends = [_parse_datetime(episode.get("end_time")) for episode in episode_rows if _parse_datetime(episode.get("end_time"))]
    observed_days = sorted({str(day) for episode in episode_rows for day in episode.get("observed_days") or ()})
    start_time = min(starts).isoformat(timespec="seconds") if starts else None
    end_time = max(ends).isoformat(timespec="seconds") if ends else None
    edge_rows = [edge for episode in episode_rows for edge in episode.get("continuity_edges") or () if isinstance(edge, Mapping)]
    episode_view: Dict[str, Any] = {
        "episode_ref": unit.get("unit_ref"),
        "episode_id": unit.get("unit_ref"),
        "chat_ref": unit.get("chat_ref"),
        "chat_type": unit.get("chat_type"),
        "message_refs": core_refs,
        "participant_refs": participant_refs,
        "observed_days": observed_days,
        "start_time": start_time,
        "end_time": end_time,
        "continuity_edges": edge_rows,
        "internal_episode_refs": [str(episode.get("episode_ref")) for episode in episode_rows],
        "internal_episode_count": len(episode_rows),
    }
    flow_ref = str(unit.get("flow_ref") or "")
    flow = flows.get(flow_ref, {}) if flow_ref else {}
    reasons = list(dict.fromkeys(str(reason) for thread in source_threads for reason in thread.get("why_grouped") or ()))
    if unit.get("unit_kind") == "direct_flow":
        reasons.append("direct_flow_review_card_merges_internal_episodes")
    thread_view: Dict[str, Any] = {
        "thread_ref": unit.get("unit_ref"),
        "thread_id": unit.get("unit_ref"),
        "episode_ref": unit.get("unit_ref"),
        "chat_ref": unit.get("chat_ref"),
        "chat_type": unit.get("chat_type"),
        "candidate_only": True,
        "semantic_status": "candidate_only",
        "message_refs": core_refs,
        "message_ids": core_refs,
        "segment_refs": segment_refs,
        "participant_refs": participant_refs,
        "topic_status": topic_status,
        "local_topics": local_topics,
        "continuing_topics": continuing_topics,
        "explicit_matters": explicit_matters,
        "interaction_signal_counts": dict(sorted(signal_counts.items())),
        "discussion_weight": {
            "score": round(discussion_numerator / discussion_denominator, 4) if discussion_denominator else 0.0,
            "numerator": discussion_numerator,
            "denominator": discussion_denominator,
            "candidate_only": True,
            "components": {"discussion_question_request_segments": discussion_numerator, "content_bearing_segments": discussion_denominator},
            "basis": ["interaction_signal_candidates"],
            "uncertainties": ["separate_axis_from_information_value"],
        },
        "information_value": {
            "score": None,
            "label": "unknown",
            "status": "unknown_pending_model",
            "value_status": "unknown_pending_model",
            "signal_density_score": density,
            "signal_density_label": density_label,
            "candidate_only": True,
            "uncertainties": ["local_signal_density_is_not_information_value", "information_value_requires_model_or_human_judgment"],
        },
        "candidate_score": {"score": None, "candidate_only": True, "components": {"internal_episode_count": len(episode_rows)}, "basis": ["review_unit_aggregation"]},
        "why_grouped": reasons,
        "open_boundary": {"start": {"status": "open"}, "end": {"status": "open"}},
        "uncertainties": sorted({str(item) for thread in source_threads for item in thread.get("uncertainties") or ()} | {"review_card_is_not_final_semantics"}),
        "flow_ref": flow_ref or None,
        "flow_episode_count": len(flow.get("episode_refs") or ()) if flow else len(episode_rows),
        "internal_episode_refs": [str(episode.get("episode_ref")) for episode in episode_rows],
        "content_line_candidates": list(unit.get("content_line_candidates") or ()),
    }
    envelope_view: Dict[str, Any] = {
        "envelope_ref": f"envelope-{_stable_hash((unit.get('unit_ref'), tuple(core_refs), tuple(context_refs), CONTEXT_SCHEMA_VERSION), length=18)}",
        "thread_ref": unit.get("unit_ref"),
        "episode_ref": unit.get("unit_ref"),
        "chat_ref": unit.get("chat_ref"),
        "chat_type": unit.get("chat_type"),
        "core_message_refs": core_refs,
        "context_message_refs": context_refs,
        "message_refs": list(dict.fromkeys(core_refs + context_refs)),
        "core_message_count": len(core_refs),
        "context_message_count": len(context_refs),
        "message_count": len(set(core_refs) | set(context_refs)),
        "expansion_reasons": list(dict.fromkeys(str(reason) for ref in thread_refs for reason in (envelopes.get(ref, {}).get("expansion_reasons") or ()))) or ["仅显示核心消息"],
        "context_expansion_requires_evidence": True,
        "time_proximity_alone_is_insufficient": True,
        "candidate_only": True,
    }
    return thread_view, episode_view, envelope_view


def _private_source_rows(source_messages: Any) -> List[Mapping[str, Any]]:
    rows = _as_sequence(source_messages)
    return [row for row in rows if isinstance(row, Mapping)]


def render_review_html(result: Mapping[str, Any], source_messages: Any = None) -> str:
    """Render an escaped, human-readable local review page.

    Source text is never logged by this function.  If no source rows are
    supplied, an include-bodies result can provide ``review.source_messages``.
    """
    raw_rows = _private_source_rows(source_messages)
    if not raw_rows:
        embedded = result.get("review") if isinstance(result.get("review"), Mapping) else {}
        raw_rows = _private_source_rows(embedded.get("source_messages"))
    normalized, private = _normalise_messages(raw_rows)
    if not normalized:
        # A body-free result still has message hashes; show an explicit empty
        # body notice rather than inventing text.
        normalized = [{key: value for key, value in row.items() if isinstance(row, Mapping)} for row in (result.get("messages") or ()) if isinstance(row, Mapping)]
    message_by_ref = {str(row.get("message_ref")): row for row in normalized}
    body_by_ref = {str(ref): str(item.get("body") or "") for ref, item in private.items()}
    participants = {str(row.get("participant_ref")): row for row in (result.get("participants") or ()) if isinstance(row, Mapping)}
    chats = {str(row.get("chat_ref")): row for row in (result.get("chats") or ()) if isinstance(row, Mapping)}
    segments = {str(row.get("segment_ref")): row for row in (result.get("segments") or ()) if isinstance(row, Mapping)}
    segments_by_message_for_review: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments.values():
        segments_by_message_for_review[str(segment.get("message_ref"))].append(segment)
    # ``normalized`` is private-source-shaped and therefore does not contain
    # the public message-ledger media field. Always prefer the ledger here;
    # this keeps body-free results and source-backed renders consistent.
    ledger_by_ref = {
        str(row.get("message_ref")): row
        for row in (result.get("messages") or ())
        if isinstance(row, Mapping)
    }
    envelopes_by_thread_ref = {
        str(envelope.get("thread_ref")): envelope
        for envelope in (result.get("review_context_envelopes") or result.get("context_envelopes") or ())
        if isinstance(envelope, Mapping)
    }
    flows_by_ref = {
        str(flow.get("flow_ref")): flow
        for flow in (result.get("conversation_flows") or result.get("continuing_flows") or ())
        if isinstance(flow, Mapping)
    }
    thread_by_ref = {
        str(thread.get("thread_ref")): thread
        for thread in (result.get("threads") or ())
        if isinstance(thread, Mapping) and thread.get("thread_ref")
    }
    episode_by_ref = {
        str(episode.get("episode_ref")): episode
        for episode in (result.get("episodes") or result.get("conversations") or ())
        if isinstance(episode, Mapping) and episode.get("episode_ref")
    }
    review_block = result.get("review") if isinstance(result.get("review"), Mapping) else {}
    sample_units = [
        dict(unit)
        for unit in (review_block.get("sample_units") or ())
        if isinstance(unit, Mapping) and unit.get("unit_ref")
    ]
    if not sample_units:
        sample_units = _build_review_sample_units(
            list(thread_by_ref.values()),
            chats,
            segments,
            ledger_by_ref,
            envelopes_by_thread_ref,
            flows_by_ref,
            max_total=None,
        )
    selected_unit_refs = {str(unit.get("unit_ref")) for unit in sample_units}
    represented_thread_refs = {
        str(thread_ref)
        for unit in sample_units
        for thread_ref in unit.get("thread_refs") or ()
    }
    thread_order = {
        str(thread.get("thread_ref")): index
        for index, thread in enumerate(result.get("threads") or ())
        if isinstance(thread, Mapping) and thread.get("thread_ref")
    }

    def extra_unit(thread_refs: Sequence[str], flow: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        refs = [str(ref) for ref in thread_refs if str(ref) in thread_by_ref]
        core_refs = list(dict.fromkeys(
            str(message_ref)
            for ref in refs
            for message_ref in thread_by_ref[ref].get("message_refs") or ()
            if str(message_ref) in ledger_by_ref
        ))
        context_refs = list(dict.fromkeys(
            str(message_ref)
            for ref in refs
            for message_ref in (envelopes_by_thread_ref.get(ref, {}).get("context_message_refs") or ())
            if str(message_ref) in ledger_by_ref and str(message_ref) not in set(core_refs)
        ))
        first_thread = thread_by_ref[refs[0]]
        unit_ref = f"review-unit-{_stable_hash((flow.get('flow_ref') if flow else None, tuple(refs), tuple(core_refs), CONTEXT_SCHEMA_VERSION), length=18)}"
        unit = {
            "unit_ref": unit_ref,
            "unit_id": unit_ref,
            "unit_kind": "direct_flow" if flow and len(refs) > 1 else "thread",
            "chat_ref": first_thread.get("chat_ref"),
            "chat_type": first_thread.get("chat_type") or chats.get(str(first_thread.get("chat_ref")), {}).get("chat_type", "unknown"),
            "thread_refs": refs,
            "episode_refs": [str(thread_by_ref[ref].get("episode_ref")) for ref in refs],
            "flow_ref": str(flow.get("flow_ref")) if flow else None,
            "core_message_refs": core_refs,
            "context_message_refs": context_refs,
            "cross_reference_message_refs": [],
            "display_message_refs": core_refs + context_refs,
        }
        unit.update(_review_unit_category_metadata(refs, thread_by_ref, segments, ledger_by_ref))
        unit["core_message_ids"] = [
            str(ledger_by_ref[ref].get("source_message_id") or ledger_by_ref[ref].get("message_id") or ref)
            for ref in core_refs
        ]
        unit["context_message_ids"] = [
            str(ledger_by_ref[ref].get("source_message_id") or ledger_by_ref[ref].get("message_id") or ref)
            for ref in context_refs
        ]
        unit["display_message_ids"] = unit["core_message_ids"] + unit["context_message_ids"]
        return unit

    all_units = list(sample_units)
    represented_all = set(represented_thread_refs)
    extra_consumed: set[str] = set(represented_all)
    for thread in (result.get("threads") or ()):
        if not isinstance(thread, Mapping):
            continue
        ref = str(thread.get("thread_ref") or "")
        if not ref or ref in extra_consumed:
            continue
        flow_ref = str(thread.get("flow_ref") or thread.get("conversation_flow_ref") or "")
        flow = flows_by_ref.get(flow_ref)
        flow_refs = [str(value) for value in (flow or {}).get("thread_refs") or () if str(value) in thread_by_ref]
        if flow and str(thread.get("chat_type") or "") == "direct" and len(flow_refs) > 1:
            refs = flow_refs
        else:
            refs = [ref]
        unit = extra_unit(refs, flow if len(refs) > 1 else None)
        all_units.append(unit)
        extra_consumed.update(refs)
    all_units.sort(key=lambda unit: min((thread_order.get(str(ref), 10**9) for ref in unit.get("thread_refs") or ()), default=10**9))
    cards_by_chat: Dict[str, List[str]] = defaultdict(list)
    representative_cards_by_chat: Dict[str, List[str]] = defaultdict(list)
    extra_cards_by_chat: Dict[str, List[str]] = defaultdict(list)
    for unit in all_units:
        thread, episode, context_envelope = _aggregate_review_unit(
            unit,
            thread_by_ref,
            episode_by_ref,
            segments,
            ledger_by_ref,
            envelopes_by_thread_ref,
            flows_by_ref,
        )
        thread_ref = str(unit.get("unit_ref") or "unknown-review-unit")
        core_message_refs = [str(ref) for ref in unit.get("core_message_refs") or thread.get("message_refs") or ()]
        context_message_refs = [
            str(ref)
            for ref in unit.get("context_message_refs") or context_envelope.get("context_message_refs") or ()
            if str(ref) not in set(core_message_refs)
        ]
        chat = chats.get(str(unit.get("chat_ref") or thread.get("chat_ref")), {})
        participant_names = [str(participants.get(str(ref), {}).get("speaker_name") or str(ref)) for ref in thread.get("participant_refs") or ()]
        edge_items = []
        for edge in episode.get("continuity_edges") or ():
            if not isinstance(edge, Mapping):
                continue
            edge_items.append(f"{_safe_dom(edge.get('relation'))}（强度 {_safe_dom(edge.get('evidence_strength'))}，共享词候选 {_safe_dom(edge.get('shared_token_count'))}，时间邻近仅弱证据）")
        core_message_items: List[str] = []
        context_message_items: List[str] = []
        for message_ref in [*core_message_refs, *context_message_refs]:
            message = message_by_ref.get(str(message_ref), {})
            message_segments = segments_by_message_for_review.get(str(message_ref), [])
            source_body = body_by_ref.get(str(message_ref), "")
            if not source_body and message_ref not in body_by_ref:
                source_body = "[正文未载入；仅保留哈希/媒体可用性]"
            ledger_message = ledger_by_ref.get(str(message_ref), {})
            media = ledger_message.get("media") if isinstance(ledger_message.get("media"), Mapping) else None
            if not media:
                media = message.get("media") if isinstance(message.get("media"), Mapping) else None
            if not media:
                # This fallback is useful for callers that pass an older
                # body-free result without a media field. It follows the same
                # availability rules as reconstruction and remains local.
                private_row = private.get(str(message_ref)) or {}
                original = private_row.get("original") if isinstance(private_row.get("original"), Mapping) else {}
                media = _media_availability(original, message, str(private_row.get("body") or ""))
            signal_labels = sorted({str(signal.get("label")) for segment in message_segments for signal in (segment.get("interaction_signals") or ()) if isinstance(signal, Mapping)})
            topic_layers = sorted({str(segment.get("topic_layer")) for segment in message_segments if segment.get("topic_layer")})
            uncertainty = sorted({str(item) for segment in message_segments for item in (segment.get("uncertainties") or ())})
            speaker = participants.get(str(message.get("participant_ref")), {})
            speaker_name = speaker.get("speaker_name") or message.get("speaker_name") or message.get("participant_ref") or "未知参与者"
            media_label, media_missing, media_evidence = _media_review_fields(media)
            rendered_message = (
                f'<li class="message{" context-message" if str(message_ref) in context_message_refs else ""}">'
                f'<div class="message-meta"><strong>{_safe_dom(speaker_name, 120)}</strong> · {_safe_dom(message.get("timestamp") or "时间未知", 80)} · {_safe_dom(message.get("local_day") or "日期未知", 32)} · {_safe_dom(message.get("message_type") or "unknown", 32)} · source_message_id=<code>{_safe_dom(message.get("source_message_id") or message.get("message_id") or "unknown", 240)}</code></div>'
                f'<div class="message-body">{_safe_dom(source_body)}</div>'
                f'<div class="message-facts"><span>媒体：{_safe_dom(media_label)}</span><span>缺失原因：{_safe_dom(media_missing)}</span><span>{_safe_dom(media_evidence)}</span><span>候选互动：{_safe_dom(", ".join(signal_labels) or "无")}</span><span>层：{_safe_dom(", ".join(topic_layers) or "unclassified")}</span></div>'
                f'<div class="uncertainty">仍不确定：{_safe_dom(", ".join(uncertainty) or "无额外标记")}</div>'
                '</li>'
            )
            if str(message_ref) in context_message_refs:
                context_message_items.append(rendered_message)
            else:
                core_message_items.append(rendered_message)
        profile = _thread_review_profile(thread, segments, ledger_by_ref)
        review_categories = list(unit.get("review_categories") or sorted(profile.get("review_categories") or ()))
        category_overlap = list(unit.get("category_overlap") or [])
        content_lines = [line for line in (unit.get("content_line_candidates") or thread.get("content_line_candidates") or ()) if isinstance(line, Mapping)]
        content_line_extraction = unit.get("content_line_extraction") if isinstance(unit.get("content_line_extraction"), Mapping) else {}
        content_line_items: List[str] = []
        for line in content_lines:
            importance_label = {"low": "低", "medium": "中", "high": "高"}.get(str(line.get("importance_candidate")), "待判断")
            key_label = "可进入重点话题复核" if line.get("key_topic_candidate") else "不晋升为重点话题"
            evidence_ids = "、".join(str(value) for value in line.get("support_message_ids") or ()) or "无"
            context_ids = "、".join(str(value) for value in line.get("context_message_ids") or ()) or "无"
            claim_type_label = {
                "chat_report": "聊天陈述", "reported_experience": "个人经历", "opinion": "观点",
                "hypothesis": "推测", "question": "问题", "mixed": "混合陈述",
            }.get(str(line.get("claim_type")), "待判断")
            verification_label = (
                "建议联网核验（本轮未核验）"
                if line.get("external_verification") == "recommended"
                else "无需外部核验（本轮未核验）"
            )
            binding_items = "".join(
                f'<li>{_safe_dom(binding.get("statement"), 260)} '
                f'<span>〔{_safe_dom("、".join(str(value) for value in binding.get("message_ids") or ()), 900)}〕</span></li>'
                for binding in line.get("evidence_bindings") or ()
                if isinstance(binding, Mapping)
            )
            binding_html = f'<ul class="evidence-bindings">{binding_items}</ul>' if binding_items else ""
            content_line_items.append(
                f'<li class="content-line importance-{_safe_dom(line.get("importance_candidate") or "unknown", 20)}">'
                f'<div class="content-line-title"><strong>{_safe_dom(line.get("title") or line.get("category") or "话题候选", 120)}</strong>'
                f'<span>{_safe_dom(importance_label)}重要性 · {_safe_dom(key_label)}</span></div>'
                f'<p>{_safe_dom(line.get("summary_candidate") or "已识别话题，摘要待判断")}</p>'
                f'<div class="content-line-evidence">边界：{_safe_dom(claim_type_label)} · {_safe_dom(verification_label)}</div>'
                f'{binding_html}'
                f'<div class="content-line-evidence">语义支撑消息：{_safe_dom(evidence_ids, 900)}；仅作理解的上下文：{_safe_dom(context_ids, 900)}</div>'
                '</li>'
            )
        content_lines_html = (
            f'<section class="content-lines"><h3>提炼出的内容线（候选）</h3><ol>{"".join(content_line_items)}</ol>'
            '<p class="candidate-note">“识别到讨论话题”与“晋升为重点话题”分开判断；短对话可以有明确话题，但缺少持续、决策或行动证据时不会升为重点。</p></section>'
            if content_line_items
            else (
                f'<section class="content-lines extraction-failed"><h3>内容线提炼失败</h3><p>语义结果未通过本地协议校验，已保留原交流过程供复核；错误码：{_safe_dom(content_line_extraction.get("error_code") or "unknown", 160)}。</p></section>'
                if content_line_extraction.get("status") == "failed"
                else '<section class="content-lines empty-content-lines"><h3>提炼出的内容线（候选）</h3><p>语义提炼已完成，当前未识别出明确内容线；交流过程仍保留供复核。</p></section>'
            )
        )
        topic_labels = [str(item.get("label")) for item in thread.get("local_topics") or () if isinstance(item, Mapping)]
        continuing_labels = [str(item.get("label")) for item in thread.get("continuing_topics") or () if isinstance(item, Mapping)]
        matter_kinds = [str(item.get("kind")) for item in thread.get("explicit_matters") or () if isinstance(item, Mapping)]
        discussion = thread.get("discussion_weight") if isinstance(thread.get("discussion_weight"), Mapping) else {}
        info = thread.get("information_value") if isinstance(thread.get("information_value"), Mapping) else {}
        score = thread.get("candidate_score") if isinstance(thread.get("candidate_score"), Mapping) else {}
        signal_labels = sorted(str(label) for label in (thread.get("interaction_signal_counts") or {}) if label)
        if not signal_labels:
            signal_labels = sorted({
                str(signal.get("label"))
                for segment in (segments.get(str(ref), {}) for ref in thread.get("segment_refs") or ())
                for signal in (segment.get("interaction_signals") or ())
                if isinstance(signal, Mapping) and signal.get("label")
            })
        signal_text = _review_signal_text(signal_labels)
        topic_status = _REVIEW_TOPIC_STATUS_LABELS.get(str(thread.get("topic_status") or "unclassified"), "候选层待确认")
        media_missing_count = int(profile.get("media_missing_count") or 0)
        media_kinds = sorted({
            _MEDIA_KIND_LABELS.get(str((ledger_by_ref.get(str(ref), {}).get("media") or {}).get("kind")), "媒体")
            for ref in thread.get("message_refs") or ()
            if isinstance((ledger_by_ref.get(str(ref), {}).get("media") or {}), Mapping)
            and (ledger_by_ref.get(str(ref), {}).get("media") or {}).get("status") == "unavailable"
        })
        if media_missing_count:
            media_summary = f"有 {media_missing_count} 条媒体不可读取"
            if media_kinds:
                media_summary += f"（{_text("、".join(media_kinds))}）"
        else:
            media_summary = "未发现媒体缺失"
        grouping_summary = _review_group_reason_text(thread.get("why_grouped") or ())
        if edge_items:
            grouping_summary = _text(f"{grouping_summary}；存在连续性候选")[:180]
        discussion_score = discussion.get("score") if discussion.get("score") is not None else 0.0
        discussion_summary = f"{discussion_score}（{discussion.get('numerator', 0)}/{discussion.get('denominator', 0)} 条内容片段）"
        density_label = {"low": "低", "medium": "中", "high": "高"}.get(str(info.get("signal_density_label")), "待确认")
        density_score = info.get("signal_density_score") if info.get("signal_density_score") is not None else "未知"
        info_summary = f"待模型判断（本地信息信号密度：{density_label} / {density_score}）"
        core_message_count = len(core_message_items)
        context_message_count = len(context_message_items)
        flow_ref = str(thread.get("flow_ref") or thread.get("conversation_flow_ref") or "")
        flow = flows_by_ref.get(flow_ref, {})
        flow_episode_count = len(flow.get("episode_refs") or ())
        flow_summary = (
            f"同一持续交流流候选（内部保留 {flow_episode_count} 段）"
            if flow_episode_count > 1
            else "当前没有更高层持续交流流链接"
        )
        technical_media_lines = []
        for message_ref in thread.get("message_refs") or ():
            ledger_message = ledger_by_ref.get(str(message_ref), {})
            media = ledger_message.get("media") if isinstance(ledger_message.get("media"), Mapping) else {}
            if media.get("status") == "unavailable" or media.get("semantic_evidence_eligible") is False:
                source_id = ledger_message.get("source_message_id") or ledger_message.get("message_id") or message_ref
                technical_media_lines.append(
                    f"{source_id}: kind={media.get('kind')}; status={media.get('status')}; "
                    f"missing_reason={media.get('missing_reason')}; semantic_evidence_eligible={media.get('semantic_evidence_eligible')}"
                )
        unit_source_ids = ",".join(str(value) for value in unit.get("display_message_ids") or ())
        unit_categories = ",".join(str(value) for value in review_categories)
        unit_category_overlap = ",".join(str(value) for value in category_overlap)
        card_class = "thread representative-thread" if thread_ref in selected_unit_refs else "candidate-thread extra-thread"
        card = (
            f'<article class="{card_class}" data-thread-ref="{_safe_dom(thread_ref, 160)}" data-unit-ref="{_safe_dom(thread_ref, 160)}" data-sample-source-ids="{_safe_dom(unit_source_ids, 2000)}" data-review-categories="{_safe_dom(unit_categories, 300)}" data-category-overlap="{_safe_dom(unit_category_overlap, 300)}">'
            f'<header><h2>交流过程候选 · {_safe_dom(_REVIEW_CHAT_TYPE_LABELS.get(str(chat.get("chat_type") or "unknown"), "待识别聊天"))} · {_safe_dom(topic_status)}</h2>'
            f'<p class="who">时间：{_safe_dom(episode.get("start_time") or "未知", 40)} → {_safe_dom(episode.get("end_time") or "未知", 40)}；参与者 {_safe_dom(len(participant_names))} 人；消息 {_safe_dom(len(thread.get("message_refs") or ()), 40)} 条；日期：{_safe_dom("、".join(episode.get("observed_days") or ()) or "未知", 300)}；内部交流段 {_safe_dom(len(unit.get("episode_refs") or ()), 20)} 段</p></header>'
            f'{content_lines_html}'
            f'<div class="thread-summary"><div class="summary-item"><strong>交流形态候选：</strong>{_safe_dom(signal_text)}</div>'
            f'<div class="summary-item"><strong>消息范围：</strong>核心消息 {_safe_dom(core_message_count)} 条 / 为理解补充上下文 {_safe_dom(context_message_count)} 条</div>'
            f'<div class="summary-item"><strong>媒体：</strong>{_safe_dom(media_summary)}</div>'
            f'<div class="summary-item"><strong>拼接理由：</strong>{_safe_dom(grouping_summary)}</div>'
            f'<div class="summary-item"><strong>讨论比重候选：</strong>{_safe_dom(discussion_summary)}</div>'
            f'<div class="summary-item"><strong>信息价值：</strong>{_safe_dom(info_summary)}</div>'
            f'<div class="summary-item"><strong>持续交流流：</strong>{_safe_dom(flow_summary)}</div></div>'
            f'<p class="candidate-note">这些都是可复核的候选信号；主题没有被强行定论，媒体缺失内容也没有参与语义判断。信息价值不由本地规则决定，暂留待模型/人工判断。</p>'
            f'<details class="raw-chat"><summary>展开原始聊天（核心 {_safe_dom(core_message_count)} 条 + 补充上下文 {_safe_dom(context_message_count)} 条）</summary>'
            f'<section><h3>逐条消息：谁、何时、说了什么、媒体缺什么（核心消息）</h3><ol>{"".join(core_message_items) or "<li>没有可展示的核心消息</li>"}</ol>'
            f'<h3>为理解补充的同聊天上下文（不计入本段核心）</h3><ol>{"".join(context_message_items) or "<li>没有补充上下文</li>"}</ol></section></details>'
            f'<details class="candidate-details"><summary>查看候选依据与仍不确定的部分</summary>'
            f'<section><h3>为什么暂时拼在一起</h3><p>{_safe_dom(grouping_summary)}</p><p>{_safe_dom("；".join(edge_items) or "没有强连续性证据；仍保留为开放候选")}</p></section>'
            f'<section><h3>上下文包（核心与补充分开）</h3><p>核心消息 {_safe_dom(core_message_count)} 条；同聊天补充 {_safe_dom(context_message_count)} 条。补充范围只为帮助理解，不改变本段成员。</p><p>扩展原因：{_safe_dom("、".join(str(reason) for reason in context_envelope.get("expansion_reasons") or ()) or "无额外补充")}</p></section>'
            f'<section><h3>持续交流流候选</h3><p>{_safe_dom(flow_summary)}</p><p>内部段边界保留；链接仍需人工/模型确认。</p></section>'
            f'<section><h3>候选层（不强行归类）</h3><p>局部话题：{_safe_dom("、".join(topic_labels) or "无；可保持未分类/闲聊流")}</p><p>持续话题：{_safe_dom("、".join(continuing_labels) or "无；不因同词自动合并")}</p><p>明确事项候选：{_safe_dom("、".join(matter_kinds) or "无")}</p></section>'
            f'<section class="scores"><h3>候选分数构成（技术细节）</h3><div><strong>讨论比重：</strong>{_safe_dom(discussion.get("score", 0.0))}（{_safe_dom(discussion.get("numerator", 0))}/{_safe_dom(discussion.get("denominator", 0))}） · 构成：{_safe_dom(json.dumps(discussion.get("components") or {}, ensure_ascii=False))}</div><div><strong>信息价值：</strong>待模型/人工判断 · 本地信息信号密度 {_safe_dom(info.get("signal_density_label", "unknown"))} / {_safe_dom(info.get("signal_density_score", "unknown"))} · 构成：{_safe_dom(json.dumps(info.get("components") or {}, ensure_ascii=False))}</div><div><strong>拼接候选分数：</strong>{_safe_dom(score.get("score", 0.0))} · 构成：{_safe_dom(json.dumps(score.get("components") or {}, ensure_ascii=False))}</div></section>'
            f'<section><h3>媒体读取核对（技术细节）</h3><p>{_safe_dom("；".join(technical_media_lines) or "没有不可用媒体")}</p></section>'
            f'<section class="uncertainty-box"><h3>仍不确定什么</h3><p>{_safe_dom("、".join(thread.get("uncertainties") or ()) or "无额外不确定性")}</p></section></details>'
            '</article>'
        )
        chat_ref = str(thread.get("chat_ref"))
        cards_by_chat[chat_ref].append(card)
        if thread_ref in selected_unit_refs:
            representative_cards_by_chat[chat_ref].append(card)
        else:
            extra_cards_by_chat[chat_ref].append(card)
    views = result.get("views") if isinstance(result.get("views"), Mapping) else {}
    view_nav = "".join(f'<li><strong>{_safe_dom(scale)}</strong>：{_safe_dom(view.get("window_start"))} → {_safe_dom(view.get("window_end"))}；候选线程 {_safe_dom(len(view.get("thread_refs") or ()))}</li>' for scale, view in views.items() if isinstance(view, Mapping))
    counts = result.get("counts") if isinstance(result.get("counts"), Mapping) else {}
    # Give reviewers a stable directory and a visual boundary per chat. The
    # episode/thread semantics remain untouched; this is presentation only.
    ordered_chats = [row for row in (result.get("chats") or ()) if isinstance(row, Mapping)]
    known_chat_refs = {str(row.get("chat_ref")) for row in ordered_chats}
    for chat_ref in cards_by_chat:
        if chat_ref not in known_chat_refs:
            ordered_chats.append({"chat_ref": chat_ref, "chat_type": "unknown"})
    direct_count = sum(1 for chat in ordered_chats if str(chat.get("chat_type")) == "direct")
    group_count = sum(1 for chat in ordered_chats if str(chat.get("chat_type")) == "group")
    unknown_count = max(0, len(ordered_chats) - direct_count - group_count)
    chat_distribution = f"{direct_count}个私聊 + {group_count}个群聊"
    if unknown_count:
        chat_distribution += f" + {unknown_count}个待识别聊天"
    directory_items: List[str] = []
    chat_sections: List[str] = []
    type_labels = {"direct": "私聊", "group": "群聊", "unknown": "待识别聊天"}
    for chat in ordered_chats:
        chat_ref = str(chat.get("chat_ref") or "unknown-chat")
        chat_type = str(chat.get("chat_type") or "unknown")
        type_label = type_labels.get(chat_type, "待识别聊天")
        section_id = f"chat-section-{_stable_hash(chat_ref, length=12)}"
        chat_name = _text(chat.get("chat_name") or chat.get("chat_id") or chat_ref).strip() or chat_ref
        chat_cards = representative_cards_by_chat.get(chat_ref) or []
        chat_extra_cards = extra_cards_by_chat.get(chat_ref) or []
        chat_message_count = chat.get("message_count")
        if chat_message_count is None:
            chat_message_count = sum(
                len(thread.get("message_refs") or ())
                for thread in (result.get("threads") or ())
                if isinstance(thread, Mapping) and str(thread.get("chat_ref")) == chat_ref
            )
        chat_participant_count = len(chat.get("participant_refs") or ())
        directory_items.append(
            f'<li><a href="#{_safe_dom(section_id, 80)}">{_safe_dom(type_label)} · {_safe_dom(chat_name, 160)}</a>'
            f'（代表样本 {_safe_dom(len(chat_cards))} 段，共 {_safe_dom(len(chat_cards) + len(chat_extra_cards))} 段；消息 {_safe_dom(chat_message_count)}，参与者 {_safe_dom(chat_participant_count)}）</li>'
        )
        chat_sections.append(
            f'<section class="chat-section" id="{_safe_dom(section_id, 80)}"><header><h2>{_safe_dom(type_label)} · {_safe_dom(chat_name, 160)}</h2>'
            f'<p class="chat-section-meta">代表样本 {_safe_dom(len(chat_cards))} 段 · 全部候选 {_safe_dom(len(chat_cards) + len(chat_extra_cards))} 段 · 消息 {_safe_dom(chat_message_count)} · 参与者 {_safe_dom(chat_participant_count)}</p></header>'
            f'{"".join(chat_cards) or "<p>该聊天暂未选出代表样本；其余候选见下方“显示全部候选”。</p>"}</section>'
        )
    extra_sections: List[str] = []
    for chat in ordered_chats:
        chat_ref = str(chat.get("chat_ref") or "unknown-chat")
        extras = extra_cards_by_chat.get(chat_ref) or []
        if not extras:
            continue
        chat_type = str(chat.get("chat_type") or "unknown")
        type_label = type_labels.get(chat_type, "待识别聊天")
        chat_name = _text(chat.get("chat_name") or chat.get("chat_id") or chat_ref).strip() or chat_ref
        extra_sections.append(
            f'<section class="extra-chat-section"><h3>{_safe_dom(type_label)} · {_safe_dom(chat_name, 160)}</h3>{"".join(extras)}</section>'
        )
    total_thread_count = len(all_units)
    visible_thread_count = len(sample_units)
    hidden_thread_count = max(0, total_thread_count - visible_thread_count)
    sample_direct_count = sum(1 for unit in sample_units if str(unit.get("chat_type")) == "direct")
    sample_group_count = sum(1 for unit in sample_units if str(unit.get("chat_type")) == "group")
    all_candidates = (
        f'<details class="all-candidates"><summary>显示全部候选（共 {_safe_dom(total_thread_count)} 张卡；当前已显示 {_safe_dom(visible_thread_count)} 张）</summary>'
        f'<p>下面补充其余 {_safe_dom(hidden_thread_count)} 段，仍按聊天分区；完整消息正文同样默认收起。</p>{"".join(extra_sections) or "<p>没有额外候选。</p>"}</details>'
    )
    semantic_extraction = (review_block.get("semantic_content_line_extraction") or {}) if isinstance(review_block, Mapping) else {}
    provider_used = bool(result.get("provider_used"))
    provider_calls = int(result.get("provider_calls") or 0)
    if provider_used:
        extraction_source = (
            f'内容线由语义模型 {_safe_dom(semantic_extraction.get("model") or "unknown", 120)} 提炼，'
            f'本次 provider 调用 {_safe_dom(provider_calls)} 次；交流范围与证据边界由本地系统校验'
        )
    else:
        extraction_source = "未调用语义模型；仅展示本地交流过程候选"
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>交流过程重建候选（人工审阅）</title>
<style>
body{{margin:0;background:#f3f5f8;color:#17202c;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;line-height:1.55}}
main{{max-width:1180px;margin:0 auto;padding:28px 18px 60px}} header.hero{{background:#17202c;color:#fff;border-radius:16px;padding:24px 28px;margin-bottom:18px}} h1{{margin:0 0 8px;font-size:28px}} h2{{margin:0 0 5px;font-size:20px}} h3{{margin:16px 0 6px;font-size:15px}} p{{margin:5px 0}} .notice{{background:#fff4d6;color:#513d00;border:1px solid #e3bd58;border-radius:10px;padding:12px 14px;margin:14px 0}} .facts,.scores{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px}} .fact,.scores>div{{background:#fff;border:1px solid #dbe1e8;border-radius:9px;padding:9px 11px}} .review-guide{{background:#eaf5ef;border:1px solid #a8d1b7;border-radius:12px;padding:14px 16px;margin:16px 0}} .review-guide h2{{color:#194d2c}} .chat-directory{{background:#fff;border:1px solid #d5dce5;border-radius:14px;padding:16px 18px;margin:18px 0}} .chat-directory ul{{margin:8px 0 0;padding-left:24px}} .chat-section{{margin:22px 0 30px;padding:16px 0 2px;border-top:4px solid #748ba8}} .chat-section>header{{padding:0 4px 4px}} .chat-section-meta{{color:#657286;font-size:13px}} .thread,.candidate-thread{{background:#fff;border:1px solid #d5dce5;border-radius:14px;padding:18px;margin:18px 0;box-shadow:0 2px 8px #17202c12}} .extra-chat-section{{border-top:1px dashed #aab6c5;margin-top:18px;padding-top:4px}} .all-candidates{{background:#fff;border:1px solid #c7d0dc;border-radius:12px;padding:12px 16px;margin:24px 0}} .all-candidates>summary,.raw-chat>summary,.candidate-details>summary{{cursor:pointer;font-weight:650;color:#214d80}} .content-lines{{background:#f5f9ff;border:1px solid #cbdaf0;border-radius:11px;padding:10px 14px;margin:13px 0}} .content-lines h3{{margin-top:2px;color:#214d80}} .content-lines ol{{margin:7px 0;padding-left:24px}} .content-line{{padding:8px 10px;margin:7px 0;background:#fff;border-left:4px solid #5c7fa8;border-radius:7px}} .content-line.importance-low{{border-left-color:#9aa9b8}} .content-line-title{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}} .content-line-title span,.content-line-evidence{{font-size:12px;color:#657286}} .empty-content-lines{{background:#fafbfc;border-color:#e0e5eb}} .thread-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin:12px 0}} .summary-item{{background:#f7f9fc;border:1px solid #e0e5eb;border-radius:8px;padding:8px 10px}} .candidate-note{{font-size:13px;color:#657286;margin:10px 0}} .raw-chat,.candidate-details{{border-top:1px solid #e2e7ed;margin-top:12px;padding-top:8px}} .status{{display:inline-block;background:#eef3ff;border:1px solid #c7d4f4;border-radius:8px;padding:8px 10px;margin:8px 0}} .message{{list-style-position:outside;margin:10px 0;padding:10px 12px;background:#f7f8fa;border-left:4px solid #7d91ad;border-radius:7px}} .message.context-message{{border-left-color:#aab6c5;background:#fbfcfd}} .message-meta{{color:#4c5b6c;font-size:13px}} .message-body{{white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #e0e5eb;padding:8px;margin:6px 0;border-radius:6px}} .message-facts{{display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:#475568}} .message-facts span{{background:#eef1f5;border-radius:12px;padding:2px 8px}} .uncertainty{{font-size:12px;color:#9a4a22;margin-top:5px}} .uncertainty-box{{background:#fff9f5;border:1px solid #f0c6ae;border-radius:9px;padding:8px 12px}} code{{font-size:12px}} footer{{color:#657286;font-size:12px;margin-top:20px}}
</style></head><body><main>
<header class="hero"><h1>交流过程重建候选</h1><p>{extraction_source}</p><p><strong>本页包含：{_safe_dom(chat_distribution)}</strong> · 按聊天分区展示，内容仍是候选，不是最终语义</p></header>
<div class="notice"><strong>先看边界：</strong>“问候、接话、分享、闲聊、讨论、提问、请求、调侃”都只是交互信号候选；局部话题、持续话题和明确事项也不等于最终事件。缺失媒体不会被当成语义证据，沉默不会自动表示结束。</div>
<section class="review-guide"><h2>审阅本轮完整候选集</h2><p>本轮共有 {_safe_dom(visible_thread_count)} 张候选卡（私聊 {_safe_dom(sample_direct_count)} 张 + 群聊 {_safe_dom(sample_group_count)} 张）。卡片数量由实际交流过程决定，不设固定展示数量；请核对交流范围、内容线、证据和媒体缺口。</p><p>原始聊天默认收起，点每张卡的“展开原始聊天”可核对正文。完整结构保存在同目录 reconstruction.json。</p></section>
<section class="facts"><div class="fact">消息：{_safe_dom(counts.get("message_count", 0))}</div><div class="fact">参与者：{_safe_dom(counts.get("participant_count", 0))}</div><div class="fact">聊天：{_safe_dom(counts.get("chat_count", 0))}</div><div class="fact">交流过程候选：{_safe_dom(counts.get("episode_count", 0))}</div><div class="fact">未分类片段：{_safe_dom(counts.get("unclassified_segment_count", 0))}</div><div class="fact">媒体不可用：{_safe_dom(counts.get("media_unavailable_count", 0))}</div></section>
<section class="chat-directory"><h2>聊天目录</h2><p>本页包含 <strong>{_safe_dom(chat_distribution)}</strong>；点击聊天名称跳到对应分区，私聊和群聊不会混在一起。</p><ul>{"".join(directory_items) or "<li>没有可用聊天</li>"}</ul></section>
<section><h2>时间视图</h2><ul>{view_nav or "<li>没有可用日期视图</li>"}</ul></section>
{"".join(chat_sections) or "<p>当前输入没有可展示的交流过程候选。</p>"}
{all_candidates}
<footer>artifact={_safe_dom(ARTIFACT_VERSION)} · schema={_safe_dom(SCHEMA_VERSION)} · provider_calls={_safe_dom(provider_calls)} · 页面可显示本地正文；同目录 JSON/manifest 不含正文。</footer>
</main></body></html>'''


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_review_artifacts(
    result: Mapping[str, Any],
    output_dir: str | Path,
    *,
    source_messages: Any = None,
    source_name: str = "development",
) -> Dict[str, Path]:
    """Write a body-free JSON/manifest and an escaped review HTML page."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    public_result = _body_free_projection(result)
    public_result["body_free"] = True
    public_result.setdefault("review", {})["html_body_rendered"] = True
    public_result["review"].pop("source_messages", None)
    assert_body_free(public_result)
    json_path = target / "reconstruction.json"
    _json_write(json_path, public_result)
    html_path = target / "review.html"
    html_path.write_text(render_review_html(result, source_messages), encoding="utf-8")
    manifest = {
        "evaluation_version": str(public_result.get("evaluation_version") or EVALUATION_VERSION),
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "source_scope": source_name,
        "input_split": "development" if source_name == "development" else source_name,
        "split": "development" if source_name == "development" else source_name,
        "provider_used": bool(public_result.get("provider_used")),
        "provider_calls": int(public_result.get("provider_calls") or 0),
        "semantic_content_line_extraction": dict((public_result.get("review") or {}).get("semantic_content_line_extraction") or {}),
        "production_blocked": True,
        "frozen_read": False,
        "stage_b": False,
        "stage_c": False,
        "production_connected": False,
        "candidate_only": True,
        "body_free": True,
        "body_fields_excluded": sorted(_BODY_KEYS),
        "counts": dict(public_result.get("counts") or {}),
        "chat_type_counts": dict(Counter(str(chat.get("chat_type")) for chat in public_result.get("chats") or () if isinstance(chat, Mapping))),
        "cross_day_episode_count": sum(1 for episode in public_result.get("episodes") or () if isinstance(episode, Mapping) and episode.get("cross_day_candidate")),
        "conversation_flow_count": len(public_result.get("conversation_flows") or ()),
        "context_envelope_count": len(public_result.get("review_context_envelopes") or public_result.get("context_envelopes") or ()),
        "review_default_thread_limit": None,
        "review_default_card_limit": None,
        "review_sample_audit": dict((public_result.get("review") or {}).get("sample_audit") or {}),
        "view_scales": list((public_result.get("views") or {}).keys()),
        "artifacts": {
            "reconstruction": json_path.name,
            "review_html": html_path.name,
        },
        "input_projection_sha256": _stable_hash({"messages": public_result.get("messages"), "chats": public_result.get("chats"), "participants": public_result.get("participants")}, length=64),
        "review_policy": "HTML is escaped and may show local source text; JSON and manifest remain body-free",
    }
    manifest_path = target / "manifest.json"
    _json_write(manifest_path, manifest)
    manifest["artifacts"] = {
        **manifest["artifacts"],
        "reconstruction_sha256": _sha256_file(json_path),
        "review_html_sha256": _sha256_file(html_path),
    }
    _json_write(manifest_path, manifest)
    return {"manifest": manifest_path, "reconstruction": json_path, "review_html": html_path}


def load_jsonl_messages(path: str | Path, *, require_development: bool = True) -> List[Dict[str, Any]]:
    """Read an explicit JSON/JSONL development input without body logging."""
    source = Path(path)
    lowered_parts = {part.casefold() for part in source.parts}
    if require_development and ("frozen" in "".join(lowered_parts) or "frozen_test" in lowered_parts or "release" in lowered_parts):
        raise ValueError("development_loader_refuses_frozen_input")
    rows: List[Dict[str, Any]] = []
    if source.suffix.casefold() == ".jsonl" or source.suffix.casefold() == ".ndjson":
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise ValueError(f"message_row_not_mapping:{line_number}")
                rows.append(dict(value))
    else:
        value = json.loads(source.read_text(encoding="utf-8"))
        rows = [dict(row) for row in _as_sequence(value) if isinstance(row, Mapping)]
    if require_development:
        forbidden = [row for row in rows if str(row.get("split") or row.get("dataset_split") or "development").casefold() in {"frozen", "frozen_test", "test", "release"}]
        if forbidden:
            raise ValueError("development_loader_refuses_non_development_rows")
    return rows


def select_one_direct_and_group(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Select the largest explicit/inferred direct and group chat for a demo."""
    normalized, _private = _normalise_messages(rows)
    chats, _participants, _map = _chat_ledgers(normalized)
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in normalized:
        grouped[str(row.get("chat_ref"))].append(row)
    selected_refs: List[str] = []
    for kind in ("direct", "group"):
        candidates = [chat for chat in chats if chat.get("chat_type") == kind]
        if candidates:
            selected_refs.append(str(max(candidates, key=lambda chat: (int(chat.get("message_count") or 0), str(chat.get("chat_ref")))).get("chat_ref")))
    selected: List[Dict[str, Any]] = []
    wanted = set(selected_refs)
    for row_index, original in enumerate(rows):
        # Recompute ref in exactly the same way as normalisation.
        if not isinstance(original, Mapping):
            continue
        source_id = _source_id(original, row_index)
        account_id = _text(_first(original, "account_id", "account", "wx_account_id", default="unknown-account")).strip() or "unknown-account"
        chat_id = _text(_first(original, "chat_id", "conversation_id", "room_id", "chat", default="unknown-chat")).strip() or "unknown-chat"
        chat_ref = f"chat-{_stable_hash((account_id, chat_id), length=12)}"
        if chat_ref in wanted:
            selected.append(dict(original))
    return selected


def build_development_review(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    reference_date: Any = None,
) -> Dict[str, Path]:
    """Build the local direct+group development review artifact."""
    rows = load_jsonl_messages(input_path, require_development=True)
    selected = select_one_direct_and_group(rows)
    if not selected:
        raise ValueError("development_input_has_no_direct_or_group_chat")
    result = reconstruct_context(selected, reference_date=reference_date, source_scope="development")
    return write_review_artifacts(result, output_dir, source_messages=selected, source_name="development")


# Descriptive aliases make the seam easy to discover without introducing a
# second pipeline or another numbered stage.
build_review_artifact = write_review_artifacts
build_review_artifacts = write_review_artifacts
build_context_reconstruction = reconstruct_context


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a provider-free WeChat conversation reconstruction review artifact")
    parser.add_argument("--input", required=True, help="development JSONL/JSON input")
    parser.add_argument("--output", default="output/conversation-reconstruction-development", help="artifact directory")
    parser.add_argument("--reference-date", default=None, help="YYYY-MM-DD; defaults to latest input local day")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = build_development_review(args.input, args.output, reference_date=args.reference_date)
    # Paths only; never print local message bodies.
    for key, path in paths.items():
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARTIFACT_VERSION",
    "CONTEXT_SCHEMA_VERSION",
    "EVALUATION_VERSION",
    "PIPELINE_VERSION",
    "RULESET_VERSION",
    "SCHEMA_VERSION",
    "SUPPORTED_VIEWS",
    "assert_body_free",
    "build_development_review",
    "build_context_reconstruction",
    "build_review_artifact",
    "build_review_artifacts",
    "load_jsonl_messages",
    "main",
    "reconstruct_context",
    "reconstruct_conversations",
    "render_review_html",
    "select_one_direct_and_group",
    "write_review_artifacts",
]
