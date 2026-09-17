"""Low-cost, provider-free scheduler for contextual bundle candidates.

Workstream I sits between dialogue-bundle generation and any provider call.
It selects a small, auditable set of representatives from a larger candidate
set, while retaining every candidate/window membership in the private
decision projection.  The scheduler never reads frozen inputs, never calls a
provider, and never creates events or frontend data.

The selection contract is deliberately structural.  It uses scope, message,
fragment, claim, evidence, semantic-slot and channel metadata.  Time and
same-segment metadata are retained as zero-weight audit fields and cannot
create priority or linkage.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contextual_bundle_pipeline_runner import _read_messages
from .semantic_registry import stable_hash
from .shadow_run_artifact import (
    _assert_body_free,
    _body_fields,
    _guard_contextual_artifact,
    _guard_development_input,
    _guard_output,
    _guard_path,
    _read_json,
    _read_jsonl,
    _sha256_file,
    _write_json,
)


SCHEDULER_ARTIFACT_VERSION = "contextual_bundle_scheduler_v2_9"
SCHEDULER_SCHEMA_VERSION = "contextual_bundle_scheduler_schema_v2_9"
SCHEDULER_PROVENANCE_VERSION = "contextual_bundle_scheduler_provenance_v2_9"
SCHEDULER_RULESET_VERSION = "contextual_bundle_scheduler_rules_v2_9"
SOURCE_ARTIFACT_VERSION = "contextual_bundle_pipeline_v2_8"
DEVELOPMENT_SPLIT = "development"
DEVELOPMENT_LOCAL_DAY = "2026-08-25"
INPUT_FILENAME = "messages.private.jsonl"
SOURCE_DECISIONS_FILENAME = "decisions.private.jsonl"
SOURCE_BUNDLES_FILENAME = "bundles.private.jsonl"
SOURCE_MANIFEST_FILENAME = "manifest.private.json"
SCHEDULER_OUTPUT_FILES = {
    "manifest": "manifest.private.json",
    "provenance": "provenance.private.json",
    "aggregate": "aggregate.private.json",
    "decisions": "decisions.private.jsonl",
    "coverage": "coverage.private.json",
}
DEFAULT_PROVIDER_CALL_BUDGET = 14
PENDING_CHANNELS = frozenset({"pending_context", "cold_recoverable"})
INFO_SCORES = {"high": 8.0, "medium": 5.0, "unknown": 2.0, "low": 1.0, "none": 0.0}
COMPLETENESS_SCORES = {"sufficient": 8.0, "partial": 3.0, "unknown": 1.0, "not_applicable": 0.0}
CHANNEL_SCORES = {"immediate": 2.0, "pending_context": 4.0, "cold_recoverable": 3.0, "background": 0.0}
SCALE_SCORES = {"session": 3.0, "local": 2.0, "turn": 1.0, "micro": 1.0, "sparse": 1.0, "cold": 1.0}
QUESTION_ROLES = frozenset({"question", "request", "answer", "reply", "qa", "query"})
KNOWN_STATES = frozenset({"planned", "ongoing", "resolved", "failed", "cancelled", "recurring"})


def _tuple_strings(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        values = (value,)
    elif isinstance(value, Mapping):
        values = (value.get("id") or value.get("message_id") or value.get("fragment_id"),)
    else:
        try:
            values = tuple(value)
        except TypeError:
            values = (value,)
    output: List[str] = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("id") or item.get("message_id") or item.get("fragment_id") or item.get("claim_id")
        text = str(item or "").strip()
        if text and text not in output:
            output.append(text)
    return tuple(output)


def _mapping_values(value: Any) -> Tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (str, bytes)) or value is None:
        return ()
    try:
        return tuple(item for item in value if isinstance(item, Mapping))
    except TypeError:
        return ()


def _evidence_keys(value: Any) -> Tuple[str, ...]:
    output: List[str] = []
    values = value if isinstance(value, (list, tuple)) else ((value,) if isinstance(value, Mapping) else ())
    for item in values:
        if not isinstance(item, Mapping):
            continue
        evidence_id = str(item.get("evidence_id") or item.get("id") or item.get("message_id") or "").strip()
        span = item.get("span") if isinstance(item.get("span"), Mapping) else {}
        key = "%s:%s:%s" % (evidence_id, span.get("start", ""), span.get("end", ""))
        if key != ":" and key not in output:
            output.append(key)
    return tuple(output)


def _semantic_flag(semantic: Mapping[str, Any], *names: str) -> bool:
    for name in names:
        value = semantic.get(name)
        if isinstance(value, Mapping):
            if str(value.get("id") or value.get("entity_id") or value.get("label") or "").strip():
                return True
        elif isinstance(value, (list, tuple, set, frozenset)):
            if any(item not in (None, "", (), [], {}) for item in value):
                return True
        elif value not in (None, "", "unknown", "UNKNOWN", "N/A", "n/a", (), [], {}):
            return True
    return False


def _semantic_flags(semantic: Mapping[str, Any]) -> Dict[str, bool]:
    state = str(semantic.get("state") or "unknown").casefold()
    claim_role = str(semantic.get("claim_type") or semantic.get("claim_role") or "").casefold()
    modality = str(semantic.get("modality") or "").casefold()
    return {
        "explicit_subject": _semantic_flag(semantic, "subject", "speaker"),
        "explicit_object": _semantic_flag(semantic, "object", "objects", "target"),
        "explicit_action": _semantic_flag(semantic, "action", "actions"),
        "explicit_state": state in KNOWN_STATES,
        "mentioned_person": _semantic_flag(semantic, "mentioned_person", "mentioned_people"),
        "qa": claim_role in QUESTION_ROLES or modality in QUESTION_ROLES,
        "semantic_evidence": _semantic_flag(semantic, "evidence", "evidence_refs"),
        "semantic_uncertainty": _semantic_flag(semantic, "uncertainties", "unresolved_slots"),
    }


def _normalise_candidate(value: Mapping[str, Any], ordinal: int) -> Dict[str, Any]:
    candidate_id = str(value.get("bundle_id") or value.get("candidate_id") or "candidate-%06d" % ordinal).strip()
    source_message_ids = _tuple_strings(value.get("source_message_ids") or value.get("member_message_ids") or value.get("message_ids"))
    fragment_ids = _tuple_strings(value.get("fragment_ids") or value.get("member_fragment_ids"))
    claim_ids = _tuple_strings(value.get("claim_ids") or value.get("member_claim_ids"))
    evidence_refs = value.get("evidence_refs") or value.get("evidence") or ()
    evidence_keys = _evidence_keys(evidence_refs)
    semantic = value.get("semantic_bundle") if isinstance(value.get("semantic_bundle"), Mapping) else {}
    flags = _semantic_flags(semantic)
    uncertainties = _tuple_strings(value.get("uncertainties") or value.get("unresolved_slot_codes"))
    if not source_message_ids and fragment_ids:
        source_message_ids = fragment_ids
    return {
        "candidate_id": candidate_id,
        "bundle_id": candidate_id,
        "scale": str(value.get("scale") or value.get("window_scale") or "unknown"),
        "channel": str(value.get("channel") or value.get("gate_channel") or "pending_context"),
        "chat_id": str(value.get("chat_id") or "unknown"),
        "account_id": str(value.get("account_id") or "unknown"),
        "source_message_ids": source_message_ids,
        "fragment_ids": fragment_ids,
        "claim_ids": claim_ids,
        "evidence_keys": evidence_keys,
        "speaker_ids": _tuple_strings(value.get("speaker_ids") or value.get("speaker_refs")) + _tuple_strings(semantic.get("speaker")),
        "mentioned_person_ids": _tuple_strings(value.get("mentioned_person_ids") or value.get("mentioned_person_refs")) + _tuple_strings(semantic.get("mentioned_person")),
        "subject_ids": _tuple_strings(value.get("subject_ids") or value.get("subject_refs")) + _tuple_strings(semantic.get("subject")),
        "object_refs": tuple(_mapping_values(value.get("object_refs"))) + tuple(_mapping_values(semantic.get("object") or semantic.get("objects"))),
        "state_sequence": _tuple_strings(value.get("state_sequence") or value.get("state_refs")) + ((str(semantic.get("state")),) if flags["explicit_state"] else ()),
        "information_value": str(value.get("information_value") or "unknown").casefold(),
        "event_completeness": str(value.get("event_completeness") or "unknown").casefold(),
        "uncertainties": uncertainties,
        "candidate_bundle_ids": _tuple_strings(value.get("candidate_bundle_ids") or value.get("member_bundle_ids")),
        "open_context_snapshot_id": str(value.get("open_context_snapshot_id") or "").strip(),
        "forced_snapshot": bool(value.get("forced_snapshot")),
        "closed": bool(value.get("closed")),
        "candidate_rank": int(value.get("candidate_rank") or 0),
        "semantic": semantic,
        "flags": flags,
        # These are intentionally retained as audit zeros.  They are never
        # used by scoring, deduplication, or coverage.
        "time_signal": 0.0,
        "same_segment_signal": 0.0,
    }


def _package_key(candidate: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Exact structural package key; scale/window membership is excluded."""

    flags = candidate.get("flags") or {}
    # Object references remain mappings in the normalized candidate so the
    # decision projection can retain their shape/count.  The package key must
    # nevertheless be hashable (and deterministic across mapping order), so
    # use canonical content digests only for grouping.
    object_keys = tuple(sorted(stable_hash(item) for item in candidate.get("object_refs") or ()))
    return (
        str(candidate.get("chat_id") or "unknown"),
        tuple(sorted(candidate.get("source_message_ids") or ())),
        tuple(sorted(candidate.get("fragment_ids") or ())),
        tuple(sorted(candidate.get("claim_ids") or ())),
        tuple(sorted(candidate.get("evidence_keys") or ())),
        tuple(sorted(candidate.get("subject_ids") or ())),
        tuple(sorted(candidate.get("speaker_ids") or ())),
        tuple(sorted(candidate.get("mentioned_person_ids") or ())),
        object_keys,
        tuple(sorted(candidate.get("state_sequence") or ())),
        tuple(sorted((name, bool(value)) for name, value in flags.items())),
        str(candidate.get("event_completeness") or "unknown"),
    )


