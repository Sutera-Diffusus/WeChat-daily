"""Independent Workstream E shadow semantic service.

The service composes the existing registry/gate/bundle pipeline with the
strict Workstream D thread/event projection.  It is deliberately below the
production event, title and frontend layers: the returned ``EventCandidate``
records remain shadow artifacts and are never written to a production table.

The model outcome is represented by one of four source markers:

``llm_accepted``
    Every selected bundle completed the fixed model contract.
``llm_pending``
    A model call failed, timed out or remained pending; threads are retained,
    but event candidates are not materialized.
``conservative_fallback``
    Model semantics were disabled or the conservative extractor was used.
``provider_blocked``
    A provider was required but unavailable/configuration-blocked.

The module has no file, database, network or frontend side effects.  Its
in-memory cache/store keep full DTOs only for the current process; their
serialized snapshots are body-free and contain hashes/provenance instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contextual_bundle_pipeline import (
    PIPELINE_VERSION as CONTEXTUAL_PIPELINE_VERSION,
    SUPPORTED_MODES,
    ContextualBundlePipeline,
    PipelineRunResult,
)
from .dialogue_bundle import BundleClaim, BundleFragment, DialogueBundleBuilder, DialogueBundleResult
from .discourse_event_candidates import (
    DiscourseEventResult,
    EventCandidate,
    EventDerivationConfig,
    DiscourseThread,
    KNOWN_RESOLUTIONS,
    KNOWN_STATES,
    ShadowSemanticStore,
    derive_discourse_and_events,
)
from .semantic_registry import CONTEXT_SCHEMA_VERSION, SCHEMA_VERSION, UNKNOWN, stable_hash


SHADOW_SCHEMA_VERSION = "semantic_shadow_run_v1"
SHADOW_PIPELINE_VERSION = "workstream_e_shadow_v1"
SHADOW_RULESET_VERSION = "workstream_e_shadow_rules_v1"
SOURCE_LLM_ACCEPTED = "llm_accepted"
SOURCE_LLM_PENDING = "llm_pending"
SOURCE_CONSERVATIVE_FALLBACK = "conservative_fallback"
SOURCE_PROVIDER_BLOCKED = "provider_blocked"
SOURCE_MARKERS = frozenset(
    {
        SOURCE_LLM_ACCEPTED,
        SOURCE_LLM_PENDING,
        SOURCE_CONSERVATIVE_FALLBACK,
        SOURCE_PROVIDER_BLOCKED,
    }
)
MODEL_STATUS_BY_SOURCE = {
    SOURCE_LLM_ACCEPTED: "accepted",
    SOURCE_LLM_PENDING: "pending",
    SOURCE_CONSERVATIVE_FALLBACK: "fallback",
    SOURCE_PROVIDER_BLOCKED: "unavailable",
}
PROVIDER_BLOCK_CODES = frozenset(
    {
        "provider_not_configured",
        "provider_not_injected",
        "provider_unavailable",
        "openai_sdk_unavailable",
        "provider_blocked",
    }
)
MODEL_PENDING_CODES = frozenset(
    {
        "model_pending",
        "model_failed",
        "model_timeout",
        "provider_timeout",
        "provider_error",
        "provider_network_error",
        "provider_protocol_error",
        "provider_rate_limited",
        "semantic_contract_error",
    }
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    return value


def _body_free(value: Any) -> Any:
    """Drop body-like fields from manifests, provenance and snapshots."""

    body_names = {
        "text",
        "content",
        "body",
        "raw",
        "raw_text",
        "raw_message",
        "message_text",
        "redacted_text",
        "surface",
        "surface_text",
        "surface_redacted",
        "fragment_text_redacted",
        "evidence_text",
        "claim_text_redacted",
        "quote",
        "summary",
        "narrative",
        "prompt",
        "response",
    }
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lower = name.casefold()
            if lower in body_names or lower.endswith(("_text", "_content", "_surface")):
                continue
            output[name] = _body_free(item)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_body_free(item) for item in value]
    return value


def _string(value: Any, default: str = UNKNOWN) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _safe_object(value: Any) -> Any:
    """Represent non-JSON config objects without credentials or body."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_object(item) for key, item in value.items() if str(key).casefold() not in {"api_key", "key", "token", "secret"}}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_object(item) for item in value]
    version = getattr(value, "model_version", None) or getattr(value, "version", None)
    return {
        "type": type(value).__module__ + "." + type(value).__qualname__,
        "version": _string(version),
    }


def _normalise_mode(value: Any) -> str:
    mode = _string(value, "disabled").casefold()
    if mode not in SUPPORTED_MODES:
        raise ValueError("mode must be one of %s" % ", ".join(sorted(SUPPORTED_MODES)))
    return mode


@dataclass(frozen=True)
class ShadowFeatureFlags:
    """Explicit switches for the independent shadow service."""

    shadow_enabled: bool = True
    bundle_semantics_enabled: bool = True
    discourse_enabled: bool = True
    event_materialization_enabled: bool = True
    cache_enabled: bool = True
    store_enabled: bool = True

    def to_dict(self) -> Dict[str, bool]:
        return {
            "shadow_enabled": bool(self.shadow_enabled),
            "bundle_semantics_enabled": bool(self.bundle_semantics_enabled),
            "discourse_enabled": bool(self.discourse_enabled),
            "event_materialization_enabled": bool(self.event_materialization_enabled),
            "cache_enabled": bool(self.cache_enabled),
            "store_enabled": bool(self.store_enabled),
        }


