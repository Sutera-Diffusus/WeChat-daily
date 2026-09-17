"""Reversible, local ContextPacket v1 construction.

This module is the boundary immediately above the registry, gate and dialogue
bundle layers.  It prepares provider-ready context without making an event,
topic, resolution or same-event decision.  Every semantic-looking item in the
packet is explicitly marked as a candidate and carries source/evidence refs.

The builder intentionally works from the public ``MessageRegistry`` envelope
and ``DialogueBundleResult``.  It never reads arbitrary/private message keys;
the registry performs that projection before this module sees a message.
Unknown scope is fail-closed for cross-message candidates.  Time and segment
membership are retained as weak context reasons only and cannot create a
candidate relation by themselves.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .dialogue_bundle import (
    BUNDLE_PIPELINE_VERSION,
    BUNDLE_RULESET_VERSION,
    BundleClaim,
    BundleFragment,
    ContextRelationCandidate,
    DialogueBundle,
    DialogueBundleResult,
    build_dialogue_bundles,
)
from .dialogue_segments import has_context_prefix, is_context_only_text
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
    SCHEMA_VERSION,
    UNKNOWN,
    MessageRegistry,
    RegisteredMessage,
    stable_hash,
)


CONTEXT_PACKET_VERSION = "context_packet_v1"
CONTEXT_PACKET_SCHEMA_VERSION = CONTEXT_PACKET_VERSION
CONTEXT_PACKET_PIPELINE_VERSION = "workstream_k2_context_packet_v1"
CONTEXT_PACKET_RULESET_VERSION = "context_packet_rules_v1"
FIXED_PART_VERSION = "context_packet_fixed_v1"
DYNAMIC_PART_VERSION = "context_packet_dynamic_v1"

_CHANNELS = frozenset(
    {
        CHANNEL_IMMEDIATE,
        CHANNEL_PENDING_CONTEXT,
        CHANNEL_BACKGROUND,
        CHANNEL_COLD_RECOVERABLE,
    }
)
_SOCIAL_ROLES = frozenset({"conversation_opener", "context_only"})
_SILENT_TYPES = frozenset({"image", "video", "audio", "file", "sticker", "emoji", "system", "location"})
_CONTEXT_FRAGMENT_TYPES = frozenset({"conversation_opener", "acknowledgement", "reaction", "context", "media"})
_KNOWN_RESOLUTIONS = frozenset({"explicit", "inherited"})
_KNOWN_STATES = frozenset({"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"})
_TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    result = str(value).strip()
    return result if result else default


def _known(value: Any) -> bool:
    return value not in (None, "", UNKNOWN, "UNKNOWN")


def _tuple_text(value: Any) -> Tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),)
    try:
        return tuple(str(item) for item in value if item not in (None, ""))
    except TypeError:
        return (str(value),)


def _scope(item: Any) -> Optional[Tuple[str, str]]:
    account_id = _text(_value(item, "account_id"), UNKNOWN)
    chat_id = _text(_value(item, "chat_id"), UNKNOWN)
    if not _known(account_id) or not _known(chat_id):
        return None
    return account_id, chat_id


def _same_scope(left: Any, right: Any) -> bool:
    left_scope, right_scope = _scope(left), _scope(right)
    if left_scope is not None and right_scope is not None:
        return left_scope == right_scope
    # Without a known account+chat pair, only fragments from the exact source
    # message may share a packet.  Unknown scope must never bridge messages.
    return _text(_value(left, "message_id"), UNKNOWN) == _text(_value(right, "message_id"), UNKNOWN)


def _fragment_is_context_only(
    fragment: BundleFragment,
    *,
    entry: Optional[RegisteredMessage] = None,
) -> bool:
    """Return whether a fragment is a context-only provider candidate.

    Role assignment is upstream and deterministic.  This final packet guard
    is intentionally narrow: explicit social/media roles and pure lexical
    acknowledgements are context, while a substantive mixed turn remains
    eligible even when it contains a greeting/confirmation prefix.  The
    result is a provider-layer classification only; ContextPacket keeps the
    complete source fragment set in ``primary_fragments`` for local recovery.
    Missing text with unknown type is left eligible (conservative unknown
    handling).
    """

    message_type = "text"
    if entry is not None:
        message_type = entry.metadata.message_type
    elif _value(fragment, "message_type") is not None:
        message_type = _value(fragment, "message_type")
    text = _text(_value(fragment, "text"), "")
    pure_context = is_context_only_text(text, message_type=message_type)
    role = _text(_value(fragment, "role"), "").casefold()
    fragment_type = _text(_value(fragment, "fragment_type"), "").casefold()
    if pure_context:
        return True
    # A typed media placeholder is context, but unknown empty text is not.
    if fragment_type in _CONTEXT_FRAGMENT_TYPES:
        if fragment_type != "media" and text and has_context_prefix(text):
            return False
        if fragment_type == "media" and text and not is_context_only_text(text, message_type=message_type):
            return bool(str(message_type).casefold() in _SILENT_TYPES)
        return True
    if role in _SOCIAL_ROLES or bool(_value(fragment, "is_opener", False)):
        # Explicit role metadata is authoritative for a non-social context
        # note.  A mixed social prefix with a real topic is substantive.
        if role in _SOCIAL_ROLES and text and has_context_prefix(text):
            return False
        return True
    if bool(_value(fragment, "is_silent", False)) and str(message_type).casefold() in _SILENT_TYPES:
        return True
    return False


def _number(value: Any) -> Optional[float]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sequence(item: Any) -> Optional[int]:
    value = _value(item, "sequence_in_chat")
    if value is None:
        value = _value(item, "sequence")
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _time(item: Any) -> Optional[float]:
    value = _value(item, "time_offset_seconds")
    if value is None:
        value = _value(item, "time_offset")
    return _number(value)


def _span(value: Any, fallback: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    if isinstance(value, Mapping):
        if "span" in value:
            value = value.get("span")
        if isinstance(value, Mapping):
            start, end = value.get("start", value.get("span_start")), value.get("end", value.get("span_end"))
        else:
            start = value.get("span_start") if isinstance(value, Mapping) else None
            end = value.get("span_end") if isinstance(value, Mapping) else None
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start, end = value[0], value[1]
    else:
        return fallback
    try:
        start_i, end_i = int(start), int(end)
    except (TypeError, ValueError):
        return fallback
    return (start_i, end_i) if end_i >= start_i else fallback


def _copy_dict(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return deepcopy({str(key): item for key, item in value.items()})


def _copy_tuple_dicts(values: Any) -> Tuple[Dict[str, Any], ...]:
    if values is None or isinstance(values, (str, bytes)):
        return ()
    try:
        result = []
        for item in values:
            if isinstance(item, Mapping):
                result.append(_copy_dict(item))
        return tuple(result)
    except TypeError:
        return ()


def _stable_id(prefix: str, value: Any) -> str:
    return "%s_%s" % (prefix, stable_hash(value)[:20])


def _message_id(item: Any) -> str:
    return _text(_value(item, "message_id"), UNKNOWN)


def _fragment_evidence_ref(fragment: Any) -> Dict[str, Any]:
    return {
        "type": "fragment",
        "id": _text(_value(fragment, "fragment_id"), UNKNOWN),
        "message_id": _message_id(fragment),
        "span": {
            "start": int(_value(fragment, "span_start", 0) or 0),
            "end": int(_value(fragment, "span_end", 0) or 0),
        },
    }


def _message_source_ref(entry: RegisteredMessage) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": entry.message_id,
        "registry_key": entry.registry_key,
        "raw_message_ref": entry.raw_message_ref.to_dict(),
        "content_hash": entry.content_hash,
        "metadata_hash": entry.metadata_hash,
        "record_hash": entry.record_hash,
    }


def _claim_evidence_ref(claim: Any) -> Dict[str, Any]:
    span = _span(_value(claim, "evidence_span"), (0, 0))
    return {
        "type": "claim",
        "id": _text(_value(claim, "claim_id"), UNKNOWN),
        "fragment_id": _text(_value(claim, "fragment_id"), UNKNOWN),
        "message_id": _message_id(claim),
        "span": {"start": span[0], "end": span[1]},
    }


def _fragment_projection(fragment: BundleFragment) -> Dict[str, Any]:
    """Project a fragment without exposing arbitrary input keys."""

    text = _text(_value(fragment, "text"))
    start = int(_value(fragment, "span_start", 0) or 0)
    end = int(_value(fragment, "span_end", len(text)) or len(text))
    return {
        "fragment_id": _text(_value(fragment, "fragment_id"), UNKNOWN),
        "message_id": _message_id(fragment),
        "account_id": _text(_value(fragment, "account_id"), UNKNOWN),
        "chat_id": _text(_value(fragment, "chat_id"), UNKNOWN),
        "segment_id": _value(fragment, "segment_id"),
        "text_redacted": text,
        "span": {"start": start, "end": end},
        "role": _text(_value(fragment, "role"), "substantive"),
        "fragment_type": _text(_value(fragment, "fragment_type"), "unknown"),
        "speaker_id": _text(_value(fragment, "speaker_id"), UNKNOWN),
        "mentioned_person_ids": list(_tuple_text(_value(fragment, "mentioned_person_ids"))),
        "subject_id": _text(_value(fragment, "subject_id"), UNKNOWN),
        "subject_type": _text(_value(fragment, "subject_type"), "unknown"),
        "object_id": _text(_value(fragment, "object_id"), UNKNOWN),
        "object_resolution": _text(_value(fragment, "object_resolution"), UNKNOWN),
        "object_inherited_from_id": _value(fragment, "object_inherited_from_id"),
        "state_candidate": _text(_value(fragment, "state"), UNKNOWN),
        "state_evidence": _text(_value(fragment, "state_evidence"), UNKNOWN),
        "modality": _text(_value(fragment, "modality"), UNKNOWN),
        "intent_candidate": _text(_value(fragment, "intent"), "statement"),
        "claim_role_candidate": _text(_value(fragment, "claim_role"), "fact"),
        "actions_candidate": list(_tuple_text(_value(fragment, "actions"))),
        "temporal_qualifier": _text(_value(fragment, "temporal_qualifier"), UNKNOWN),
        "reply_to_message_id": _value(fragment, "reply_to_message_id"),
        "topic_bearing": _fragment_topic_bearing(fragment),
        "is_opener": bool(_value(fragment, "is_opener", False)),
        "is_silent": bool(_value(fragment, "is_silent", False)),
        "information_value": _text(_value(fragment, "information_value"), "unknown"),
        "evidence_refs": _copy_tuple_dicts(_value(fragment, "evidence_refs")),
        "source": _text(_value(fragment, "source"), "workstream_a"),
        "candidate_only": True,
    }


def _authoritative_fact(entry: RegisteredMessage) -> Dict[str, Any]:
    metadata = entry.metadata
    return {
        "message_id": entry.message_id,
        "registry_key": entry.registry_key,
        "account_id": metadata.account_id,
        "chat_id": metadata.chat_id,
        "speaker_id": metadata.speaker_id,
        "direction": metadata.direction,
        "message_type": metadata.message_type,
        "sequence_in_chat": metadata.sequence_in_chat,
        "timestamp": metadata.timestamp,
        "time_offset_seconds": metadata.time_offset_seconds,
        "reply_to_message_id": metadata.reply_to_message_id,
        "dialogue_segment_id": metadata.dialogue_segment_id,
        "dialogue_role": metadata.dialogue_role,
        "source_mode": metadata.source_mode,
        "source_snapshot_fingerprint": metadata.source_snapshot_fingerprint,
        "metadata_revision": metadata.metadata_revision,
        "metadata_authoritative": True,
        "content_hash": entry.content_hash,
        "metadata_hash": entry.metadata_hash,
        "record_hash": entry.record_hash,
        "raw_message_ref": entry.raw_message_ref.to_dict(),
    }


def _relation_dict(relation: ContextRelationCandidate, left: BundleFragment, right: BundleFragment) -> Dict[str, Any]:
    supporting = [str(item) for item in relation.supporting_slot_codes if str(item) not in {"time_proximity_weak", "same_segment_weak"}]
    reasons = list(dict.fromkeys(supporting))
    # The lower bundle layer deliberately requires a stronger threshold before
    # labelling an ``answers`` edge.  This layer only prepares a reversible
    # candidate, so a question/request followed by an answer-like turn with an
    # independent semantic signal is retained as QA candidate evidence.
    left_intent = _text(_value(left, "intent"), "statement")
    right_intent = _text(_value(right, "intent"), "statement")
    right_type = _text(_value(right, "fragment_type"), "unknown")
    qa_candidate = (
        left_intent in {"question", "request"}
        and (right_intent in {"answer", "statement", "request"} or right_type == "answer")
        and bool(supporting)
    )
    relation_label = "answers" if qa_candidate else relation.label
    relation_subtype = "question_answer_candidate" if qa_candidate else relation.subtype
    if qa_candidate and "answer_cue" not in reasons:
        reasons.append("answer_cue")
    if qa_candidate and "question_or_request_then_turn" not in reasons:
        reasons.append("question_or_request_then_turn")
    if _value(left, "segment_id") is not None and _value(left, "segment_id") == _value(right, "segment_id"):
        reasons.append("same_segment_weak")
    if relation.time_distance_seconds is not None:
        reasons.append("time_proximity_weak")
    return {
        "candidate_id": relation.context_relation_id,
        "left_fragment_id": relation.left_anchor_id,
        "right_fragment_id": relation.right_anchor_id,
        "left_message_id": _message_id(left),
        "right_message_id": _message_id(right),
        "relation_label": relation_label,
        "relation_subtype": relation_subtype,
        "supporting_slot_codes": reasons,
        "semantic_support": supporting,
        "evidence_strength_candidate": relation.evidence_strength,
        "explicit_reply_present": bool(relation.explicit_reply_present),
        "time_distance_seconds": relation.time_distance_seconds,
        "time_is_weak_only": True,
        "same_segment_is_weak_only": True,
        "evidence_refs": _copy_tuple_dicts(relation.evidence_refs),
        "source_refs": (
            {"type": "message", "id": _message_id(left)},
            {"type": "message", "id": _message_id(right)},
        ),
        "uncertainties": list(relation.conflicting_slot_codes),
        "candidate_only": True,
    }


def _pair_signals(left: BundleFragment, right: BundleFragment) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return semantic candidate signals and conflicts for a local pair."""

    if not _same_scope(left, right):
        return (), ("scope_conflict",)
    if (
        bool(_value(left, "is_silent", False))
        or bool(_value(right, "is_silent", False))
        or bool(_value(left, "is_opener", False))
        or bool(_value(right, "is_opener", False))
        or _text(_value(left, "role")) in _SOCIAL_ROLES
        or _text(_value(right, "role")) in _SOCIAL_ROLES
    ):
        return (), ("social_or_silent_context",)

    signals: List[str] = []
    conflicts: List[str] = []
    left_object = _text(_value(left, "object_id"), UNKNOWN)
    right_object = _text(_value(right, "object_id"), UNKNOWN)
    left_resolution = _text(_value(left, "object_resolution"), UNKNOWN)
    right_resolution = _text(_value(right, "object_resolution"), UNKNOWN)
    left_object_known = _known(left_object) and left_resolution in _KNOWN_RESOLUTIONS
    right_object_known = _known(right_object) and right_resolution in _KNOWN_RESOLUTIONS
    if left_object_known and right_object_known and left_object != right_object:
        conflicts.append("object_conflict")
    if left_object_known and right_object_known and left_object == right_object:
        signals.append("shared_object")
    inherited_from = _value(right, "object_inherited_from_id")
    if right_resolution == "inherited" and inherited_from == _value(left, "fragment_id"):
        signals.append("object_inheritance")

    left_subject = _text(_value(left, "subject_id"), UNKNOWN)
    right_subject = _text(_value(right, "subject_id"), UNKNOWN)
    if _known(left_subject) and _known(right_subject):
        if left_subject == right_subject:
            signals.append("shared_subject")
        else:
            conflicts.append("subject_conflict")
    left_people = set(_tuple_text(_value(left, "mentioned_person_ids")))
    right_people = set(_tuple_text(_value(right, "mentioned_person_ids")))
    if left_people & right_people:
        signals.append("shared_mentioned_person")
    left_actions = set(_tuple_text(_value(left, "actions")))
    right_actions = set(_tuple_text(_value(right, "actions")))
    if left_actions & right_actions:
        signals.append("shared_action")

    left_state = _text(_value(left, "state"), UNKNOWN)
    right_state = _text(_value(right, "state"), UNKNOWN)
    known_states = _KNOWN_STATES - {UNKNOWN}
    if left_state in known_states and right_state in known_states:
        if left_state == right_state:
            signals.append("shared_state")
        elif left_object_known and right_object_known and left_object == right_object:
            signals.append("state_change")
    left_message_id, right_reply = _message_id(left), _value(right, "reply_to_message_id")
    if _known(right_reply) and str(right_reply) == left_message_id:
        signals.append("explicit_reply")

    left_intent = _text(_value(left, "intent"), "statement")
    right_intent = _text(_value(right, "intent"), "statement")
    right_type = _text(_value(right, "fragment_type"), "unknown")
    if left_intent in {"question", "request"} and right_intent in {"answer", "statement", "request"}:
        signals.append("question_or_request_then_turn")
    if right_type == "answer" or right_intent == "answer":
        signals.append("answer_cue")

    # Conflicting explicit objects/subjects block an inferred candidate.  An
    # explicit reply still remains a candidate because the reply edge itself
    # is source metadata, but the conflict is retained for downstream review.
    semantic = tuple(dict.fromkeys(signals))
    if conflicts and "explicit_reply" not in semantic:
        return (), tuple(dict.fromkeys(conflicts))
    return semantic, tuple(dict.fromkeys(conflicts))


