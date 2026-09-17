"""Pure, context-first extraction for the semantic shadow path.

This module deliberately stops at a small, auditable context layer.  It turns
an explicitly supplied sequence of messages (and, optionally, dialogue
segments) into actor, object and state references plus *relation candidates*.
It does not read a database, private/raw message fields, frozen annotation
labels, application configuration, or the production event/card pipeline.

The extractor is intentionally conservative:

* a message speaker is kept separate from people mentioned in the text;
* a subject is not silently assumed to be the speaker when the sentence has
  no subject;
* object references are marked ``explicit``, ``inherited`` or ``unknown``;
* a nearby timestamp is only weak evidence and can never create a relation on
  its own;
* an empty/silent turn never implies that a previous problem was resolved;
* greetings and context-only turns remain in the result, including an opener;
* relation candidates are graph hints only.  No event IDs are created and no
  messages are merged.

The rules are small on purpose.  They are a synthetic-testable baseline for
the next audit step, not a claim that Chinese short-message understanding is
solved.  Callers may supply richer public fields in synthetic fixtures (for
example ``mentioned_persons`` or ``object``); private/raw payloads are
ignored.
"""

from __future__ import annotations

from .dialogue_segments import is_context_only_text, is_topic_bearing

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
import unicodedata
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


# These values intentionally mirror the public dialogue-segment roles without
# importing the segmenter.  Keeping this module import-free makes it useful in
# a fully synthetic/offline harness and prevents accidental production wiring.
ROLE_CONVERSATION_OPENER = "conversation_opener"
ROLE_CONTEXT_ONLY = "context_only"
ROLE_SUBSTANTIVE = "substantive"

RESOLUTION_EXPLICIT = "explicit"
RESOLUTION_INHERITED = "inherited"
RESOLUTION_UNKNOWN = "unknown"

# Canonical certainty/modality values used by the Stage1 projection.  The
# fragment's ``intent``/``claim_role`` carries the speech act (question,
# request, and so on); keeping those axes separate avoids treating a question
# as a factual state assertion.
MODALITY_CERTAIN = "certain"
MODALITY_PROBABLE = "probable"
MODALITY_POSSIBLE = "possible"
MODALITY_REQUIRED = "required"
MODALITY_DESIRED = "desired"
MODALITY_UNKNOWN = "unknown"
# Historical descriptive aliases retained for callers of the first synthetic
# draft.  DTO values use the canonical values above.
MODALITY_ASSERTED = MODALITY_CERTAIN
MODALITY_QUESTION = MODALITY_UNKNOWN
MODALITY_HEDGED = MODALITY_POSSIBLE
MODALITY_REPORTED = MODALITY_PROBABLE
MODALITY_HYPOTHETICAL = MODALITY_POSSIBLE
MODALITY_SUGGESTION = MODALITY_DESIRED
MODALITY_VALUES = frozenset(
    {
        MODALITY_CERTAIN,
        MODALITY_PROBABLE,
        MODALITY_POSSIBLE,
        MODALITY_REQUIRED,
        MODALITY_DESIRED,
        MODALITY_UNKNOWN,
    }
)

# The public relation label is a frozen seven-value vocabulary.  Legacy
# descriptive names below are input/diagnostic subtypes only.
LABEL_CONTINUES = "continues"
LABEL_ELABORATES = "elaborates"
LABEL_ANSWERS = "answers"
LABEL_CONTRASTS = "contrasts"
LABEL_TOPIC_SHIFT = "topic_shift"
LABEL_POSSIBLY_RELATED = "possibly_related"
LABEL_INSUFFICIENT = "insufficient"
REL_CONTINUES = LABEL_CONTINUES
REL_ELABORATES = LABEL_ELABORATES
REL_ANSWERS = LABEL_ANSWERS
REL_CONTRASTS = LABEL_CONTRASTS
# Compatibility aliases accepted by older synthetic callers.  Exported
# relation DTOs always normalize these to the LABEL_* values above.
REL_CONTINUATION = "continuation"
REL_OBJECT_INHERITANCE = "object_inheritance"
REL_STATE_TRANSITION = "state_transition"
REL_CONTRAST = "contrast"
REL_TOPIC_SHIFT = "topic_shift"
REL_QUESTION_ANSWER = "question_answer"
REL_REPLY = "reply"

# Stage1's frozen public state vocabulary.  Lexical/detail cues stay in
# ``StateAssertion.state_detail``; they must not widen this slot.  ``reported``
# and ``recurring`` are accepted as input/detail aliases, but the public six
# values follow the versioned design/gold contract.
STATE_PLANNED = "planned"
STATE_ONGOING = "ongoing"
STATE_RESOLVED = "resolved"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"
STATE_VALUES = frozenset(
    {STATE_PLANNED, STATE_ONGOING, STATE_RESOLVED, STATE_FAILED, STATE_CANCELLED, STATE_UNKNOWN}
)
# Input-only compatibility names.  They are mapped to the frozen output
# values and are intentionally not exported as public state vocabulary.
STATE_REPORTED = "reported"
STATE_RECURRING = "recurring"
STATE_ACTIVE = STATE_ONGOING
STATE_BLOCKED = STATE_ONGOING

RELATION_LABELS = frozenset(
    {
        "continues",
        "elaborates",
        "answers",
        "contrasts",
        "topic_shift",
        "possibly_related",
        "insufficient",
    }
)
RELATION_STRENGTHS = frozenset({"strong", "medium", "weak", "none"})
ACTOR_ROLES = frozenset({"speaker", "mentioned_person", "subject"})
OBJECT_RESOLUTIONS = frozenset({RESOLUTION_EXPLICIT, RESOLUTION_INHERITED, RESOLUTION_UNKNOWN})
FRAGMENT_TYPES = frozenset(
    {
        "conversation_opener",
        "statement",
        "question",
        "request",
        "answer",
        "acknowledgement",
        "reaction",
        "context",
        "media",
        "unknown",
    }
)
CLAIM_TYPES = frozenset({"fact", "opinion", "question", "suggestion", "hypothesis"})
ARGUMENT_ROLES = frozenset({"subject", "object", "agent", "recipient", "source", "target", "unknown"})
ENTITY_TYPES = frozenset({"person", "object", "group", "organization", "unknown"})


def _confidence_level(score: Any) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    return "high" if value >= 0.85 else "medium" if value >= 0.6 else "low"


def _jsonable(value: Any) -> Any:
    """Return ordinary JSON-compatible values for the DTOs."""

    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


class SerializableContext:
    """Small mapping-friendly mixin used by all context DTOs."""

    def to_dict(self) -> Dict[str, Any]:
        payload = _jsonable(self)
        # Keep version metadata on every exported Stage1 DTO.  It is metadata
        # for replay/audit only; it does not imply an event or a production
        # pipeline integration.
        if isinstance(payload, dict):
            payload.update(
                {
                    "schema_version": "semantic_v2",
                    "context_schema_version": "dialogue_context_v1",
                    "pipeline_version": "stage1_contextual_fragments_v1",
                    "ruleset_version": "synthetic_rules_v1",
                }
            )
        return payload

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class ActorRef(SerializableContext):
    """A located actor occurrence.

    ``actor_id`` is the best available identity key; ``ref_id`` identifies
    this occurrence.  ``role`` is one of ``speaker``, ``mentioned_person`` or
    ``subject``.  A pronoun that cannot be linked to a previous actor remains
    ``resolution=unknown`` instead of being guessed.
    """

    ref_id: str = "ACTOR_REF_UNKNOWN"
    actor_id: str = "ACTOR_UNKNOWN"
    surface_text: str = "unknown"
    role: str = "mentioned_person"
    resolution: str = RESOLUTION_UNKNOWN
    message_id: str = ""
    fragment_id: str = ""
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    source: str = "unknown"
    confidence: float = 0.0

    @property
    def id(self) -> str:
        return self.ref_id

    @property
    def name(self) -> str:
        return self.surface_text

    @property
    def surface(self) -> str:
        return self.surface_text

    @property
    def kind(self) -> str:
        return self.role

    @property
    def value(self) -> str:
        return self.surface_text

    @property
    def is_unknown(self) -> bool:
        return self.resolution == RESOLUTION_UNKNOWN or self.actor_id.startswith("ACTOR_UNKNOWN")

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        source = self.source
        if source.startswith("message.speaker"):
            source = "message_metadata"
        elif source.startswith("text."):
            source = "text"
        elif source.startswith("reply"):
            source = "reply_context"
        elif source.startswith("subject."):
            source = "unknown"
        payload.update(
            {
                "role": "mentioned_person" if self.role == "mentioned" else self.role,
                "actor_role": "mentioned_person" if self.role == "mentioned" else self.role,
                "value": self.surface_text,
                # PersonV2-compatible projection.  Keep the numeric score as
                # an additive field while exposing the contract's level value.
                "person_ref_id": self.ref_id,
                "person_id": "unknown" if self.is_unknown else self.actor_id,
                "surface_redacted": self.surface_text,
                "claim_id": None,
                "evidence_refs": (
                    [
                        {
                            "type": "fragment",
                            "id": self.fragment_id,
                            "span": {"start": self.span_start, "end": self.span_end},
                        }
                    ]
                    if self.fragment_id and not self.is_unknown
                    else []
                ),
                "source": source if source in {"message_metadata", "text", "reply_context", "unknown"} else "unknown",
                "confidence_level": _confidence_level(self.confidence),
                "confidence_score": self.confidence,
            }
        )
        return payload


@dataclass(frozen=True)
class PersonV2(SerializableContext):
    """Contract-shaped person projection for the Stage1 actor occurrences."""

    person_ref_id: str = "PERSON_REF_UNKNOWN"
    person_id: str = "unknown"
    resolution: str = RESOLUTION_UNKNOWN
    role: str = "mentioned_person"
    message_id: str = ""
    fragment_id: str = ""
    claim_id: Optional[str] = None
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    surface_redacted: Optional[str] = None
    source: str = "unknown"
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    confidence: str = "low"
    confidence_score: float = 0.0

    @property
    def id(self) -> str:
        return self.person_ref_id

    @property
    def is_unknown(self) -> bool:
        return self.resolution == RESOLUTION_UNKNOWN or self.person_id == "unknown"

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


# A concise name is useful in synthetic callers while PersonV2 remains the
# explicit contract name used by the audit documents.
PersonRef = PersonV2


@dataclass(frozen=True)
class ArgumentV2(SerializableContext):
    """A subject/object role slot; it is not an event argument graph."""

    argument_id: str = "ARGUMENT_UNKNOWN"
    fragment_id: str = ""
    claim_id: Optional[str] = None
    role: str = "unknown"
    entity_id: str = "unknown"
    entity_type: str = "unknown"
    resolution: str = RESOLUTION_UNKNOWN
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    inherited_from_id: Optional[str] = None
    confidence: str = "low"
    confidence_score: float = 0.0

    @property
    def id(self) -> str:
        return self.argument_id

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


@dataclass(frozen=True)
class ClaimV2(SerializableContext):
    """A minimal claim projection retained before any discourse/event layer."""

    claim_id: str = "CLAIM_UNKNOWN"
    message_id: str = ""
    fragment_id: str = ""
    speaker_id: str = "unknown"
    mentioned_person_ids: Tuple[str, ...] = ()
    subject_id: str = "unknown"
    subject_type: str = "unknown"
    object_id: str = "unknown"
    object_resolution: str = RESOLUTION_UNKNOWN
    object_evidence_refs: Tuple[Dict[str, Any], ...] = ()
    object_inherited_from_id: Optional[str] = None
    claim_type: str = "fact"
    claim_text_redacted: str = ""
    target_entity_ids: Tuple[str, ...] = ()
    event_mention_ids: Tuple[str, ...] = ()
    evidence_spans: Tuple[Dict[str, int], ...] = ()
    stance: str = "unknown"
    polarity: str = "unknown"
    modality: str = MODALITY_UNKNOWN
    status: str = STATE_UNKNOWN
    state: str = STATE_UNKNOWN
    state_evidence: str = RESOLUTION_UNKNOWN
    closure_reason: str = "unknown"
    temporal_qualifier: str = "unknown"
    start_time_offset_seconds: Optional[float] = None
    start_time_source: str = RESOLUTION_UNKNOWN
    end_time_offset_seconds: Optional[float] = None
    end_time_source: str = RESOLUTION_UNKNOWN
    information_value: str = "unknown"
    event_completeness: str = "unknown"
    attribution: str = "direct"
    timestamp_message_id: str = ""
    timestamp: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    context_message_ids: Tuple[str, ...] = ()
    evidence_span: Optional[Dict[str, int]] = None
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    confidence: str = "low"
    confidence_score: float = 0.0

    @property
    def id(self) -> str:
        return self.claim_id

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload["object_evidence_refs"] = list(self.object_evidence_refs)
        payload["evidence_spans"] = list(self.evidence_spans)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


