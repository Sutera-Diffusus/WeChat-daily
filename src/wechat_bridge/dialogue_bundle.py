"""Dynamic, multi-scale dialogue bundles for the semantic shadow path.

The builder consumes immutable registrations and optional public fragment/claim
candidates.  It remains below the event layer: bundles are reversible context
windows, not event clusters, titles or UI cards.  A message may yield several
fragments and several local bundle candidates, while each claim keeps exactly
one typed evidence span.

The implementation is intentionally low-cost.  It uses explicit structured
fields when supplied, small lexical checks only for social/silent defaults,
finite local windows, and hard scope/semantic gates.  Time and segment
membership enrich an existing candidate but never create one on their own.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace, is_dataclass, asdict
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .semantic_gate import (
    CHANNEL_BACKGROUND,
    CHANNEL_COLD_RECOVERABLE,
    CHANNEL_IMMEDIATE,
    CHANNEL_PENDING_CONTEXT,
    GateDecision,
    SemanticGate,
)
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
from .dialogue_segments import has_context_prefix, is_context_only_text


BUNDLE_PIPELINE_VERSION = "workstream_a_bundle_v1"
BUNDLE_RULESET_VERSION = "bundle_rules_v1"
SCALE_MICRO = "micro"
SCALE_TURN = "turn"
SCALE_LOCAL = "local"
SCALE_SESSION = "session"
# W3 is a sparse, same-scope recall window.  It is kept separate from the
# cold-recovery W4 lane so a long gap never masquerades as a hard semantic
# boundary or a cross-chat bridge.
SCALE_SPARSE = "sparse"
SCALE_COLD = "cold"
BUNDLE_SCALES = frozenset({SCALE_MICRO, SCALE_TURN, SCALE_LOCAL, SCALE_SESSION, SCALE_SPARSE, SCALE_COLD})
# Contract-facing window names.  The implementation keeps readable scale
# names for compatibility and exposes these as a projection in ``to_dict``.
WINDOW_W0 = "W0"
WINDOW_W1 = "W1"
WINDOW_W2 = "W2"
WINDOW_W3 = "W3"
WINDOW_W4 = "W4"
WINDOW_SCALE_BY_BUNDLE_SCALE = {
    SCALE_MICRO: WINDOW_W0,
    SCALE_TURN: WINDOW_W0,
    SCALE_LOCAL: WINDOW_W1,
    SCALE_SESSION: WINDOW_W2,
    SCALE_SPARSE: WINDOW_W3,
    SCALE_COLD: WINDOW_W4,
}

REL_CONTINUES = "continues"
REL_ELABORATES = "elaborates"
REL_ANSWERS = "answers"
REL_CONTRASTS = "contrasts"
REL_TOPIC_SHIFT = "topic_shift"
REL_POSSIBLY_RELATED = "possibly_related"
REL_INSUFFICIENT = "insufficient"
RELATION_LABELS = frozenset(
    {
        REL_CONTINUES,
        REL_ELABORATES,
        REL_ANSWERS,
        REL_CONTRASTS,
        REL_TOPIC_SHIFT,
        REL_POSSIBLY_RELATED,
        REL_INSUFFICIENT,
    }
)
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})
KNOWN_STATES = frozenset({"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"})
KNOWN_RESOLUTIONS = frozenset({"explicit", "inherited", "unknown"})
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
_MEDIA_MESSAGE_TYPES = frozenset(
    {"image", "video", "audio", "voice", "file", "sticker", "emoji", "system", "location", "media"}
)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _tuple_text(values: Any) -> Tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes)):
        return (str(values),) if values not in (None, "") else ()
    try:
        return tuple(str(item) for item in values if item not in (None, ""))
    except TypeError:
        return (str(values),)


def _known(value: Any) -> bool:
    return value not in (None, "", UNKNOWN, "unknown", "UNKNOWN")


def _scope(account_id: Any, chat_id: Any) -> Optional[Tuple[str, str]]:
    account, chat = str(account_id or UNKNOWN), str(chat_id or UNKNOWN)
    if not _known(account) or not _known(chat):
        return None
    return account, chat


def _time_value(item: Any) -> Optional[float]:
    value = _value(item, "time_offset_seconds", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _span(value: Any, default: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    if isinstance(value, Mapping):
        if "start" in value or "end" in value:
            start, end = value.get("start"), value.get("end")
        else:
            start, end = value.get("span_start"), value.get("span_end")
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        start, end = value[0], value[1]
    else:
        return default
    try:
        return int(start), int(end)
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text_is_social(text: Any, *, message_type: Any = "text") -> bool:
    return is_context_only_text(text, message_type=message_type)


def _normalise_fragment_role(
    raw_role: Any,
    *,
    role_explicit: bool,
    text: str,
    message_type: Any,
) -> str:
    """Resolve the authoritative role without filtering mixed turns.

    A pure social/confirmation turn is context-only by default.  An explicit
    opener role remains available for segmentation compatibility, while any
    non-pure text (including a greeting/confirmation followed by an object,
    action, state, question or claim) is substantive.  This is intentionally
    lexical and deterministic; no provider or reply edge is consulted.
    """

    role = str(raw_role or "").strip().casefold()
    if role in {"opener", "conversation opener", "conversation_opener"}:
        role = "conversation_opener"
    elif role in {"context", "context-only", "context_only"}:
        role = "context_only"
    elif role != "substantive":
        role = "substantive"
    pure_context = is_context_only_text(text, message_type=message_type)
    if pure_context:
        # Preserve an explicitly authored opener for the legacy segmentation
        # surface; ContextPacket/linear paging still treats it as context and
        # never emits it as a standalone primary row.
        return "conversation_opener" if role_explicit and role == "conversation_opener" else "context_only"
    # A supplied context label can be retained for a non-prefixed context
    # note, but it cannot hide a recognised social/confirmation prefix plus
    # substantive text.  Do not use broad topic heuristics here: they would
    # turn arbitrary body-free/opaque labels into a false positive.
    if role_explicit and role in {"conversation_opener", "context_only"} and not has_context_prefix(text):
        return role
    # A role label cannot hide substantive text.  This is the key mixed-turn
    # safeguard: ``你好，接口失败了`` and ``确认项目状态`` stay primary.
    return "substantive"


def _stable_id(prefix: str, value: Any) -> str:
    return "%s_%s" % (prefix, stable_hash(value)[:20])


@dataclass(frozen=True)
class BundleFragment:
    """Contract-shaped fragment candidate used by the dynamic bundle layer."""

    fragment_id: str = "FRAGMENT_UNKNOWN"
    message_id: str = UNKNOWN
    account_id: str = UNKNOWN
    chat_id: str = UNKNOWN
    segment_id: Optional[str] = None
    text: str = ""
    span_start: int = 0
    span_end: int = 0
    role: str = "substantive"
    fragment_type: str = "unknown"
    speaker_id: str = UNKNOWN
    mentioned_person_ids: Tuple[str, ...] = ()
    subject_id: str = UNKNOWN
    subject_type: str = "unknown"
    object_id: str = UNKNOWN
    object_resolution: str = "unknown"
    object_inherited_from_id: Optional[str] = None
    object_evidence_refs: Tuple[Dict[str, Any], ...] = ()
    state: str = "unknown"
    state_evidence: str = "unknown"
    closure_reason: str = "unknown"
    temporal_qualifier: str = "unknown"
    intent: str = "statement"
    claim_role: str = "fact"
    modality: str = "unknown"
    actions: Tuple[str, ...] = ()
    topic_shift: bool = False
    # ``dialogue_segments`` may already have classified a fragment as
    # topic-bearing.  Preserve that typed marker when a contextual Fragment
    # is normalised into the bundle layer; ``None`` keeps the conservative
    # structural fallback for legacy callers.
    topic_bearing: Optional[bool] = None
    contrast_marker: bool = False
    state_change: bool = False
    is_opener: bool = False
    is_silent: bool = False
    timestamp: Optional[str] = None
    time_offset_seconds: Optional[float] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    start_time_source: str = "unknown"
    end_time_source: str = "unknown"
    reply_to_message_id: Optional[str] = None
    information_value: str = "unknown"
    event_completeness: str = "unknown"
    context_message_ids: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    uncertainties: Tuple[str, ...] = ()
    channel: str = CHANNEL_PENDING_CONTEXT
    source: str = "workstream_a"

    @property
    def id(self) -> str:
        return self.fragment_id

    @property
    def object_known(self) -> bool:
        return _known(self.object_id) and self.object_resolution in {"explicit", "inherited"}

    @property
    def scope_key(self) -> Optional[Tuple[str, str]]:
        return _scope(self.account_id, self.chat_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "message_id": self.message_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "segment_id": self.segment_id,
            "fragment_text_redacted": self.text,
            "text": self.text,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "evidence_text": self.text if not self.is_silent else "",
            "role": self.role,
            "fragment_type": self.fragment_type,
            "speaker_id": self.speaker_id,
            "mentioned_person_ids": list(self.mentioned_person_ids),
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "object_id": self.object_id,
            "object_resolution": self.object_resolution,
            "object_inherited_from_id": self.object_inherited_from_id,
            "object_evidence_refs": [dict(item) for item in self.object_evidence_refs],
            "state": self.state,
            "state_evidence": self.state_evidence,
            "closure_reason": self.closure_reason,
            "temporal_qualifier": self.temporal_qualifier,
            "intent": self.intent,
            "claim_role": self.claim_role,
            "modality": self.modality,
            "actions": list(self.actions),
            "topic_shift": self.topic_shift,
            "topic_bearing": self.topic_bearing,
            "contrast_marker": self.contrast_marker,
            "state_change": self.state_change,
            "is_opener": self.is_opener,
            "is_silent": self.is_silent,
            "timestamp": self.timestamp,
            "time_offset_seconds": self.time_offset_seconds,
            "start_time_offset_seconds": self.start_time,
            "end_time_offset_seconds": self.end_time,
            "start_time_source": self.start_time_source,
            "end_time_source": self.end_time_source,
            "reply_to_message_id": self.reply_to_message_id,
            "information_value": self.information_value,
            "event_completeness": self.event_completeness,
            "context_message_ids": list(self.context_message_ids),
            "claim_ids": list(self.claim_ids),
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "uncertainties": list(self.uncertainties),
            "channel": self.channel,
            "source": self.source,
            "schema_version": SCHEMA_VERSION,
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "pipeline_version": BUNDLE_PIPELINE_VERSION,
            "ruleset_version": BUNDLE_RULESET_VERSION,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, fallback_message_id: str = UNKNOWN) -> "BundleFragment":
        message_id = str(value.get("message_id") or fallback_message_id or UNKNOWN)
        text = str(value.get("text", value.get("fragment_text_redacted", value.get("evidence_text", ""))) or "")
        message_type = str(value.get("message_type") or "text")
        start, end = _span(value, (0, len(text)))
        if end < start:
            start, end = 0, len(text)
        fragment_id = str(value.get("fragment_id") or _stable_id("FRAGMENT", {"message_id": message_id, "start": start, "end": end, "text": text}))
        state = str(value.get("state") or UNKNOWN)
        if state not in KNOWN_STATES:
            state = UNKNOWN
        resolution = str(value.get("object_resolution") or UNKNOWN)
        if resolution not in KNOWN_RESOLUTIONS:
            resolution = UNKNOWN
        object_id = str(value.get("object_id") or UNKNOWN)
        if not _known(object_id):
            object_id = UNKNOWN
        raw_role = value.get("role", value.get("message_role"))
        role = _normalise_fragment_role(
            raw_role,
            role_explicit=raw_role not in (None, ""),
            text=text,
            message_type=message_type,
        )
        fragment_type = str(value.get("fragment_type", value.get("kind", "unknown")) or "unknown")
        # A supplied opener flag cannot override substantive mixed text.
        is_opener = role == "conversation_opener"
        explicit_silent = value.get("is_silent")
        is_silent = (
            bool(explicit_silent)
            if explicit_silent is not None
            else str(message_type).casefold() in _MEDIA_MESSAGE_TYPES
        )
        inherited_from = value.get("object_inherited_from_id")
        inherited_from_id = str(inherited_from) if inherited_from not in (None, "") else None
        # An inherited object is not resolved until its source anchor is
        # explicit.  This prevents a bare pronoun/label from becoming a
        # semantic link merely because an ID was supplied.
        if resolution == "inherited" and inherited_from_id is None:
            resolution = UNKNOWN
            object_id = UNKNOWN
        # A media/silent or opener fragment may carry no terminal evidence;
        # never let an adapter's incidental state value turn it into a closed
        # context record.
        if is_silent or is_opener:
            if state in TERMINAL_STATES:
                state = UNKNOWN
            if value.get("state_evidence") in (None, "", "explicit"):
                state_evidence = UNKNOWN
            else:
                state_evidence = str(value.get("state_evidence"))
            closure = UNKNOWN
        else:
            state_evidence = str(value.get("state_evidence") or UNKNOWN)
            closure = str(value.get("closure_reason") or (state if state in TERMINAL_STATES else UNKNOWN))
        object_refs = tuple(dict(item) for item in (value.get("object_evidence_refs") or ()) if isinstance(item, Mapping))
        if not object_refs and resolution == "explicit" and _known(object_id):
            object_refs = ({"type": "fragment", "id": fragment_id, "span": {"start": start, "end": end}},)
        if is_silent:
            if fragment_type == "unknown":
                fragment_type = "media" if str(message_type).casefold() in _MEDIA_MESSAGE_TYPES else "unknown"
        elif role == "conversation_opener":
            fragment_type = "conversation_opener"
        elif role == "context_only":
            fragment_type = "acknowledgement"
        elif fragment_type in {"", "unknown", "acknowledgement", "conversation_opener"}:
            fragment_type = "question" if "?" in text or "？" in text else "statement"
        raw_topic_boundary = str(value.get("topic_boundary") or "").strip().casefold().replace("-", "_")
        topic_shift = bool(value.get("topic_shift", False)) or raw_topic_boundary in {
            "shift",
            "topic_shift",
            "topic_change",
            "topic_transition",
            "boundary",
            "explicit_boundary",
        }
        return cls(
            fragment_id=fragment_id,
            message_id=message_id,
            account_id=str(value.get("account_id") or UNKNOWN),
            chat_id=str(value.get("chat_id") or UNKNOWN),
            segment_id=(str(value.get("segment_id") or value.get("dialogue_segment_id")) if value.get("segment_id", value.get("dialogue_segment_id")) not in (None, "") else None),
            text=text,
            span_start=start,
            span_end=end,
            role=role,
            fragment_type=fragment_type,
            speaker_id=str(value.get("speaker_id") or UNKNOWN),
            mentioned_person_ids=_tuple_text(value.get("mentioned_person_ids")),
            subject_id=str(value.get("subject_id") or UNKNOWN),
            subject_type=str(value.get("subject_type") or "unknown"),
            object_id=object_id,
            object_resolution=resolution,
            object_inherited_from_id=inherited_from_id if resolution == "inherited" else None,
            object_evidence_refs=object_refs,
            state=state,
            state_evidence=state_evidence,
            closure_reason=closure,
            temporal_qualifier=str(value.get("temporal_qualifier") or UNKNOWN),
            intent=str(value.get("intent") or "statement"),
            claim_role=str(value.get("claim_role", value.get("claim_type", "fact")) or "fact"),
            modality=str(value.get("modality") or UNKNOWN),
            actions=_tuple_text(value.get("actions")),
            topic_shift=topic_shift,
            topic_bearing=(bool(value.get("topic_bearing")) if value.get("topic_bearing") is not None else None),
            contrast_marker=bool(value.get("contrast_marker", False)),
            state_change=bool(value.get("state_change", False)),
            is_opener=is_opener,
            is_silent=is_silent,
            timestamp=(str(value.get("timestamp")) if value.get("timestamp") not in (None, "") else None),
            time_offset_seconds=_time_value(value),
            start_time=_number(value.get("start_time_offset_seconds")),
            end_time=_number(value.get("end_time_offset_seconds")),
            start_time_source=str(value.get("start_time_source") or UNKNOWN),
            end_time_source=str(value.get("end_time_source") or UNKNOWN),
            reply_to_message_id=(str(value.get("reply_to_message_id")) if value.get("reply_to_message_id") not in (None, "") else None),
            information_value=str(value.get("information_value") or "unknown"),
            event_completeness=str(value.get("event_completeness") or "unknown"),
            context_message_ids=_tuple_text(value.get("context_message_ids")),
            claim_ids=_tuple_text(value.get("claim_ids")),
            evidence_refs=tuple(dict(item) for item in (value.get("evidence_refs") or ()) if isinstance(item, Mapping)),
            uncertainties=_tuple_text(value.get("uncertainties")),
            channel=str(value.get("channel") or CHANNEL_PENDING_CONTEXT),
            source=str(value.get("source") or "workstream_a"),
        )

    @classmethod
    def from_registered(cls, entry: RegisteredMessage, *, channel: str = CHANNEL_PENDING_CONTEXT) -> "BundleFragment":
        text = entry.content
        message_type = entry.metadata.message_type.casefold()
        raw_role = entry.metadata.dialogue_role
        role = _normalise_fragment_role(
            raw_role,
            role_explicit=raw_role not in (None, ""),
            text=text,
            message_type=message_type,
        )
        is_opener = role == "conversation_opener"
        # Missing/unknown text is not silently discarded.  Only a known media
        # message is inherently silent; a text/unknown row remains a
        # conservative candidate even when its body was not materialized.
        silent = message_type in _MEDIA_MESSAGE_TYPES
        if silent:
            fragment_type = "media" if message_type not in {"text", "link", UNKNOWN} else "unknown"
        elif is_opener:
            fragment_type = "conversation_opener"
        elif role == "context_only":
            fragment_type = "acknowledgement"
        elif "?" in text or "？" in text:
            fragment_type = "question"
        else:
            fragment_type = "statement"
        fragment_id = _stable_id(
            "FRAGMENT",
            {
                "message_id": entry.message_id,
                "span": (0, len(text)),
                "content_hash": entry.content_hash,
                "ruleset": BUNDLE_RULESET_VERSION,
            },
        )
        return cls(
            fragment_id=fragment_id,
            message_id=entry.message_id,
            account_id=entry.account_id,
            chat_id=entry.chat_id,
            segment_id=entry.metadata.dialogue_segment_id,
            text=text,
            span_start=0,
            span_end=len(text),
            role=role,
            fragment_type=fragment_type,
            speaker_id=entry.metadata.speaker_id,
            subject_id=UNKNOWN,
            subject_type="unknown",
            object_id=UNKNOWN,
            object_resolution=UNKNOWN,
            state=UNKNOWN,
            state_evidence=UNKNOWN,
            closure_reason=UNKNOWN,
            intent="question" if fragment_type == "question" else "greeting" if is_opener else "silence" if silent else "statement",
            claim_role="question" if fragment_type == "question" else "fact",
            is_opener=is_opener,
            is_silent=silent,
            timestamp=entry.metadata.timestamp,
            time_offset_seconds=entry.metadata.time_offset_seconds,
            reply_to_message_id=entry.metadata.reply_to_message_id,
            information_value="low" if is_opener else "none" if silent else "unknown",
            event_completeness="not_applicable" if is_opener or silent or role == "context_only" else "unknown",
            channel=channel,
            source="registry_default",
        )


@dataclass(frozen=True)
class BundleClaim:
    """One claim with exactly one evidence span."""

    claim_id: str
    fragment_id: str
    message_id: str
    evidence_span: Tuple[int, int]
    claim_type: str
    speaker_id: str = UNKNOWN
    mentioned_person_ids: Tuple[str, ...] = ()
    subject_id: str = UNKNOWN
    subject_type: str = "unknown"
    object_id: str = UNKNOWN
    object_resolution: str = UNKNOWN
    object_evidence_refs: Tuple[Dict[str, Any], ...] = ()
    object_inherited_from_id: Optional[str] = None
    state: str = UNKNOWN
    state_evidence: str = UNKNOWN
    modality: str = UNKNOWN
    polarity: str = "unknown"
    stance: str = "unknown"
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    source: str = "workstream_a"
    claim_text_redacted: str = ""
    target_entity_ids: Tuple[str, ...] = ()
    event_mention_ids: Tuple[str, ...] = ()
    evidence_spans: Tuple[Dict[str, int], ...] = ()
    closure_reason: str = UNKNOWN
    temporal_qualifier: str = UNKNOWN
    start_time_offset_seconds: Optional[float] = None
    end_time_offset_seconds: Optional[float] = None
    start_time_source: str = UNKNOWN
    end_time_source: str = UNKNOWN
    information_value: str = "unknown"
    event_completeness: str = "unknown"
    context_message_ids: Tuple[str, ...] = ()
    confidence: str = "low"

    @property
    def id(self) -> str:
        return self.claim_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "fragment_id": self.fragment_id,
            "message_id": self.message_id,
            "speaker_id": self.speaker_id,
            "mentioned_person_ids": list(self.mentioned_person_ids),
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "object_id": self.object_id,
            "object_resolution": self.object_resolution,
            "object_evidence_refs": [dict(item) for item in self.object_evidence_refs],
            "object_inherited_from_id": self.object_inherited_from_id,
            "claim_type": self.claim_type,
            "claim_text_redacted": self.claim_text_redacted,
            "target_entity_ids": list(self.target_entity_ids),
            "event_mention_ids": list(self.event_mention_ids),
            "state": self.state,
            "state_evidence": self.state_evidence,
            "modality": self.modality,
            "polarity": self.polarity,
            "stance": self.stance,
            "status_or_modality": self.modality,
            "evidence_span": {"start": self.evidence_span[0], "end": self.evidence_span[1]},
            "evidence_spans": [dict(item) for item in (self.evidence_spans or ({"start": self.evidence_span[0], "end": self.evidence_span[1]},))],
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "closure_reason": self.closure_reason,
            "temporal_qualifier": self.temporal_qualifier,
            "start_time_offset_seconds": self.start_time_offset_seconds,
            "end_time_offset_seconds": self.end_time_offset_seconds,
            "start_time_source": self.start_time_source,
            "end_time_source": self.end_time_source,
            "information_value": self.information_value,
            "event_completeness": self.event_completeness,
            "context_message_ids": list(self.context_message_ids),
            "confidence": self.confidence,
            "source": self.source,
            "schema_version": SCHEMA_VERSION,
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "pipeline_version": BUNDLE_PIPELINE_VERSION,
            "ruleset_version": BUNDLE_RULESET_VERSION,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, fragment: Optional[BundleFragment] = None) -> "BundleClaim":
        fragment_id = str(value.get("fragment_id") or (fragment.fragment_id if fragment else UNKNOWN))
        message_id = str(value.get("message_id") or (fragment.message_id if fragment else UNKNOWN))
        evidence_value = value.get("evidence_span", value.get("evidence_span_offset"))
        if evidence_value is None and value.get("evidence_spans"):
            evidence_value = tuple(value.get("evidence_spans") or ())[0]
        span = _span(evidence_value, (fragment.span_start, fragment.span_end) if fragment else (0, 0))
        claim_id = str(value.get("claim_id") or _stable_id("CLAIM", {"fragment_id": fragment_id, "span": span, "type": value.get("claim_type", "fact")}))
        refs = tuple(dict(item) for item in (value.get("evidence_refs") or ()) if isinstance(item, Mapping))
        if not refs:
            refs = ({"type": "fragment", "id": fragment_id, "span": {"start": span[0], "end": span[1]}},)
        span_values = tuple(
            {"start": item[0], "end": item[1]}
            for item in (_span(item, span) for item in (value.get("evidence_spans") or ()))
        )
        if not span_values:
            span_values = ({"start": span[0], "end": span[1]},)
        return cls(
            claim_id=claim_id,
            fragment_id=fragment_id,
            message_id=message_id,
            evidence_span=span,
            claim_type=str(value.get("claim_type", value.get("claim_role", "fact")) or "fact"),
            speaker_id=str(value.get("speaker_id") or (fragment.speaker_id if fragment else UNKNOWN)),
            mentioned_person_ids=_tuple_text(value.get("mentioned_person_ids", fragment.mentioned_person_ids if fragment else ())),
            subject_id=str(value.get("subject_id") or (fragment.subject_id if fragment else UNKNOWN)),
            subject_type=str(value.get("subject_type") or (fragment.subject_type if fragment else "unknown")),
            object_id=str(value.get("object_id") or (fragment.object_id if fragment else UNKNOWN)),
            object_resolution=str(value.get("object_resolution") or (fragment.object_resolution if fragment else UNKNOWN)),
            object_evidence_refs=tuple(
                dict(item)
                for item in (value.get("object_evidence_refs") or (fragment.object_evidence_refs if fragment else ()))
                if isinstance(item, Mapping)
            ),
            object_inherited_from_id=(
                str(value.get("object_inherited_from_id"))
                if value.get("object_inherited_from_id") not in (None, "")
                else (fragment.object_inherited_from_id if fragment else None)
            ),
            state=str(value.get("state") or (fragment.state if fragment else UNKNOWN)),
            state_evidence=str(value.get("state_evidence") or (fragment.state_evidence if fragment else UNKNOWN)),
            modality=str(value.get("modality") or (fragment.modality if fragment else UNKNOWN)),
            polarity=str(value.get("polarity") or "unknown"),
            stance=str(value.get("stance") or "unknown"),
            evidence_refs=refs,
            source=str(value.get("source") or "workstream_a"),
            claim_text_redacted=str(value.get("claim_text_redacted") or value.get("claim_text") or (fragment.text if fragment else "")),
            target_entity_ids=_tuple_text(value.get("target_entity_ids")),
            event_mention_ids=_tuple_text(value.get("event_mention_ids")),
            evidence_spans=span_values,
            closure_reason=str(value.get("closure_reason") or (fragment.closure_reason if fragment else UNKNOWN)),
            temporal_qualifier=str(value.get("temporal_qualifier") or (fragment.temporal_qualifier if fragment else UNKNOWN)),
            start_time_offset_seconds=_number(value.get("start_time_offset_seconds")),
            end_time_offset_seconds=_number(value.get("end_time_offset_seconds")),
            start_time_source=str(value.get("start_time_source") or UNKNOWN),
            end_time_source=str(value.get("end_time_source") or UNKNOWN),
            information_value=str(value.get("information_value") or (fragment.information_value if fragment else "unknown")),
            event_completeness=str(value.get("event_completeness") or (fragment.event_completeness if fragment else "unknown")),
            context_message_ids=_tuple_text(value.get("context_message_ids", fragment.context_message_ids if fragment else ())),
            confidence=str(value.get("confidence") or "low"),
        )


@dataclass(frozen=True)
class ContextRelationCandidate:
    """A typed context edge; it is never an event merge."""

    context_relation_id: str
    left_anchor_id: str
    right_anchor_id: str
    anchor_type: str = "fragment"
    label: str = REL_INSUFFICIENT
    subtype: str = ""
    supporting_slot_codes: Tuple[str, ...] = ()
    conflicting_slot_codes: Tuple[str, ...] = ()
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    evidence_message_ids: Tuple[str, ...] = ()
    explicit_reply_present: bool = False
    time_distance_seconds: Optional[float] = None
    time_evidence: str = "none"
    evidence_strength: str = "none"
    confidence: str = "low"
    confidence_score: float = 0.0
    provenance: Dict[str, Any] = field(default_factory=dict)
    source: str = "workstream_a"

    @property
    def relation(self) -> str:
        return self.label

    @property
    def relation_type(self) -> str:
        return self.label

    @property
    def source_message_ids(self) -> Tuple[str, ...]:
        """Compatibility alias for the Stage1 context relation contract."""

        return self.evidence_message_ids

    @property
    def relation_id(self) -> str:
        return self.context_relation_id

    @property
    def id(self) -> str:
        return self.context_relation_id

    @property
    def is_event_merge(self) -> bool:
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context_relation_id": self.context_relation_id,
            "relation_id": self.context_relation_id,
            "left_anchor_id": self.left_anchor_id,
            "right_anchor_id": self.right_anchor_id,
            "anchor_type": self.anchor_type,
            "label": self.label,
            "relation": self.label,
            "relation_type": self.label,
            "subtype": self.subtype,
            "supporting_slot_codes": list(self.supporting_slot_codes),
            "conflicting_slot_codes": list(self.conflicting_slot_codes),
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "evidence_message_ids": list(self.evidence_message_ids),
            "explicit_reply_present": self.explicit_reply_present,
            "time_distance_seconds": self.time_distance_seconds,
            "time_evidence": self.time_evidence,
            "evidence_strength": self.evidence_strength,
            "confidence": self.confidence,
            "confidence_score": self.confidence_score,
            "provenance": dict(self.provenance),
            "source": self.source,
            "schema_version": SCHEMA_VERSION,
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "pipeline_version": BUNDLE_PIPELINE_VERSION,
            "ruleset_version": BUNDLE_RULESET_VERSION,
        }


@dataclass(frozen=True)
class OpenContextSnapshot:
    """Immutable, body-free snapshot of one still-open context window.

    A snapshot records unresolved slots and activation clues only; it never
    asserts that a thread is resolved or creates an event cluster.  The
    deterministic ``captured_at`` value is intentionally a projection of the
    input boundary rather than wall-clock time, keeping replays hash-stable.
    """

    snapshot_id: str
    captured_at: str = "unknown"
    analysis_run_id: str = "RUN_WORKSTREAM_A"
    open_thread_ids: Tuple[str, ...] = ()
    unresolved_slots: Tuple[Dict[str, Any], ...] = ()
    recent_fragment_ids: Tuple[str, ...] = ()
    recent_claim_ids: Tuple[str, ...] = ()
    pending_relation_candidates: Tuple[str, ...] = ()
    activation_cues: Tuple[str, ...] = ()
    excluded_candidate_reasons: Tuple[str, ...] = ()
    snapshot_version: str = "open_context_snapshot_v1"
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.snapshot_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "analysis_run_id": self.analysis_run_id,
            "open_thread_ids": list(self.open_thread_ids),
            "unresolved_slots": [dict(item) for item in self.unresolved_slots],
            "recent_fragment_ids": list(self.recent_fragment_ids),
            "recent_claim_ids": list(self.recent_claim_ids),
            "pending_relation_candidates": list(self.pending_relation_candidates),
            "activation_cues": list(self.activation_cues),
            "excluded_candidate_reasons": list(self.excluded_candidate_reasons),
            "snapshot_version": self.snapshot_version,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class DialogueBundle:
    """An open, reversible context window at one temporal scale."""

    bundle_id: str
    scale: str
    account_id: str
    chat_id: str
    fragment_ids: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    candidate_bundle_ids: Tuple[str, ...] = ()
    context_relation_ids: Tuple[str, ...] = ()
    source_message_ids: Tuple[str, ...] = ()
    speaker_ids: Tuple[str, ...] = ()
    mentioned_person_ids: Tuple[str, ...] = ()
    subject_ids: Tuple[str, ...] = ()
    object_refs: Tuple[Tuple[str, str], ...] = ()
    state_sequence: Tuple[str, ...] = ()
    latest_state: str = UNKNOWN
    closure_reason: str = UNKNOWN
    start_fragment_id: Optional[str] = None
    end_fragment_id: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    start_time_source: str = UNKNOWN
    end_time_source: str = UNKNOWN
    information_value: str = "unknown"
    event_completeness: str = "unknown"
    open_boundary: bool = True
    forced_snapshot: bool = False
    closed: bool = False
    candidate_rank: int = 0
    candidate_of: Optional[str] = None
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    uncertainties: Tuple[str, ...] = ()
    channel: str = CHANNEL_PENDING_CONTEXT
    input_hash: str = ""
    cache_key: str = ""
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = BUNDLE_PIPELINE_VERSION
    ruleset_version: str = BUNDLE_RULESET_VERSION

    @property
    def id(self) -> str:
        return self.bundle_id

    @property
    def dialogue_bundle_id(self) -> str:
        return self.bundle_id

    @property
    def analysis_run_id(self) -> str:
        # A deterministic run label keeps this low-cost artifact replayable;
        # callers that need per-run isolation can namespace the input IDs.
        return "RUN_WORKSTREAM_A"

    @property
    def anchor_fragment_ids(self) -> Tuple[str, ...]:
        return self.fragment_ids[:1]

    @property
    def anchor_claim_ids(self) -> Tuple[str, ...]:
        return self.claim_ids[:1]

    @property
    def candidate_window_id(self) -> str:
        return _stable_id("CANDIDATE_WINDOW", {"bundle_id": self.bundle_id, "scale": self.scale})

    @property
    def window_scale(self) -> str:
        return WINDOW_SCALE_BY_BUNDLE_SCALE.get(self.scale, WINDOW_W1)

    @property
    def member_message_ids(self) -> Tuple[str, ...]:
        return self.source_message_ids

    @property
    def member_fragment_ids(self) -> Tuple[str, ...]:
        return self.fragment_ids

    @property
    def member_claim_ids(self) -> Tuple[str, ...]:
        return self.claim_ids

    @property
    def member_bundle_ids(self) -> Tuple[str, ...]:
        return self.candidate_bundle_ids

    @property
    def open_context_snapshot_id(self) -> Optional[str]:
        if not self.open_boundary or self.closed:
            return None
        return _stable_id(
            "OPEN_CONTEXT_SNAPSHOT",
            {"bundle_id": self.bundle_id, "input_hash": self.input_hash},
        )

    @property
    def speaker_refs(self) -> Tuple[str, ...]:
        return self.speaker_ids

    @property
    def mentioned_person_refs(self) -> Tuple[str, ...]:
        return self.mentioned_person_ids

    @property
    def subject_refs(self) -> Tuple[str, ...]:
        return self.subject_ids

    @property
    def state_refs(self) -> Tuple[str, ...]:
        return self.state_sequence

    @property
    def unresolved_slot_codes(self) -> Tuple[str, ...]:
        return self.uncertainties

    @property
    def candidate_pair_ids(self) -> Tuple[str, ...]:
        return ()

    @property
    def chat_scope(self) -> Dict[str, Any]:
        return {"account_id": self.account_id, "chat_ids": [self.chat_id]}

    @property
    def cross_chat_bridge_refs(self) -> Tuple[str, ...]:
        return ()

    @property
    def budget_class(self) -> str:
        return "recovery" if self.scale == SCALE_COLD else "immediate" if self.channel == CHANNEL_IMMEDIATE else "deferred"

    @property
    def gate_channel(self) -> str:
        return self.channel

    @property
    def status(self) -> str:
        return "provisional" if self.closed else "open"

    @property
    def provenance(self) -> Dict[str, Any]:
        return {
            "stage": "dialogue_bundle",
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
            "event_merge": False,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dialogue_bundle_id": self.bundle_id,
            "bundle_id": self.bundle_id,
            "scale": self.scale,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "fragment_ids": list(self.fragment_ids),
            "claim_ids": list(self.claim_ids),
            "candidate_bundle_ids": list(self.candidate_bundle_ids),
            "context_relation_ids": list(self.context_relation_ids),
            "source_message_ids": list(self.source_message_ids),
            "speaker_ids": list(self.speaker_ids),
            "mentioned_person_ids": list(self.mentioned_person_ids),
            "subject_ids": list(self.subject_ids),
            "object_refs": [
                {"id": object_id, "object_id": object_id, "resolution": resolution, "source_id": None}
                for object_id, resolution in self.object_refs
            ],
            "state_sequence": list(self.state_sequence),
            "latest_state": self.latest_state,
            "closure_reason": self.closure_reason,
            "start_fragment_id": self.start_fragment_id,
            "end_fragment_id": self.end_fragment_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "start_time_source": self.start_time_source,
            "end_time_source": self.end_time_source,
            "information_value": self.information_value,
            "event_completeness": self.event_completeness,
            "open_boundary": self.open_boundary,
            "forced_snapshot": self.forced_snapshot,
            "closed": self.closed,
            "candidate_rank": self.candidate_rank,
            "candidate_of": self.candidate_of,
            "channel": self.channel,
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "uncertainties": list(self.uncertainties),
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
            # Contract-facing aliases.  The readable fields above remain for
            # existing shadow adapters, while these preserve the W0/W4 and
            # member/anchor vocabulary used by the bundle schema.
            "analysis_run_id": self.analysis_run_id,
            "anchor_fragment_ids": list(self.anchor_fragment_ids),
            "anchor_claim_ids": list(self.anchor_claim_ids),
            "candidate_window_id": self.candidate_window_id,
            "window_scale": self.window_scale,
            "member_message_ids": list(self.member_message_ids),
            "member_fragment_ids": list(self.member_fragment_ids),
            "member_claim_ids": list(self.member_claim_ids),
            "member_bundle_ids": list(self.member_bundle_ids),
            "open_context_snapshot_id": self.open_context_snapshot_id,
            "speaker_refs": list(self.speaker_refs),
            "mentioned_person_refs": list(self.mentioned_person_refs),
            "subject_refs": list(self.subject_refs),
            "state_refs": list(self.state_refs),
            "unresolved_slot_codes": list(self.unresolved_slot_codes),
            "candidate_pair_ids": list(self.candidate_pair_ids),
            "chat_scope": self.chat_scope,
            "cross_chat_bridge_refs": list(self.cross_chat_bridge_refs),
            "budget_class": self.budget_class,
            "gate_channel": self.gate_channel,
            "status": self.status,
            "provenance": self.provenance,
        }


def _open_context_snapshot(bundle: DialogueBundle) -> Optional[OpenContextSnapshot]:
    """Build a deterministic snapshot projection for an open bundle."""

    snapshot_id = bundle.open_context_snapshot_id
    if snapshot_id is None:
        return None
    unresolved = tuple(
        {
            "thread_id": bundle.bundle_id,
            "slot": code,
            "value": UNKNOWN,
            "resolution": UNKNOWN,
            "evidence_refs": [dict(item) for item in bundle.evidence_refs],
        }
        for code in bundle.uncertainties
    )
    return OpenContextSnapshot(
        snapshot_id=snapshot_id,
        open_thread_ids=(bundle.bundle_id,),
        unresolved_slots=unresolved,
        recent_fragment_ids=tuple(bundle.fragment_ids),
        recent_claim_ids=tuple(bundle.claim_ids),
        pending_relation_candidates=tuple(bundle.context_relation_ids),
        activation_cues=(),
        excluded_candidate_reasons=tuple(bundle.uncertainties),
        provenance={
            "stage": "dialogue_bundle",
            "bundle_id": bundle.bundle_id,
            "event_merge": False,
            "terminal_state_inferred": False,
        },
    )


@dataclass(frozen=True)
class DialogueBundleResult:
    """Immutable output of one builder view."""

    registrations: Tuple[RegisteredMessage, ...] = ()
    gate_decisions: Tuple[GateDecision, ...] = ()
    fragments: Tuple[BundleFragment, ...] = ()
    claims: Tuple[BundleClaim, ...] = ()
    relations: Tuple[ContextRelationCandidate, ...] = ()
    bundles: Tuple[DialogueBundle, ...] = ()
    open_context_snapshots: Tuple[OpenContextSnapshot, ...] = ()
    forced_snapshot: bool = False
    snapshot_reason: Optional[str] = None
    input_hash: str = ""
    cache_key: str = ""
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = BUNDLE_PIPELINE_VERSION
    ruleset_version: str = BUNDLE_RULESET_VERSION

    @property
    def context_relations(self) -> Tuple[ContextRelationCandidate, ...]:
        return self.relations

    @property
    def dialogue_bundles(self) -> Tuple[DialogueBundle, ...]:
        return self.bundles

    @property
    def snapshots(self) -> Tuple[OpenContextSnapshot, ...]:
        """Compatibility alias for the first-class open-context snapshots."""

        return self.open_context_snapshots

    def to_dict(self) -> Dict[str, Any]:
        return {
            "registrations": [item.to_dict() for item in self.registrations],
            "gate_decisions": [item.to_dict() for item in self.gate_decisions],
            "fragments": [item.to_dict() for item in self.fragments],
            "claims": [item.to_dict() for item in self.claims],
            "relations": [item.to_dict() for item in self.relations],
            "context_relations": [item.to_dict() for item in self.relations],
            "bundles": [item.to_dict() for item in self.bundles],
            "dialogue_bundles": [item.to_dict() for item in self.bundles],
            "open_context_snapshots": [item.to_dict() for item in self.open_context_snapshots],
            "forced_snapshot": self.forced_snapshot,
            "snapshot_reason": self.snapshot_reason,
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


class DialogueBundleBuilder:
    """Maintain finite-window candidate bundles without event merging."""

    def __init__(
        self,
        registry: Optional[MessageRegistry] = None,
        gate: Optional[SemanticGate] = None,
        *,
        window_size: int = 8,
        time_window_seconds: float = 15 * 60,
        max_candidates: int = 3,
    ) -> None:
        if int(window_size) < 1:
            raise ValueError("window_size must be positive")
        if float(time_window_seconds) < 0:
            raise ValueError("time_window_seconds must be non-negative")
        if int(max_candidates) < 1:
            raise ValueError("max_candidates must be positive")
        self.registry = registry if registry is not None else MessageRegistry()
        self.gate = gate if gate is not None else SemanticGate(self.registry)
        if self.gate.registry is not self.registry:
            # A gate with another registry would make metadata/hash lookups
            # ambiguous; sharing the explicit registry is safer than guessing.
            raise ValueError("gate and registry must reference the same registry")
        self.window_size = int(window_size)
        self.time_window_seconds = float(time_window_seconds)
        self.max_candidates = int(max_candidates)
        self._registrations: Dict[str, RegisteredMessage] = {}
        self._fragments: List[BundleFragment] = []
        self._claims: Dict[str, BundleClaim] = {}
        self._relations: Dict[str, ContextRelationCandidate] = {}
        self._bundles: Dict[str, DialogueBundle] = {}
        self._active_local: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        self._active_turn: Dict[str, str] = {}
        self._active_session: Dict[Tuple[str, str], str] = {}
        self._active_cold: Dict[str, str] = {}
        self._snapshot_counter = 0

    def __len__(self) -> int:
        return len(self._fragments)

    def _entry_for_fragment(self, fragment: BundleFragment) -> RegisteredMessage:
        existing = self.registry.maybe_get(fragment.message_id)
        if existing is not None:
            return existing
        # This adapter uses only fields explicitly carried by the fragment;
        # missing scope/speaker remains unknown in the registry.
        return self.registry.register(
            {
                "message_id": fragment.message_id,
                "account_id": fragment.account_id,
                "chat_id": fragment.chat_id,
                "speaker_id": fragment.speaker_id,
                "content": fragment.text,
                "message_type": "text" if not fragment.is_silent else "unknown",
                "time_offset_seconds": fragment.time_offset_seconds,
                "dialogue_segment_id": fragment.segment_id,
                "dialogue_role": fragment.role,
                "split": "development",
            }
        )

    def _bind_fragment(self, fragment: BundleFragment, entry: RegisteredMessage, decision: GateDecision) -> BundleFragment:
        # A supplied structured fragment may omit metadata that is authoritative
        # on its source registration.  Fill only unknown fields; never replace
        # a conflicting explicit value with a guess.
        updates: Dict[str, Any] = {"channel": decision.channel}
        if fragment.object_resolution == "inherited" and not fragment.object_inherited_from_id:
            updates.update(object_id=UNKNOWN, object_resolution=UNKNOWN, object_evidence_refs=())
        if (fragment.is_silent or fragment.is_opener) and fragment.state in TERMINAL_STATES:
            # Silence/opener is retained as evidence but cannot provide a
            # terminal state or closure proof.
            updates.update(state=UNKNOWN, state_evidence=UNKNOWN, closure_reason=UNKNOWN)
        if not _known(fragment.account_id) and _known(entry.account_id):
            updates["account_id"] = entry.account_id
        if not _known(fragment.chat_id) and _known(entry.chat_id):
            updates["chat_id"] = entry.chat_id
        if not _known(fragment.speaker_id) and _known(entry.metadata.speaker_id):
            updates["speaker_id"] = entry.metadata.speaker_id
        if fragment.segment_id is None and entry.metadata.dialogue_segment_id is not None:
            updates["segment_id"] = entry.metadata.dialogue_segment_id
        if fragment.time_offset_seconds is None:
            updates["time_offset_seconds"] = entry.metadata.time_offset_seconds
        if fragment.timestamp is None:
            updates["timestamp"] = entry.metadata.timestamp
        if fragment.reply_to_message_id is None:
            updates["reply_to_message_id"] = entry.metadata.reply_to_message_id
        if not fragment.evidence_refs and not fragment.is_silent:
            updates["evidence_refs"] = (
                {"type": "message", "id": fragment.message_id, "span": {"start": fragment.span_start, "end": fragment.span_end}},
            )
        if not fragment.object_evidence_refs and fragment.object_resolution == "explicit" and fragment.object_known:
            updates["object_evidence_refs"] = (
                {"type": "fragment", "id": fragment.fragment_id, "span": {"start": fragment.span_start, "end": fragment.span_end}},
            )
        return replace(fragment, **updates)

    def _fragment_candidates(self, entry: RegisteredMessage, decision: GateDecision) -> Tuple[BundleFragment, ...]:
        payload = entry.raw_message_ref
        supplied = payload.get("fragment_candidates", payload.get("fragments"))
        if supplied is not None:
            if isinstance(supplied, Mapping):
                supplied_values = (supplied,)
            else:
                try:
                    supplied_values = tuple(supplied)
                except TypeError:
                    supplied_values = ()
            result: List[BundleFragment] = []
            for value in supplied_values:
                if isinstance(value, BundleFragment):
                    fragment = value
                elif isinstance(value, Mapping):
                    fragment = BundleFragment.from_mapping(value, fallback_message_id=entry.message_id)
                else:
                    continue
                result.append(self._bind_fragment(fragment, entry, decision))
            if result:
                return tuple(result)
        return (self._bind_fragment(BundleFragment.from_registered(entry, channel=decision.channel), entry, decision),)

    def _normalise_claim(self, claim: Any, fragment: BundleFragment) -> BundleClaim:
        if isinstance(claim, BundleClaim):
            value = claim
        elif isinstance(claim, Mapping):
            value = BundleClaim.from_mapping(claim, fragment=fragment)
        else:
            raise TypeError("claim candidates must be mappings or BundleClaim")
        if value.fragment_id != fragment.fragment_id or value.message_id != fragment.message_id:
            raise ValueError("claim evidence must point to its source fragment/message")
        start, end = value.evidence_span
        if start < fragment.span_start or end > fragment.span_end or start > end:
            raise ValueError("claim evidence span must be inside its fragment")
        if value.object_resolution not in KNOWN_RESOLUTIONS:
            value = replace(value, object_resolution=UNKNOWN)
        if value.object_resolution == "inherited" and not value.object_inherited_from_id:
            value = replace(value, object_id=UNKNOWN, object_resolution=UNKNOWN, object_evidence_refs=())
        if value.object_resolution == "explicit" and not value.object_evidence_refs and value.object_id not in (UNKNOWN, ""):
            value = replace(
                value,
                object_evidence_refs=(
                    {"type": "fragment", "id": fragment.fragment_id, "span": {"start": start, "end": end}},
                ),
            )
        if value.state not in KNOWN_STATES:
            value = replace(value, state=UNKNOWN)
        if (fragment.is_silent or fragment.is_opener) and value.state in TERMINAL_STATES:
            value = replace(value, state=UNKNOWN, state_evidence=UNKNOWN, closure_reason=UNKNOWN)
        if len(value.evidence_refs) != 1:
            value = replace(
                value,
                evidence_refs=(
                    {"type": "fragment", "id": fragment.fragment_id, "span": {"start": start, "end": end}},
                ),
            )
        return value

    def _claims_for(
        self,
        fragment: BundleFragment,
        supplied: Iterable[Any],
        *,
        auto_generate: bool = True,
    ) -> Tuple[BundleClaim, ...]:
        values: List[Any] = list(supplied or ())
        if not values:
            payload_claims = None
            entry = self.registry.maybe_get(fragment.message_id)
            if entry is not None:
                payload_claims = entry.raw_message_ref.get("claims")
            if payload_claims is not None:
                try:
                    values = list(payload_claims) if not isinstance(payload_claims, Mapping) else [payload_claims]
                except TypeError:
                    values = []
        if not values and auto_generate and (
            fragment.role == "substantive"
            and not fragment.is_silent
            and fragment.fragment_type in {"statement", "question", "request", "answer"}
        ):
            values = [
                BundleClaim(
                    claim_id=_stable_id("CLAIM", {"fragment_id": fragment.fragment_id, "span": (fragment.span_start, fragment.span_end), "role": fragment.claim_role}),
                    fragment_id=fragment.fragment_id,
                    message_id=fragment.message_id,
                    evidence_span=(fragment.span_start, fragment.span_end),
                    claim_type=fragment.claim_role if fragment.claim_role in {"fact", "opinion", "question", "suggestion", "hypothesis"} else "fact",
                    speaker_id=fragment.speaker_id,
                    mentioned_person_ids=fragment.mentioned_person_ids,
                    subject_id=fragment.subject_id,
                    subject_type=fragment.subject_type,
                    object_id=fragment.object_id,
                    object_resolution=fragment.object_resolution,
                    object_evidence_refs=fragment.object_evidence_refs,
                    state=fragment.state,
                    state_evidence=fragment.state_evidence,
                    modality=fragment.modality,
                    claim_text_redacted=fragment.text,
                    evidence_spans=({"start": fragment.span_start, "end": fragment.span_end},),
                    closure_reason=fragment.closure_reason,
                    temporal_qualifier=fragment.temporal_qualifier,
                    start_time_offset_seconds=fragment.start_time,
                    end_time_offset_seconds=fragment.end_time,
                    start_time_source=fragment.start_time_source,
                    end_time_source=fragment.end_time_source,
                    information_value=fragment.information_value,
                    event_completeness=fragment.event_completeness,
                    context_message_ids=fragment.context_message_ids,
                )
            ]
        result: List[BundleClaim] = []
        for value in values:
            claim = self._normalise_claim(value, fragment)
            prior = self._claims.get(claim.claim_id)
            if prior is not None and (
                prior.fragment_id != claim.fragment_id
                or prior.message_id != claim.message_id
                or prior.evidence_span != claim.evidence_span
            ):
                raise ValueError("claim_id has more than one evidence location")
            self._claims[claim.claim_id] = claim
            if claim.claim_id not in result:
                result.append(claim)
        return tuple(result)

    def _time_distance(self, left: BundleFragment, right: BundleFragment) -> Optional[float]:
        left_time, right_time = _time_value(left), _time_value(right)
        if left_time is None or right_time is None:
            return None
        return abs(right_time - left_time)

    def _relation(self, left: BundleFragment, right: BundleFragment) -> Optional[ContextRelationCandidate]:
        left_scope, right_scope = left.scope_key, right.scope_key
        # Unknown/unequal account+chat scopes are a hard boundary.  No shared
        # object or timestamp can override it.
        if left_scope is None or right_scope is None or left_scope != right_scope:
            return None
        if (
            left.is_silent
            or right.is_silent
            or left.is_opener
            or right.is_opener
            or left.role == "context_only"
            or right.role == "context_only"
        ):
            return None
        left_object = left.object_id if left.object_known else None
        right_object = right.object_id if right.object_known else None
        shared_object = left_object is not None and left_object == right_object
        object_conflict = left_object is not None and right_object is not None and left_object != right_object
        shared_subject = _known(left.subject_id) and left.subject_id == right.subject_id
        shared_people = bool(set(left.mentioned_person_ids) & set(right.mentioned_person_ids))
        shared_actions = bool(set(left.actions) & set(right.actions))
        shared_state = left.state in KNOWN_STATES - {UNKNOWN} and left.state == right.state
        state_change = shared_object and left.state in KNOWN_STATES - {UNKNOWN} and right.state in KNOWN_STATES - {UNKNOWN} and left.state != right.state
        explicit_reply = right.reply_to_message_id == left.message_id and right.reply_to_message_id not in (None, "")
        inherited = right.object_resolution == "inherited" and right.object_known and right.object_inherited_from_id == left.fragment_id
        qa = left.intent in {"question", "request"} and right.intent in {"answer", "statement", "request"}
        answer_cue = right.fragment_type == "answer" or right.intent == "answer"
        signals: List[str] = []
        if shared_object:
            signals.append("shared_object")
        if inherited:
            signals.append("object_inheritance")
        if shared_subject:
            signals.append("shared_subject")
        if shared_people:
            signals.append("shared_mentioned_person")
        if shared_actions:
            signals.append("shared_action")
        if shared_state:
            signals.append("shared_state")
        if state_change:
            signals.append("state_change")
        if explicit_reply:
            signals.append("explicit_reply")
        if qa:
            signals.append("question_or_request_then_turn")
        if right.topic_shift:
            signals.append("explicit_topic_shift")
        conflicts: List[str] = []
        if object_conflict:
            conflicts.append("object_conflict")
        if _known(left.subject_id) and _known(right.subject_id) and left.subject_id != right.subject_id:
            conflicts.append("subject_conflict")

        if right.topic_shift:
            return self._make_relation(left, right, REL_TOPIC_SHIFT, "topic_shift", signals, conflicts, explicit_reply, 0.82, "medium")
        semantic_count = sum(
            bool(item)
            for item in (shared_object, inherited, shared_subject, shared_people, shared_actions, shared_state, state_change)
        )
        compatible = not object_conflict and (shared_object or inherited or shared_subject or shared_people or shared_actions)
        if explicit_reply and compatible and (qa or answer_cue or semantic_count >= 2):
            return self._make_relation(left, right, REL_ANSWERS, "question_answer", signals, conflicts, True, 0.94, "strong")
        if qa and answer_cue and compatible and semantic_count >= 2:
            return self._make_relation(left, right, REL_ANSWERS, "question_answer", signals, conflicts, False, 0.84, "medium")
        if right.contrast_marker and shared_object:
            return self._make_relation(left, right, REL_CONTRASTS, "contrast", signals, conflicts, explicit_reply, 0.82, "strong")
        if state_change and not object_conflict:
            return self._make_relation(left, right, REL_CONTINUES, "state_update", signals, conflicts, explicit_reply, 0.88, "strong")
        if inherited and not object_conflict:
            return self._make_relation(left, right, REL_ELABORATES, "object_inheritance", signals, conflicts, explicit_reply, 0.86, "strong")
        if shared_object and (shared_actions or shared_state or state_change):
            return self._make_relation(left, right, REL_CONTINUES, "continuation", signals, conflicts, explicit_reply, 0.72, "medium")
        if (shared_subject or shared_people) and shared_actions and not object_conflict:
            return self._make_relation(left, right, REL_ELABORATES, "person_object_history", signals, conflicts, explicit_reply, 0.68, "medium")
        if shared_object and not object_conflict:
            return self._make_relation(left, right, REL_POSSIBLY_RELATED, "shared_object", signals, conflicts, explicit_reply, 0.35, "weak")
        return None

    def _make_relation(
        self,
        left: BundleFragment,
        right: BundleFragment,
        label: str,
        subtype: str,
        signals: Iterable[str],
        conflicts: Iterable[str],
        explicit_reply: bool,
        score: float,
        strength: str,
    ) -> ContextRelationCandidate:
        distance = self._time_distance(left, right)
        support = list(dict.fromkeys(str(item) for item in signals))
        if distance is not None:
            support.append("time_proximity_weak")
        time_evidence = "weak" if distance is not None else "none"
        relation_id = _stable_id(
            "CONTEXT_RELATION",
            {"left": left.fragment_id, "right": right.fragment_id, "label": label, "ruleset": BUNDLE_RULESET_VERSION},
        )
        return ContextRelationCandidate(
            context_relation_id=relation_id,
            left_anchor_id=left.fragment_id,
            right_anchor_id=right.fragment_id,
            anchor_type="fragment",
            label=label,
            subtype=subtype,
            supporting_slot_codes=tuple(dict.fromkeys(support)),
            conflicting_slot_codes=tuple(dict.fromkeys(str(item) for item in conflicts)),
            evidence_refs=(
                {"type": "fragment", "id": left.fragment_id, "span": {"start": left.span_start, "end": left.span_end}},
                {"type": "fragment", "id": right.fragment_id, "span": {"start": right.span_start, "end": right.span_end}},
            ),
            evidence_message_ids=(left.message_id, right.message_id),
            explicit_reply_present=explicit_reply,
            time_distance_seconds=distance,
            time_evidence=time_evidence,
            evidence_strength=strength,
            confidence="high" if score >= 0.85 else "medium" if score >= 0.6 else "low",
            confidence_score=score,
            provenance={
                "stage": "context_bundle_candidate",
                "time_is_weak_only": True,
                "same_segment_is_not_sufficient": True,
                "event_merge": False,
            },
            source="workstream_a",
        )

    def _append_relation(self, relation: Optional[ContextRelationCandidate]) -> None:
        if relation is not None:
            self._relations[relation.context_relation_id] = relation

    def _new_bundle(
        self,
        scale: str,
        fragment: BundleFragment,
        claims: Tuple[BundleClaim, ...],
        *,
        candidate_of: Optional[str] = None,
        candidate_rank: int = 0,
    ) -> DialogueBundle:
        if scale not in BUNDLE_SCALES:
            raise ValueError("unknown bundle scale: %s" % scale)
        scope = fragment.scope_key
        account_id, chat_id = scope if scope is not None else (fragment.account_id, fragment.chat_id)
        bundle_id = _stable_id(
            "DIALOGUE_BUNDLE",
            {
                "scale": scale,
                "scope": (account_id, chat_id),
                "fragment_ids": (fragment.fragment_id,),
                "candidate_of": candidate_of,
                "rank": candidate_rank,
                "ruleset": BUNDLE_RULESET_VERSION,
            },
        )
        state = fragment.state if fragment.state in KNOWN_STATES else UNKNOWN
        closed = (
            state in TERMINAL_STATES
            and fragment.state_evidence == "explicit"
            and fragment.object_known
            and not fragment.is_silent
        )
        uncertainty = []
        if fragment.object_resolution == UNKNOWN:
            uncertainty.append("object_unknown")
        if fragment.state == UNKNOWN:
            uncertainty.append("state_unknown")
        if fragment.is_silent:
            uncertainty.append("silent_turn")
        if fragment.is_opener:
            uncertainty.append("conversation_opener")
        claim_ids = tuple(item.claim_id for item in claims)
        object_refs = () if not fragment.object_known else ((fragment.object_id, fragment.object_resolution),)
        evidence_refs = () if fragment.is_silent else fragment.evidence_refs
        input_hash = stable_hash({"fragment": fragment.fragment_id, "claims": claim_ids, "scale": scale})
        return DialogueBundle(
            bundle_id=bundle_id,
            scale=scale,
            account_id=account_id,
            chat_id=chat_id,
            fragment_ids=(fragment.fragment_id,),
            claim_ids=claim_ids,
            candidate_bundle_ids=(),
            context_relation_ids=(),
            source_message_ids=(fragment.message_id,),
            speaker_ids=(fragment.speaker_id,) if _known(fragment.speaker_id) else (),
            mentioned_person_ids=tuple(dict.fromkeys(fragment.mentioned_person_ids)),
            subject_ids=(fragment.subject_id,) if _known(fragment.subject_id) else (),
            object_refs=object_refs,
            state_sequence=(state,) if state != UNKNOWN else (),
            latest_state=state,
            closure_reason=fragment.closure_reason if closed else UNKNOWN,
            start_fragment_id=None,
            end_fragment_id=None,
            start_time=None,
            end_time=None,
            start_time_source=UNKNOWN,
            end_time_source=UNKNOWN,
            information_value=fragment.information_value,
            event_completeness=fragment.event_completeness,
            open_boundary=not closed,
            forced_snapshot=False,
            closed=closed,
            candidate_rank=candidate_rank,
            candidate_of=candidate_of,
            evidence_refs=evidence_refs,
            uncertainties=tuple(dict.fromkeys(uncertainty)),
            channel=fragment.channel if fragment.channel in {CHANNEL_IMMEDIATE, CHANNEL_PENDING_CONTEXT, CHANNEL_BACKGROUND, CHANNEL_COLD_RECOVERABLE} else CHANNEL_PENDING_CONTEXT,
            input_hash=input_hash,
            cache_key="bundle:%s:%s" % (BUNDLE_RULESET_VERSION, input_hash),
        )

    def _append_bundle(
        self,
        bundle: DialogueBundle,
        fragment: BundleFragment,
        claims: Tuple[BundleClaim, ...],
        relation_ids: Iterable[str],
    ) -> DialogueBundle:
        state = fragment.state if fragment.state in KNOWN_STATES else UNKNOWN
        states = list(bundle.state_sequence)
        if state != UNKNOWN and (not states or states[-1] != state):
            states.append(state)
        closed = bundle.closed or (
            state in TERMINAL_STATES
            and fragment.state_evidence == "explicit"
            and fragment.object_known
            and not fragment.is_silent
        )
        object_refs = list(bundle.object_refs)
        if fragment.object_known and (fragment.object_id, fragment.object_resolution) not in object_refs:
            object_refs.append((fragment.object_id, fragment.object_resolution))
        speakers = list(bundle.speaker_ids)
        if _known(fragment.speaker_id) and fragment.speaker_id not in speakers:
            speakers.append(fragment.speaker_id)
        subjects = list(bundle.subject_ids)
        if _known(fragment.subject_id) and fragment.subject_id not in subjects:
            subjects.append(fragment.subject_id)
        mentioned = list(bundle.mentioned_person_ids)
        for person_id in fragment.mentioned_person_ids:
            if person_id not in mentioned:
                mentioned.append(person_id)
        fragment_ids = bundle.fragment_ids + (fragment.fragment_id,)
        claim_ids = list(bundle.claim_ids)
        for claim in claims:
            if claim.claim_id not in claim_ids:
                claim_ids.append(claim.claim_id)
        source_ids = bundle.source_message_ids + ((fragment.message_id,) if fragment.message_id not in bundle.source_message_ids else ())
        relation_values = list(bundle.context_relation_ids)
        for relation_id in relation_ids:
            if relation_id not in relation_values:
                relation_values.append(relation_id)
        uncertainties = list(bundle.uncertainties)
        if fragment.object_resolution == UNKNOWN and "object_unknown" not in uncertainties:
            uncertainties.append("object_unknown")
        if fragment.state == UNKNOWN and "state_unknown" not in uncertainties:
            uncertainties.append("state_unknown")
        if fragment.is_silent and "silent_turn" not in uncertainties:
            uncertainties.append("silent_turn")
        information = bundle.information_value
        order = {"none": 0, "low": 1, "unknown": 1, "medium": 2, "high": 3}
        if order.get(fragment.information_value, 1) > order.get(information, 1):
            information = fragment.information_value
        completeness = "sufficient" if bundle.event_completeness == "sufficient" or (
            fragment.object_resolution == "explicit" and fragment.state in TERMINAL_STATES and not fragment.is_silent
        ) else bundle.event_completeness
        input_hash = stable_hash({"fragment_ids": fragment_ids, "claim_ids": claim_ids, "relations": relation_values, "scale": bundle.scale})
        return replace(
            bundle,
            fragment_ids=fragment_ids,
            claim_ids=tuple(claim_ids),
            context_relation_ids=tuple(relation_values),
            source_message_ids=tuple(source_ids),
            speaker_ids=tuple(speakers),
            mentioned_person_ids=tuple(mentioned),
            subject_ids=tuple(subjects),
            object_refs=tuple(object_refs),
            state_sequence=tuple(states),
            latest_state=state if state != UNKNOWN else bundle.latest_state,
            closure_reason=fragment.closure_reason if closed and fragment.closure_reason != UNKNOWN else bundle.closure_reason,
            end_fragment_id=None,
            information_value=information,
            event_completeness=completeness,
            open_boundary=not closed,
            closed=closed,
            evidence_refs=bundle.evidence_refs + (() if fragment.is_silent else fragment.evidence_refs),
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            input_hash=input_hash,
            cache_key="bundle:%s:%s" % (BUNDLE_RULESET_VERSION, input_hash),
        )

    def _window_candidates(self, fragment: BundleFragment) -> Tuple[BundleFragment, ...]:
        values = self._fragments[-self.window_size :]
        result: List[BundleFragment] = []
        for previous in reversed(values):
            if previous.fragment_id == fragment.fragment_id:
                continue
            # A long gap is a scale hint, not a semantic hard cut.  Keep a
            # finite fragment window and let explicit semantic support decide.
            result.append(previous)
        return tuple(result)

    def _add_scales(
        self,
        fragment: BundleFragment,
        claims: Tuple[BundleClaim, ...],
        relation_ids: Tuple[str, ...],
        decision: GateDecision,
    ) -> Tuple[DialogueBundle, ...]:
        created: List[DialogueBundle] = []
        micro = self._new_bundle(SCALE_MICRO, fragment, claims)
        self._bundles[micro.bundle_id] = micro
        created.append(micro)

        # A turn groups multiple fragments from the same message without
        # treating their common message/segment metadata as semantic linkage.
        # This is a separate scale from the local/session context windows.
        turn_id = self._active_turn.get(fragment.message_id)
        if turn_id is None or turn_id not in self._bundles:
            turn_bundle = self._new_bundle(SCALE_TURN, fragment, claims)
        else:
            turn_bundle = self._append_bundle(self._bundles[turn_id], fragment, claims, ())
        self._bundles[turn_bundle.bundle_id] = turn_bundle
        self._active_turn[fragment.message_id] = turn_bundle.bundle_id
        created.append(turn_bundle)

        scope = fragment.scope_key or (fragment.account_id, fragment.chat_id, fragment.message_id)
        parents: List[DialogueBundle] = []
        for bundle_id in tuple(self._active_local.get(scope, ())):
            current = self._bundles.get(bundle_id)
            if current is None or current.closed:
                continue
            last_id = current.fragment_ids[-1] if current.fragment_ids else None
            last = next((item for item in reversed(self._fragments[:-1]) if item.fragment_id == last_id), None)
            if last is None:
                continue
            relation = self._relation(last, fragment)
            if relation is not None and relation.label != REL_TOPIC_SHIFT and "object_conflict" not in relation.conflicting_slot_codes:
                self._append_relation(relation)
                parents.append(current)
        parents = parents[: self.max_candidates]
        local_bundles: List[DialogueBundle] = []
        if not parents:
            local = self._new_bundle(SCALE_LOCAL, fragment, claims)
            self._bundles[local.bundle_id] = local
            local_bundles.append(local)
            # Keep recent open alternatives alive when a new object/topic has
            # no semantic parent.  A later fragment with a shared subject or
            # action may legitimately resolve to more than one local bundle;
            # dropping the older candidate here would make the result
            # irreversible.  Explicit topic shifts still block relation
            # construction below, so they cannot merge the old bundle.
            for prior_id in tuple(self._active_local.get(scope, ())):
                prior = self._bundles.get(prior_id)
                if prior is not None and not prior.closed and prior.bundle_id != local.bundle_id:
                    local_bundles.append(prior)
            local_bundles = local_bundles[: self.max_candidates]
        else:
            for rank, parent in enumerate(parents):
                parent_relation_ids = tuple(
                    relation_id
                    for relation_id in relation_ids
                    if relation_id in self._relations
                    and self._relations[relation_id].left_anchor_id in parent.fragment_ids
                )
                local = self._append_bundle(parent, fragment, claims, parent_relation_ids)
                local = replace(
                    local,
                    bundle_id=_stable_id("DIALOGUE_BUNDLE", {"base": parent.bundle_id, "fragment": fragment.fragment_id, "rank": rank}),
                    candidate_rank=rank,
                    candidate_of=parent.bundle_id,
                )
                self._bundles[local.bundle_id] = local
                local_bundles.append(local)
        if len(local_bundles) > 1:
            candidate_ids = tuple(item.bundle_id for item in local_bundles)
            for item in local_bundles:
                self._bundles[item.bundle_id] = replace(item, candidate_bundle_ids=candidate_ids)
        self._active_local[scope] = [item.bundle_id for item in local_bundles]
        created.extend(local_bundles)

        session = self._active_session.get(scope)
        if session is None or session not in self._bundles:
            session_bundle = self._new_bundle(SCALE_SESSION, fragment, claims)
        else:
            session_parent = self._bundles[session]
            session_relation_ids = tuple(
                relation_id
                for relation_id in relation_ids
                if relation_id in self._relations
                and self._relations[relation_id].left_anchor_id in session_parent.fragment_ids
            )
            session_bundle = self._append_bundle(session_parent, fragment, claims, session_relation_ids)
        self._bundles[session_bundle.bundle_id] = session_bundle
        self._active_session[scope] = session_bundle.bundle_id
        created.append(session_bundle)

        if decision.channel == CHANNEL_COLD_RECOVERABLE:
            # Keep a same-scope sparse-recall window (W3) beside the explicit
            # cold-recovery lane (W4).  A long gap is a recall hint, never a
            # hard semantic link; both windows remain reversible.
            sparse = self._new_bundle(SCALE_SPARSE, fragment, claims)
            self._bundles[sparse.bundle_id] = sparse
            created.append(sparse)
            cold_key = fragment.message_id
            cold = self._new_bundle(SCALE_COLD, fragment, claims)
            self._bundles[cold.bundle_id] = cold
            self._active_cold[cold_key] = cold.bundle_id
            created.append(cold)
        return tuple(created)

    def add_fragment(
        self,
        fragment: Any,
        *,
        claims: Iterable[Any] = (),
        gate_decision: Optional[GateDecision] = None,
        auto_generate_claims: bool = True,
    ) -> Tuple[DialogueBundle, ...]:
        """Add one public fragment and return affected bundle snapshots."""

        if isinstance(fragment, BundleFragment):
            value = fragment
        elif isinstance(fragment, Mapping):
            value = BundleFragment.from_mapping(fragment)
        else:
            raise TypeError("fragment must be a mapping or BundleFragment")
        if any(item.fragment_id == value.fragment_id for item in self._fragments):
            raise ValueError("duplicate fragment_id: %s" % value.fragment_id)
        entry = self._entry_for_fragment(value)
        self._registrations[entry.registry_key] = entry
        decision = gate_decision or self.gate.route(entry)
        value = self._bind_fragment(value, entry, decision)
        claim_values = self._claims_for(value, claims, auto_generate=auto_generate_claims)
        for claim in claim_values:
            if claim.claim_id not in value.claim_ids:
                value = replace(value, claim_ids=value.claim_ids + (claim.claim_id,))
        previous_values = self._window_candidates(value)
        relation_ids: List[str] = []
        for previous in previous_values:
            relation = self._relation(previous, value)
            self._append_relation(relation)
            if relation is not None:
                relation_ids.append(relation.context_relation_id)
        self._fragments.append(value)
        return self._add_scales(value, claim_values, tuple(relation_ids), decision)

    def add_message(
        self,
        message: Any,
        *,
        fragments: Optional[Iterable[Any]] = None,
        claims: Iterable[Any] = (),
    ) -> Tuple[BundleFragment, ...]:
        """Register a message and add one or more supplied/default fragments."""

        entry = message if isinstance(message, RegisteredMessage) else self.registry.register(message)
        self._registrations[entry.registry_key] = entry
        decision = self.gate.route(entry)
        values: Tuple[Any, ...]
        if fragments is None:
            values = self._fragment_candidates(entry, decision)
        else:
            values = tuple(fragments)
        materialized_claims = tuple(claims or ())
        result: List[BundleFragment] = []
        for value in values:
            if isinstance(value, BundleFragment):
                fragment = value
            elif isinstance(value, Mapping):
                fragment = BundleFragment.from_mapping(value, fallback_message_id=entry.message_id)
            else:
                raise TypeError("fragments must be mappings or BundleFragment")
            # Avoid cross-message accidental attachment while keeping a caller
            # supplied missing message ID anchored to this registration.
            if fragment.message_id in {UNKNOWN, ""}:
                fragment = replace(fragment, message_id=entry.message_id)
            claim_subset = [
                claim
                for claim in materialized_claims
                if _value(claim, "fragment_id", fragment.fragment_id) == fragment.fragment_id
            ]
            self.add_fragment(
                fragment,
                claims=claim_subset,
                gate_decision=decision,
                auto_generate_claims=not bool(materialized_claims),
            )
            result.append(self._fragments[-1])
        return tuple(result)

    def ingest(
        self,
        messages: Iterable[Any],
        *,
        fragments: Optional[Iterable[Any]] = None,
        claims: Optional[Iterable[Any]] = None,
    ) -> DialogueBundleResult:
        """Ingest messages with optional public fragment/claim candidates."""

        fragment_map: Dict[str, List[Any]] = defaultdict(list)
        for value in tuple(fragments or ()):
            message_id = str(_value(value, "message_id", UNKNOWN) or UNKNOWN)
            fragment_map[message_id].append(value)
        claim_values = tuple(claims or ())
        consumed_message_ids: Set[str] = set()
        for message in tuple(messages or ()):
            message_id = str(_value(message, "message_id", UNKNOWN) or UNKNOWN)
            self.add_message(
                message,
                fragments=fragment_map.get(message_id) if message_id in fragment_map else None,
                claims=[claim for claim in claim_values if str(_value(claim, "message_id", UNKNOWN) or UNKNOWN) == message_id],
            )
            consumed_message_ids.add(message_id)
        # Keep explicitly supplied fragment candidates recoverable even when
        # an adapter omitted their message envelope.  This path still uses
        # the fragment's own public metadata and never fabricates sequence or
        # time values.
        for message_id, values in fragment_map.items():
            if message_id in consumed_message_ids:
                continue
            for value in values:
                self.add_fragment(
                    value,
                    claims=[claim for claim in claim_values if str(_value(claim, "message_id", UNKNOWN) or UNKNOWN) == message_id],
                )
        return self.build()

    def force_snapshot(self, reason: str = "forced_snapshot") -> DialogueBundleResult:
        return self.snapshot(force=True, reason=reason)

    def snapshot(self, *, force: bool = False, reason: Optional[str] = None) -> DialogueBundleResult:
        """Return an immutable view; forcing never closes an open boundary."""

        self._snapshot_counter += 1
        bundles = tuple(self._bundles.values())
        if force:
            bundles = tuple(
                replace(item, forced_snapshot=True, open_boundary=item.open_boundary or not item.closed)
                for item in bundles
            )
        return self._result(bundles=bundles, forced_snapshot=bool(force), reason=reason)

    def _result(
        self,
        *,
        bundles: Optional[Tuple[DialogueBundle, ...]] = None,
        forced_snapshot: bool = False,
        reason: Optional[str] = None,
    ) -> DialogueBundleResult:
        registrations = tuple(self._registrations.values())
        decisions = tuple(
            self.gate.current(entry.registry_key)
            for entry in registrations
            if self.gate.history(entry.registry_key)
        )
        fragments = tuple(self._fragments)
        claims = tuple(self._claims.values())
        relations = tuple(self._relations.values())
        values = tuple(self._bundles.values()) if bundles is None else bundles
        open_snapshots = tuple(
            snapshot
            for item in values
            for snapshot in (_open_context_snapshot(item),)
            if snapshot is not None
        )
        input_hash = stable_hash(
            {
                "registrations": [entry.record_hash for entry in registrations],
                "fragments": [item.fragment_id for item in fragments],
                "claims": [item.claim_id for item in claims],
                "relations": [item.context_relation_id for item in relations],
                "bundles": [item.bundle_id for item in values],
                "forced_snapshot": forced_snapshot,
            }
        )
        return DialogueBundleResult(
            registrations=registrations,
            gate_decisions=decisions,
            fragments=fragments,
            claims=claims,
            relations=relations,
            bundles=values,
            open_context_snapshots=open_snapshots,
            forced_snapshot=forced_snapshot,
            snapshot_reason=reason,
            input_hash=input_hash,
            cache_key="dialogue-bundle:%s:%s" % (BUNDLE_RULESET_VERSION, input_hash),
            schema_version=SCHEMA_VERSION,
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            pipeline_version=BUNDLE_PIPELINE_VERSION,
            ruleset_version=BUNDLE_RULESET_VERSION,
        )

    def build(self) -> DialogueBundleResult:
        return self._result()


def build_dialogue_bundles(
    messages: Iterable[Any],
    *,
    fragments: Optional[Iterable[Any]] = None,
    claims: Optional[Iterable[Any]] = None,
    registry: Optional[MessageRegistry] = None,
    gate: Optional[SemanticGate] = None,
    window_size: int = 8,
    time_window_seconds: float = 15 * 60,
    max_candidates: int = 3,
) -> DialogueBundleResult:
    """One-shot public API for the registry → gate → bundle path."""

    builder = DialogueBundleBuilder(
        registry=registry,
        gate=gate,
        window_size=window_size,
        time_window_seconds=time_window_seconds,
        max_candidates=max_candidates,
    )
    return builder.ingest(messages, fragments=fragments, claims=claims)


# Short aliases make the boundary convenient for adapters while retaining
# explicit names in the serialized contract.
FragmentCandidate = BundleFragment
ClaimCandidate = BundleClaim
DialogueBundleSnapshot = DialogueBundleResult


__all__ = [
    "SCALE_MICRO",
    "SCALE_TURN",
    "SCALE_LOCAL",
    "SCALE_SESSION",
    "SCALE_SPARSE",
    "SCALE_COLD",
    "BUNDLE_SCALES",
    "WINDOW_W0",
    "WINDOW_W1",
    "WINDOW_W2",
    "WINDOW_W3",
    "WINDOW_W4",
    "WINDOW_SCALE_BY_BUNDLE_SCALE",
    "REL_CONTINUES",
    "REL_ELABORATES",
    "REL_ANSWERS",
    "REL_CONTRASTS",
    "REL_TOPIC_SHIFT",
    "REL_POSSIBLY_RELATED",
    "REL_INSUFFICIENT",
    "RELATION_LABELS",
    "BundleFragment",
    "FragmentCandidate",
    "BundleClaim",
    "ClaimCandidate",
    "ContextRelationCandidate",
    "OpenContextSnapshot",
    "DialogueBundle",
    "DialogueBundleResult",
    "DialogueBundleSnapshot",
    "DialogueBundleBuilder",
    "build_dialogue_bundles",
]