@dataclass(frozen=True)
class ShadowRunConfig:
    """Configuration for one replayable shadow semantic run.

    ``pipeline_kwargs`` is an escape hatch for bounded, public pipeline
    options (window/budget/cache settings).  Provider/model objects are passed
    to :func:`run_shadow_semantic` separately and are never serialized.
    """

    mode: str = "disabled"
    shadow_enabled: bool = True
    bundle_semantics_enabled: bool = True
    discourse_enabled: bool = True
    event_materialization_enabled: bool = True
    cache_enabled: bool = True
    store_enabled: bool = True
    analysis_run_id: str = "RUN_WORKSTREAM_E"
    split: str = "development"
    legacy_fallback_source: str = "legacy_fallback"
    pipeline_kwargs: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    feature_flags: Optional[ShadowFeatureFlags] = field(default=None, repr=False, compare=False)

    @property
    def resolved_mode(self) -> str:
        return _normalise_mode(self.mode)

    @property
    def resolved_features(self) -> ShadowFeatureFlags:
        if self.feature_flags is not None:
            return self.feature_flags
        return ShadowFeatureFlags(
            shadow_enabled=bool(self.shadow_enabled),
            bundle_semantics_enabled=bool(self.bundle_semantics_enabled),
            discourse_enabled=bool(self.discourse_enabled),
            event_materialization_enabled=bool(self.event_materialization_enabled),
            cache_enabled=bool(self.cache_enabled),
            store_enabled=bool(self.store_enabled),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.resolved_mode,
            "feature_flags": self.resolved_features.to_dict(),
            "analysis_run_id": _string(self.analysis_run_id, "RUN_WORKSTREAM_E"),
            "split": _string(self.split, "development"),
            "legacy_fallback_source": _string(self.legacy_fallback_source, "legacy_fallback"),
            "pipeline_kwargs": _safe_object(self.pipeline_kwargs),
        }


ShadowSemanticConfig = ShadowRunConfig


def _model_version(model: Any, config: ShadowRunConfig) -> str:
    if model is not None:
        return _string(getattr(model, "model_version", None) or getattr(model, "version", None))
    options = config.pipeline_kwargs if isinstance(config.pipeline_kwargs, Mapping) else {}
    provider_config = options.get("provider_config")
    if provider_config is not None:
        return _string(getattr(provider_config, "model", None))
    return UNKNOWN


def shadow_input_hash(
    messages: Iterable[Any],
    *,
    fragments: Optional[Iterable[Any]] = None,
    claims: Optional[Iterable[Any]] = None,
    config: Optional[ShadowRunConfig] = None,
    mode: Optional[str] = None,
    model: Any = None,
) -> str:
    """Compute an input/config fingerprint without exposing the input body."""

    cfg = config or ShadowRunConfig()
    selected_mode = _normalise_mode(mode if mode is not None else cfg.mode)
    values = {
        "messages": _jsonable(tuple(messages or ())),
        "fragments": _jsonable(tuple(fragments or ())),
        "claims": _jsonable(tuple(claims or ())),
        "config": {
            "mode": selected_mode,
            "features": cfg.resolved_features.to_dict(),
            "analysis_run_id": _string(cfg.analysis_run_id, "RUN_WORKSTREAM_E"),
            "split": _string(cfg.split, "development"),
            "pipeline_kwargs": _safe_object(cfg.pipeline_kwargs),
            "model_version": _model_version(model, cfg),
        },
    }
    return stable_hash(values)


def classify_source_marker(result: PipelineRunResult) -> str:
    """Map C's detailed outcomes to the four strict E source markers."""

    mode = _normalise_mode(result.mode)
    cost = result.cost if isinstance(result.cost, Mapping) else {}
    errors = result.errors if isinstance(result.errors, (list, tuple)) else ()
    error_codes = {
        _string(item.get("code"), "unknown").casefold()
        for item in errors
        if isinstance(item, Mapping)
    }
    if bool(cost.get("blocked")) or error_codes.intersection(PROVIDER_BLOCK_CODES):
        return SOURCE_PROVIDER_BLOCKED
    if mode == "disabled":
        return SOURCE_CONSERVATIVE_FALLBACK
    statuses = [
        _string(item.get("status"), "unknown").casefold()
        for item in (result.decisions or ())
        if isinstance(item, Mapping)
    ]
    if not statuses:
        return SOURCE_CONSERVATIVE_FALLBACK
    if any(status == "pending" for status in statuses):
        return SOURCE_LLM_PENDING
    if any(status == "fallback" for status in statuses):
        # A mixed complete/fallback run is not accepted: a replay must retain
        # all pending uncertainty instead of silently promoting a subset.
        return SOURCE_LLM_PENDING if any(status == "complete" for status in statuses) else SOURCE_CONSERVATIVE_FALLBACK
    if all(status == "complete" for status in statuses) and not error_codes.intersection(MODEL_PENDING_CODES):
        return SOURCE_LLM_ACCEPTED
    return SOURCE_LLM_PENDING


def _source_model_status(source_marker: str) -> str:
    return MODEL_STATUS_BY_SOURCE.get(source_marker, "unavailable")


def _provider_status_for(
    source_marker: str,
    *,
    mode: str,
    feature_flags: Mapping[str, Any],
) -> str:
    """Map the strict E marker to the small public API provider enum."""

    if source_marker == SOURCE_PROVIDER_BLOCKED:
        return "blocked"
    if source_marker == SOURCE_LLM_ACCEPTED:
        return "succeeded"
    if source_marker == SOURCE_LLM_PENDING:
        return "failed"
    if not bool(feature_flags.get("shadow_enabled", True)) or mode == "disabled":
        return "disabled"
    # A conservative result can be produced after a configured provider
    # declines to provide model semantics.  It is explicitly not accepted.
    return "configured"


