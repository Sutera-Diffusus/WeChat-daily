"""Public, provider-free active-period reconstruction seam.

The older blind-round runner owns the SQLite-specific two-phase source.  This
module exposes the smaller in-memory contract used by development audits:
metadata chooses complete chat/time periods, then the same periods are
materialised, reconstructed, and rendered as one card each.  The stages keep
ordered message references so a validator can fail closed on any drift.

Selection never reads message bodies, topic hints, values, or model output.
An explicit reply/quote may keep a period together across a long gap, but the
gap remains a retrieval candidate and never becomes a semantic resolution.
"""

from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .context_reconstruction import reconstruct_context
from .context_reconstruction_blind_runner import (
    DEFAULT_ACTIVE_PERIOD_GAP_SECONDS,
    DEFAULT_EXCLUDED_DATES,
    DEFAULT_PREFERRED_DATES,
    MetadataMessage,
    MetadataWindow,
    _coerce_metadata_message,
    _event_timestamp_epoch,
    build_metadata_windows,
    select_representative_windows,
)


MODULE_VERSION = "active_period_runner_v1"
DEFAULT_MAX_PERIODS = 3

_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "text",
        "message_text",
        "message_content",
        "raw_text",
        "raw_message",
        "prompt",
        "completion",
        "provider_response",
        "model_response",
        "reasoning",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _opaque(prefix: str, value: Any, length: int = 18) -> str:
    return f"{prefix}-{_sha256(value)[:length]}"