def _base_score(candidate: Mapping[str, Any]) -> Tuple[float, Dict[str, float]]:
    flags = candidate.get("flags") or {}
    values: Dict[str, float] = {
        "information_density": INFO_SCORES.get(str(candidate.get("information_value") or "unknown"), 1.0),
        "event_completeness": COMPLETENESS_SCORES.get(str(candidate.get("event_completeness") or "unknown"), 1.0),
        "explicit_object": 7.0 if flags.get("explicit_object") else 0.0,
        "explicit_subject": 4.0 if flags.get("explicit_subject") else 0.0,
        "explicit_action": 6.0 if flags.get("explicit_action") else 0.0,
        "explicit_state": 5.0 if flags.get("explicit_state") else 0.0,
        "mentioned_person": 2.0 if flags.get("mentioned_person") else 0.0,
        "qa_unresolved": 4.0 if flags.get("qa") else 0.0,
        "new_evidence": 3.0 if candidate.get("evidence_keys") else 0.0,
        "unresolved_slots": 2.0 if candidate.get("uncertainties") else 0.0,
        "reactivation": 3.0 if candidate.get("candidate_bundle_ids") or candidate.get("open_context_snapshot_id") or candidate.get("forced_snapshot") else 0.0,
        "channel_priority": CHANNEL_SCORES.get(str(candidate.get("channel") or ""), 0.0),
        "window_scale": SCALE_SCORES.get(str(candidate.get("scale") or ""), 0.0),
        "time": 0.0,
        "same_segment": 0.0,
    }
    return sum(values.values()), values