def _derived_candidate(left: BundleFragment, right: BundleFragment) -> Optional[Dict[str, Any]]:
    signals, conflicts = _pair_signals(left, right)
    semantic = [item for item in signals if item not in {"question_or_request_then_turn", "answer_cue"}]
    if not semantic:
        return None
    if "explicit_reply" in signals or (
        "question_or_request_then_turn" in signals
        and ("answer_cue" in signals or semantic)
    ):
        label, subtype = "answers", "question_answer_candidate"
    elif "state_change" in signals:
        label, subtype = "continues", "state_update_candidate"
    elif "object_inheritance" in signals:
        label, subtype = "elaborates", "object_inheritance_candidate"
    elif "shared_object" in signals:
        label, subtype = "possibly_related", "shared_object_candidate"
    else:
        label, subtype = "elaborates", "person_or_action_history_candidate"
    left_ref, right_ref = _fragment_evidence_ref(left), _fragment_evidence_ref(right)
    distance = None
    left_time, right_time = _time(left), _time(right)
    if left_time is not None and right_time is not None:
        distance = abs(right_time - left_time)
    reasons = list(signals)
    if _value(left, "segment_id") is not None and _value(left, "segment_id") == _value(right, "segment_id"):
        reasons.append("same_segment_weak")
    if distance is not None:
        reasons.append("time_proximity_weak")
    return {
        "candidate_id": _stable_id(
            "PACKET_CANDIDATE",
            {"left": _value(left, "fragment_id"), "right": _value(right, "fragment_id"), "label": label, "signals": semantic},
        ),
        "left_fragment_id": _value(left, "fragment_id"),
        "right_fragment_id": _value(right, "fragment_id"),
        "left_message_id": _message_id(left),
        "right_message_id": _message_id(right),
        "relation_label": label,
        "relation_subtype": subtype,
        "supporting_slot_codes": list(dict.fromkeys(reasons)),
        "semantic_support": list(dict.fromkeys(semantic)),
        "evidence_strength_candidate": "candidate",
        "explicit_reply_present": "explicit_reply" in signals,
        "time_distance_seconds": distance,
        "time_is_weak_only": True,
        "same_segment_is_weak_only": True,
        "evidence_refs": [left_ref, right_ref],
        "source_refs": [
            {"type": "message", "id": _message_id(left)},
            {"type": "message", "id": _message_id(right)},
        ],
        "uncertainties": list(conflicts),
        "candidate_only": True,
    }


def _candidate_kind(candidate: Mapping[str, Any]) -> str:
    label = _text(candidate.get("relation_label"), "")
    subtype = _text(candidate.get("relation_subtype"), "")
    support = set(_tuple_text(candidate.get("semantic_support")))
    if label == "answers" or "explicit_reply" in support or "answer_cue" in support:
        return "qa"
    if "state_change" in support or "shared_state" in support or "state_update" in subtype:
        return "state"
    if "shared_object" in support or "object_inheritance" in support or "object" in subtype:
        return "object"
    if {"shared_subject", "shared_mentioned_person"} & support or "person" in subtype:
        return "person"
    return "person" if {"shared_action", "shared_subject"} & support else "object"


