"""Deterministic conversation-window segmentation for the semantic shadow path.

The production analysis path intentionally does not import this module.  It is
small, pure, and useful to the offline gold-standard builder and to synthetic
tests.  A message is never removed: segmentation returns a role for every
input message and keeps the original mapping (with non-destructive annotation
fields) in ``result.messages``.

The segmenter is deliberately conservative.  A greeting is not an event, but
it is a useful boundary marker: a greeting immediately before a substantive
turn remains in the same segment even when the topic introduced by that turn
is new.  Long gaps split a segment unless the source contains an explicit
reply/reference to an earlier message.  Topic changes are only split when the
change is explicit or is separated by a meaningful pause; this avoids turning
normal turn-taking into many one-message events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


DEFAULT_MAX_GAP_SECONDS = 15 * 60
"""Default conversation-window gap, matching the design contract."""

ROLE_CONVERSATION_OPENER = "conversation_opener"
ROLE_CONTEXT_ONLY = "context_only"
ROLE_SUBSTANTIVE = "substantive"
MESSAGE_ROLES = frozenset(
    {ROLE_CONVERSATION_OPENER, ROLE_CONTEXT_ONLY, ROLE_SUBSTANTIVE}
)


# The list is intentionally short and high precision.  It covers common
# Chinese and English openers without treating a topic-bearing sentence that
# happens to contain a polite word as a greeting.  Matching is done after
# punctuation/emoji normalization, so "你好，最近怎么样？" is still social
# context while "你好，GPT 又重置了" is mixed and remains substantive.
_GREETING_PHRASES = (
    "最近还好吗",
    "最近怎么样",
    "最近好吗",
    "近来怎么样",
    "吃饭了吗",
    "吃了吗",
    "好久不见",
    "howareyou",
    "howsitgoing",
    "whatsup",
    "goodmorning",
    "goodafternoon",
    "goodevening",
    "goodnight",
    "thankyou",
    "areyouthere",
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "哈罗",
    "嘿",
    "早上好",
    "上午好",
    "中午好",
    "下午好",
    "晚上好",
    "晚安",
    "辛苦了",
    "辛苦",
    "谢谢",
    "感谢",
    "谢了",
    "多谢",
    "在吗",
    "忙吗",
    "hi",
    "hello",
    "hey",
    "thanks",
    "ok",
    "okay",
)
_GREETING_PHRASES = tuple(sorted(set(_GREETING_PHRASES), key=len, reverse=True))
_SOCIAL_FILLERS = frozenset("啊呀哦喔哇啦呢哈嘛了呐")
_SOCIAL_WORDS = frozenset(
    {
        "收到",
        "好的",
        "好",
        "嗯",
        "哦",
        "啊",
        "哈哈",
        "哈哈哈",
        "感谢",
        "谢谢",
        "辛苦",
        "辛苦了",
        "没问题",
        "明白",
        "行",
        "可以",
        "ok",
        "okay",
        "thanks",
        "thankyou",
    }
)

# These are deliberately exact, short acknowledgement/confirmation turns.
# A prefix match is not used: ``确认项目上线状态`` and ``你好，接口失败了`` must
# remain substantive.  Keep this list separate from ``_SOCIAL_WORDS`` so the
# long-standing ``is_greeting_only`` API retains its opener-oriented meaning.
_CONTEXT_ONLY_PHRASES = frozenset(
    {
        "确认",
        "已确认",
        "已经确认",
        "确认了",
        "确认收到",
        "收到",
        "收到了",
        "收到啦",
        "明白",
        "明白了",
        "了解",
        "了解了",
        "知道了",
        "同意",
        "没问题",
        "没事",
        "可以",
        "行",
        "好的",
        "好",
        "嗯",
        "哦",
        "啊",
        "谢谢",
        "谢谢你",
        "感谢",
        "辛苦",
        "辛苦了",
        "不客气",
        "不用谢",
        "已阅",
        "看到了",
        "gotit",
        "ok",
        "okay",
        "thanks",
        "thankyou",
    }
)
_MEDIA_MESSAGE_TYPES = frozenset(
    {"image", "video", "audio", "voice", "file", "sticker", "emoji", "system", "location", "media"}
)
_MEDIA_PLACEHOLDER = re.compile(r"^(?:\[[^\]]{1,120}\]|<[^>]{1,120}>)$")

_PUNCT_OR_SYMBOL = re.compile(r"[\W_]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_PLACEHOLDER = re.compile(r"\[[^\]]{1,120}\]")
_LATIN_TOPIC = re.compile(r"(?i)(?<![a-z0-9])(?:[a-z][a-z0-9_-]{2,}|\d+[a-z][a-z0-9_-]*)")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,}")

# Topic families are intentionally broad.  They are used for conservative
# continuity/split decisions, not for event extraction or user-facing copy.
_TOPIC_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "ai_model",
        re.compile(r"(?i)(?:gpt|codex|claude|deepseek|llm|模型|人工智能|多模态|推理|token)") ,
    ),
    (
        "account_platform",
        re.compile(r"(?i)(?:账号|账户|登录|登陆|注册|验证码|邮箱|邮件|github|linux\.do|v2ex|平台)") ,
    ),
    (
        "cost_usage",
        re.compile(r"(?i)(?:额度|消耗|用量|费用|价格|成本|收费|预算|付款|订阅)") ,
    ),
    (
        "development_tooling",
        re.compile(r"(?i)(?:代码|编程|接口|api|部署|仓库|插件|工具|服务|版本|测试|bug)") ,
    ),
    (
        "project_work",
        re.compile(r"(?:项目|需求|客户|会议|方案|交付|上线|报名|课程|选课|课表|教务)") ,
    ),
    (
        "risk_failure",
        re.compile(r"(?:风险|故障|异常|失败|封禁|封号|风控|安全|漏洞|无法|中断|泄露)") ,
    ),
    (
        "communication_archive",
        re.compile(r"(?:群聊|私聊|聊天|消息|同步|归档|总结|日报|周报|转写|录音|发言人)") ,
    ),
    (
        "life_logistics",
        re.compile(r"(?:家人|朋友|吃饭|到家|生日|旅行|孩子|医院|快递|地址)") ,
    ),
)
_GENERIC_TOPIC_WORDS = frozenset(
    {
        "问题",
        "事情",
        "内容",
        "消息",
        "东西",
        "这个",
        "那个",
        "一下",
        "怎么",
        "如何",
        "是否",
        "可以",
        "需要",
        "请",
        "帮忙",
        "确认",
        "处理",
        "看看",
        "研究",
        "现在",
        "已经",
        "可能",
        "应该",
        "感觉",
        "觉得",
        "收到",
        "好的",
        "谢谢",
        "感谢",
    }
)
_TOPIC_SHIFT = re.compile(
    r"(?:换个话题|另一个话题|另外|顺便|对了|说到|还有一个|除此之外|再说一个|换句话说)"
)
_REFERENCE_CUE = re.compile(
    r"(?:这个|那个|它|这件事|这块|那块|上述|前面|刚才|继续|还是|照旧|同样|回复|引用)"
)
_DIRECT_REPLY_KEYS = (
    "reply_to_message_id",
    "reply_to_id",
    "quoted_message_id",
    "quote_message_id",
    "referenced_message_id",
    "reference_message_id",
    "parent_message_id",
    "in_reply_to",
)


def _jsonable(value: Any) -> Any:
    """Convert nested dataclasses/mappings to ordinary JSON values."""

    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _text_for(message: Mapping[str, Any]) -> str:
    value = message.get("redacted_text")
    if value is None:
        value = message.get("content", "")
    return str(value or "")


def _compact_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Symbols/punctuation include emoji, ASCII punctuation and CJK punctuation.
    # Keep letters and numbers so topic-bearing tokens cannot be erased.
    return _PUNCT_OR_SYMBOL.sub("", text)


def _social_compact(value: Any) -> str:
    compact = _compact_text(value)
    # Conversational particles are harmless in a greeting-only turn.  Do not
    # strip them from arbitrary substantive text; this helper is only called
    # when checking the compact candidate against social phrases.
    return "".join(char for char in compact if char not in _SOCIAL_FILLERS)


def is_greeting_only(text: Any) -> bool:
    """Return whether *text* contains only a greeting/social opener.

    This deliberately does not classify a mixed message as noise.  A prefix
    such as ``你好，帮我看一下接口`` returns ``False`` and remains eligible for
    substantive analysis.
    """

    compact = _social_compact(text)
    if not compact:
        return False
    if compact in _SOCIAL_WORDS:
        return True
    # Exact phrase plus a polite suffix (e.g. "你好呀" or "谢谢你") is still
    # social context.  The suffix is constrained so topic-bearing content does
    # not disappear behind a loose prefix match.
    for phrase in _GREETING_PHRASES:
        if compact == phrase:
            return True
        if compact.startswith(phrase):
            suffix = compact[len(phrase) :]
            if suffix in {"你", "您", "啦", "呀", "啊", "喔", "哈", "了", "呢", "吗"}:
                return True
    return False


def is_context_only_text(text: Any, *, message_type: Any = "text") -> bool:
    """Return whether a message is a pure social/acknowledgement turn.

    This helper is intentionally conservative.  It recognises only exact
    greeting, thanks and confirmation phrases (with harmless conversational
    particles removed).  Any additional object, action, state, question or
    claim text returns ``False`` so the message remains a substantive
    candidate.  Empty text is context-only only when the message type clearly
    identifies a media/system placeholder; unknown text is retained.
    """

    value = str(text or "").strip()
    kind = str(message_type or "text").strip().casefold()
    if not value:
        return kind in _MEDIA_MESSAGE_TYPES
    if kind in _MEDIA_MESSAGE_TYPES and _MEDIA_PLACEHOLDER.fullmatch(value):
        return True
    compact = _social_compact(value)
    if not compact:
        return False
    if compact in _CONTEXT_ONLY_PHRASES:
        return True
    return is_greeting_only(value)


def has_context_prefix(text: Any) -> bool:
    """Return whether a social/confirmation prefix has substantive suffix.

    This is used only to correct an over-broad upstream context label.  It is
    false for a pure phrase and true for turns such as ``确认项目状态`` or
    ``你好，接口失败了``.  Unknown text with no recognised prefix is left to
    its supplied role.
    """

    value = str(text or "").strip()
    compact = _compact_text(value)
    if not compact or is_context_only_text(value):
        return False
    prefixes = tuple(sorted(_CONTEXT_ONLY_PHRASES | set(_GREETING_PHRASES), key=len, reverse=True))
    return any(compact.startswith(prefix) for prefix in prefixes)


def has_greeting_prefix(text: Any) -> bool:
    """Return whether text starts with a recognized social opener."""

    compact = _compact_text(text)
    if not compact:
        return False
    return any(compact.startswith(phrase) for phrase in _GREETING_PHRASES)


def topic_keys(text: Any) -> Tuple[str, ...]:
    """Return stable, coarse topic-family keys for continuity decisions."""

    raw = str(text or "")
    if is_context_only_text(raw):
        return ()
    without_placeholders = _PLACEHOLDER.sub(" ", raw)
    keys = {name for name, pattern in _TOPIC_PATTERNS if pattern.search(without_placeholders)}
    for token in _LATIN_TOPIC.findall(without_placeholders):
        if token.casefold() not in {"the", "and", "are", "you", "for", "with", "this", "that"}:
            keys.add("token:" + token.casefold())
    for run in _CJK_RUN.findall(without_placeholders):
        # Keep only runs that include a non-generic character.  Short runs are
        # still useful for explicit names/products, hence the threshold of 2.
        if any(run[index : index + 2] not in _GENERIC_TOPIC_WORDS for index in range(len(run) - 1)):
            keys.add("cjk:" + run[:12])
    return tuple(sorted(keys))


def is_topic_bearing(text: Any, *, message_type: Any = "text") -> bool:
    """Whether a message contains an explicit, potentially evidential topic."""

    value = str(text or "").strip()
    if not value:
        return False
    if is_context_only_text(value, message_type=message_type):
        return False
    # Media-only placeholders provide useful context but no textual topic.
    if str(message_type or "text").casefold() not in {"text", "link"}:
        return False
    if topic_keys(value):
        return True
    # A non-social CJK sentence with at least three meaningful characters or a
    # normal Latin token is substantive even when it belongs to an unseen
    # domain.  Generic acknowledgements remain context-only.
    compact = _social_compact(value)
    if compact in _SOCIAL_WORDS:
        return False
    meaningful = "".join(char for char in compact if char not in _GENERIC_TOPIC_WORDS)
    return len(meaningful) >= 3 or bool(_LATIN_TOPIC.search(value))


def _message_time(message: Mapping[str, Any]) -> Optional[float]:
    """Return a comparable timestamp, preferring explicit relative offsets."""

    offset = message.get("time_offset_seconds")
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        return float(offset)
    if offset is not None:
        try:
            return float(str(offset))
        except (TypeError, ValueError):
            pass
    value = message.get("timestamp")
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _sequence_value(message: Mapping[str, Any]) -> Optional[int]:
    value = message.get("sequence_in_chat")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _speaker_id(message: Mapping[str, Any]) -> str:
    return str(
        message.get("speaker_id")
        or message.get("sender_id")
        or message.get("sender_name")
        or "unknown"
    )


def _account_id(message: Mapping[str, Any]) -> str:
    return str(message.get("account_id") or "default")


def _chat_id(message: Mapping[str, Any]) -> str:
    return str(message.get("chat_id") or "unknown")


def _message_id(message: Mapping[str, Any], fallback_index: int) -> str:
    value = message.get("message_id")
    if value is None or not str(value):
        return "MESSAGE_%06d" % (fallback_index + 1)
    return str(value)


def _reply_target(message: Mapping[str, Any]) -> Optional[str]:
    for key in _DIRECT_REPLY_KEYS:
        value = message.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _sort_key(item: Tuple[int, Mapping[str, Any]]) -> Tuple[Any, ...]:
    index, message = item
    sequence = _sequence_value(message)
    timestamp = _message_time(message)
    # A present sequence is the strongest local order signal.  Timestamp is a
    # deterministic tie-breaker for synthetic inputs with repeated sequence 0.
    return (
        _chat_id(message),
        _account_id(message),
        sequence is None,
        sequence if sequence is not None else 0,
        timestamp is None,
        timestamp if timestamp is not None else 0.0,
        _message_id(message, index),
        index,
    )


@dataclass(frozen=True)
class DialogueSegment:
    """A contiguous, same-chat conversation segment."""

    segment_id: str
    account_id: str
    chat_id: str
    message_ids: Tuple[str, ...]
    opener_message_ids: Tuple[str, ...]
    context_message_ids: Tuple[str, ...]
    substantive_message_ids: Tuple[str, ...]
    topic_bearing_message_ids: Tuple[str, ...]
    evidence_eligible_message_ids: Tuple[str, ...]
    topic_keys: Tuple[str, ...]
    speaker_ids: Tuple[str, ...]
    start_message_id: str
    end_message_id: str
    boundary_before: str
    start_time: Optional[float]
    end_time: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "message_ids": list(self.message_ids),
            "opener_message_ids": list(self.opener_message_ids),
            "context_message_ids": list(self.context_message_ids),
            "substantive_message_ids": list(self.substantive_message_ids),
            "topic_bearing_message_ids": list(self.topic_bearing_message_ids),
            "evidence_eligible_message_ids": list(self.evidence_eligible_message_ids),
            "topic_keys": list(self.topic_keys),
            "speaker_ids": list(self.speaker_ids),
            "start_message_id": self.start_message_id,
            "end_message_id": self.end_message_id,
            "boundary_before": self.boundary_before,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class DialogueSegmentationResult:
    """Result object exposing both attribute and mapping-style access."""

    segments: Tuple[DialogueSegment, ...]
    message_roles: Dict[str, str]
    message_annotations: Dict[str, Dict[str, Any]]
    messages: Tuple[Dict[str, Any], ...]
    topic_bearing_message_ids: Tuple[str, ...]
    evidence_eligible_message_ids: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segments": [segment.to_dict() for segment in self.segments],
            "message_roles": dict(self.message_roles),
            "message_annotations": {
                key: dict(value) for key, value in self.message_annotations.items()
            },
            "messages": [dict(message) for message in self.messages],
            "topic_bearing_message_ids": list(self.topic_bearing_message_ids),
            "evidence_eligible_message_ids": list(self.evidence_eligible_message_ids),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Tuple[str, ...]:
        return tuple(self.to_dict())

    def items(self) -> Iterator[Tuple[str, Any]]:
        return iter(self.to_dict().items())


def _base_annotation(message: Mapping[str, Any], index: int) -> Dict[str, Any]:
    text = _text_for(message)
    greeting_only = is_greeting_only(text)
    social_only = is_context_only_text(text, message_type=message.get("message_type", "text"))
    keys = topic_keys(text)
    topic_bearing = is_topic_bearing(text, message_type=message.get("message_type", "text"))
    # The initial role is refined once segment position is known: only a
    # social turn at the start of a segment is an opener; later social turns
    # are context-only acknowledgements.
    return {
        "message_id": _message_id(message, index),
        "account_id": _account_id(message),
        "chat_id": _chat_id(message),
        "speaker_id": _speaker_id(message),
        "role_candidate": ROLE_CONTEXT_ONLY if social_only else ROLE_SUBSTANTIVE,
        "greeting_only": greeting_only,
        "greeting_prefix": has_greeting_prefix(text),
        "topic_keys": keys,
        "topic_bearing": topic_bearing,
        "evidence_eligible": bool(topic_bearing and not social_only),
        "time": _message_time(message),
        "sequence": _sequence_value(message),
        "reply_target": _reply_target(message),
        "text": text,
    }


def _gap_seconds(previous: Mapping[str, Any], current: Mapping[str, Any]) -> Optional[float]:
    left = previous.get("time")
    right = current.get("time")
    if left is None or right is None:
        return None
    return max(0.0, float(right) - float(left))


def _should_split(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    current_entries: Sequence[Mapping[str, Any]],
    *,
    max_gap_seconds: float,
    known_segment_by_message: Mapping[str, int],
) -> Tuple[bool, str]:
    """Decide whether a new segment starts before ``current``."""

    if previous.get("chat_id") != current.get("chat_id") or previous.get("account_id") != current.get("account_id"):
        return True, "chat_or_account_changed"

    reply_target = current.get("reply_target")
    current_ids = {str(item.get("message_id")) for item in current_entries}
    explicit_reply = bool(
        reply_target
        and (
            str(reply_target) in current_ids
            or str(reply_target) in known_segment_by_message
        )
    )
    gap = _gap_seconds(previous, current)
    if gap is not None and gap > max_gap_seconds and not explicit_reply:
        return True, "max_gap_exceeded"
    if explicit_reply:
        # A reply/reference is an explicit relation even after a long pause.
        return False, "explicit_reply_or_reference"

    previous_role = previous.get("role_candidate")
    current_role = current.get("role_candidate")
    # The opener is a bridge to the next turn, not an event on its own.  Keep a
    # greeting plus the next substantive message together even when the topic
    # is new; this is the key anti-filtering behaviour.
    if previous_role == ROLE_CONTEXT_ONLY or current_role == ROLE_CONTEXT_ONLY:
        return False, "social_context_continuation"

    previous_topics = set(previous.get("topic_keys") or ())
    current_topics = set(current.get("topic_keys") or ())
    disjoint_topics = bool(previous_topics and current_topics and previous_topics.isdisjoint(current_topics))
    if disjoint_topics and _TOPIC_SHIFT.search(str(current.get("text") or "")):
        return True, "explicit_topic_shift"

    # A substantive turn with a different topic after a meaningful pause is a
    # conservative boundary.  Short back-and-forth turns stay together even
    # when coarse topic families differ (e.g. a question followed by a reply).
    meaningful_pause = gap is not None and gap > min(max_gap_seconds / 3.0, 60.0)
    if disjoint_topics and meaningful_pause:
        return True, "topic_shift_after_pause"

    # A run of messages all from one speaker with no shared topic and no
    # continuation cue can be a fresh note, but only after a moderate pause.
    speakers = {str(item.get("speaker_id") or "unknown") for item in current_entries}
    if disjoint_topics and meaningful_pause and len(speakers) == 1 and not _REFERENCE_CUE.search(str(current.get("text") or "")):
        return True, "same_speaker_topic_shift"
    return False, "same_chat_continuation"


def _finalize_segment(
    entries: Sequence[Mapping[str, Any]],
    *,
    segment_index: int,
    boundary_before: str,
) -> DialogueSegment:
    first = entries[0]
    message_ids = tuple(str(entry["message_id"]) for entry in entries)
    opener_ids: List[str] = []
    context_ids: List[str] = []
    substantive_ids: List[str] = []
    topic_ids: List[str] = []
    evidence_ids: List[str] = []
    roles_seen_substantive = False
    for entry in entries:
        role = str(entry["role"])
        message_id = str(entry["message_id"])
        if role == ROLE_CONVERSATION_OPENER:
            opener_ids.append(message_id)
            context_ids.append(message_id)
        elif role == ROLE_CONTEXT_ONLY:
            context_ids.append(message_id)
        else:
            roles_seen_substantive = True
            substantive_ids.append(message_id)
        if entry.get("topic_bearing"):
            topic_ids.append(message_id)
        if entry.get("evidence_eligible"):
            evidence_ids.append(message_id)
    keys = sorted({key for entry in entries for key in entry.get("topic_keys", ())})
    speakers = tuple(dict.fromkeys(str(entry.get("speaker_id") or "unknown") for entry in entries))
    return DialogueSegment(
        segment_id="DIALOGUE_SEGMENT_%06d" % segment_index,
        account_id=str(first.get("account_id") or "default"),
        chat_id=str(first.get("chat_id") or "unknown"),
        message_ids=message_ids,
        opener_message_ids=tuple(opener_ids),
        context_message_ids=tuple(context_ids),
        substantive_message_ids=tuple(substantive_ids),
        topic_bearing_message_ids=tuple(topic_ids),
        evidence_eligible_message_ids=tuple(evidence_ids),
        topic_keys=tuple(keys),
        speaker_ids=speakers,
        start_message_id=message_ids[0],
        end_message_id=message_ids[-1],
        boundary_before=boundary_before,
        start_time=first.get("time"),
        end_time=entries[-1].get("time"),
    )


def segment_dialogues(
    messages: Iterable[Mapping[str, Any]],
    *,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
) -> DialogueSegmentationResult:
    """Segment messages into same-chat conversation windows.

    Parameters
    ----------
    messages:
        Legacy or redacted message mappings.  ``message_id`` and ``chat_id``
        are preferred; ``content``/``timestamp`` and ``redacted_text`` /
        ``time_offset_seconds`` are both supported.
    max_gap_seconds:
        Gap that starts a new window, except for an explicit reply/reference.

    Returns
    -------
    DialogueSegmentationResult
        ``segments`` contains the windows.  ``message_roles`` maps every input
        message ID to one of the three public roles.  ``messages`` contains a
        copy of every input mapping with annotation fields; the input is never
        mutated or filtered.
    """

    try:
        max_gap = float(max_gap_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_gap_seconds must be a non-negative number") from exc
    if max_gap < 0:
        raise ValueError("max_gap_seconds must be a non-negative number")

    original = list(messages)
    if any(not isinstance(message, Mapping) for message in original):
        raise TypeError("messages must contain mapping objects")

    prepared: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, message in enumerate(original):
        annotation = _base_annotation(message, index)
        message_id = str(annotation["message_id"])
        if message_id in seen_ids:
            raise ValueError("duplicate message_id: %s" % message_id)
        seen_ids.add(message_id)
        annotation["input_index"] = index
        prepared.append(annotation)

    ordered = sorted(enumerate(prepared), key=lambda item: _sort_key((item[0], original[item[0]])))
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for _, entry in ordered:
        groups.setdefault((str(entry["account_id"]), str(entry["chat_id"])), []).append(entry)

    # Stable group order means shuffled input produces identical IDs and
    # boundaries.  Within a group ``ordered`` already follows local sequence,
    # timestamp, message ID and source position.
    all_segments: List[DialogueSegment] = []
    segment_entries_by_id: Dict[str, List[Dict[str, Any]]] = {}
    known_segment_by_message: Dict[str, int] = {}
    boundary_reasons: Dict[str, str] = {}
    segment_index = 0
    for group_key in sorted(groups):
        group_entries = groups[group_key]
        current: List[Dict[str, Any]] = []
        boundary_before = "initial_segment"
        for entry in group_entries:
            if not current:
                current = [entry]
                continue
            split, reason = _should_split(
                current[-1],
                entry,
                current,
                max_gap_seconds=max_gap,
                known_segment_by_message=known_segment_by_message,
            )
            if split:
                segment_index += 1
                # Social role assignment happens at finalization below.
                for position, item in enumerate(current):
                    if item["role_candidate"] == ROLE_CONTEXT_ONLY:
                        item["role"] = ROLE_CONVERSATION_OPENER if position == 0 else ROLE_CONTEXT_ONLY
                    else:
                        item["role"] = ROLE_SUBSTANTIVE
                segment = _finalize_segment(
                    current,
                    segment_index=segment_index,
                    boundary_before=boundary_before,
                )
                all_segments.append(segment)
                segment_entries_by_id[segment.segment_id] = list(current)
                boundary_reasons[segment.segment_id] = boundary_before
                for item in current:
                    known_segment_by_message[str(item["message_id"])] = segment_index
                current = [entry]
                boundary_before = reason
            else:
                current.append(entry)
        if current:
            segment_index += 1
            for position, item in enumerate(current):
                if item["role_candidate"] == ROLE_CONTEXT_ONLY:
                    item["role"] = ROLE_CONVERSATION_OPENER if position == 0 else ROLE_CONTEXT_ONLY
                else:
                    item["role"] = ROLE_SUBSTANTIVE
            segment = _finalize_segment(
                current,
                segment_index=segment_index,
                boundary_before=boundary_before,
            )
            all_segments.append(segment)
            segment_entries_by_id[segment.segment_id] = list(current)
            boundary_reasons[segment.segment_id] = boundary_before
            for item in current:
                known_segment_by_message[str(item["message_id"])] = segment_index

    # Only a social turn at the start of a segment is an opener.  If a
    # substantive turn precedes a social reply, all social turns are context
    # only, even if they contain a greeting word.
    roles: Dict[str, str] = {}
    annotations: Dict[str, Dict[str, Any]] = {}
    entry_lookup = {str(entry["message_id"]): entry for entry in prepared}
    segment_by_message: Dict[str, DialogueSegment] = {
        message_id: segment
        for segment in all_segments
        for message_id in segment.message_ids
    }
    for segment in all_segments:
        for position, message_id in enumerate(segment.message_ids):
            entry = entry_lookup[message_id]
            if entry["role_candidate"] == ROLE_CONTEXT_ONLY and position == 0:
                role = ROLE_CONVERSATION_OPENER
            else:
                role = (
                    ROLE_CONTEXT_ONLY
                    if entry["role_candidate"] == ROLE_CONTEXT_ONLY
                    else ROLE_SUBSTANTIVE
                )
            roles[message_id] = role
            annotations[message_id] = {
                "message_id": message_id,
                "segment_id": segment.segment_id,
                "position_in_segment": position,
                "role": role,
                "event_role": ROLE_CONTEXT_ONLY if role != ROLE_SUBSTANTIVE else ROLE_SUBSTANTIVE,
                "greeting_only": bool(entry["greeting_only"]),
                "greeting_prefix": bool(entry["greeting_prefix"]),
                "topic_keys": list(entry["topic_keys"]),
                "topic_bearing": bool(entry["topic_bearing"]),
                "evidence_eligible": bool(entry["evidence_eligible"]),
                "speaker_id": str(entry["speaker_id"]),
                "chat_id": str(entry["chat_id"]),
                "account_id": str(entry["account_id"]),
            }

    annotated_messages: List[Dict[str, Any]] = []
    for index, message in enumerate(original):
        message_id = _message_id(message, index)
        copy = dict(message)
        annotation = annotations[message_id]
        # These fields are namespaced enough to avoid colliding with either
        # legacy or gold-standard message fields, while remaining convenient
        # for the private pilot builder.
        copy.update(
            {
                "dialogue_segment_id": annotation["segment_id"],
                "dialogue_role": annotation["role"],
                "dialogue_event_role": annotation["event_role"],
                "dialogue_topic_bearing": annotation["topic_bearing"],
                "dialogue_evidence_eligible": annotation["evidence_eligible"],
                "dialogue_topic_keys": list(annotation["topic_keys"]),
                "dialogue_greeting_only": annotation["greeting_only"],
                "dialogue_greeting_prefix": annotation["greeting_prefix"],
            }
        )
        annotated_messages.append(copy)

    topic_ids = tuple(
        message_id
        for segment in all_segments
        for message_id in segment.topic_bearing_message_ids
    )
    evidence_ids = tuple(
        message_id
        for segment in all_segments
        for message_id in segment.evidence_eligible_message_ids
    )
    return DialogueSegmentationResult(
        segments=tuple(all_segments),
        message_roles=roles,
        message_annotations=annotations,
        messages=tuple(annotated_messages),
        topic_bearing_message_ids=topic_ids,
        evidence_eligible_message_ids=evidence_ids,
    )


__all__ = [
    "DEFAULT_MAX_GAP_SECONDS",
    "ROLE_CONVERSATION_OPENER",
    "ROLE_CONTEXT_ONLY",
    "ROLE_SUBSTANTIVE",
    "MESSAGE_ROLES",
    "DialogueSegment",
    "DialogueSegmentationResult",
    "is_greeting_only",
    "is_context_only_text",
    "has_context_prefix",
    "has_greeting_prefix",
    "topic_keys",
    "is_topic_bearing",
    "segment_dialogues",
]