def _sequence(value: Any) -> List[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    if isinstance(value, Mapping):
        return list(value.keys())
    return []


def _row_ref(row: Mapping[str, Any], index: int = 0) -> str:
    for key in ("message_ref", "canonical_message_ref", "blind_message_ref", "message_id", "id"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return str(value)
    return f"metadata-row-{index + 1:06d}"


def _source_id(row: Mapping[str, Any], fallback: str) -> str:
    for key in ("source_message_id", "message_id", "id", "record_id"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return str(value)
    return fallback


def _body_free(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _body_free(child)
            for key, child in value.items()
            if str(key).casefold() not in _BODY_KEYS
        }
    if isinstance(value, list):
        return [_body_free(child) for child in value]
    if isinstance(value, tuple):
        return [_body_free(child) for child in value]
    if isinstance(value, set):
        return [_body_free(child) for child in sorted(value, key=str)]
    return value


def _period_status(window: MetadataWindow, used_messages: int, cap: Optional[int]) -> Tuple[str, int]:
    if cap is None:
        return "selected", used_messages + window.message_count
    if window.message_count > cap or used_messages + window.message_count > cap:
        return "deferred", used_messages
    return "selected", used_messages + window.message_count


def _metadata_rows(rows: Sequence[Mapping[str, Any]]) -> Tuple[List[MetadataMessage], Dict[str, Dict[str, Any]], List[str]]:
    metadata: List[MetadataMessage] = []
    raw_by_ref: Dict[str, Dict[str, Any]] = {}
    duplicate_refs: List[str] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        ref = _row_ref(row, index)
        if ref in raw_by_ref:
            duplicate_refs.append(ref)
            continue
        row["message_ref"] = ref
        raw_by_ref[ref] = row
        item = _coerce_metadata_message(row, index)
        if item is not None:
            metadata.append(item)
    return metadata, raw_by_ref, sorted(set(duplicate_refs))


def _selected_body_rows(
    windows: Sequence[MetadataWindow],
    raw_by_ref: Mapping[str, Mapping[str, Any]],
    *,
    include_bodies: bool,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    refs = [str(row.message_ref) for window in windows for row in window.rows]
    materialized: List[Dict[str, Any]] = []
    actual_refs: List[str] = []
    for ref in refs:
        row = raw_by_ref.get(ref)
        if row is None:
            continue
        value = copy.deepcopy(dict(row))
        value["message_ref"] = ref
        value.setdefault("message_id", _source_id(value, ref))
        value["blind_selection_locked"] = True
        if not include_bodies:
            value = _body_free(value)
        materialized.append(value)
        actual_refs.append(ref)
    return materialized, actual_refs


def _explicit_group_value(row: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return str(value)
    return ""


def _source_ids_for_refs(refs: Sequence[str], raw_by_ref: Mapping[str, Mapping[str, Any]]) -> List[str]:
    return [_source_id(raw_by_ref.get(ref, {}), ref) for ref in refs]


def _gap_candidates(window: MetadataWindow, raw_by_ref: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    ordered = list(window.rows)
    result: List[Dict[str, Any]] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_epoch = _event_timestamp_epoch(previous.timestamp, previous.timestamp_epoch)
        current_epoch = _event_timestamp_epoch(current.timestamp, current.timestamp_epoch)
        if previous_epoch is None or current_epoch is None:
            continue
        gap = current_epoch - previous_epoch
        if gap <= float(window.active_period_gap_seconds or 0.0):
            continue
        current_raw = raw_by_ref.get(str(current.message_ref), {})
        targets = {
            str(current_raw.get(key))
            for key in (
                "reply_to_message_id",
                "reply_to",
                "quoted_message_id",
                "referenced_message_id",
                "in_reply_to",
            )
            if current_raw.get(key) not in (None, "")
        }
        quote_values = _sequence(
            current_raw.get("quote_refs")
            or current_raw.get("quote_message_ids")
            or current_raw.get("quoted_message_ids")
        )
        targets.update(str(value) for value in quote_values if value not in (None, ""))
        prior_identifiers = {str(row.message_ref) for row in ordered[: ordered.index(current)]}
        prior_identifiers.update(
            str(row.source_message_id)
            for row in ordered[: ordered.index(current)]
            if row.source_message_id
        )
        result.append({
            "kind": "gap",
            "from_message_ref": str(previous.message_ref),
            "to_message_ref": str(current.message_ref),
            "gap_seconds": round(gap, 3),
            "bridge": "explicit_reply_or_quote" if targets & prior_identifiers else "none",
            "candidate_only": True,
            "semantic_boundary_inferred": False,
            "semantic_status": "candidate_only",
            "uncertainties": ["retrieval_gap_is_not_semantic_resolution"],
        })
    return result


def _internal_episode_candidates(
    window: MetadataWindow,
    reconstruction: Mapping[str, Any],
    raw_by_ref: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    refs = [str(row.message_ref) for row in window.rows]
    explicit: Dict[str, List[str]] = {}
    explicit_order: List[str] = []
    for ref in refs:
        raw = raw_by_ref.get(ref, {})
        value = _explicit_group_value(
            raw,
            ("internal_episode_id", "conversation_episode_id", "episode_candidate_id"),
        )
        if not value:
            explicit = {}
            explicit_order = []
            break
        if value not in explicit:
            explicit[value] = []
            explicit_order.append(value)
        explicit[value].append(ref)

    if explicit:
        return [
            {
                "episode_ref": value,
                "episode_id": value,
                "message_refs": list(explicit[value]),
                "message_ids": _source_ids_for_refs(explicit[value], raw_by_ref),
                "candidate_only": True,
                "semantic_status": "candidate_only",
                "source": "explicit_metadata_candidate",
                "uncertainties": ["metadata_episode_id_is_not_semantic_truth"],
            }
            for value in explicit_order
        ]

    candidates: List[Dict[str, Any]] = []
    ref_set = set(refs)
    for episode in reconstruction.get("episodes") or ():
        if not isinstance(episode, Mapping):
            continue
        episode_refs = [str(ref) for ref in episode.get("message_refs") or () if str(ref) in ref_set]
        if not episode_refs:
            continue
        value = dict(episode)
        value["message_refs"] = episode_refs
        value["message_ids"] = _source_ids_for_refs(episode_refs, raw_by_ref)
        candidates.append(value)
    return candidates


def _parallel_candidates(
    window: MetadataWindow,
    internal_episodes: Sequence[Mapping[str, Any]],
    raw_by_ref: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    refs = [str(row.message_ref) for row in window.rows]
    explicit: Dict[str, List[str]] = {}
    order: List[str] = []
    for ref in refs:
        value = _explicit_group_value(raw_by_ref.get(ref, {}), ("strand_id", "strand_ref", "topic_strand_id"))
        if not value:
            explicit = {}
            order = []
            break
        if value not in explicit:
            explicit[value] = []
            order.append(value)
        explicit[value].append(ref)
    if explicit and len(explicit) > 1:
        return [
            {
                "strand_ref": value,
                "message_refs": list(explicit[value]),
                "message_ids": _source_ids_for_refs(explicit[value], raw_by_ref),
                "candidate_only": True,
                "semantic_status": "candidate_only",
                "reason": "explicit_parallel_strand_metadata",
            }
            for value in order
        ]
    if len(internal_episodes) <= 1:
        return []
    return [
        {
            "strand_ref": _opaque("parallel-strand", (window.window_ref, tuple(episode.get("message_refs") or ())), 16),
            "message_refs": list(episode.get("message_refs") or ()),
            "message_ids": list(episode.get("message_ids") or ()),
            "candidate_only": True,
            "semantic_status": "candidate_only",
            "reason": "internal_episode_parallel_candidate",
        }
        for episode in internal_episodes
    ]


def _period_row(
    window: MetadataWindow,
    status: str,
    reconstruction: Mapping[str, Any],
    raw_by_ref: Mapping[str, Mapping[str, Any]],
    *,
    include_bodies: bool,
) -> Dict[str, Any]:
    refs = [str(row.message_ref) for row in window.rows]
    internal_episodes = _internal_episode_candidates(window, reconstruction, raw_by_ref)
    parallel = _parallel_candidates(window, internal_episodes, raw_by_ref)
    source_rows = [copy.deepcopy(dict(raw_by_ref[ref])) for ref in refs if ref in raw_by_ref]
    if include_bodies:
        materialized_rows: Any = source_rows
    else:
        materialized_rows = _body_free(source_rows)
    gaps = _gap_candidates(window, raw_by_ref)
    return {
        "period_ref": window.window_ref,
        "active_period_ref": window.window_ref,
        "period_id": window.window_ref,
        "chat_ref": window.chat_ref,
        "chat_type": window.chat_type,
        "local_day": window.local_day,
        "observed_days": list(window.observed_days or (window.local_day,)),
        "period_start_timestamp": window.start_timestamp,
        "period_end_timestamp": window.end_timestamp,
        "source_sequence_start": window.source_sequence_start,
        "source_sequence_end": window.source_sequence_end,
        "message_refs": refs,
        "timeline_refs": refs,
        "message_ids": _source_ids_for_refs(refs, raw_by_ref),
        "message_count": len(refs),
        "messages": materialized_rows,
        "source_messages": materialized_rows,
        "internal_episodes": internal_episodes,
        "episodes": internal_episodes,
        "internal_episode_refs": [str(item.get("episode_ref")) for item in internal_episodes],
        "parallel_strands": parallel,
        "strands": parallel,
        "retrieval_candidates": gaps,
        "gap_candidates": gaps,
        "period_status": status,
        "status": status,
        "candidate_only": True,
        "semantic_status": "candidate_only",
        "semantic_resolution": "unknown",
        "state": "unknown",
        "whole_period": True,
        "period_complete_required": True,
        "calendar_day_forced_closure": False,
        "gap_is_retrieval_only": True,
        "uncertainties": [
            "active_period_is_retrieval_candidate",
            "period_card_is_not_final_topic_or_event",
        ],
    }


def _period_card(row: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(row))
    value["card_ref"] = _opaque("active-card", value.get("period_ref"), 18)
    value["unit_ref"] = value["card_ref"]
    value["unit_id"] = value["card_ref"]
    value["rendered"] = True
    value["one_period_one_review_card"] = True
    value["internal_episodes_not_review_cards"] = True
    value["review_status"] = "candidate_only"
    return value


def _metric(refs: Sequence[str], observed: Iterable[str], reason: str) -> Dict[str, Any]:
    values = sorted({str(value) for value in observed if value not in (None, "")})
    return {
        "count": len(values),
        "refs": values,
        "status": "observed_candidate" if values else "not_measured",
        "measurement_status": "observed_candidate" if values else "not_measured",
        "candidate_only": True,
        "reason": "candidate evidence observed" if values else reason,
        "empty_collection_pass": False,
    }


def _build_metrics(rows: Sequence[Mapping[str, Any]], selected_refs: Sequence[str]) -> Dict[str, Any]:
    selected = set(str(ref) for ref in selected_refs)
    context_refs: List[str] = []
    evidence_refs: List[str] = []
    for row in rows:
        for key, target in (("context_refs", context_refs), ("evidence_refs", evidence_refs)):
            for value in _sequence(row.get(key)):
                if str(value) in selected:
                    target.append(str(value))
    return {
        "context": _metric(selected_refs, context_refs, "no authoritative context refs were supplied"),
        "evidence": _metric(selected_refs, evidence_refs, "no authoritative evidence records were supplied"),
    }


def _validate_options(max_periods: Any, whole_period_cap: Any) -> Tuple[int, Optional[int]]:
    try:
        period_limit = int(max_periods)
    except (TypeError, ValueError):
        raise ValueError("max_periods_must_be_positive") from None
    if period_limit < 1:
        raise ValueError("max_periods_must_be_positive")
    if whole_period_cap is None:
        return period_limit, None
    try:
        cap = int(whole_period_cap)
    except (TypeError, ValueError):
        raise ValueError("whole_period_cap_must_be_positive") from None
    if cap < 1:
        raise ValueError("whole_period_cap_must_be_positive")
    return period_limit, cap


def run_active_periods(
    messages: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    max_periods: int = DEFAULT_MAX_PERIODS,
    whole_period_cap: Optional[int] = None,
    source: Any = "synthetic",
    include_bodies: bool = False,
    provider: Any = None,
    production_blocked: bool = True,
    preferred_dates: Sequence[str] = DEFAULT_PREFERRED_DATES,
    excluded_dates: Sequence[str] = DEFAULT_EXCLUDED_DATES,
    active_period_gap_seconds: float = DEFAULT_ACTIVE_PERIOD_GAP_SECONDS,
) -> Dict[str, Any]:
    """Run the public in-memory active-period candidate pipeline.

    ``provider`` and ``production_blocked`` are accepted as compatibility
    knobs at this seam, but the implementation is fail-closed: it never calls
    a provider and always emits ``production_blocked=true``.
    """

    period_limit, cap = _validate_options(max_periods, whole_period_cap)
    if provider is not None:
        raise ValueError("active_period_runner_is_provider_free")
    if isinstance(messages, Mapping):
        raw_values = messages.get("messages") or messages.get("rows") or messages.get("items") or ()
    else:
        raw_values = messages
    rows = [dict(row) for row in raw_values if isinstance(row, Mapping)]
    metadata, raw_by_ref, duplicate_refs = _metadata_rows(rows)
    windows = build_metadata_windows(metadata, active_period_gap_seconds=active_period_gap_seconds)
    selected = select_representative_windows(
        windows,
        preferred_dates=preferred_dates,
        excluded_dates=excluded_dates,
        max_cards=period_limit,
        # Whole-period cap is a status/defer signal for this public contract;
        # applying it to selection would hide the complete deferred period.
        cost_cap_messages=None,
    )
    selected_refs = [str(row.message_ref) for window in selected for row in window.rows]
    selected_ref_set = set(selected_refs)
    materialized_rows, materialized_refs = _selected_body_rows(
        selected,
        raw_by_ref,
        include_bodies=True,
    )
    # Body-bearing rows are needed internally even when the public projection
    # is body-free.  They are never included in the manifest in either mode.
    reference_day = max(
        (
            str(day)
            for window in selected
            for day in (window.observed_days or (window.local_day,))
            if day and day != "unknown"
        ),
        default=None,
    )
    reconstructed_all = reconstruct_context(
        materialized_rows,
        reference_date=reference_day,
        include_bodies=True,
        source_scope="development",
    )
    reconstruction_messages = {
        str(row.get("message_ref")): row
        for row in reconstructed_all.get("messages") or ()
        if isinstance(row, Mapping) and row.get("message_ref")
    }
    # A duplicate/omitted source ref is a hard structural issue, but preserve
    # the rows we did receive so the caller can inspect the blocked result.
    used_messages = 0
    selection_rows: List[Dict[str, Any]] = []
    materialized_periods: List[Dict[str, Any]] = []
    reconstructed_periods: List[Dict[str, Any]] = []
    rendered_cards: List[Dict[str, Any]] = []
    for window in selected:
        status, used_messages = _period_status(window, used_messages, cap)
        selection_row = window.body_free()
        selection_row.update({
            "period_status": status,
            "status": status,
            "candidate_only": True,
            "whole_period": True,
        })
        selection_rows.append(selection_row)
        refs = [str(row.message_ref) for row in window.rows]
        period_messages = [copy.deepcopy(raw_by_ref[ref]) for ref in refs if ref in raw_by_ref]
        if not include_bodies:
            period_messages = _body_free(period_messages)
        materialized_period = {
            "period_ref": window.window_ref,
            "active_period_ref": window.window_ref,
            "chat_ref": window.chat_ref,
            "chat_type": window.chat_type,
            "observed_days": list(window.observed_days or (window.local_day,)),
            "message_refs": refs,
            "timeline_refs": refs,
            "message_ids": _source_ids_for_refs(refs, raw_by_ref),
            "messages": period_messages,
            "source_messages": period_messages,
            "period_status": status,
            "status": status,
            "candidate_only": True,
            "semantic_status": "candidate_only",
            "whole_period": True,
            "period_complete_required": True,
        }
        materialized_periods.append(materialized_period)
        period_reconstruction = _period_row(
            window,
            status,
            reconstructed_all,
            raw_by_ref,
            include_bodies=include_bodies,
        )
        # Preserve the public reconstruction message order in a diagnostic
        # field, but keep period refs authoritative and card-major.
        period_reconstruction["reconstruction_message_refs"] = [
            str(ref) for ref in reconstruction_messages if str(ref) in set(refs)
        ]
        reconstructed_periods.append(period_reconstruction)
        rendered_cards.append(_period_card(period_reconstruction))

    expected_groups = [list(row.get("message_refs") or ()) for row in selection_rows]
    metadata_projection = [
        {
            "message_ref": row.message_ref,
            "source_message_id": row.source_message_id,
            "chat_ref": row.chat_ref,
            "chat_type": row.chat_type,
            "local_day": row.local_day,
            "timestamp": row.timestamp,
            "timestamp_epoch": row.timestamp_epoch,
            "sequence": row.sequence,
            "participant_ref": row.participant_ref,
            "message_type": row.message_type,
            "media": row.media,
            "reply_to_message_id": row.reply_to_message_id,
            "quote_message_ids": list(row.quote_message_ids),
        }
        for row in metadata
        if row.message_ref in selected_ref_set
    ]
    selection_fingerprint = _sha256({
        "module_version": MODULE_VERSION,
        "gap_seconds": float(active_period_gap_seconds),
        "metadata": metadata_projection,
        "expected_period_groups": expected_groups,
    })
    manifest: Dict[str, Any] = {
        "schema_version": MODULE_VERSION,
        "pipeline_version": MODULE_VERSION,
        "phase": "metadata_locked_period_reconstruction",
        "status": "candidate_only",
        "source": str(source) if source not in (None, "") else "unknown",
        "provider_calls": 0,
        "provider_used": False,
        "production_blocked": True,
        "production_connected": False,
        "frozen_read": False,
        "gold_read": False,
        "stage_b": False,
        "stage_c": False,
        "body_free": True,
        "body_fields_read_during_selection": False,
        "selection_locked_before_body_materialize": True,
        "selection_phase": "metadata_only",
        "materialization_phase": "selected_complete_periods_only",
        "active_period_gap_seconds": float(active_period_gap_seconds),
        "gap_semantics": "retrieval_candidate_only_not_semantic_boundary",
        "calendar_day_forced_closure": False,
        "fixed_message_count_partition": False,
        "tail_messages_dropped": False,
        "requested_period_count": period_limit,
        "available_period_count": len(windows),
        "selected_period_count": len(selected),
        "period_count": len(selected),
        "selected_message_count": len(selected_refs),
        "materialized_message_count": len(materialized_refs),
        "expected_refs": list(selected_refs),
        "expected_period_groups": expected_groups,
        "selection_hash": selection_fingerprint,
        "input_fingerprint": _sha256(metadata_projection),
        "duplicate_input_refs": duplicate_refs,
        "reference_integrity_status": "passed" if selected_refs == materialized_refs else "blocked",
        "release_gate": {
            "status": "blocked",
            "reason": "development_structural_pilot_only",
            "production_blocked": True,
        },
        "coverage": {
            "period_count": len(selected),
            "requested_period_count": period_limit,
            "direct_period_count": sum(1 for window in selected if window.chat_type == "direct"),
            "group_period_count": sum(1 for window in selected if window.chat_type == "group"),
            "observed_date_count": len({day for window in selected for day in (window.observed_days or (window.local_day,)) if day and day != "unknown"}),
            "complete_period_selection": all(row.get("whole_period") is True for row in selection_rows),
        },
    }
    manifest["body_free"] = True
    metrics = _build_metrics(rows, selected_refs)
    phase_trace = [
        {"phase": "metadata_only_scan", "body_fields_selected": False, "completed": True, "record_count": len(metadata), "period_count": len(windows)},
        {"phase": "selection_lock", "body_fields_selected": False, "completed": True, "selection_hash": selection_fingerprint},
        {"phase": "whole_period_materialize", "body_fields_selected": True, "completed": True, "period_count": len(selected), "message_count": len(materialized_refs)},
        {"phase": "provider_free_reconstruction", "body_fields_selected": False, "completed": True, "period_count": len(reconstructed_periods)},
        {"phase": "one_period_one_card_render", "body_fields_selected": True, "completed": True, "card_count": len(rendered_cards)},
    ]
    result: Dict[str, Any] = {
        "module_version": MODULE_VERSION,
        "phase": "candidate_artifact_generated",
        "source": str(source) if source not in (None, "") else "unknown",
        "manifest": manifest,
        "run_manifest": manifest,
        "selection": selection_rows,
        "materialized": materialized_periods,
        "reconstructed": reconstructed_periods,
        "rendered": rendered_cards,
        "stages": {
            "selection": selection_rows,
            "materialized": materialized_periods,
            "reconstructed": reconstructed_periods,
            "rendered": rendered_cards,
        },
        "metrics": metrics,
        "context": metrics["context"],
        "evidence": metrics["evidence"],
        "phase_trace": phase_trace,
        "expected_refs": list(selected_refs),
        "expected_period_groups": expected_groups,
        "provider_calls": 0,
        "provider_used": False,
        "production_blocked": True,
        "production_connected": False,
        "frozen_read": False,
        "gold_read": False,
        "stage_b": False,
        "stage_c": False,
        "candidate_only": True,
        "body_free": not include_bodies,
    }
    if include_bodies:
        result["source_messages"] = [copy.deepcopy(raw_by_ref[ref]) for ref in selected_refs if ref in raw_by_ref]
    else:
        result["source_messages"] = _body_free([copy.deepcopy(raw_by_ref[ref]) for ref in selected_refs if ref in raw_by_ref])
    return result


def _stage_groups(result: Mapping[str, Any], stage: str) -> List[List[str]]:
    values = result.get(stage)
    if isinstance(values, Mapping):
        values = values.get("rows") or values.get("periods") or values.get("cards") or values.get("items") or ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    groups: List[List[str]] = []
    for row in values:
        if isinstance(row, Mapping):
            refs = row.get("message_refs") or row.get("timeline_refs") or row.get("refs") or row.get("message_ids") or ()
        else:
            refs = row if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)) else ()
        if isinstance(refs, Mapping):
            refs = list(refs.keys())
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes, bytearray)):
            groups.append([str(ref) for ref in refs if ref not in (None, "")])
    return groups


def validate_active_period_result(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Fail closed when any public stage loses, adds, or reorders a ref."""

    if not isinstance(result, Mapping):
        return {
            "status": "blocked",
            "blocked": True,
            "ref_gate": {"status": "blocked", "reason": "result_not_mapping"},
            "release_gate": {"status": "blocked"},
        }
    manifest = result.get("manifest") if isinstance(result.get("manifest"), Mapping) else {}
    expected_groups = manifest.get("expected_period_groups") or result.get("expected_period_groups") or ()
    expected = [
        [str(ref) for ref in group if ref not in (None, "")]
        for group in expected_groups
        if isinstance(group, Sequence) and not isinstance(group, (str, bytes, bytearray))
    ]
    if not expected:
        flat = manifest.get("expected_refs") or result.get("expected_refs") or ()
        if isinstance(flat, Sequence) and not isinstance(flat, (str, bytes, bytearray)):
            expected = [[str(ref) for ref in flat if ref not in (None, "")]] if flat else []
    stage_reports: Dict[str, Any] = {}
    errors: List[str] = []
    for stage in ("selection", "materialized", "reconstructed", "rendered"):
        actual = _stage_groups(result, stage)
        exact = actual == expected
        stage_reports[stage] = {
            "status": "passed" if exact else "blocked",
            "exact_set_and_order": exact,
            "expected": expected,
            "actual": actual,
        }
        if not exact:
            errors.append(f"{stage}_reference_drift")
    duplicate_expected = sorted({ref for group in expected for ref in group if sum(ref in other for other in expected) > 1})
    if duplicate_expected:
        errors.append("expected_reference_repeated_across_periods")
    result_body_free = manifest.get("body_free") is True
    if manifest and not result_body_free:
        errors.append("manifest_not_body_free")
    passed = not errors
    return {
        "status": "passed" if passed else "blocked",
        "blocked": not passed,
        "errors": errors,
        "ref_gate": {
            "status": "passed" if passed else "blocked",
            "expected_period_count": len(expected),
            "stage_reports": stage_reports,
        },
        "validation": {
            "status": "passed" if passed else "blocked",
            "reference_integrity": stage_reports,
        },
        "release_gate": {
            "status": "blocked",
            "reason": "development_structural_pilot_only",
        },
        "production_blocked": True,
    }


__all__ = [
    "DEFAULT_MAX_PERIODS",
    "MODULE_VERSION",
    "run_active_periods",
    "validate_active_period_result",
]
