"""Rule matching and conservative automatic-reply decisions."""

import re
from dataclasses import dataclass, field
from datetime import time as datetime_time
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .ai import ReplyGenerator
from .models import IncomingMessage, ReplyDecision
from .timeutil import DEFAULT_TIMEZONE, as_timezone, get_timezone


def _strict_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("布尔配置必须使用 true 或 false")
    return value


def _parse_clock(value: str) -> datetime_time:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return datetime_time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise ValueError("时间段必须使用 HH:MM 格式: %r" % value) from exc


def _parse_time_ranges(values: Iterable[object]) -> Tuple[Tuple[datetime_time, datetime_time], ...]:
    ranges = []
    for value in values:
        if isinstance(value, Mapping):
            start = value.get("start")
            end = value.get("end")
        else:
            parts = str(value).split("-", 1)
            if len(parts) != 2:
                raise ValueError("时间段必须使用 HH:MM-HH:MM 格式: %r" % value)
            start, end = parts
        ranges.append((_parse_clock(str(start)), _parse_clock(str(end))))
    return tuple(ranges)


@dataclass(frozen=True)
class ReplyRule:
    name: str
    reply_text: Optional[str] = None
    keywords: Tuple[str, ...] = ()
    regexes: Tuple[str, ...] = ()
    chats: Tuple[str, ...] = ()
    senders: Tuple[str, ...] = ()
    time_ranges: Tuple[Tuple[datetime_time, datetime_time], ...] = ()
    message_types: Tuple[str, ...] = ("text",)
    enabled: bool = True
    match_all_keywords: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, object], index: int) -> "ReplyRule":
        name = str(data.get("name") or "rule-%s" % (index + 1))
        regexes = tuple(
            str(value) for value in (data.get("regex") or data.get("regexes") or ())
        )
        for pattern in regexes:
            re.compile(pattern)
        reply_value = data.get("reply", data.get("reply_text"))
        return cls(
            name=name,
            reply_text=str(reply_value) if reply_value is not None else None,
            keywords=tuple(str(value) for value in (data.get("keywords") or ())),
            regexes=regexes,
            chats=tuple(
                str(value)
                for value in (data.get("chats") or data.get("contacts") or ())
            ),
            senders=tuple(str(value) for value in (data.get("senders") or ())),
            time_ranges=_parse_time_ranges(
                data.get("time_ranges") or data.get("times") or ()
            ),
            message_types=tuple(
                str(value) for value in (data.get("message_types") or ("text",))
            ),
            enabled=_strict_bool(data.get("enabled"), True),
            match_all_keywords=_strict_bool(data.get("match_all_keywords"), False),
        )

    def matches(self, message: IncomingMessage, timezone_name: str) -> bool:
        if not self.enabled:
            return False
        if self.message_types and message.message_type not in self.message_types:
            return False
        if self.chats and message.chat_id not in self.chats and message.chat_name not in self.chats:
            return False
        if self.senders:
            sender_values = {
                str(message.sender_id or ""),
                str(message.sender_name or ""),
            }
            if not sender_values.intersection(self.senders):
                return False
        content = message.content or ""
        if self.keywords:
            checks = [keyword.casefold() in content.casefold() for keyword in self.keywords]
            if not (all(checks) if self.match_all_keywords else any(checks)):
                return False
        if self.regexes and not any(
            re.search(pattern, content, re.IGNORECASE) for pattern in self.regexes
        ):
            return False
        if self.time_ranges:
            local_value = as_timezone(message.timestamp, timezone_name).time()
            if not any(
                self._in_range(local_value, start, end)
                for start, end in self.time_ranges
            ):
                return False
        return bool(
            self.keywords
            or self.regexes
            or self.chats
            or self.senders
            or self.time_ranges
            or self.message_types
        )

    @staticmethod
    def _in_range(value: datetime_time, start: datetime_time, end: datetime_time) -> bool:
        if start <= end:
            return start <= value <= end
        return value >= start or value <= end


