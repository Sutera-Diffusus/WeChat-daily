"""Reversible four-channel semantic gating for registered messages.

The gate is deliberately cheap and explainable.  It routes an immutable
``RegisteredMessage`` into ``immediate``, ``pending_context``, ``background``
or ``cold_recoverable`` while retaining every transition.  It never merges
messages, infers missing metadata, or treats a route as an event/state
decision.  A later semantic/LLM worker can reactivate pending records using
the stable registry key and input hash.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import re
import unicodedata
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .semantic_registry import (
    CONTEXT_SCHEMA_VERSION,
    PIPELINE_VERSION as REGISTRY_PIPELINE_VERSION,
    RULESET_VERSION as REGISTRY_RULESET_VERSION,
    SCHEMA_VERSION,
    UNKNOWN,
    MessageRegistry,
    RegisteredMessage,
    stable_hash,
)


CHANNEL_IMMEDIATE = "immediate"
CHANNEL_PENDING_CONTEXT = "pending_context"
CHANNEL_BACKGROUND = "background"
CHANNEL_COLD_RECOVERABLE = "cold_recoverable"
CHANNELS = frozenset(
    {
        CHANNEL_IMMEDIATE,
        CHANNEL_PENDING_CONTEXT,
        CHANNEL_BACKGROUND,
        CHANNEL_COLD_RECOVERABLE,
    }
)
# Short names mirror the contract's channel vocabulary for adapters that do
# not want to depend on the longer constant names.
IMMEDIATE = CHANNEL_IMMEDIATE
PENDING_CONTEXT = CHANNEL_PENDING_CONTEXT
BACKGROUND = CHANNEL_BACKGROUND
COLD_RECOVERABLE = CHANNEL_COLD_RECOVERABLE

GATE_PIPELINE_VERSION = "workstream_a_gate_v1"
GATE_RULESET_VERSION = "gate_rules_v1"
_SOCIAL_WORDS = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "哈罗",
        "嘿",
        "早上好",
        "上午好",
        "下午好",
        "晚上好",
        "晚安",
        "在吗",
        "谢谢",
        "感谢",
        "辛苦",
        "辛苦了",
        "收到",
        "好的",
        "好",
        "嗯",
        "哦",
        "ok",
        "okay",
        "thanks",
    }
)
_CONTEXT_CUES = re.compile(
    r"(?:这个|那个|它|这件事|这块|那块|上述|前面|刚才|继续|还是|照旧|同样|待确认|稍后|上下文)",
    flags=re.IGNORECASE,
)
_SILENT_TYPES = frozenset(
    {
        "image",
        "video",
        "audio",
        "file",
        "sticker",
        "emoji",
        "system",
        "location",
        "unknown",
    }
)


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _message_key(entry: RegisteredMessage) -> str:
    return entry.registry_key


def _entry_from(value: Any, registry: MessageRegistry) -> RegisteredMessage:
    if isinstance(value, RegisteredMessage):
        if registry.maybe_get(value.registry_key) is None:
            raise ValueError("registered message belongs to another registry")
        return value
    # Let the registry perform the idempotence and conflict checks.  Reusing
    # an existing ID without checking the new public envelope would silently
    # accept changed authoritative metadata/content.
    return registry.register(value)


def _bool(value: Any) -> bool:
    return isinstance(value, bool) and value


@dataclass(frozen=True)
class GateDecision:
    """One reversible channel decision for one registered message."""

    decision_id: str
    message_id: str
    registry_key: str
    channel: str
    previous_channel: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    semantic_signals: Tuple[str, ...] = ()
    metadata_authoritative: bool = True
    metadata_complete: bool = False
    reversible: bool = True
    active: bool = True
    transition_index: int = 0
    input_hash: str = ""
    cache_key: str = ""
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = GATE_PIPELINE_VERSION
    ruleset_version: str = GATE_RULESET_VERSION

    @property
    def id(self) -> str:
        return self.decision_id

    @property
    def from_channel(self) -> str:
        """Contract alias for the channel before this transition."""

        return self.previous_channel or "registered"

    @property
    def to_channel(self) -> str:
        """Contract alias for the channel after this transition."""

        return self.channel

    @property
    def trigger_codes(self) -> Tuple[str, ...]:
        """Explainable trigger/reason projection for gate consumers."""

        return self.reason_codes

    @property
    def budget_class(self) -> str:
        if self.channel == CHANNEL_COLD_RECOVERABLE:
            return "recovery"
        if self.channel == CHANNEL_IMMEDIATE:
            return "immediate"
        return "deferred"

    @property
    def activation_cues(self) -> Tuple[str, ...]:
        return self.semantic_signals

    @property
    def input_fingerprint(self) -> str:
        return self.input_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "message_id": self.message_id,
            "registry_key": self.registry_key,
            "channel": self.channel,
            "from_channel": self.from_channel,
            "to_channel": self.to_channel,
            "previous_channel": self.previous_channel,
            "reason_codes": list(self.reason_codes),
            "trigger_codes": list(self.trigger_codes),
            "semantic_signals": list(self.semantic_signals),
            "activation_cues": list(self.activation_cues),
            "budget_class": self.budget_class,
            "metadata_authoritative": self.metadata_authoritative,
            "metadata_complete": self.metadata_complete,
            "reversible": self.reversible,
            "active": self.active,
            "transition_index": self.transition_index,
            "input_hash": self.input_hash,
            "input_fingerprint": self.input_fingerprint,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


@dataclass(frozen=True)
class GateSnapshot:
    """Immutable routing state; transitions remain available in history."""

    decisions: Tuple[GateDecision, ...]
    history_count: int
    registry_version: int
    forced_snapshot: bool
    input_hash: str
    cache_key: str
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = GATE_PIPELINE_VERSION
    ruleset_version: str = GATE_RULESET_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decisions": [item.to_dict() for item in self.decisions],
            "history_count": self.history_count,
            "registry_version": self.registry_version,
            "forced_snapshot": self.forced_snapshot,
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


class SemanticGate:
    """Route registered messages into four reversible semantic channels."""

    def __init__(
        self,
        registry: Optional[MessageRegistry] = None,
        *,
        pipeline_version: str = GATE_PIPELINE_VERSION,
        ruleset_version: str = GATE_RULESET_VERSION,
    ) -> None:
        self.registry = registry if registry is not None else MessageRegistry()
        self.pipeline_version = str(pipeline_version)
        self.ruleset_version = str(ruleset_version)
        self._current: Dict[str, GateDecision] = {}
        self._history: Dict[str, List[GateDecision]] = {}
        self._order: List[str] = []
        self._transition_index = 0

    def __len__(self) -> int:
        return len(self._current)

    def __iter__(self) -> Iterator[GateDecision]:
        return iter(self._current.values())

    def _classify(self, entry: RegisteredMessage) -> Tuple[str, Tuple[str, ...], Tuple[str, ...]]:
        metadata = entry.metadata
        payload = entry.raw_message_ref
        requested = payload.get("semantic_channel", payload.get("gate_channel"))
        requested_text = str(requested or "")
        if requested_text in CHANNELS:
            return requested_text, ("explicit_channel_hint",), ()
        if _bool(payload.get("cold_recoverable")):
            return CHANNEL_COLD_RECOVERABLE, ("explicit_cold_recoverable",), ()

        text = entry.content.strip()
        compact = _compact(text)
        message_type = metadata.message_type.casefold()
        role = str(metadata.dialogue_role or "")
        if not text or message_type in (_SILENT_TYPES - {UNKNOWN}):
            return CHANNEL_BACKGROUND, ("silent_or_non_text",), ()
        if metadata.source_mode in {"history", "recovered"}:
            return CHANNEL_COLD_RECOVERABLE, ("historical_source",), ("recovery_cue",)
        if role == "conversation_opener" or compact in {_compact(item) for item in _SOCIAL_WORDS}:
            return CHANNEL_BACKGROUND, ("social_or_opener",), ()
        if message_type == UNKNOWN:
            return CHANNEL_PENDING_CONTEXT, ("message_type_unknown",), ("metadata_incomplete",)
        if role == "context_only":
            return CHANNEL_PENDING_CONTEXT, ("context_only_role",), ("context_role",)
        if payload.get("context_message_ids"):
            return CHANNEL_PENDING_CONTEXT, ("explicit_context_dependency",), ("context_dependency",)
        if _CONTEXT_CUES.search(text):
            return CHANNEL_PENDING_CONTEXT, ("lexical_context_cue",), ("context_cue",)
        if not metadata.metadata_complete:
            return CHANNEL_PENDING_CONTEXT, ("metadata_incomplete",), ("metadata_incomplete",)
        return CHANNEL_IMMEDIATE, ("substantive_public_message",), ("substantive",)

    def _decision(
        self,
        entry: RegisteredMessage,
        channel: str,
        reasons: Tuple[str, ...],
        signals: Tuple[str, ...],
        previous: Optional[GateDecision],
    ) -> GateDecision:
        self._transition_index += 1
        previous_channel = previous.channel if previous is not None else None
        basis = {
            "registry_key": entry.registry_key,
            "input_hash": entry.record_hash,
            "channel": channel,
            "previous_channel": previous_channel,
            "transition_index": self._transition_index,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }
        decision_id = "GATE_" + stable_hash(basis)[:20]
        cache_key = "gate:%s:%s" % (self.ruleset_version, stable_hash(basis))
        decision = GateDecision(
            decision_id=decision_id,
            message_id=entry.message_id,
            registry_key=entry.registry_key,
            channel=channel,
            previous_channel=previous_channel,
            reason_codes=tuple(dict.fromkeys(reasons)),
            semantic_signals=tuple(dict.fromkeys(signals)),
            metadata_authoritative=True,
            metadata_complete=entry.metadata.metadata_complete,
            reversible=True,
            active=True,
            transition_index=self._transition_index,
            input_hash=entry.record_hash,
            cache_key=cache_key,
            schema_version=SCHEMA_VERSION,
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            pipeline_version=self.pipeline_version,
            ruleset_version=self.ruleset_version,
        )
        self._current[entry.registry_key] = decision
        self._history.setdefault(entry.registry_key, []).append(decision)
        if entry.registry_key not in self._order:
            self._order.append(entry.registry_key)
        return decision

    def route(
        self,
        message: Any,
        *,
        channel: Optional[str] = None,
        reason: Optional[str] = None,
        semantic_signals: Iterable[str] = (),
    ) -> GateDecision:
        """Route a message, retaining a reversible transition record."""

        entry = _entry_from(message, self.registry)
        if entry.metadata.split in {"frozen", "frozen_test"}:
            raise ValueError("semantic gate accepts development/public inputs only")
        inferred, reasons, inferred_signals = self._classify(entry)
        selected = str(channel or inferred)
        if selected not in CHANNELS:
            raise ValueError("channel must be one of %s" % ", ".join(sorted(CHANNELS)))
        if channel is not None:
            reasons = tuple(dict.fromkeys(("explicit_route",) + reasons))
        if reason:
            reasons = tuple(dict.fromkeys(reasons + (str(reason),)))
        caller_signals = tuple(str(item) for item in semantic_signals)
        signals = tuple(dict.fromkeys(tuple(inferred_signals) + caller_signals))
        previous = self._current.get(entry.registry_key)
        if (
            previous is not None
            and previous.input_hash == entry.record_hash
            and previous.channel == selected
            and channel is None
            and reason is None
            and not caller_signals
        ):
            return previous
        return self._decision(entry, selected, reasons, signals, previous)

    decide = route

    def reactivate(
        self,
        message_id: str,
        *,
        channel: str = CHANNEL_IMMEDIATE,
        reason: str = "reactivated_with_context",
        semantic_signals: Iterable[str] = (),
    ) -> GateDecision:
        """Reactivate a pending/cold/background record without deleting history."""

        if channel not in CHANNELS:
            raise ValueError("channel must be one of %s" % ", ".join(sorted(CHANNELS)))
        entry = self.registry.get(str(message_id))
        previous = self._current.get(entry.registry_key)
        if previous is None:
            raise KeyError(str(message_id))
        return self._decision(
            entry,
            channel,
            ("reactivation", str(reason)),
            tuple(str(item) for item in semantic_signals),
            previous,
        )

    def reactivate_pending(
        self,
        *,
        related_message_id: Optional[str] = None,
        channel: str = CHANNEL_IMMEDIATE,
        reason: str = "new_context",
    ) -> Tuple[GateDecision, ...]:
        """Reactivate pending messages, optionally scoped to a related message."""

        related = self.registry.maybe_get(related_message_id) if related_message_id else None
        decisions: List[GateDecision] = []
        for key in tuple(self._order):
            current = self._current[key]
            if current.channel != CHANNEL_PENDING_CONTEXT:
                continue
            if related is not None:
                entry = self.registry.get(key)
                if entry.scope_key is None or related.scope_key is None or entry.scope_key != related.scope_key:
                    continue
            decisions.append(self.reactivate(current.message_id, channel=channel, reason=reason))
        return tuple(decisions)

    def history(self, message_id: str) -> Tuple[GateDecision, ...]:
        entry = self.registry.get(str(message_id))
        return tuple(self._history.get(entry.registry_key, ()))

    def current(self, message_id: str) -> GateDecision:
        entry = self.registry.get(str(message_id))
        try:
            return self._current[entry.registry_key]
        except KeyError as exc:
            raise KeyError(str(message_id)) from exc

    def channel_ids(self, channel: str) -> Tuple[str, ...]:
        if channel not in CHANNELS:
            raise ValueError("unknown channel: %s" % channel)
        return tuple(
            self._current[key].message_id
            for key in self._order
            if self._current[key].channel == channel
        )

    def active_ids(self) -> Tuple[str, ...]:
        return tuple(self._current[key].message_id for key in self._order)

    def snapshot(self, *, force: bool = False) -> GateSnapshot:
        decisions = tuple(self._current[key] for key in self._order)
        input_hash = stable_hash(
            {
                "registry_version": self.registry.version,
                "decisions": [item.decision_id for item in decisions],
            }
        )
        return GateSnapshot(
            decisions=decisions,
            history_count=sum(len(value) for value in self._history.values()),
            registry_version=self.registry.version,
            forced_snapshot=bool(force),
            input_hash=input_hash,
            cache_key="gate-snapshot:%d:%s" % (self.registry.version, input_hash),
            schema_version=SCHEMA_VERSION,
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            pipeline_version=self.pipeline_version,
            ruleset_version=self.ruleset_version,
        )


__all__ = [
    "CHANNEL_IMMEDIATE",
    "CHANNEL_PENDING_CONTEXT",
    "CHANNEL_BACKGROUND",
    "CHANNEL_COLD_RECOVERABLE",
    "CHANNELS",
    "IMMEDIATE",
    "PENDING_CONTEXT",
    "BACKGROUND",
    "COLD_RECOVERABLE",
    "GATE_PIPELINE_VERSION",
    "GATE_RULESET_VERSION",
    "GateDecision",
    "GateSnapshot",
    "SemanticGate",
]