@dataclass(frozen=True)
class ObjectRef(SerializableContext):
    """A located object occurrence with explicit/inherited/unknown provenance."""

    ref_id: str = "OBJECT_REF_UNKNOWN"
    object_id: str = "OBJECT_UNKNOWN"
    surface_text: str = "unknown"
    resolution: str = RESOLUTION_UNKNOWN
    message_id: str = ""
    fragment_id: str = ""
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    source: str = "unknown"
    confidence: float = 0.0
    inherited_from_id: Optional[str] = None

    @property
    def id(self) -> str:
        return self.ref_id

    @property
    def name(self) -> str:
        return self.surface_text

    @property
    def surface(self) -> str:
        return self.surface_text

    @property
    def kind(self) -> str:
        return "object"

    @property
    def object_resolution(self) -> str:
        return self.resolution

    @property
    def value(self) -> str:
        return self.surface_text

    @property
    def is_unknown(self) -> bool:
        return self.resolution == RESOLUTION_UNKNOWN or self.object_id.startswith("OBJECT_UNKNOWN")

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update({"object_resolution": self.resolution, "value": self.surface_text})
        return payload


@dataclass(frozen=True)
class StateAssertion(SerializableContext):
    """A state/status assertion attributed to a subject and object."""

    assertion_id: str = "STATE_ASSERTION_UNKNOWN"
    state: str = "unknown"
    state_detail: str = "unknown"
    modality: str = MODALITY_ASSERTED
    polarity: str = "positive"
    actor_ref_id: str = "ACTOR_REF_UNKNOWN"
    object_ref_id: str = "OBJECT_REF_UNKNOWN"
    evidence_text: str = ""
    message_id: str = ""
    fragment_id: str = ""
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    source: str = "rule"
    confidence: float = 0.0

    @property
    def id(self) -> str:
        return self.assertion_id

    @property
    def status(self) -> str:
        """Compatibility alias: status and state mean the same slot here."""

        return self.state

    @property
    def certainty(self) -> str:
        return self.modality

    @property
    def state_evidence(self) -> str:
        return RESOLUTION_EXPLICIT if self.evidence_text else RESOLUTION_UNKNOWN

    @property
    def subject_ref_id(self) -> str:
        return self.actor_ref_id

    @property
    def is_negated(self) -> bool:
        return self.polarity == "negative"

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update({"status": self.state, "certainty": self.modality, "state_evidence": self.state_evidence})
        return payload


@dataclass(frozen=True)
class Fragment(SerializableContext):
    """One message clause, retaining its speaker, subject and context role."""

    fragment_id: str = "FRAGMENT_UNKNOWN"
    message_id: str = ""
    segment_id: str = ""
    text: str = ""
    span_start: int = 0
    span_end: int = 0
    role: str = ROLE_SUBSTANTIVE
    fragment_type: str = "statement"
    speaker: ActorRef = ActorRef()
    subject: ActorRef = ActorRef()
    mentioned_persons: Tuple[ActorRef, ...] = ()
    actor_refs: Tuple[ActorRef, ...] = ()
    objects: Tuple[ObjectRef, ...] = ()
    state_assertions: Tuple[StateAssertion, ...] = ()
    intent: str = "statement"
    claim_role: str = "fact"
    modality: str = MODALITY_ASSERTED
    speech_modality: str = MODALITY_ASSERTED
    actions: Tuple[str, ...] = ()
    topic_shift: bool = False
    contrast_marker: bool = False
    state_change: bool = False
    is_silent: bool = False
    is_opener: bool = False
    timestamp: Optional[str] = None
    time_offset_seconds: Optional[float] = None
    reply_to_message_id: Optional[str] = None
    evidence_text: str = ""
    speaker_id: str = "unknown"
    mentioned_person_ids: Tuple[str, ...] = ()
    subject_id: str = "unknown"
    subject_type: str = "unknown"
    object_id: str = "unknown"
    object_resolution: str = RESOLUTION_UNKNOWN
    object_inherited_from_id: Optional[str] = None
    object_evidence_refs: Tuple[Dict[str, Any], ...] = ()
    state: str = STATE_UNKNOWN
    state_evidence: str = RESOLUTION_UNKNOWN
    closure_reason: str = "unknown"
    temporal_qualifier: str = "unknown"
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    start_time_source: str = RESOLUTION_UNKNOWN
    end_time_source: str = RESOLUTION_UNKNOWN
    information_value: str = "unknown"
    event_completeness: str = "unknown"
    topic_boundary: str = "none"
    context_message_ids: Tuple[str, ...] = ()
    uncertainties: Tuple[str, ...] = ()
    claim_ids: Tuple[str, ...] = ()
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    source: str = "synthetic_rule"

    @property
    def id(self) -> str:
        return self.fragment_id

    @property
    def message_role(self) -> str:
        return self.role

    @property
    def fragment_text_redacted(self) -> str:
        return self.text

    @property
    def speaker_ref(self) -> ActorRef:
        return self.speaker

    @property
    def subject_ref(self) -> ActorRef:
        return self.subject

    @property
    def mentioned_person_refs(self) -> Tuple[ActorRef, ...]:
        return self.mentioned_persons

    @property
    def kind(self) -> str:
        return self.fragment_type

    @property
    def object_refs(self) -> Tuple[ObjectRef, ...]:
        return self.objects

    @property
    def object_ref(self) -> ObjectRef:
        return self.primary_object

    @property
    def states(self) -> Tuple[StateAssertion, ...]:
        return self.state_assertions

    @property
    def state_assertion(self) -> Optional[StateAssertion]:
        return self.state_assertions[0] if self.state_assertions else None

    @property
    def primary_object(self) -> ObjectRef:
        return self.objects[0] if self.objects else ObjectRef()

    @property
    def primary_actor(self) -> ActorRef:
        return self.subject

    @property
    def speaker_ref_id(self) -> str:
        return self.speaker.ref_id

    @property
    def subject_ref_id(self) -> str:
        return self.subject.ref_id

    @property
    def status(self) -> str:
        return self.state

    @property
    def has_unknown_subject(self) -> bool:
        return self.subject.resolution == RESOLUTION_UNKNOWN

    @property
    def has_unknown_object(self) -> bool:
        return not self.objects or self.objects[0].resolution == RESOLUTION_UNKNOWN

    @property
    def is_topic_boundary(self) -> bool:
        return self.topic_shift

    @property
    def start_time_offset_seconds(self) -> Optional[float]:
        return self.start_time

    @property
    def end_time_offset_seconds(self) -> Optional[float]:
        return self.end_time

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "fragment_text_redacted": self.text,
                "speaker_ref": self.speaker.to_dict(),
                "subject_ref": self.subject.to_dict(),
                "mentioned_person_refs": [item.to_dict() for item in self.mentioned_persons],
                "object_refs": [item.to_dict() for item in self.objects],
                "states": [item.to_dict() for item in self.state_assertions],
                "claim_type": self.claim_role,
                "closure_reason": self.closure_reason,
                "start_time_offset_seconds": self.start_time,
                "end_time_offset_seconds": self.end_time,
            }
        )
        return payload


@dataclass(frozen=True)
class ContextRelation(SerializableContext):
    """A context graph edge candidate; it never denotes an event merge."""

    relation_id: str = "CONTEXT_RELATION_UNKNOWN"
    left_fragment_id: str = ""
    right_fragment_id: str = ""
    # ``relation`` is the controlled public label; ``relation_type`` is a
    # second name for the controlled public label.  ``subtype`` retains the
    # extractor's diagnostic detail (e.g. state_transition).
    relation: str = LABEL_CONTINUES
    relation_type: str = LABEL_CONTINUES
    subtype: str = REL_CONTINUATION
    supporting_signals: Tuple[str, ...] = ()
    conflicting_signals: Tuple[str, ...] = ()
    anchor_type: str = "fragment"
    source_message_ids: Tuple[str, ...] = ()
    evidence_refs: Tuple[Dict[str, Any], ...] = ()
    provenance: Optional[Dict[str, Any]] = None
    object_ids: Tuple[str, ...] = ()
    state_from: Optional[str] = None
    state_to: Optional[str] = None
    time_distance_seconds: Optional[float] = None
    time_evidence: str = "none"
    evidence_strength: str = "weak"
    confidence: float = 0.0
    candidate: bool = True
    requires_review: bool = True
    uncertainties: Tuple[str, ...] = ()
    confidence_level: str = "low"
    annotator_a_label: Optional[str] = None
    annotator_b_label: Optional[str] = None
    adjudication_id: Optional[str] = None
    source: str = "synthetic_rule"

    @property
    def id(self) -> str:
        return self.relation_id

    @property
    def label(self) -> str:
        return self.relation

    @property
    def left_anchor_id(self) -> str:
        return self.left_fragment_id

    @property
    def right_anchor_id(self) -> str:
        return self.right_fragment_id

    @property
    def supporting_slot_codes(self) -> Tuple[str, ...]:
        return self.supporting_signals

    @property
    def conflicting_slot_codes(self) -> Tuple[str, ...]:
        return self.conflicting_signals

    @property
    def evidence_message_ids(self) -> Tuple[str, ...]:
        return self.source_message_ids

    @property
    def explicit_reply_present(self) -> bool:
        return "explicit_reply" in self.supporting_signals

    @property
    def is_event_merge(self) -> bool:
        return False

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "relation": self.relation,
                "relation_type": self.relation,
                "label": self.relation,
                "subtype": self.subtype,
                "relation_subtype": self.subtype,
                "anchor_type": "fragment",
                "left_anchor_id": self.left_fragment_id,
                "right_anchor_id": self.right_fragment_id,
                "supporting_slot_codes": list(self.supporting_signals),
                "conflicting_slot_codes": list(self.conflicting_signals),
                "explicit_reply_present": self.explicit_reply_present,
                "evidence_message_ids": list(self.source_message_ids),
                "confidence": self.confidence_level,
                "confidence_score": self.confidence,
            }
        )
        return payload


@dataclass(frozen=True)
class ContextualFragmentsResult(SerializableContext):
    """Output bundle for the independent context extractor."""

    fragments: Tuple[Fragment, ...] = ()
    actor_refs: Tuple[ActorRef, ...] = ()
    persons: Tuple[PersonV2, ...] = ()
    object_refs: Tuple[ObjectRef, ...] = ()
    state_assertions: Tuple[StateAssertion, ...] = ()
    arguments: Tuple[ArgumentV2, ...] = ()
    claims: Tuple[ClaimV2, ...] = ()
    relations: Tuple[ContextRelation, ...] = ()
    # The explicit alias helps callers use the wording in the design contract.
    context_relations: Tuple[ContextRelation, ...] = ()
    message_ids: Tuple[str, ...] = ()
    segment_ids: Tuple[str, ...] = ()

    @property
    def actors(self) -> Tuple[ActorRef, ...]:
        return self.actor_refs

    @property
    def objects(self) -> Tuple[ObjectRef, ...]:
        return self.object_refs

    @property
    def states(self) -> Tuple[StateAssertion, ...]:
        return self.state_assertions

    @property
    def context_relation_candidates(self) -> Tuple[ContextRelation, ...]:
        return self.relations

    def __iter__(self) -> Iterator[Fragment]:
        # A result is sequence-like for convenient synthetic tests while still
        # carrying the extracted actor/object/state tables and relations.
        return iter(self.fragments)

    def __len__(self) -> int:
        return len(self.fragments)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self.fragments[key]
        return self.to_dict()[key]

    def keys(self) -> Tuple[str, ...]:
        return tuple(self.to_dict())

    def items(self) -> Iterator[Tuple[str, Any]]:
        return iter(self.to_dict().items())

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "fragments": [item.to_dict() for item in self.fragments],
                "actor_refs": [item.to_dict() for item in self.actor_refs],
                "actors": [item.to_dict() for item in self.actor_refs],
                "persons": [item.to_dict() for item in self.persons],
                "object_refs": [item.to_dict() for item in self.object_refs],
                "objects": [item.to_dict() for item in self.object_refs],
                "state_assertions": [item.to_dict() for item in self.state_assertions],
                "states": [item.to_dict() for item in self.state_assertions],
                "arguments": [item.to_dict() for item in self.arguments],
                "claims": [item.to_dict() for item in self.claims],
                "relations": [item.to_dict() for item in self.relations],
                "context_relations": [item.to_dict() for item in self.context_relations],
                "context_relation_candidates": [item.to_dict() for item in self.relations],
            }
        )
        return payload


@dataclass(frozen=True)
class _MessageEntry:
    index: int
    message: Mapping[str, Any]
    message_id: str
    segment_id: str
    role: str
    timestamp: Optional[str]
    time_offset_seconds: Optional[float]
    reply_to_message_id: Optional[str]
    role_explicit: bool = False


@dataclass(frozen=True)
class _Mention:
    surface: str
    start: int
    end: int
    explicit: bool
    source: str


@dataclass(frozen=True)
class _ObjectMention:
    surface: str
    start: int
    end: int
    explicit: bool
    source: str
    # Public object annotations may already carry an opaque/stable identity.
    # Preserve that identity; lexical mentions still leave this unset and are
    # deterministically sluggified at the fragment boundary.
    object_id: Optional[str] = None


