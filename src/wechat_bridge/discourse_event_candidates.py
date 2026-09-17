"""Shadow-only DiscourseThread and strict EventCandidate derivation.

This module is the Workstream D boundary below the production event/title/UI
layers.  It consumes only in-memory ``DialogueBundle``/fragment/claim
artifacts and returns versioned, evidence-linked candidates.  A bundle is a
context window, a thread is an open discourse projection, and an
``EventCandidate`` is emitted only after an explicit structural evidence gate.

No database, network, selector, title generator or legacy event merger is
called here.  The optional model status is metadata supplied by a later
worker; pending/fallback/invalid model runs remain thread-only by default.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .dialogue_bundle import (
    BUNDLE_RULESET_VERSION,
    BundleClaim,
    BundleFragment,
    ContextRelationCandidate,
    DialogueBundle,
    DialogueBundleResult,
    REL_ANSWERS,
    REL_CONTRASTS,
    REL_CONTINUES,
    REL_ELABORATES,
    REL_INSUFFICIENT,
    REL_POSSIBLY_RELATED,
    REL_TOPIC_SHIFT,
)
from .semantic_registry import (
    CONTEXT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    UNKNOWN,
    stable_hash,
)


THREAD_PIPELINE_VERSION = "workstream_d_thread_v1"
EVENT_PIPELINE_VERSION = "workstream_d_event_candidate_v1"
PIPELINE_VERSION = THREAD_PIPELINE_VERSION
RULESET_VERSION = "workstream_d_rules_v1"
THREAD_RULESET_VERSION = RULESET_VERSION
EVENT_RULESET_VERSION = RULESET_VERSION
SEMANTIC_SHADOW_SOURCE = "semantic_v2_shadow"
LEGACY_FALLBACK_SOURCE = "legacy_fallback"

THREAD_STATUSES = frozenset({"open", "provisional", "closed"})
EVENT_CANDIDATE_STATUSES = frozenset({"candidate", "blocked", "abstained"})
MODEL_STATUSES = frozenset(
    {"none", "accepted", "pending", "fallback", "invalid", "timeout", "unavailable"}
)
BLOCKED_MODEL_STATUSES = frozenset(
    {"pending", "fallback", "invalid", "timeout", "unavailable"}
)
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})
KNOWN_STATES = frozenset(
    {"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"}
)
KNOWN_RESOLUTIONS = frozenset({"explicit", "inherited", "unknown"})
THREAD_RELATION_LABELS = frozenset(
    {REL_CONTINUES, REL_ELABORATES, REL_ANSWERS, REL_CONTRASTS, REL_TOPIC_SHIFT, REL_POSSIBLY_RELATED, REL_INSUFFICIENT}
)
LINKING_RELATION_LABELS = frozenset({REL_CONTINUES, REL_ELABORATES, REL_ANSWERS, REL_CONTRASTS})
# Older shadow artifacts used these subtype-like labels as their public
# relation type.  D consumes them only through this normalization; it never
# promotes a subtype (for example ``state_transition``) to a new canonical
# relation label in output.
RELATION_LABEL_ALIASES = {
    "continuation": REL_CONTINUES,
    "question_answer": REL_ANSWERS,
    "contrast": REL_CONTRASTS,
    "reply": REL_ANSWERS,
    "object_inheritance": REL_ELABORATES,
    "state_transition": REL_CONTINUES,
    "state_update": REL_CONTINUES,
    "related": REL_POSSIBLY_RELATED,
    "possibly-related": REL_POSSIBLY_RELATED,
}
# A relation label alone is not proof.  In particular, ``same_segment``,
# time proximity, embeddings and generic scores are deliberately absent.
SEMANTIC_RELATION_SUPPORT = frozenset(
    {
        "shared_object",
        "shared_subject",
        "shared_action",
        "shared_actions",
        "shared_state",
        "shared_mentioned_person",
        "shared_people",
        "object_inheritance",
        "state_change",
        "state_update",
        "question_or_request_then_turn",
        "question_answer",
        "qa",
        "answer_cue",
        "explicit_object",
        "explicit_subject",
    }
)
MNL_CODES = frozenset(
    {
        "mnl",
        "must_not_link",
        "must-not-link",
        "no_merge",
        "same_event_forbidden",
        "cross_chat_conflict",
        "object_conflict",
        "scope_conflict",
    }
)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _tuple_text(value: Any) -> Tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return (str(value),) if value not in (None, "") else ()
    try:
        return tuple(str(item) for item in value if item not in (None, ""))
    except TypeError:
        return (str(value),)


def _known(value: Any) -> bool:
    return value not in (None, "", UNKNOWN, "unknown", "UNKNOWN")


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    output: List[str] = []
    seen: Set[str] = set()
    for value in values:
        text = str(value)
        if not _known(text) or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return tuple(output)


def _scope(item: Any) -> Optional[Tuple[str, str]]:
    account = _value(item, "account_id", UNKNOWN)
    chat = _value(item, "chat_id", UNKNOWN)
    if isinstance(_value(item, "chat_scope"), Mapping):
        scope = _value(item, "chat_scope")
        account = scope.get("account_id", account)
        chats = scope.get("chat_ids") or ()
        chat = chats[0] if chats else chat
    if not _known(account) or not _known(chat):
        return None
    return str(account), str(chat)


def _span(value: Any) -> Optional[Tuple[int, int]]:
    if isinstance(value, Mapping):
        start = value.get("start", value.get("span_start"))
        end = value.get("end", value.get("span_end"))
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        start, end = value[0], value[1]
    else:
        return None
    try:
        start_value, end_value = int(start), int(end)
    except (TypeError, ValueError):
        return None
    return (start_value, end_value) if 0 <= start_value <= end_value else None


def _copy_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_refs(item: Any) -> Tuple[Dict[str, Any], ...]:
    values = _value(item, "evidence_refs", ()) or ()
    if isinstance(values, Mapping):
        values = (values,)
    result: List[Dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        ref = dict(value)
        if not _known(ref.get("id")) and not _known(ref.get("evidence_id")):
            continue
        if "id" not in ref and _known(ref.get("evidence_id")):
            ref["id"] = str(ref["evidence_id"])
        ref.setdefault("type", "fragment")
        result.append(ref)
    return tuple(result)


def _dedupe_refs(values: Iterable[Mapping[str, Any]]) -> Tuple[Dict[str, Any], ...]:
    output: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for value in values:
        ref = dict(value)
        key = stable_hash(ref)
        if key in seen:
            continue
        seen.add(key)
        output.append(ref)
    return tuple(output)


def _as_fragment(value: Any) -> Optional[BundleFragment]:
    if isinstance(value, BundleFragment):
        return value
    if isinstance(value, Mapping):
        return BundleFragment.from_mapping(value)
    return None


def _as_claim(value: Any, fragment: Optional[BundleFragment] = None) -> Optional[BundleClaim]:
    if isinstance(value, BundleClaim):
        return value
    if isinstance(value, Mapping):
        try:
            return BundleClaim.from_mapping(value, fragment=fragment)
        except (TypeError, ValueError):
            return None
    return None


def _input_parts(value: Any) -> Tuple[Tuple[Any, ...], Tuple[Any, ...], Tuple[Any, ...], Tuple[Any, ...]]:
    """Return bundles, fragments, claims and relations from public DTO/mapping input."""

    if isinstance(value, DialogueBundleResult):
        return value.bundles, value.fragments, value.claims, value.relations
    if isinstance(value, DialogueBundle):
        return (value,), (), (), ()
    if isinstance(value, Mapping):
        bundles = value.get("bundles", value.get("dialogue_bundles", ()))
        fragments = value.get("fragments", ())
        claims = value.get("claims", ())
        relations = value.get("relations", value.get("context_relations", ()))
        if isinstance(value.get("bundle_id"), str):
            bundles = (value,)
        return tuple(bundles or ()), tuple(fragments or ()), tuple(claims or ()), tuple(relations or ())
    values = tuple(value or ())
    if not values:
        return (), (), (), ()
    if all(isinstance(item, (DialogueBundle, Mapping)) for item in values):
        return values, (), (), ()
    return (), values, (), ()


def _bundle_id(bundle: Any) -> str:
    return str(_value(bundle, "bundle_id", _value(bundle, "dialogue_bundle_id", UNKNOWN)) or UNKNOWN)


def _fragment_ids(bundle: Any) -> Tuple[str, ...]:
    return _tuple_text(_value(bundle, "fragment_ids", _value(bundle, "member_fragment_ids", ())))


def _claim_ids(bundle: Any) -> Tuple[str, ...]:
    return _tuple_text(_value(bundle, "claim_ids", _value(bundle, "member_claim_ids", ())))


def _relation_id(relation: Any) -> str:
    return str(
        _value(
            relation,
            "context_relation_id",
            _value(relation, "relation_id", _value(relation, "id", UNKNOWN)),
        )
        or UNKNOWN
    )


def _relation_label(relation: Any) -> str:
    raw = str(_value(relation, "label", _value(relation, "relation", "insufficient")) or "insufficient")
    return RELATION_LABEL_ALIASES.get(raw.casefold(), raw)


def _anchor_ids(relation: Any) -> Tuple[str, str]:
    left = _value(relation, "left_anchor_id", _value(relation, "left_fragment_id", UNKNOWN))
    right = _value(relation, "right_anchor_id", _value(relation, "right_fragment_id", UNKNOWN))
    return str(left or UNKNOWN), str(right or UNKNOWN)


def _truthy(item: Any, *names: str) -> bool:
    return any(bool(_value(item, name, False)) for name in names)


def _has_mnl(item: Any) -> bool:
    if _truthy(item, "mnl", "must_not_link", "no_merge", "same_event_forbidden"):
        return True
    codes = _tuple_text(_value(item, "uncertainties", ())) + _tuple_text(
        _value(item, "conflicting_slot_codes", ())
    )
    return any(str(code).casefold() in MNL_CODES for code in codes)


def _terminal_proof(fragment: BundleFragment) -> bool:
    return (
        not fragment.is_silent
        and not fragment.is_opener
        and fragment.state in TERMINAL_STATES
        and fragment.state_evidence in {"explicit", "inherited"}
        and fragment.object_known
        and bool(fragment.object_evidence_refs)
        and bool(_evidence_refs(fragment))
    )


def _fragment_object(fragment: BundleFragment) -> Optional[Dict[str, Any]]:
    # Keep an explicit unknown slot on the thread.  It is useful audit data,
    # but ``_event_block_reasons`` still excludes it from event identity.
    if not fragment.object_known:
        return {
            "id": UNKNOWN,
            "object_id": UNKNOWN,
            "resolution": UNKNOWN,
            "source_id": None,
            "evidence_refs": [],
        }
    return {
        "id": fragment.object_id,
        "object_id": fragment.object_id,
        "resolution": fragment.object_resolution,
        "source_id": fragment.object_inherited_from_id,
        "evidence_refs": [dict(item) for item in fragment.object_evidence_refs],
    }


def _relation_safe_for_thread(
    relation: Any,
    left: Optional[BundleFragment],
    right: Optional[BundleFragment],
) -> bool:
    """Return whether a typed relation is strong enough to join a thread.

    This is intentionally a narrow gate over already-produced relation
    candidates.  A canonical label, same-segment hint, time proximity,
    embedding score, or explicit reply flag by itself cannot create a thread.
    The two endpoints must be substantive and the relation must carry both a
    semantic support code and typed evidence.
    """

    if left is None or right is None:
        return False
    if left.is_silent or right.is_silent or left.is_opener or right.is_opener:
        return False
    if left.role == "context_only" or right.role == "context_only":
        return False
    if _relation_label(relation) not in LINKING_RELATION_LABELS:
        return False
    if _has_mnl(relation):
        return False
    supporting = {
        str(code).casefold()
        for code in _tuple_text(_value(relation, "supporting_slot_codes", ()))
    }
    if not supporting.intersection(SEMANTIC_RELATION_SUPPORT):
        return False
    return bool(_evidence_refs(relation))


def _fragment_actions(fragment: BundleFragment) -> Tuple[str, ...]:
    return _unique(fragment.actions)


def _event_time(fragment: BundleFragment, prefix: str) -> Tuple[Optional[float], str, Optional[str]]:
    value = getattr(fragment, f"{prefix}_time", None)
    source = str(getattr(fragment, f"{prefix}_time_source", UNKNOWN) or UNKNOWN)
    if value is None or source not in {"explicit", "inherited"}:
        return None, UNKNOWN, None
    return float(value), source, fragment.fragment_id


@dataclass(frozen=True)
class EventDerivationConfig:
    """Safety switches for the shadow derivation boundary."""

    materialize_events: bool = False
    event_materialization_enabled: Optional[bool] = None
    model_status: str = "none"
    model_id: str = UNKNOWN
    model_version: str = UNKNOWN
    prompt_version: str = UNKNOWN
    analysis_run_id: str = "RUN_WORKSTREAM_D"
    source_marker: str = SEMANTIC_SHADOW_SOURCE
    legacy_fallback_source: str = LEGACY_FALLBACK_SOURCE
    allow_cross_chat: bool = False

    @property
    def events_enabled(self) -> bool:
        if self.event_materialization_enabled is not None:
            return bool(self.event_materialization_enabled)
        return bool(self.materialize_events)

    @property
    def normalized_model_status(self) -> str:
        status = str(self.model_status or "none").casefold()
        return status if status in MODEL_STATUSES else "unavailable"


@dataclass(frozen=True)
class DiscourseThread:
    """An open/provisional discourse projection, never an event."""

    discourse_thread_id: str
    fragment_ids: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    bundle_ids: Tuple[str, ...] = ()
    conversation_opener_fragment_ids: Tuple[str, ...] = ()
    speaker_ids: Tuple[str, ...] = ()
    mentioned_person_ids: Tuple[str, ...] = ()
    subject_ids: Tuple[str, ...] = ()
    object_refs: Tuple[Dict[str, Any], ...] = ()
    state_sequence: Tuple[str, ...] = (UNKNOWN,)
    closure_reason: str = UNKNOWN
    start_fragment_id: str = UNKNOWN
    end_fragment_id: str = UNKNOWN
    start_time_offset_seconds: Optional[float] = None
    start_time_source: str = UNKNOWN
    end_time_offset_seconds: Optional[float] = None
    end_time_source: str = UNKNOWN
    information_value: str = "unknown"
    event_completeness: str = "unknown"
    event_candidate: str = "no"
    context_relation_ids: Tuple[str, ...] = ()
    source_message_ids: Tuple[str, ...] = ()
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    uncertainties: Tuple[str, ...] = ()
    open_boundary: bool = True
    status: str = "open"
    confidence: str = "low"
    analysis_run_id: str = "RUN_WORKSTREAM_D"
    source: str = SEMANTIC_SHADOW_SOURCE
    model_id: str = UNKNOWN
    model_version: str = UNKNOWN
    prompt_version: str = UNKNOWN
    input_hash: str = ""
    cache_key: str = ""
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = THREAD_PIPELINE_VERSION
    ruleset_version: str = THREAD_RULESET_VERSION

    @property
    def id(self) -> str:
        return self.discourse_thread_id

    @property
    def thread_id(self) -> str:
        return self.discourse_thread_id

    @property
    def source_bundle_ids(self) -> Tuple[str, ...]:
        return self.bundle_ids

    @property
    def event_candidate_allowed(self) -> bool:
        return self.event_candidate == "yes"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discourse_thread_id": self.discourse_thread_id,
            "thread_id": self.discourse_thread_id,
            "fragment_ids": list(self.fragment_ids),
            "claim_ids": list(self.claim_ids),
            "bundle_ids": list(self.bundle_ids),
            "source_bundle_ids": list(self.bundle_ids),
            "conversation_opener_fragment_ids": list(self.conversation_opener_fragment_ids),
            "speaker_ids": list(self.speaker_ids),
            "mentioned_person_ids": list(self.mentioned_person_ids),
            "subject_ids": list(self.subject_ids),
            "object_refs": [dict(item) for item in self.object_refs],
            "state_sequence": list(self.state_sequence),
            "closure_reason": self.closure_reason,
            "start_fragment_id": self.start_fragment_id,
            "end_fragment_id": self.end_fragment_id,
            "start_time_offset_seconds": self.start_time_offset_seconds,
            "start_time_source": self.start_time_source,
            "end_time_offset_seconds": self.end_time_offset_seconds,
            "end_time_source": self.end_time_source,
            "information_value": self.information_value,
            "event_completeness": self.event_completeness,
            "event_candidate": self.event_candidate,
            "context_relation_ids": list(self.context_relation_ids),
            "source_message_ids": list(self.source_message_ids),
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "uncertainties": list(self.uncertainties),
            "open_boundary": self.open_boundary,
            "status": self.status,
            "confidence": self.confidence,
            "analysis_run_id": self.analysis_run_id,
            "source": self.source,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


@dataclass(frozen=True)
class EventCandidate:
    """Strictly gated event candidate; not a production event record."""

    event_candidate_id: str
    thread_id: str
    bundle_ids: Tuple[str, ...] = ()
    fragment_ids: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    source_message_ids: Tuple[str, ...] = ()
    subject_id: str = UNKNOWN
    subject_type: str = UNKNOWN
    object_id: str = UNKNOWN
    object_resolution: str = UNKNOWN
    object_inherited_from_id: Optional[str] = None
    actions: Tuple[str, ...] = ()
    state: str = UNKNOWN
    state_sequence: Tuple[str, ...] = (UNKNOWN,)
    event_type: str = UNKNOWN
    modality: str = UNKNOWN
    start_time_offset_seconds: Optional[float] = None
    start_time_source: str = UNKNOWN
    end_time_offset_seconds: Optional[float] = None
    end_time_source: str = UNKNOWN
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    context_relation_ids: Tuple[str, ...] = ()
    uncertainties: Tuple[str, ...] = ()
    confidence: str = "medium"
    confidence_score: float = 0.0
    status: str = "candidate"
    materialized: bool = True
    analysis_run_id: str = "RUN_WORKSTREAM_D"
    source: str = SEMANTIC_SHADOW_SOURCE
    model_id: str = UNKNOWN
    model_version: str = UNKNOWN
    prompt_version: str = UNKNOWN
    input_hash: str = ""
    cache_key: str = ""
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = EVENT_PIPELINE_VERSION
    ruleset_version: str = EVENT_RULESET_VERSION

    @property
    def id(self) -> str:
        return self.event_candidate_id

    @property
    def event_id(self) -> str:
        return self.event_candidate_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_candidate_id": self.event_candidate_id,
            "event_id": self.event_candidate_id,
            "thread_id": self.thread_id,
            "bundle_ids": list(self.bundle_ids),
            "fragment_ids": list(self.fragment_ids),
            "claim_ids": list(self.claim_ids),
            "source_message_ids": list(self.source_message_ids),
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "object_id": self.object_id,
            "object_resolution": self.object_resolution,
            "object_inherited_from_id": self.object_inherited_from_id,
            "actions": list(self.actions),
            "state": self.state,
            "state_sequence": list(self.state_sequence),
            "event_type": self.event_type,
            "modality": self.modality,
            "start_time_offset_seconds": self.start_time_offset_seconds,
            "start_time_source": self.start_time_source,
            "end_time_offset_seconds": self.end_time_offset_seconds,
            "end_time_source": self.end_time_source,
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "context_relation_ids": list(self.context_relation_ids),
            "uncertainties": list(self.uncertainties),
            "confidence": self.confidence,
            "confidence_score": self.confidence_score,
            "status": self.status,
            "materialized": self.materialized,
            "analysis_run_id": self.analysis_run_id,
            "source": self.source,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


@dataclass(frozen=True)
class DiscourseEventResult:
    """Versioned shadow output containing threads and optional candidates."""

    threads: Tuple[DiscourseThread, ...] = ()
    event_candidates: Tuple[EventCandidate, ...] = ()
    source_marker: str = SEMANTIC_SHADOW_SOURCE
    fallback_source: str = LEGACY_FALLBACK_SOURCE
    analysis_run_id: str = "RUN_WORKSTREAM_D"
    input_hash: str = ""
    cache_key: str = ""
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = THREAD_PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION

    @property
    def discourse_threads(self) -> Tuple[DiscourseThread, ...]:
        return self.threads

    @property
    def events(self) -> Tuple[EventCandidate, ...]:
        # Deliberately named as a read-only compatibility alias; serialized
        # output uses ``event_candidates`` and never pretends these are events.
        return self.event_candidates

    def __iter__(self) -> Iterator[DiscourseThread]:
        return iter(self.threads)

    def __len__(self) -> int:
        return len(self.threads)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threads": [item.to_dict() for item in self.threads],
            "discourse_threads": [item.to_dict() for item in self.threads],
            "event_candidates": [item.to_dict() for item in self.event_candidates],
            "source_marker": self.source_marker,
            "fallback_source": self.fallback_source,
            "analysis_run_id": self.analysis_run_id,
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


def legacy_fallback_marker(config: Optional[EventDerivationConfig] = None) -> Dict[str, Any]:
    """Return an explicit source marker for old-chain fallback consumers."""

    cfg = config or EventDerivationConfig()
    return {
        "source": str(cfg.source_marker or SEMANTIC_SHADOW_SOURCE),
        "fallback_source": str(cfg.legacy_fallback_source or LEGACY_FALLBACK_SOURCE),
        "pipeline_version": THREAD_PIPELINE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "event_materialization_enabled": cfg.events_enabled,
        "model_status": cfg.normalized_model_status,
    }


source_marker = legacy_fallback_marker


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent.setdefault(value, value)
        while parent != self.parent[parent]:
            self.parent[parent] = self.parent[self.parent[parent]]
            parent = self.parent[parent]
        root = parent
        parent = value
        while self.parent[parent] != parent:
            next_value = self.parent[parent]
            self.parent[parent] = root
            parent = next_value
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _normalise_inputs(value: Any) -> Tuple[Dict[str, BundleFragment], Dict[str, BundleClaim], Dict[str, Any], Dict[str, Any], Dict[str, int]]:
    raw_bundles, raw_fragments, raw_claims, raw_relations = _input_parts(value)
    fragments: Dict[str, BundleFragment] = {}
    for raw in raw_fragments:
        fragment = _as_fragment(raw)
        if fragment is not None and _known(fragment.fragment_id):
            fragments.setdefault(fragment.fragment_id, fragment)
    claims: Dict[str, BundleClaim] = {}
    for raw in raw_claims:
        claim = _as_claim(raw, fragments.get(str(_value(raw, "fragment_id", UNKNOWN))))
        if claim is not None and _known(claim.claim_id):
            claims.setdefault(claim.claim_id, claim)
    bundles: Dict[str, Any] = {}
    for raw in raw_bundles:
        bundle_id = _bundle_id(raw)
        if _known(bundle_id):
            bundles.setdefault(bundle_id, raw)
        for fragment_id in _fragment_ids(raw):
            if fragment_id not in fragments:
                # A serialized bundle may carry only member IDs.  Keep the
                # ID as a placeholder; no semantic slot will be guessed.
                continue
    relations: Dict[str, Any] = {}
    for raw in raw_relations:
        relation_id = _relation_id(raw)
        if _known(relation_id):
            relations.setdefault(relation_id, raw)
    order = {fragment_id: index for index, fragment_id in enumerate(fragments)}
    return fragments, claims, bundles, relations, order


def _fragment_mappings(
    fragments: Mapping[str, BundleFragment],
    claims: Mapping[str, BundleClaim],
    bundles: Mapping[str, Any],
    relations: Mapping[str, Any],
    order: Mapping[str, int],
) -> Tuple[_UnionFind, Dict[str, Set[str]], Dict[str, Set[str]], Dict[str, Set[str]]]:
    uf = _UnionFind(fragments.keys())
    blocked_relations: Set[str] = set()
    topic_relations: Set[str] = set()
    relation_fragments: Dict[str, Set[str]] = defaultdict(set)
    for relation_id, relation in relations.items():
        left, right = _anchor_ids(relation)
        label = _relation_label(relation)
        left_fragment = left if left in fragments else str(_value(claims.get(left), "fragment_id", UNKNOWN))
        right_fragment = right if right in fragments else str(_value(claims.get(right), "fragment_id", UNKNOWN))
        relation_fragments[relation_id].update(
            item for item in (left_fragment, right_fragment) if item in fragments
        )
        left_scope = _scope(fragments.get(left_fragment))
        right_scope = _scope(fragments.get(right_fragment))
        if left_scope is None or right_scope is None or left_scope != right_scope:
            blocked_relations.add(relation_id)
            continue
        if _has_mnl(relation):
            blocked_relations.add(relation_id)
            continue
        if label == REL_TOPIC_SHIFT:
            topic_relations.add(relation_id)
            continue
        # Weak/insufficient relation candidates remain evidence but never
        # define a thread component or an event identity.
        if not _relation_safe_for_thread(
            relation,
            fragments.get(left_fragment),
            fragments.get(right_fragment),
        ):
            continue
        uf.union(left_fragment, right_fragment)
    return uf, relation_fragments, {"blocked": blocked_relations, "topic": topic_relations}, defaultdict(set)


def _component_data(
    fragments: Mapping[str, BundleFragment],
    claims: Mapping[str, BundleClaim],
    bundles: Mapping[str, Any],
    relations: Mapping[str, Any],
    order: Mapping[str, int],
    uf: _UnionFind,
    relation_fragments: Mapping[str, Set[str]],
    relation_flags: Mapping[str, Set[str]],
    config: EventDerivationConfig,
) -> Tuple[DiscourseThread, ...]:
    components: Dict[str, List[str]] = defaultdict(list)
    for fragment_id in fragments:
        components[uf.find(fragment_id)].append(fragment_id)
    result: List[DiscourseThread] = []
    for component in components.values():
        fragment_ids = tuple(sorted(component, key=lambda item: order.get(item, 0)))
        member_fragments = [fragments[item] for item in fragment_ids]
        member_fragment_set = set(fragment_ids)
        claim_values = [claim for claim in claims.values() if claim.fragment_id in member_fragment_set]
        claim_ids = tuple(claim.claim_id for claim in claim_values)
        bundle_ids = tuple(
            bundle_id
            for bundle_id, bundle in bundles.items()
            if member_fragment_set.intersection(_fragment_ids(bundle))
        )
        relation_ids = tuple(
            relation_id
            for relation_id, endpoints in relation_fragments.items()
            if endpoints.intersection(member_fragment_set)
        )
        source_message_ids = _unique(fragment.message_id for fragment in member_fragments)
        scopes = {_scope(fragment) for fragment in member_fragments}
        uncertainties: List[str] = []
        if None in scopes:
            uncertainties.append("scope_unknown")
        if len(scopes) > 1:
            uncertainties.append("cross_chat_conflict")
        if any(_has_mnl(fragment) for fragment in member_fragments) or any(_has_mnl(claim) for claim in claim_values):
            uncertainties.append("must_not_link")
        if any(relation_id in relation_flags.get("blocked", set()) for relation_id in relation_ids):
            uncertainties.append("relation_conflict")
        if any(relation_id in relation_flags.get("topic", set()) for relation_id in relation_ids):
            uncertainties.append("topic_shift_boundary")
        opener_ids = tuple(fragment.fragment_id for fragment in member_fragments if fragment.is_opener)
        speaker_ids = _unique(fragment.speaker_id for fragment in member_fragments)
        mentioned_person_ids = _unique(
            person_id for fragment in member_fragments for person_id in fragment.mentioned_person_ids
        )
        subject_ids = _unique(fragment.subject_id for fragment in member_fragments)
        object_values: List[Dict[str, Any]] = []
        for fragment in member_fragments:
            item = _fragment_object(fragment)
            if item is not None:
                object_values.append(item)
        for claim in claim_values:
            if _known(claim.object_id) and claim.object_resolution in {"explicit", "inherited"}:
                object_values.append(
                    {
                        "id": claim.object_id,
                        "object_id": claim.object_id,
                        "resolution": claim.object_resolution,
                        "source_id": claim.object_inherited_from_id,
                        "evidence_refs": [dict(item) for item in claim.object_evidence_refs],
                    }
                )
        object_refs: List[Dict[str, Any]] = []
        seen_objects: Set[Tuple[str, str, Optional[str]]] = set()
        for item in object_values:
            key = (str(item.get("id")), str(item.get("resolution")), item.get("source_id"))
            if key not in seen_objects:
                seen_objects.add(key)
                object_refs.append(item)
        state_sequence = _unique(
            fragment.state for fragment in member_fragments if fragment.state in KNOWN_STATES - {UNKNOWN}
        )
        if not state_sequence:
            state_sequence = (UNKNOWN,)
            uncertainties.append("state_unknown")
        if any(fragment.state == UNKNOWN or fragment.state not in KNOWN_STATES for fragment in member_fragments):
            uncertainties.append("state_unknown")
        if not subject_ids or any(not _known(fragment.subject_id) for fragment in member_fragments):
            uncertainties.append("subject_unknown")
        if not object_refs or any(not fragment.object_known for fragment in member_fragments):
            uncertainties.append("object_unknown")
        actions = _unique(action for fragment in member_fragments for action in _fragment_actions(fragment))
        if not actions or any(not _fragment_actions(fragment) for fragment in member_fragments):
            uncertainties.append("action_unknown")
        evidence_refs = _dedupe_refs(
            ref for fragment in member_fragments for ref in _evidence_refs(fragment)
        )
        evidence_refs = _dedupe_refs(
            list(evidence_refs) + [ref for claim in claim_values for ref in _evidence_refs(claim)]
        )
        if not evidence_refs or any(not _evidence_refs(fragment) for fragment in member_fragments):
            uncertainties.append("evidence_insufficient")
        if any(fragment.is_silent for fragment in member_fragments):
            uncertainties.append("silent_context")
        if any(fragment.is_opener for fragment in member_fragments):
            uncertainties.append("conversation_opener")
        if any(fragment.role == "context_only" for fragment in member_fragments):
            uncertainties.append("context_only")
        model_status = config.normalized_model_status
        if model_status in BLOCKED_MODEL_STATUSES:
            uncertainties.append("model_" + model_status)
        terminal_fragments = [fragment for fragment in member_fragments if _terminal_proof(fragment)]
        terminal_states = {fragment.state for fragment in terminal_fragments}
        if len(terminal_states) > 1 and not any(
            _value(relations.get(relation_id), "subtype", "") in {"state_update", "state_transition"}
            for relation_id in relation_ids
        ):
            uncertainties.append("state_conflict")
        closure_reason = UNKNOWN
        if terminal_fragments and "state_conflict" not in uncertainties:
            closure_reason = next(
                (fragment.closure_reason for fragment in reversed(terminal_fragments) if fragment.closure_reason != UNKNOWN),
                UNKNOWN,
            )
        closed = closure_reason != UNKNOWN and not any(fragment.is_silent or fragment.is_opener for fragment in member_fragments)
        # Semantic start/end remain unknown unless an input explicitly marked
        # the time boundary.  Collection order is not semantic evidence.
        start_value: Optional[float] = None
        start_source = UNKNOWN
        start_fragment_id = UNKNOWN
        end_value: Optional[float] = None
        end_source = UNKNOWN
        end_fragment_id = UNKNOWN
        starts = [_event_time(fragment, "start") for fragment in member_fragments]
        starts = [item for item in starts if item[0] is not None]
        ends = [_event_time(fragment, "end") for fragment in member_fragments]
        ends = [item for item in ends if item[0] is not None]
        if starts:
            start_value, start_source, start_fragment_id = min(starts, key=lambda item: item[0] or 0)
        if ends:
            end_value, end_source, end_fragment_id = max(ends, key=lambda item: item[0] or 0)
        information_values = [str(fragment.information_value or UNKNOWN) for fragment in member_fragments]
        information_order = {"none": 0, "low": 1, "unknown": 1, "medium": 2, "high": 3}
        information_value = max(information_values, key=lambda item: information_order.get(item, 1), default=UNKNOWN)
        completeness_values = [str(fragment.event_completeness or UNKNOWN) for fragment in member_fragments]
        event_completeness = "sufficient" if "sufficient" in completeness_values else (
            "not_applicable" if completeness_values and all(item == "not_applicable" for item in completeness_values) else "unknown"
        )
        thread_id = "DISCOURSE_THREAD_" + stable_hash(
            {
                "fragment_ids": fragment_ids,
                "claim_ids": claim_ids,
                "bundle_ids": bundle_ids,
                "relation_ids": relation_ids,
                "ruleset": THREAD_RULESET_VERSION,
            }
        )[:20]
        thread_input_hash = stable_hash(
            {"thread_id": thread_id, "fragment_ids": fragment_ids, "claim_ids": claim_ids, "relation_ids": relation_ids}
        )
        # Event sufficiency is evaluated after constructing the complete
        # thread so every blocked reason remains auditable on the thread.
        structural_reasons = _event_block_reasons(
            member_fragments,
            claim_values,
            object_refs,
            subject_ids,
            actions,
            state_sequence,
            evidence_refs,
            relation_ids,
            relations,
            relation_flags,
            scopes,
            config,
            uncertainties,
        )
        uncertainties.extend(structural_reasons)
        event_candidate = "no"
        if config.events_enabled and not structural_reasons:
            event_candidate = "yes"
        status = "closed" if closed else "open"
        thread = DiscourseThread(
            discourse_thread_id=thread_id,
            fragment_ids=fragment_ids,
            claim_ids=claim_ids,
            bundle_ids=bundle_ids,
            conversation_opener_fragment_ids=opener_ids,
            speaker_ids=speaker_ids,
            mentioned_person_ids=mentioned_person_ids,
            subject_ids=subject_ids,
            object_refs=tuple(object_refs),
            state_sequence=state_sequence,
            closure_reason=closure_reason,
            start_fragment_id=start_fragment_id,
            end_fragment_id=end_fragment_id,
            start_time_offset_seconds=start_value,
            start_time_source=start_source,
            end_time_offset_seconds=end_value,
            end_time_source=end_source,
            information_value=information_value,
            event_completeness=event_completeness,
            event_candidate=event_candidate,
            context_relation_ids=relation_ids,
            source_message_ids=source_message_ids,
            evidence_refs=evidence_refs,
            uncertainties=_unique(uncertainties),
            open_boundary=not closed,
            status=status,
            confidence="medium" if event_candidate == "yes" else "low",
            analysis_run_id=config.analysis_run_id,
            source=config.source_marker,
            model_id=config.model_id,
            model_version=config.model_version,
            prompt_version=config.prompt_version,
            input_hash=thread_input_hash,
            cache_key="thread:%s:%s" % (THREAD_RULESET_VERSION, thread_input_hash),
        )
        result.append(thread)
    return tuple(result)


def _event_block_reasons(
    fragments: Sequence[BundleFragment],
    claims: Sequence[BundleClaim],
    object_refs: Sequence[Mapping[str, Any]],
    subject_ids: Sequence[str],
    actions: Sequence[str],
    state_sequence: Sequence[str],
    evidence_refs: Sequence[Mapping[str, Any]],
    relation_ids: Sequence[str],
    relations: Mapping[str, Any],
    relation_flags: Mapping[str, Set[str]],
    scopes: Set[Optional[Tuple[str, str]]],
    config: EventDerivationConfig,
    existing_uncertainties: Sequence[str],
) -> Tuple[str, ...]:
    reasons: List[str] = []
    if not subject_ids:
        reasons.append("subject_unknown")
    elif len(set(subject_ids)) > 1:
        reasons.append("subject_ambiguous")
    object_ids = {str(item.get("id")) for item in object_refs if _known(item.get("id"))}
    if not object_ids:
        reasons.append("object_unknown")
    elif len(object_ids) > 1:
        reasons.append("object_conflict")
    if not actions:
        reasons.append("action_unknown")
    if not state_sequence or all(state == UNKNOWN for state in state_sequence):
        reasons.append("state_unknown")
    valid_evidence = []
    for ref in evidence_refs:
        if _known(ref.get("id") or ref.get("evidence_id")) and _known(ref.get("type")):
            span = ref.get("span")
            if span is None or _span(span) is not None:
                valid_evidence.append(ref)
    if not valid_evidence:
        reasons.append("evidence_insufficient")
    for fragment in fragments:
        if fragment.is_silent or fragment.is_opener:
            reasons.append("non_substantive_fragment")
        elif fragment.role == "context_only":
            reasons.append("context_only_fragment")
        else:
            # Event materialization is deliberately stricter than thread
            # organization: every substantive member must carry the core
            # slots it contributes.  An unknown slot may remain safely on an
            # open thread, but cannot be silently filled from a sibling.
            if not _known(fragment.subject_id):
                reasons.append("subject_unknown")
            if not fragment.object_known:
                reasons.append("object_unknown")
            if not _fragment_actions(fragment):
                reasons.append("action_unknown")
            if fragment.state not in KNOWN_STATES - {UNKNOWN}:
                reasons.append("state_unknown")
            if not _evidence_refs(fragment):
                reasons.append("evidence_insufficient")
        if fragment.object_resolution not in KNOWN_RESOLUTIONS:
            reasons.append("object_resolution_invalid")
        if fragment.object_resolution == "explicit" and fragment.object_known and not fragment.object_evidence_refs:
            reasons.append("object_evidence_insufficient")
        if fragment.object_resolution == "inherited":
            if not fragment.object_inherited_from_id:
                reasons.append("object_inheritance_source_missing")
            elif not any(
                fragment.object_inherited_from_id in (_anchor_ids(relations.get(relation_id))[0], _anchor_ids(relations.get(relation_id))[1])
                and _relation_label(relations.get(relation_id)) in LINKING_RELATION_LABELS
                for relation_id in relation_ids
                if relations.get(relation_id) is not None
            ):
                reasons.append("object_inheritance_evidence_insufficient")
        if fragment.state != UNKNOWN and fragment.state_evidence not in {"explicit", "inherited"}:
            reasons.append("state_evidence_insufficient")
        if fragment.state in TERMINAL_STATES and (fragment.is_silent or fragment.is_opener):
            reasons.append("terminal_non_substantive")
    for claim in claims:
        span = _span(claim.evidence_span)
        if span is None or len(claim.evidence_refs) != 1:
            reasons.append("claim_evidence_invalid")
    if None in scopes:
        reasons.append("scope_unknown")
    if len(scopes) > 1:
        reasons.append("cross_chat_conflict")
    if any(code in existing_uncertainties for code in {"must_not_link", "relation_conflict", "cross_chat_conflict"}):
        reasons.append("must_not_link")
    if any(relation_id in relation_flags.get("blocked", set()) for relation_id in relation_ids):
        reasons.append("relation_conflict")
    model_status = config.normalized_model_status
    if model_status in BLOCKED_MODEL_STATUSES:
        reasons.append("model_" + model_status)
    elif model_status not in {"none", "accepted"}:
        reasons.append("model_unavailable")
    if not config.events_enabled:
        reasons.append("event_materialization_disabled")
    return _unique(reasons)


def _make_event_candidate(
    thread: DiscourseThread,
    fragments: Mapping[str, BundleFragment],
    claims: Mapping[str, BundleClaim],
    config: EventDerivationConfig,
) -> Optional[EventCandidate]:
    if thread.event_candidate != "yes" or not config.events_enabled:
        return None
    member_fragments = [fragments[item] for item in thread.fragment_ids if item in fragments]
    member_claims = [claims[item] for item in thread.claim_ids if item in claims]
    object_ids = _unique(item.get("id") for item in thread.object_refs)
    if len(object_ids) != 1 or not thread.subject_ids or not thread.state_sequence or thread.state_sequence == (UNKNOWN,):
        return None
    object_id = object_ids[0]
    object_entries = [item for item in thread.object_refs if item.get("id") == object_id]
    object_resolution = "explicit" if any(item.get("resolution") == "explicit" for item in object_entries) else "inherited"
    inherited_from = next((item.get("source_id") for item in object_entries if item.get("source_id")), None)
    actions = _unique(action for fragment in member_fragments for action in fragment.actions)
    state = next((item for item in reversed(thread.state_sequence) if item != UNKNOWN), UNKNOWN)
    event_id = "EVENT_CANDIDATE_" + stable_hash(
        {
            "thread_id": thread.discourse_thread_id,
            "fragment_ids": thread.fragment_ids,
            "claim_ids": thread.claim_ids,
            "object_id": object_id,
            "state": state,
            "ruleset": EVENT_RULESET_VERSION,
        }
    )[:20]
    input_hash = stable_hash({"thread": thread.input_hash, "event_id": event_id})
    return EventCandidate(
        event_candidate_id=event_id,
        thread_id=thread.discourse_thread_id,
        bundle_ids=thread.bundle_ids,
        fragment_ids=thread.fragment_ids,
        claim_ids=thread.claim_ids,
        source_message_ids=thread.source_message_ids,
        subject_id=thread.subject_ids[0],
        subject_type=next(
            (fragment.subject_type for fragment in member_fragments if fragment.subject_id == thread.subject_ids[0] and _known(fragment.subject_type)),
            UNKNOWN,
        ),
        object_id=object_id,
        object_resolution=object_resolution,
        object_inherited_from_id=inherited_from,
        actions=actions,
        state=state,
        state_sequence=thread.state_sequence,
        event_type=UNKNOWN,
        modality=next((fragment.modality for fragment in member_fragments if fragment.modality != UNKNOWN), UNKNOWN),
        start_time_offset_seconds=thread.start_time_offset_seconds,
        start_time_source=thread.start_time_source,
        end_time_offset_seconds=thread.end_time_offset_seconds,
        end_time_source=thread.end_time_source,
        evidence_refs=thread.evidence_refs,
        context_relation_ids=thread.context_relation_ids,
        uncertainties=thread.uncertainties,
        confidence="high" if thread.event_completeness == "sufficient" else "medium",
        confidence_score=0.9 if thread.event_completeness == "sufficient" else 0.8,
        status="candidate",
        materialized=True,
        analysis_run_id=config.analysis_run_id,
        source=config.source_marker,
        model_id=config.model_id,
        model_version=config.model_version,
        prompt_version=config.prompt_version,
        input_hash=input_hash,
        cache_key="event-candidate:%s:%s" % (EVENT_RULESET_VERSION, input_hash),
    )


def derive_discourse_and_events(
    value: Any,
    *,
    config: Optional[EventDerivationConfig] = None,
) -> DiscourseEventResult:
    """Build open threads and optionally materialize strictly gated candidates."""

    cfg = config or EventDerivationConfig()
    fragments, claims, bundles, relations, order = _normalise_inputs(value)
    uf, relation_fragments, relation_flags, _unused = _fragment_mappings(
        fragments, claims, bundles, relations, order
    )
    threads = _component_data(
        fragments,
        claims,
        bundles,
        relations,
        order,
        uf,
        relation_fragments,
        relation_flags,
        cfg,
    )
    candidates = tuple(
        candidate
        for thread in threads
        for candidate in (_make_event_candidate(thread, fragments, claims, cfg),)
        if candidate is not None
    )
    input_hash = stable_hash(
        {
            "threads": [thread.input_hash for thread in threads],
            "events": [candidate.input_hash for candidate in candidates],
            "config": {
                "events_enabled": cfg.events_enabled,
                "model_status": cfg.normalized_model_status,
                "source": cfg.source_marker,
            },
        }
    )
    return DiscourseEventResult(
        threads=threads,
        event_candidates=candidates,
        source_marker=cfg.source_marker,
        fallback_source=cfg.legacy_fallback_source,
        analysis_run_id=cfg.analysis_run_id,
        input_hash=input_hash,
        cache_key="discourse-event:%s:%s" % (RULESET_VERSION, input_hash),
    )


def build_discourse_threads(
    value: Any,
    *,
    config: Optional[EventDerivationConfig] = None,
) -> DiscourseEventResult:
    """Thread-only facade; event candidates remain disabled by construction."""

    cfg = config or EventDerivationConfig()
    if cfg.events_enabled:
        cfg = EventDerivationConfig(
            materialize_events=False,
            event_materialization_enabled=False,
            model_status=cfg.model_status,
            model_id=cfg.model_id,
            model_version=cfg.model_version,
            prompt_version=cfg.prompt_version,
            analysis_run_id=cfg.analysis_run_id,
            source_marker=cfg.source_marker,
            legacy_fallback_source=cfg.legacy_fallback_source,
            allow_cross_chat=cfg.allow_cross_chat,
        )
    return derive_discourse_and_events(value, config=cfg)


def materialize_event_candidates(
    value: Any,
    *,
    config: Optional[EventDerivationConfig] = None,
) -> Tuple[EventCandidate, ...]:
    """Return candidates only; disabled/pending/fallback paths return empty."""

    return derive_discourse_and_events(value, config=config or EventDerivationConfig(materialize_events=True)).event_candidates


build_event_candidates = materialize_event_candidates
derive_threads_and_events = derive_discourse_and_events


class ShadowSemanticStore:
    """Small append-only in-memory store for thread/event shadow artifacts."""

    def __init__(self, *, source_marker_value: str = SEMANTIC_SHADOW_SOURCE) -> None:
        self.source_marker_value = str(source_marker_value or SEMANTIC_SHADOW_SOURCE)
        self._threads: Dict[str, DiscourseThread] = {}
        self._events: Dict[str, EventCandidate] = {}

    def append(self, result: DiscourseEventResult) -> None:
        for thread in result.threads:
            prior = self._threads.get(thread.discourse_thread_id)
            if prior is not None and prior.input_hash != thread.input_hash:
                raise ValueError("conflicting discourse thread id: %s" % thread.discourse_thread_id)
            self._threads[thread.discourse_thread_id] = thread
        for event in result.event_candidates:
            prior = self._events.get(event.event_candidate_id)
            if prior is not None and prior.input_hash != event.input_hash:
                raise ValueError("conflicting event candidate id: %s" % event.event_candidate_id)
            self._events[event.event_candidate_id] = event

    put = append

    def threads(self) -> Tuple[DiscourseThread, ...]:
        return tuple(self._threads.values())

    def events(self) -> Tuple[EventCandidate, ...]:
        return tuple(self._events.values())

    def get_thread(self, thread_id: str) -> Optional[DiscourseThread]:
        return self._threads.get(str(thread_id))

    def get_event(self, event_id: str) -> Optional[EventCandidate]:
        return self._events.get(str(event_id))

    def snapshot(self) -> Dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threads": [item.to_dict() for item in self.threads()],
            "event_candidates": [item.to_dict() for item in self.events()],
            "source_marker": self.source_marker_value,
            "schema_version": SCHEMA_VERSION,
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "pipeline_version": THREAD_PIPELINE_VERSION,
            "ruleset_version": RULESET_VERSION,
        }


SemanticShadowStore = ShadowSemanticStore


def legacy_fallback_projection(
    value: DiscourseEventResult,
    *,
    config: Optional[EventDerivationConfig] = None,
) -> Dict[str, Any]:
    """Return an explicit, body-free marker for old-chain fallback callers."""

    cfg = config or EventDerivationConfig()
    return {
        **legacy_fallback_marker(cfg),
        "thread_ids": [thread.discourse_thread_id for thread in value.threads],
        "event_candidate_ids": [candidate.event_candidate_id for candidate in value.event_candidates],
        "input_hash": value.input_hash,
        "cache_key": value.cache_key,
    }


v2_result_to_legacy_preview = legacy_fallback_projection


__all__ = [
    "THREAD_PIPELINE_VERSION",
    "EVENT_PIPELINE_VERSION",
    "PIPELINE_VERSION",
    "RULESET_VERSION",
    "THREAD_RULESET_VERSION",
    "EVENT_RULESET_VERSION",
    "SEMANTIC_SHADOW_SOURCE",
    "LEGACY_FALLBACK_SOURCE",
    "THREAD_STATUSES",
    "EVENT_CANDIDATE_STATUSES",
    "MODEL_STATUSES",
    "KNOWN_STATES",
    "KNOWN_RESOLUTIONS",
    "THREAD_RELATION_LABELS",
    "LINKING_RELATION_LABELS",
    "RELATION_LABEL_ALIASES",
    "MNL_CODES",
    "EventDerivationConfig",
    "DiscourseThread",
    "EventCandidate",
    "DiscourseEventResult",
    "ShadowSemanticStore",
    "SemanticShadowStore",
    "legacy_fallback_marker",
    "source_marker",
    "legacy_fallback_projection",
    "v2_result_to_legacy_preview",
    "build_discourse_threads",
    "materialize_event_candidates",
    "build_event_candidates",
    "derive_discourse_and_events",
    "derive_threads_and_events",
]