def _fallback_reason_for(
    source_marker: str,
    *,
    mode: str,
    feature_flags: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]] = (),
) -> Optional[str]:
    if source_marker == SOURCE_LLM_ACCEPTED:
        return None
    if source_marker == SOURCE_PROVIDER_BLOCKED:
        return "provider_blocked"
    if source_marker == SOURCE_LLM_PENDING:
        for item in errors:
            code = _string(item.get("code")) if isinstance(item, Mapping) else UNKNOWN
            if code != UNKNOWN:
                return code
        return "model_pending"
    if not bool(feature_flags.get("shadow_enabled", True)):
        return "shadow_feature_disabled"
    if mode == "disabled":
        return "disabled_mode"
    return "conservative_fallback"


@dataclass(frozen=True)
class ShadowSemanticRunResult:
    """Body-free API projection plus private in-memory composition handles."""

    run_id: str
    analysis_run_id: str
    source_marker: str
    model_status: str
    mode: str
    input_sha256: str
    cache_key: str
    replay_key: str
    cache_hit: bool = False
    feature_flags: Mapping[str, Any] = field(default_factory=dict)
    discourse_result: DiscourseEventResult = field(default_factory=DiscourseEventResult, repr=False, compare=False)
    pipeline_result: Optional[PipelineRunResult] = field(default=None, repr=False, compare=False)
    manifest: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    errors: Tuple[Mapping[str, Any], ...] = ()

    @property
    def threads(self) -> Tuple[DiscourseThread, ...]:
        return self.discourse_result.threads

    @property
    def discourse_threads(self) -> Tuple[DiscourseThread, ...]:
        return self.threads

    @property
    def event_candidates(self) -> Tuple[EventCandidate, ...]:
        return self.discourse_result.event_candidates

    @property
    def events(self) -> Tuple[EventCandidate, ...]:
        # Read-only compatibility alias.  Serialized output deliberately uses
        # ``event_candidates`` so the shadow records cannot be mistaken for
        # production events.
        return self.event_candidates

    @property
    def provider_status(self) -> str:
        return _provider_status_for(
            self.source_marker,
            mode=self.mode,
            feature_flags=self.feature_flags,
        )

    @property
    def llm_accepted(self) -> bool:
        return self.source_marker == SOURCE_LLM_ACCEPTED and self.provider_status == "succeeded"

    @property
    def fallback_reason(self) -> Optional[str]:
        return _fallback_reason_for(
            self.source_marker,
            mode=self.mode,
            feature_flags=self.feature_flags,
            errors=self.errors,
        )

    def api_envelope(self) -> Dict[str, Any]:
        """Return the body-free service envelope used by a future shadow API."""

        return {
            "ok": True,
            "analysis_run_id": self.analysis_run_id,
            "run_id": self.run_id,
            "source": self.source_marker,
            "source_marker": self.source_marker,
            "provider_status": self.provider_status,
            "llm_accepted": self.llm_accepted,
            "fallback_reason": self.fallback_reason,
            "model_status": self.model_status,
            "mode": self.mode,
            "input_sha256": self.input_sha256,
            "cache_key": self.cache_key,
            "replay_key": self.replay_key,
            "feature_flags": _body_free(dict(self.feature_flags)),
        }

    to_api_dict = api_envelope

    def with_cache_hit(self) -> "ShadowSemanticRunResult":
        manifest = dict(self.manifest)
        manifest["cache_hit"] = True
        provenance = dict(self.provenance)
        provenance["cache_hit"] = True
        return replace(self, cache_hit=True, manifest=manifest, provenance=provenance)

    def to_dict(self) -> Dict[str, Any]:
        pipeline = self.pipeline_result.artifacts() if self.pipeline_result is not None else {}
        return {
            **self.api_envelope(),
            "cache_hit": bool(self.cache_hit),
            "manifest": _body_free(dict(self.manifest)),
            "provenance": _body_free(dict(self.provenance)),
            "pipeline": _body_free(pipeline),
            "threads": [thread.to_dict() for thread in self.threads],
            "discourse_threads": [thread.to_dict() for thread in self.threads],
            "event_candidates": [candidate.to_dict() for candidate in self.event_candidates],
            "errors": [_body_free(dict(item)) for item in self.errors],
        }


ShadowRunResult = ShadowSemanticRunResult


def _cache_key(
    input_sha256: str,
    config: ShadowRunConfig,
    model: Any,
    *,
    mode: Optional[str] = None,
) -> str:
    value = stable_hash(
        {
            "input_sha256": input_sha256,
            "pipeline_version": SHADOW_PIPELINE_VERSION,
            "ruleset_version": SHADOW_RULESET_VERSION,
            "mode": _normalise_mode(mode if mode is not None else config.resolved_mode),
            "model_version": _model_version(model, config),
        }
    )
    return "shadow-semantic:%s:%s" % (SHADOW_RULESET_VERSION, value)


def _build_pipeline(config: ShadowRunConfig, mode: str, model: Any) -> ContextualBundlePipeline:
    options = dict(config.pipeline_kwargs) if isinstance(config.pipeline_kwargs, Mapping) else {}
    for key in ("mode", "model", "fragments", "claims", "split"):
        options.pop(key, None)
    options["mode"] = mode
    if model is not None:
        options["model"] = model
    return ContextualBundlePipeline(**options)


def _dialogue_result_from_pipeline(
    pipeline_result: Optional[PipelineRunResult],
    messages: Sequence[Mapping[str, Any]],
    fragments: Sequence[Any],
    claims: Sequence[Any],
) -> DialogueBundleResult:
    if pipeline_result is not None:
        value = getattr(pipeline_result, "dialogue_result", None)
        if isinstance(value, DialogueBundleResult):
            return value
    # Compatibility path for a custom pipeline implementation that predates
    # the in-memory DTO handle.  It does not read any store or provider data.
    if messages:
        builder = DialogueBundleBuilder()
        return builder.ingest(messages, fragments=fragments, claims=claims)
    return DialogueBundleResult()