def _candidate_projection(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    return _copy_dict(candidate)


# The packet builder deliberately keeps ordinary relation rows as candidates.
# These helpers are the narrow bridge for structure that has already been
# computed by the fragment/bundle layers.  They do not infer a topic or answer
# from text, time, or segment membership.  A row is promoted only when the
# endpoint fragments carry the corresponding typed fields and the relation
# itself carries the matching semantic support.
_PERSON_TYPED_SUPPORT = frozenset({"shared_subject", "shared_mentioned_person"})
_OBJECT_TYPED_SUPPORT = frozenset({"shared_object", "object_inheritance"})
_STATE_TYPED_SUPPORT = frozenset({"shared_state", "state_change"})
_CONTINUATION_SIGNAL_FAMILIES = {
    "shared_object": "object",
    "object_inheritance": "object",
    "shared_action": "action",
    "shared_state": "state",
    "state_change": "state",
    "question_or_request_then_turn": "question_answer",
    "answer_cue": "question_answer",
}
_COMPETITION_SUPPORT = frozenset(
    {
        "candidate_competition",
        "competition",
        "competing_candidates",
        "mutually_exclusive",
        "exclusive_candidates",
        "alternative_candidates",
        "candidate_conflict",
        "candidate_contradiction",
        "object_conflict",
        "subject_conflict",
        "contrast",
        "contradiction",
    }
)


def _scope_payload(item: Any) -> Dict[str, str]:
    scope = _scope(item)
    if scope is None:
        return {"account_id": UNKNOWN, "chat_id": UNKNOWN}
    return {"account_id": scope[0], "chat_id": scope[1]}


def _fragment_topic_bearing(fragment: Any) -> bool:
    """Use typed fragment metadata to decide whether an endpoint is topical.

    ``dialogue_segments`` may provide an explicit marker when a producer has
    it.  The fallback is intentionally structural: a known object/person/
    state/action or question/answer intent is sufficient; role/context/media
    markers and body text alone are not.
    """

    explicit = _value(fragment, "topic_bearing")
    if explicit is None:
        explicit = _value(fragment, "is_topic_bearing")
    if explicit is None:
        explicit = _value(fragment, "dialogue_topic_bearing")
    if explicit is not None:
        return bool(explicit)
    role = _text(_value(fragment, "role"), "").casefold()
    fragment_type = _text(_value(fragment, "fragment_type"), "").casefold()
    if (
        bool(_value(fragment, "is_silent", False))
        or bool(_value(fragment, "is_opener", False))
        or role in _SOCIAL_ROLES
        or fragment_type in _CONTEXT_FRAGMENT_TYPES
    ):
        return False
    object_id = _text(_value(fragment, "object_id"), UNKNOWN)
    object_resolution = _text(_value(fragment, "object_resolution"), UNKNOWN)
    person_ids = set(_tuple_text(_value(fragment, "mentioned_person_ids")))
    subject_id = _text(_value(fragment, "subject_id"), UNKNOWN)
    state = _text(_value(fragment, "state"), UNKNOWN)
    intent = _text(_value(fragment, "intent"), "statement").casefold()
    actions = _tuple_text(_value(fragment, "actions"))
    return bool(
        (_known(object_id) and object_resolution in _KNOWN_RESOLUTIONS)
        or _known(subject_id)
        or person_ids
        or state in (_KNOWN_STATES - {UNKNOWN})
        or actions
        or intent in {"question", "request", "answer"}
        or fragment_type in {"question", "request", "answer"}
    )


def _typed_fragment_fields(fragment: Any, kind: str) -> Tuple[str, ...]:
    """Return the typed fields that make ``fragment`` auditable for ``kind``."""

    fields: List[str] = []
    subject = _text(_value(fragment, "subject_id"), UNKNOWN)
    people = _tuple_text(_value(fragment, "mentioned_person_ids"))
    object_id = _text(_value(fragment, "object_id"), UNKNOWN)
    object_resolution = _text(_value(fragment, "object_resolution"), UNKNOWN)
    state = _text(_value(fragment, "state"), UNKNOWN)
    state_evidence = _text(_value(fragment, "state_evidence"), UNKNOWN)
    if kind == "person":
        if _known(subject):
            fields.append("subject_id")
        if people:
            fields.append("mentioned_person_ids")
        # A person row must carry a typed person and an auditable location;
        # callers add the fragment ref separately.
    elif kind == "object":
        if _known(object_id) and object_resolution in _KNOWN_RESOLUTIONS:
            fields.extend(("object_id", "object_resolution"))
        if _copy_tuple_dicts(_value(fragment, "object_evidence_refs")):
            fields.append("object_evidence_refs")
    elif kind == "state":
        if state in (_KNOWN_STATES - {UNKNOWN}):
            fields.append("state")
        if _known(state_evidence):
            fields.append("state_evidence")
    return tuple(dict.fromkeys(fields))


def _typed_fragment_evidence(fragment: Any, kind: str) -> Tuple[str, ...]:
    fields = _typed_fragment_fields(fragment, kind)
    # Typed values alone are not enough for the sidecar; the endpoint's own
    # span is the scoped evidence location.  A supplied object evidence ref is
    # retained as an additional typed anchor where available.
    if not fields:
        return ()
    return tuple(dict.fromkeys((str(_value(fragment, "fragment_id", UNKNOWN)),)))


def _scoped_relation_ref(
    prefix: str,
    left: Any,
    right: Any,
    *,
    relation_id: Any,
    kind: str,
    fields: Sequence[str] = (),
) -> Dict[str, Any]:
    left_ref = _fragment_evidence_ref(left)
    right_ref = _fragment_evidence_ref(right)
    value = {
        "type": prefix,
        "id": _stable_id(prefix.upper(), {"relation_id": relation_id, "kind": kind}),
        "relation_id": str(relation_id),
        "scope": _scope_payload(left),
        "message_refs": [_message_id(left), _message_id(right)],
        "fragment_refs": [left_ref, right_ref],
    }
    if fields:
        value["typed_fields"] = list(dict.fromkeys(str(item) for item in fields))
    return value


def _materialize_typed_history(
    candidate: Mapping[str, Any],
    left: Any,
    right: Any,
    *,
    kind: str,
) -> Optional[Dict[str, Any]]:
    support = set(_tuple_text(candidate.get("semantic_support")))
    required_support = {
        "person": _PERSON_TYPED_SUPPORT,
        "object": _OBJECT_TYPED_SUPPORT,
        "state": _STATE_TYPED_SUPPORT,
    }.get(kind, frozenset())
    if not required_support or not (support & required_support):
        return None
    if not _same_scope(left, right):
        return None
    left_fields = _typed_fragment_fields(left, kind)
    right_fields = _typed_fragment_fields(right, kind)
    if not left_fields or not right_fields:
        return None
    relation_id = candidate.get("candidate_id", UNKNOWN)
    relation_ref = _scoped_relation_ref(
        "typed_history_relation",
        left,
        right,
        relation_id=relation_id,
        kind=kind,
        fields=tuple(dict.fromkeys((*left_fields, *right_fields))),
    )
    projected = _candidate_projection(candidate)
    candidate_ref = _text(candidate.get("candidate_id"), UNKNOWN)
    evidence = [
        _fragment_evidence_ref(left),
        _fragment_evidence_ref(right),
    ]
    projected.update(
        {
            "candidate_ref": candidate_ref,
            "candidate_refs": [candidate_ref],
            "scoped_evidence_ref": relation_ref,
            "scoped_evidence_refs": [relation_ref],
            "typed_evidence_fields": list(dict.fromkeys((*left_fields, *right_fields))),
            "typed_evidence_fragment_refs": [
                _text(_value(left, "fragment_id"), UNKNOWN),
                _text(_value(right, "fragment_id"), UNKNOWN),
            ],
            # ``candidate_only`` is deliberately retained: this is a strong
            # local structure contract, not a final semantic decision.
            "materialized_relation": True,
            "strong_relation": True,
            "metadata_evidence_strength": "strong",
            "evidence_refs": evidence,
            "account_id": _text(_value(left, "account_id"), UNKNOWN),
            "chat_id": _text(_value(left, "chat_id"), UNKNOWN),
        }
    )
    return projected


def _materialize_greeting_boundaries(
    ordered: Sequence[Any],
    context_ids: Set[str],
) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    candidates = [item for item in ordered if str(_value(item, "fragment_id")) in context_ids]
    for index, opener in enumerate(candidates):
        role = _text(_value(opener, "role"), "").casefold()
        fragment_type = _text(_value(opener, "fragment_type"), "").casefold()
        if not (
            bool(_value(opener, "is_opener", False))
            or role == "conversation_opener"
            or (role == "context_only" and _text(_value(opener, "intent"), "").casefold() == "greeting")
            or fragment_type in {"conversation_opener", "greeting"}
            or _text(_value(opener, "intent"), "").casefold() == "greeting"
        ):
            continue
        opener_message = _message_id(opener)
        for followup in candidates[index + 1 :]:
            if not _same_scope(opener, followup):
                continue
            followup_id = _text(_value(followup, "fragment_id"), UNKNOWN)
            if followup_id == _text(_value(opener, "fragment_id"), UNKNOWN):
                continue
            if not _fragment_topic_bearing(followup):
                continue
            followup_message = _message_id(followup)
            segment_left = _value(opener, "segment_id")
            segment_right = _value(followup, "segment_id")
            if segment_left not in (None, "") and segment_right not in (None, "") and segment_left != segment_right:
                boundary_reason = "dialogue_segment_boundary"
            elif _value(followup, "context_message_ids") and opener_message in _tuple_text(_value(followup, "context_message_ids")):
                boundary_reason = "explicit_context_continuation"
            else:
                # A typed role transition (opener -> topic-bearing fragment)
                # is the minimum same-segment continuation evidence.
                boundary_reason = "opener_to_substantive_continuation"
            relation_id = _stable_id(
                "GREETING_BOUNDARY",
                {"opener": _value(opener, "fragment_id"), "followup": followup_id, "reason": boundary_reason},
            )
            relation_ref = _scoped_relation_ref(
                "greeting_topic_boundary",
                opener,
                followup,
                relation_id=relation_id,
                kind="greeting_new_topic",
                fields=("is_opener", "topic_bearing", "role_transition"),
            )
            rows.append(
                {
                    "message_ref": opener_message,
                    "message_refs": [opener_message, followup_message],
                    "endpoint_message_refs": [opener_message, followup_message],
                    "opener_message_ref": opener_message,
                    "topic_bearing_followup_message_ref": followup_message,
                    "topic_bearing_followup_message_refs": [followup_message],
                    "is_opener_or_greeting": True,
                    "topic_bearing_followup": True,
                    "boundary_reason": boundary_reason,
                    "evidence_type": "greeting_new_topic",
                    "scoped_evidence_ref": relation_ref,
                    "scoped_evidence_refs": [relation_ref],
                    "evidence_refs": [_fragment_evidence_ref(opener), _fragment_evidence_ref(followup)],
                    "strong_relation": True,
                    "materialized_relation": True,
                    "account_id": _text(_value(opener, "account_id"), UNKNOWN),
                    "chat_id": _text(_value(opener, "chat_id"), UNKNOWN),
                }
            )
            # The first typed continuation is the deterministic boundary for
            # this opener; later topic-bearing messages belong to its packet
            # but do not multiply the canonical evidence.
            break
    return tuple(rows)


def _materialize_topic_transitions(
    candidates: Sequence[Mapping[str, Any]],
    fragments: Mapping[str, Any],
) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        support = set(_tuple_text(candidate.get("semantic_support")))
        if not ({"explicit_topic_shift", "topic_boundary", "topic_transition"} & support):
            relation_label = _text(candidate.get("relation_label"), "").casefold()
            relation_subtype = _text(candidate.get("relation_subtype"), "").casefold()
            if relation_label not in {"topic_shift", "topic_change", "topic_boundary"} and relation_subtype not in {"topic_shift", "topic_change", "topic_boundary"}:
                continue
        left = fragments.get(str(candidate.get("left_fragment_id")))
        right = fragments.get(str(candidate.get("right_fragment_id")))
        if left is None or right is None or not _same_scope(left, right):
            continue
        if not (_fragment_topic_bearing(left) and _fragment_topic_bearing(right)):
            continue
        evidence_refs = _copy_tuple_dicts(candidate.get("evidence_refs"))
        if not evidence_refs:
            continue
        relation_id = _text(candidate.get("candidate_id"), UNKNOWN)
        reason = "explicit_topic_shift" if "explicit_topic_shift" in support else "explicit_topic_boundary"
        relation_ref = _scoped_relation_ref(
            "topic_transition",
            left,
            right,
            relation_id=relation_id,
            kind="topic_shift",
            fields=("topic_bearing_left", "topic_bearing_right", reason),
        )
        rows.append(
            {
                "topic_shift_or_boundary": True,
                "topic_shift": True,
                "transition_type": "topic_shift",
                "boundary_reason": reason,
                "endpoint_message_refs": [_message_id(left), _message_id(right)],
                "message_refs": [_message_id(left), _message_id(right)],
                "candidate_ref": relation_id,
                "candidate_refs": [relation_id],
                "scoped_evidence_ref": relation_ref,
                "scoped_evidence_refs": [relation_ref],
                "evidence_refs": evidence_refs,
                "strong_relation": True,
                "materialized_relation": True,
                "account_id": _text(_value(left, "account_id"), UNKNOWN),
                "chat_id": _text(_value(left, "chat_id"), UNKNOWN),
            }
        )
    # A relation can appear in both the result and derived candidate pass.
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        unique[stable_hash(row)] = row
    return tuple(unique.values())


def _materialize_competition(
    candidates: Sequence[Mapping[str, Any]],
    fragments: Mapping[str, Any],
) -> Tuple[Dict[str, Any], ...]:
    marked: List[Tuple[Mapping[str, Any], Any, Any, Set[str]]] = []
    for candidate in candidates:
        support = set(_tuple_text(candidate.get("semantic_support"))) | set(_tuple_text(candidate.get("supporting_slot_codes")))
        explicit = any(
            bool(candidate.get(key))
            for key in ("explicit_competition_relation", "candidate_competition", "mutually_exclusive", "exclusive", "competing")
        )
        label = _text(candidate.get("relation_label"), "").casefold()
        subtype = _text(candidate.get("relation_subtype"), "").casefold()
        if not explicit and not (support & _COMPETITION_SUPPORT) and label not in {"contrast", "competes", "competition"} and subtype not in {"contrast", "competition", "candidate_competition"}:
            continue
        left = fragments.get(str(candidate.get("left_fragment_id")))
        right = fragments.get(str(candidate.get("right_fragment_id")))
        if left is None or right is None or not _same_scope(left, right):
            continue
        evidence = _copy_tuple_dicts(candidate.get("evidence_refs"))
        candidate_ref = _text(candidate.get("candidate_id"), UNKNOWN)
        grounded_pairs = (
            (_text(_value(left, "object_id"), UNKNOWN), _text(_value(left, "object_resolution"), UNKNOWN)),
            (_text(_value(right, "object_id"), UNKNOWN), _text(_value(right, "object_resolution"), UNKNOWN)),
        )
        grounded = {
            object_id
            for object_id, resolution in grounded_pairs
            if _known(object_id) and resolution in _KNOWN_RESOLUTIONS
        }
        if not evidence or not _known(candidate_ref) or not grounded:
            continue
        marked.append((candidate, left, right, grounded))
    # Competition is a relation between alternatives, not a count of arbitrary
    # rows.  Require at least two distinct candidate and grounded object refs,
    # and preserve the conflict/competition support that justified the group.
    candidate_refs = {_text(item[0].get("candidate_id"), UNKNOWN) for item in marked}
    candidate_refs.discard(UNKNOWN)
    grounded_refs = set().union(*(item[3] for item in marked)) if marked else set()
    if len(candidate_refs) < 2 or len(grounded_refs) < 2:
        return ()
    message_refs = list(
        dict.fromkeys(
            [_message_id(item[1]) for item in marked]
            + [_message_id(item[2]) for item in marked]
        )
    )
    evidence_refs = list({stable_hash(ref): ref for item in marked for ref in _copy_tuple_dicts(item[0].get("evidence_refs"))}.values())
    first_left, first_right = marked[0][1], marked[0][2]
    relation_ref = _scoped_relation_ref(
        "candidate_competition",
        first_left,
        first_right,
        relation_id=_stable_id("COMPETITION", sorted(candidate_refs)),
        kind="candidate_competition",
        fields=("grounded_candidate_refs", "conflict_or_competition_support"),
    )
    return (
        {
            "explicit_competition_relation": True,
            "candidate_competition": True,
            "competing_candidate_refs": sorted(candidate_refs),
            "candidate_refs": sorted(candidate_refs),
            "grounded_object_refs": sorted(grounded_refs),
            "grounded_candidate_refs": sorted(candidate_refs),
            "conflict_or_competition_support": sorted(
                set().union(*(set(_tuple_text(item[0].get("semantic_support"))) | set(_tuple_text(item[0].get("supporting_slot_codes"))) for item in marked))
                & _COMPETITION_SUPPORT
            ),
            "message_refs": message_refs,
            "endpoint_message_refs": message_refs,
            "scoped_evidence_ref": relation_ref,
            "scoped_evidence_refs": [relation_ref],
            "evidence_refs": evidence_refs,
            "strong_relation": True,
            "materialized_relation": True,
            "account_id": _text(_value(first_left, "account_id"), UNKNOWN),
            "chat_id": _text(_value(first_left, "chat_id"), UNKNOWN),
        },
    )


def _materialize_reply_status(
    candidates: Sequence[Mapping[str, Any]],
    fragments: Mapping[str, Any],
) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        if bool(candidate.get("explicit_reply_present")):
            continue
        support = set(_tuple_text(candidate.get("semantic_support")))
        families = {_CONTINUATION_SIGNAL_FAMILIES[item] for item in support if item in _CONTINUATION_SIGNAL_FAMILIES}
        if len(families) < 2:
            continue
        left = fragments.get(str(candidate.get("left_fragment_id")))
        right = fragments.get(str(candidate.get("right_fragment_id")))
        if left is None or right is None or not _same_scope(left, right):
            continue
        if _text(_value(left, "intent"), "statement").casefold() not in {"question", "request"}:
            continue
        evidence_refs = _copy_tuple_dicts(candidate.get("evidence_refs"))
        if not evidence_refs:
            continue
        relation_id = _text(candidate.get("candidate_id"), UNKNOWN)
        relation_ref = _scoped_relation_ref(
            "reply_status",
            left,
            right,
            relation_id=relation_id,
            kind="no_reply",
            fields=tuple(sorted(families)),
        )
        rows.append(
            {
                "authoritative_status": "awaiting_reply",
                "reply_status": "awaiting_reply",
                "no_explicit_reply": True,
                "reply_edge_checked": True,
                "message_ref": _message_id(left),
                "message_refs": [_message_id(left), _message_id(right)],
                "candidate_ref": relation_id,
                "candidate_refs": [relation_id],
                "semantic_continuation_signals": sorted(families),
                "continuation_signal_count": len(families),
                "scoped_evidence_ref": relation_ref,
                "scoped_evidence_refs": [relation_ref],
                "evidence_refs": evidence_refs,
                "strong_relation": True,
                "materialized_relation": True,
                "account_id": _text(_value(left, "account_id"), UNKNOWN),
                "chat_id": _text(_value(left, "chat_id"), UNKNOWN),
            }
        )
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        unique[stable_hash(row)] = row
    return tuple(unique.values())


def _unresolved_slots(fragments: Sequence[BundleFragment]) -> Tuple[Dict[str, Any], ...]:
    slots: List[Dict[str, Any]] = []
    for fragment in fragments:
        fragment_id = _text(_value(fragment, "fragment_id"), UNKNOWN)
        object_id = _text(_value(fragment, "object_id"), UNKNOWN)
        resolution = _text(_value(fragment, "object_resolution"), UNKNOWN)
        if not (_known(object_id) and resolution in _KNOWN_RESOLUTIONS):
            slots.append({"slot": "object", "fragment_id": fragment_id, "resolution": "unknown"})
        state = _text(_value(fragment, "state"), UNKNOWN)
        if state == UNKNOWN:
            slots.append({"slot": "state", "fragment_id": fragment_id, "resolution": "unknown"})
        subject = _text(_value(fragment, "subject_id"), UNKNOWN)
        if not _known(subject):
            slots.append({"slot": "subject", "fragment_id": fragment_id, "resolution": "unknown"})
    return tuple(slots)


def _open_thread_candidate(bundle: DialogueBundle, fragments: Sequence[BundleFragment], claims: Sequence[BundleClaim]) -> Dict[str, Any]:
    fragment_ids = tuple(_text(_value(item, "fragment_id"), UNKNOWN) for item in fragments)
    claim_ids = tuple(_text(_value(item, "claim_id"), UNKNOWN) for item in claims)
    message_ids = tuple(dict.fromkeys(_message_id(item) for item in fragments))
    evidence_refs: List[Dict[str, Any]] = []
    for fragment in fragments:
        if not bool(_value(fragment, "is_silent", False)):
            evidence_refs.append(_fragment_evidence_ref(fragment))
    return {
        "candidate_id": _stable_id("OPEN_THREAD_CANDIDATE", {"bundle_id": bundle.bundle_id, "fragments": fragment_ids}),
        "bundle_id": bundle.bundle_id,
        "scale": bundle.scale,
        "window_scale": bundle.to_dict().get("window_scale"),
        "account_id": bundle.account_id,
        "chat_id": bundle.chat_id,
        "fragment_ids": list(fragment_ids),
        "claim_ids": list(claim_ids),
        "source_message_ids": list(message_ids),
        "open_boundary": True,
        "unresolved_slots": [dict(item) for item in _unresolved_slots(fragments)],
        "candidate_basis": ["dialogue_bundle", "reversible_context_window"],
        "evidence_refs": evidence_refs,
        "source_refs": [{"type": "message", "id": item} for item in message_ids],
        "candidate_only": True,
    }


@dataclass(frozen=True)
class ContextPacket:
    """One reversible context packet; it is not an event or final topic.

    ``primary_fragments`` is the packet's authoritative retained fragment
    view, not a model-topic primary list.  It deliberately includes
    context-only opener/acknowledgement/media fragments when they are part of
    the packet window.  Provider-facing primary/context roles are projected
    later by the Stage-A adapters.
    """

    packet_id: str
    account_id: str = UNKNOWN
    chat_id: str = UNKNOWN
    anchor_fragment_id: str = UNKNOWN
    anchor_bundle_id: str = UNKNOWN
    source_message_ids: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    primary_fragments: Tuple[Dict[str, Any], ...] = ()
    authoritative_facts: Tuple[Dict[str, Any], ...] = ()
    adjacent_context: Tuple[Dict[str, Any], ...] = ()
    candidate_qa_links: Tuple[Dict[str, Any], ...] = ()
    candidate_person_history: Tuple[Dict[str, Any], ...] = ()
    candidate_object_history: Tuple[Dict[str, Any], ...] = ()
    candidate_state_history: Tuple[Dict[str, Any], ...] = ()
    open_thread_candidates: Tuple[Dict[str, Any], ...] = ()
    activation_cues: Tuple[Dict[str, Any], ...] = ()
    candidate_reason: Tuple[str, ...] = ()
    uncertainties: Tuple[str, ...] = ()
    source_refs: Tuple[Dict[str, Any], ...] = ()
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    fixed_part: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    dynamic_part: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    fixed_hash: str = ""
    dynamic_hash: str = ""
    packet_hash: str = ""
    cache_key: str = ""
    packet_version: str = CONTEXT_PACKET_VERSION
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = CONTEXT_PACKET_PIPELINE_VERSION
    ruleset_version: str = CONTEXT_PACKET_RULESET_VERSION

    @property
    def id(self) -> str:
        return self.packet_id

    @property
    def version(self) -> str:
        return self.packet_version

    @property
    def hash(self) -> str:
        return self.packet_hash

    @property
    def context_packet_id(self) -> str:
        return self.packet_id

    @property
    def fragment_ids(self) -> Tuple[str, ...]:
        return tuple(str(item.get("fragment_id", UNKNOWN)) for item in self.primary_fragments)

    @property
    def message_ids(self) -> Tuple[str, ...]:
        return self.source_message_ids

    @property
    def fixed_prefix(self) -> Mapping[str, Any]:
        """Stable provider-prefix projection; dynamic candidates are separate."""

        return self.fixed_part

    @property
    def dynamic_context(self) -> Mapping[str, Any]:
        return self.dynamic_part

    def cache_context(self) -> Dict[str, Any]:
        return {
            "packet_version": self.packet_version,
            "fixed_part_version": FIXED_PART_VERSION,
            "dynamic_part_version": DYNAMIC_PART_VERSION,
            "fixed_hash": self.fixed_hash,
            "dynamic_hash": self.dynamic_hash,
            "packet_hash": self.packet_hash,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "context_packet_id": self.packet_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "anchor_fragment_id": self.anchor_fragment_id,
            "anchor_bundle_id": self.anchor_bundle_id,
            "source_message_ids": list(self.source_message_ids),
            "claim_ids": list(self.claim_ids),
            "primary_fragments": [deepcopy(item) for item in self.primary_fragments],
            "authoritative_facts": [deepcopy(item) for item in self.authoritative_facts],
            "adjacent_context": [deepcopy(item) for item in self.adjacent_context],
            "candidate_qa_links": [deepcopy(item) for item in self.candidate_qa_links],
            "candidate_person_history": [deepcopy(item) for item in self.candidate_person_history],
            "candidate_object_history": [deepcopy(item) for item in self.candidate_object_history],
            "candidate_state_history": [deepcopy(item) for item in self.candidate_state_history],
            "open_thread_candidates": [deepcopy(item) for item in self.open_thread_candidates],
            "activation_cues": [deepcopy(item) for item in self.activation_cues],
            "activation_cue_codes": [str(item.get("cue_type", "")) for item in self.activation_cues],
            "candidate_reason": list(self.candidate_reason),
            "uncertainties": list(self.uncertainties),
            "source_refs": [deepcopy(item) for item in self.source_refs],
            "evidence_refs": [deepcopy(item) for item in self.evidence_refs],
            # Keep the typed sidecar visible beside the packet layers.  The
            # same rows also live in ``dynamic_part`` so cache restore and
            # linear projection retain the exact contract.
            "message_metadata": deepcopy(self.dynamic_part.get("message_metadata", ())),
            "topic_transitions": deepcopy(self.dynamic_part.get("topic_transitions", ())),
            "candidate_competition": deepcopy(self.dynamic_part.get("candidate_competition", ())),
            "reply_status": deepcopy(self.dynamic_part.get("reply_status", ())),
            "pronoun_person_object_state": deepcopy(self.dynamic_part.get("pronoun_person_object_state", ())),
            "fixed_part": deepcopy(dict(self.fixed_part)),
            "dynamic_part": deepcopy(dict(self.dynamic_part)),
            "fixed_hash": self.fixed_hash,
            "dynamic_hash": self.dynamic_hash,
            "packet_hash": self.packet_hash,
            "hash": self.packet_hash,
            "cache_key": self.cache_key,
            "packet_version": self.packet_version,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContextPacket":
        data = _copy_dict(value)
        fixed = _copy_dict(data.get("fixed_part", data.get("fixed")))
        dynamic = _copy_dict(data.get("dynamic_part", data.get("dynamic")))
        primary = _copy_tuple_dicts(data.get("primary_fragments"))
        facts = _copy_tuple_dicts(data.get("authoritative_facts"))
        adjacent = _copy_tuple_dicts(data.get("adjacent_context"))
        qa = _copy_tuple_dicts(data.get("candidate_qa_links"))
        people = _copy_tuple_dicts(data.get("candidate_person_history"))
        objects = _copy_tuple_dicts(data.get("candidate_object_history"))
        states = _copy_tuple_dicts(data.get("candidate_state_history"))
        threads = _copy_tuple_dicts(data.get("open_thread_candidates"))
        cues = _copy_tuple_dicts(data.get("activation_cues"))
        candidate_reason = _tuple_text(data.get("candidate_reason"))
        uncertainties = _tuple_text(data.get("uncertainties"))
        source_refs = _copy_tuple_dicts(data.get("source_refs"))
        evidence_refs = _copy_tuple_dicts(data.get("evidence_refs"))
        packet_version = _text(data.get("packet_version"), CONTEXT_PACKET_VERSION)
        fixed_hash = _text(data.get("fixed_hash"), stable_hash(fixed))
        dynamic_hash = _text(data.get("dynamic_hash"), stable_hash(dynamic))
        packet_hash = _text(data.get("packet_hash") or data.get("hash"), stable_hash({"packet_version": packet_version, "fixed_hash": fixed_hash, "dynamic_hash": dynamic_hash}))
        cache_key = _text(data.get("cache_key"), ContextPacketCache.make_key(packet_version, fixed_hash, dynamic_hash, packet_hash))
        return cls(
            packet_id=_text(data.get("packet_id") or data.get("context_packet_id"), UNKNOWN),
            account_id=_text(data.get("account_id"), UNKNOWN),
            chat_id=_text(data.get("chat_id"), UNKNOWN),
            anchor_fragment_id=_text(data.get("anchor_fragment_id"), UNKNOWN),
            anchor_bundle_id=_text(data.get("anchor_bundle_id"), UNKNOWN),
            source_message_ids=_tuple_text(data.get("source_message_ids")),
            claim_ids=_tuple_text(data.get("claim_ids")),
            primary_fragments=primary,
            authoritative_facts=facts,
            adjacent_context=adjacent,
            candidate_qa_links=qa,
            candidate_person_history=people,
            candidate_object_history=objects,
            candidate_state_history=states,
            open_thread_candidates=threads,
            activation_cues=cues,
            candidate_reason=candidate_reason,
            uncertainties=uncertainties,
            source_refs=source_refs,
            evidence_refs=evidence_refs,
            fixed_part=fixed,
            dynamic_part=dynamic,
            fixed_hash=fixed_hash,
            dynamic_hash=dynamic_hash,
            packet_hash=packet_hash,
            cache_key=cache_key,
            packet_version=packet_version,
            schema_version=_text(data.get("schema_version"), SCHEMA_VERSION),
            context_schema_version=_text(data.get("context_schema_version"), CONTEXT_SCHEMA_VERSION),
            pipeline_version=_text(data.get("pipeline_version"), CONTEXT_PACKET_PIPELINE_VERSION),
            ruleset_version=_text(data.get("ruleset_version"), CONTEXT_PACKET_RULESET_VERSION),
        )


class ContextPacketCache:
    """Stable content-addressed cache with explicit optional persistence."""

    cache_schema_version = "context_packet_cache_v1"

    def __init__(self, initial: Optional[Mapping[str, Any]] = None) -> None:
        self._values: Dict[str, Dict[str, Any]] = {}
        if initial:
            self.restore(initial)

    @staticmethod
    def make_key(packet_version: str, fixed_hash: str, dynamic_hash: str, packet_hash: str = "") -> str:
        return "context-packet:%s:%s" % (
            _text(packet_version, CONTEXT_PACKET_VERSION),
            stable_hash(
                {
                    "packet_version": _text(packet_version, CONTEXT_PACKET_VERSION),
                    "fixed_hash": str(fixed_hash),
                    "dynamic_hash": str(dynamic_hash),
                    "packet_hash": str(packet_hash),
                }
            ),
        )

    def key(self, value: Any = None, *, packet_version: str = CONTEXT_PACKET_VERSION, fixed_hash: Optional[str] = None, dynamic_hash: Optional[str] = None) -> str:
        if isinstance(value, ContextPacket):
            return value.cache_key or self.make_key(value.packet_version, value.fixed_hash, value.dynamic_hash, value.packet_hash)
        if fixed_hash is not None or dynamic_hash is not None:
            return self.make_key(packet_version, fixed_hash or "", dynamic_hash or "")
        if isinstance(value, Mapping):
            packet_hash = _text(value.get("packet_hash") or value.get("hash"))
            fixed = _text(value.get("fixed_hash"), stable_hash(value.get("fixed_part", value.get("fixed", {}))))
            dynamic = _text(value.get("dynamic_hash"), stable_hash(value.get("dynamic_part", value.get("dynamic", {}))))
            return self.make_key(_text(value.get("packet_version"), packet_version), fixed, dynamic, packet_hash)
        return self.make_key(packet_version, stable_hash(value), "", stable_hash(value))

    def get(self, key: Any) -> Optional[Mapping[str, Any]]:
        resolved = self.key(key) if isinstance(key, (ContextPacket, Mapping)) else str(key)
        value = self._values.get(resolved)
        return deepcopy(value) if value is not None else None

    def put(self, key_or_packet: Any, value: Optional[Mapping[str, Any]] = None) -> str:
        if isinstance(key_or_packet, ContextPacket):
            key = key_or_packet.cache_key or self.key(key_or_packet)
            payload = key_or_packet.to_dict() if value is None else _copy_dict(value)
        elif value is None and isinstance(key_or_packet, Mapping):
            payload = _copy_dict(key_or_packet)
            key = self.key(payload)
        else:
            key = str(key_or_packet)
            payload = _copy_dict(value)
        if not payload:
            raise ValueError("context packet cache values must be non-empty mappings")
        self._values[key] = payload
        return key

    def contains(self, key: Any) -> bool:
        return self.get(key) is not None

    def clear(self) -> None:
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: Any) -> bool:
        return self.contains(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def export(self) -> Dict[str, Any]:
        return {
            "cache_schema_version": self.cache_schema_version,
            "entry_count": len(self._values),
            "entries": {key: deepcopy(self._values[key]) for key in sorted(self._values)},
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.export()

    def restore(self, value: Mapping[str, Any]) -> None:
        entries = value.get("entries", value) if isinstance(value, Mapping) else {}
        if not isinstance(entries, Mapping):
            raise ValueError("invalid context packet cache payload")
        for key, item in entries.items():
            if isinstance(item, Mapping):
                self._values[str(key)] = _copy_dict(item)

    def dumps(self) -> str:
        return json.dumps(self.export(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def loads(cls, payload: str) -> "ContextPacketCache":
        value = json.loads(payload)
        if not isinstance(value, Mapping):
            raise ValueError("invalid context packet cache JSON")
        return cls(value)

    def save(self, path: Any) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.dumps(), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Any) -> "ContextPacketCache":
        return cls.loads(Path(path).read_text(encoding="utf-8"))


# Explicit aliases make the persistence contract discoverable to adapters.
ContentAddressedPacketCache = ContextPacketCache
VersionedContextPacketCache = ContextPacketCache


@dataclass(frozen=True)
class ContextPacketResult:
    packets: Tuple[ContextPacket, ...] = ()
    dialogue_result: Optional[DialogueBundleResult] = field(default=None, repr=False, compare=False)
    input_hash: str = ""
    cache_key: str = ""
    cache_hits: int = 0
    cache_misses: int = 0
    packet_version: str = CONTEXT_PACKET_VERSION
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = CONTEXT_PACKET_PIPELINE_VERSION
    ruleset_version: str = CONTEXT_PACKET_RULESET_VERSION

    @property
    def context_packets(self) -> Tuple[ContextPacket, ...]:
        return self.packets

    @property
    def packet_ids(self) -> Tuple[str, ...]:
        return tuple(item.packet_id for item in self.packets)

    @property
    def fragments(self) -> Tuple[Any, ...]:
        return self.dialogue_result.fragments if self.dialogue_result is not None else ()

    @property
    def bundles(self) -> Tuple[Any, ...]:
        return self.dialogue_result.bundles if self.dialogue_result is not None else ()

    def __len__(self) -> int:
        return len(self.packets)

    def __iter__(self) -> Iterator[ContextPacket]:
        return iter(self.packets)

    def __getitem__(self, index: int) -> ContextPacket:
        return self.packets[index]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "packets": [item.to_dict() for item in self.packets],
            "context_packets": [item.to_dict() for item in self.packets],
            "packet_ids": list(self.packet_ids),
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "packet_version": self.packet_version,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


class ContextPacketBuilder:
    """Build high-recall, reversible packets from public context candidates."""

    def __init__(
        self,
        registry: Optional[MessageRegistry] = None,
        gate: Optional[SemanticGate] = None,
        *,
        window_size: int = 8,
        time_window_seconds: float = 15 * 60,
        max_candidates: int = 3,
        max_packets: int = 4096,
        cache: Optional[ContextPacketCache] = None,
        packet_version: str = CONTEXT_PACKET_VERSION,
    ) -> None:
        if int(window_size) < 1:
            raise ValueError("window_size must be positive")
        if float(time_window_seconds) < 0:
            raise ValueError("time_window_seconds must be non-negative")
        if int(max_candidates) < 1:
            raise ValueError("max_candidates must be positive")
        if int(max_packets) < 1:
            raise ValueError("max_packets must be positive")
        self.registry = registry if registry is not None else MessageRegistry()
        self.gate = gate if gate is not None else SemanticGate(self.registry)
        if self.gate.registry is not self.registry:
            raise ValueError("gate and registry must reference the same registry")
        self.window_size = int(window_size)
        self.time_window_seconds = float(time_window_seconds)
        self.max_candidates = int(max_candidates)
        self.max_packets = int(max_packets)
        self.cache = cache if cache is not None else ContextPacketCache()
        self.packet_version = _text(packet_version, CONTEXT_PACKET_VERSION)

    def _check_development_only(self, result: DialogueBundleResult) -> None:
        for entry in result.registrations:
            if entry.metadata.split in {"frozen", "frozen_test"}:
                raise ValueError("context packet builder accepts development/public inputs only")

    def _result_from_input(
        self,
        messages: Optional[Iterable[Any]],
        *,
        fragments: Optional[Iterable[Any]],
        claims: Optional[Iterable[Any]],
        dialogue_result: Optional[DialogueBundleResult],
    ) -> DialogueBundleResult:
        if dialogue_result is not None:
            return dialogue_result
        if isinstance(messages, DialogueBundleResult):
            return messages
        return build_dialogue_bundles(
            tuple(messages or ()),
            fragments=tuple(fragments or ()) if fragments is not None else None,
            claims=tuple(claims or ()) if claims is not None else None,
            registry=self.registry,
            gate=self.gate,
            window_size=self.window_size,
            time_window_seconds=self.time_window_seconds,
            max_candidates=self.max_candidates,
        )

    @staticmethod
    def _index_fragments(result: DialogueBundleResult) -> Dict[str, BundleFragment]:
        return {str(item.fragment_id): item for item in result.fragments}

    @staticmethod
    def _index_claims(result: DialogueBundleResult) -> Dict[str, BundleClaim]:
        return {str(item.claim_id): item for item in result.claims}

    @staticmethod
    def _index_entries(result: DialogueBundleResult) -> Dict[str, RegisteredMessage]:
        output: Dict[str, RegisteredMessage] = {}
        for item in result.registrations:
            output[item.message_id] = item
            output[item.registry_key] = item
        return output

    def _candidate_index(self, result: DialogueBundleResult, fragments: Mapping[str, BundleFragment]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        candidates: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for relation in result.relations:
            left = fragments.get(str(relation.left_anchor_id))
            right = fragments.get(str(relation.right_anchor_id))
            if left is None or right is None or not _same_scope(left, right):
                continue
            value = _relation_dict(relation, left, right)
            candidates[(str(relation.left_anchor_id), str(relation.right_anchor_id))] = value

        ordered = list(result.fragments)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 : index + 1 + self.window_size]:
                pair = (str(left.fragment_id), str(right.fragment_id))
                if pair in candidates:
                    continue
                derived = _derived_candidate(left, right)
                if derived is not None:
                    candidates[pair] = derived
        return candidates

    def _adjacent(
        self,
        primary_ids: Sequence[str],
        ordered_fragments: Sequence[BundleFragment],
        candidate_index: Mapping[Tuple[str, str], Mapping[str, Any]],
    ) -> Tuple[Dict[str, Any], ...]:
        primary_set = set(primary_ids)
        selected = [item for item in ordered_fragments if str(item.fragment_id) in primary_set]
        if not selected:
            return ()
        result: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in selected:
            anchor_index = next((index for index, item in enumerate(ordered_fragments) if item.fragment_id == anchor.fragment_id), -1)
            if anchor_index < 0:
                continue
            for distance in range(1, self.window_size + 1):
                for index in (anchor_index - distance, anchor_index + distance):
                    if index < 0 or index >= len(ordered_fragments):
                        continue
                    neighbour = ordered_fragments[index]
                    if str(neighbour.fragment_id) in primary_set or str(neighbour.fragment_id) in seen:
                        continue
                    if not _same_scope(anchor, neighbour):
                        continue
                    left_id, right_id = str(anchor.fragment_id), str(neighbour.fragment_id)
                    pair = candidate_index.get((left_id, right_id)) or candidate_index.get((right_id, left_id))
                    reasons: List[str] = ["finite_local_window", "same_scope"]
                    if _value(anchor, "segment_id") is not None and _value(anchor, "segment_id") == _value(neighbour, "segment_id"):
                        reasons.append("same_segment_weak")
                    left_time, right_time = _time(anchor), _time(neighbour)
                    time_distance = None
                    if left_time is not None and right_time is not None:
                        time_distance = abs(right_time - left_time)
                        reasons.append("time_proximity_weak")
                    if pair is not None:
                        reasons.append("semantic_candidate_available")
                    result.append(
                        {
                            "fragment_id": str(neighbour.fragment_id),
                            "message_id": _message_id(neighbour),
                            "account_id": _text(_value(neighbour, "account_id"), UNKNOWN),
                            "chat_id": _text(_value(neighbour, "chat_id"), UNKNOWN),
                            "relative_to_fragment_id": str(anchor.fragment_id),
                            "distance_in_fragment_order": distance,
                            "same_segment": bool(_value(anchor, "segment_id") is not None and _value(anchor, "segment_id") == _value(neighbour, "segment_id")),
                            "time_distance_seconds": time_distance,
                            "candidate_id": pair.get("candidate_id") if pair else None,
                            "candidate_reason": list(dict.fromkeys(reasons)),
                            "text_redacted": _text(_value(neighbour, "text")),
                            "evidence_ref": _fragment_evidence_ref(neighbour),
                            "candidate_only": True,
                        }
                    )
                    seen.add(str(neighbour.fragment_id))
                    if len(result) >= self.window_size:
                        return tuple(result)
        return tuple(result)

    def _cues(
        self,
        fragments: Sequence[BundleFragment],
        entries: Mapping[str, RegisteredMessage],
        decisions: Mapping[str, GateDecision],
        candidate_values: Sequence[Mapping[str, Any]],
        adjacent: Sequence[Mapping[str, Any]],
    ) -> Tuple[Dict[str, Any], ...]:
        values: List[Dict[str, Any]] = []

        def add(cue_type: str, *, message_ids: Iterable[str] = (), fragment_ids: Iterable[str] = (), detail: Optional[Mapping[str, Any]] = None) -> None:
            messages = tuple(dict.fromkeys(str(item) for item in message_ids if _known(item)))
            fragment_values = tuple(dict.fromkeys(str(item) for item in fragment_ids if _known(item)))
            payload = {
                "cue_type": cue_type,
                "message_ids": list(messages),
                "fragment_ids": list(fragment_values),
                "detail": _copy_dict(detail or {}),
            }
            payload["replay_key"] = _stable_id("ACTIVATION_CUE", payload)
            if payload["replay_key"] not in {item["replay_key"] for item in values}:
                values.append(payload)

        for fragment in fragments:
            fragment_id = _text(_value(fragment, "fragment_id"), UNKNOWN)
            message_id = _message_id(fragment)
            entry = entries.get(message_id)
            decision = decisions.get(entry.registry_key if entry is not None else message_id)
            if decision is not None:
                add("gate_channel", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"channel": decision.channel, "reason_codes": list(decision.reason_codes)})
                for reason in decision.reason_codes:
                    add("gate_reason", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"reason": reason})
            object_id = _text(_value(fragment, "object_id"), UNKNOWN)
            resolution = _text(_value(fragment, "object_resolution"), UNKNOWN)
            if not (_known(object_id) and resolution in _KNOWN_RESOLUTIONS):
                add("object_reference", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"resolution": "unknown"})
            else:
                add("object_history", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"object_id": object_id, "resolution": resolution})
            subject = _text(_value(fragment, "subject_id"), UNKNOWN)
            people = _tuple_text(_value(fragment, "mentioned_person_ids"))
            if _known(subject) or people:
                add("person_history", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"subject_id": subject, "mentioned_person_ids": list(people)})
            if _text(_value(fragment, "intent"), "statement") in {"question", "request"}:
                add("question_follow_up", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"intent": _value(fragment, "intent")})
            if _known(_value(fragment, "reply_to_message_id")):
                add("explicit_reference", message_ids=(message_id, str(_value(fragment, "reply_to_message_id"))), fragment_ids=(fragment_id,), detail={"reply_to_message_id": _value(fragment, "reply_to_message_id")})
            state = _text(_value(fragment, "state"), UNKNOWN)
            if state != UNKNOWN:
                add("state_history", message_ids=(message_id,), fragment_ids=(fragment_id,), detail={"state_candidate": state, "state_evidence": _value(fragment, "state_evidence")})
        for candidate in candidate_values:
            kind = _candidate_kind(candidate)
            add("candidate_%s" % kind, message_ids=(candidate.get("left_message_id"), candidate.get("right_message_id")), fragment_ids=(candidate.get("left_fragment_id"), candidate.get("right_fragment_id")), detail={"candidate_id": candidate.get("candidate_id"), "semantic_support": candidate.get("semantic_support", [])})
        if adjacent:
            add("adjacent_context", fragment_ids=[item.get("fragment_id") for item in adjacent], detail={"weak_only": True})
        return tuple(values)

    def _build_packet(
        self,
        bundle: DialogueBundle,
        primary: Sequence[BundleFragment],
        result: DialogueBundleResult,
        fragments: Mapping[str, BundleFragment],
        claims: Mapping[str, BundleClaim],
        entries: Mapping[str, RegisteredMessage],
        decisions: Mapping[str, GateDecision],
        candidate_index: Mapping[Tuple[str, str], Mapping[str, Any]],
    ) -> ContextPacket:
        # ``primary_fragments`` is the reversible K2 retention view.  A
        # context-only fragment next to a substantive bundle must remain in
        # that view even though it is not eligible to become a provider topic
        # primary.  Keep the original bundle anchors as the seed and append
        # only context-only neighbours; substantive neighbours remain in the
        # explicit adjacent window and cannot be silently promoted.
        bundle_fragments = tuple(primary)
        anchor_ids = tuple(str(item.fragment_id) for item in bundle_fragments)
        adjacent = self._adjacent(anchor_ids, result.fragments, candidate_index)
        retained: List[BundleFragment] = list(bundle_fragments)
        retained_ids = {str(item.fragment_id) for item in retained}
        for adjacent_row in adjacent:
            fragment_id = str(adjacent_row.get("fragment_id", ""))
            neighbour = fragments.get(fragment_id)
            if neighbour is None or fragment_id in retained_ids:
                continue
            entry = entries.get(_message_id(neighbour))
            if _fragment_is_context_only(neighbour, entry=entry):
                retained.append(neighbour)
                retained_ids.add(fragment_id)

        # The first substantive fragment remains the semantic anchor for
        # hashes/IDs when one exists.  A pure social/media packet still gets a
        # deterministic local packet so its complete source can be recovered;
        # Stage-A later declines to emit it as an independent primary.
        semantic_anchors = tuple(
            item
            for item in bundle_fragments
            if not _fragment_is_context_only(item, entry=entries.get(_message_id(item)))
        )
        anchor_fragment_id = str((semantic_anchors or bundle_fragments)[0].fragment_id)
        primary = tuple(retained)
        primary_ids = tuple(str(item.fragment_id) for item in primary)
        primary_ids_set = set(primary_ids)
        primary_claims = tuple(
            claim
            for claim_id in bundle.claim_ids
            if str(claim_id) in claims
            for claim in (claims[str(claim_id)],)
            if str(claim.fragment_id) in primary_ids_set
        )
        context_ids = set(primary_ids) | {str(item.get("fragment_id")) for item in adjacent}
        candidates: List[Mapping[str, Any]] = []
        for candidate in candidate_index.values():
            pair_ids = {str(candidate.get("left_fragment_id")), str(candidate.get("right_fragment_id"))}
            if pair_ids <= context_ids:
                left = fragments.get(str(candidate.get("left_fragment_id")))
                right = fragments.get(str(candidate.get("right_fragment_id")))
                if (
                    left is not None
                    and right is not None
                    and _same_scope(left, right)
                    and not _fragment_is_context_only(left, entry=entries.get(_message_id(left)))
                    and not _fragment_is_context_only(right, entry=entries.get(_message_id(right)))
                ):
                    candidates.append(candidate)
        unique_candidates: Dict[str, Mapping[str, Any]] = {str(item.get("candidate_id")): item for item in candidates}
        candidates = list(unique_candidates.values())

        qa: List[Dict[str, Any]] = []
        people: List[Dict[str, Any]] = []
        objects: List[Dict[str, Any]] = []
        states: List[Dict[str, Any]] = []
        for candidate in candidates:
            kind = _candidate_kind(candidate)
            support = set(_tuple_text(candidate.get("semantic_support")))
            typed_rows = {
                name: _materialize_typed_history(candidate, fragments.get(str(candidate.get("left_fragment_id"))), fragments.get(str(candidate.get("right_fragment_id"))), kind=name)
                for name in ("person", "object", "state")
                if fragments.get(str(candidate.get("left_fragment_id"))) is not None and fragments.get(str(candidate.get("right_fragment_id"))) is not None
            }
            projected = _candidate_projection(candidate)
            if kind == "qa":
                qa.append(projected)
            if kind == "person" or {"shared_subject", "shared_mentioned_person"} & support:
                people.append(typed_rows.get("person") or projected)
            if kind == "state" or {"shared_state", "state_change"} & support:
                states.append(typed_rows.get("state") or projected)
            if kind == "object" or {"shared_object", "object_inheritance"} & support:
                objects.append(typed_rows.get("object") or projected)
            # A candidate may intentionally appear in several history views:
            # QA plus object/person/state evidence must not be collapsed into a
            # single mutually-exclusive label before provider review.
            if not (kind in {"qa", "person", "object", "state"} or support & {
                "shared_subject", "shared_mentioned_person", "shared_state", "state_change", "shared_object", "object_inheritance"
            }):
                objects.append(projected)

        # Materialize only the existing, typed structure.  These rows remain
        # local candidates (``candidate_only`` is preserved on history rows),
        # while the explicit marker fields make the evidence auditable to the
        # body-free selection sidecar.
        context_ids = set(primary_ids) | {str(item.get("fragment_id")) for item in adjacent}
        greeting_metadata = _materialize_greeting_boundaries(result.fragments, context_ids)
        topic_metadata = _materialize_topic_transitions(candidates, fragments)
        competition_metadata = _materialize_competition(candidates, fragments)
        reply_metadata = _materialize_reply_status(candidates, fragments)
        if competition_metadata:
            qa.extend(dict(item) for item in competition_metadata)

        triad_metadata: Tuple[Dict[str, Any], ...] = ()
        if people and objects and states:
            triad_candidates = list(
                dict.fromkeys(
                    _text(item.get("candidate_ref") or item.get("candidate_id"), UNKNOWN)
                    for values in (people, objects, states)
                    for item in values
                    if _known(item.get("candidate_ref") or item.get("candidate_id"))
                )
            )
            triad_messages = list(
                dict.fromkeys(
                    str(value)
                    for values in (people, objects, states)
                    for item in values
                    for value in (item.get("left_message_id"), item.get("right_message_id"))
                    if _known(value)
                )
            )
            triad_evidence = list(
                {
                    stable_hash(ref): ref
                    for values in (people, objects, states)
                    for item in values
                    for ref in _copy_tuple_dicts(item.get("scoped_evidence_refs") or item.get("evidence_refs"))
                }.values()
            )
            if triad_candidates and triad_messages and triad_evidence:
                triad_metadata = (
                    {
                        "stratum": "pronoun_person_object_state",
                        "candidate_ref": triad_candidates[0],
                        "candidate_refs": triad_candidates,
                        "message_refs": triad_messages,
                        "scoped_evidence_refs": triad_evidence,
                        "evidence_refs": triad_evidence,
                        "strong_relation": True,
                        "materialized_relation": True,
                        "account_id": _text(_value(bundle, "account_id"), UNKNOWN),
                        "chat_id": _text(_value(bundle, "chat_id"), UNKNOWN),
                    },
                )

        open_thread = _open_thread_candidate(bundle, primary, primary_claims)
        candidate_values = tuple(qa + people + objects + states)
        packet_reasons: List[str] = ["dialogue_bundle_candidate", "reversible_context", "finite_local_window"]
        if bundle.scale:
            packet_reasons.append("scale:%s" % bundle.scale)
        if bundle.open_boundary:
            packet_reasons.append("open_boundary")
        for item in adjacent:
            packet_reasons.extend(str(reason) for reason in item.get("candidate_reason", ()))
        for candidate in candidate_values:
            packet_reasons.extend(str(reason) for reason in candidate.get("supporting_slot_codes", ()))
        packet_reasons = list(dict.fromkeys(packet_reasons))
        uncertainty: List[str] = []
        scope = _scope(bundle)
        if scope is None:
            uncertainty.append("scope_unknown")
        if not candidate_values:
            uncertainty.append("no_semantic_candidate")
        if any("time_proximity_weak" in item.get("candidate_reason", ()) for item in adjacent) and not candidate_values:
            uncertainty.append("time_weak_only")
        if any("same_segment_weak" in item.get("candidate_reason", ()) for item in adjacent) and not candidate_values:
            uncertainty.append("same_segment_weak_only")
        for fragment in primary:
            if not _known(_value(fragment, "speaker_id")):
                uncertainty.append("speaker_unknown")
            if not _known(_value(fragment, "subject_id")):
                uncertainty.append("subject_unknown")
            if not (_known(_value(fragment, "object_id")) and _text(_value(fragment, "object_resolution"), UNKNOWN) in _KNOWN_RESOLUTIONS):
                uncertainty.append("object_unknown")
            if _text(_value(fragment, "state"), UNKNOWN) == UNKNOWN:
                uncertainty.append("state_unknown")
        uncertainty = list(dict.fromkeys(uncertainty))

        source_ids = tuple(dict.fromkeys(_message_id(item) for item in primary))
        facts: List[Dict[str, Any]] = []
        source_refs: List[Dict[str, Any]] = []
        for message_id in source_ids:
            entry = entries.get(message_id)
            if entry is not None:
                facts.append(_authoritative_fact(entry))
                source_refs.append(_message_source_ref(entry))
        primary_projection = tuple(_fragment_projection(item) for item in primary)
        evidence_refs: List[Dict[str, Any]] = []
        for fragment in primary:
            if not bool(_value(fragment, "is_silent", False)):
                evidence_refs.append(_fragment_evidence_ref(fragment))
                source_refs.append(_fragment_evidence_ref(fragment))
        for claim in primary_claims:
            evidence_refs.append(_claim_evidence_ref(claim))
            source_refs.append(_claim_evidence_ref(claim))
        source_refs = list({stable_hash(item): item for item in source_refs}.values())
        evidence_refs = list({stable_hash(item): item for item in evidence_refs}.values())
        fixed = {
            "fixed_part_version": FIXED_PART_VERSION,
            "packet_version": self.packet_version,
            "scope": {"account_id": _text(_value(bundle, "account_id"), UNKNOWN), "chat_id": _text(_value(bundle, "chat_id"), UNKNOWN)},
            "anchor_fragment_id": anchor_fragment_id if primary_ids else UNKNOWN,
            "anchor_bundle_id": bundle.bundle_id,
            "source_message_ids": list(source_ids),
            "claim_ids": [str(item.claim_id) for item in primary_claims],
            "authoritative_facts": deepcopy(facts),
            "primary_fragments": deepcopy(primary_projection),
            "source_refs": deepcopy(source_refs),
        }
        dynamic = {
            "dynamic_part_version": DYNAMIC_PART_VERSION,
            "adjacent_context": deepcopy(adjacent),
            "candidate_qa_links": deepcopy(qa),
            "candidate_person_history": deepcopy(people),
            "candidate_object_history": deepcopy(objects),
            "candidate_state_history": deepcopy(states),
            "open_thread_candidates": [deepcopy(open_thread)],
            "activation_cues": [],
            "candidate_reason": list(packet_reasons),
            "uncertainties": list(uncertainty),
            "evidence_refs": deepcopy(evidence_refs),
            # Canonical sidecar inputs.  Every row is body-free and exists
            # only when the upstream fragment/relation carried the required
            # typed evidence; absence is intentionally represented by an
            # empty list rather than a guessed label.
            "message_metadata": deepcopy(greeting_metadata),
            "topic_transitions": deepcopy(topic_metadata),
            "candidate_competition": deepcopy(competition_metadata),
            "reply_status": deepcopy(reply_metadata),
            "pronoun_person_object_state": deepcopy(triad_metadata),
        }
        cues = self._cues(primary, entries, decisions, candidate_values, adjacent)
        dynamic["activation_cues"] = deepcopy(cues)
        fixed_hash = stable_hash(fixed)
        dynamic_hash = stable_hash(dynamic)
        packet_hash = stable_hash({"packet_version": self.packet_version, "fixed_hash": fixed_hash, "dynamic_hash": dynamic_hash})
        packet_id = _stable_id(
            "CONTEXT_PACKET",
            {"bundle_id": bundle.bundle_id, "anchor_fragment_id": anchor_fragment_id if primary_ids else UNKNOWN, "packet_version": self.packet_version},
        )
        cache_key = ContextPacketCache.make_key(self.packet_version, fixed_hash, dynamic_hash, packet_hash)
        return ContextPacket(
            packet_id=packet_id,
            account_id=_text(_value(bundle, "account_id"), UNKNOWN),
            chat_id=_text(_value(bundle, "chat_id"), UNKNOWN),
            anchor_fragment_id=anchor_fragment_id if primary_ids else UNKNOWN,
            anchor_bundle_id=bundle.bundle_id,
            source_message_ids=source_ids,
            claim_ids=tuple(str(item.claim_id) for item in primary_claims),
            primary_fragments=primary_projection,
            authoritative_facts=tuple(facts),
            adjacent_context=tuple(adjacent),
            candidate_qa_links=tuple(qa),
            candidate_person_history=tuple(people),
            candidate_object_history=tuple(objects),
            candidate_state_history=tuple(states),
            open_thread_candidates=(open_thread,),
            activation_cues=cues,
            candidate_reason=tuple(packet_reasons),
            uncertainties=tuple(uncertainty),
            source_refs=tuple(source_refs),
            evidence_refs=tuple(evidence_refs),
            fixed_part=fixed,
            dynamic_part=dynamic,
            fixed_hash=fixed_hash,
            dynamic_hash=dynamic_hash,
            packet_hash=packet_hash,
            cache_key=cache_key,
            packet_version=self.packet_version,
        )

    def build(
        self,
        messages: Optional[Iterable[Any]] = None,
        *,
        fragments: Optional[Iterable[Any]] = None,
        claims: Optional[Iterable[Any]] = None,
        dialogue_result: Optional[DialogueBundleResult] = None,
        bundle_result: Optional[DialogueBundleResult] = None,
    ) -> ContextPacketResult:
        result = self._result_from_input(
            messages,
            fragments=fragments,
            claims=claims,
            dialogue_result=dialogue_result or bundle_result,
        )
        self._check_development_only(result)
        fragment_index = self._index_fragments(result)
        claim_index = self._index_claims(result)
        entry_index = self._index_entries(result)
        decisions: Dict[str, GateDecision] = {}
        for decision in result.gate_decisions:
            decisions[decision.registry_key] = decision
            decisions[decision.message_id] = decision
        candidate_index = self._candidate_index(result, fragment_index)
        input_hash = stable_hash(
            {
                "packet_version": self.packet_version,
                "dialogue_input_hash": result.input_hash,
                "fragment_ids": [item.fragment_id for item in result.fragments],
                "claim_ids": [item.claim_id for item in result.claims],
                "bundle_ids": [item.bundle_id for item in result.bundles],
            }
        )
        packets: List[ContextPacket] = []
        cache_hits = 0
        cache_misses = 0
        seen_packet_ids: set[str] = set()
        bundles = list(result.bundles)
        for bundle in bundles:
            primary = tuple(
                fragment_index[str(fragment_id)]
                for fragment_id in bundle.fragment_ids
                if str(fragment_id) in fragment_index
            )
            if not primary:
                continue
            packet = self._build_packet(bundle, primary, result, fragment_index, claim_index, entry_index, decisions, candidate_index)
            if packet.packet_id in seen_packet_ids:
                continue
            cached = self.cache.get(packet.cache_key)
            if cached is not None:
                cache_hits += 1
                packet = ContextPacket.from_mapping(cached)
            else:
                cache_misses += 1
                self.cache.put(packet.cache_key, packet.to_dict())
            packets.append(packet)
            seen_packet_ids.add(packet.packet_id)
            if len(packets) >= self.max_packets:
                break
        # A supplied public result can contain a fragment not represented in a
        # bundle.  Keep it recoverable as a one-fragment open packet.
        if len(packets) < self.max_packets:
            represented = {fragment_id for packet in packets for fragment_id in packet.fragment_ids}
            for fragment in result.fragments:
                if str(fragment.fragment_id) in represented:
                    continue
                fallback_bundle = DialogueBundle(
                    bundle_id=_stable_id("PACKET_FALLBACK_BUNDLE", {"fragment_id": fragment.fragment_id}),
                    scale="micro",
                    account_id=_text(_value(fragment, "account_id"), UNKNOWN),
                    chat_id=_text(_value(fragment, "chat_id"), UNKNOWN),
                    fragment_ids=(str(fragment.fragment_id),),
                    claim_ids=tuple(str(item.claim_id) for item in result.claims if item.fragment_id == fragment.fragment_id),
                    source_message_ids=(_message_id(fragment),),
                    open_boundary=True,
                    closed=False,
                )
                packet = self._build_packet(fallback_bundle, (fragment,), result, fragment_index, claim_index, entry_index, decisions, candidate_index)
                cached = self.cache.get(packet.cache_key)
                if cached is not None:
                    cache_hits += 1
                    packet = ContextPacket.from_mapping(cached)
                else:
                    cache_misses += 1
                    self.cache.put(packet.cache_key, packet.to_dict())
                packets.append(packet)
                if len(packets) >= self.max_packets:
                    break
        packets_tuple = tuple(packets)
        result_hash = stable_hash({"input_hash": input_hash, "packet_hashes": [item.packet_hash for item in packets_tuple]})
        return ContextPacketResult(
            packets=packets_tuple,
            dialogue_result=result,
            input_hash=input_hash,
            cache_key="context-packet-result:%s:%s" % (self.packet_version, result_hash),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            packet_version=self.packet_version,
        )