@dataclass(frozen=True)
class _StateMention:
    state: str
    surface: str
    start: int
    end: int


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a public mapping/dataclass field only.

    We intentionally do not fall back to ``raw_message``/``private_*`` or
    arbitrary underscored attributes.  This is a boundary guard as well as a
    convenience for synthetic dataclass fixtures.
    """

    if isinstance(value, Mapping):
        return value.get(name, default)
    if name.startswith("_"):
        return default
    return getattr(value, name, default)


def _text_for(message: Any) -> str:
    # ``content`` is the public normalized message field.  ``text`` and
    # ``message_text`` are accepted for tiny synthetic fixtures.  In
    # particular, this function never inspects raw/private/frozen fields.
    for name in ("content", "text", "message_text"):
        value = _field(message, name, None)
        if value is not None:
            return str(value)
    return ""


def _normal_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _compact(value: Any) -> str:
    return re.sub(r"[\W_]+", "", _normal_text(value).casefold(), flags=re.UNICODE)


def _slug(value: Any) -> str:
    compact = _compact(value)
    return compact[:80] or "unknown"


def _stable_id(prefix: str, *values: Any) -> str:
    raw = "\x1f".join(str(value or "") for value in values)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return "%s_%s" % (prefix, digest)


def _message_id(message: Any, index: int) -> str:
    value = _field(message, "message_id", None)
    return str(value) if value is not None and str(value) else "MESSAGE_%06d" % (index + 1)


def _chat_id(message: Any) -> str:
    return str(_field(message, "chat_id", "unknown") or "unknown")


def _account_id(message: Any) -> str:
    return str(_field(message, "account_id", "default") or "default")


def _speaker_identity(message: Any) -> Tuple[str, str, str]:
    value = (
        _field(message, "speaker_id", None)
        or _field(message, "sender_id", None)
        or _field(message, "sender_name", None)
    )
    label = _field(message, "sender_name", None) or value or "unknown"
    if value is None or not str(value).strip():
        return "ACTOR_UNKNOWN", "unknown", "message.speaker.unknown"
    identity = "ACTOR:" + _slug(value)
    return identity, str(label), "message.speaker"


def _timestamp_value(message: Any) -> Optional[str]:
    value = _field(message, "timestamp", None)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _time_offset(message: Any) -> Optional[float]:
    value = _field(message, "time_offset_seconds", None)
    if value is None:
        # A synthetic fixture may use ``time_offset``; it remains explicit
        # ordering metadata, not semantic evidence.
        value = _field(message, "time_offset", None)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _time_value(message: Any) -> Optional[float]:
    offset = _time_offset(message)
    if offset is not None:
        return offset
    timestamp = _field(message, "timestamp", None)
    if timestamp is None:
        return None
    if isinstance(timestamp, datetime):
        parsed = timestamp
    else:
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _reply_to(message: Any) -> Optional[str]:
    for key in (
        "reply_to_message_id",
        "reply_to_id",
        "quoted_message_id",
        "quote_message_id",
        "referenced_message_id",
        "reference_message_id",
        "parent_message_id",
        "in_reply_to",
    ):
        value = _field(message, key, None)
        if value is not None and str(value):
            return str(value)
    return None


def _sequence_value(message: Any) -> Optional[int]:
    value = _field(message, "sequence_in_chat", None)
    if value is None:
        value = _field(message, "sequence", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_SOCIAL_VALUES = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "哈罗",
        "嘿",
        "hi",
        "hello",
        "hey",
        "早上好",
        "上午好",
        "下午好",
        "晚上好",
        "晚安",
        "谢谢",
        "谢谢你",
        "感谢",
        "辛苦",
        "辛苦了",
        "收到",
        "好的",
        "好",
        "嗯",
        "哦",
        "哈哈",
        "ok",
        "okay",
        "thanks",
        "thankyou",
        "在吗",
        "忙吗",
        "最近怎么样",
        "最近好吗",
        "吃饭了吗",
    }
)
_SOCIAL_PARTICLES = frozenset("啊呀哦喔哇啦呢哈嘛了呐")


def is_greeting_or_social(text: Any) -> bool:
    compact = _compact(text)
    compact = "".join(char for char in compact if char not in _SOCIAL_PARTICLES)
    if not compact:
        return False
    if compact in _SOCIAL_VALUES:
        return True
    for value in sorted(_SOCIAL_VALUES, key=len, reverse=True):
        if compact.startswith(value) and compact[len(value) :] in {"你", "您", "呀", "啊", "呢", "了"}:
            return True
    return False


def _mixed_social_prefix(text: Any) -> bool:
    """Whether a social/confirmation token is followed by real content."""

    compact = _compact(text)
    if not compact:
        return False
    prefixes = (
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
        "确认",
        "已确认",
        "已经确认",
        "收到",
        "谢谢",
        "感谢",
        "辛苦",
    )
    return any(compact.startswith(prefix) and compact != prefix for prefix in prefixes)


def _is_silent(text: Any, message_type: Any = "text") -> bool:
    value = _normal_text(text)
    if not value:
        return True
    if value.casefold() in {"[silence]", "<silence>", "[沉默]", "（沉默）", "(silence)"}:
        return True
    # Media-only turns are retained as context but have no textual state.  A
    # marker is useful to synthetic callers and avoids claiming a decode.
    if str(message_type or "text").casefold() not in {"text", "link"} and re.fullmatch(
        r"(?:\[[^\]]+\]|<[^>]+>)", value
    ):
        return True
    return False


def _normalize_role(value: Any) -> str:
    value = str(value or "").strip().casefold()
    if value in {"opener", "conversation opener", ROLE_CONVERSATION_OPENER}:
        return ROLE_CONVERSATION_OPENER
    if value in {"context", "context-only", "context_only", ROLE_CONTEXT_ONLY}:
        return ROLE_CONTEXT_ONLY
    return ROLE_SUBSTANTIVE


def _segment_items(segments: Any) -> Tuple[Sequence[Any], Mapping[str, Any]]:
    """Accept a segment sequence or a public segmentation result object."""

    if segments is None:
        return (), {}
    result_roles: Mapping[str, Any] = {}
    raw = segments
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        candidate = _field(raw, "segments", None)
        if candidate is not None:
            result_roles = _field(raw, "message_roles", {}) or {}
            raw = candidate
    if raw is None:
        return (), result_roles
    try:
        return tuple(raw), result_roles
    except TypeError:
        return (), result_roles


def _build_segment_map(
    segments: Any,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Return message -> segment, message -> role, message -> chat maps."""

    segment_map: Dict[str, str] = {}
    role_map: Dict[str, str] = {}
    chat_map: Dict[str, str] = {}
    items, result_roles = _segment_items(segments)
    for message_id, role in (result_roles or {}).items():
        role_map[str(message_id)] = _normalize_role(role)
    for index, segment in enumerate(items):
        segment_id = str(_field(segment, "segment_id", None) or "SEGMENT_%03d" % (index + 1))
        ids = _field(segment, "message_ids", ()) or ()
        opener_ids = {str(value) for value in (_field(segment, "opener_message_ids", ()) or ())}
        context_ids = {str(value) for value in (_field(segment, "context_message_ids", ()) or ())}
        substantive_ids = {str(value) for value in (_field(segment, "substantive_message_ids", ()) or ())}
        chat_id = _field(segment, "chat_id", None)
        for value in ids:
            message_id = str(value)
            segment_map[message_id] = segment_id
            if message_id in opener_ids:
                role_map[message_id] = ROLE_CONVERSATION_OPENER
            elif message_id in context_ids and message_id not in substantive_ids:
                role_map[message_id] = ROLE_CONTEXT_ONLY
            elif message_id in substantive_ids:
                role_map[message_id] = ROLE_SUBSTANTIVE
            if chat_id is not None:
                chat_map[message_id] = str(chat_id)
    return segment_map, role_map, chat_map


def _message_entries(messages: Iterable[Any], segments: Any) -> Tuple[_MessageEntry, ...]:
    materialized: List[Tuple[int, Any]] = list(enumerate(messages or ()))
    # Synthetic fixtures with explicit ordering metadata should be replayable
    # regardless of input order.  Without metadata preserve caller order.
    if any(_sequence_value(message) is not None or _time_value(message) is not None for _, message in materialized):
        materialized.sort(
            key=lambda pair: (
                _chat_id(pair[1]),
                _account_id(pair[1]),
                _sequence_value(pair[1]) is None,
                _sequence_value(pair[1]) if _sequence_value(pair[1]) is not None else 0,
                _time_value(pair[1]) is None,
                _time_value(pair[1]) if _time_value(pair[1]) is not None else 0.0,
                _message_id(pair[1], pair[0]),
                pair[0],
            )
        )
    segment_map, role_map, chat_map = _build_segment_map(segments)
    seen_ids: set[str] = set()
    output: List[_MessageEntry] = []
    fallback_segment_by_chat: Dict[str, str] = {}
    fallback_index_by_chat: Dict[str, int] = {}
    for index, message in materialized:
        message_id = _message_id(message, index)
        if message_id in seen_ids:
            raise ValueError("duplicate message_id: %s" % message_id)
        seen_ids.add(message_id)
        chat_id = chat_map.get(message_id, _chat_id(message))
        segment_id = (
            segment_map.get(message_id)
            or str(_field(message, "dialogue_segment_id", "") or "")
        )
        if not segment_id:
            if chat_id not in fallback_segment_by_chat:
                fallback_index_by_chat[chat_id] = len(fallback_segment_by_chat) + 1
                fallback_segment_by_chat[chat_id] = "SEGMENT_%s_%03d" % (
                    _slug(chat_id),
                    fallback_index_by_chat[chat_id],
                )
            segment_id = fallback_segment_by_chat[chat_id]
        role_value = role_map.get(message_id)
        role_explicit = role_value is not None
        if role_value is None:
            role_value = _field(message, "dialogue_role", None)
            role_explicit = role_value is not None
        if role_value is None:
            role_value = _field(message, "message_role", None)
            role_explicit = role_value is not None
        if role_value is None:
            # Defer opener-vs-context-only assignment to the small second pass
            # below; every social turn cannot be an opener.
            role_value = (
                ROLE_CONTEXT_ONLY
                if is_context_only_text(
                    _text_for(message),
                    message_type=_field(message, "message_type", "text"),
                )
                else ROLE_SUBSTANTIVE
            )
        role = _normalize_role(role_value)
        message_text = _text_for(message)
        message_type = _field(message, "message_type", "text")
        # Explicit context metadata cannot hide a mixed social turn.  The
        # prefix check is deliberately narrow so a genuine context note such
        # as ``我先记一下`` remains context-only.
        if (
            role in {ROLE_CONVERSATION_OPENER, ROLE_CONTEXT_ONLY}
            and message_text
            and not is_context_only_text(message_text, message_type=message_type)
            and _mixed_social_prefix(message_text)
        ):
            role = ROLE_SUBSTANTIVE
        output.append(
            _MessageEntry(
                index=index,
                message=message if isinstance(message, Mapping) else _public_message_mapping(message),
                message_id=message_id,
                segment_id=segment_id,
                role=role,
                timestamp=_timestamp_value(message),
                time_offset_seconds=_time_offset(message),
                reply_to_message_id=_reply_to(message),
                role_explicit=role_explicit,
            )
        )
    # If callers did not supply segment role metadata, social turns after the
    # first turn in a segment are context-only.  A greeting opener remains in
    # the output rather than being filtered.
    seen_by_segment: Dict[str, bool] = {}
    refined: List[_MessageEntry] = []
    for entry in output:
        social = is_greeting_or_social(_text_for(entry.message))
        if social and entry.role in {ROLE_CONVERSATION_OPENER, ROLE_CONTEXT_ONLY}:
            role = (
                ROLE_CONVERSATION_OPENER
                if entry.role == ROLE_CONVERSATION_OPENER and entry.role_explicit
                else ROLE_CONVERSATION_OPENER if not seen_by_segment.get(entry.segment_id) else ROLE_CONTEXT_ONLY
            )
            seen_by_segment[entry.segment_id] = True
            if role != entry.role:
                entry = _MessageEntry(
                    entry.index,
                    entry.message,
                    entry.message_id,
                    entry.segment_id,
                    role,
                    entry.timestamp,
                    entry.time_offset_seconds,
                    entry.reply_to_message_id,
                    entry.role_explicit,
                )
        elif entry.role == ROLE_CONVERSATION_OPENER:
            seen_by_segment.setdefault(entry.segment_id, True)
        elif entry.role == ROLE_SUBSTANTIVE:
            seen_by_segment.setdefault(entry.segment_id, True)
        elif entry.role == ROLE_CONTEXT_ONLY:
            seen_by_segment.setdefault(entry.segment_id, True)
        elif is_greeting_or_social(_text_for(entry.message)):
            role = ROLE_CONVERSATION_OPENER if not seen_by_segment.get(entry.segment_id) else ROLE_CONTEXT_ONLY
            seen_by_segment[entry.segment_id] = True
            entry = _MessageEntry(
                entry.index,
                entry.message,
                entry.message_id,
                entry.segment_id,
                role,
                entry.timestamp,
                entry.time_offset_seconds,
                entry.reply_to_message_id,
                entry.role_explicit,
            )
        refined.append(entry)
    return tuple(refined)


