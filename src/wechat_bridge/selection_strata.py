"""Offline, body-free canonical selection-strata metadata (K28).

This module is a side-car for the development packet path.  It consumes only
explicit metadata emitted by the registry/context-packet/linear-stage-packet
layers and produces an auditable projection for page selection.  It is not a
provider adapter and it does not inspect a private message store, frozen data,
or a model response.

There are two deliberately separate concepts in this file:

* :func:`build_canonical_strata_metadata` records every strongly supported
  stratum on a page.  A page may therefore have more than one factual
  stratum.
* :func:`select_pages_by_strata` chooses a bounded, deterministic sample.  It
  uses rare-stratum coverage and reports when complete coverage would require
  more than one scope.  It never turns candidate volume, time proximity,
  same-segment membership, or a missing reply edge into a stratum.

The K10 v2 diagnostic intentionally reads only ``manifest.private.json``,
``pages.private.jsonl``, ``materialized_map.private.jsonl`` and
``selection_map.private.jsonl``.  Its conclusion is necessarily about
*observability*: without canonical upstream metadata, an offline audit cannot
  distinguish a corpus absence from upstream non-materialization.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple, Union


STRATA_SCHEMA_VERSION = "canonical_selection_strata_v1"
STRATA_PIPELINE_VERSION = "workstream_k28_body_free_selection_strata_v1"
K10_INPUT_ARTIFACT_VERSION = "linear_stage_packet_development_v2"
K10_LOCAL_DAY = "2026-08-25"

CANONICAL_STRATA: Tuple[str, ...] = (
    "pronoun_person_object_state",
    "greeting_new_topic",
    "topic_shift",
    "candidate_competition",
    "no_reply",
)

# These are producer-side fields, not labels that this side-car may infer.
# Keeping the contract next to the canonical order makes a blocked artifact
# actionable without exposing any message body.
REQUIRED_UPSTREAM_FIELDS: Dict[str, Tuple[str, ...]] = {
    "pronoun_person_object_state": (
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "candidate_ref",
        "scoped_evidence_ref",
    ),
    "greeting_new_topic": (
        "message_metadata.is_opener_or_greeting",
        "message_metadata.message_ref",
    ),
    "topic_shift": (
        "topic_transitions.topic_shift_or_boundary",
        "topic_transitions.endpoint_message_refs",
        "topic_transitions.scoped_evidence_ref",
    ),
    "candidate_competition": (
        "candidate_qa_links.explicit_competition_relation",
        "candidate_qa_links.competing_candidate_refs",
        "candidate_qa_links.scoped_evidence_ref",
    ),
    "no_reply": (
        "reply_status.authoritative_status",
        "reply_status.message_ref",
        "reply_status.scoped_evidence_ref",
    ),
}

# K27 exposed this spelling.  Keep the alias so callers can migrate to K28
# without having to duplicate the canonical order.
CATEGORY_NAMES = CANONICAL_STRATA

STRATA_OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "strata": "strata_map.private.jsonl",
    "selection": "selection_map.private.jsonl",
    "diagnosis": "diagnosis.private.json",
}

_BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "completion",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message",
        "message_text",
        "output_text",
        "prompt",
        "quote",
        "raw",
        "raw_content",
        "raw_output",
        "raw_reasoning",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "response",
        "response_body",
        "response_text",
        "summary",
        "text",
        "text_body",
        "text_redacted",
        "thoughts",
        "transcript",
        "user_input",
        "user_packet",
        "user_canonical_json",
    }
)

_FROZEN_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})

_WEAK_REASON_CODES = frozenset(
    {
        "time_proximity_weak",
        "same_segment_weak",
        "same_segment_only",
        "time_proximity",
        "time_only",
        "time_proximity_only",
        "same_segment",
        "temporal_proximity",
        "temporal_only",
        "same_dialogue_segment",
    }
)

_LABEL_KEYS = frozenset(
    {
        "category",
        "categories",
        "canonical_category",
        "canonical_categories",
        "canonical_stratum",
        "canonical_strata",
        "selection_category",
        "selection_categories",
        "selection_stratum",
        "selection_strata",
        "stratum",
        "strata",
    }
)

_DIRECT_STRATUM_KEYS = frozenset(
    {
        "candidate_competition",
        "pronoun_person_object_state",
        "greeting_new_topic",
        "topic_shift",
        "no_reply",
    }
)

_AMBIGUITY_KEYS = frozenset(
    {
        "ambiguous",
        "ambiguity",
        "ambiguous_strata",
        "conflict",
        "conflicting_strata",
        "metadata_conflict",
        "stratum_conflict",
        "selection_ambiguous",
    }
)

_MESSAGE_ROW_KEYS = (
    "primary_fragments",
    "primary",
    "fragments",
    "adjacent_context",
    "adjacent",
    "context_fragments",
    "authoritative_facts",
    "message_metadata",
    "message_rows",
    "messages",
)

_CANDIDATE_ROW_KEYS = (
    "candidate_qa_links",
    "candidate_person_history",
    "candidate_object_history",
    "candidate_state_history",
    "continuity_candidates",
    "qa_candidates",
    "person_history",
    "object_history",
    "state_history",
    "candidate_rows",
    "candidates",
    "candidate_reasons",
    "open_thread_candidates",
    "open_threads",
)

_EVIDENCE_ROW_KEYS = (
    "evidence_refs",
    "evidence",
    "evidence_references",
    "evidence_rows",
)

_TRANSITION_ROW_KEYS = (
    "topic_transitions",
    "topic_boundaries",
    "topic_changes",
    "topic_shift_evidence",
    "transition_rows",
    "topic_boundary_rows",
    "transitions",
)

_REPLY_ROW_KEYS = (
    "reply_status",
    "reply_state",
    "response_status",
    "answer_status",
    "reply_evidence",
    "reply_metadata",
)

_PERSON_ALIASES = frozenset({"person", "person_history", "candidate_person_history", "people"})
_OBJECT_ALIASES = frozenset({"object", "object_history", "candidate_object_history", "objects"})
_STATE_ALIASES = frozenset({"state", "state_history", "candidate_state_history", "states"})

_OPENER_VALUES = frozenset(
    {"greeting", "conversation_opener", "conversation_open", "opener", "welcome", "new_topic_opener"}
)
_ACK_ROLE_VALUES = frozenset(
    {"context", "context_only", "ack", "acknowledgement", "acknowledgment", "confirmation", "social_filler"}
)
_ORDINARY_ADVERSATIVE_VALUES = frozenset(
    {"但是", "不过", "but", "however", "adversative", "ordinary_adversative", "contrast_only", "contrast_within_topic"}
)
_COMPETITION_VALUES = frozenset(
    {
        "candidate_competition",
        "competition",
        "competing_candidates",
        "mutually_exclusive",
        "exclusive_candidates",
        "candidate_set_exclusive",
        "alternative_candidates",
    }
)
_NO_REPLY_VALUES = frozenset(
    {"no_reply", "no_response", "unanswered", "awaiting_reply", "pending_reply", "reply_pending", "reply_missing"}
)
_REPLIED_VALUES = frozenset({"replied", "answered", "has_reply", "responded", "reply_present"})


class SelectionStrataError(ValueError):
    """Stable fail-closed error for K28 side-car inputs/outputs."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=lambda item: repr(item))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """Canonical JSON used by every K28 hash/replay key."""

    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalise(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip().casefold()
    text = re.sub(r"^(?:category|canonical_category|stratum|canonical_stratum)\s*[:=]\s*", "", text)
    text = text.replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", text)


def canonical_stratum(value: Any) -> str:
    """Map an explicit label to one canonical stratum, or ``""``."""

    label = _normalise(value)
    if label.endswith("_stratum"):
        label = label[: -len("_stratum")]
    aliases = {
        "pronoun_person_object_state": "pronoun_person_object_state",
        "pronoun_person_object": "pronoun_person_object_state",
        "person_object_state": "pronoun_person_object_state",
        "person_object_state_history": "pronoun_person_object_state",
        "greeting_new_topic": "greeting_new_topic",
        "greeting": "greeting_new_topic",
        "conversation_opener": "greeting_new_topic",
        "conversation_open": "greeting_new_topic",
        "opener": "greeting_new_topic",
        "welcome": "greeting_new_topic",
        "new_topic_opener": "greeting_new_topic",
        "topic_shift": "topic_shift",
        "topic_change": "topic_shift",
        "topic_transition": "topic_shift",
        "topic_boundary": "topic_shift",
        "candidate_competition": "candidate_competition",
        "competition": "candidate_competition",
        "competing_candidates": "candidate_competition",
        "mutually_exclusive": "candidate_competition",
        "exclusive_candidates": "candidate_competition",
        "candidate_set_exclusive": "candidate_competition",
        "alternative_candidates": "candidate_competition",
        "no_reply": "no_reply",
        "no_response": "no_reply",
        "unanswered": "no_reply",
        "awaiting_reply": "no_reply",
        "pending_reply": "no_reply",
        "reply_pending": "no_reply",
        "reply_missing": "no_reply",
    }
    return aliases.get(label, "")


def _truthy(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return isinstance(value, str) and _normalise(value) in {"true", "yes", "on", "enabled"}


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], (), {}, set(), frozenset())


def _iter_values(value: Any) -> Iterator[Any]:
    if isinstance(value, Mapping):
        # A boolean map is a common canonical flag projection:
        # {"topic_shift": true}.  A structured evidence object is yielded as
        # one object so its handles/strength markers remain available.
        if any(key in value for key in ("stratum", "category", "name", "evidence_type", "evidence_kind")):
            yield value
            return
        for key, child in value.items():
            if _truthy(child):
                yield key
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        yield from value
        return
    if _nonempty(value):
        yield value


def _rows(value: Any) -> List[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        # Named tables are accepted, while a single evidence row remains one
        # row.  The body-free projector never walks unknown descendants.
        if any(
            key in value
            for key in (
                "message_id",
                "message_handle",
                "message_ref",
                "message_refs",
                "endpoint_message_ref",
                "endpoint_message_refs",
                "endpoint_message_ids",
                "is_opener_or_greeting",
                "candidate_id",
                "candidate_handle",
                "candidate_ref",
                "candidate_refs",
                "evidence_id",
                "evidence_handle",
                "scoped_evidence_ref",
                "scoped_evidence_refs",
                "stratum",
                "category",
                "relation_label",
                "explicit_competition_relation",
                "fragment_type",
                "authoritative_status",
            )
        ):
            return [value]
        if value and all(isinstance(child, Mapping) for child in value.values()):
            return [child for child in value.values() if isinstance(child, Mapping)]
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [child for child in value if isinstance(child, Mapping)]
    return []


def _source_mapping(value: Any, *, include_body: bool = False) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    method = getattr(value, "to_dict", None)
    if callable(method):
        for kwargs in (
            {"include_body": include_body},
            {"include_bodies": include_body},
            {"include_content": include_body},
            {},
        ):
            try:
                candidate = method(**kwargs)
            except TypeError:
                continue
            if isinstance(candidate, Mapping):
                return candidate
    return {}


def _table(store: Any, *names: str) -> Mapping[str, Mapping[str, Any]]:
    for name in names:
        value = store.get(name) if isinstance(store, Mapping) else getattr(store, name, None)
        if isinstance(value, Mapping):
            return {str(key): child for key, child in value.items() if isinstance(child, Mapping)}
        if isinstance(value, (list, tuple)):
            result: Dict[str, Mapping[str, Any]] = {}
            for row in value:
                if not isinstance(row, Mapping):
                    continue
                key = _first(row, "message_handle", "candidate_handle", "evidence_handle", "handle", "id")
                if key not in (None, ""):
                    result[str(key)] = row
            if result:
                return result
    return {}


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _ref_values(value: Any, names: Sequence[str]) -> Iterator[str]:
    if isinstance(value, Mapping):
        candidate = _first(value, *names)
        if candidate not in (None, ""):
            if isinstance(candidate, (Mapping, list, tuple, set, frozenset)):
                # Structured contracts often use ``*_refs`` with a list of
                # rows/handles.  Recurse through that value while retaining
                # the same field whitelist; this does not inspect arbitrary
                # descendants or message bodies.
                yield from _ref_values(candidate, names)
            else:
                yield str(candidate)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _ref_values(child, names)
        return
    if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        yield str(value)


_MESSAGE_REF_FIELDS = (
    "message_handles",
    "primary_message_handles",
    "adjacent_message_handles",
    "context_message_handles",
    "authority_message_handles",
    "message_ids",
    "primary_message_ids",
    "context_message_ids",
    "source_message_ids",
    "member_message_ids",
    "message_id",
    "source_message_id",
    "fragment_id",
    "fragment_ids",
    "left_message_id",
    "right_message_id",
    "left_fragment_id",
    "right_fragment_id",
    "question_id",
    "answer_id",
    "reply_to_message_id",
    "quoted_message_id",
    # Canonical producer contracts may expose one or more opaque message
    # references under a semantic name instead of the legacy left/right
    # aliases.  These are references only; no text is inspected.
    "message_ref",
    "message_refs",
    "endpoint_message_ref",
    "endpoint_message_refs",
    "endpoint_message_ids",
)
_CANDIDATE_REF_FIELDS = (
    "candidate_handles",
    "candidate_link_refs",
    "candidate_ids",
    "candidate_handle",
    "candidate_id",
    "candidate_ref",
    "candidate_refs",
    "candidate_set",
    "candidate_group",
    "candidate_rows",
)
_COMPETING_CANDIDATE_REF_FIELDS = (
    "competing_candidate_refs",
    "competing_candidate_handles",
    "competing_candidate_ids",
    "alternative_candidate_refs",
    "alternative_candidate_handles",
    "alternative_candidate_ids",
    "competing_candidates",
    "alternative_candidates",
)
_GROUNDED_OBJECT_REF_FIELDS = (
    "grounded_object_refs",
    "grounded_object_handles",
    "grounded_object_ids",
    "different_grounded_object_refs",
    "different_grounded_object_handles",
    "different_grounded_object_ids",
)
_EVIDENCE_REF_FIELDS = (
    "evidence_handles",
    "evidence_handle_refs",
    "evidence_ids",
    "evidence_ref_ids",
    "evidence_handle",
    "evidence_id",
    "evidence_ref",
    "evidence_refs",
    "evidence_references",
    "evidence",
    "scoped_evidence_ref",
    "scoped_evidence_refs",
    "scoped_evidence_handle",
    "scoped_evidence_handles",
)


def _refs(source: Any, family: str) -> Set[str]:
    mapping = _source_mapping(source)
    names = {
        "message": _MESSAGE_REF_FIELDS,
        "candidate": _CANDIDATE_REF_FIELDS,
        "evidence": _EVIDENCE_REF_FIELDS,
        "source": ("source_refs", "source_ref", "source_ids", "source_id", "registry_key", "packet_id", "context_packet_id", "root_id", "page_id", "page_hash"),
    }[family]
    result: Set[str] = set()
    for name in names:
        if name not in mapping:
            continue
        for value in _ref_values(mapping.get(name), names=(
            "message_handle",
            "message_id",
            "source_message_id",
            "fragment_id",
            "id",
            "ref_id",
            "message_ref",
            "message_refs",
            "endpoint_message_ref",
            "endpoint_message_refs",
            "endpoint_message_ids",
        ) if family == "message" else (
            "candidate_handle",
            "candidate_id",
            "candidate_ref",
            "candidate_refs",
            "competing_candidate_ref",
            "competing_candidate_refs",
            "id",
            "handle",
            "ref_id",
        ) if family == "candidate" else (
            "evidence_handle",
            "evidence_id",
            "evidence_ref_id",
            "evidence_ref",
            "evidence_refs",
            "scoped_evidence_ref",
            "scoped_evidence_refs",
            "id",
            "handle",
            "ref_id",
        ) if family == "evidence" else (
            "source_ref_id", "source_id", "packet_id", "context_packet_id", "root_id", "page_id", "page_hash", "id"
        )):
            result.add(value)
    return result


def _named_refs(source: Any, names: Sequence[str], *, value_names: Sequence[str]) -> Set[str]:
    """Collect handles from explicitly named relation fields only."""

    mapping = _source_mapping(source)
    result: Set[str] = set()
    for name in names:
        if name not in mapping:
            continue
        result.update(_ref_values(mapping.get(name), names=value_names))
    return result


def _nested_sources(source: Mapping[str, Any], names: Sequence[str]) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    """Yield only named, known metadata containers."""

    for container_name in names:
        value = source.get(container_name)
        for row in _rows(value):
            yield container_name, row
        # fixed/dynamic parts are canonical context-packet containers.  Their
        # keys are still restricted to ``names`` so arbitrary body fields are
        # never traversed.
        for parent_name in (
            "fixed_part",
            "dynamic_part",
            "selection_metadata",
            "strata_metadata",
            "candidate_context",
            "authoritative_facts_contract",
            "boundary",
        ):
            parent = source.get(parent_name)
            if not isinstance(parent, Mapping):
                continue
            nested = parent.get(container_name)
            for row in _rows(nested):
                yield container_name, row


def _handle(kind: str, value: Any) -> str:
    """Hash an input identity into an opaque, stable K28 handle."""

    raw = canonical_json(value) if isinstance(value, (Mapping, list, tuple, set, frozenset)) else str(value)
    return "k28_%s_%s" % (kind, stable_hash({"kind": kind, "value": raw})[:24])


def _opaque_handles(kind: str, values: Iterable[str]) -> List[str]:
    return sorted({_handle(kind, value) for value in values if value not in (None, "")})


def _scope_parts(value: Any) -> Tuple[str, str]:
    if isinstance(value, Mapping):
        account = _first(value, "account_id", "account", "a")
        chat = _first(value, "chat_id", "chat", "c")
        return (str(account or ""), str(chat or ""))
    if isinstance(value, str) and value and "/" in value and "|" not in value:
        account, chat = value.split("/", 1)
        return account, chat
    return "", ""


def _scope(source: Any) -> Tuple[str, str]:
    mapping = _source_mapping(source)
    parts = _scope_parts(mapping.get("scope"))
    account = str(_first(mapping, "account_id", "account") or parts[0] or "")
    chat = str(_first(mapping, "chat_id", "chat") or parts[1] or "")
    return account, chat


def _weak_only(source: Mapping[str, Any]) -> bool:
    reasons: Set[str] = set()
    for key in ("candidate_reason", "candidate_reasons", "supporting_slot_codes", "reason_codes", "reasons"):
        value = source.get(key)
        if isinstance(value, str):
            reasons.add(_normalise(value))
        elif isinstance(value, (list, tuple, set, frozenset)):
            reasons.update(_normalise(item) for item in value)
    reasons.discard("")
    strong_flag = _explicit_strong(source)
    if reasons and reasons <= _WEAK_REASON_CODES and not strong_flag:
        return True
    weak_flag = any(_truthy(source.get(key)) for key in ("time_is_weak_only", "same_segment_is_weak_only", "weak_only", "time_only", "same_segment_only"))
    return weak_flag and not strong_flag and not (reasons - _WEAK_REASON_CODES)


def _explicit_strong(source: Mapping[str, Any]) -> bool:
    """Return whether the producer explicitly overrode a weak candidate."""

    if any(_truthy(source.get(key)) for key in ("strong_relation", "is_strong", "materialized_relation", "canonical", "authoritative", "metadata_authoritative", "canonical_metadata")):
        return True
    for key in ("relation_strength", "evidence_strength", "evidence_strength_candidate", "strength", "confidence_tier"):
        value = _normalise(source.get(key))
        if value in {"strong", "authoritative", "canonical", "explicit", "resolved", "high"}:
            return True
    return False


def _candidate_is_weak(source: Mapping[str, Any]) -> bool:
    """Reject candidate-only/low-confidence rows unless explicitly promoted."""

    if _explicit_strong(source):
        return False
    if any(_truthy(source.get(key)) for key in ("candidate_only", "candidate_only_relation", "is_candidate_only")):
        return True
    relation = _normalise(_first(source, "relation_label", "relation", "relation_type", "selection_relation"))
    if relation in {"candidate_only", "candidate", "surface_context_candidate", "weak_candidate"}:
        return True
    for key in ("evidence_strength", "evidence_strength_candidate", "confidence", "confidence_tier"):
        value = _normalise(source.get(key))
        if value in {"weak", "low", "uncertain", "candidate_only", "candidate"}:
            return True
    return _weak_only(source)


def _selection_cue_only(source: Mapping[str, Any]) -> bool:
    """Return whether a row is a shallow recall cue, not semantic evidence."""

    if not isinstance(source, Mapping):
        return False
    # Core ContextPacket typed rows may retain ``candidate_only`` while also
    # carrying an explicit strong/materialized relation.  The development
    # runner's shallow projection marks its rows with ``selection_cue`` and
    # must therefore be ignored by canonical-strata inference until a later
    # semantic stage promotes it.
    return bool(_truthy(source.get("selection_cue")) and not _explicit_strong(source))


def _message_is_true_opener(message_row: Mapping[str, Any], *, fragment: str, role: str) -> bool:
    """Accept only explicit opener/greeting metadata, not an acknowledgement.

    The body-free side-car cannot inspect message text.  It can still prevent
    a common producer projection error: a context-only acknowledgement being
    marked ``is_opener`` merely because it starts a segment.  A genuine
    greeting marker or opener fragment/role remains eligible.
    """

    explicit_greeting = _truthy(message_row.get("is_greeting")) or _truthy(message_row.get("greeting_only"))
    opener_value = _normalise(message_row.get("is_opener_or_greeting"))
    explicit_opener_value = opener_value in _OPENER_VALUES
    opener_fragment_or_role = fragment in _OPENER_VALUES or role in _OPENER_VALUES
    opener_marker = (
        explicit_greeting
        or explicit_opener_value
        or _truthy(message_row.get("is_opener"))
        or opener_fragment_or_role
    )
    if role in _ACK_ROLE_VALUES and not explicit_greeting:
        return False
    return opener_marker


def _transition_has_explicit_new_topic(transition: Mapping[str, Any]) -> bool:
    """Reject ordinary contrast markers masquerading as topic transitions."""

    transition_reason = _first(transition, "topic_boundary_reason", "transition_reason", "boundary_reason", "reason_codes", "reason")
    reason_codes = {
        _normalise(item)
        for item in (_iter_values(transition_reason) if transition_reason is not None else ())
        if _normalise(item)
    }
    if reason_codes & _ORDINARY_ADVERSATIVE_VALUES:
        return False
    transition_text = _normalise(_first(transition, "cue", "marker", "transition_cue"))
    if transition_text in _ORDINARY_ADVERSATIVE_VALUES:
        return False
    return True


def _candidate_kind(value: Any) -> str:
    label = _normalise(value)
    if label in _PERSON_ALIASES or "person_history" in label:
        return "person"
    if label in _OBJECT_ALIASES or "object_history" in label:
        return "object"
    if label in _STATE_ALIASES or "state_history" in label:
        return "state"
    return ""


def _strong_evidence_type(value: Any) -> str:
    label = _normalise(value)
    aliases = {
        "explicit_canonical": "explicit_canonical_stratum",
        "explicit_canonical_stratum": "explicit_canonical_stratum",
        "canonical_metadata": "explicit_canonical_stratum",
        "candidate_history_triad": "candidate_history_triad",
        "history_triad": "candidate_history_triad",
        "candidate_competition_relation": "candidate_competition_relation",
        "competition_relation": "candidate_competition_relation",
        "explicit_competition": "candidate_competition_relation",
        "opener_fragment": "opener_fragment",
        "greeting_marker": "opener_fragment",
        "topic_transition": "topic_transition",
        "topic_boundary": "topic_transition",
        "authoritative_reply_status": "authoritative_reply_status",
        "reply_status": "authoritative_reply_status",
        "explicit_reply_status": "authoritative_reply_status",
    }
    return aliases.get(label, "")


@dataclass(frozen=True)
class StratumEvidence:
    """One body-free, strong evidence item for one canonical stratum."""

    stratum: str
    evidence_type: str
    source_kind: str
    message_refs: Tuple[str, ...] = ()
    candidate_refs: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    source_refs: Tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    reason_codes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        message_handles = _opaque_handles("message", self.message_refs)
        candidate_handles = _opaque_handles("candidate", self.candidate_refs)
        evidence_handles = _opaque_handles("evidence", self.evidence_refs)
        source_handles = _opaque_handles("source", self.source_refs)
        counts = {
            "messages": len(message_handles),
            "candidates": len(candidate_handles),
            "evidence": len(evidence_handles),
            "sources": len(source_handles),
        }
        return {
            "evidence_type": self.evidence_type,
            "source_kind": self.source_kind,
            "message_handles": message_handles,
            "candidate_handles": candidate_handles,
            "evidence_handles": evidence_handles,
            "source_handles": source_handles,
            "reason_codes": sorted({_normalise(value) for value in self.reason_codes if _normalise(value)}),
            "counts": counts,
            "evidence_hash": stable_hash(
                {
                    "stratum": self.stratum,
                    "evidence_type": self.evidence_type,
                    "source_kind": self.source_kind,
                    "message_handles": message_handles,
                    "candidate_handles": candidate_handles,
                    "evidence_handles": evidence_handles,
                    "source_handles": source_handles,
                    "reason_codes": sorted({_normalise(value) for value in self.reason_codes if _normalise(value)}),
                    "counts": counts,
                }
            ),
        }


@dataclass(frozen=True)
class PageStrataMetadata:
    """Public body-free projection for one page/root."""

    page_handle: str
    root_handle: str
    source_handle: str
    scope_handle: str
    scope_known: bool
    ordinal: int
    strata: Mapping[str, Mapping[str, Any]]
    status: str
    observed_strata: Tuple[str, ...]
    metadata_missing_strata: Tuple[str, ...]
    ambiguous_strata: Tuple[str, ...]
    ambiguity_codes: Tuple[str, ...] = ()
    metadata_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "page_handle": self.page_handle,
            "root_handle": self.root_handle,
            "source_handle": self.source_handle,
            "scope_handle": self.scope_handle,
            "scope_known": bool(self.scope_known),
            "ordinal": int(self.ordinal),
            "strata": {str(name): dict(self.strata[name]) for name in CANONICAL_STRATA},
            "status": self.status,
            "observed_strata": list(self.observed_strata),
            "metadata_missing_strata": list(self.metadata_missing_strata),
            "ambiguous_strata": list(self.ambiguous_strata),
            "ambiguity_codes": list(self.ambiguity_codes),
            "body_free": True,
        }
        value["metadata_hash"] = self.metadata_hash or stable_hash(value)
        return value


@dataclass(frozen=True)
class _PageInternal:
    public: PageStrataMetadata
    raw_key: str
    raw_scope: Tuple[str, str]


def _empty_stratum(name: str, status: str = "metadata_missing", *, reasons: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "stratum": name,
        "status": status,
        "required_upstream_fields": list(REQUIRED_UPSTREAM_FIELDS.get(name, ())),
        "evidence_types": [],
        "evidence": [],
        "message_handles": [],
        "candidate_handles": [],
        "evidence_handles": [],
        "counts": {"messages": 0, "candidates": 0, "evidence": 0, "evidence_items": 0},
        "reason_codes": list(reasons),
    }


def _add_explicit_labels(
    source: Mapping[str, Any],
    *,
    source_kind: str,
    inherited_messages: Set[str],
    inherited_candidates: Set[str],
    inherited_evidence: Set[str],
    inherited_sources: Set[str],
    evidence: List[StratumEvidence],
    ambiguous: Set[str],
    ambiguity_codes: Set[str],
) -> None:
    if _selection_cue_only(source):
        return
    for raw_key, raw_value in source.items():
        key = _normalise(raw_key)
        if key in _LABEL_KEYS:
            for value in _iter_values(raw_value):
                if isinstance(value, Mapping):
                    candidate = _first(value, "stratum", "category", "name", "label")
                    mapped = canonical_stratum(candidate)
                    if mapped:
                        evidence_type = _strong_evidence_type(_first(value, "evidence_type", "evidence_kind")) or "explicit_canonical_stratum"
                        if not _truthy(value.get("strong", True)) and evidence_type != "explicit_canonical_stratum":
                            continue
                        local_messages = inherited_messages | _refs(value, "message")
                        local_candidates = inherited_candidates | _refs(value, "candidate")
                        local_evidence = inherited_evidence | _refs(value, "evidence")
                        local_sources = inherited_sources | _refs(value, "source")
                        if local_messages or local_candidates or local_evidence:
                            evidence.append(StratumEvidence(mapped, evidence_type, source_kind, tuple(local_messages), tuple(local_candidates), tuple(local_evidence), tuple(local_sources)))
                    continue
                mapped = canonical_stratum(value)
                if mapped and (inherited_messages or inherited_candidates or inherited_evidence):
                    evidence.append(StratumEvidence(mapped, "explicit_canonical_stratum", source_kind, tuple(inherited_messages), tuple(inherited_candidates), tuple(inherited_evidence), tuple(inherited_sources)))
        elif key in _DIRECT_STRATUM_KEYS and _truthy(raw_value):
            # A typed sidecar is commonly represented as a list of rows.  Do
            # not treat a candidate-only list as a page-level assertion just
            # because the page has unrelated inherited anchors.  Strong rows
            # may still assert their own canonical stratum with their local
            # refs.
            if isinstance(raw_value, (list, tuple, set, frozenset)):
                for child in _iter_values(raw_value):
                    if not isinstance(child, Mapping) or _selection_cue_only(child):
                        continue
                    local_messages = inherited_messages | _refs(child, "message")
                    local_candidates = inherited_candidates | _refs(child, "candidate")
                    local_evidence = inherited_evidence | _refs(child, "evidence")
                    local_sources = inherited_sources | _refs(child, "source")
                    if local_messages or local_candidates or local_evidence:
                        evidence.append(StratumEvidence(key, "explicit_canonical_stratum", source_kind, tuple(local_messages), tuple(local_candidates), tuple(local_evidence), tuple(local_sources)))
            elif isinstance(raw_value, Mapping):
                if _selection_cue_only(raw_value):
                    continue
                local_messages = inherited_messages | _refs(raw_value, "message")
                local_candidates = inherited_candidates | _refs(raw_value, "candidate")
                local_evidence = inherited_evidence | _refs(raw_value, "evidence")
                local_sources = inherited_sources | _refs(raw_value, "source")
                if local_messages or local_candidates or local_evidence:
                    evidence.append(StratumEvidence(key, "explicit_canonical_stratum", source_kind, tuple(local_messages), tuple(local_candidates), tuple(local_evidence), tuple(local_sources)))
            elif inherited_messages or inherited_candidates or inherited_evidence:
                evidence.append(StratumEvidence(key, "explicit_canonical_stratum", source_kind, tuple(inherited_messages), tuple(inherited_candidates), tuple(inherited_evidence), tuple(inherited_sources)))
        elif key in _AMBIGUITY_KEYS and _nonempty(raw_value):
            values = list(_iter_values(raw_value))
            mapped_values = {canonical_stratum(value) for value in values if canonical_stratum(value)}
            if not mapped_values:
                mapped_values = set(CANONICAL_STRATA)
            ambiguous.update(mapped_values)
            ambiguity_codes.add("explicit_metadata_conflict")


def _candidate_row_kind(container: str, row: Mapping[str, Any]) -> str:
    kind = _candidate_kind(container)
    if kind:
        return kind
    for key in ("view", "view_name", "view_names", "candidate_reason", "candidate_reasons", "relation_subtype", "candidate_kind", "kind"):
        value = row.get(key)
        for child in _iter_values(value):
            kind = _candidate_kind(child)
            if kind:
                return kind
    return ""


def _candidate_rows_from_source(source: Mapping[str, Any]) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    for name, row in _nested_sources(source, _CANDIDATE_ROW_KEYS):
        yield name, row


def _message_rows_from_source(source: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for _, row in _nested_sources(source, _MESSAGE_ROW_KEYS):
        yield row


def _transition_rows_from_source(source: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for name in _TRANSITION_ROW_KEYS:
        value = source.get(name)
        for row in _rows(value):
            yield row
        for parent_name in (
            "fixed_part",
            "dynamic_part",
            "selection_metadata",
            "strata_metadata",
            "candidate_context",
            "authoritative_facts_contract",
            "boundary",
        ):
            parent = source.get(parent_name)
            if isinstance(parent, Mapping):
                for row in _rows(parent.get(name)):
                    yield row


def _reply_rows_from_source(source: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    # A scalar reply_status lives on the source row itself.  Structured reply
    # evidence is handled as a named row and never causes absence inference.
    if any(name in source for name in _REPLY_ROW_KEYS):
        yield source
    for name in _REPLY_ROW_KEYS:
        value = source.get(name)
        if isinstance(value, Mapping):
            # ``reply_status`` may itself be the authoritative row, e.g.
            # ``{"authoritative_status": "awaiting_reply", ...}``.
            # Preserve that typed row for the strict checks below; do not
            # infer a missing reply from open boundaries or counts.
            yield value
        for row in _rows(value):
            yield row
    for parent_name in (
        "fixed_part",
        "dynamic_part",
        "selection_metadata",
        "strata_metadata",
        "candidate_context",
        "authoritative_facts_contract",
        "boundary",
    ):
        parent = source.get(parent_name)
        if not isinstance(parent, Mapping):
            continue
        for name in _REPLY_ROW_KEYS:
            value = parent.get(name)
            if isinstance(value, Mapping):
                # Preserve the typed row even when it contains only
                # authoritative_status/message_ref/scoped_evidence_ref.
                yield value
            for row in _rows(value):
                yield row


def _status_value(source: Mapping[str, Any]) -> str:
    for key in (
        "reply_status",
        "reply_state",
        "response_status",
        "answer_status",
        "authoritative_status",
        "status",
        "value",
    ):
        value = source.get(key)
        if isinstance(value, str):
            label = _normalise(value)
            if label in _NO_REPLY_VALUES or label in _REPLIED_VALUES:
                return label
        elif isinstance(value, Mapping):
            # Structured reply-status contracts keep the status under a
            # named authoritative field.  Only that explicit value is read.
            nested = _status_value(value)
            if nested:
                return nested
    return ""


def _lookup_row(store_tables: Mapping[str, Mapping[str, Any]], family: str, handle: str) -> Mapping[str, Any]:
    table = store_tables.get(family, {})
    if handle in table:
        return table[handle]
    for row in table.values():
        value = _first(row, "message_handle", "candidate_handle", "evidence_handle", "handle", "id")
        if value not in (None, "") and str(value) == str(handle):
            return row
    return {}


def _store_tables(store: Any) -> Dict[str, Mapping[str, Mapping[str, Any]]]:
    return {
        "message": _table(store, "message_table", "messages", "message_rows"),
        "candidate": _table(store, "candidate_table", "candidates", "candidate_rows"),
        "evidence": _table(store, "evidence_table", "evidence", "evidence_rows"),
    }


def _root_for_page(root_map: Mapping[str, Mapping[str, Any]], page: Mapping[str, Any]) -> Mapping[str, Any]:
    root_id = _first(page, "root_id", "root_handle")
    if root_id not in (None, "") and str(root_id) in root_map:
        return root_map[str(root_id)]
    source_id = _first(page, "source_packet_id", "source_ref", "packet_id")
    if source_id not in (None, "") and str(source_id) in root_map:
        return root_map[str(source_id)]
    return {}


def _root_and_page_inputs(source: Any, *, pages: Any = None, roots: Any = None, packets: Any = None) -> Tuple[List[Mapping[str, Any]], Dict[str, Mapping[str, Any]], Dict[str, Mapping[str, Any]], Any]:
    """Normalize a store/mapping/sequence without calling a bodyful export."""

    store = source if source is not None and (hasattr(source, "pages") or hasattr(source, "page_table") or isinstance(source, Mapping) and any(key in source for key in ("message_table", "candidate_table", "page_table", "pages"))) else None
    if pages is None and store is not None:
        if hasattr(store, "pages"):
            pages = getattr(store, "pages")
        elif isinstance(store, Mapping):
            pages = store.get("pages", store.get("page_table"))
    if roots is None and store is not None:
        if hasattr(store, "roots"):
            roots = getattr(store, "roots")
        elif isinstance(store, Mapping):
            roots = store.get("roots", store.get("root_table"))
    if packets is None and source is not None and not store:
        packets = source
    if pages is None and packets is not None:
        pages = packets
    if isinstance(pages, Mapping):
        if any(key in pages for key in ("page_id", "page_hash", "packet_id", "context_packet_id")):
            page_rows = [pages]
        else:
            page_rows = [row for row in pages.values() if isinstance(row, Mapping)]
    elif isinstance(pages, (list, tuple, set, frozenset)):
        page_rows = [row for row in pages if isinstance(row, Mapping)]
    else:
        page_rows = []
    if isinstance(roots, Mapping):
        if any(key in roots for key in ("root_id", "packet_id", "source_packet_id")):
            root_rows = [roots]
        else:
            root_rows = [row for row in roots.values() if isinstance(row, Mapping)]
    elif isinstance(roots, (list, tuple, set, frozenset)):
        root_rows = [row for row in roots if isinstance(row, Mapping)]
    else:
        root_rows = []
    root_map: Dict[str, Mapping[str, Any]] = {}
    for row in root_rows:
        for key in ("root_id", "packet_id", "source_packet_id", "context_packet_id"):
            value = row.get(key)
            if value not in (None, ""):
                root_map[str(value)] = row
    packet_map: Dict[str, Mapping[str, Any]] = {}
    if packets is not None:
        if isinstance(packets, Mapping):
            packet_rows = [packets] if any(key in packets for key in ("packet_id", "context_packet_id", "packet_hash")) else [row for row in packets.values() if isinstance(row, Mapping)]
        elif isinstance(packets, (list, tuple, set, frozenset)):
            packet_rows = [row for row in packets if isinstance(row, Mapping)]
        else:
            packet_rows = []
        for row in packet_rows:
            value = _first(row, "packet_id", "context_packet_id", "source_packet_id", "root_id")
            if value not in (None, ""):
                packet_map[str(value)] = row
    return page_rows, root_map, packet_map, store


def _page_key(page: Mapping[str, Any], index: int) -> str:
    value = _first(page, "page_id", "page_hash", "root_id", "packet_id", "context_packet_id", "source_packet_id")
    return str(value) if value not in (None, "") else "ordinal:%08d" % index


def _make_page_internal(
    page: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    packet: Mapping[str, Any],
    store_tables: Mapping[str, Mapping[str, Mapping[str, Any]]],
    source_kind: str,
    ordinal: int,
) -> _PageInternal:
    page_key = _page_key(page, ordinal)
    root_key = str(_first(root, "root_id", "packet_id", "source_packet_id", "context_packet_id") or _first(page, "root_id", "packet_id", "source_packet_id") or page_key)
    source_key = str(_first(packet, "packet_id", "context_packet_id", "source_packet_id", "root_id") or _first(page, "source_packet_id", "packet_id", "root_id") or root_key)
    raw_scope = _scope(page)
    if not raw_scope[0] or not raw_scope[1]:
        raw_scope = _scope(root)
    if not raw_scope[0] or not raw_scope[1]:
        raw_scope = _scope(packet)
    page_messages = _refs(page, "message")
    page_candidates = _refs(page, "candidate")
    page_evidence = _refs(page, "evidence")
    page_sources = _refs(page, "source") | _refs(root, "source") | _refs(packet, "source")
    source_rows: List[Tuple[str, Mapping[str, Any]]] = [("page", page)]
    if root:
        source_rows.append(("linear_root", root))
        source_template = root.get("source_template")
        if isinstance(source_template, Mapping):
            # K29 keeps non-structural producer metadata in this reversible
            # template.  Walk it through the same whitelist as a source row;
            # do not decode or inspect any body-bearing value.
            source_rows.append(("linear_root_source_template", source_template))
    if packet and packet is not page:
        source_rows.append(("context_packet", packet))

    evidence_items: List[StratumEvidence] = []
    ambiguous: Set[str] = set()
    ambiguity_codes: Set[str] = set()
    history: Dict[str, Dict[str, Set[str]]] = {
        "person": {"message": set(), "candidate": set(), "evidence": set(), "source": set()},
        "object": {"message": set(), "candidate": set(), "evidence": set(), "source": set()},
        "state": {"message": set(), "candidate": set(), "evidence": set(), "source": set()},
    }

    def add(item: StratumEvidence) -> None:
        if item.stratum not in CANONICAL_STRATA or not _strong_evidence_type(item.evidence_type):
            return
        if not (item.message_refs or item.candidate_refs or item.evidence_refs):
            return
        # Canonical labels are strong metadata, but they still need the
        # minimum opaque anchors that make the asserted stratum auditable.
        # In particular, a competition label on one candidate (or with no
        # evidence) is not a competition relation; this prevents a producer
        # from laundering candidate volume into semantics.
        if item.stratum == "candidate_competition" and item.evidence_type == "explicit_canonical_stratum":
            if len(set(item.candidate_refs)) < 2 or not item.evidence_refs:
                return
        if item.stratum == "topic_shift" and item.evidence_type == "explicit_canonical_stratum":
            if len(set(item.message_refs)) < 2 and not item.evidence_refs:
                return
        evidence_items.append(item)

    for row_kind, source_row in source_rows:
        source_kind_for_row = source_kind if row_kind == "page" else row_kind
        local_messages = page_messages | _refs(source_row, "message")
        local_candidates = page_candidates | _refs(source_row, "candidate")
        local_evidence = page_evidence | _refs(source_row, "evidence")
        local_sources = page_sources | _refs(source_row, "source")
        _add_explicit_labels(
            source_row,
            source_kind=source_kind_for_row,
            inherited_messages=local_messages,
            inherited_candidates=local_candidates,
            inherited_evidence=local_evidence,
            inherited_sources=local_sources,
            evidence=evidence_items,
            ambiguous=ambiguous,
            ambiguity_codes=ambiguity_codes,
        )

        for message_row in _message_rows_from_source(source_row):
            message_refs = local_messages | _refs(message_row, "message")
            candidate_refs = local_candidates | _refs(message_row, "candidate")
            evidence_refs = local_evidence | _refs(message_row, "evidence")
            source_refs = local_sources | _refs(message_row, "source")
            _add_explicit_labels(
                message_row,
                source_kind=source_kind_for_row,
                inherited_messages=message_refs,
                inherited_candidates=candidate_refs,
                inherited_evidence=evidence_refs,
                inherited_sources=source_refs,
                evidence=evidence_items,
                ambiguous=ambiguous,
                ambiguity_codes=ambiguity_codes,
            )
            if _selection_cue_only(message_row):
                continue
            fragment = _normalise(_first(message_row, "fragment_type", "message_type"))
            role = _normalise(_first(message_row, "dialogue_role", "message_role", "role"))
            opener_marker = _message_is_true_opener(message_row, fragment=fragment, role=role)
            if opener_marker and message_refs:
                add(StratumEvidence("greeting_new_topic", "opener_fragment", source_kind_for_row, tuple(message_refs), tuple(candidate_refs), tuple(evidence_refs), tuple(source_refs)))
            if (_truthy(message_row.get("topic_shift")) or _truthy(message_row.get("topic_change")) or _truthy(message_row.get("topic_boundary")) or _truthy(message_row.get("new_topic_boundary"))) and message_refs:
                add(StratumEvidence("topic_shift", "topic_transition", source_kind_for_row, tuple(message_refs), tuple(candidate_refs), tuple(evidence_refs), tuple(source_refs)))
            reply_status = _status_value(message_row)
            if reply_status in _NO_REPLY_VALUES and message_refs:
                add(StratumEvidence("no_reply", "authoritative_reply_status", source_kind_for_row, tuple(message_refs), tuple(candidate_refs), tuple(evidence_refs), tuple(source_refs)))

        for container, candidate_row in _candidate_rows_from_source(source_row):
            message_refs = local_messages | _refs(candidate_row, "message")
            row_candidate_refs = _refs(candidate_row, "candidate")
            candidate_refs = local_candidates | row_candidate_refs
            evidence_refs = local_evidence | _refs(candidate_row, "evidence")
            source_refs = local_sources | _refs(candidate_row, "source")
            _add_explicit_labels(
                candidate_row,
                source_kind=source_kind_for_row,
                inherited_messages=message_refs,
                inherited_candidates=candidate_refs,
                inherited_evidence=evidence_refs,
                inherited_sources=source_refs,
                evidence=evidence_items,
                ambiguous=ambiguous,
                ambiguity_codes=ambiguity_codes,
            )
            kind = _candidate_row_kind(container, candidate_row)
            # History views are only strong when the row carries a candidate
            # ref and scoped evidence.  A view name or candidate count alone
            # cannot create the pronoun stratum.
            row_evidence_refs = _refs(candidate_row, "evidence")
            # Candidate-only/low-confidence history views are useful input
            # for later semantic work, but are not canonical strata evidence.
            # Require each kind to carry its own scoped evidence so a page
            # level evidence count cannot promote an unrelated candidate.
            if kind and row_candidate_refs and row_evidence_refs and not _candidate_is_weak(candidate_row):
                history[kind]["message"].update(message_refs)
                history[kind]["candidate"].update(row_candidate_refs)
                history[kind]["evidence"].update(row_evidence_refs)
                history[kind]["source"].update(source_refs)

            relation_values = {
                _normalise(_first(candidate_row, "relation_label", "relation", "relation_type", "candidate_type", "candidate_set_type", "selection_relation")),
                _normalise(_first(candidate_row, "relation_subtype")),
                _normalise(_first(candidate_row, "explicit_competition_relation")),
            }
            relation_values.discard("")
            exclusive = any(_truthy(candidate_row.get(key)) for key in ("mutually_exclusive", "exclusive", "candidate_competition", "competing", "explicit_competition_relation"))
            competing_refs = row_candidate_refs | _named_refs(
                candidate_row,
                _COMPETING_CANDIDATE_REF_FIELDS,
                value_names=("candidate_handle", "candidate_id", "candidate_ref", "id", "handle", "ref_id"),
            )
            grounded_fields_present = any(name in candidate_row for name in _GROUNDED_OBJECT_REF_FIELDS)
            grounded_refs = _named_refs(
                candidate_row,
                _GROUNDED_OBJECT_REF_FIELDS,
                value_names=("object_handle", "object_id", "object_ref", "id", "handle", "ref_id"),
            )
            # A competition assertion must identify at least two candidate
            # handles and carry evidence.  It is never inferred from volume.
            if (
                (relation_values & _COMPETITION_VALUES or exclusive)
                and len(competing_refs) >= 2
                and row_evidence_refs
                and (not grounded_fields_present or len(grounded_refs) >= 2)
                and not _candidate_is_weak(candidate_row)
            ):
                add(StratumEvidence("candidate_competition", "candidate_competition_relation", source_kind_for_row, tuple(message_refs), tuple(competing_refs), tuple(row_evidence_refs), tuple(source_refs)))
            status = _status_value(candidate_row)
            if status in _NO_REPLY_VALUES and message_refs:
                add(StratumEvidence("no_reply", "authoritative_reply_status", source_kind_for_row, tuple(message_refs), tuple(candidate_refs), tuple(evidence_refs), tuple(source_refs)))

        for transition in _transition_rows_from_source(source_row):
            if _selection_cue_only(transition):
                continue
            message_refs = local_messages | _refs(transition, "message")
            candidate_refs = local_candidates | _refs(transition, "candidate")
            evidence_refs = local_evidence | _refs(transition, "evidence")
            source_refs = local_sources | _refs(transition, "source")
            # Topic ids are evidence metadata but are not substituted for
            # endpoint message handles.  A transition needs two endpoints or
            # an explicit transition handle plus evidence.
            endpoint_count = len(message_refs)
            has_transition_marker = any(
                _truthy(transition.get(key))
                for key in (
                    "topic_shift",
                    "topic_change",
                    "topic_boundary",
                    "topic_shift_or_boundary",
                    "new_topic_boundary",
                    "explicit_transition",
                    "canonical",
                )
            ) or _normalise(_first(transition, "transition_type", "kind", "type")) in {"topic_shift", "topic_change", "topic_transition", "topic_boundary"}
            transition_reason = _first(transition, "topic_boundary_reason", "transition_reason", "boundary_reason", "reason_codes", "reason")
            reason_codes = tuple(
                _normalise(item)
                for item in (_iter_values(transition_reason) if transition_reason is not None else ())
                if _normalise(item)
            )
            if has_transition_marker and _transition_has_explicit_new_topic(transition) and endpoint_count >= 2 and evidence_refs:
                add(StratumEvidence("topic_shift", "topic_transition", source_kind_for_row, tuple(message_refs), tuple(candidate_refs), tuple(evidence_refs), tuple(source_refs), reason_codes=reason_codes))

        for reply_row in _reply_rows_from_source(source_row):
            if _selection_cue_only(reply_row):
                continue
            status = _status_value(reply_row)
            if status not in _NO_REPLY_VALUES:
                continue
            message_refs = local_messages | _refs(reply_row, "message")
            candidate_refs = local_candidates | _refs(reply_row, "candidate")
            evidence_refs = local_evidence | _refs(reply_row, "evidence")
            source_refs = local_sources | _refs(reply_row, "source")
            reply_shape_is_authoritative = (
                _status_value(reply_row) in _NO_REPLY_VALUES
                and any(key in reply_row for key in ("authoritative_status", "message_ref", "scoped_evidence_ref"))
            )
            if message_refs and (
                _normalise(_first(reply_row, "evidence_type", "evidence_kind"))
                in {"reply_status", "authoritative_reply_status", "explicit_reply_status"}
                or reply_row is source_row
                or reply_shape_is_authoritative
            ):
                add(StratumEvidence("no_reply", "authoritative_reply_status", source_kind_for_row, tuple(message_refs), tuple(candidate_refs), tuple(evidence_refs), tuple(source_refs)))

    # Linear roots retain candidate_views as an authoritative view index.  We
    # still require each view's candidate records to have scoped evidence.
    for container, values in (root.get("candidate_views", {}) if isinstance(root.get("candidate_views"), Mapping) else {}).items():
        kind = _candidate_row_kind(str(container), {})
        if not kind:
            continue
        handles = {str(value) for value in values if value not in (None, "")} if isinstance(values, (list, tuple, set, frozenset)) else set()
        for handle in handles:
            row = _lookup_row(store_tables, "candidate", handle)
            if not row:
                continue
            row_candidates = {handle} | _refs(row, "candidate")
            row_evidence = _refs(row, "evidence")
            if not row_evidence or _candidate_is_weak(row):
                continue
            history[kind]["message"].update(page_messages | _refs(row, "message"))
            history[kind]["candidate"].update(row_candidates)
            history[kind]["evidence"].update(row_evidence)
            history[kind]["source"].update(page_sources | _refs(row, "source"))

    triad_message_refs = set().union(*(history[kind]["message"] for kind in ("person", "object", "state")))
    if (
        all(
            history[kind]["message"] and history[kind]["candidate"] and history[kind]["evidence"]
            for kind in ("person", "object", "state")
        )
        and len(triad_message_refs) >= 2
    ):
        add(
            StratumEvidence(
                "pronoun_person_object_state",
                "candidate_history_triad",
                source_kind,
                tuple(triad_message_refs),
                tuple(set().union(*(history[kind]["candidate"] for kind in ("person", "object", "state")))),
                tuple(set().union(*(history[kind]["evidence"] for kind in ("person", "object", "state")))),
                tuple(set().union(*(history[kind]["source"] for kind in ("person", "object", "state")))),
            )
        )

    grouped: Dict[str, List[StratumEvidence]] = defaultdict(list)
    for item in evidence_items:
        if item.stratum == "candidate_competition" and item.evidence_type == "explicit_canonical_stratum":
            if len(set(item.candidate_refs)) < 2 or not item.evidence_refs:
                continue
        if item.stratum == "topic_shift" and item.evidence_type == "explicit_canonical_stratum":
            if len(set(item.message_refs)) < 2 and not item.evidence_refs:
                continue
        grouped[item.stratum].append(item)
    strata: Dict[str, Mapping[str, Any]] = {}
    for name in CANONICAL_STRATA:
        if name in ambiguous:
            value = _empty_stratum(name, "ambiguous", reasons=sorted(ambiguity_codes) or ["explicit_metadata_conflict"])
            value["ambiguous"] = True
            strata[name] = value
            continue
        unique: Dict[str, StratumEvidence] = {}
        for item in grouped.get(name, ()):
            unique[stable_hash(item.to_dict())] = item
        if not unique:
            strata[name] = _empty_stratum(name)
            continue
        item_dicts = [unique[key].to_dict() for key in sorted(unique)]
        message_handles = sorted({handle for item in item_dicts for handle in item["message_handles"]})
        candidate_handles = sorted({handle for item in item_dicts for handle in item["candidate_handles"]})
        evidence_handles = sorted({handle for item in item_dicts for handle in item["evidence_handles"]})
        strata[name] = {
            "stratum": name,
            "status": "observed",
            "required_upstream_fields": list(REQUIRED_UPSTREAM_FIELDS.get(name, ())),
            "evidence_types": sorted({str(item["evidence_type"]) for item in item_dicts}),
            "evidence": item_dicts,
            "message_handles": message_handles,
            "candidate_handles": candidate_handles,
            "evidence_handles": evidence_handles,
            "counts": {
                "messages": len(message_handles),
                "candidates": len(candidate_handles),
                "evidence": len(evidence_handles),
                "evidence_items": len(item_dicts),
            },
            "reason_codes": [],
        }

    observed = tuple(name for name in CANONICAL_STRATA if strata[name]["status"] == "observed")
    missing = tuple(name for name in CANONICAL_STRATA if strata[name]["status"] == "metadata_missing")
    ambiguous_names = tuple(name for name in CANONICAL_STRATA if strata[name]["status"] == "ambiguous")
    status = "observed" if observed else "ambiguous" if ambiguous_names else "metadata_missing"
    public_seed = {
        "page_handle": _handle("page", page_key),
        "root_handle": _handle("root", root_key),
        "source_handle": _handle("source", source_key),
        "scope_handle": _handle("scope", "%s/%s" % raw_scope) if raw_scope[0] and raw_scope[1] else _handle("scope", "missing"),
        "scope_known": bool(raw_scope[0] and raw_scope[1]),
        "ordinal": ordinal,
        "strata": strata,
        "status": status,
        "observed_strata": list(observed),
        "metadata_missing_strata": list(missing),
        "ambiguous_strata": list(ambiguous_names),
        "ambiguity_codes": sorted(ambiguity_codes),
        "body_free": True,
    }
    public = PageStrataMetadata(
        page_handle=public_seed["page_handle"],
        root_handle=public_seed["root_handle"],
        source_handle=public_seed["source_handle"],
        scope_handle=public_seed["scope_handle"],
        scope_known=bool(raw_scope[0] and raw_scope[1]),
        ordinal=ordinal,
        strata=strata,
        status=status,
        observed_strata=observed,
        metadata_missing_strata=missing,
        ambiguous_strata=ambiguous_names,
        ambiguity_codes=tuple(sorted(ambiguity_codes)),
        metadata_hash=stable_hash(public_seed),
    )
    return _PageInternal(public=public, raw_key=page_key, raw_scope=raw_scope)


def _assert_body_free(value: Any) -> None:
    hits: List[str] = []

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key.casefold() in _BODY_KEYS and _nonempty(child):
                    hits.append(path + key)
                visit(child, path + key + ".")
        elif isinstance(item, (list, tuple, set, frozenset)):
            for index, child in enumerate(item):
                visit(child, path + str(index) + ".")

    visit(value)
    if hits:
        raise SelectionStrataError("body_free_violation")


def _metadata_page_rows(source: Any, *, pages: Any = None, roots: Any = None, packets: Any = None) -> Tuple[List[_PageInternal], Any]:
    page_rows, root_map, packet_map, store = _root_and_page_inputs(source, pages=pages, roots=roots, packets=packets)
    store_tables = _store_tables(store)
    internals: List[_PageInternal] = []
    for ordinal, page in enumerate(page_rows):
        root = _root_for_page(root_map, page)
        packet_key = _first(page, "source_packet_id", "source_ref", "packet_id", "context_packet_id", "root_id")
        packet = packet_map.get(str(packet_key), {}) if packet_key not in (None, "") else {}
        if not packet and not root and packet_key not in (None, ""):
            packet = packet_map.get(str(_first(page, "packet_id", "context_packet_id")), {})
        internals.append(
            _make_page_internal(
                page,
                root=root,
                packet=packet,
                store_tables=store_tables,
                source_kind="linear_stage_packet" if root or store is not None else "context_packet",
                ordinal=ordinal + 1,
            )
        )
    return internals, store


def _scope_summary(internals: Sequence[_PageInternal]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[_PageInternal]] = defaultdict(list)
    for item in internals:
        groups[item.public.scope_handle].append(item)
    output: Dict[str, Dict[str, Any]] = {}
    for scope_handle in sorted(groups):
        rows = groups[scope_handle]
        counts = Counter(name for row in rows for name in row.public.observed_strata)
        output[scope_handle] = {
            "scope_handle": scope_handle,
            "scope_known": all(row.public.scope_known for row in rows),
            "page_count": len(rows),
            "page_handles": sorted(row.public.page_handle for row in rows),
            "available_strata": [name for name in CANONICAL_STRATA if counts.get(name, 0)],
            "stratum_page_counts": {name: int(counts.get(name, 0)) for name in CANONICAL_STRATA},
        }
    return output


def build_canonical_strata_metadata(
    source: Any = None,
    *,
    pages: Any = None,
    roots: Any = None,
    packets: Any = None,
    store: Any = None,
) -> Dict[str, Any]:
    """Build body-free per-page canonical strata facts.

    ``source`` may be a sequence/mapping of context packets, a mapping with
    ``pages``/``roots`` tables, or a ``LinearStagePacketStore``.  Passing a
    store separately is useful when ``pages`` and ``roots`` are already a
    body-free projection.  The store is accessed through its handle tables;
    no bodyful ``to_dict`` export is requested.
    """

    if store is not None:
        # Keep the call-site explicit while preserving the convenient source
        # positional argument for packet lists.
        source_for_inputs = source if source is not None else store
        internals, _ = _metadata_page_rows(source_for_inputs, pages=pages, roots=roots, packets=packets)
        # If callers supplied a projected page list plus a store, the first
        # normalization cannot see the separate table.  Re-run with a small
        # mapping that carries only the allowed tables.
        if store is not source and not hasattr(source_for_inputs, "page_table") and pages is not None:
            internals, _ = _metadata_page_rows(
                {"pages": pages, "roots": roots or (), "message_table": _table(store, "message_table", "messages"), "candidate_table": _table(store, "candidate_table", "candidates"), "evidence_table": _table(store, "evidence_table", "evidence")},
                pages=pages,
                roots=roots,
            )
    else:
        internals, _ = _metadata_page_rows(source, pages=pages, roots=roots, packets=packets)
    page_rows = [item.public.to_dict() for item in internals]
    summary: Dict[str, Dict[str, Any]] = {}
    status_counts = Counter(item.public.status for item in internals)
    stratum_summary: Dict[str, Dict[str, Any]] = {}
    for name in CANONICAL_STRATA:
        observed = [item for item in internals if item.public.strata[name]["status"] == "observed"]
        ambiguous = [item for item in internals if item.public.strata[name]["status"] == "ambiguous"]
        missing = [item for item in internals if item.public.strata[name]["status"] == "metadata_missing"]
        stratum_summary[name] = {
            "stratum": name,
            "required_upstream_fields": list(REQUIRED_UPSTREAM_FIELDS.get(name, ())),
            "observed_page_count": len(observed),
            "ambiguous_page_count": len(ambiguous),
            "metadata_missing_page_count": len(missing),
            "page_handles": sorted(item.public.page_handle for item in observed),
            "evidence_item_count": sum(int(item.public.strata[name]["counts"]["evidence_items"]) for item in observed),
            "message_handle_count": sum(int(item.public.strata[name]["counts"]["messages"]) for item in observed),
            "candidate_handle_count": sum(int(item.public.strata[name]["counts"]["candidates"]) for item in observed),
            "evidence_handle_count": sum(int(item.public.strata[name]["counts"]["evidence"]) for item in observed),
        }
    metadata_seed = {
        "schema_version": STRATA_SCHEMA_VERSION,
        "pipeline_version": STRATA_PIPELINE_VERSION,
        "pages": page_rows,
        "strata": stratum_summary,
    }
    metadata_hash = stable_hash(metadata_seed)
    report: Dict[str, Any] = {
        "schema_version": STRATA_SCHEMA_VERSION,
        "pipeline_version": STRATA_PIPELINE_VERSION,
        "canonical_strata": list(CANONICAL_STRATA),
        "body_free": True,
        "frozen_read": False,
        "provider_called": False,
        "page_count": len(page_rows),
        "classified_page_count": sum(1 for item in internals if item.public.observed_strata),
        "ambiguous_page_count": sum(1 for item in internals if item.public.ambiguous_strata),
        "metadata_missing_page_count": sum(1 for item in internals if item.public.status == "metadata_missing"),
        "status_counts": dict(sorted(status_counts.items())),
        "available_strata": [name for name in CANONICAL_STRATA if stratum_summary[name]["observed_page_count"]],
        "missing_strata": [name for name in CANONICAL_STRATA if not stratum_summary[name]["observed_page_count"]],
        "required_upstream_fields": {name: list(REQUIRED_UPSTREAM_FIELDS.get(name, ())) for name in CANONICAL_STRATA},
        "strata": stratum_summary,
        "pages": page_rows,
        "scope_coverage": _scope_summary(internals),
        "metadata_hash": metadata_hash,
        "replay": {
            "stable_hash": metadata_hash,
            "idempotent_contract": True,
            "source_order_independent": True,
        },
    }
    _assert_body_free(report)
    return report


def materialize_context_packet_strata(packets: Any) -> Dict[str, Any]:
    """Explicit upstream adapter for ContextPacket mappings."""

    return build_canonical_strata_metadata(packets, packets=packets)


def materialize_linear_stage_packet_strata(store: Any, *, pages: Any = None, roots: Any = None) -> Dict[str, Any]:
    """Project a LinearStagePacketStore without changing its artifact schema."""

    return build_canonical_strata_metadata(store, pages=pages, roots=roots, store=store)


def _record_list(value: Any) -> List[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        rows = value.get("pages")
        if isinstance(rows, (list, tuple)):
            return [row for row in rows if isinstance(row, Mapping)]
        if value.get("page_handle"):
            return [value]
    if isinstance(value, (list, tuple)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _observed_names(record: Mapping[str, Any]) -> Set[str]:
    strata = record.get("strata") if isinstance(record.get("strata"), Mapping) else {}
    return {name for name in CANONICAL_STRATA if isinstance(strata.get(name), Mapping) and strata[name].get("status") == "observed"}


def _greedy_coverage(records: Sequence[Mapping[str, Any]], max_pages: int) -> List[Mapping[str, Any]]:
    selected: List[Mapping[str, Any]] = []
    selected_handles: Set[str] = set()
    covered: Set[str] = set()
    available = set().union(*(_observed_names(row) for row in records)) if records else set()
    while len(selected) < max_pages and available - covered:
        counts = {name: sum(name in _observed_names(row) for row in records) for name in available - covered}
        rarest = min(counts, key=lambda name: (counts[name], CANONICAL_STRATA.index(name)))
        candidates = [row for row in records if str(row.get("page_handle") or "") not in selected_handles and rarest in _observed_names(row)]
        candidates.sort(
            key=lambda row: (
                -len(_observed_names(row) - covered),
                -sum(int((row.get("strata", {}).get(name, {}).get("counts", {}) if isinstance(row.get("strata"), Mapping) and isinstance(row.get("strata", {}).get(name), Mapping) else {}).get("evidence_items", 0) or 0) for name in _observed_names(row)),
                str(row.get("page_handle") or ""),
            )
        )
        if not candidates:
            break
        chosen = candidates[0]
        selected.append(chosen)
        selected_handles.add(str(chosen.get("page_handle") or ""))
        covered.update(_observed_names(chosen))
    for row in sorted(records, key=lambda item: str(item.get("page_handle") or "")):
        if len(selected) >= max_pages:
            break
        handle = str(row.get("page_handle") or "")
        if handle not in selected_handles:
            selected.append(row)
            selected_handles.add(handle)
    return selected


def _selection_rows(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for rank, row in enumerate(records, start=1):
        observed = sorted(_observed_names(row), key=CANONICAL_STRATA.index)
        strata_counts = {
            name: dict(row.get("strata", {}).get(name, {}).get("counts", {}))
            for name in observed
            if isinstance(row.get("strata"), Mapping) and isinstance(row.get("strata", {}).get(name), Mapping)
        }
        output.append(
            {
                "selection_rank": rank,
                "page_handle": str(row.get("page_handle") or ""),
                "root_handle": str(row.get("root_handle") or ""),
                "scope_handle": str(row.get("scope_handle") or ""),
                "strata": observed,
                "strata_counts": strata_counts,
                "status": str(row.get("status") or "metadata_missing"),
            }
        )
    return output


def select_pages_by_strata(
    metadata_or_pages: Any,
    *,
    max_pages: int = 5,
    allow_multi_scope: bool = False,
) -> Dict[str, Any]:
    """Select rare-stratum pages and transparently report scope limits."""

    if int(max_pages) < 1:
        raise SelectionStrataError("selection_budget_invalid")
    if isinstance(metadata_or_pages, Mapping) and isinstance(metadata_or_pages.get("pages"), (list, tuple)):
        metadata = metadata_or_pages
    else:
        metadata = build_canonical_strata_metadata(metadata_or_pages)
    records = _record_list(metadata)
    target = set(CANONICAL_STRATA)
    by_scope: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        by_scope[str(row.get("scope_handle") or _handle("scope", "missing"))].append(row)
    global_plan = _greedy_coverage(records, int(max_pages))
    global_covered = set().union(*(_observed_names(row) for row in global_plan)) if global_plan else set()
    global_scopes = {str(row.get("scope_handle") or "") for row in global_plan}
    scope_plans: Dict[str, List[Mapping[str, Any]]] = {scope: _greedy_coverage(rows, int(max_pages)) for scope, rows in by_scope.items()}
    full_scope_plans = {
        scope: plan
        for scope, plan in scope_plans.items()
        if target <= set().union(*(_observed_names(row) for row in plan)) if plan
    }
    if full_scope_plans:
        selected_scope = sorted(full_scope_plans)[0]
    elif scope_plans:
        def score(scope: str) -> Tuple[int, int, int, str]:
            plan = scope_plans[scope]
            covered = set().union(*(_observed_names(row) for row in plan)) if plan else set()
            return (-len(covered), -sum(bool(_observed_names(row)) for row in plan), -len(plan), scope)
        selected_scope = sorted(scope_plans, key=score)[0]
    else:
        selected_scope = ""
    single_plan = scope_plans.get(selected_scope, [])
    single_covered = set().union(*(_observed_names(row) for row in single_plan)) if single_plan else set()
    scope_authorization_required = bool(target <= global_covered and len(global_scopes) > 1 and not (target <= single_covered))
    if allow_multi_scope and target <= global_covered:
        chosen = global_plan
        chosen_scope_count = len(global_scopes)
    else:
        chosen = single_plan
        chosen_scope_count = 1 if chosen else 0
    selected_rows = _selection_rows(chosen)
    selected_covered = set().union(*(set(row["strata"]) for row in selected_rows)) if selected_rows else set()
    scope_coverage: Dict[str, Dict[str, Any]] = {}
    for scope, rows in by_scope.items():
        scope_coverage[scope] = {
            "scope_handle": scope,
            "page_count": len(rows),
            "available_strata": [name for name in CANONICAL_STRATA if any(name in _observed_names(row) for row in rows)],
            "stratum_page_counts": {name: sum(name in _observed_names(row) for row in rows) for name in CANONICAL_STRATA},
        }
    result: Dict[str, Any] = {
        "schema_version": STRATA_SCHEMA_VERSION,
        "body_free": True,
        "canonical_strata": list(CANONICAL_STRATA),
        "selected": selected_rows,
        "selected_page_count": len(selected_rows),
        "selected_page_limit": int(max_pages),
        "selected_scope": selected_scope or None,
        "selected_scope_count": chosen_scope_count,
        "available_strata": [name for name in CANONICAL_STRATA if any(name in _observed_names(row) for row in records)],
        "missing_strata": [name for name in CANONICAL_STRATA if name not in selected_covered],
        "selected_stratum_counts": {name: sum(name in row["strata"] for row in selected_rows) for name in CANONICAL_STRATA},
        "global_coverage_plan_page_handles": [str(row.get("page_handle") or "") for row in _selection_rows(global_plan)],
        "global_coverage_plan_scope_count": len(global_scopes),
        "global_coverage_plan_available": bool(target <= global_covered),
        "single_scope_coverage_plan_available": bool(target <= single_covered),
        "scope_authorization_required": scope_authorization_required,
        "provider_allowed": bool(target <= selected_covered and (allow_multi_scope or not scope_authorization_required)),
        "scope_coverage": scope_coverage,
        "selection_rule": "rare_strata_first_then_stable_handle_then_budget",
        "page_strata_multi_label_facts_preserved": True,
        "metadata_missing_vs_ambiguous_transparent": True,
        "authorization_error": "multi_scope_authorization_required" if scope_authorization_required and not allow_multi_scope else None,
    }
    result["selection_hash"] = stable_hash(result)
    _assert_body_free(result)
    return result


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SelectionStrataError("k10_invalid_json") from exc
    if not isinstance(value, Mapping):
        raise SelectionStrataError("k10_json_object_required")
    return value


def _read_jsonl(path: Path) -> List[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SelectionStrataError("k10_invalid_jsonl") from exc
    rows: List[Mapping[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, ValueError) as exc:
            raise SelectionStrataError("k10_invalid_jsonl") from exc
        if isinstance(value, Mapping):
            rows.append(value)
        else:
            raise SelectionStrataError("k10_jsonl_object_required")
    return rows


def diagnose_k10_v2_artifact(input_directory: Union[str, Path]) -> Dict[str, Any]:
    """Audit K10 v2 metadata without opening its store/private body file.

    Only four files are read.  In particular, ``store.private.json`` and any
    recovery/body map are intentionally not touched.
    """

    root = Path(input_directory).expanduser().resolve()
    if any(part.casefold() in _FROZEN_PARTS for part in root.parts):
        raise SelectionStrataError("k10_frozen_input_forbidden")
    manifest_path = root / "manifest.private.json"
    pages_path = root / "pages.private.jsonl"
    materialized_path = root / "materialized_map.private.jsonl"
    selection_path = root / "selection_map.private.jsonl"
    manifest = _read_json(manifest_path)
    pages = _read_jsonl(pages_path)
    materialized = _read_jsonl(materialized_path)
    selections = _read_jsonl(selection_path)
    if manifest.get("artifact_version") not in (None, K10_INPUT_ARTIFACT_VERSION):
        raise SelectionStrataError("k10_unexpected_artifact_version")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, K10_LOCAL_DAY):
        raise SelectionStrataError("k10_unexpected_split")
    if manifest.get("frozen_read") is True or manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise SelectionStrataError("k10_provider_or_frozen_input")
    materialized_by_page = {str(row.get("page_id") or row.get("page_hash") or ""): row for row in materialized}
    projected_pages: List[Dict[str, Any]] = []
    for page in pages:
        page_key = str(page.get("page_id") or page.get("page_hash") or "")
        material = materialized_by_page.get(page_key)
        projected = dict(page)
        # Only the whitelisted metadata keys are copied.  This keeps the
        # diagnostic independent of any body-shaped materialized extension.
        if isinstance(material, Mapping):
            for key in (
                "status",
                "within_limits",
                "page_counts",
                "message_count",
                "candidate_count",
                "evidence_count",
                "stage_b_status",
                "stage_c_status",
            ):
                if key in material:
                    projected[key] = material[key]
        projected_pages.append(projected)
    metadata = build_canonical_strata_metadata(projected_pages)
    selection = select_pages_by_strata(metadata, max_pages=5)
    observed_any = bool(metadata.get("available_strata"))
    all_required = not metadata.get("missing_strata") and not any(
        isinstance(row, Mapping) and row.get("ambiguous_strata") for row in metadata.get("pages", ())
    )
    diagnosis = {
        "schema_version": STRATA_SCHEMA_VERSION,
        "diagnosis_kind": "k10_v2_metadata_only",
        "body_free": True,
        "frozen_read": False,
        "provider_called": False,
        "files_read": [manifest_path.name, pages_path.name, materialized_path.name, selection_path.name],
        "store_read": False,
        "private_body_read": False,
        "provider_read": False,
        "page_count": len(pages),
        "materialized_page_count": len(materialized),
        "selection_row_count": len(selections),
        "available_strata": list(metadata.get("available_strata", ())),
        "missing_strata": list(metadata.get("missing_strata", ())),
        "metadata_missing_page_count": int(metadata.get("metadata_missing_page_count", 0) or 0),
        "ambiguous_page_count": int(metadata.get("ambiguous_page_count", 0) or 0),
        "canonical_strata_observed": observed_any,
        "can_offline_reconstruct": bool(all_required),
        "rebuild_development_artifact_required": not bool(all_required),
        "corpus_absence_vs_upstream_nonmaterialization_distinguishable": bool(observed_any),
        "conclusion": "canonical_strata_metadata_missing_upstream_rebuild_required" if not all_required else "canonical_strata_metadata_complete_in_allowed_projection",
        "selection": selection,
        "metadata_report": metadata,
    }
    # ``manifest`` itself is used only for gates/versions; no arbitrary
    # manifest fields are copied into the diagnosis.
    _assert_body_free(diagnosis)
    return diagnosis


def verify_strata_replay(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Check two body-free projections for deterministic replay equality."""

    left = dict(first)
    right = dict(second)
    for value in (left, right):
        value.pop("metadata_hash", None)
        value.pop("replay", None)
    return stable_hash(left) == stable_hash(right)