def _activation_cues(candidate: Mapping[str, Any]) -> Tuple[Dict[str, Any], ...]:
    """Build deterministic, executable cues for a pending/cold candidate."""

    flags = candidate.get("flags") or {}
    target_ids = tuple(candidate.get("source_message_ids") or ())
    cues: List[Tuple[str, str, Tuple[str, ...]]] = []
    if flags.get("explicit_object") or "object_unknown" in candidate.get("uncertainties", ()) or not candidate.get("object_refs"):
        cues.append(("object_resolution", "query_or_resolve_object", ("object",)))
    if flags.get("explicit_subject") or candidate.get("speaker_ids") or candidate.get("mentioned_person_ids"):
        cues.append(("person_resolution", "resolve_speaker_or_mentioned_person", ("person",)))
    if flags.get("explicit_state") or "state_unknown" in candidate.get("uncertainties", ()) or not candidate.get("state_sequence"):
        cues.append(("state_update", "query_latest_state_or_state_change", ("state",)))
    if candidate.get("evidence_keys") or candidate.get("claim_ids") or flags.get("semantic_evidence"):
        cues.append(("evidence_lookup", "replay_claim_evidence_refs", ("evidence", "reference")))
    if flags.get("qa"):
        cues.append(("qa_followup", "resolve_question_or_answer_link", ("question", "answer")))
    if candidate.get("candidate_bundle_ids") or candidate.get("open_context_snapshot_id") or candidate.get("forced_snapshot"):
        cues.append(("context_reactivation", "reactivate_prior_context_window", ("reactivation",)))
    if candidate.get("channel") in PENDING_CHANNELS:
        cues.append(("budget_recovery", "retry_when_scheduler_budget_recovers", ("budget", "replay")))
    if not cues:
        cues.append(("context_query", "query_structured_context_for_replay", ("context",)))
    result: List[Dict[str, Any]] = []
    for cue_type, action, required_fields in cues:
        replay_key = stable_hash(
            {
                "scheduler_ruleset": SCHEDULER_RULESET_VERSION,
                "candidate_id": candidate.get("candidate_id"),
                "cue_type": cue_type,
                "target_ids": target_ids,
                "required_fields": required_fields,
            }
        )
        result.append(
            {
                "cue_id": "ACTIVATION_CUE_" + replay_key[:20],
                "cue_type": cue_type,
                "action": action,
                "required_fields": list(required_fields),
                "target_message_count": len(target_ids),
                "target_scope": str(candidate.get("chat_id") or "unknown"),
                "replay_key": replay_key,
                "executable": True,
            }
        )
    return tuple(result)