def _public_message_mapping(message: Any) -> Dict[str, Any]:
    """Copy only known public fields from a dataclass fixture."""

    names = (
        "message_id",
        "chat_id",
        "account_id",
        "speaker_id",
        "sender_id",
        "sender_name",
        "content",
        "text",
        "message_text",
        "message_type",
        "timestamp",
        "time_offset_seconds",
        "time_offset",
        "sequence_in_chat",
        "sequence",
        "reply_to_message_id",
        "reply_to_id",
        "quoted_message_id",
        "quote_message_id",
        "referenced_message_id",
        "reference_message_id",
        "parent_message_id",
        "in_reply_to",
        "dialogue_segment_id",
        "dialogue_role",
        "message_role",
        "mentioned_persons",
        "mentioned_people",
        "person_mentions",
        "mentions",
        "object",
        "object_ref",
        "objects",
        "object_refs",
        "target",
        "target_entity",
    )
    return {name: _field(message, name, None) for name in names if _field(message, name, None) is not None}


def _split_clauses(text: str) -> Tuple[Tuple[int, int, str], ...]:
    """Split only at strong sentence/viewpoint boundaries, preserving spans."""

    value = str(text or "")
    if not value.strip():
        return ((0, 0, ""),)
    boundaries: List[int] = []
    # A social opener and a substantive request in one message are separate
    # fragments.  The opener is retained; it must not hide the question/state
    # that follows it.
    opener_boundary = re.match(
        r"\s*(?:你好|您好|嗨|哈喽|哈罗|嘿|早上好|上午好|下午好|晚上好|晚安|在吗|hi|hello|hey|thanks?|谢谢|感谢|辛苦(?:了)?)[,，]\s*",
        value,
        flags=re.IGNORECASE,
    )
    if opener_boundary is not None and opener_boundary.end() < len(value):
        boundaries.append(opener_boundary.end())
    for match in re.finditer(r"(?<=[。！？!?；;\n])", value):
        boundaries.append(match.end())
    # Chinese contrast/new-viewpoint markers and common English connectors.
    for match in re.finditer(
        r"[,，]\s*(?=(?:换个话题|另一个话题|新话题|但是|但|不过|然而|可是|后来|现在|同时|另外|对了|顺便|and\b|but\b|however\b|then\b))",
        value,
        flags=re.IGNORECASE,
    ):
        boundaries.append(match.end())
    # ``张三说……，李四认为……`` is two viewpoints even without ``但``.
    for match in re.finditer(
        r"[,，]\s*(?=(?:@?[\u3400-\u4dbf\u4e00-\u9fff]{2,8}|[A-Z][A-Za-z0-9_.-]{1,32})\s*(?:说|认为|觉得|表示|提到|问|反馈|回复|says?\b|thinks?\b))",
        value,
        flags=re.IGNORECASE,
    ):
        boundaries.append(match.end())
    for match in re.finditer(
        r"\s+(?=(?:but|however|although)\s+(?:[A-Z][A-Za-z0-9_.-]{1,32}|[\u3400-\u4dbf\u4e00-\u9fff]{2,8})\s*(?:says?\b|thinks?\b|认为|觉得|表示|说))",
        value,
        flags=re.IGNORECASE,
    ):
        boundaries.append(match.end())
    boundaries = sorted(set(boundaries))
    starts = [0] + boundaries
    pieces: List[Tuple[int, int, str]] = []
    for left, right in zip(starts, starts[1:] + [len(value)]):
        raw = value[left:right]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        start = left + leading
        end = left + trailing
        if start < end:
            pieces.append((start, end, value[start:end]))
    return tuple(pieces or ((0, len(value), value),))


_PERSON_PRONOUNS = frozenset({"他", "她", "他们", "她们", "对方", "客户", "老板", "同事", "用户", "负责人", "he", "she", "they", "them", "customer", "owner", "user"})
_FIRST_PERSON = frozenset({"我", "我们", "本人", "i", "we", "me", "us"})
_SECOND_PERSON = frozenset({"你", "您", "你们", "you"})
_PERSON_VERBS = r"说|认为|觉得|表示|提到|问|反馈|回复|告诉|联系|担心|建议|think|thinks|believe|believes|say|says"
_EXPLICIT_PERSON_RE = re.compile(
    r"(?<![\w\u3400-\u4dbf\u4e00-\u9fff])(@?[\u3400-\u4dbf\u4e00-\u9fff]{2,8}|[A-Z][A-Za-z0-9_.-]{1,32})"
    r"(?=\s*(?:" + _PERSON_VERBS + r")|\s*[:：])"
)
_MULTI_PERSON_RE = re.compile(
    r"(?P<left>[\u3400-\u4dbf\u4e00-\u9fff]{2,4})\s*(?:和|与|及|、)\s*"
    r"(?P<right>[\u3400-\u4dbf\u4e00-\u9fff]{2,4}?)"
    r"(?=\s*(?:都|一起|分别)?\s*(?:说|认为|觉得|表示|提到|问|反馈|回复))"
)
_PRONOUN_RE = re.compile(r"(?<![\w])(?:他们|她们|对方|客户|老板|同事|负责人|用户|我|我们|本人|你|您|你们|他|她|he|she|they|them|customer|owner|user)(?![\w])", flags=re.IGNORECASE)
_NON_PERSON_SURFACES = frozenset(
    {
        "后来",
        "现在",
        "已经",
        "还在",
        "仍然",
        "可能",
        "应该",
        "可以",
        "无法",
        "没有",
        "只是",
        "这个",
        "那个",
        "问题",
        "事情",
        "接口",
        "服务",
        "账号",
        "额度",
        "模型",
        "方案",
        "价格",
        "费用",
        "成本",
        "状态",
        "恢复",
        "失败",
        "正常",
        "成功",
        "完成",
    }
)
_DISCOURSE_PREFIXES = (
    "by the way",
    "however",
    "although",
    "but",
    "then",
    "and",
    "但是",
    "不过",
    "然而",
    "可是",
    "后来",
    "现在",
    "同时",
    "另外",
    "对了",
    "顺便",
    "但",
)


def _iter_public_person_mentions(message: Mapping[str, Any]) -> Tuple[_Mention, ...]:
    values: List[Any] = []
    for key in ("mentioned_persons", "mentioned_people", "person_mentions", "mentions"):
        candidate = message.get(key)
        if candidate is None:
            continue
        if isinstance(candidate, (str, bytes)) or isinstance(candidate, Mapping):
            values.append(candidate)
        else:
            try:
                values.extend(candidate)
            except TypeError:
                values.append(candidate)
    output: List[_Mention] = []
    for value in values:
        if isinstance(value, Mapping):
            kind = str(value.get("type", value.get("mention_type", "person")) or "person").casefold()
            if kind not in {"person", "actor", "mentioned_person", "human"}:
                continue
            surface = value.get("surface_text", value.get("surface", value.get("name", value.get("label", value.get("id", "")))))
            start = value.get("span_start", value.get("start", 0))
            end = value.get("span_end", value.get("end", 0))
        else:
            surface = value
            start, end = 0, 0
        surface = _normal_text(surface)
        if not surface:
            continue
        try:
            start_int, end_int = int(start), int(end)
        except (TypeError, ValueError):
            start_int, end_int = 0, 0
        output.append(_Mention(surface, start_int, end_int, True, "message.mentioned_persons"))
    return tuple(output)


def _person_mentions(clause: str, clause_start: int, message: Mapping[str, Any]) -> Tuple[_Mention, ...]:
    found: List[_Mention] = list(_iter_public_person_mentions(message))
    grouped_spans: List[Tuple[int, int]] = []
    for match in _MULTI_PERSON_RE.finditer(clause):
        grouped_spans.append(match.span())
        for group_name in ("left", "right"):
            start, end = match.span(group_name)
            found.append(
                _Mention(
                    match.group(group_name),
                    clause_start + start,
                    clause_start + end,
                    True,
                    "text.person_name",
                )
            )
    for match in _EXPLICIT_PERSON_RE.finditer(clause):
        surface = match.group(1)
        start = clause_start + match.start(1)
        for prefix in _DISCOURSE_PREFIXES:
            if surface.casefold().startswith(prefix.casefold()) and len(surface) > len(prefix):
                surface = surface[len(prefix) :]
                start += len(prefix)
                break
        if _compact(surface) in _NON_PERSON_SURFACES:
            continue
        if any(match.start(1) < right and match.end(1) > left for left, right in grouped_spans):
            continue
        found.append(_Mention(surface, start, start + len(surface), True, "text.person_name"))
    for match in _PRONOUN_RE.finditer(clause):
        surface = match.group(0)
        # ``它`` is an object pronoun and deliberately does not enter this
        # actor list.  The actor regex excludes it, too, but the guard keeps
        # this invariant obvious when the rule list changes.
        if surface == "它":
            continue
        found.append(_Mention(surface, clause_start + match.start(), clause_start + match.end(), False, "text.pronoun"))
    # Stable de-duplication by span/surface; public person fields with no span
    # are retained once even if a textual name rule also sees them.
    unique: List[_Mention] = []
    seen: set[Tuple[str, int, int]] = set()
    for item in sorted(found, key=lambda value: (value.start, value.end, value.surface)):
        key = (_compact(item.surface), item.start, item.end)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


_OBJECT_COMPOUND_RE = re.compile(
    r"(?:GPT(?:[-_ ]?[0-9A-Za-z.]+)?|ChatGPT|Codex|Claude|DeepSeek|OpenAI|multica|GitHub|Linux\.do)"
    r"\s*(?:额度|配额|账号|账户|接口|价格|费用|成本|消耗|状态|服务|模型|API)",
    flags=re.IGNORECASE,
)
_OBJECT_KNOWN_RE = re.compile(
    r"GPT(?:[-_ ]?[0-9A-Za-z.]+)?|ChatGPT|Codex|Claude|DeepSeek|OpenAI|multica|GitHub|Linux\.do|"
    r"API|接口|账号|账户|额度|配额|模型|服务|方案|项目|密码|验证码|价格|费用|成本|订阅|会议|课程|课表|选课|报名|教务|链接|订单|地址|快递|部署|注册|登录|登陆|系统|网络|任务|文件|图片|消息|问题|故障|错误|报错|消耗",
    flags=re.IGNORECASE,
)
_OBJECT_PRONOUN_RE = re.compile(r"(?:它|这个|那个|这件事|这块|那块|上述|前面|该问题|该方案|同样|还是|it|this|that|the same)", flags=re.IGNORECASE)
_OBJECT_MARKED_RE = re.compile(r"(?:这|那|该)(?:个|件|块)?(?:接口|方案|问题|服务|账号|模型|项目|事情|课程|会议|价格|链接)")


def _object_values(message: Mapping[str, Any]) -> List[Any]:
    values: List[Any] = []
    for key in ("object", "object_ref", "objects", "object_refs", "target", "target_entity"):
        candidate = message.get(key)
        if candidate is None:
            continue
        if isinstance(candidate, (str, bytes)) or isinstance(candidate, Mapping):
            values.append(candidate)
        else:
            try:
                values.extend(candidate)
            except TypeError:
                values.append(candidate)
    return values