def _model_evidence_refs(
    semantic_bundle: Mapping[str, Any],
    message_id: str,
) -> Tuple[Dict[str, Any], ...]:
    """Convert accepted fixed-schema model evidence into D typed refs."""

    values = semantic_bundle.get("evidence") or ()
    if isinstance(values, Mapping):
        values = (values,)
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        evidence_id = _string(item.get("evidence_id") or item.get("id"))
        evidence_message_id = _string(item.get("message_id"))
        span = item.get("span")
        if evidence_id == UNKNOWN or evidence_message_id != message_id or not isinstance(span, Mapping):
            continue
        try:
            start, end = int(span.get("start")), int(span.get("end"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        key = "%s:%d:%d" % (evidence_id, start, end)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "type": "message",
                "id": evidence_message_id,
                "evidence_id": evidence_id,
                "span": {"start": start, "end": end},
                "field": _string(item.get("field")),
            }
        )
    return tuple(output)


def _model_slot_evidence(
    semantic_bundle: Mapping[str, Any],
    message_id: str,
    raw_slot: Any,
    *fields: str,
) -> Tuple[Dict[str, Any], ...]:
    """Select model refs for one slot, retaining typed evidence only."""

    all_refs = _model_evidence_refs(semantic_bundle, message_id)
    evidence_ids: set[str] = set()
    if isinstance(raw_slot, Mapping):
        values = raw_slot.get("evidence_ids") or raw_slot.get("evidence_refs") or ()
        if isinstance(values, (str, bytes)):
            values = (values,)
        for value in values if isinstance(values, Iterable) else ():
            if isinstance(value, Mapping):
                value = value.get("evidence_id") or value.get("id")
            if _string(value) != UNKNOWN:
                evidence_ids.add(_string(value))
    selected = tuple(
        ref
        for ref in all_refs
        if (not evidence_ids or ref.get("evidence_id") in evidence_ids)
        and (not fields or str(ref.get("field", "")).casefold() in {field.casefold() for field in fields})
    )
    return selected or (all_refs if not evidence_ids and not fields else ())


def _known_model_entity(
    semantic_bundle: Mapping[str, Any],
    message_id: str,
    raw_slot: Any,
    role: str,
) -> Tuple[str, str, Tuple[Dict[str, Any], ...]]:
    if isinstance(raw_slot, (list, tuple)):
        raw_slot = raw_slot[0] if len(raw_slot) == 1 else None
    if not isinstance(raw_slot, Mapping):
        return UNKNOWN, UNKNOWN, ()
    entity_id = _string(raw_slot.get("id") or raw_slot.get("entity_id") or raw_slot.get("person_id"))
    resolution = _string(raw_slot.get("resolution"), "explicit" if entity_id != UNKNOWN else UNKNOWN)
    if entity_id == UNKNOWN or resolution not in KNOWN_RESOLUTIONS - {UNKNOWN}:
        return UNKNOWN, UNKNOWN, ()
    refs = _model_slot_evidence(semantic_bundle, message_id, raw_slot, role)
    if not refs:
        return UNKNOWN, UNKNOWN, ()
    return entity_id, _string(raw_slot.get("type"), "person" if role == "subject" else UNKNOWN), refs


def _overlay_fragment_from_model(
    fragment: BundleFragment,
    semantic_bundle: Mapping[str, Any],
) -> BundleFragment:
    """Apply only evidence-backed accepted slots to one existing fragment."""

    message_id = fragment.message_id
    subject_raw = semantic_bundle.get("subject")
    subject_id, subject_type, subject_refs = _known_model_entity(
        semantic_bundle, message_id, subject_raw, "subject"
    )
    if subject_id == UNKNOWN:
        subject_id, subject_type = fragment.subject_id, fragment.subject_type
        subject_refs = ()

    object_raw_values = semantic_bundle.get("object") or semantic_bundle.get("objects") or ()
    if not isinstance(object_raw_values, (list, tuple)):
        object_raw_values = (object_raw_values,)
    known_objects: List[Tuple[str, str, Optional[str], Tuple[Dict[str, Any], ...]]] = []
    for raw_object in object_raw_values:
        if not isinstance(raw_object, Mapping):
            continue
        object_id = _string(raw_object.get("id") or raw_object.get("object_id"))
        resolution = _string(raw_object.get("resolution"), "explicit" if object_id != UNKNOWN else UNKNOWN)
        inherited_from = raw_object.get("source_id") or raw_object.get("object_inherited_from_id")
        inherited_from_id = _string(inherited_from) if inherited_from not in (None, "") else None
        if object_id == UNKNOWN or resolution not in KNOWN_RESOLUTIONS - {UNKNOWN}:
            continue
        if resolution == "inherited" and not inherited_from_id:
            continue
        refs = _model_slot_evidence(semantic_bundle, message_id, raw_object, "object")
        if refs:
            known_objects.append((object_id, resolution, inherited_from_id, refs))
    if len(known_objects) == 1:
        object_id, object_resolution, inherited_from_id, object_refs = known_objects[0]
    else:
        object_id = fragment.object_id
        object_resolution = fragment.object_resolution
        inherited_from_id = fragment.object_inherited_from_id
        object_refs = fragment.object_evidence_refs

    actions: List[str] = []
    raw_actions = semantic_bundle.get("action", semantic_bundle.get("actions")) or ()
    if not isinstance(raw_actions, (list, tuple)):
        raw_actions = (raw_actions,)
    for raw_action in raw_actions:
        if isinstance(raw_action, Mapping):
            label = _string(raw_action.get("label") or raw_action.get("action"))
        else:
            label = _string(raw_action)
            raw_action = {"label": label}
        if label == UNKNOWN:
            continue
        if _model_slot_evidence(semantic_bundle, message_id, raw_action, "action"):
            actions.append(label)
    if not actions:
        actions = list(fragment.actions)

    state = _string(semantic_bundle.get("state"))
    state_refs = _model_slot_evidence(semantic_bundle, message_id, {"evidence_ids": ()}, "state")
    if state not in KNOWN_STATES - {UNKNOWN} or not state_refs:
        state = fragment.state
        state_refs = ()
    modality = _string(semantic_bundle.get("modality"))
    if modality == UNKNOWN:
        modality = fragment.modality
    claim_role = _string(semantic_bundle.get("claim_type"))
    if claim_role == UNKNOWN:
        claim_role = fragment.claim_role
    model_refs = _model_evidence_refs(semantic_bundle, message_id)
    evidence_refs = model_refs or fragment.evidence_refs
    return replace(
        fragment,
        subject_id=subject_id,
        subject_type=subject_type,
        object_id=object_id,
        object_resolution=object_resolution,
        object_inherited_from_id=inherited_from_id,
        object_evidence_refs=object_refs,
        state=state,
        state_evidence="explicit" if state != UNKNOWN and state_refs else fragment.state_evidence,
        claim_role=claim_role,
        modality=modality,
        actions=tuple(dict.fromkeys(actions)),
        evidence_refs=evidence_refs,
        event_completeness="sufficient" if subject_id != UNKNOWN and object_id != UNKNOWN and actions and state != UNKNOWN and evidence_refs else fragment.event_completeness,
        source="workstream_e_llm_accepted",
    )


