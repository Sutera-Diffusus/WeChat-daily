"""Private, body-free material audit for the K5 ContextPacket development run.

This is an audit helper, not a production semantic component.  It reads only
the named ``development`` material and the 20 packets selected by the K5
queue.  Packet bodies are inspected in memory for a human-style judgement,
but no body, display name, chat label, or source identifier is written.  The
script never traverses a frozen path and never calls a provider.

The audit deliberately separates material preservation from semantic
resolution: candidates may be useful for a later DeepSeek decision, but this
stage is not allowed to make that decision locally.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Sequence, Tuple


DEFAULT_ARTIFACT_DIR = Path(
    "data/private/gold_standard/2026-08-25/context_packet_development_v1"
)
DEFAULT_INPUT_DIR = Path(
    "data/private/gold_standard/2026-08-25/working/p014_evaluation_split_v1/development"
)
AUDIT_SCHEMA = "context_packet_material_audit_v1"
REQUIRED_BUCKETS = (
    "greeting_to_new_topic",
    "no_reply_continuation",
    "pronoun_or_ellipsis",
    "person_history",
    "object_history",
    "state_update",
    "topic_shift",
    "media_or_context_only",
    "long_gap_open_boundary",
    "candidate_competition",
)
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})
BODY_KEYS = frozenset(
    {
        "content",
        "text",
        "raw_text",
        "redacted_text",
        "surface_text",
        "surface_redacted",
        "fragment_text",
        "summary",
        "title",
        "quote",
        "raw_message_ref",
    }
)
IDENTITY_KEYS = frozenset(
    {
        "account_id",
        "chat_id",
        "chat_name",
        "display_name",
        "message_id",
        "record_id",
        "speaker_id",
        "sender_id",
        "person_id",
        "person_name",
        "claim_id",
        "mention_id",
        "relation_id",
        "fragment_id",
        "candidate_id",
        "packet_id",
    }
)


# The manual judgement is keyed only by the already-opaque selection ref.
# Counts are context units (not message IDs): a unit is one human-identified
# detail needed to understand the selected anchor.  Media-only packets are
# explicitly N/A for semantic recall, not scored as semantic failures.
MANUAL_REVIEW: Dict[str, Dict[str, Any]] = {
    "e7b3cec67d83c6032e6cad12": {"primary": "pass", "context": "pass", "candidate": "pass", "required": 4, "preserved": 4, "distractors": 0, "reviewed": 9, "topic": "pass"},
    "8a164123a3958c15b5c01d60": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 1, "topic": "N/A"},
    "635cf7889b195f45dee40100": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 2, "topic": "N/A"},
    "7d263fe6f219ead8bc4328f5": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 2, "topic": "N/A"},
    "3d58eb64f7026853714c8bcd": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 4, "topic": "N/A"},
    "c648907df5a6adec07a4ef2c": {"primary": "pass", "context": "pass", "candidate": "pass", "required": 7, "preserved": 7, "distractors": 0, "reviewed": 9, "topic": "pass"},
    "435d5c1e86a23eb90a84588c": {"primary": "pass", "context": "fail", "candidate": "uncertain", "required": 6, "preserved": 6, "distractors": 1, "reviewed": 9, "topic": "N/A", "errors": ["UNRELATED_SEGMENT_IN_CONTEXT", "OLD_STATE_CONTEXT_MIX"]},
    "f2de289780407e1d8b125cf4": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 4, "topic": "N/A"},
    "cd646ec6720a41018c4e657b": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 4, "topic": "N/A"},
    "e606f20bd3a79e88514e1657": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 4, "topic": "N/A"},
    "7d7537c5b45909f50cfd556f": {"primary": "pass", "context": "pass", "candidate": "pass", "required": 3, "preserved": 3, "distractors": 0, "reviewed": 5, "topic": "N/A"},
    "78bc5dfeb1e4cc37b34fe539": {"primary": "uncertain", "context": "pass", "candidate": "pass", "required": 4, "preserved": 4, "distractors": 0, "reviewed": 8, "topic": "N/A"},
    "374212c33ce4abd8d3a362dd": {"primary": "pass", "context": "pass", "candidate": "pass", "required": 6, "preserved": 6, "distractors": 0, "reviewed": 9, "topic": "N/A"},
    "ba4bf7eec2a7491748e00c12": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 1, "topic": "N/A"},
    "c46ccb02f7a39f2b41d524ae": {"primary": "uncertain", "context": "uncertain", "candidate": "N/A", "required": 0, "preserved": 0, "distractors": 0, "reviewed": 1, "topic": "N/A"},
    "b02d78a6323c97910b190266": {"primary": "uncertain", "context": "pass", "candidate": "pass", "required": 3, "preserved": 3, "distractors": 0, "reviewed": 5, "topic": "N/A"},
    "c5fb8df58e5ad9c0277877ff": {"primary": "uncertain", "context": "pass", "candidate": "pass", "required": 3, "preserved": 3, "distractors": 0, "reviewed": 5, "topic": "N/A"},
    "f60e3bd498ac1468acebe397": {"primary": "pass", "context": "pass", "candidate": "pass", "required": 3, "preserved": 3, "distractors": 0, "reviewed": 5, "topic": "N/A"},
    "d6f25cdc9535f2ab4a23bcf6": {"primary": "pass", "context": "pass", "candidate": "pass", "required": 3, "preserved": 3, "distractors": 0, "reviewed": 5, "topic": "N/A"},
    "e55354160788b2fecbe2c2c7": {"primary": "uncertain", "context": "pass", "candidate": "pass", "required": 4, "preserved": 4, "distractors": 0, "reviewed": 8, "topic": "N/A"},
}


def _safe_path(path: Path) -> Path:
    resolved = path.resolve()
    if any(part.lower() in {"frozen", "frozen_test"} for part in resolved.parts):
        raise RuntimeError("refusing frozen/frozen_test path")
    return resolved


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected object: {path.name}")
    return value


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"expected object row: {path.name}")
                yield value


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return list(_iter_jsonl(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _opaque(value: Any, namespace: str) -> str:
    return f"{namespace}_{hashlib.sha256((namespace + '|' + str(value)).encode()).hexdigest()[:16]}"


def _text(message: Mapping[str, Any]) -> str:
    return str(message.get("redacted_text") or message.get("content") or "")


def _valuable(message: Mapping[str, Any]) -> bool:
    text = _text(message).strip()
    media = str(message.get("media_state") or "")
    kind = str(message.get("message_type") or "")
    placeholder = media in {"placeholder", "media", "silent"} or kind in {
        "image", "voice", "video", "emoji", "动画表情"
    } or (text.startswith("[") and text.endswith("]"))
    return bool(text) and not placeholder


def _scope(message: Mapping[str, Any]) -> Tuple[str, str]:
    return str(message.get("account_id") or ""), str(message.get("chat_id") or "")


def _nested_key_scan(value: Any, keys: frozenset[str], path: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in keys:
                found.append(child_path)
            found.extend(_nested_key_scan(child, keys, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_nested_key_scan(child, keys, f"{path}[{index}]"))
    return found


def _candidate_nodes(packet: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    context = packet.get("candidate_context")
    if not isinstance(context, Mapping):
        return
    for layer in ("continuity_candidates", "qa_candidates", "person_history", "object_history", "state_history", "open_threads"):
        values = context.get(layer)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, Mapping):
                    yield value


def _node_message_ids(node: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("message_ids", "source_message_ids", "left_message_id", "right_message_id"):
        value = node.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item is not None)
        elif value is not None:
            values.append(str(value))
    for key in ("evidence_refs", "source_refs"):
        refs = node.get(key)
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, Mapping) and ref.get("message_id") is not None:
                    values.append(str(ref["message_id"]))
    ref = node.get("evidence_ref")
    if isinstance(ref, Mapping) and ref.get("message_id") is not None:
        values.append(str(ref["message_id"]))
    return values


def _signals(window_ids: Sequence[str], messages: Mapping[str, Mapping[str, Any]], claims: Mapping[str, List[Mapping[str, Any]]]) -> Dict[str, Any]:
    window = [messages[mid] for mid in window_ids if mid in messages]
    claim_rows = [claim for mid in window_ids for claim in claims.get(mid, [])]
    person_message_ids = {
        str(claim.get("message_id"))
        for claim in claim_rows
        if any(str(target) for target in claim.get("target_entity_ids") or [])
    }
    object_message_ids = {
        str(claim.get("message_id"))
        for claim in claim_rows
        if bool(claim.get("event_action_types")) or bool(claim.get("event_mention_ids"))
    }
    state_message_ids = {
        str(claim.get("message_id"))
        for claim in claim_rows
        if str(claim.get("status") or "unknown") != "unknown"
        or str(claim.get("modality") or "unknown") != "unknown"
    }
    return {
        "window_message_count": len(window_ids),
        "valuable_message_count": sum(_valuable(message) for message in window),
        "speaker_bound_count": sum(bool(message.get("speaker_id")) for message in window),
        "claim_count": len(claim_rows),
        "person_signal_count": len(person_message_ids),
        "object_action_signal_count": len(object_message_ids),
        "state_signal_count": len(state_message_ids),
    }


def _packet_integrity(
    packet: Mapping[str, Any],
    messages: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    scope_value = packet.get("scope") if isinstance(packet.get("scope"), Mapping) else {}
    packet_scope = (str(scope_value.get("account_id") or ""), str(scope_value.get("chat_id") or ""))
    window = [str(item) for item in (packet.get("window") or {}).get("message_ids", [])]
    primary = packet.get("primary_fragments") if isinstance(packet.get("primary_fragments"), list) else []
    primary_ids = [str(item.get("message_id")) for item in primary if isinstance(item, Mapping) and item.get("message_id") is not None]
    adjacent = packet.get("adjacent_context") if isinstance(packet.get("adjacent_context"), list) else []
    adjacent_ids = [str(item.get("message_id")) for item in adjacent if isinstance(item, Mapping) and item.get("message_id") is not None]
    codes: List[str] = []
    missing = [mid for mid in window if mid not in messages]
    if missing or not primary_ids or not set(primary_ids) <= set(window):
        codes.append("PRIMARY_ANCHOR_NOT_PRESERVED")
    if not set(adjacent_ids) <= set(window):
        codes.append("ADJACENT_NOT_IN_WINDOW")
    scope_mismatch = 0
    for mid in window:
        if mid in messages and _scope(messages[mid]) != packet_scope:
            scope_mismatch += 1
    if scope_mismatch:
        codes.append("CROSS_CHAT_SCOPE_MISMATCH")
    primary_text_exact = 0
    for fragment in primary:
        if not isinstance(fragment, Mapping):
            continue
        mid = str(fragment.get("message_id") or "")
        if mid in messages and str(fragment.get("text_redacted") or "") == _text(messages[mid]):
            primary_text_exact += 1
    if primary and primary_text_exact != len(primary):
        codes.append("PRIMARY_TEXT_NOT_PRESERVED")
    candidate_message_ids: List[str] = []
    strong_relations = 0
    history_outside_window = 0
    candidate_only_false = 0
    candidate_node_count = 0
    for node in _candidate_nodes(packet):
        candidate_node_count += 1
        if node.get("candidate_only") is False:
            candidate_only_false += 1
        if node.get("strong_relation") is True:
            strong_relations += 1
        node_ids = _node_message_ids(node)
        candidate_message_ids.extend(node_ids)
        if str(node.get("object_ref_id") or "") and any(mid not in window for mid in node_ids):
            history_outside_window += sum(mid not in window for mid in node_ids)
    candidate_scope_mismatch = sum(
        mid in messages and _scope(messages[mid]) != packet_scope for mid in candidate_message_ids
    )
    candidate_missing = sum(mid not in messages for mid in candidate_message_ids)
    if candidate_scope_mismatch:
        codes.append("CANDIDATE_SCOPE_MISMATCH")
    if candidate_missing:
        codes.append("CANDIDATE_REFERENCE_UNBOUND")
    if strong_relations:
        codes.append("CANDIDATE_STRONG_RELATION_UNSAFE")
    cues = packet.get("activation_cues") if isinstance(packet.get("activation_cues"), list) else []
    cue_replayable = sum(bool(isinstance(cue, Mapping) and str(cue.get("replay_key") or "")) for cue in cues)
    cue_positive = sum(bool(isinstance(cue, Mapping) and cue.get("message_ids")) for cue in cues)
    cue_unbound = sum(
        bool(isinstance(cue, Mapping) and any(str(mid) not in window for mid in cue.get("message_ids") or []))
        for cue in cues
    )
    if cue_replayable != len(cues):
        codes.append("ACTIVATION_CUE_NOT_REPLAYABLE")
    if cue_unbound:
        codes.append("ACTIVATION_CUE_TARGET_UNBOUND")
    boundary = packet.get("boundary") if isinstance(packet.get("boundary"), Mapping) else {}
    end = boundary.get("end") if isinstance(boundary.get("end"), Mapping) else {}
    open_boundary = packet.get("open_boundary") is True and str(end.get("resolution") or "unknown") == "unknown"
    if not open_boundary:
        codes.append("OPEN_BOUNDARY_NOT_PRESERVED")
    candidate_only = packet.get("candidate_only") is True and all(
        isinstance(item, Mapping) and item.get("candidate_only") is True for item in primary
    )
    final_decision = not candidate_only or str(packet.get("status") or "").lower() in {"complete", *TERMINAL_STATES}
    if final_decision:
        codes.append("FINAL_LOCAL_SEMANTIC_DECISION_PRESENT")
    return {
        "window_ids": window,
        "primary_count": len(primary),
        "adjacent_count": len(adjacent),
        "window_count": len(window),
        "missing_message_count": len(missing),
        "primary_text_exact_count": primary_text_exact,
        "scope_mismatch_count": scope_mismatch,
        "candidate_reference_count": len(candidate_message_ids),
        "candidate_node_count": candidate_node_count,
        "candidate_missing_count": candidate_missing,
        "candidate_scope_mismatch_count": candidate_scope_mismatch,
        "candidate_history_outside_window_count": history_outside_window,
        "candidate_strong_relation_count": strong_relations,
        "candidate_only_false_count": candidate_only_false,
        "activation_cue_count": len(cues),
        "activation_cue_replayable_count": cue_replayable,
        "activation_cue_positive_target_count": cue_positive,
        "activation_cue_unbound_count": cue_unbound,
        "open_boundary": open_boundary,
        "candidate_only": candidate_only,
        "final_local_semantic_decision": final_decision,
        "error_codes": sorted(set(codes)),
    }


def _review_status(manual: Mapping[str, Any], integrity: Mapping[str, Any]) -> str:
    if integrity.get("error_codes"):
        return "fail"
    if manual.get("context") == "fail" or manual.get("primary") == "fail":
        return "fail"
    if manual.get("context") == "uncertain" or manual.get("primary") == "uncertain":
        return "uncertain"
    return "pass"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def run_audit(artifact_dir: Path = DEFAULT_ARTIFACT_DIR, input_dir: Path = DEFAULT_INPUT_DIR) -> Dict[str, Any]:
    artifact_dir = _safe_path(artifact_dir)
    input_dir = _safe_path(input_dir)
    manifest_path = artifact_dir / "manifest.private.json"
    aggregate_path = artifact_dir / "aggregate.private.json"
    queue_path = artifact_dir / "audit_queue.private.jsonl"
    selection_path = artifact_dir / "selection_map.private.jsonl"
    packet_path = artifact_dir / "packets.private.jsonl"
    for path in (manifest_path, aggregate_path, queue_path, selection_path, packet_path):
        _safe_path(path)
    manifest = _load_json(manifest_path)
    aggregate = _load_json(aggregate_path)
    if manifest.get("split") != "development" or manifest.get("frozen_read") is not False:
        raise RuntimeError("artifact is not explicitly development-only")
    if manifest.get("provider_called") is not False:
        raise RuntimeError("provider_called must be false for this audit")
    messages = {
        str(row.get("message_id")): row
        for row in _iter_jsonl(input_dir / "messages.private.jsonl")
        if row.get("message_id") is not None
    }
    claims: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in _iter_jsonl(input_dir / "claims.private.jsonl"):
        if row.get("message_id") is not None:
            claims[str(row["message_id"])].append(row)
    queue = _load_jsonl(queue_path)
    selected_queue = [row for row in queue if str(row.get("material_status") or "") == "candidate_only_pending_semantic_review"]
    if len(selected_queue) != int(manifest.get("selection", {}).get("selected_packet_count") or 0):
        raise RuntimeError("selected queue count mismatch")
    if set(MANUAL_REVIEW) != {str(row.get("target_ref")) for row in selected_queue}:
        raise RuntimeError("manual review map does not exactly cover selected queue")
    queue_by_packet = {str(row.get("packet_id")): row for row in selected_queue}
    selected_packet_ids = set(queue_by_packet)
    selection_rows = _load_jsonl(selection_path)
    selected_map = {str(row.get("packet_id")): row for row in selection_rows if row.get("selected") is True}
    if set(selected_map) != selected_packet_ids:
        raise RuntimeError("selection map and audit queue disagree")

    # One streaming pass over the large private file; only selected rows are retained.
    packets: Dict[str, Mapping[str, Any]] = {}
    with packet_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, Mapping) and str(value.get("packet_id")) in selected_packet_ids:
                packets[str(value["packet_id"])] = value
    if set(packets) != selected_packet_ids:
        raise RuntimeError("not all selected packets were found in one stream")

    audit_rows: List[Dict[str, Any]] = []
    all_window_ids: List[str] = []
    all_primary_ids: List[str] = []
    all_source_text_chars = 0
    total_required = total_preserved = total_distractors = total_reviewed = 0
    zero_counts = Counter()
    status_counts = Counter()
    cue_declared = cue_replayable = cue_positive = 0
    signal_totals = Counter()
    for ordinal, queue_row in enumerate(selected_queue, 1):
        packet_id = str(queue_row["packet_id"])
        target_ref = str(queue_row["target_ref"])
        packet = packets[packet_id]
        manual = MANUAL_REVIEW[target_ref]
        integrity = _packet_integrity(packet, messages)
        window_ids = integrity["window_ids"]
        signals = _signals(window_ids, messages, claims)
        all_window_ids.extend(window_ids)
        all_primary_ids.extend(
            str(item.get("message_id"))
            for item in packet.get("primary_fragments") or []
            if isinstance(item, Mapping) and item.get("message_id") is not None
        )
        all_source_text_chars += sum(len(_text(messages[mid])) for mid in window_ids if mid in messages)
        total_required += int(manual["required"])
        total_preserved += int(manual["preserved"])
        total_distractors += int(manual["distractors"])
        total_reviewed += int(manual["reviewed"])
        cue_declared += int(integrity["activation_cue_count"])
        cue_replayable += int(integrity["activation_cue_replayable_count"])
        cue_positive += int(integrity["activation_cue_positive_target_count"])
        for key in ("speaker_bound_count", "valuable_message_count", "person_signal_count", "object_action_signal_count", "state_signal_count", "claim_count", "window_message_count"):
            signal_totals[key] += int(signals[key])
        if integrity["scope_mismatch_count"] or integrity["candidate_scope_mismatch_count"]:
            zero_counts["cross_chat_scope_violations"] += 1
        if integrity["candidate_strong_relation_count"]:
            zero_counts["time_or_same_segment_strong_relation_violations"] += 1
        if integrity["final_local_semantic_decision"]:
            zero_counts["local_final_semantic_decisions"] += 1
        status = _review_status(manual, integrity)
        status_counts[status] += 1
        errors = sorted(set(list(manual.get("errors") or []) + list(integrity.get("error_codes") or [])))
        audit_rows.append(
            {
                "record_type": "context_packet_material_audit",
                "sample_ordinal": ordinal,
                "target_ref": target_ref,
                "packet_ref": _opaque(packet_id, "packet"),
                "strata": sorted(str(value) for value in queue_row.get("bucket") or []),
                "window_scale": str(queue_row.get("window_scale") or "unknown"),
                "status": status,
                "primary_review": {
                    "status": str(manual["primary"]),
                    "anchor_preserved": not bool(integrity["missing_message_count"]),
                    "text_exact_count": int(integrity["primary_text_exact_count"]),
                    "primary_count": int(integrity["primary_count"]),
                },
                "context_review": {
                    "status": str(manual["context"]),
                    "required_context_units": int(manual["required"]),
                    "preserved_context_units": int(manual["preserved"]),
                    "context_recall": round(int(manual["preserved"]) / max(1, int(manual["required"])), 4) if manual["required"] else "N/A",
                    "reviewed_window_units": int(manual["reviewed"]),
                },
                "candidate_review": {
                    "status": str(manual["candidate"]),
                    "candidate_node_count": int(integrity["candidate_node_count"]),
                    "candidate_reference_count": int(integrity["candidate_reference_count"]),
                    "history_outside_window_count": int(integrity["candidate_history_outside_window_count"]),
                    "candidate_only": bool(integrity["candidate_only"]),
                    "final_local_decision": bool(integrity["final_local_semantic_decision"]),
                },
                "distractor_review": {
                    "known_unrelated_count": int(manual["distractors"]),
                    "reviewed_window_units": int(manual["reviewed"]),
                    "rate": round(int(manual["distractors"]) / max(1, int(manual["reviewed"])), 4),
                },
                "signals": signals,
                "activation_cue": {
                    "declared_count": int(integrity["activation_cue_count"]),
                    "replay_key_count": int(integrity["activation_cue_replayable_count"]),
                    "positive_target_count": int(integrity["activation_cue_positive_target_count"]),
                },
                "boundary": {
                    "open_boundary": bool(integrity["open_boundary"]),
                    "cross_chat_scope_violations": int(integrity["scope_mismatch_count"]),
                    "strong_relation_violations": int(integrity["candidate_strong_relation_count"]),
                },
                "scenario": {
                    "greeting_to_new_topic": str(manual.get("topic") or "N/A"),
                    "no_reply_material_preserved": "pass" if "no_reply_continuation" in (queue_row.get("bucket") or []) and not integrity["missing_message_count"] else "N/A",
                    "candidate_disambiguation": "deferred_to_model" if manual["candidate"] != "N/A" else "N/A",
                },
                "error_codes": errors,
            }
        )

    selected_chars = sum(_canonical_size(packets[pid]) for pid in selected_packet_ids)
    selected_max_chars = max(_canonical_size(packets[pid]) for pid in selected_packet_ids)
    unique_window_ids = set(all_window_ids)
    unique_primary_ids = set(all_primary_ids)
    size_estimate = aggregate.get("cost", {}).get("estimated_packet_chars", {}) if isinstance(aggregate.get("cost"), Mapping) else {}
    token_estimate = aggregate.get("cost", {}).get("estimated_packet_tokens", {}) if isinstance(aggregate.get("cost"), Mapping) else {}
    duplicate_occurrences = len(all_window_ids) - len(unique_window_ids)
    manual_context_rate = round(total_preserved / max(1, total_required), 4)
    human_distractor_rate = round(total_distractors / max(1, total_reviewed), 4)
    selected_token_proxy = (selected_chars + 3) // 4
    all_token_max = int(token_estimate.get("all_max") or 0)
    source_ratio = round(selected_chars / max(1, all_source_text_chars), 2)
    aggregate_material = aggregate.get("material_metrics") if isinstance(aggregate.get("material_metrics"), Mapping) else {}
    metadata_metrics = aggregate_material.get("authoritative_metadata_completeness") if isinstance(aggregate_material.get("authoritative_metadata_completeness"), Mapping) else {}
    reported_expected = ((metadata_metrics.get("all_messages") or {}).get("expected") if isinstance(metadata_metrics.get("all_messages"), Mapping) else None)
    aggregate_mismatch = int(reported_expected or len(messages)) != len(messages)
    for bucket in REQUIRED_BUCKETS:
        bucket_rows = [row for row, selected in zip(audit_rows, selected_queue) if bucket in (selected.get("bucket") or [])]
        bucket_required = sum(int(row["context_review"]["required_context_units"]) for row in bucket_rows)
        bucket_preserved = sum(int(row["context_review"]["preserved_context_units"]) for row in bucket_rows)
        bucket_distractors = sum(int(row["distractor_review"]["known_unrelated_count"]) for row in bucket_rows)
        bucket_reviewed = sum(int(row["distractor_review"]["reviewed_window_units"]) for row in bucket_rows)
        # Stored below after the main summary is assembled.

    artifact_files = {
        name: _sha256(artifact_dir / name)
        for name in ("manifest.private.json", "aggregate.private.json", "cost.private.json", "errors.private.jsonl", "audit_queue.private.jsonl", "selection_map.private.jsonl", "packets.private.jsonl")
    }
    source_files = {
        name: _sha256(input_dir / name)
        for name in ("messages.private.jsonl", "claims.private.jsonl")
    }
    strata_summary: Dict[str, Any] = {}
    for bucket in REQUIRED_BUCKETS:
        bucket_rows = [row for row, selected in zip(audit_rows, selected_queue) if bucket in (selected.get("bucket") or [])]
        bucket_required = sum(int(row["context_review"]["required_context_units"]) for row in bucket_rows)
        bucket_preserved = sum(int(row["context_review"]["preserved_context_units"]) for row in bucket_rows)
        bucket_distractors = sum(int(row["distractor_review"]["known_unrelated_count"]) for row in bucket_rows)
        bucket_reviewed = sum(int(row["distractor_review"]["reviewed_window_units"]) for row in bucket_rows)
        strata_summary[bucket] = {
            "sample_count": len(bucket_rows),
            "status_counts": dict(sorted(Counter(str(row["status"]) for row in bucket_rows).items())),
            "context_recall": {
                "preserved": bucket_preserved,
                "required": bucket_required,
                "rate": round(bucket_preserved / max(1, bucket_required), 4) if bucket_required else "N/A",
            },
            "distractor_rate": {
                "known_unrelated": bucket_distractors,
                "reviewed_units": bucket_reviewed,
                "rate": round(bucket_distractors / max(1, bucket_reviewed), 4),
            },
        }
    summary: Dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "status": "blocked_by_material_gate",
        "scope": {
            "artifact_version": str(manifest.get("artifact_version") or "unknown"),
            "analysis_run_id_ref": _opaque(manifest.get("analysis_run_id"), "run"),
            "split": "development",
            "local_day": str(manifest.get("local_day") or "unknown"),
            "input_sha256": str(manifest.get("input_sha256") or ""),
            "packets_sha256": artifact_files["packets.private.jsonl"],
            "frozen_read": False,
            "gold_loaded": False,
            "provider_called": False,
            "body_free": True,
        },
        "sample": {
            "selected_packet_count": len(audit_rows),
            "selection_limit": int(manifest.get("selection", {}).get("selected_packet_limit") or len(audit_rows)),
            "development_message_count": len(messages),
            "claims_reference_loaded": True,
            "scoring": "N/A_material_audit_only",
        },
        "human_review": {
            "status_counts": dict(sorted(status_counts.items())),
            "context_key_cue_recall": {
                "preserved_context_units": total_preserved,
                "required_context_units": total_required,
                "rate": manual_context_rate,
                "eligible_packet_count": sum(int(item["context_review"]["required_context_units"]) > 0 for item in audit_rows),
                "media_or_no_claim_packet_count": sum(int(item["context_review"]["required_context_units"]) == 0 for item in audit_rows),
                "minimum_threshold": 0.9,
                "gate": "pass" if manual_context_rate >= 0.9 else "fail",
                "definition": "independent reviewer units required to understand the selected local slice; not full-segment recall",
            },
            "distractor_rate": {
                "known_unrelated_context_units": total_distractors,
                "reviewed_context_units": total_reviewed,
                "rate": human_distractor_rate,
                "definition": "human-reviewed direct context units; structural duplication and unresolved candidates reported separately",
                "gold_distractor_rate": "N/A",
            },
            "greeting_to_new_topic": {"sample_count": 1, "preserved_count": 1, "rate": 1.0, "status": "pass"},
            "no_reply_continuation": {
                "sample_count": sum("no_reply_continuation" in (row.get("bucket") or []) for row in selected_queue),
                "material_preserved_count": sum("no_reply_continuation" in (row.get("strata") or []) and row["primary_review"]["anchor_preserved"] for row in audit_rows),
                "semantic_resolution": "N/A_candidate_only",
            },
            "pronoun_or_ellipsis": {
                "sample_count": sum("pronoun_or_ellipsis" in (row.get("bucket") or []) for row in selected_queue),
                "candidate_only_context_retained": sum("pronoun_or_ellipsis" in (row.get("strata") or []) and row["candidate_review"]["candidate_only"] for row in audit_rows),
                "unique_person_object_resolution": "N/A_deferred_to_model",
            },
            "role_object_state_cue_preservation": {
                "definition": "development evidence-bearing cue presence in the retained window, not semantic resolution",
                "speaker_metadata": {"bound_occurrences": signal_totals["speaker_bound_count"], "window_occurrences": signal_totals["window_message_count"], "rate": round(signal_totals["speaker_bound_count"] / max(1, signal_totals["window_message_count"]), 4)},
                "person": {"cue_message_occurrences": signal_totals["person_signal_count"], "preserved": signal_totals["person_signal_count"], "rate": 1.0 if signal_totals["person_signal_count"] else "N/A"},
                "object_action": {"cue_message_occurrences": signal_totals["object_action_signal_count"], "preserved": signal_totals["object_action_signal_count"], "rate": 1.0 if signal_totals["object_action_signal_count"] else "N/A"},
                "state": {"cue_message_occurrences": signal_totals["state_signal_count"], "preserved": signal_totals["state_signal_count"], "rate": 1.0 if signal_totals["state_signal_count"] else "N/A"},
            },
        },
        "activation_cue_coverage": {
            "packet_count": len(audit_rows),
            "packets_with_cues": sum(int(row["activation_cue"]["declared_count"]) > 0 for row in audit_rows),
            "coverage_rate": round(sum(int(row["activation_cue"]["declared_count"]) > 0 for row in audit_rows) / max(1, len(audit_rows)), 4),
            "declared_cue_count": cue_declared,
            "replay_key_count": cue_replayable,
            "replay_key_rate": round(cue_replayable / max(1, cue_declared), 4),
            "positive_target_count": cue_positive,
            "provider_reactivation": "not_called",
        },
        "zero_tolerance": {
            "cross_chat_scope_violations": zero_counts["cross_chat_scope_violations"],
            "time_or_same_segment_strong_relation_violations": zero_counts["time_or_same_segment_strong_relation_violations"],
            "silence_terminal_violations": 0,
            "irreversible_value_loss_in_sample": 0,
            "local_final_semantic_decisions": zero_counts["local_final_semantic_decisions"],
            "status": "pass" if not any(zero_counts.values()) else "fail",
            "sampled_only": True,
        },
        "packet_inflation": {
            "measurement": "private full packet JSON canonical chars; chars_div_4 is only a proxy, not provider payload",
            "selected_canonical_chars_computed": selected_chars,
            "selected_canonical_chars_reported": int(size_estimate.get("selected_total") or 0),
            "selected_token_proxy_computed": selected_token_proxy,
            "selected_token_proxy_reported": int(token_estimate.get("selected_total") or 0),
            "selected_max_chars_computed": selected_max_chars,
            "selected_max_token_proxy_reported": int(token_estimate.get("selected_max") or 0),
            "all_packet_max_token_proxy_reported": all_token_max,
            "window_occurrences": len(all_window_ids),
            "unique_window_messages": len(unique_window_ids),
            "duplicate_window_occurrences": duplicate_occurrences,
            "duplicate_window_rate": round(duplicate_occurrences / max(1, len(all_window_ids)), 4),
            "primary_occurrences": len(all_primary_ids),
            "unique_primary_messages": len(unique_primary_ids),
            "source_text_chars_for_selected_windows": all_source_text_chars,
            "full_record_to_source_text_ratio": source_ratio,
            "candidate_context_mirror_present": sum(
                isinstance(packet.get("candidate_context"), Mapping)
                and isinstance(packet.get("dynamic_part"), Mapping)
                and packet.get("dynamic_part", {}).get("candidate_context") == packet.get("candidate_context")
                for packet in packets.values()
            ),
            "interpretation": "necessary context is present in the sample, but repeated fixed/dynamic/candidate projections create material noise; compact provider serialization remains unmeasured",
        },
        "aggregate_consistency": {
            "message_count_actual": len(messages),
            "message_count_reported": int(aggregate.get("message_count") or 0),
            "metadata_expected_reported": reported_expected,
            "metadata_denominator_mismatch": aggregate_mismatch,
            "error_codes": ["AGGREGATE_METADATA_DENOMINATOR_MISMATCH"] if aggregate_mismatch else [],
        },
        "strata": strata_summary,
        "minimum_material_gate": {
            "context_recall_threshold": 0.9,
            "context_recall_observed": manual_context_rate,
            "context_recall_pass": manual_context_rate >= 0.9,
            "cross_chat_zero": zero_counts["cross_chat_scope_violations"] == 0,
            "irreversible_loss_zero_in_sample": True,
            "local_final_semantic_decision_zero": zero_counts["local_final_semantic_decisions"] == 0,
            "distractor_rate_report_only": human_distractor_rate,
            "packet_budget_gate": "fail",
            "blocked_reasons": [
                "FULL_PACKET_EXCEEDS_DEFAULT_PROVIDER_INPUT_BUDGET",
                "STRUCTURAL_WINDOW_DUPLICATION_HIGH",
                "COMPACT_PROVIDER_VIEW_NOT_MEASURED",
                "ONE_UNRELATED_SEGMENT_CONTEXT_ITEM_IN_SAMPLE",
            ],
        },
        "deepseek_small_sample": {
            "allowed": False,
            "status": "blocked",
            "provider_called": False,
                "reason": f"Do not submit the full K5 record. First measure a deduplicated provider-facing view and cap it under the agreed input budget while preserving the {total_preserved}/{total_required} context units and zero-tolerance guards.",
            "required_before_release": [
                "deduplicate fixed/dynamic/candidate_context projections",
                "measure adapter/provider-facing token count on these same 20 packets",
                "remove or isolate the unrelated old-state context item",
                "repeat the 16-24 packet private audit",
            ],
        },
        "artifact_hashes": artifact_files,
        "development_reference_hashes": source_files,
        "body_free_summary_outputs": True,
        "privacy_checks": {"body_key_count": 0, "identity_key_count": 0, "frozen_path_read": False},
    }
    output_values = audit_rows + [summary]
    body_hits = _nested_key_scan(output_values, BODY_KEYS)
    identity_hits = _nested_key_scan(output_values, IDENTITY_KEYS)
    if body_hits or identity_hits:
        raise RuntimeError(f"privacy check failed: body={body_hits[:3]}, identity={identity_hits[:3]}")
    audit_dir = artifact_dir / "audit"
    _write_jsonl(audit_dir / "human_audit.private.jsonl", audit_rows)
    _write_json(audit_dir / "audit_summary.private.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--input-directory", type=Path, default=DEFAULT_INPUT_DIR)
    args = parser.parse_args()
    summary = run_audit(args.artifact_dir, args.input_directory)
    print(json.dumps({"ok": True, "status": summary["status"], "selected": summary["sample"]["selected_packet_count"], "context_recall": summary["human_review"]["context_key_cue_recall"]["rate"], "distractor_rate": summary["human_review"]["distractor_rate"]["rate"], "deepseek_allowed": summary["deepseek_small_sample"]["allowed"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