@dataclass(frozen=True)
class SchedulerRun:
    """Pure scheduler result; no provider calls or event materialization."""

    decisions: Tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any]

    @property
    def selected(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(item for item in self.decisions if item.get("selection_status") == "selected")

    def to_dict(self) -> Dict[str, Any]:
        value = {
            "decisions": [dict(item) for item in self.decisions],
            "metrics": dict(self.metrics),
            "provider_calls": int(self.metrics.get("provider_calls") or 0),
            "encoded_bundle_count": int(self.metrics.get("encoded_bundle_count") or 0),
            "body_free": True,
        }
        _assert_body_free(value, label="scheduler run")
        return value


def schedule_bundle_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    max_provider_calls: int = DEFAULT_PROVIDER_CALL_BUDGET,
) -> SchedulerRun:
    """Select at most ``max_provider_calls`` unique semantic packages.

    This function is intentionally provider-free.  ``selected`` means a
    candidate is scheduled for a possible future encode; it does not mean a
    provider request happened.  The returned decision stream contains one
    record per original candidate, including duplicate/window membership and
    explicit not-selected reasons.
    """

    budget = max(0, int(max_provider_calls))
    normalised = tuple(_normalise_candidate(value, index) for index, value in enumerate(candidates, 1))
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    for candidate in normalised:
        groups[_package_key(candidate)].append(candidate)
    packages: List[Dict[str, Any]] = []
    for key, members in groups.items():
        ranked = sorted(
            members,
            key=lambda item: (
                _base_score(item)[0],
                SCALE_SCORES.get(str(item.get("scale") or ""), 0.0),
                -int(item.get("candidate_rank") or 0),
                str(item.get("candidate_id") or ""),
            ),
            reverse=True,
        )
        representative = ranked[0]
        base, breakdown = _base_score(representative)
        packages.append(
            {
                "package_key": key,
                "semantic_package_id": "SEMANTIC_PACKAGE_" + stable_hash(key)[:20],
                "members": tuple(members),
                "representative": representative,
                "base_score": base,
                "base_breakdown": breakdown,
                "window_membership": tuple(sorted({str(item.get("scale") or "unknown") for item in members})),
                "channels": tuple(sorted({str(item.get("channel") or "unknown") for item in members})),
            }
        )
    packages.sort(
        key=lambda item: (
            item["base_score"],
            len(item["representative"].get("source_message_ids") or ()),
            str(item["semantic_package_id"]),
        ),
        reverse=True,
    )
    selected_packages: List[Dict[str, Any]] = []
    selected_messages: set[str] = set()
    selected_chats: set[str] = set()
    selected_scales: set[str] = set()
    for _ in range(min(budget, len(packages))):
        best = None
        best_value: Tuple[float, ...] = ()
        for package in packages:
            if package in selected_packages:
                continue
            representative = package["representative"]
            messages = set(representative.get("source_message_ids") or ())
            chat = str(representative.get("chat_id") or "unknown")
            scales = set(package["window_membership"])
            coverage_gain = min(4.0, float(len(messages - selected_messages)) * 0.25)
            chat_diversity = 3.0 if chat not in selected_chats else 0.0
            window_diversity = 1.5 if not scales.intersection(selected_scales) else 0.0
            reactivation_bonus = 1.0 if representative.get("channel") in PENDING_CHANNELS else 0.0
            value = (
                package["base_score"] + coverage_gain + chat_diversity + window_diversity + reactivation_bonus,
                coverage_gain,
                chat_diversity,
                window_diversity,
                package["base_score"],
                -float(len(messages)),
                -float(len(package["members"])),
            )
            if best is None or value > best_value or (value == best_value and package["semantic_package_id"] < best["semantic_package_id"]):
                best = package
                best_value = value
        if best is None:
            break
        selected_packages.append(best)
        representative = best["representative"]
        selected_messages.update(representative.get("source_message_ids") or ())
        selected_chats.add(str(representative.get("chat_id") or "unknown"))
        selected_scales.update(best["window_membership"])
    selected_ids = {package["semantic_package_id"] for package in selected_packages}
    selected_representative_ids = {package["representative"]["candidate_id"] for package in selected_packages}
    package_by_candidate: Dict[str, Dict[str, Any]] = {}
    for package in packages:
        for member in package["members"]:
            package_by_candidate[member["candidate_id"]] = package

    decisions: List[Dict[str, Any]] = []
    selected_message_union: set[str] = set()
    selected_chat_values: set[str] = set()
    selected_scale_values: set[str] = set()
    for candidate in normalised:
        package = package_by_candidate[candidate["candidate_id"]]
        is_representative = candidate["candidate_id"] == package["representative"]["candidate_id"]
        package_selected = package["semantic_package_id"] in selected_ids
        if package_selected and is_representative:
            status = "selected"
            reason = "selected_budget_rank"
            selected_message_union.update(candidate["source_message_ids"])
            selected_chat_values.add(str(candidate.get("chat_id") or "unknown"))
            selected_scale_values.update(package["window_membership"])
        elif package_selected:
            status = "not_selected"
            reason = "duplicate_semantic_package"
        else:
            status = "not_selected"
            reason = "scheduler_budget_exhausted"
        cues = _activation_cues(candidate)
        base_score, score_breakdown = _base_score(candidate)
        score_breakdown = dict(score_breakdown)
        package_members = package["members"]
        decisions.append(
            {
                "decision_version": SCHEDULER_SCHEMA_VERSION,
                "candidate_id": candidate["candidate_id"],
                "semantic_package_id": package["semantic_package_id"],
                "selection_status": status,
                "selection_reason": reason,
                "selected_representative_id": package["representative"]["candidate_id"],
                "scheduled_for_encode": bool(status == "selected"),
                "scale": candidate["scale"],
                "window_membership": list(package["window_membership"]),
                "channel": candidate["channel"],
                "chat_id": candidate["chat_id"],
                "source_message_count": len(candidate["source_message_ids"]),
                "source_message_ids": list(candidate["source_message_ids"]),
                "fragment_count": len(candidate["fragment_ids"]),
                "claim_count": len(candidate["claim_ids"]),
                "evidence_count": len(candidate["evidence_keys"]),
                "speaker_count": len(set(candidate["speaker_ids"])),
                "mentioned_person_count": len(set(candidate["mentioned_person_ids"])),
                "subject_count": len(set(candidate["subject_ids"])),
                "activation_cues": [dict(cue) for cue in cues],
                "activation_cue_count": len(cues),
                "activation_cue_replayable": all(cue.get("executable") is True and cue.get("replay_key") for cue in cues),
                "semantic_package_candidate_count": len(package_members),
                "semantic_package_candidate_ids": [item["candidate_id"] for item in package_members],
                "score": base_score,
                "score_breakdown": score_breakdown,
                "time_signal_weight": 0.0,
                "same_segment_signal_weight": 0.0,
            }
        )
    all_messages = set().union(*(set(item.get("source_message_ids") or ()) for item in normalised)) if normalised else set()
    eligible = [item for item in decisions if item["channel"] in PENDING_CHANNELS]
    cue_eligible = [item for item in eligible if item["activation_cues"]]
    selected_count = len(selected_packages)
    metrics: Dict[str, Any] = {
        "scheduler_version": SCHEDULER_ARTIFACT_VERSION,
        "ruleset_version": SCHEDULER_RULESET_VERSION,
        "candidate_count": len(normalised),
        "decision_count": len(decisions),
        "unique_semantic_package_count": len(packages),
        "duplicate_candidate_count": len(normalised) - len(packages),
        "selected_count": selected_count,
        "scheduled_encoded_bundle_count": selected_count,
        "encoded_bundle_count": 0,
        "provider_calls": 0,
        "provider_calls_are_actual_requests": True,
        "dry_run": True,
        "pending_source_count": sum(1 for item in normalised if item["channel"] in PENDING_CHANNELS),
        "cold_recoverable_count": sum(1 for item in normalised if item["channel"] == "cold_recoverable"),
        "activation_cue_eligible_count": len(eligible),
        "activation_cue_covered_count": len(cue_eligible),
        "activation_cue_zero_count": len(eligible) - len(cue_eligible),
        "activation_cue_coverage_rate": (float(len(cue_eligible)) / float(len(eligible))) if eligible else 1.0,
        "total_unique_source_message_count": len(all_messages),
        "estimated_message_coverage_count": len(selected_message_union),
        "estimated_message_coverage_ratio": (float(len(selected_message_union)) / float(len(all_messages))) if all_messages else 0.0,
        "selected_chat_count": len(selected_chat_values),
        "selected_window_scale_count": len(selected_scale_values),
        "compression_ratio": (float(len(normalised)) / float(selected_count)) if selected_count else 0.0,
        "package_compression_ratio": (float(len(packages)) / float(selected_count)) if selected_count else 0.0,
        "time_signal_weight": 0.0,
        "same_segment_signal_weight": 0.0,
        "time_or_same_segment_only_strong_link_count": 0,
    }
    # This invariant is the zero-tolerance gate for all pending/recovery lanes.
    if metrics["activation_cue_zero_count"]:
        raise ValueError("pending/cold candidates must have executable activation cues")
    value = SchedulerRun(decisions=tuple(decisions), metrics=metrics)
    _assert_body_free(value.to_dict(), label="scheduler result")
    return value