@dataclass
class ReplyPolicy:
    """Allow-list, safety gates, optional rules, and optional AI generation."""

    reply_text: str = "已收到，这是 M1 固定回复。"
    enabled: bool = True
    allowed_chats: Tuple[str, ...] = ("文件传输助手",)
    blocked_chats: Tuple[str, ...] = ()
    allowed_message_types: Tuple[str, ...] = ("text",)
    max_retries: int = 2
    cooldown_seconds: float = 0.0
    rules: Tuple[ReplyRule, ...] = ()
    timezone_name: str = DEFAULT_TIMEZONE
    reply_generator: Optional[ReplyGenerator] = None
    context_limit: int = 12
    _last_reply_at: Dict[str, float] = field(default_factory=dict, init=False)

    @classmethod
    def from_values(
        cls,
        reply_text: str,
        allowed_chats: Iterable[str],
        enabled: bool = True,
        blocked_chats: Iterable[str] = (),
        max_retries: int = 2,
        cooldown_seconds: float = 0.0,
        rules: Iterable[ReplyRule] = (),
        timezone_name: str = DEFAULT_TIMEZONE,
        reply_generator: Optional[ReplyGenerator] = None,
        context_limit: int = 12,
    ) -> "ReplyPolicy":
        get_timezone(timezone_name)
        return cls(
            reply_text=reply_text,
            enabled=enabled,
            allowed_chats=tuple(x for x in allowed_chats if x),
            blocked_chats=tuple(x for x in blocked_chats if x),
            max_retries=max(0, max_retries),
            cooldown_seconds=max(0.0, cooldown_seconds),
            rules=tuple(rules),
            timezone_name=timezone_name,
            reply_generator=reply_generator,
            context_limit=max(0, int(context_limit)),
        )

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object],
        reply_generator: Optional[ReplyGenerator] = None,
        allowed_chats_override: Optional[Sequence[str]] = None,
    ) -> "ReplyPolicy":
        rules = tuple(
            ReplyRule.from_dict(item, index)
            for index, item in enumerate(config.get("rules") or ())
        )
        allowed = allowed_chats_override or tuple(
            str(value)
            for value in (config.get("allowed_chats") or ("文件传输助手",))
        )
        if "reply_text" in config:
            configured_reply = config.get("reply_text")
        elif "reply" in config:
            configured_reply = config.get("reply")
        elif reply_generator is not None:
            configured_reply = ""
        else:
            configured_reply = "已收到，这是 M1 固定回复。"
        return cls.from_values(
            reply_text=str(configured_reply or ""),
            allowed_chats=allowed,
            enabled=_strict_bool(config.get("enabled"), True),
            blocked_chats=tuple(
                str(value) for value in (config.get("blocked_chats") or ())
            ),
            max_retries=int(config.get("max_retries", 2)),
            cooldown_seconds=float(config.get("cooldown_seconds", 0.0)),
            rules=rules,
            timezone_name=str(config.get("timezone") or DEFAULT_TIMEZONE),
            reply_generator=reply_generator,
            context_limit=int(config.get("context_limit", 12)),
        )

    def decide(
        self,
        message: IncomingMessage,
        now_monotonic: Optional[float] = None,
    ) -> ReplyDecision:
        if not self.enabled:
            return ReplyDecision(False, "auto_reply_disabled")
        if message.is_self is True:
            return ReplyDecision(False, "self_message")
        if message.is_self is None:
            return ReplyDecision(False, "self_state_unknown")
        if message.chat_id in self.blocked_chats or message.chat_name in self.blocked_chats:
            return ReplyDecision(False, "chat_blocked")
        if (
            self.allowed_chats
            and message.chat_id not in self.allowed_chats
            and message.chat_name not in self.allowed_chats
        ):
            return ReplyDecision(False, "chat_not_allowlisted")
        if message.message_type not in self.allowed_message_types:
            return ReplyDecision(False, "message_type_not_supported")
        if not message.content.strip():
            return ReplyDecision(False, "empty_content")

        rule = None
        if self.rules:
            rule = next(
                (
                    candidate
                    for candidate in self.rules
                    if candidate.matches(message, self.timezone_name)
                ),
                None,
            )
            if rule is None:
                return ReplyDecision(False, "no_rule_matched")
        reply_text = rule.reply_text if rule else self.reply_text
        if not reply_text and self.reply_generator is None:
            return ReplyDecision(False, "reply_text_empty")

        if now_monotonic is not None and self.cooldown_seconds > 0:
            previous = self._last_reply_at.get(message.chat_id)
            if previous is not None and now_monotonic - previous < self.cooldown_seconds:
                return ReplyDecision(False, "chat_cooldown")
            self._last_reply_at[message.chat_id] = now_monotonic

        if reply_text:
            return ReplyDecision(
                True,
                "rule:%s" % rule.name if rule else "matched",
                reply_text,
            )
        return ReplyDecision(
            True,
            "rule:%s:ai" % rule.name if rule else "matched_ai",
            None,
        )

    def generate_reply(
        self,
        message: IncomingMessage,
        context: Iterable[Mapping[str, object]] = (),
    ) -> str:
        if self.reply_generator is None:
            return self.reply_text
        return self.reply_generator.generate(message, context).strip()