def _apply_accepted_model_semantics(
    dialogue: DialogueBundleResult,
    pipeline_result: PipelineRunResult,
) -> DialogueBundleResult:
    """Overlay accepted one-message bundle decisions onto A fragments.

    Bundle decisions can cover multiple messages, but the fixed schema has no
    per-message span attribution for an aggregate slot.  D therefore only
    applies a decision to a single-message bundle; broader decisions remain
    retained in the pipeline projection and cannot create an event by
    attribution guesswork.
    """

    decisions = tuple(pipeline_result.decisions or ())
    if not decisions or not dialogue.fragments:
        return dialogue
    by_message: Dict[str, List[BundleFragment]] = {}
    for fragment in dialogue.fragments:
        by_message.setdefault(fragment.message_id, []).append(fragment)
    updated = list(dialogue.fragments)
    updated_claims = list(dialogue.claims)
    consumed_messages: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, Mapping) or _string(decision.get("status")) != "complete":
            continue
        message_ids = tuple(str(item) for item in (decision.get("message_ids") or ()) if _string(item) != UNKNOWN)
        if len(message_ids) != 1 or message_ids[0] in consumed_messages:
            continue
        semantic_bundle = decision.get("semantic_bundle")
        if not isinstance(semantic_bundle, Mapping):
            continue
        candidates = by_message.get(message_ids[0], ())
        substantive = [item for item in candidates if not item.is_silent and not item.is_opener and item.role == "substantive"]
        if len(substantive) != 1:
            continue
        original = substantive[0]
        overlay = _overlay_fragment_from_model(original, semantic_bundle)
        # The source marker changes on every attempted overlay, so compare
        # semantic slots explicitly before consuming the message. An empty or
        # evidence-invalid model response must not shadow a later decision.
        semantic_changed = any(
            getattr(overlay, name) != getattr(original, name)
            for name in (
                "subject_id",
                "subject_type",
                "object_id",
                "object_resolution",
                "object_inherited_from_id",
                "object_evidence_refs",
                "state",
                "state_evidence",
                "claim_role",
                "modality",
                "actions",
                "evidence_refs",
                "event_completeness",
            )
        )
        if not semantic_changed:
            continue
        index = next((index for index, item in enumerate(updated) if item.fragment_id == original.fragment_id), None)
        if index is None:
            continue
        updated[index] = overlay
        # A's conservative claim is derived before C's accepted model result
        # is available. Keep its identity/span, but replace only semantic
        # slots used by D; otherwise the old object remains as a second
        # explicit object and correctly blocks the event as ambiguous.
        for claim_index, claim in enumerate(updated_claims):
            if claim.fragment_id != original.fragment_id:
                continue
            updated_claims[claim_index] = replace(
                claim,
                claim_type=overlay.claim_role,
                subject_id=overlay.subject_id,
                subject_type=overlay.subject_type,
                object_id=overlay.object_id,
                object_resolution=overlay.object_resolution,
                object_evidence_refs=overlay.object_evidence_refs,
                object_inherited_from_id=overlay.object_inherited_from_id,
                state=overlay.state,
                state_evidence=overlay.state_evidence,
                modality=overlay.modality,
                # D's claim contract requires exactly one typed evidence ref;
                # the fragment retains the complete model evidence set.
                evidence_refs=(overlay.evidence_refs[0],) if overlay.evidence_refs else claim.evidence_refs,
                event_completeness=overlay.event_completeness,
                source="workstream_e_llm_accepted",
            )
        consumed_messages.add(message_ids[0])
    if tuple(updated) == dialogue.fragments and tuple(updated_claims) == dialogue.claims:
        return dialogue
    return replace(dialogue, fragments=tuple(updated), claims=tuple(updated_claims))