def _object_mentions(clause: str, clause_start: int, message: Mapping[str, Any]) -> Tuple[_ObjectMention, ...]:
    found: List[_ObjectMention] = []
    for value in _object_values(message):
        if isinstance(value, Mapping):
            surface = value.get("surface_text", value.get("surface", value.get("name", value.get("label", value.get("object_id", "")))))
            start = value.get("span_start", value.get("start", 0))
            end = value.get("span_end", value.get("end", 0))
            object_id_value = value.get("object_id", value.get("entity_id", value.get("id")))
        else:
            surface = value
            start, end = 0, 0
            object_id_value = None
        surface = _normal_text(surface)
        if surface:
            try:
                start_int, end_int = int(start), int(end)
            except (TypeError, ValueError):
                start_int, end_int = 0, 0
            object_id = _normal_text(object_id_value) if object_id_value is not None else None
            found.append(_ObjectMention(surface, start_int, end_int, True, "message.object", object_id or None))
    occupied: List[Tuple[int, int]] = []
    for pattern, source in ((_OBJECT_COMPOUND_RE, "text.object_compound"), (_OBJECT_MARKED_RE, "text.object_marked"), (_OBJECT_KNOWN_RE, "text.object_name")):
        for match in pattern.finditer(clause):
            start, end = match.span()
            if any(start < right and end > left for left, right in occupied):
                continue
            occupied.append((start, end))
            found.append(_ObjectMention(match.group(0), clause_start + start, clause_start + end, True, source))
    for match in _OBJECT_PRONOUN_RE.finditer(clause):
        start, end = match.span()
        if any(start < right and end > left for left, right in occupied):
            continue
        found.append(_ObjectMention(match.group(0), clause_start + start, clause_start + end, False, "text.object_pronoun"))
    unique: List[_ObjectMention] = []
    seen: set[Tuple[str, int, int]] = set()
    for item in sorted(found, key=lambda value: (value.start, -(value.end - value.start), value.surface)):
        key = (_compact(item.surface), item.start, item.end, item.object_id or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


_STATE_RULES: Tuple[Tuple[str, str], ...] = (
    ("将要", "planned"),
    ("计划", "planned"),
    ("准备", "planned"),
    ("打算", "planned"),
    ("进行中", "active"),
    ("处理中", "active"),
    ("运行中", "active"),
    ("卡住", "blocked"),
    ("待确认", "pending"),
    ("未解决", "pending"),
    ("没解决", "pending"),
    ("尚未", "pending"),
    ("反复", "recurring"),
    ("重复", "recurring"),
    ("经常", "recurring"),
    ("仍然", "ongoing"),
    ("仍在", "ongoing"),
    ("还在", "ongoing"),
    ("等待", "pending"),
    ("等着", "pending"),
    ("不可用", "unavailable"),
    ("无法使用", "unavailable"),
    ("报错", "failed"),
    ("失败", "failed"),
    ("故障", "failed"),
    ("异常", "failed"),
    ("错误", "failed"),
    ("中断", "interrupted"),
    ("超时", "failed"),
    ("挂了", "failed"),
    ("挂掉", "failed"),
    ("坏了", "failed"),
    ("不够", "insufficient"),
    ("不足", "insufficient"),
    ("用完", "depleted"),
    ("耗尽", "depleted"),
    ("没额度", "depleted"),
    ("够了", "sufficient"),
    ("够用", "sufficient"),
    ("充足", "sufficient"),
    ("恢复", "resolved"),
    ("好了", "resolved"),
    ("正常了", "resolved"),
    ("正常", "active"),
    ("解决了", "resolved"),
    ("解决", "resolved"),
    ("取消", "cancelled"),
    ("canceled", "cancelled"),
    ("cancelled", "cancelled"),
    ("可用", "available"),
    ("能用了", "available"),
    ("成功", "succeeded"),
    ("完成", "completed"),
    ("waiting", "pending"),
    ("pending", "pending"),
    ("ongoing", "ongoing"),
    ("blocked", "blocked"),
    ("failed", "failed"),
    ("failure", "failed"),
    ("broken", "failed"),
    ("unavailable", "unavailable"),
    ("resolved", "resolved"),
    ("recovered", "resolved"),
    ("available", "available"),
    ("working", "active"),
    ("enough", "sufficient"),
    ("insufficient", "insufficient"),
    ("planned", "planned"),
    ("ready", "resolved"),
    ("高", "high"),
    ("低", "low"),
    ("贵", "expensive"),
    ("便宜", "cheap"),
)
_STATE_PATTERN = re.compile("|".join(re.escape(surface) for surface, _ in _STATE_RULES), flags=re.IGNORECASE)
_STATE_LOOKUP = {surface.casefold(): state for surface, state in _STATE_RULES}
_ACTION_RULES: Tuple[Tuple[str, str], ...] = (
    ("重置", "reset"),
    ("注册", "register"),
    ("登录", "login"),
    ("登陆", "login"),
    ("调用", "call"),
    ("部署", "deploy"),
    ("申请", "apply"),
    ("购买", "buy"),
    ("比较", "compare"),
    ("检查", "check"),
    ("查看", "inspect"),
    ("处理", "handle"),
    ("修复", "fix"),
    ("解决", "resolve"),
    ("等待", "wait"),
    ("使用", "use"),
    ("消耗", "consume"),
    ("扣", "deduct"),
    ("选课", "enroll"),
    ("报名", "register"),
    ("发送", "send"),
    ("回复", "reply"),
)
_ACTION_PATTERN = re.compile("|".join(re.escape(surface) for surface, _ in _ACTION_RULES), flags=re.IGNORECASE)
_ACTION_LOOKUP = {surface.casefold(): action for surface, action in _ACTION_RULES}
_NEGATION_RE = re.compile(r"(?:不|没|没有|未|尚未|无法|无|别|不是|并非)")
_HISTORICAL_RE = re.compile(r"历史上|过去|以前|曾经|historically|previously|in the past", flags=re.IGNORECASE)
_TOPIC_SHIFT_RE = re.compile(r"换个话题|另一个话题|另外|顺便|对了|新话题|除此之外|换句话说|by the way|new topic|anyway", flags=re.IGNORECASE)
_CONTRAST_RE = re.compile(r"但是|但|不过|然而|可是|相反|同时|but|however|although", flags=re.IGNORECASE)
_QUESTION_RE = re.compile(r"\?|？|吗[？?！!。.]?$|(?:怎么|如何|为什么|为何|是否|能否|可否|能不能|可以吗|有没有|请问)")
_HEDGE_RE = re.compile(r"可能|也许|似乎|好像|应该|大概|感觉|觉得|看起来|probably|maybe|seems?", flags=re.IGNORECASE)
_REPORT_RE = re.compile(r"听说|据说|有人说|消息称|据反馈|reported|they say", flags=re.IGNORECASE)
_HYPOTHETICAL_RE = re.compile(r"如果|假如|要是|若是|万一|if\b|suppose", flags=re.IGNORECASE)
_SUGGESTION_RE = re.compile(r"建议|需要|必须|请|最好|应该|帮我|麻烦|recommend|need|must|please", flags=re.IGNORECASE)


def _state_mentions(clause: str) -> Tuple[_StateMention, ...]:
    found: List[_StateMention] = []
    for match in _STATE_PATTERN.finditer(clause):
        surface = match.group(0)
        found.append(_StateMention(_STATE_LOOKUP[surface.casefold()], surface, match.start(), match.end()))
    # Prefer the longer/state-specific span when one rule is nested in
    # another (for example ``正常了`` contains the useful status as a whole).
    output: List[_StateMention] = []
    for item in sorted(found, key=lambda value: (value.start, -(value.end - value.start))):
        if any(item.start < current.end and item.end > current.start for current in output):
            continue
        output.append(item)
    return tuple(sorted(output, key=lambda value: (value.start, value.end)))


def _state_projection(detail: str) -> str:
    """Map lexical/input status detail onto Stage1's frozen six states."""

    if detail in {"resolved", "available", "succeeded", "completed", "sufficient"}:
        return STATE_RESOLVED
    if detail in {"failed"}:
        return STATE_FAILED
    if detail in {
        "pending",
        "ongoing",
        "blocked",
        "unavailable",
        "insufficient",
        "depleted",
        "interrupted",
        "active",
        "recurring",
    }:
        return STATE_ONGOING
    if detail in {"planned", "reported"}:
        return STATE_PLANNED
    if detail in {"cancelled", "canceled"}:
        return STATE_CANCELLED
    if detail in {"high", "low", "expensive", "cheap"}:
        return STATE_UNKNOWN
    return STATE_UNKNOWN


def _fragment_state(assertions: Sequence[StateAssertion]) -> str:
    """Choose the most informative state when a clause has several cues."""

    priority = {
        STATE_FAILED: 0,
        STATE_RESOLVED: 1,
        STATE_RECURRING: 2,
        STATE_ONGOING: 3,
        STATE_REPORTED: 4,
        STATE_UNKNOWN: 5,
    }
    return min((item.state for item in assertions), key=lambda value: priority.get(value, 99), default=STATE_UNKNOWN)


def _claim_role(clause: str, intent: str) -> str:
    if intent == "question":
        return "question"
    if _HYPOTHETICAL_RE.search(clause):
        return "hypothesis"
    if intent == "request" or _SUGGESTION_RE.search(clause):
        return "suggestion"
    if re.search(r"觉得|认为|看来|感觉|think|believe|opinion", clause, flags=re.IGNORECASE):
        return "opinion"
    return "fact"


def _actions(clause: str) -> Tuple[str, ...]:
    values: List[str] = []
    for match in _ACTION_PATTERN.finditer(clause):
        action = _ACTION_LOOKUP.get(match.group(0).casefold())
        if action and action not in values:
            values.append(action)
    return tuple(values)


def _modality(clause: str, intent: str) -> str:
    if intent == "question" or _QUESTION_RE.search(clause):
        return MODALITY_QUESTION
    if _HYPOTHETICAL_RE.search(clause):
        return MODALITY_HYPOTHETICAL
    if _REPORT_RE.search(clause):
        return MODALITY_REPORTED
    if _SUGGESTION_RE.search(clause):
        return MODALITY_SUGGESTION
    if _HEDGE_RE.search(clause):
        return MODALITY_HEDGED
    return MODALITY_ASSERTED


def _intent(clause: str, role: str, silent: bool, greeting: bool) -> str:
    if silent:
        return "silence"
    if greeting:
        return "greeting"
    if _QUESTION_RE.search(clause):
        return "question"
    if _SUGGESTION_RE.search(clause) and not _HEDGE_RE.search(clause):
        return "request"
    if role == ROLE_CONTEXT_ONLY or is_greeting_or_social(clause):
        return "acknowledgement"
    return "statement"


def _subject_mention(
    clause: str,
    mentions: Sequence[_Mention],
    speaker_id: str,
    speaker_label: str,
    speaker_source: str,
    prior_subject: Optional[ActorRef],
    message_id: str,
    fragment_id: str,
    clause_start: int,
) -> Tuple[_Mention, str, str]:
    """Select an explicit/inherited/unknown subject mention."""

    stripped = clause.lstrip()
    leading_offset = len(clause) - len(stripped)
    # Clause splitting keeps a contrast/discourse marker (for example ``但``)
    # at the beginning of the right-hand clause.  It is not part of the
    # grammatical subject, so remove it for head selection while retaining the
    # original span in the returned mention.
    for prefix in _DISCOURSE_PREFIXES:
        if stripped.casefold().startswith(prefix.casefold()):
            stripped = stripped[len(prefix) :].lstrip()
            leading_offset = len(clause) - len(stripped)
            break
    # Explicit first person is an attributable subject, not a separate
    # mentioned person.  Its speaker identity is explicit from the message.
    first_persons = tuple(sorted(_FIRST_PERSON, key=len, reverse=True))
    stripped_folded = stripped.casefold()
    if any(stripped_folded.startswith(value.casefold()) for value in first_persons):
        surface = next(value for value in first_persons if stripped_folded.startswith(value.casefold()))
        return _Mention(surface, clause_start + leading_offset, clause_start + leading_offset + len(surface), True, "text.first_person"), speaker_id, RESOLUTION_EXPLICIT
    for item in mentions:
        if item.start < clause_start or item.end > clause_start + len(clause):
            continue
        relative = item.start - clause_start
        if relative > leading_offset + 1:
            continue
        if item.surface.casefold() in _SECOND_PERSON:
            return item, "ACTOR_UNKNOWN", RESOLUTION_UNKNOWN
        if item.surface.casefold() in _PERSON_PRONOUNS:
            if prior_subject is not None and not prior_subject.actor_id.startswith("ACTOR_UNKNOWN"):
                return item, prior_subject.actor_id, RESOLUTION_INHERITED
            return item, "ACTOR_UNKNOWN", RESOLUTION_UNKNOWN
        return item, "ACTOR:" + _slug(item.surface.lstrip("@")), RESOLUTION_EXPLICIT
    # A reporting/name pattern can occur just after a discourse prefix.
    for item in mentions:
        if item.surface.casefold() in _PERSON_PRONOUNS or item.surface.casefold() in _FIRST_PERSON or item.surface.casefold() in _SECOND_PERSON:
            continue
        if re.search(r"(?:据说|听说|有人说|关于|针对|至于)\s*%s" % re.escape(item.surface), clause):
            return item, "ACTOR:" + _slug(item.surface.lstrip("@")), RESOLUTION_EXPLICIT
    # No grammatical head: retain an unknown subject even though speaker is
    # known.  This is the key no-head invariant.
    return _Mention("unknown", clause_start, clause_start, False, "subject.missing"), "ACTOR_UNKNOWN", RESOLUTION_UNKNOWN


def _actor_ref(
    actor_id: str,
    surface: str,
    role: str,
    resolution: str,
    message_id: str,
    fragment_id: str,
    start: Optional[int],
    end: Optional[int],
    source: str,
) -> ActorRef:
    if actor_id == "ACTOR_UNKNOWN":
        actor_id = "ACTOR_UNKNOWN:%s" % fragment_id
    ref_id = _stable_id("ACTOR_REF", fragment_id, role, actor_id, start, end, surface)
    confidence = 0.95 if resolution == RESOLUTION_EXPLICIT else 0.72 if resolution == RESOLUTION_INHERITED else 0.0
    return ActorRef(ref_id, actor_id, surface or "unknown", role, resolution, message_id, fragment_id, start, end, source, confidence)


def _object_ref(
    surface: str,
    resolution: str,
    object_id: str,
    message_id: str,
    fragment_id: str,
    start: int,
    end: int,
    source: str,
    inherited_from_id: Optional[str] = None,
) -> ObjectRef:
    if object_id == "OBJECT_UNKNOWN":
        object_id = "OBJECT_UNKNOWN:%s" % fragment_id
    ref_id = _stable_id("OBJECT_REF", fragment_id, object_id, resolution, start, end, surface)
    confidence = 0.95 if resolution == RESOLUTION_EXPLICIT else 0.72 if resolution == RESOLUTION_INHERITED else 0.0
    return ObjectRef(
        ref_id,
        object_id,
        surface or "unknown",
        resolution,
        message_id,
        fragment_id,
        start,
        end,
        source,
        confidence,
        inherited_from_id,
    )


def _polarity(clause: str, state_start: int) -> str:
    # Inspect a short window before the state only; a later ``不`` must not
    # negate an earlier assertion by accident.
    window = clause[max(0, state_start - 5) : state_start]
    return "negative" if _NEGATION_RE.search(window) else "positive"


def _make_fragment(
    entry: _MessageEntry,
    clause_start: int,
    clause_end: int,
    clause: str,
    prior_object: Optional[ObjectRef],
    prior_subject: Optional[ActorRef],
) -> Fragment:
    message = entry.message
    fragment_id = _stable_id("FRAGMENT", entry.message_id, clause_start, clause_end, clause)
    text = clause
    message_type = _field(message, "message_type", "text")
    silent = _is_silent(text, message_type)
    greeting = is_greeting_or_social(text)
    # A mixed greeting remains substantive; only greeting-only text is social.
    role = entry.role
    if greeting and role == ROLE_SUBSTANTIVE:
        role = ROLE_CONVERSATION_OPENER if clause_start == 0 else ROLE_CONTEXT_ONLY
    if role == ROLE_CONVERSATION_OPENER and not greeting and not silent:
        role = ROLE_SUBSTANTIVE
    intent = _intent(text, role, silent, greeting)
    modality = _modality(text, intent)
    speaker_id, speaker_label, speaker_source = _speaker_identity(message)
    speaker = _actor_ref(
        speaker_id,
        speaker_label,
        "speaker",
        RESOLUTION_EXPLICIT if speaker_id != "ACTOR_UNKNOWN" else RESOLUTION_UNKNOWN,
        entry.message_id,
        fragment_id,
        None,
        None,
        speaker_source,
    )
    mentions = _person_mentions(text, clause_start, message)
    grouped_people = _MULTI_PERSON_RE.search(text)
    if grouped_people is not None:
        group_start, group_end = grouped_people.span()
        group_surface = grouped_people.group(0)
        subject_mention = _Mention(
            group_surface,
            clause_start + group_start,
            clause_start + group_end,
            True,
            "text.person_group",
        )
        subject_id = "GROUP:" + _slug(
            grouped_people.group("left") + "-" + grouped_people.group("right")
        )
        subject_resolution = RESOLUTION_EXPLICIT
    else:
        subject_mention, subject_id, subject_resolution = _subject_mention(
            text,
            mentions,
            speaker.actor_id if speaker.resolution != RESOLUTION_UNKNOWN else "ACTOR_UNKNOWN",
            speaker_label,
            speaker_source,
            prior_subject,
            entry.message_id,
            fragment_id,
            clause_start,
        )
    subject_surface = subject_mention.surface
    if subject_id == speaker.actor_id and subject_surface in _FIRST_PERSON:
        subject_source = "text.first_person"
    elif subject_mention.source == "subject.missing":
        subject_source = "subject.missing"
    elif subject_mention.explicit:
        subject_source = subject_mention.source
    else:
        subject_source = "text.pronoun"
    subject = _actor_ref(
        subject_id,
        subject_surface,
        "subject",
        subject_resolution,
        entry.message_id,
        fragment_id,
        subject_mention.start,
        subject_mention.end,
        subject_source,
    )
    mentioned: List[ActorRef] = []
    for mention in mentions:
        if mention.surface == "unknown":
            continue
        if mention.surface.casefold() in _FIRST_PERSON:
            # First-person subject is already represented by ``subject`` and
            # never becomes a mentioned third party.
            continue
        if mention.surface == subject_surface and mention.start == subject_mention.start and mention.end == subject_mention.end:
            # Keep an explicit third-person subject in the mentioned table as
            # well: this is useful when distinguishing speaker from mentioned.
            if mention.surface in _PERSON_PRONOUNS and subject_resolution != RESOLUTION_UNKNOWN:
                continue
        actor_id = subject_id if mention.start == subject_mention.start and mention.end == subject_mention.end else (
            speaker.actor_id if mention.surface in _FIRST_PERSON and speaker.actor_id != "ACTOR_UNKNOWN" else (
                "ACTOR:" + _slug(mention.surface.lstrip("@")) if mention.surface not in _PERSON_PRONOUNS else (
                    prior_subject.actor_id if prior_subject is not None and not prior_subject.actor_id.startswith("ACTOR_UNKNOWN") else "ACTOR_UNKNOWN"
                )
            )
        )
        resolution = subject_resolution if mention.start == subject_mention.start and mention.end == subject_mention.end else (
            RESOLUTION_EXPLICIT if mention.explicit else RESOLUTION_INHERITED if actor_id != "ACTOR_UNKNOWN" else RESOLUTION_UNKNOWN
        )
        mentioned.append(
            _actor_ref(
                actor_id,
                mention.surface,
                "mentioned",
                resolution,
                entry.message_id,
                fragment_id,
                mention.start,
                mention.end,
                mention.source,
            )
        )
    # Do not duplicate an explicit subject occurrence in actor_refs; it remains
    # available as ``subject`` and as a mentioned person if it is third-party.
    actors: List[ActorRef] = [speaker, subject]
    actors.extend(mentioned)
    unique_actors: List[ActorRef] = []
    seen_actor_refs: set[str] = set()
    for actor in actors:
        if actor.ref_id in seen_actor_refs:
            continue
        seen_actor_refs.add(actor.ref_id)
        unique_actors.append(actor)

    object_mentions = _object_mentions(text, clause_start, message)
    explicit_objects = [item for item in object_mentions if item.explicit]
    pronoun_objects = [item for item in object_mentions if not item.explicit]
    topic_shift = bool(_TOPIC_SHIFT_RE.search(text))
    actions = _actions(text)
    state_mentions = _state_mentions(text)
    if intent == "question":
        # In ``怎么恢复/如何解决`` the status word is an action being asked
        # about, not evidence that recovery/resolution already happened.  A
        # status question such as ``API恢复了吗`` still keeps its state cue.
        state_mentions = tuple(
            item
            for item in state_mentions
            if not re.search(
                r"(?:怎么|如何|怎样|能否|可否|whether|how)\s*$",
                text[: item.start],
                flags=re.IGNORECASE,
            )
        )
    inherit_allowed = bool(pronoun_objects or _OBJECT_PRONOUN_RE.search(text) or state_mentions or actions or intent == "question") and not topic_shift and not greeting and not silent
    object_refs: List[ObjectRef] = []
    for item in explicit_objects:
        object_refs.append(
            _object_ref(
                item.surface,
                RESOLUTION_EXPLICIT,
                item.object_id or "OBJECT:" + _slug(item.surface),
                entry.message_id,
                fragment_id,
                item.start,
                item.end,
                item.source,
            )
        )
    if not object_refs and inherit_allowed and prior_object is not None and not prior_object.is_unknown:
        item = pronoun_objects[0] if pronoun_objects else _ObjectMention("(省略)", clause_start, clause_start, False, "context.ellipsis")
        object_refs.append(
            _object_ref(
                item.surface,
                RESOLUTION_INHERITED,
                prior_object.object_id,
                entry.message_id,
                fragment_id,
                item.start,
                item.end,
                item.source,
                prior_object.ref_id,
            )
        )
    if not object_refs:
        # Unknown tail is explicit in the structure, even for an opener or a
        # silent turn.  It is never used to link unrelated fragments.
        object_refs.append(
            _object_ref(
                "unknown",
                RESOLUTION_UNKNOWN,
                "OBJECT_UNKNOWN",
                entry.message_id,
                fragment_id,
                clause_start,
                clause_start,
                "object.missing",
            )
        )

    assertions: List[StateAssertion] = []
    for item in state_mentions:
        assertion_id = _stable_id("STATE", fragment_id, item.state, item.start, item.end)
        projected_state = _state_projection(item.state)
        assertions.append(
            StateAssertion(
                assertion_id=assertion_id,
                state=projected_state,
                state_detail=item.surface,
                modality=modality,
                polarity=_polarity(text, item.start),
                actor_ref_id=subject.ref_id,
                object_ref_id=object_refs[0].ref_id,
                evidence_text=item.surface,
                message_id=entry.message_id,
                fragment_id=fragment_id,
                span_start=clause_start + item.start,
                span_end=clause_start + item.end,
                source="text.state_rule",
                confidence=0.9,
            )
        )
    state_change = len(assertions) > 1 or bool(re.search(r"从.+到|后来|现在|又|恢复|变成|turned into|now", text, flags=re.IGNORECASE))
    if silent:
        fragment_type = "media" if str(message_type or "text").casefold() not in {"text", "link"} else "unknown"
    elif role == ROLE_CONVERSATION_OPENER and greeting:
        fragment_type = "conversation_opener"
    elif intent == "question":
        fragment_type = "question"
    elif intent == "request":
        fragment_type = "request"
    elif intent == "acknowledgement" or role == ROLE_CONTEXT_ONLY:
        fragment_type = "acknowledgement"
    else:
        fragment_type = "statement"
    projected_state = _fragment_state(assertions)
    object_ref = object_refs[0]
    speaker_source_id = _field(message, "speaker_id", None) or _field(message, "sender_id", None) or _field(message, "sender_name", None)
    speaker_source_id = str(speaker_source_id) if speaker_source_id is not None and str(speaker_source_id) else "unknown"
    mentioned_ids = tuple(
        "unknown" if item.is_unknown else item.actor_id
        for item in mentioned
    )
    object_evidence_refs: Tuple[Dict[str, Any], ...]
    if object_ref.resolution == RESOLUTION_UNKNOWN:
        object_evidence_refs = ()
    elif object_ref.resolution == RESOLUTION_INHERITED:
        object_evidence_refs = (
            {"type": "fragment", "id": object_ref.inherited_from_id or "unknown", "span": None},
        )
    else:
        object_evidence_refs = (
            {
                "type": "mention",
                "id": object_ref.ref_id,
                "span": {"start": object_ref.span_start, "end": object_ref.span_end},
            },
        )
    context_ids = (prior_object.message_id,) if object_ref.resolution == RESOLUTION_INHERITED and prior_object is not None else ()
    if silent or greeting or role == ROLE_CONTEXT_ONLY:
        information_value = "none" if silent else "low"
        event_completeness = "not_applicable"
    elif object_ref.is_unknown and subject.resolution == RESOLUTION_UNKNOWN and not assertions:
        information_value = "unknown"
        event_completeness = "unknown"
    elif object_ref.is_unknown or subject.resolution == RESOLUTION_UNKNOWN:
        information_value = "medium"
        event_completeness = "partial"
    elif assertions or actions:
        information_value = "high"
        event_completeness = "sufficient" if object_ref.resolution == RESOLUTION_EXPLICIT else "partial"
    else:
        information_value = "medium"
        event_completeness = "partial"
    return Fragment(
        fragment_id=fragment_id,
        message_id=entry.message_id,
        segment_id=entry.segment_id,
        text=text,
        span_start=clause_start,
        span_end=clause_end,
        role=role,
        fragment_type=fragment_type,
        speaker=speaker,
        subject=subject,
        mentioned_persons=tuple(mentioned),
        actor_refs=tuple(unique_actors),
        objects=tuple(object_refs),
        state_assertions=tuple(assertions),
        intent=intent,
        claim_role=_claim_role(text, intent),
        modality=modality,
        speech_modality=modality,
        actions=actions,
        topic_shift=topic_shift,
        contrast_marker=bool(_CONTRAST_RE.search(text)),
        state_change=state_change,
        is_silent=silent,
        is_opener=role == ROLE_CONVERSATION_OPENER,
        timestamp=entry.timestamp,
        time_offset_seconds=entry.time_offset_seconds,
        reply_to_message_id=entry.reply_to_message_id,
        evidence_text="" if silent else text,
        speaker_id=speaker_source_id,
        mentioned_person_ids=mentioned_ids,
        subject_id="unknown" if subject.is_unknown else subject.actor_id,
        subject_type="group" if subject.actor_id.startswith("GROUP:") else "person" if not subject.is_unknown else "unknown",
        object_id="unknown" if object_ref.is_unknown else object_ref.object_id,
        object_resolution=object_ref.resolution,
        object_inherited_from_id=object_ref.inherited_from_id,
        object_evidence_refs=object_evidence_refs,
        state=projected_state,
        state_evidence=RESOLUTION_EXPLICIT if assertions else RESOLUTION_UNKNOWN,
        closure_reason=projected_state if projected_state in {STATE_RESOLVED, STATE_FAILED, STATE_CANCELLED} else "unknown",
        temporal_qualifier="historical" if _HISTORICAL_RE.search(text) else "unknown",
        start_time=None,
        end_time=None,
        start_time_source=RESOLUTION_UNKNOWN,
        end_time_source=RESOLUTION_UNKNOWN,
        information_value=information_value,
        event_completeness=event_completeness,
        topic_boundary="shift" if topic_shift else "none",
        context_message_ids=context_ids,
        uncertainties=tuple(
            reason
            for reason, condition in (
                ("subject_unknown", subject.is_unknown),
                ("object_unknown", object_ref.is_unknown),
                ("state_unknown", not assertions),
                ("silent_turn", silent),
            )
            if condition
        ),
        claim_ids=(),
        evidence_refs=(
            {
                "type": "message",
                "id": entry.message_id,
                "span": {"start": clause_start, "end": clause_end},
            },
        ) if not silent else (),
    )


def _fragment_object_ids(fragment: Fragment) -> Tuple[str, ...]:
    return tuple(item.object_id for item in fragment.objects if not item.is_unknown)


def _fragment_primary_state(fragment: Fragment) -> Optional[str]:
    return fragment.state_assertions[0].state if fragment.state_assertions else None


def _time_distance(left: Fragment, right: Fragment) -> Optional[float]:
    left_time = left.time_offset_seconds
    right_time = right.time_offset_seconds
    if left_time is None or right_time is None:
        # Use ISO timestamps only as a fallback; invalid values stay absent.
        try:
            left_time = datetime.fromisoformat(str(left.timestamp).replace("Z", "+00:00")).timestamp() if left.timestamp else None
            right_time = datetime.fromisoformat(str(right.timestamp).replace("Z", "+00:00")).timestamp() if right.timestamp else None
        except (TypeError, ValueError, AttributeError):
            return None
    if left_time is None or right_time is None:
        return None
    return abs(float(right_time) - float(left_time))


def _relation_kind(left: Fragment, right: Fragment, same_segment: bool) -> Tuple[Optional[str], Tuple[str, ...], Optional[str], Optional[str], float, str]:
    left_objects = set(_fragment_object_ids(left))
    right_objects = set(_fragment_object_ids(right))
    shared_objects = tuple(sorted(left_objects & right_objects))
    left_state = _fragment_primary_state(left)
    right_state = _fragment_primary_state(right)
    shared_actions = tuple(sorted(set(left.actions) & set(right.actions)))
    subject_conflict = (
        not left.subject.is_unknown
        and not right.subject.is_unknown
        and left.subject.actor_id != right.subject.actor_id
    )
    signals: List[str] = []
    if same_segment:
        signals.append("same_dialogue_segment")
    if shared_objects:
        signals.append("shared_explicit_or_inherited_object")
    if shared_actions:
        signals.append("shared_action")
    if not left.subject.is_unknown and not right.subject.is_unknown and left.subject.actor_id == right.subject.actor_id:
        signals.append("same_subject")
    if right.reply_to_message_id == left.message_id:
        signals.append("explicit_reply")
    if right.topic_shift:
        signals.append("explicit_topic_shift")
    if right.contrast_marker:
        signals.append("contrast_marker")
    if left.intent == "question" and right.intent != "question":
        signals.append("question_then_turn")
    if right.objects and any(item.resolution == RESOLUTION_INHERITED for item in right.objects):
        signals.append("inherited_object")
    if left_state and right_state and left_state != right_state and shared_objects:
        signals.append("state_change")
    if left_state and right_state and left_state == right_state and shared_objects:
        signals.append("shared_state")

    # Priority keeps the graph useful without pretending that multiple signals
    # are one event identity.
    if right.topic_shift:
        return REL_TOPIC_SHIFT, tuple(signals), left_state, right_state, 0.96, "strong"
    explicit_reply = right.reply_to_message_id == left.message_id
    answer_turn = right.fragment_type == "answer" or right.intent == "answer"
    object_conflict = bool(
        left_objects
        and right_objects
        and not shared_objects
        and any(item.resolution == RESOLUTION_EXPLICIT for item in left.objects)
        and any(item.resolution == RESOLUTION_EXPLICIT for item in right.objects)
    )
    if explicit_reply and answer_turn and left.intent in {"question", "request"} and not object_conflict:
        return REL_QUESTION_ANSWER, tuple(signals), left_state, right_state, 0.94, "strong"
    # A contrast connective is a discourse cue, not a relation by itself.
    # Same-segment membership and clock proximity are deliberately excluded:
    # an unrelated question followed by ``但是`` must remain unlinked.  A
    # shared object is the safest baseline support; shared action plus a
    # known viewpoint/state is also admissible for a genuinely contrastive
    # pair without an explicit object.
    contrast_support = bool(
        shared_objects
        or (
            shared_actions
            and (
                subject_conflict
                or (left_state is not None and right_state is not None)
            )
        )
        or (
            left_state is not None
            and right_state is not None
            and not left.subject.is_unknown
            and not right.subject.is_unknown
            and not subject_conflict
        )
    )
    if (right.contrast_marker and contrast_support) or (
        shared_objects
        and left_state in {STATE_FAILED, STATE_CANCELLED, STATE_ONGOING}
        and right_state in {STATE_RESOLVED, STATE_PLANNED, STATE_ONGOING}
        and subject_conflict
    ):
        return REL_CONTRAST, tuple(signals), left_state, right_state, 0.82, "strong"
    if left_state and right_state and left_state != right_state and shared_objects:
        return REL_STATE_TRANSITION, tuple(signals), left_state, right_state, 0.86, "strong"
    if (
        left.intent in {"question", "request"}
        and answer_turn
        and not object_conflict
        and (
            "inherited_object" in signals
            or "shared_explicit_or_inherited_object" in signals
            or "shared_action" in signals
            or "shared_state" in signals
            or "state_change" in signals
            or "same_subject" in signals
        )
    ):
        return REL_QUESTION_ANSWER, tuple(signals), left_state, right_state, 0.84, "medium"
    if right.objects and any(item.resolution == RESOLUTION_INHERITED for item in right.objects) and (
        shared_objects or any(item.ref_id in {candidate.ref_id for candidate in left.objects} for item in right.objects)
    ):
        return REL_OBJECT_INHERITANCE, tuple(signals), left_state, right_state, 0.8, "medium"
    if explicit_reply and not object_conflict:
        return REL_REPLY, tuple(signals), left_state, right_state, 0.9, "strong"
    # A single shared object is recall evidence, not enough to call a strong
    # continuation.  Keep it as a weak candidate unless another semantic cue
    # (state/action/object inheritance) is present.
    if shared_objects and (shared_actions or left_state or right_state or right.objects and any(item.resolution == RESOLUTION_INHERITED for item in right.objects)):
        return REL_CONTINUATION, tuple(signals), left_state, right_state, 0.62, "medium"
    if shared_objects:
        return "possibly_related", tuple(signals), left_state, right_state, 0.35, "weak"
    if same_segment and left.is_opener and left.message_id == right.message_id:
        return REL_CONTINUATION, tuple(signals), left_state, right_state, 0.58, "medium"
    return None, tuple(signals), left_state, right_state, 0.0, "weak"


def build_context_relations(fragments: Sequence[Fragment], *, max_window: int = 3) -> Tuple[ContextRelation, ...]:
    """Build finite-window relation candidates without clustering/merging."""

    values = tuple(fragments or ())
    if max_window < 1:
        raise ValueError("max_window must be a positive integer")
    output: List[ContextRelation] = []
    for right_index, right in enumerate(values):
        for left_index in range(max(0, right_index - max_window), right_index):
            left = values[left_index]
            kind, signals, state_from, state_to, confidence, strength = _relation_kind(
                left,
                right,
                left.segment_id == right.segment_id,
            )
            if kind is None:
                continue
            distance = _time_distance(left, right)
            if distance is not None:
                signals = tuple(list(signals) + ["time_proximity_weak"])
                time_evidence = "weak"
            else:
                time_evidence = "none"
            relation_id = _stable_id("CONTEXT_RELATION", left.fragment_id, right.fragment_id, kind)
            relation_label = {
                REL_CONTINUATION: LABEL_CONTINUES,
                REL_OBJECT_INHERITANCE: LABEL_ELABORATES,
                REL_STATE_TRANSITION: LABEL_CONTINUES,
                REL_CONTRAST: LABEL_CONTRASTS,
                REL_TOPIC_SHIFT: LABEL_TOPIC_SHIFT,
                REL_QUESTION_ANSWER: LABEL_ANSWERS,
                REL_REPLY: LABEL_ANSWERS,
            }.get(kind, LABEL_POSSIBLY_RELATED)
            legacy_signal = kind if kind in {
                REL_CONTINUATION,
                REL_OBJECT_INHERITANCE,
                REL_STATE_TRANSITION,
                REL_CONTRAST,
                REL_QUESTION_ANSWER,
                REL_REPLY,
            } else None
            supporting_signals = tuple(
                dict.fromkeys(signals + ((legacy_signal,) if legacy_signal else ()))
            )
            left_object_ids = set(_fragment_object_ids(left))
            right_object_ids = set(_fragment_object_ids(right))
            conflicting_signals: List[str] = []
            if left_object_ids and right_object_ids and not left_object_ids.intersection(right_object_ids):
                conflicting_signals.append("object_conflict")
            if (
                not left.subject.is_unknown
                and not right.subject.is_unknown
                and left.subject.actor_id != right.subject.actor_id
            ):
                conflicting_signals.append("subject_conflict")
            output.append(
                ContextRelation(
                    relation_id=relation_id,
                    left_fragment_id=left.fragment_id,
                    right_fragment_id=right.fragment_id,
                    relation=relation_label,
                    relation_type=relation_label,
                    subtype=kind,
                    supporting_signals=supporting_signals,
                    conflicting_signals=tuple(conflicting_signals),
                    source_message_ids=(left.message_id, right.message_id),
                    evidence_refs=(
                        {"type": "fragment", "id": left.fragment_id, "span": {"start": left.span_start, "end": left.span_end}},
                        {"type": "fragment", "id": right.fragment_id, "span": {"start": right.span_start, "end": right.span_end}},
                    ),
                    provenance={
                        "stage": "context_relation_candidate",
                        "input_fragment_ids": (left.fragment_id, right.fragment_id),
                        "time_is_weak_only": True,
                    },
                    object_ids=tuple(sorted(set(_fragment_object_ids(left)) | set(_fragment_object_ids(right)))),
                    state_from=state_from,
                    state_to=state_to,
                    time_distance_seconds=distance,
                    time_evidence=time_evidence,
                    evidence_strength=strength,
                    confidence=confidence,
                    candidate=True,
                    requires_review=True,
                    uncertainties=("relation_candidate_not_event",) if confidence < 0.95 else (),
                    confidence_level="high" if confidence >= 0.85 else "medium" if confidence >= 0.6 else "low",
                )
            )
    return tuple(output)


def _mark_answers(fragments: Sequence[Fragment]) -> Tuple[Fragment, ...]:
    """Mark a local substantive response to a question/request as ``answer``."""

    def compatible(question: Fragment, answer: Fragment) -> bool:
        left_objects = set(_fragment_object_ids(question))
        right_objects = set(_fragment_object_ids(answer))
        shared_objects = bool(left_objects & right_objects)
        inherited_object = any(item.resolution == RESOLUTION_INHERITED for item in answer.objects)
        left_subject = question.subject.actor_id if not question.subject.is_unknown else None
        right_subject = answer.subject.actor_id if not answer.subject.is_unknown else None
        same_subject = bool(left_subject and left_subject == right_subject)
        shared_actions = bool(set(question.actions) & set(answer.actions))
        answer_has_state = bool(answer.state_assertions)
        question_has_state = bool(question.state_assertions)
        # A no-reply answer needs a semantic response cue in addition to
        # proximity/segment membership.  Shared object alone is recall
        # evidence and is intentionally insufficient (for example,
        # ``GPT怎么样？`` followed by ``GPT在线``).
        if shared_objects and (shared_actions or answer_has_state or question_has_state):
            return True
        if inherited_object and (shared_actions or answer_has_state):
            return True
        if shared_actions and (same_subject or shared_objects or inherited_object):
            return True
        if same_subject and (shared_actions or answer_has_state or question_has_state):
            return True
        # A direct reply can be a short answer without repeating the object;
        # explicit reply metadata is still not allowed to cross a clear topic
        # shift (checked by the caller).
        return answer.reply_to_message_id == question.message_id

    output: List[Fragment] = []
    for index, fragment in enumerate(fragments):
        if fragment.intent == "statement" and not fragment.is_silent and not fragment.topic_shift:
            # Look back over a short window so an opener, acknowledgement or
            # other context-only bridge does not hide a real question.  A
            # topic shift terminates the local search.
            for previous_index in range(index - 1, max(-1, index - 4), -1):
                previous = fragments[previous_index]
                if previous.topic_shift:
                    break
                if previous.role in {ROLE_CONVERSATION_OPENER, ROLE_CONTEXT_ONLY} or previous.is_silent:
                    continue
                if previous.intent not in {"question", "request"}:
                    break
                explicit = fragment.reply_to_message_id == previous.message_id
                if (explicit or compatible(previous, fragment)) and (
                    previous.segment_id == fragment.segment_id or explicit or compatible(previous, fragment)
                ):
                    fragment = replace(fragment, intent="answer", fragment_type="answer")
                break
        output.append(fragment)
    return tuple(output)


def _source_bucket(source: str) -> str:
    if source.startswith("message.speaker"):
        return "message_metadata"
    if source.startswith("text."):
        return "text"
    if source.startswith("reply"):
        return "reply_context"
    return "unknown"


def _actor_evidence_refs(actor: ActorRef) -> Tuple[Dict[str, Any], ...]:
    if actor.is_unknown or not actor.fragment_id:
        return ()
    return (
        {
            "type": "fragment",
            "id": actor.fragment_id,
            "span": {"start": actor.span_start, "end": actor.span_end},
        },
    )


def _person_projection(actor: ActorRef, claim_id: Optional[str] = None) -> PersonV2:
    score = actor.confidence
    return PersonV2(
        person_ref_id=actor.ref_id,
        person_id="unknown" if actor.is_unknown else actor.actor_id,
        resolution=actor.resolution,
        role="mentioned_person" if actor.role == "mentioned" else actor.role,
        message_id=actor.message_id,
        fragment_id=actor.fragment_id,
        claim_id=claim_id,
        span_start=actor.span_start,
        span_end=actor.span_end,
        surface_redacted=actor.surface_text,
        source=_source_bucket(actor.source),
        evidence_refs=_actor_evidence_refs(actor),
        confidence=_confidence_level(score),
        confidence_score=score,
    )


def _claim_for_fragment(fragment: Fragment, claim_id: str) -> ClaimV2:
    evidence_span = {"start": fragment.span_start, "end": fragment.span_end}
    evidence_spans = (evidence_span,) if not fragment.is_silent else ()
    object_ids = tuple(item.object_id for item in fragment.objects if not item.is_unknown)
    state = fragment.state if fragment.state in STATE_VALUES else STATE_UNKNOWN
    if state in {STATE_RESOLVED, STATE_FAILED, STATE_CANCELLED}:
        closure_reason = state
    else:
        closure_reason = "unknown"
    polarity = fragment.states[0].polarity if fragment.states else "unknown"
    stance = "mixed" if fragment.contrast_marker else "unknown"
    score_parts = [fragment.speaker.confidence, fragment.subject.confidence]
    score_parts.extend(item.confidence for item in fragment.objects)
    score = min(score_parts) if score_parts else 0.0
    return ClaimV2(
        claim_id=claim_id,
        message_id=fragment.message_id,
        fragment_id=fragment.fragment_id,
        speaker_id=fragment.speaker_id,
        mentioned_person_ids=fragment.mentioned_person_ids,
        subject_id=fragment.subject_id,
        subject_type=fragment.subject_type,
        object_id=fragment.object_id,
        object_resolution=fragment.object_resolution,
        object_evidence_refs=fragment.object_evidence_refs,
        object_inherited_from_id=fragment.object_inherited_from_id,
        claim_type=fragment.claim_role if fragment.claim_role in CLAIM_TYPES else "fact",
        claim_text_redacted=fragment.text,
        target_entity_ids=object_ids,
        event_mention_ids=(),
        evidence_spans=evidence_spans,
        stance=stance,
        polarity=polarity,
        modality=fragment.modality if fragment.modality in MODALITY_VALUES else MODALITY_UNKNOWN,
        status=state,
        state=state,
        state_evidence=fragment.state_evidence,
        closure_reason=closure_reason,
        temporal_qualifier=fragment.temporal_qualifier,
        start_time_offset_seconds=fragment.start_time,
        start_time_source=fragment.start_time_source,
        end_time_offset_seconds=fragment.end_time,
        end_time_source=fragment.end_time_source,
        information_value=fragment.information_value,
        event_completeness=fragment.event_completeness,
        attribution="reply" if fragment.reply_to_message_id else "direct",
        timestamp_message_id=fragment.message_id,
        timestamp=fragment.timestamp,
        reply_to_message_id=fragment.reply_to_message_id,
        context_message_ids=fragment.context_message_ids,
        evidence_span=evidence_span if not fragment.is_silent else None,
        evidence_refs=fragment.evidence_refs,
        confidence=_confidence_level(score),
        confidence_score=score,
    )


def _arguments_for_fragment(fragment: Fragment, claim_id: Optional[str]) -> Tuple[ArgumentV2, ...]:
    values: List[ArgumentV2] = []
    subject = fragment.subject
    subject_known = not subject.is_unknown
    subject_refs = (
        {
            "type": "fragment",
            "id": fragment.fragment_id,
            "span": {"start": subject.span_start, "end": subject.span_end},
        },
    ) if subject_known else ()
    values.append(
        ArgumentV2(
            argument_id=_stable_id("ARGUMENT", fragment.fragment_id, claim_id, "subject", subject.actor_id),
            fragment_id=fragment.fragment_id,
            claim_id=claim_id,
            role="subject",
            entity_id=fragment.subject_id if subject_known else "unknown",
            entity_type=fragment.subject_type if subject_known else "unknown",
            resolution=subject.resolution,
            evidence_refs=subject_refs,
            confidence=_confidence_level(subject.confidence),
            confidence_score=subject.confidence,
        )
    )
    for object_ref in fragment.objects:
        known = not object_ref.is_unknown
        values.append(
            ArgumentV2(
                argument_id=_stable_id("ARGUMENT", fragment.fragment_id, claim_id, "object", object_ref.ref_id),
                fragment_id=fragment.fragment_id,
                claim_id=claim_id,
                role="object",
                entity_id=object_ref.object_id if known else "unknown",
                entity_type="object" if known else "unknown",
                resolution=object_ref.resolution,
                evidence_refs=fragment.object_evidence_refs,
                inherited_from_id=object_ref.inherited_from_id,
                confidence=_confidence_level(object_ref.confidence),
                confidence_score=object_ref.confidence,
            )
        )
    return tuple(values)


def extract_context_fragments(
    messages: Iterable[Any],
    dialogue_segments: Any = None,
    *,
    segments: Any = None,
) -> ContextualFragmentsResult:
    """Extract context DTOs from explicit messages and optional segments.

    ``dialogue_segments`` may be a sequence of segment mappings/dataclasses or
    a public segmentation result exposing ``segments`` and ``message_roles``.
    ``segments=`` is a keyword alias for callers that use the shorter name.
    The input sequence and all nested objects are treated as immutable; no
    input mapping is modified.
    """

    if segments is not None:
        if dialogue_segments is not None:
            raise TypeError("pass either dialogue_segments or segments, not both")
        dialogue_segments = segments
    entries = _message_entries(messages, dialogue_segments)
    all_fragments: List[Fragment] = []
    all_actors: List[ActorRef] = []
    all_objects: List[ObjectRef] = []
    all_states: List[StateAssertion] = []
    prior_object_by_segment: Dict[str, ObjectRef] = {}
    prior_subject_by_segment: Dict[str, ActorRef] = {}
    for entry in entries:
        pieces = _split_clauses(_text_for(entry.message))
        for clause_start, clause_end, clause in pieces:
            fragment = _make_fragment(
                entry,
                clause_start,
                clause_end,
                clause,
                prior_object_by_segment.get(entry.segment_id),
                prior_subject_by_segment.get(entry.segment_id),
            )
            all_fragments.append(fragment)
            all_actors.extend(fragment.actor_refs)
            all_objects.extend(fragment.objects)
            all_states.extend(fragment.state_assertions)
            if fragment.subject.resolution != RESOLUTION_UNKNOWN:
                prior_subject_by_segment[entry.segment_id] = fragment.subject
            concrete_objects = [item for item in fragment.objects if not item.is_unknown]
            if len(concrete_objects) == 1:
                # Only a unique antecedent is safe for lexical ellipsis.  A
                # fragment mentioning two objects creates a deliberate
                # ambiguity; subsequent ``它/这个`` stays unknown until a new
                # explicit object appears.  Distinct explicit objects in
                # adjacent fragments are competing antecedents too.
                current = concrete_objects[0]
                previous = prior_object_by_segment.get(entry.segment_id)
                if previous is not None and previous.object_id != current.object_id:
                    prior_object_by_segment.pop(entry.segment_id, None)
                else:
                    prior_object_by_segment[entry.segment_id] = current
            elif len(concrete_objects) > 1:
                prior_object_by_segment.pop(entry.segment_id, None)
            if fragment.topic_shift:
                # Explicit new-topic text must not inherit the previous object.
                prior_object_by_segment.pop(entry.segment_id, None)

    # Preserve occurrence refs (they carry spans and provenance) but remove
    # accidental duplicate references generated by an unusual fixture.
    def unique_by_id(values: Iterable[Any]) -> Tuple[Any, ...]:
        result: List[Any] = []
        seen: set[str] = set()
        for value in values:
            value_id = str(getattr(value, "id", ""))
            if value_id in seen:
                continue
            seen.add(value_id)
            result.append(value)
        return tuple(result)

    fragments = _mark_answers(tuple(all_fragments))
    claims: List[ClaimV2] = []
    fragments_with_claims: List[Fragment] = []
    claim_id_by_fragment: Dict[str, str] = {}
    for fragment in fragments:
        claim_id: Optional[str] = None
        if (
            fragment.role == ROLE_SUBSTANTIVE
            and not fragment.is_silent
            and fragment.fragment_type in {"statement", "question", "request", "answer"}
        ):
            claim_id = _stable_id("CLAIM", fragment.fragment_id, fragment.text)
            claim_id_by_fragment[fragment.fragment_id] = claim_id
            claims.append(_claim_for_fragment(fragment, claim_id))
        if claim_id is not None:
            fragment = replace(fragment, claim_ids=(claim_id,))
        fragments_with_claims.append(fragment)
    fragments = tuple(fragments_with_claims)
    arguments = tuple(
        argument
        for fragment in fragments
        for argument in _arguments_for_fragment(fragment, claim_id_by_fragment.get(fragment.fragment_id))
    )
    persons = tuple(
        _person_projection(actor, claim_id_by_fragment.get(actor.fragment_id))
        for fragment in fragments
        for actor in fragment.actor_refs
    )
    relations = build_context_relations(fragments)
    return ContextualFragmentsResult(
        fragments=fragments,
        actor_refs=unique_by_id(all_actors),
        persons=unique_by_id(persons),
        object_refs=unique_by_id(all_objects),
        state_assertions=unique_by_id(all_states),
        arguments=arguments,
        claims=tuple(claims),
        relations=relations,
        context_relations=relations,
        message_ids=tuple(entry.message_id for entry in entries),
        segment_ids=tuple(dict.fromkeys(entry.segment_id for entry in entries)),
    )


def extract_fragments(
    messages: Iterable[Any],
    dialogue_segments: Any = None,
    *,
    segments: Any = None,
) -> ContextualFragmentsResult:
    """Short alias for :func:`extract_context_fragments`."""

    return extract_context_fragments(messages, dialogue_segments, segments=segments)


def extract_context_relations(
    messages_or_fragments: Iterable[Any],
    dialogue_segments: Any = None,
    *,
    segments: Any = None,
) -> Tuple[ContextRelation, ...]:
    """Return only relation candidates for a message/fragment input."""

    values = tuple(messages_or_fragments or ())
    if not values or isinstance(values[0], Fragment):
        return build_context_relations(values)  # type: ignore[arg-type]
    return extract_context_fragments(values, dialogue_segments, segments=segments).relations


def analyze_context(
    messages: Iterable[Any],
    dialogue_segments: Any = None,
    *,
    segments: Any = None,
) -> ContextualFragmentsResult:
    """Descriptive alias used by offline notebooks/tests."""

    return extract_context_fragments(messages, dialogue_segments, segments=segments)


# A few descriptive aliases keep the pure module easy to discover from an
# offline notebook without adding a second implementation.
extract_context = extract_context_fragments
extract_fragment_context = extract_context_fragments
build_fragments = extract_context_fragments


__all__ = [
    "ROLE_CONVERSATION_OPENER",
    "ROLE_CONTEXT_ONLY",
    "ROLE_SUBSTANTIVE",
    "RESOLUTION_EXPLICIT",
    "RESOLUTION_INHERITED",
    "RESOLUTION_UNKNOWN",
    "MODALITY_ASSERTED",
    "MODALITY_CERTAIN",
    "MODALITY_PROBABLE",
    "MODALITY_POSSIBLE",
    "MODALITY_REQUIRED",
    "MODALITY_DESIRED",
    "MODALITY_UNKNOWN",
    "MODALITY_QUESTION",
    "MODALITY_HEDGED",
    "MODALITY_REPORTED",
    "MODALITY_HYPOTHETICAL",
    "MODALITY_SUGGESTION",
    "MODALITY_VALUES",
    "ACTOR_ROLES",
    "OBJECT_RESOLUTIONS",
    "FRAGMENT_TYPES",
    "CLAIM_TYPES",
    "ARGUMENT_ROLES",
    "ENTITY_TYPES",
    "REL_CONTINUATION",
    "REL_OBJECT_INHERITANCE",
    "REL_STATE_TRANSITION",
    "REL_CONTRAST",
    "REL_TOPIC_SHIFT",
    "REL_QUESTION_ANSWER",
    "REL_REPLY",
    "REL_CONTINUES",
    "REL_ELABORATES",
    "REL_ANSWERS",
    "REL_CONTRASTS",
    "LABEL_CONTINUES",
    "LABEL_ELABORATES",
    "LABEL_ANSWERS",
    "LABEL_CONTRASTS",
    "LABEL_TOPIC_SHIFT",
    "LABEL_POSSIBLY_RELATED",
    "LABEL_INSUFFICIENT",
    "STATE_UNKNOWN",
    "STATE_PLANNED",
    "STATE_REPORTED",
    "STATE_ONGOING",
    "STATE_RESOLVED",
    "STATE_FAILED",
    "STATE_CANCELLED",
    "STATE_RECURRING",
    "STATE_VALUES",
    "RELATION_LABELS",
    "RELATION_STRENGTHS",
    "ActorRef",
    "PersonV2",
    "PersonRef",
    "ObjectRef",
    "StateAssertion",
    "ArgumentV2",
    "ClaimV2",
    "Fragment",
    "ContextRelation",
    "ContextualFragmentsResult",
    "is_greeting_or_social",
    "build_context_relations",
    "extract_context_fragments",
    "extract_fragments",
    "extract_context_relations",
    "analyze_context",
    "extract_context",
    "extract_fragment_context",
    "build_fragments",
]