def build_context_packets(
    messages: Optional[Iterable[Any]] = None,
    *,
    fragments: Optional[Iterable[Any]] = None,
    claims: Optional[Iterable[Any]] = None,
    registry: Optional[MessageRegistry] = None,
    gate: Optional[SemanticGate] = None,
    dialogue_result: Optional[DialogueBundleResult] = None,
    bundle_result: Optional[DialogueBundleResult] = None,
    window_size: int = 8,
    time_window_seconds: float = 15 * 60,
    max_candidates: int = 3,
    max_packets: int = 4096,
    cache: Optional[ContextPacketCache] = None,
    packet_version: str = CONTEXT_PACKET_VERSION,
) -> ContextPacketResult:
    """One-shot public API for registry → gate → bundle → packet construction."""

    builder = ContextPacketBuilder(
        registry=registry,
        gate=gate,
        window_size=window_size,
        time_window_seconds=time_window_seconds,
        max_candidates=max_candidates,
        max_packets=max_packets,
        cache=cache,
        packet_version=packet_version,
    )
    return builder.build(
        messages,
        fragments=fragments,
        claims=claims,
        dialogue_result=dialogue_result,
        bundle_result=bundle_result,
    )


build_context_packet = build_context_packets
ContextPacketSnapshot = ContextPacketResult


__all__ = [
    "CONTEXT_PACKET_VERSION",
    "CONTEXT_PACKET_SCHEMA_VERSION",
    "CONTEXT_PACKET_PIPELINE_VERSION",
    "CONTEXT_PACKET_RULESET_VERSION",
    "FIXED_PART_VERSION",
    "DYNAMIC_PART_VERSION",
    "ContextPacket",
    "ContextPacketResult",
    "ContextPacketSnapshot",
    "ContextPacketCache",
    "ContentAddressedPacketCache",
    "VersionedContextPacketCache",
    "ContextPacketBuilder",
    "build_context_packets",
    "build_context_packet",
]