def _feature_disabled_result(
    *,
    input_sha256: str,
    cache_key: str,
    config: ShadowRunConfig,
    mode: str,
) -> ShadowSemanticRunResult:
    features = config.resolved_features.to_dict()
    analysis_run_id = _string(config.analysis_run_id, "RUN_WORKSTREAM_E")
    run_id = "SHADOW_RUN_" + stable_hash({"input_sha256": input_sha256, "analysis_run_id": analysis_run_id})[:20]
    replay_key = stable_hash(
        {"cache_key": cache_key, "run_id": run_id, "pipeline_version": SHADOW_PIPELINE_VERSION}
    )
    discourse = DiscourseEventResult(
        source_marker=SOURCE_CONSERVATIVE_FALLBACK,
        fallback_source=_string(config.legacy_fallback_source, "legacy_fallback"),
        analysis_run_id=analysis_run_id,
        input_hash=input_sha256,
        cache_key=cache_key,
        pipeline_version=SHADOW_PIPELINE_VERSION,
        ruleset_version=SHADOW_RULESET_VERSION,
    )
    manifest = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "pipeline_version": SHADOW_PIPELINE_VERSION,
        "ruleset_version": SHADOW_RULESET_VERSION,
        "run_id": run_id,
        "analysis_run_id": analysis_run_id,
        "mode": mode,
        "source_marker": SOURCE_CONSERVATIVE_FALLBACK,
        "source": SOURCE_CONSERVATIVE_FALLBACK,
        "provider_status": "disabled",
        "llm_accepted": False,
        "fallback_reason": "shadow_feature_disabled",
        "model_status": "fallback",
        "input_sha256": input_sha256,
        "cache_key": cache_key,
        "replay_key": replay_key,
        "cache_hit": False,
        "feature_flags": features,
        "provider_blocked": False,
        "thread_count": 0,
        "event_candidate_count": 0,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "disabled_reason": "shadow_feature_disabled",
    }
    provenance = {
        "source": SOURCE_CONSERVATIVE_FALLBACK,
        "source_marker": SOURCE_CONSERVATIVE_FALLBACK,
        "provider_status": "disabled",
        "llm_accepted": False,
        "fallback_reason": "shadow_feature_disabled",
        "analysis_run_id": analysis_run_id,
        "input_sha256": input_sha256,
        "cache_key": cache_key,
        "replay_key": replay_key,
        "model_status": "fallback",
        "event_materialization_enabled": False,
        "evidence_policy": "typed_only",
        "component_versions": {
            "shadow": SHADOW_PIPELINE_VERSION,
            "ruleset": SHADOW_RULESET_VERSION,
        },
    }
    return ShadowSemanticRunResult(
        run_id=run_id,
        analysis_run_id=analysis_run_id,
        source_marker=SOURCE_CONSERVATIVE_FALLBACK,
        model_status="fallback",
        mode=mode,
        input_sha256=input_sha256,
        cache_key=cache_key,
        replay_key=replay_key,
        feature_flags=features,
        discourse_result=discourse,
        manifest=manifest,
        provenance=provenance,
        errors=(
            {
                "code": "shadow_feature_disabled",
                "source": SHADOW_PIPELINE_VERSION,
                "severity": "info",
            },
        ),
    )