def write_canonical_strata_sidecar(
    metadata: Mapping[str, Any],
    output_directory: Union[str, Path],
    *,
    selection: Optional[Mapping[str, Any]] = None,
    diagnosis: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Write an immutable body-free K28 sidecar artifact."""

    root = Path(output_directory).expanduser().resolve()
    if any(part.casefold() in _FROZEN_PARTS for part in root.parts):
        raise SelectionStrataError("k28_frozen_output_forbidden")
    if root.exists():
        raise FileExistsError("k28_output_artifact_is_immutable")
    projected = dict(metadata)
    if selection is None:
        selection = select_pages_by_strata(projected)
    aggregate = dict(projected)
    aggregate["selection"] = dict(selection)
    if diagnosis is not None:
        aggregate["diagnosis"] = dict(diagnosis)
    manifest: Dict[str, Any] = {
        "schema_version": STRATA_SCHEMA_VERSION,
        "pipeline_version": STRATA_PIPELINE_VERSION,
        "artifact_version": "canonical_selection_strata_development_v1",
        "body_free": True,
        "frozen_read": False,
        "provider_called": False,
        "selected_page_count": int(selection.get("selected_page_count", 0) or 0),
        "page_count": int(projected.get("page_count", 0) or 0),
        "output_files": dict(STRATA_OUTPUT_FILENAMES),
        "metadata_hash": str(projected.get("metadata_hash") or stable_hash(projected)),
    }
    strata_rows = projected.get("pages") if isinstance(projected.get("pages"), list) else []
    selection_rows = selection.get("selected") if isinstance(selection, Mapping) and isinstance(selection.get("selected"), list) else []
    diagnosis_value = dict(diagnosis) if diagnosis is not None else {
        "schema_version": STRATA_SCHEMA_VERSION,
        "diagnosis_kind": "not_run",
        "body_free": True,
        "frozen_read": False,
        "provider_called": False,
    }
    for value in (strata_rows, selection_rows, aggregate, diagnosis_value, manifest):
        _assert_body_free(value)
    root.mkdir(parents=True, exist_ok=False)
    (root / STRATA_OUTPUT_FILENAMES["strata"]).write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in strata_rows),
        encoding="utf-8",
    )
    (root / STRATA_OUTPUT_FILENAMES["selection"]).write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in selection_rows),
        encoding="utf-8",
    )
    (root / STRATA_OUTPUT_FILENAMES["aggregate"]).write_text(json.dumps(aggregate, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (root / STRATA_OUTPUT_FILENAMES["diagnosis"]).write_text(json.dumps(diagnosis_value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest["artifact_hashes"] = {
        filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for filename in STRATA_OUTPUT_FILENAMES.values()
        if filename != STRATA_OUTPUT_FILENAMES["manifest"]
    }
    (root / STRATA_OUTPUT_FILENAMES["manifest"]).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {key: str(root / filename) for key, filename in STRATA_OUTPUT_FILENAMES.items()}


# Friendly aliases for callers using the upstream terminology.
emit_canonical_strata_metadata = build_canonical_strata_metadata
derive_selection_strata = build_canonical_strata_metadata
select_strata_pages = select_pages_by_strata
audit_k10_v2_metadata = diagnose_k10_v2_artifact


__all__ = [
    "CANONICAL_STRATA",
    "CATEGORY_NAMES",
    "REQUIRED_UPSTREAM_FIELDS",
    "K10_INPUT_ARTIFACT_VERSION",
    "K10_LOCAL_DAY",
    "STRATA_OUTPUT_FILENAMES",
    "STRATA_PIPELINE_VERSION",
    "STRATA_SCHEMA_VERSION",
    "PageStrataMetadata",
    "SelectionStrataError",
    "StratumEvidence",
    "audit_k10_v2_metadata",
    "build_canonical_strata_metadata",
    "canonical_json",
    "canonical_stratum",
    "derive_selection_strata",
    "diagnose_k10_v2_artifact",
    "emit_canonical_strata_metadata",
    "materialize_context_packet_strata",
    "materialize_linear_stage_packet_strata",
    "select_pages_by_strata",
    "select_strata_pages",
    "stable_hash",
    "verify_strata_replay",
    "write_canonical_strata_sidecar",
]