@dataclass(frozen=True)
class SchedulerArtifact:
    output_directory: str
    artifact_paths: Mapping[str, str]
    manifest: Mapping[str, Any]
    provenance: Mapping[str, Any]
    aggregate: Mapping[str, Any]
    run: SchedulerRun

    def to_dict(self) -> Dict[str, Any]:
        value = {
            "output_directory": self.output_directory,
            "artifact_paths": dict(self.artifact_paths),
            "manifest": dict(self.manifest),
            "provenance": dict(self.provenance),
            "aggregate": dict(self.aggregate),
            "metrics": dict(self.run.metrics),
        }
        _assert_body_free(value, label="scheduler artifact")
        return value


def _scheduler_code_sha256() -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__).with_name("contextual_bundle_scheduler_v2_9.py"),
        Path(__file__).with_name("dialogue_bundle.py"),
        Path(__file__).with_name("semantic_registry.py"),
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def run_development_scheduler_v29(
    input_directory: Union[str, Path],
    contextual_artifact_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    max_provider_calls: int = DEFAULT_PROVIDER_CALL_BUDGET,
    analysis_run_id: str = SCHEDULER_ARTIFACT_VERSION,
) -> SchedulerArtifact:
    """Create the development-only, provider-free v2.9 scheduler artifact."""

    input_root = _guard_development_input(input_directory)
    source_root = _guard_contextual_artifact(contextual_artifact_directory)
    output_root = _guard_output(output_directory)
    messages, raw = _read_messages(input_root)
    source_manifest = _read_json(source_root / SOURCE_MANIFEST_FILENAME)
    if source_manifest.get("artifact_version") != SOURCE_ARTIFACT_VERSION:
        raise ValueError("scheduler source must be contextual_bundle_pipeline_v2_8")
    if source_manifest.get("split") != DEVELOPMENT_SPLIT:
        raise ValueError("scheduler source must be development")
    if source_manifest.get("frozen_read") is not False or source_manifest.get("gold_loaded") is not False:
        raise ValueError("scheduler source must prove no frozen/gold read")
    if source_manifest.get("body_free_outputs") is not True:
        raise ValueError("scheduler source must prove body_free_outputs=true")
    input_sha256 = hashlib.sha256(raw).hexdigest()
    if str(source_manifest.get("input_sha256") or "") != input_sha256:
        raise ValueError("scheduler development input does not match v2.8 source")
    source_decisions = _read_jsonl(source_root / SOURCE_DECISIONS_FILENAME)
    source_bundles = _read_jsonl(source_root / SOURCE_BUNDLES_FILENAME)
    if len(source_decisions) != len(source_bundles):
        raise ValueError("scheduler source decisions/bundles count mismatch")
    decision_by_id = {str(row.get("bundle_id") or ""): row for row in source_decisions}
    if len(decision_by_id) != len(source_decisions):
        raise ValueError("scheduler source decisions contain duplicate bundle ids")
    candidates: List[Dict[str, Any]] = []
    for bundle in source_bundles:
        bundle_id = str(bundle.get("bundle_id") or "")
        decision = decision_by_id.get(bundle_id)
        if decision is None:
            raise ValueError("scheduler bundle has no matching decision")
        candidate = dict(bundle)
        candidate["semantic_bundle"] = decision.get("semantic_bundle") if isinstance(decision.get("semantic_bundle"), Mapping) else {}
        candidate["source_decision_status"] = str(decision.get("status") or "unknown")
        candidates.append(candidate)
    scheduler = schedule_bundle_candidates(candidates, max_provider_calls=max_provider_calls)
    source_status_counts = Counter(str(row.get("status") or "unknown") for row in source_decisions)
    code_sha256 = _scheduler_code_sha256()
    source_manifest_sha256 = _sha256_file(source_root / SOURCE_MANIFEST_FILENAME)
    source_decisions_sha256 = _sha256_file(source_root / SOURCE_DECISIONS_FILENAME)
    source_bundles_sha256 = _sha256_file(source_root / SOURCE_BUNDLES_FILENAME)
    metrics = dict(scheduler.metrics)
    manifest: Dict[str, Any] = {
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "schema_version": SCHEDULER_SCHEMA_VERSION,
        "provenance_version": SCHEDULER_PROVENANCE_VERSION,
        "ruleset_version": SCHEDULER_RULESET_VERSION,
        "analysis_run_id": str(analysis_run_id).strip() or SCHEDULER_ARTIFACT_VERSION,
        "mode": "dry_run",
        "split": DEVELOPMENT_SPLIT,
        "local_day": DEVELOPMENT_LOCAL_DAY,
        "input_directory_name": input_root.name,
        "input_filename": INPUT_FILENAME,
        "development_input_sha256": input_sha256,
        "source_artifact_version": SOURCE_ARTIFACT_VERSION,
        "source_artifact_directory_name": source_root.name,
        "source_artifact_manifest_sha256": source_manifest_sha256,
        "source_artifact_decisions_sha256": source_decisions_sha256,
        "source_artifact_bundles_sha256": source_bundles_sha256,
        "source_message_count": len(messages),
        "source_decision_status_counts": dict(sorted(source_status_counts.items())),
        "provider_calls": 0,
        "encoded_bundle_count": 0,
        "candidate_count": metrics["candidate_count"],
        "decision_count": metrics["decision_count"],
        "selected_count": metrics["selected_count"],
        "scheduled_encoded_bundle_count": metrics["scheduled_encoded_bundle_count"],
        "unique_semantic_package_count": metrics["unique_semantic_package_count"],
        "estimated_message_coverage_count": metrics["estimated_message_coverage_count"],
        "estimated_message_coverage_ratio": metrics["estimated_message_coverage_ratio"],
        "compression_ratio": metrics["compression_ratio"],
        "activation_cue_eligible_count": metrics["activation_cue_eligible_count"],
        "activation_cue_covered_count": metrics["activation_cue_covered_count"],
        "activation_cue_zero_count": metrics["activation_cue_zero_count"],
        "activation_cue_coverage_rate": metrics["activation_cue_coverage_rate"],
        "time_signal_weight": 0.0,
        "same_segment_signal_weight": 0.0,
        "dry_run": True,
        "provider_calls_are_actual_requests": True,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": code_sha256,
        "output_files": dict(SCHEDULER_OUTPUT_FILES),
    }
    provenance: Dict[str, Any] = {
        "provenance_version": SCHEDULER_PROVENANCE_VERSION,
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "analysis_run_id": manifest["analysis_run_id"],
        "source_artifact_version": SOURCE_ARTIFACT_VERSION,
        "source_artifact_directory_name": source_root.name,
        "development_input_sha256": input_sha256,
        "source_artifact_manifest_sha256": source_manifest_sha256,
        "source_artifact_decisions_sha256": source_decisions_sha256,
        "source_artifact_bundles_sha256": source_bundles_sha256,
        "scheduler_policy": "14-call structural priority with exact semantic-package dedup",
        "activation_cue_policy": "pending_context_and_cold_recoverable_require_executable_replayable_cues",
        "coverage_policy": "union_source_messages_of_selected_representatives",
        "time_policy": "time_and_same_segment_are_zero_weight",
        "provider_calls": 0,
        "encoded_bundle_count": 0,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": code_sha256,
    }
    aggregate = {
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "schema_version": SCHEDULER_SCHEMA_VERSION,
        "analysis_run_id": manifest["analysis_run_id"],
        "candidate_count": metrics["candidate_count"],
        "decision_count": metrics["decision_count"],
        "unique_semantic_package_count": metrics["unique_semantic_package_count"],
        "selected_count": metrics["selected_count"],
        "provider_calls": 0,
        "encoded_bundle_count": 0,
        "pending_source_count": metrics["pending_source_count"],
        "cold_recoverable_count": metrics["cold_recoverable_count"],
        "activation_cue_coverage_rate": metrics["activation_cue_coverage_rate"],
        "estimated_message_coverage_count": metrics["estimated_message_coverage_count"],
        "estimated_message_coverage_ratio": metrics["estimated_message_coverage_ratio"],
        "compression_ratio": metrics["compression_ratio"],
        "time_signal_weight": 0.0,
        "same_segment_signal_weight": 0.0,
        "dry_run": True,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": code_sha256,
    }
    coverage = {
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "analysis_run_id": manifest["analysis_run_id"],
        "candidate_count": metrics["candidate_count"],
        "unique_semantic_package_count": metrics["unique_semantic_package_count"],
        "selected_count": metrics["selected_count"],
        "selected_package_ids": [item["semantic_package_id"] for item in scheduler.selected],
        "estimated_message_coverage_count": metrics["estimated_message_coverage_count"],
        "estimated_message_coverage_ratio": metrics["estimated_message_coverage_ratio"],
        "selected_chat_count": metrics["selected_chat_count"],
        "selected_window_scale_count": metrics["selected_window_scale_count"],
        "compression_ratio": metrics["compression_ratio"],
        "package_compression_ratio": metrics["package_compression_ratio"],
        "provider_calls": 0,
        "encoded_bundle_count": 0,
        "body_free": True,
        "frozen_read": False,
        "gold_loaded": False,
    }
    for label, value in (
        ("scheduler manifest", manifest),
        ("scheduler provenance", provenance),
        ("scheduler aggregate", aggregate),
        ("scheduler coverage", coverage),
    ):
        _assert_body_free(value, label=label)
    output_root.mkdir(parents=True, exist_ok=False)
    paths: Dict[str, str] = {}
    _write_json(output_root / SCHEDULER_OUTPUT_FILES["manifest"], manifest)
    paths["manifest"] = str(output_root / SCHEDULER_OUTPUT_FILES["manifest"])
    _write_json(output_root / SCHEDULER_OUTPUT_FILES["provenance"], provenance)
    paths["provenance"] = str(output_root / SCHEDULER_OUTPUT_FILES["provenance"])
    _write_json(output_root / SCHEDULER_OUTPUT_FILES["aggregate"], aggregate)
    paths["aggregate"] = str(output_root / SCHEDULER_OUTPUT_FILES["aggregate"])
    _write_json(output_root / SCHEDULER_OUTPUT_FILES["coverage"], coverage)
    paths["coverage"] = str(output_root / SCHEDULER_OUTPUT_FILES["coverage"])
    _write_jsonl(output_root / SCHEDULER_OUTPUT_FILES["decisions"], scheduler.decisions)
    paths["decisions"] = str(output_root / SCHEDULER_OUTPUT_FILES["decisions"])
    _assert_body_free([dict(item) for item in scheduler.decisions], label="scheduler decisions")
    return SchedulerArtifact(
        output_directory=str(output_root),
        artifact_paths=paths,
        manifest=manifest,
        provenance=provenance,
        aggregate=aggregate,
        run=scheduler,
    )


__all__ = [
    "SCHEDULER_ARTIFACT_VERSION",
    "SCHEDULER_SCHEMA_VERSION",
    "SCHEDULER_PROVENANCE_VERSION",
    "SCHEDULER_RULESET_VERSION",
    "SCHEDULER_OUTPUT_FILES",
    "DEFAULT_PROVIDER_CALL_BUDGET",
    "SchedulerRun",
    "SchedulerArtifact",
    "schedule_bundle_candidates",
    "run_development_scheduler_v29",
]