def run_shadow_semantic(
    messages: Iterable[Mapping[str, Any]],
    *,
    mode: Optional[str] = None,
    model: Any = None,
    fragments: Optional[Iterable[Any]] = None,
    claims: Optional[Iterable[Any]] = None,
    config: Optional[ShadowRunConfig] = None,
    pipeline: Optional[Any] = None,
    dialogue_result: Optional[DialogueBundleResult] = None,
    cache: Optional["ShadowSemanticCache"] = None,
    store: Optional["ShadowSemanticRunStore"] = None,
) -> ShadowSemanticRunResult:
    """Run C -> D in memory and return an independently versioned shadow run."""

    cfg = config or ShadowRunConfig()
    selected_mode = _normalise_mode(mode if mode is not None else cfg.mode)
    message_values = tuple(messages or ())
    fragment_values = tuple(fragments or ())
    claim_values = tuple(claims or ())
    input_sha256 = shadow_input_hash(
        message_values,
        fragments=fragment_values,
        claims=claim_values,
        config=cfg,
        mode=selected_mode,
        model=model,
    )
    cache_key = _cache_key(input_sha256, cfg, model, mode=selected_mode)
    features = cfg.resolved_features

    if features.cache_enabled and cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            replay = cached.with_cache_hit()
            if features.store_enabled and store is not None:
                store.append(replay)
            return replay

    if not features.shadow_enabled:
        result = _feature_disabled_result(
            input_sha256=input_sha256,
            cache_key=cache_key,
            config=cfg,
            mode=selected_mode,
        )
        if features.cache_enabled and cache is not None:
            cache.put(result)
        if features.store_enabled and store is not None:
            store.append(result)
        return result

    effective_mode = selected_mode if features.bundle_semantics_enabled else "disabled"
    pipeline_value = pipeline or _build_pipeline(cfg, effective_mode, model)
    if not callable(getattr(pipeline_value, "run", None)):
        raise TypeError("pipeline must expose run")
    pipeline_result: PipelineRunResult = pipeline_value.run(
        message_values,
        fragments=fragment_values,
        claims=claim_values,
        split=_string(cfg.split, "development"),
    )
    source_marker = classify_source_marker(pipeline_result)
    model_status = _source_model_status(source_marker)
    if not features.discourse_enabled:
        discourse = DiscourseEventResult(
            source_marker=source_marker,
            fallback_source=_string(cfg.legacy_fallback_source, "legacy_fallback"),
            analysis_run_id=_string(cfg.analysis_run_id, "RUN_WORKSTREAM_E"),
            input_hash=input_sha256,
            cache_key=cache_key,
            pipeline_version=SHADOW_PIPELINE_VERSION,
            ruleset_version=SHADOW_RULESET_VERSION,
        )
    else:
        source_dialogue = dialogue_result or _dialogue_result_from_pipeline(
            pipeline_result,
            message_values,
            fragment_values,
            claim_values,
        )
        if source_marker == SOURCE_LLM_ACCEPTED and dialogue_result is None:
            source_dialogue = _apply_accepted_model_semantics(source_dialogue, pipeline_result)
        materialize = bool(
            features.event_materialization_enabled and source_marker == SOURCE_LLM_ACCEPTED
        )
        d_config = EventDerivationConfig(
            materialize_events=materialize,
            event_materialization_enabled=materialize,
            model_status=model_status,
            model_id=_string(pipeline_result.manifest.get("provider"), UNKNOWN),
            model_version=_string(pipeline_result.manifest.get("model"), UNKNOWN),
            prompt_version=_string(pipeline_result.manifest.get("bundle_prompt_version"), UNKNOWN),
            analysis_run_id=_string(cfg.analysis_run_id, "RUN_WORKSTREAM_E"),
            source_marker=source_marker,
            legacy_fallback_source=_string(cfg.legacy_fallback_source, "legacy_fallback"),
        )
        discourse = derive_discourse_and_events(source_dialogue, config=d_config)

    analysis_run_id = _string(cfg.analysis_run_id, "RUN_WORKSTREAM_E")
    run_id = "SHADOW_RUN_" + stable_hash(
        {"input_sha256": input_sha256, "analysis_run_id": analysis_run_id}
    )[:20]
    replay_key = stable_hash(
        {
            "cache_key": cache_key,
            "run_id": run_id,
            "source_marker": source_marker,
            "component_versions": {
                "contextual": CONTEXTUAL_PIPELINE_VERSION,
                "shadow": SHADOW_PIPELINE_VERSION,
                "thread": discourse.pipeline_version,
                "event": discourse.ruleset_version,
            },
        }
    )
    pipeline_manifest = pipeline_result.manifest if isinstance(pipeline_result.manifest, Mapping) else {}
    provider_blocked = source_marker == SOURCE_PROVIDER_BLOCKED
    errors = tuple(_body_free(dict(item)) for item in (pipeline_result.errors or ()) if isinstance(item, Mapping))
    provider_status = _provider_status_for(
        source_marker,
        mode=selected_mode,
        feature_flags=features.to_dict(),
    )
    llm_accepted = source_marker == SOURCE_LLM_ACCEPTED and provider_status == "succeeded"
    fallback_reason = _fallback_reason_for(
        source_marker,
        mode=selected_mode,
        feature_flags=features.to_dict(),
        errors=errors,
    )
    dialogue_handle = getattr(pipeline_result, "dialogue_result", None)
    dialogue_input_hash = getattr(dialogue_handle, "input_hash", UNKNOWN)
    manifest: Dict[str, Any] = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "pipeline_version": SHADOW_PIPELINE_VERSION,
        "ruleset_version": SHADOW_RULESET_VERSION,
        "run_id": run_id,
        "analysis_run_id": analysis_run_id,
        "mode": selected_mode,
        "effective_bundle_mode": effective_mode,
        "source_marker": source_marker,
        "source": source_marker,
        "provider_status": provider_status,
        "llm_accepted": llm_accepted,
        "fallback_reason": fallback_reason,
        "model_status": model_status,
        "input_sha256": input_sha256,
        "pipeline_input_sha256": pipeline_result.input_sha256,
        "dialogue_input_sha256": dialogue_input_hash,
        "cache_key": cache_key,
        "replay_key": replay_key,
        "cache_hit": False,
        "feature_flags": features.to_dict(),
        "provider_blocked": provider_blocked,
        "provider": _string(pipeline_manifest.get("provider"), UNKNOWN),
        "model": _string(pipeline_manifest.get("model"), UNKNOWN),
        "thread_count": len(discourse.threads),
        "event_candidate_count": len(discourse.event_candidates),
        "pipeline_error_count": len(errors),
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "component_versions": {
            "contextual_pipeline": CONTEXTUAL_PIPELINE_VERSION,
            "contextual_schema": _string(pipeline_manifest.get("schema_version"), UNKNOWN),
            "thread_pipeline": discourse.pipeline_version,
            "thread_ruleset": discourse.ruleset_version,
        },
    }
    provenance = {
        "source": source_marker,
        "source_marker": source_marker,
        "provider_status": provider_status,
        "llm_accepted": llm_accepted,
        "fallback_reason": fallback_reason,
        "analysis_run_id": analysis_run_id,
        "input_sha256": input_sha256,
        "pipeline_input_sha256": pipeline_result.input_sha256,
        "dialogue_input_sha256": dialogue_input_hash,
        "cache_key": cache_key,
        "replay_key": replay_key,
        "model_status": model_status,
        "provider_blocked": provider_blocked,
        "event_materialization_enabled": bool(features.event_materialization_enabled),
        "event_candidate_gate": "accepted_model_and_typed_subject_object_action_state_evidence",
        "evidence_policy": "typed_only",
        "fallback_source": _string(cfg.legacy_fallback_source, "legacy_fallback"),
        "component_versions": manifest["component_versions"],
    }
    result = ShadowSemanticRunResult(
        run_id=run_id,
        analysis_run_id=analysis_run_id,
        source_marker=source_marker,
        model_status=model_status,
        mode=selected_mode,
        input_sha256=input_sha256,
        cache_key=cache_key,
        replay_key=replay_key,
        feature_flags=features.to_dict(),
        discourse_result=discourse,
        pipeline_result=pipeline_result,
        manifest=manifest,
        provenance=provenance,
        errors=errors,
    )
    if features.cache_enabled and cache is not None:
        cache.put(result)
    if features.store_enabled and store is not None:
        store.append(result)
    return result


def replay_shadow_semantic(
    messages: Iterable[Mapping[str, Any]],
    *,
    config: Optional[ShadowRunConfig] = None,
    **kwargs: Any,
) -> ShadowSemanticRunResult:
    """Replay the same public input/config through the same hash/cache path."""

    return run_shadow_semantic(messages, config=config, **kwargs)


run_shadow_semantic_run = run_shadow_semantic
run_shadow_semantic_orchestrator = run_shadow_semantic


class ShadowSemanticCache:
    """Versioned in-memory cache keyed by the complete shadow input hash."""

    def __init__(self) -> None:
        self._values: Dict[str, ShadowSemanticRunResult] = {}

    def get(self, cache_key: str) -> Optional[ShadowSemanticRunResult]:
        return self._values.get(str(cache_key))

    def put(self, result: ShadowSemanticRunResult) -> None:
        key = str(result.cache_key)
        prior = self._values.get(key)
        if prior is not None and (
            prior.input_sha256 != result.input_sha256 or prior.replay_key != result.replay_key
        ):
            raise ValueError("conflicting shadow cache key: %s" % key)
        self._values[key] = result

    append = put

    def clear(self) -> None:
        self._values.clear()

    def snapshot(self) -> Dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "pipeline_version": SHADOW_PIPELINE_VERSION,
            "ruleset_version": SHADOW_RULESET_VERSION,
            "entry_count": len(self._values),
            "entries": [
                {
                    "cache_key": result.cache_key,
                    "run_id": result.run_id,
                    "input_sha256": result.input_sha256,
                    "replay_key": result.replay_key,
                    "source_marker": result.source_marker,
                }
                for result in self._values.values()
            ],
        }


ShadowRunCache = ShadowSemanticCache


class ShadowSemanticRunStore:
    """Append-only in-memory shadow run store with D's artifact index."""

    def __init__(self) -> None:
        self._runs: Dict[str, ShadowSemanticRunResult] = {}
        self._artifacts = ShadowSemanticStore()

    def append(self, result: ShadowSemanticRunResult) -> None:
        key = str(result.run_id)
        prior = self._runs.get(key)
        if prior is not None and prior.input_sha256 != result.input_sha256:
            raise ValueError("conflicting shadow run id: %s" % key)
        self._runs[key] = result
        self._artifacts.append(result.discourse_result)

    put = append

    def get(self, run_id: str) -> Optional[ShadowSemanticRunResult]:
        return self._runs.get(str(run_id))

    def runs(self) -> Tuple[ShadowSemanticRunResult, ...]:
        return tuple(self._runs.values())

    def threads(self) -> Tuple[DiscourseThread, ...]:
        return self._artifacts.threads()

    def events(self) -> Tuple[EventCandidate, ...]:
        return self._artifacts.events()

    def snapshot(self) -> Dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "pipeline_version": SHADOW_PIPELINE_VERSION,
            "ruleset_version": SHADOW_RULESET_VERSION,
            "run_count": len(self._runs),
            "runs": [
                {
                    "run_id": result.run_id,
                    "analysis_run_id": result.analysis_run_id,
                    "source_marker": result.source_marker,
                    "model_status": result.model_status,
                    "input_sha256": result.input_sha256,
                    "cache_key": result.cache_key,
                    "replay_key": result.replay_key,
                    "manifest": _body_free(dict(result.manifest)),
                    "provenance": _body_free(dict(result.provenance)),
                }
                for result in self._runs.values()
            ],
            "threads": [thread.to_dict() for thread in self.threads()],
            "event_candidates": [candidate.to_dict() for candidate in self.events()],
        }


ShadowStore = ShadowSemanticRunStore


class ShadowSemanticRunner:
    """Reusable service facade around :func:`run_shadow_semantic`."""

    def __init__(
        self,
        *,
        config: Optional[ShadowRunConfig] = None,
        pipeline: Optional[Any] = None,
        cache: Optional[ShadowSemanticCache] = None,
        store: Optional[ShadowSemanticRunStore] = None,
    ) -> None:
        self.config = config or ShadowRunConfig()
        self.pipeline = pipeline
        self.cache = cache if cache is not None else ShadowSemanticCache()
        self.store = store if store is not None else ShadowSemanticRunStore()

    def run(self, messages: Iterable[Mapping[str, Any]], **kwargs: Any) -> ShadowSemanticRunResult:
        kwargs.setdefault("config", self.config)
        kwargs.setdefault("pipeline", self.pipeline)
        kwargs.setdefault("cache", self.cache)
        kwargs.setdefault("store", self.store)
        return run_shadow_semantic(messages, **kwargs)

    process = run

    def replay(self, messages: Iterable[Mapping[str, Any]], **kwargs: Any) -> ShadowSemanticRunResult:
        return self.run(messages, **kwargs)


ShadowSemanticService = ShadowSemanticRunner


__all__ = [
    "SHADOW_SCHEMA_VERSION",
    "SHADOW_PIPELINE_VERSION",
    "SHADOW_RULESET_VERSION",
    "SOURCE_LLM_ACCEPTED",
    "SOURCE_LLM_PENDING",
    "SOURCE_CONSERVATIVE_FALLBACK",
    "SOURCE_PROVIDER_BLOCKED",
    "SOURCE_MARKERS",
    "ShadowFeatureFlags",
    "ShadowRunConfig",
    "ShadowSemanticConfig",
    "ShadowSemanticRunResult",
    "ShadowRunResult",
    "ShadowSemanticCache",
    "ShadowRunCache",
    "ShadowSemanticRunStore",
    "ShadowStore",
    "ShadowSemanticRunner",
    "ShadowSemanticService",
    "shadow_input_hash",
    "classify_source_marker",
    "run_shadow_semantic",
    "run_shadow_semantic_run",
    "run_shadow_semantic_orchestrator",
    "replay_shadow_semantic",
]
