"""Independent private K9 audit for the linear stage-packet artifact.

This file is an audit tool, not a runner or a production component.  It is
deliberately independent of the K9 implementation: it reads the completed
artifact's materialized and recovery JSONL maps, and the already-reviewed K5
selection/baseline.  It never imports the provider, starts a provider call,
reads frozen data, or emits packet bodies and raw identifiers.

The target is accepted only when the exact
``linear_stage_packet_development_v1`` manifest is complete.  The two K9
maps are streamed line by line; only rows that can be tied to one of the same
twenty K5 selected roots are retained.  Every emitted identifier is a
one-way opaque reference.  Missing lineage, missing key context, incomplete
Stage A accounting, an unrecoverable pending envelope, a non-linear page
count, or a zero-tolerance violation blocks the Stage A pilot.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


DEFAULT_ARTIFACT_DIR = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_packet_development_v1"
)
DEFAULT_K5_DIR = Path(
    "data/private/gold_standard/2026-08-25/context_packet_development_v1"
)
TARGET_ARTIFACT_VERSION = "linear_stage_packet_development_v1"
K5_ARTIFACT_VERSION = "context_packet_development_v1"
AUDIT_SCHEMA = "linear_stage_packet_k9_private_audit_v1"
SELECTION_LIMIT = 20
MIN_RECALL = 0.90
MAX_INPUT_TOKENS = 2000
MAX_USER_TOKENS = 1600
MAX_MESSAGES = 24
MAX_CANDIDATES = 64
MAX_EVIDENCE = 64

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_body",
        "text_redacted",
        "transcript",
    }
)

# K9 transport envelopes intentionally carry both an opaque materialized
# packet and a stage-A map.  The packet/user JSON can contain arbitrary
# nested ``stage`` words; it is not a second accounting envelope for this
# audit and must not be traversed while collecting Stage-A stats.
STAGE_A_BODY_KEYS = BODY_KEYS | frozenset({"system_prompt", "user_packet", "user_canonical_json"})

# These are only checked in the audit outputs.  ``*_ref`` and ``*_refs``
# values are intentionally allowed because they are opaque hashes.
IDENTITY_KEYS = frozenset(
    {
        "account_id",
        "candidate_id",
        "chat_id",
        "claim_id",
        "display_name",
        "evidence_id",
        "fragment_id",
        "message_id",
        "packet_id",
        "person_id",
        "person_name",
        "record_id",
        "relation_id",
        "sender_id",
        "source_id",
        "speaker_id",
        "thread_id",
    }
)

LINEAGE_KEYS = frozenset(
    {
        "target_ref",
        "selection_ref",
        "selected_root_ref",
        "k5_target_ref",
        "root_id",
        "root_ref",
        "root_packet_id",
        "source_root_id",
        "source_packet_id",
        "source_id",
        "source_ref",
        "source_packet_hash",
        "packet_id",
        "packet_ref",
        "packet_hash",
        "page_id",
        "page_ref",
        "page_ids",
        "page_refs",
        "leaf_id",
        "leaf_ref",
        "leaf_ids",
        "leaf_packet_id",
        "leaf_packet_ids",
        "subpacket_id",
        "subpacket_ids",
        "subpacket_ref",
        "subpacket_refs",
        "parent_id",
        "parent_ref",
        "parent_packet_id",
        "root_hash",
        "source_hash",
        "fixed_hash",
        "dynamic_hash",
        "content_hash",
        "selection_rank",
    }
)

PRIMARY_FIELDS = frozenset(
    {
        "primary",
        "primary_ids",
        "primary_refs",
        "primary_fragments",
        "primary_messages",
        "primary_message_ids",
        "primary_message_handles",
    }
)
ADJACENT_FIELDS = frozenset(
    {
        "adjacent",
        "adjacent_ids",
        "adjacent_refs",
        "adjacent_context",
        "adjacent_messages",
        "adjacent_message_ids",
        "adjacent_message_handles",
        "context_fragments",
        "greeting",
        "greeting_context",
    }
)
MESSAGE_FIELDS = frozenset(
    {
        "message_ids",
        "message_refs",
        "message_handles",
        "messages",
        "window_refs",
        "window_message_ids",
        "retained_message_ids",
        "all_message_ids",
        "source_message_ids",
        "context_message_ids",
    }
)
CANDIDATE_FIELDS = frozenset(
    {
        "candidate_ids",
        "candidate_refs",
        "candidate_handles",
        "candidate_links",
        "candidate_link_refs",
        "candidate_rows",
        "candidates",
        "continuity_candidates",
        "candidate_context",
        "candidate_qa_links",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "open_thread_candidates",
        "open_threads",
        "person_history",
        "object_history",
        "state_history",
        "qa_candidates",
        "qa_links",
    }
)
EVIDENCE_FIELDS = frozenset(
    {
        "evidence_ids",
        "evidence_refs",
        "evidence_handles",
        "evidence_handle_refs",
        "evidence",
        "source_refs",
        "source_ref_ids",
        "source_references",
        "all_source_refs",
    }
)
WEAK_REASON_CODES = frozenset(
    {
        "time_proximity",
        "time_proximity_weak",
        "time_proximity_only",
        "temporal_proximity",
        "temporal_only",
        "time_only",
        "same_segment",
        "same_segment_weak",
        "same_segment_only",
        "same_dialogue_segment",
    }
)


def _safe_path(path: Path) -> Path:
    resolved = path.resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in resolved.parts):
        raise RuntimeError(f"K9 private audit refuses frozen path: {resolved}")
    return resolved


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return dict(value)


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"expected JSON object in {path.name}:{line_number}")
            yield dict(value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _opaque(value: Any, namespace: str) -> str:
    raw = "<missing>" if value is None else str(value)
    return f"{namespace}_{hashlib.sha256((namespace + '|' + raw).encode('utf-8')).hexdigest()[:24]}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _first(value: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in value and value[name] not in (None, ""):
            return value[name]
    return default


def _values(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _strings(value: Any) -> List[str]:
    return [str(item) for item in _values(value) if item not in (None, "") and not isinstance(item, Mapping)]


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _nested_key_scan(value: Any, keys: Set[str], path: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            child_path = f"{path}.{lowered}" if path else lowered
            if lowered in keys and child not in (None, "", [], (), {}):
                found.append(child_path)
            found.extend(_nested_key_scan(child, keys, child_path))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            found.extend(_nested_key_scan(child, keys, f"{path}[{index}]"))
    return found


def _body_free(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _body_free(child)
            for key, child in value.items()
            if str(key).casefold() not in BODY_KEYS
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_body_free(child) for child in value]
    return value


def _scope_parts(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(value, Mapping):
        account = _first(value, ("account_id", "account", "account_ref"))
        chat = _first(value, ("chat_id", "chat", "chat_ref"))
        if account not in (None, "") and chat not in (None, ""):
            return str(account), str(chat)
        return _scope_parts(value.get("scope"))
    if isinstance(value, str):
        for separator in ("/", "::"):
            if separator in value:
                left, right = value.split(separator, 1)
                if left and right:
                    return left, right
    return None, None


def _scope_pairs(value: Any) -> Set[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            account, chat = _scope_parts(item)
            if account and chat:
                pairs.add((account, chat))
            for child in item.values():
                if isinstance(child, (Mapping, list, tuple, set, frozenset)):
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return pairs


def _normalize_identifier(value: Any) -> Set[str]:
    """Return raw and deterministic table-handle variants for comparisons."""

    if value in (None, "") or isinstance(value, Mapping):
        return set()
    text = str(value)
    result = {text}
    for marker in ("|message|", "|evidence|", "|candidate|", "|page|", "|subpacket|"):
        if marker in text:
            left, right = text.split(marker, 1)
            if right:
                result.add(right)
            if marker in {"|page|", "|subpacket|"} and left:
                result.add(left)
    return result


def _id_from_row(row: Mapping[str, Any], names: Sequence[str]) -> Set[str]:
    result: Set[str] = set()
    for name in names:
        if name not in row:
            continue
        value = row[name]
        if isinstance(value, Mapping):
            nested = _first(value, ("id", "ref", "key", "message_id", "evidence_id", "candidate_id", "handle"))
            result.update(_normalize_identifier(nested))
        else:
            for item in _values(value):
                if isinstance(item, Mapping):
                    nested = _first(item, ("id", "ref", "key", "message_id", "evidence_id", "candidate_id", "handle"))
                    result.update(_normalize_identifier(nested))
                else:
                    result.update(_normalize_identifier(item))
    return result


def _lineage_aliases(value: Any) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in LINEAGE_KEYS:
                    if isinstance(child, Mapping):
                        result.update(_id_from_row(child, tuple(child.keys())))
                        visit(child)
                    else:
                        for part in _values(child):
                            if isinstance(part, Mapping):
                                result.update(_id_from_row(part, tuple(part.keys())))
                            else:
                                result.update(_normalize_identifier(part))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)):
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _page_refs(value: Any) -> Set[str]:
    """Collect raw page references without deriving ambiguous ordinal aliases.

    ``_normalize_identifier`` intentionally derives suffixes such as ``0001``
    from page handles for lineage matching.  Those suffixes are not page
    identities, however: every selected root has a first page.  Keeping them
    out of the page-reference set prevents the streaming matcher from
    assigning later roots to the first root's page alias.
    """
    result: Set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() not in {"page_id", "page_ref", "page_ids", "page_refs"}:
                continue
            for part in _values(child):
                if isinstance(part, Mapping):
                    nested = _first(part, ("page_id", "page_ref", "id", "ref", "handle"))
                    if nested not in (None, ""):
                        result.add(str(nested))
                elif part not in (None, ""):
                    result.add(str(part))
        for key, child in value.items():
            if isinstance(child, (Mapping, list, tuple, set, frozenset)) and str(key).casefold() not in BODY_KEYS:
                result.update(_page_refs(child))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            result.update(_page_refs(child))
    return result


def _row_selection_rank(row: Mapping[str, Any]) -> Optional[int]:
    value = _first(row, ("selection_rank", "sample_ordinal", "rank"))
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _direct_rows(value: Mapping[str, Any], names: Sequence[str]) -> List[Dict[str, Any]]:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, (list, tuple)):
            return [dict(item) for item in candidate if isinstance(item, Mapping)]
        if isinstance(candidate, Mapping) and all(isinstance(item, Mapping) for item in candidate.values()):
            return [dict(item) for item in candidate.values()]
    return []


def _message_id(row: Mapping[str, Any]) -> Optional[str]:
    value = _first(row, ("message_id", "source_message_id", "message_ref", "message_handle", "id"))
    if value in (None, ""):
        return None
    return str(value)


def _evidence_id(row: Mapping[str, Any]) -> Optional[str]:
    value = _first(row, ("evidence_id", "evidence_ref_id", "evidence_handle", "ref_id", "id"))
    if value in (None, ""):
        return None
    return str(value)


def _reason_values(row: Mapping[str, Any]) -> Set[str]:
    result: Set[str] = set()
    for key in ("candidate_reason", "candidate_reasons", "reason_codes", "supporting_slot_codes", "reasons"):
        result.update(str(item).casefold() for item in _strings(row.get(key)))
    return result


def _baseline_packet_window(packet: Mapping[str, Any]) -> Dict[str, Any]:
    primary_rows = _direct_rows(packet, ("primary_fragments", "primary", "fragments"))
    adjacent_rows = _direct_rows(packet, ("adjacent_context", "adjacent", "context_fragments", "greeting_context"))
    primary_ids: Set[str] = set()
    adjacent_ids: Set[str] = set()
    for row in primary_rows:
        value = _message_id(row)
        if value:
            primary_ids.update(_normalize_identifier(value))
    for row in adjacent_rows:
        value = _message_id(row)
        if value:
            adjacent_ids.update(_normalize_identifier(value))
    distractors: Set[str] = set()
    for row in adjacent_rows:
        reasons = _reason_values(row)
        distance = row.get("time_distance_seconds")
        try:
            far = distance is not None and float(distance) > 300
        except (TypeError, ValueError):
            far = False
        explicit = any(bool(row.get(key)) for key in ("unrelated", "is_unrelated", "distractor", "old_state_context"))
        if far or explicit or "unrelated" in reasons or "old_state" in reasons:
            message = _message_id(row)
            if message:
                distractors.update(_normalize_identifier(message))
    evidence_ids: Set[str] = set()
    for row in _direct_rows(packet, ("evidence_refs", "evidence", "evidence_references")):
        value = _evidence_id(row)
        if value:
            evidence_ids.update(_normalize_identifier(value))
    source_ids: Set[str] = set()
    for row in _direct_rows(packet, ("source_refs", "sources", "source_references")):
        value = _first(row, ("source_ref_id", "source_id", "raw_message_ref", "record_hash", "id", "ref"))
        if value:
            source_ids.update(_normalize_identifier(value))
    window_ids = primary_ids | adjacent_ids
    return {
        "primary_ids": primary_ids,
        "adjacent_ids": adjacent_ids,
        "window_ids": window_ids,
        "distractor_ids": distractors,
        "key_ids": window_ids - distractors,
        "evidence_ids": evidence_ids,
        "source_ids": source_ids,
        "scope": _scope_pairs(packet),
    }


def _candidate_layer_presence(value: Any) -> Dict[str, int]:
    counts = {key: 0 for key in ("person", "object", "state", "qa", "open", "continuity")}
    seen: Dict[str, Set[str]] = {key: set() for key in counts}

    def classify(key: str, row: Mapping[str, Any]) -> str:
        lowered = key.casefold()
        if "person" in lowered or any(name in row for name in ("person_ref_id", "person_id")):
            return "person"
        if "object" in lowered or any(name in row for name in ("object_ref_id", "object_id")):
            return "object"
        if "state" in lowered or any(name in row for name in ("state_ref_id", "state_id")):
            return "state"
        if "qa" in lowered or "reply" in lowered or str(row.get("relation_subtype", "")).casefold() in {"qa", "question_answer", "reply"}:
            return "qa"
        if "open" in lowered or bool(row.get("open_boundary")):
            return "open"
        return "continuity"

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in CANDIDATE_FIELDS:
                    for child_row in _values(child):
                        if isinstance(child_row, Mapping):
                            layer = classify(lowered, child_row)
                            ident = _first(child_row, ("candidate_id", "candidate_handle", "relation_id", "id"))
                            marker = str(ident) if ident not in (None, "") else _canonical(_body_free(child_row))
                            if marker not in seen[layer]:
                                seen[layer].add(marker)
                                counts[layer] += 1
                        elif child_row not in (None, ""):
                            layer = classify(lowered, {})
                            marker = str(child_row)
                            if marker not in seen[layer]:
                                seen[layer].add(marker)
                                counts[layer] += 1
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    visit(value)
    return counts


def _message_values(value: Any) -> Set[str]:
    result: Set[str] = set()
    for item in _values(value):
        if isinstance(item, Mapping):
            message = _message_id(item)
            if message:
                result.update(_normalize_identifier(message))
        else:
            result.update(_normalize_identifier(item))
    return result


def _message_ids(value: Any) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in CANDIDATE_FIELDS or lowered in EVIDENCE_FIELDS:
                    continue
                if lowered in MESSAGE_FIELDS or lowered in PRIMARY_FIELDS or lowered in ADJACENT_FIELDS:
                    result.update(_message_values(child))
                elif lowered.endswith("_message_id") and not any(token in parent for token in ("candidate", "evidence", "source")):
                    result.update(_normalize_identifier(child))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    visit(value)
    return result


def _layer_message_ids(value: Any, layer: str) -> Set[str]:
    fields = PRIMARY_FIELDS if layer == "primary" else ADJACENT_FIELDS
    result: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in fields:
                    result.update(_message_values(child))
                elif isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in CANDIDATE_FIELDS and lowered not in EVIDENCE_FIELDS:
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _reference_ids(value: Any, fields: Set[str]) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in fields:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            nested = _first(part, ("id", "ref", "key", "handle", "source_ref_id", "evidence_id", "evidence_ref_id"))
                            if nested not in (None, ""):
                                result.update(_normalize_identifier(nested))
                        else:
                            result.update(_normalize_identifier(part))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    visit(value)
    return result


def _numeric(value: Any, names: Sequence[str]) -> Optional[int]:
    if not isinstance(value, Mapping):
        return None
    for name in names:
        if name not in value or isinstance(value[name], bool):
            continue
        raw = value[name]
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                return int(float(raw))
            except ValueError:
                pass
    return None


def _list_count(value: Any, names: Sequence[str]) -> Optional[int]:
    if not isinstance(value, Mapping):
        return None
    for name in names:
        child = value.get(name)
        if isinstance(child, (list, tuple, set, frozenset)):
            return len(child)
    return None


def _reference_observation(rows: Sequence[Mapping[str, Any]], kind: str) -> Dict[str, Any]:
    if kind == "source":
        fields = {"source_refs", "source_ref_ids", "source_references", "all_source_refs", "sources"}
        count_names = ("source_ref_count", "source_count", "recovered_source_count", "source_refs_count")
    else:
        fields = {"evidence_refs", "evidence_ref_ids", "evidence_handles", "evidence_handle_refs", "evidence_ids", "evidence"}
        count_names = ("evidence_ref_count", "evidence_count", "recovered_evidence_count", "evidence_refs_count")
    ids: Set[str] = set()
    numeric_counts: List[int] = []
    rate_recovered: List[int] = []
    rate_expected: List[int] = []

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            value = _numeric(item, count_names)
            if value is not None:
                numeric_counts.append(value)
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in fields:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            nested = _first(part, ("id", "ref", "key", "handle", "source_ref_id", "evidence_id", "evidence_ref_id"))
                            if nested not in (None, ""):
                                ids.update(_normalize_identifier(nested))
                        else:
                            ids.update(_normalize_identifier(part))
                # Recovery summaries commonly expose rates.{kind}.
                if lowered in {kind, "source" if kind == "source" else "evidence"} and isinstance(child, Mapping):
                    expected = _numeric(child, ("expected", "total", "declared"))
                    recovered = _numeric(child, ("recovered", "retained", "materialized", "actual"))
                    if expected is not None:
                        rate_expected.append(expected)
                    if recovered is not None:
                        rate_recovered.append(recovered)
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    for row in rows:
        visit(row)
    return {
        "ids": ids,
        "id_count": len(ids),
        "numeric_count": max(numeric_counts, default=0),
        "rate_expected": max(rate_expected, default=0),
        "rate_recovered": max(rate_recovered, default=0),
    }


def _status(value: Mapping[str, Any]) -> str:
    raw = _first(value, ("status", "material_status", "recovery_status", "state"), "unknown")
    return str(raw or "unknown").casefold()


def _is_pending(value: Mapping[str, Any]) -> bool:
    return _status(value) in {"pending", "deferred", "blocked", "over_capacity"} or bool(value.get("pending_reason"))


def _has_snapshot(value: Mapping[str, Any]) -> bool:
    for key in ("open_snapshot", "open_snapshot_ref", "open_thread_snapshot", "open_snapshot_refs", "snapshot", "replay_point", "replay_ref"):
        if value.get(key) not in (None, "", [], (), {}):
            return True
    return False


def _has_replay_handles(value: Mapping[str, Any]) -> bool:
    fields = (
        "page_id",
        "page_ref",
        "page_refs",
        "message_handles",
        "message_ids",
        "candidate_handles",
        "candidate_ids",
        "evidence_handles",
        "evidence_ids",
        "source_refs",
        "root_id",
        "root_ref",
    )
    return any(value.get(key) not in (None, "", [], (), {}) for key in fields)


def _open_boundary_preserved(value: Any) -> bool:
    """Find an explicit open-boundary marker in nested recovery envelopes."""

    def visit(item: Any) -> bool:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered == "open_boundary":
                    if isinstance(child, bool) and child:
                        return True
                    if str(child or "").casefold() in {"true", "open", "pending"}:
                        return True
                if lowered == "status" and str(child or "").casefold() == "open":
                    return True
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    if visit(child):
                        return True
        elif isinstance(item, (list, tuple, set, frozenset)):
            return any(visit(child) for child in item)
        return False

    return visit(value)


def _truthy_semantic_final(key: str, value: Any) -> bool:
    if key.casefold() not in {
        "final_local_semantic_decision",
        "local_final_semantic_decision",
        "semantic_final",
        "final_semantic_decision",
        "resolved_relation",
        "semantic_relation",
    }:
        return False
    if isinstance(value, bool):
        return value
    return str(value or "").casefold() in {"true", "resolved", "strong", "same_event", "complete", "final"}


def _boundary_stats(value: Any) -> Dict[str, int]:
    cross_chat = 0
    weak_strong = 0
    local_final = 0

    def visit(item: Any) -> None:
        nonlocal cross_chat, weak_strong, local_final
        if isinstance(item, Mapping):
            reasons: Set[str] = set()
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"candidate_reason", "candidate_reasons", "reason_codes", "supporting_slot_codes", "reasons"}:
                    reasons.update(str(part).casefold() for part in _strings(child))
                if _truthy_semantic_final(lowered, child):
                    local_final += 1
            relation = str(_first(item, ("relation_label", "relation", "relation_subtype"), "")).casefold()
            strong = any(bool(item.get(key)) for key in ("strong_relation", "is_strong", "strong")) or relation in {"resolved", "same_event", "strong"}
            if strong and reasons and reasons <= WEAK_REASON_CODES:
                weak_strong += 1
            if any(str(key).casefold() in {"cross_chat", "cross_chat_violation", "cross_scope", "scope_mismatch"} and bool(child) for key, child in item.items()):
                cross_chat += 1
            if len(_scope_pairs(item)) > 1:
                cross_chat += 1
            for child in item.values():
                if isinstance(child, (Mapping, list, tuple, set, frozenset)):
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return {
        "cross_chat_violations": cross_chat,
        "time_or_same_segment_strong_violations": weak_strong,
        "local_final_semantic_decisions": local_final,
    }


def _scope_violations(rows: Sequence[Mapping[str, Any]], expected: Set[Tuple[str, str]]) -> int:
    if not expected:
        return 0
    return sum(1 for row in rows for pair in _scope_pairs(row) if pair not in expected)


def _number_node(value: Mapping[str, Any], names: Sequence[str]) -> Optional[int]:
    return _numeric(value, names)


def _stats_from_node(node: Mapping[str, Any]) -> Optional[Dict[str, Optional[int]]]:
    candidate: Mapping[str, Any] = node
    for name in ("material_stats", "stats", "provider_envelope", "envelope", "accounting"):
        if isinstance(node.get(name), Mapping):
            candidate = node[name]
            break
    token = _number_node(candidate, ("input_token_proxy", "total_token_proxy", "provider_input_token_proxy", "estimated_token_proxy", "serialized_token_proxy"))
    user = _number_node(candidate, ("user_token_proxy", "user_input_token_proxy", "user_payload_token_proxy", "user_tokens"))
    message = _number_node(candidate, ("message_count", "messages_count", "materialized_message_count", "provider_message_count"))
    candidate_count = _number_node(candidate, ("candidate_count", "candidate_row_count", "candidate_rows_count", "provider_candidate_count"))
    evidence = _number_node(candidate, ("evidence_count", "evidence_ref_count", "evidence_refs_count", "provider_evidence_count"))
    if message is None:
        message = _list_count(candidate, ("messages", "message_ids", "message_handles"))
    if candidate_count is None:
        candidate_count = _list_count(candidate, ("candidates", "candidate_ids", "candidate_handles", "candidate_rows"))
    if evidence is None:
        evidence = _list_count(candidate, ("evidence", "evidence_ids", "evidence_handles", "evidence_refs"))
    if all(item is None for item in (token, user, message, candidate_count, evidence)):
        return None
    return {
        "input_token_proxy": token,
        "user_token_proxy": user,
        "message_count": message,
        "candidate_count": candidate_count,
        "evidence_count": evidence,
    }


def _stage_a_envelopes(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    envelopes: List[Dict[str, Any]] = []
    explicit_stage = False
    any_stage_marker = False

    def visit(item: Any, parent: str = "") -> None:
        nonlocal explicit_stage, any_stage_marker
        if isinstance(item, Mapping):
            stage = str(_first(item, ("stage", "stage_name", "materialization_stage"), "")).casefold()
            if stage:
                any_stage_marker = True
            # A stage marker under ``open_snapshot``/``topic_map`` is data
            # inside the envelope, not another Stage-A envelope.  Count only
            # the row itself or the dedicated stage_a wrapper.
            is_stage_container = (
                (not parent and stage in {"a", "stage_a", "stage-a"})
                or parent in {"stage_a", "stage-a", "stagea", "materialized_stage_a", "a"}
            )
            if is_stage_container:
                explicit_stage = True
                any_stage_marker = True
                stats = _stats_from_node(item)
                if stats is not None:
                    envelopes.append({"stats": stats, "status": _status(item), "page_refs": _page_refs(item)})
            for key, child in item.items():
                lowered = str(key).casefold()
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in STAGE_A_BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    for row in rows:
        visit(row)
        if not any_stage_marker:
            stats = _stats_from_node(row)
            if stats is not None:
                envelopes.append({"stats": stats, "status": _status(row), "page_refs": _page_refs(row)})

    # Stage-A metadata may be repeated by a transport wrapper.  Deduplicate
    # equal accounting/page tuples so repeated JSON nesting cannot inflate the
    # observed page count or make the report non-deterministic.
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for envelope in envelopes:
        marker = _canonical({"stats": envelope["stats"], "status": envelope["status"], "page_refs": sorted(envelope["page_refs"])})
        if marker not in seen:
            seen.add(marker)
            result.append(envelope)
    return result


def _handle_sets(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Set[str]]:
    output = {"message": set(), "candidate": set(), "evidence": set()}

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in MESSAGE_FIELDS or lowered in PRIMARY_FIELDS or lowered in ADJACENT_FIELDS:
                    output["message"].update(_message_values(child))
                elif lowered in CANDIDATE_FIELDS:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            value = _first(part, ("candidate_handle", "candidate_id", "relation_id", "id"))
                            if value:
                                output["candidate"].update(_normalize_identifier(value))
                        else:
                            output["candidate"].update(_normalize_identifier(part))
                elif lowered in EVIDENCE_FIELDS:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            value = _first(part, ("evidence_handle", "evidence_id", "evidence_ref_id", "id", "ref"))
                            if value:
                                output["evidence"].update(_normalize_identifier(value))
                        else:
                            output["evidence"].update(_normalize_identifier(part))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    for row in rows:
        visit(row)
    return output


def _page_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    pages: Set[str] = set()
    handles = _handle_sets(rows)
    explicit_counts = {key: [] for key in ("message", "candidate", "evidence")}
    page_dimensions: List[Dict[str, int]] = []
    authoritative_pages: Dict[str, Dict[str, int]] = {}

    def _direct_page_values(item: Mapping[str, Any]) -> List[str]:
        values: List[str] = []
        for name in ("page_id", "page_ref", "page_ids", "page_refs"):
            if name not in item:
                continue
            for part in _values(item[name]):
                if isinstance(part, Mapping):
                    part = _first(part, ("page_id", "page_ref", "id", "ref", "handle"))
                if part not in (None, ""):
                    values.append(str(part))
        return list(dict.fromkeys(values))

    def _count_from(mapping: Mapping[str, Any], names: Sequence[str]) -> Optional[int]:
        lowered = {str(key).casefold(): value for key, value in mapping.items()}
        for name in names:
            value = lowered.get(name.casefold())
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            pages.update(_page_refs(item))
            direct_pages = _direct_page_values(item)
            page_counts = item.get("page_counts")
            if direct_pages and isinstance(page_counts, Mapping):
                dimensions = {
                    "message": _count_from(page_counts, ("messages", "message_count", "message")) or 0,
                    "candidate": _count_from(page_counts, ("candidates", "candidate_count", "candidate")) or 0,
                    "evidence": _count_from(page_counts, ("evidence", "evidence_count", "evidence_refs")) or 0,
                }
                # The materialized map is the authoritative page accounting
                # surface.  Recovery rows repeat page references but do not
                # define another page-sized chunk, so deduplicate by page id.
                for page in direct_pages:
                    authoritative_pages.setdefault(page, dimensions)
            has_page_identity = bool(direct_pages)
            message_count = _numeric(item, ("message_count", "messages_count", "materialized_message_count"))
            candidate_count = _numeric(item, ("candidate_count", "candidate_row_count", "candidate_rows_count"))
            evidence_count = _numeric(item, ("evidence_count", "evidence_ref_count", "evidence_refs_count"))
            for key, value in (("message", message_count), ("candidate", candidate_count), ("evidence", evidence_count)):
                if value is not None:
                    explicit_counts[key].append(value)
            # Fallback for an older/private map without page_counts.  Only
            # page-identified rows are considered page chunks; aggregate
            # tables nested under a root must not inflate page dimensions.
            if not isinstance(page_counts, Mapping):
                local: Dict[str, int] = {}
                for key, fields in (
                    ("message", MESSAGE_FIELDS | PRIMARY_FIELDS | ADJACENT_FIELDS),
                    ("candidate", CANDIDATE_FIELDS),
                    ("evidence", EVIDENCE_FIELDS),
                ):
                    total = 0
                    for field in fields:
                        if field in item:
                            total += len(_message_values(item[field])) if key == "message" else len(_values(item[field]))
                    if total:
                        local[key] = total
                if local and has_page_identity:
                    page_dimensions.append(local)
            for key, child in item.items():
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and str(key).casefold() not in BODY_KEYS:
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    for row in rows:
        visit(row)

    if authoritative_pages:
        page_dimensions = list(authoritative_pages.values())
        message_count = sum(item["message"] for item in page_dimensions)
        candidate_count = sum(item["candidate"] for item in page_dimensions)
        evidence_count = sum(item["evidence"] for item in page_dimensions)
        message_handle_count = message_count
        candidate_handle_count = candidate_count
        evidence_handle_count = evidence_count
    else:
        message_count = max(len(handles["message"]), max(explicit_counts["message"], default=0))
        candidate_count = max(len(handles["candidate"]), max(explicit_counts["candidate"], default=0))
        evidence_count = max(len(handles["evidence"]), max(explicit_counts["evidence"], default=0))
        message_handle_count = len(handles["message"])
        candidate_handle_count = len(handles["candidate"])
        evidence_handle_count = len(handles["evidence"])
    dimensions = (message_count, candidate_count, evidence_count)
    linear_bound = max(
        math.ceil(message_count / MAX_MESSAGES) if message_count else 1,
        math.ceil(candidate_count / MAX_CANDIDATES) if candidate_count else 1,
        math.ceil(evidence_count / MAX_EVIDENCE) if evidence_count else 1,
    )
    cartesian_bound = (
        (math.ceil(message_count / MAX_MESSAGES) if message_count else 1)
        * (math.ceil(candidate_count / MAX_CANDIDATES) if candidate_count else 1)
        * (math.ceil(evidence_count / MAX_EVIDENCE) if evidence_count else 1)
    )
    observed = len(pages)
    if not observed:
        explicit_page_count = max(
            (_numeric(row, ("page_count", "materialized_page_count", "pages_count")) or 0)
            for row in rows
        ) if rows else 0
        observed = explicit_page_count
    max_page_dimensions = {
        key: max((int(item.get(key, 0)) for item in page_dimensions), default=0)
        for key in ("message", "candidate", "evidence")
    }
    page_chunk_limits_ok = (
        max_page_dimensions["message"] <= MAX_MESSAGES
        and max_page_dimensions["candidate"] <= MAX_CANDIDATES
        and max_page_dimensions["evidence"] <= MAX_EVIDENCE
    )
    complete = bool(rows) and observed > 0 and bool(
        authoritative_pages
        or handles["message"]
        or handles["candidate"]
        or handles["evidence"]
        or explicit_counts["message"]
        or explicit_counts["candidate"]
        or explicit_counts["evidence"]
    )
    linear_ok = complete and page_chunk_limits_ok and observed == linear_bound and observed <= cartesian_bound
    return {
        "observed_page_count": observed,
        "linear_page_bound": linear_bound,
        "cartesian_page_bound": cartesian_bound,
        "message_handle_count": message_handle_count,
        "candidate_handle_count": candidate_handle_count,
        "evidence_handle_count": evidence_handle_count,
        "message_count": message_count,
        "candidate_count": candidate_count,
        "evidence_count": evidence_count,
        "max_page_message_count": max_page_dimensions["message"],
        "max_page_candidate_count": max_page_dimensions["candidate"],
        "max_page_evidence_count": max_page_dimensions["evidence"],
        "page_chunk_limits_ok": page_chunk_limits_ok,
        "accounting_complete": complete,
        "linear_no_cartesian_explosion": linear_ok,
        "page_refs": sorted(pages),
    }


def _output_file(artifact_dir: Path, manifest: Mapping[str, Any], logical: str, candidates: Sequence[str]) -> Path:
    names: List[str] = list(candidates)
    output_files = manifest.get("output_files")
    if isinstance(output_files, Mapping):
        for key, value in output_files.items():
            if logical in str(key).casefold() or logical in str(value).casefold():
                names.append(str(value))
    for name in names:
        candidate = artifact_dir / name
        if candidate.is_file() and candidate.resolve().parent == artifact_dir.resolve():
            return candidate
    raise FileNotFoundError(f"K9 artifact has no {logical} map")


def _assert_target_manifest(artifact_dir: Path) -> Tuple[Dict[str, Any], Path, Path]:
    artifact_dir = _safe_path(artifact_dir)
    manifest_path = artifact_dir / "manifest.private.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"target K9 artifact is not complete: {manifest_path}")
    manifest = _load_json(manifest_path)
    if str(manifest.get("artifact_version") or "") != TARGET_ARTIFACT_VERSION:
        raise ValueError("refusing to audit a non-K9 linear stage-packet artifact")
    status = str(manifest.get("status") or "").casefold()
    if not status.startswith("complete"):
        raise ValueError(f"target K9 artifact is not complete: {status or 'missing status'}")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, "2026-08-25"):
        raise ValueError("target K9 artifact is not the 2026-08-25 development run")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise ValueError("target K9 artifact reports frozen/gold reads")
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("target K9 artifact reports provider calls")
    selection = manifest.get("selection") if isinstance(manifest.get("selection"), Mapping) else {}
    selected_count = _first(selection, ("selected_root_count", "selected_packet_count", "selected_root_limit"))
    if selected_count in (None, ""):
        selected_count = _first(manifest, ("selected_root_count", "selected_packet_count", "selection_limit"))
    if int(selected_count or 0) != SELECTION_LIMIT:
        raise ValueError("target K9 selection is not exactly the same 20-root scope")
    materialized = _output_file(
        artifact_dir,
        manifest,
        "materialized",
        ("materialized_map.private.jsonl", "materialization_map.private.jsonl", "stage_materialized_map.private.jsonl"),
    )
    recovery = _output_file(
        artifact_dir,
        manifest,
        "recovery",
        ("recovery_map.private.jsonl", "recover_map.private.jsonl", "recovery_mapping.private.jsonl"),
    )
    return manifest, materialized, recovery


def _load_k5_baseline(k5_dir: Path) -> Dict[str, Any]:
    k5_dir = _safe_path(k5_dir)
    manifest_path = k5_dir / "manifest.private.json"
    queue_path = k5_dir / "audit_queue.private.jsonl"
    human_path = k5_dir / "audit" / "human_audit.private.jsonl"
    packet_path = k5_dir / "packets.private.jsonl"
    for path in (manifest_path, queue_path, human_path, packet_path):
        if not path.is_file():
            raise FileNotFoundError(f"K5 baseline file missing: {path}")
    manifest = _load_json(manifest_path)
    if str(manifest.get("artifact_version") or "") != K5_ARTIFACT_VERSION:
        raise ValueError("K5 baseline artifact version mismatch")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True or manifest.get("provider_called") is True:
        raise ValueError("K5 baseline reports forbidden reads/calls")
    queue: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(queue_path):
        target = str(row.get("target_ref") or "")
        packet = str(row.get("packet_id") or "")
        if target and packet:
            queue[target] = row
    human: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(human_path):
        target = str(row.get("target_ref") or "")
        if target:
            human[target] = row
    if len(queue) != SELECTION_LIMIT or len(human) != SELECTION_LIMIT or set(queue) != set(human):
        raise ValueError("K5 baseline does not contain exactly the same 20 selected roots")
    packet_ids = {str(row["packet_id"]) for row in queue.values()}
    packets: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(packet_path):
        packet_id = str(row.get("packet_id") or "")
        if packet_id in packet_ids:
            if packet_id in packets:
                raise ValueError(f"duplicate selected K5 packet: {_opaque(packet_id, 'packet')}")
            packets[packet_id] = row
    if set(packets) != packet_ids:
        raise ValueError("K5 selected root packets are incomplete")
    by_target: Dict[str, Dict[str, Any]] = {}
    for target, queue_row in queue.items():
        packet = packets[str(queue_row["packet_id"])]
        human_row = human[target]
        context = human_row.get("context_review") if isinstance(human_row.get("context_review"), Mapping) else {}
        distractor = human_row.get("distractor_review") if isinstance(human_row.get("distractor_review"), Mapping) else {}
        window = _baseline_packet_window(packet)
        by_target[target] = {
            "target_ref": target,
            "sample_ordinal": int(human_row.get("sample_ordinal") or queue_row.get("selection_rank") or 0),
            "selection_rank": int(queue_row.get("selection_rank") or human_row.get("sample_ordinal") or 0),
            "packet_id": str(queue_row["packet_id"]),
            "packet_hash": str(queue_row.get("packet_hash") or ""),
            "bucket": tuple(str(value) for value in (queue_row.get("bucket") or [])),
            "required_units": int(context.get("required_context_units") or 0),
            "baseline_preserved_units": int(context.get("preserved_context_units") or 0),
            "known_distractors": int(distractor.get("known_unrelated_count") or 0),
            "reviewed_units": int(distractor.get("reviewed_window_units") or 0),
            "window": window,
        }
    return {
        "manifest": manifest,
        "manifest_sha256": _sha256_file(manifest_path),
        "queue_sha256": _sha256_file(queue_path),
        "human_sha256": _sha256_file(human_path),
        "packet_sha256": _sha256_file(packet_path),
        "by_target": by_target,
    }


def _alias_variants(value: Any, namespace: str) -> Set[str]:
    result = _normalize_identifier(value)
    if value not in (None, ""):
        result.add(_opaque(value, namespace))
    return result


def _page_alias_variants(value: Any) -> Set[str]:
    """Return page aliases without the ambiguous ordinal-only suffix."""

    if value in (None, ""):
        return set()
    text = str(value)
    result = {text, _opaque(text, "page")}
    for marker in ("|page|", "|subpacket|"):
        if marker in text:
            left, right = text.split(marker, 1)
            if left:
                result.add(left)
            if right:
                result.add(_opaque(left or text, "root"))
    return result


def _stream_target_maps(
    materialized_path: Path,
    recovery_path: Path,
    manifest: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    by_target = baseline["by_target"]
    aliases: Dict[str, Set[str]] = defaultdict(set)
    ranks: Dict[int, str] = {}
    for target, info in by_target.items():
        values = (target, info["packet_id"], info.get("packet_hash"))
        for value in values:
            if value in (None, ""):
                continue
            for variant in _alias_variants(value, "target") | _alias_variants(value, "root") | _alias_variants(value, "packet") | _alias_variants(value, "source"):
                aliases[variant].add(target)
        rank = int(info.get("selection_rank") or info.get("sample_ordinal") or 0)
        if rank:
            ranks[rank] = target

    materialized: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    recovery: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    page_aliases: Dict[str, Set[str]] = defaultdict(set)
    materialized_rows = 0
    recovery_rows = 0
    ambiguous_materialized_rows = 0
    ambiguous_recovery_rows = 0

    def match(row: Mapping[str, Any]) -> Set[str]:
        matched: Set[str] = set()
        for alias in _lineage_aliases(row):
            matched.update(aliases.get(alias, ()))
            matched.update(page_aliases.get(alias, ()))
        rank = _row_selection_rank(row)
        if not matched and rank in ranks:
            matched.add(ranks[rank])
        return matched

    for row in _iter_jsonl(materialized_path):
        materialized_rows += 1
        matched = match(row)
        if len(matched) > 1:
            ambiguous_materialized_rows += 1
            continue
        for target in matched:
            materialized[target].append(row)
            for page in _page_refs(row):
                for variant in _page_alias_variants(page):
                    page_aliases[variant].add(target)
    for row in _iter_jsonl(recovery_path):
        recovery_rows += 1
        matched = match(row)
        if len(matched) > 1:
            ambiguous_recovery_rows += 1
            continue
        for target in matched:
            recovery[target].append(row)
            for page in _page_refs(row):
                for variant in _page_alias_variants(page):
                    page_aliases[variant].add(target)
    return materialized, recovery, {
        "materialized_rows_streamed": materialized_rows,
        "recovery_rows_streamed": recovery_rows,
        "matched_materialized_root_count": sum(bool(materialized.get(target)) for target in by_target),
        "matched_recovery_root_count": sum(bool(recovery.get(target)) for target in by_target),
        "selected_lineage_rows_retained": sum(len(materialized.get(target, ())) + len(recovery.get(target, ())) for target in by_target),
        "ambiguous_materialized_rows": ambiguous_materialized_rows,
        "ambiguous_recovery_rows": ambiguous_recovery_rows,
    }


def _row_evidence_ids(rows: Sequence[Mapping[str, Any]]) -> Set[str]:
    return _reference_ids(rows, set(EVIDENCE_FIELDS) - {"source_refs", "source_ref_ids", "source_references", "all_source_refs"})


def _scenario_verdicts(info: Mapping[str, Any], metrics: Mapping[str, Any]) -> Dict[str, str]:
    buckets = set(info["bucket"])
    key_ok = not metrics["missing_key_ids"]
    adjacent_ok = not metrics["missing_adjacent_ids"]
    evidence_ok = not metrics["missing_evidence_ids"] and metrics["evidence_ok"]
    candidates = metrics["candidate_layers"]
    baseline_candidates = metrics["baseline_candidates"]
    results: Dict[str, str] = {}
    results["greeting_to_new_topic"] = "pass" if "greeting_to_new_topic" not in buckets or key_ok else "fail"
    results["no_reply_continuation"] = "pass" if "no_reply_continuation" not in buckets or key_ok else "fail"
    results["adjacent_context_only"] = "pass" if not info["window"]["adjacent_ids"] or adjacent_ok else "fail"
    results["media_or_context_only"] = "pass" if "media_or_context_only" not in buckets or (key_ok and evidence_ok) else "fail"
    results["person_history"] = "pass" if "person_history" not in buckets or candidates.get("person", 0) > 0 else "fail"
    results["object_history"] = "pass" if "object_history" not in buckets or candidates.get("object", 0) > 0 else "fail"
    results["state_update"] = "pass" if "state_update" not in buckets or candidates.get("state", 0) > 0 else "fail"
    results["pronoun_or_ellipsis"] = "pass" if "pronoun_or_ellipsis" not in buckets or key_ok else "fail"
    results["topic_shift"] = "pass" if "topic_shift" not in buckets or key_ok else "fail"
    results["long_gap_open_boundary"] = "pass" if "long_gap_open_boundary" not in buckets or metrics["open_boundary_preserved"] else "fail"
    baseline_any = sum(baseline_candidates.values())
    results["candidate_competition"] = "pass" if "candidate_competition" not in buckets or sum(candidates.values()) > 0 else "fail"
    if baseline_any == 0:
        results["candidate_competition"] = "N/A" if "candidate_competition" in buckets else results["candidate_competition"]
    return results


def _audit_one(info: Mapping[str, Any], materialized: Sequence[Mapping[str, Any]], recovery: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    all_rows = list(materialized) + list(recovery)
    window = info["window"]
    retained_ids = _message_ids(all_rows)
    key_ids = set(window["key_ids"])
    adjacent_ids = set(window["adjacent_ids"])
    evidence_ids = _row_evidence_ids(all_rows)
    baseline_evidence = set(window["evidence_ids"])
    missing_key_ids = key_ids - retained_ids
    missing_adjacent_ids = adjacent_ids - retained_ids
    missing_evidence_ids = baseline_evidence - evidence_ids
    evidence_obs = _reference_observation(all_rows, "evidence")
    source_obs = _reference_observation(all_rows, "source")
    if not evidence_ids and evidence_obs["rate_recovered"] >= len(baseline_evidence) and baseline_evidence:
        missing_evidence_ids = set()
    key_ratio = 1.0 if not key_ids else len(key_ids & retained_ids) / len(key_ids)
    required = int(info["required_units"])
    preserved = required if not missing_key_ids else int(required * key_ratio)
    candidate_layers = _candidate_layer_presence(all_rows)
    baseline_candidates = _candidate_layer_presence({
        "candidate_qa_links": [],
    })
    # The baseline packet is not emitted, but its candidate layer counts are
    # retained in the private in-memory info created below by run_audit.
    baseline_candidates.update(info.get("baseline_candidates", {}))
    stage_a = _stage_a_envelopes(materialized or recovery)
    stage_a_errors: List[str] = []
    for envelope in stage_a:
        stats = envelope["stats"]
        if any(stats.get(key) is None for key in ("input_token_proxy", "user_token_proxy", "message_count", "candidate_count", "evidence_count")):
            stage_a_errors.append("STAGE_A_ACCOUNTING_INCOMPLETE")
        if envelope["status"] in {"pending", "deferred", "blocked", "over_capacity"}:
            stage_a_errors.append("STAGE_A_NOT_COMPLETE")
        if any(
            stats.get(key) is not None and int(stats[key]) > limit
            for key, limit in (
                ("input_token_proxy", MAX_INPUT_TOKENS),
                ("user_token_proxy", MAX_USER_TOKENS),
                ("message_count", MAX_MESSAGES),
                ("candidate_count", MAX_CANDIDATES),
                ("evidence_count", MAX_EVIDENCE),
            )
        ):
            stage_a_errors.append("STAGE_A_OVER_BUDGET")
    stage_a_max = {
        key: max((int(item["stats"][key]) for item in stage_a if item["stats"].get(key) is not None), default=0)
        for key in ("input_token_proxy", "user_token_proxy", "message_count", "candidate_count", "evidence_count")
    }
    stage_a_ok = bool(stage_a) and not stage_a_errors
    pending_rows = [row for row in all_rows if _is_pending(row)]
    pending_unrecoverable = [row for row in pending_rows if not (_has_snapshot(row) and _has_replay_handles(row))]
    pending_ok = not pending_unrecoverable
    boundary = _boundary_stats(all_rows)
    boundary["cross_chat_violations"] += _scope_violations(all_rows, set(window["scope"]))
    page = _page_metrics(all_rows)
    source_ok = source_obs["id_count"] > 0 or source_obs["numeric_count"] > 0 or source_obs["rate_recovered"] > 0
    # Evidence is a vacuous pass when the K5 baseline declares no evidence
    # refs for this root.  For roots that do declare refs, accept either
    # opaque ids or the recovery map's explicit recovered/expected counts.
    evidence_ok = not baseline_evidence or (
        (bool(evidence_ids) and not missing_evidence_ids)
        or evidence_obs["rate_recovered"] >= len(baseline_evidence)
    )
    # The recovery packet nests its open-boundary marker under
    # ``recovered_packet``; inspect the whole body-free structure rather than
    # only the transport wrapper.
    open_boundary = _open_boundary_preserved(all_rows)
    metrics: Dict[str, Any] = {
        "missing_key_ids": missing_key_ids,
        "missing_adjacent_ids": missing_adjacent_ids,
        "missing_evidence_ids": missing_evidence_ids,
        "evidence_ok": evidence_ok,
        "candidate_layers": candidate_layers,
        "baseline_candidates": baseline_candidates,
        "open_boundary_preserved": open_boundary,
    }
    scenarios = _scenario_verdicts(info, metrics)
    irreversible = len(missing_key_ids) + len(missing_evidence_ids)
    errors: List[str] = []
    if not materialized:
        errors.append("MATERIALIZED_ROOT_NOT_FOUND")
    if not recovery:
        errors.append("RECOVERY_ROOT_NOT_FOUND")
    if missing_key_ids:
        errors.append("BASELINE_KEY_CONTEXT_LOST")
    if required and preserved / required < MIN_RECALL:
        errors.append("CONTEXT_RECALL_BELOW_0_90")
    if irreversible:
        errors.append("IRREVERSIBLE_VALUE_LOSS")
    if not source_ok:
        errors.append("SOURCE_REF_NOT_RECOVERABLE")
    if not evidence_ok:
        errors.append("EVIDENCE_NOT_RECOVERABLE")
    if not stage_a_ok:
        errors.extend(stage_a_errors or ["STAGE_A_ACCOUNTING_UNMEASURED"])
    if not pending_ok:
        errors.append("PENDING_NOT_RECOVERABLE")
    if not page["linear_no_cartesian_explosion"]:
        errors.append("PAGE_COUNT_NOT_LINEAR")
    if boundary["cross_chat_violations"]:
        errors.append("CROSS_CHAT_SCOPE_VIOLATION")
    if boundary["time_or_same_segment_strong_violations"]:
        errors.append("TIME_OR_SEGMENT_STRONG_RELATION")
    if boundary["local_final_semantic_decisions"]:
        errors.append("LOCAL_FINAL_SEMANTIC_DECISION")
    errors.extend(f"SCENARIO_{name.upper()}" for name, result in scenarios.items() if result == "fail")
    status = "pass" if not errors else "fail"
    page_refs = page["page_refs"]
    return {
        "record_type": "linear_stage_packet_k9_private_material_audit",
        "sample_ordinal": int(info["sample_ordinal"]),
        "target_ref": _opaque(info["target_ref"], "target"),
        "root_ref": _opaque(info["packet_id"], "root"),
        "status": status,
        "materialized_row_count": len(materialized),
        "recovery_row_count": len(recovery),
        "page_refs": sorted(_opaque(value, "page") for value in page_refs),
        "baseline": {
            "required_context_units": required,
            "baseline_preserved_context_units": int(info["baseline_preserved_units"]),
            "known_distractor_count": int(info["known_distractors"]),
            "reviewed_context_units": int(info["reviewed_units"]),
            "window_message_count": len(window["window_ids"]),
            "key_window_message_count": len(key_ids),
            "evidence_ref_count": len(baseline_evidence),
        },
        "context_recall": {
            "required_context_units": required,
            "preserved_context_units": preserved,
            "rate": round(preserved / required, 4) if required else "N/A",
            "missing_key_message_count": len(missing_key_ids),
            "missing_key_opaque_refs": sorted(_opaque(value, "message") for value in missing_key_ids),
            "minimum_threshold": MIN_RECALL,
        },
        "retention": {
            "retained_message_count": len(retained_ids),
            "retained_primary_message_count": len(_layer_message_ids(all_rows, "primary")),
            "retained_adjacent_message_count": len(_layer_message_ids(all_rows, "adjacent")),
            "adjacent_context_only_preserved": not missing_adjacent_ids,
            "evidence_preserved": evidence_ok,
            "missing_evidence_count": len(missing_evidence_ids),
            "missing_evidence_opaque_refs": sorted(_opaque(value, "evidence") for value in missing_evidence_ids),
            "irreversible_loss_count": irreversible,
        },
        "scenario_retention": scenarios,
        "candidate_layers": candidate_layers,
        "source_evidence": {
            "source_ref_count": max(source_obs["id_count"], source_obs["numeric_count"], source_obs["rate_recovered"]),
            "evidence_ref_count": max(evidence_obs["id_count"], evidence_obs["numeric_count"], evidence_obs["rate_recovered"]),
            "source_recovery_ok": source_ok,
            "evidence_recovery_ok": evidence_ok,
            "pending_row_count": len(pending_rows),
            "pending_unrecoverable_count": len(pending_unrecoverable),
        },
        "stage_a": {
            "envelope_count": len(stage_a),
            "complete": stage_a_ok,
            "max_input_token_proxy": stage_a_max["input_token_proxy"],
            "max_user_token_proxy": stage_a_max["user_token_proxy"],
            "max_message_count": stage_a_max["message_count"],
            "max_candidate_count": stage_a_max["candidate_count"],
            "max_evidence_count": stage_a_max["evidence_count"],
            "limits": {
                "max_input_token_proxy": MAX_INPUT_TOKENS,
                "max_user_token_proxy": MAX_USER_TOKENS,
                "max_messages": MAX_MESSAGES,
                "max_candidates": MAX_CANDIDATES,
                "max_evidence": MAX_EVIDENCE,
            },
            "error_count": len(stage_a_errors),
        },
        "linear_paging": {
            key: value for key, value in page.items() if key != "page_refs"
        },
        "distractor_review": {
            "known_unrelated_count": int(info["known_distractors"]),
            "reviewed_context_units": int(info["reviewed_units"]),
            "rate": round(int(info["known_distractors"]) / int(info["reviewed_units"]), 4) if int(info["reviewed_units"]) else "N/A",
            "not_promoted_to_strong_relation": boundary["time_or_same_segment_strong_violations"] == 0,
        },
        "zero_tolerance": {
            "cross_chat_violations": boundary["cross_chat_violations"],
            "time_or_same_segment_strong_relation_violations": boundary["time_or_same_segment_strong_violations"],
            "local_final_semantic_decisions": boundary["local_final_semantic_decisions"],
        },
        "error_codes": sorted(set(errors)),
    }


def run_audit(artifact_dir: Path = DEFAULT_ARTIFACT_DIR, k5_dir: Path = DEFAULT_K5_DIR) -> Dict[str, Any]:
    artifact_dir = _safe_path(Path(artifact_dir))
    k5_dir = _safe_path(Path(k5_dir))
    manifest, materialized_path, recovery_path = _assert_target_manifest(artifact_dir)
    baseline = _load_k5_baseline(k5_dir)
    materialized_rows, recovery_rows, stream_meta = _stream_target_maps(materialized_path, recovery_path, manifest, baseline)

    # Add only non-emitted baseline candidate metadata to each in-memory root.
    # This is derived from the private K5 packet and never reaches JSON output.
    packet_path = k5_dir / "packets.private.jsonl"
    selected_ids = {str(info["packet_id"]): target for target, info in baseline["by_target"].items()}
    selected_packets: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(packet_path):
        packet_id = str(row.get("packet_id") or "")
        if packet_id in selected_ids:
            selected_packets[packet_id] = row
    for target, info in baseline["by_target"].items():
        packet = selected_packets[str(info["packet_id"])]
        info["baseline_candidates"] = _candidate_layer_presence(packet)

    ordered_targets = sorted(
        baseline["by_target"],
        key=lambda target: (int(baseline["by_target"][target].get("sample_ordinal") or 0), target),
    )
    audit_rows: List[Dict[str, Any]] = []
    for target in ordered_targets:
        audit_rows.append(
            _audit_one(
                baseline["by_target"][target],
                materialized_rows.get(target, ()),
                recovery_rows.get(target, ()),
            )
        )

    total_required = sum(int(row["context_recall"]["required_context_units"]) for row in audit_rows)
    total_preserved = sum(int(row["context_recall"]["preserved_context_units"]) for row in audit_rows)
    total_irreversible = sum(int(row["retention"]["irreversible_loss_count"]) for row in audit_rows)
    recall = total_preserved / total_required if total_required else 0.0
    scenario_names = sorted({name for row in audit_rows for name in row["scenario_retention"]})
    scenario_summary = {
        name: {
            "sample_count": sum(row["scenario_retention"].get(name) in {"pass", "fail"} for row in audit_rows),
            "pass_count": sum(row["scenario_retention"].get(name) == "pass" for row in audit_rows),
            "fail_count": sum(row["scenario_retention"].get(name) == "fail" for row in audit_rows),
            "status": "pass" if all(row["scenario_retention"].get(name) != "fail" for row in audit_rows) else "fail",
        }
        for name in scenario_names
    }
    stage_rows = [row["stage_a"] for row in audit_rows]
    stage_a_ok = all(bool(row["complete"]) for row in stage_rows) and bool(stage_rows)
    page_rows = [row["linear_paging"] for row in audit_rows]
    paging_ok = all(bool(row["linear_no_cartesian_explosion"]) for row in page_rows) and bool(page_rows)
    source_ok = all(bool(row["source_evidence"]["source_recovery_ok"]) for row in audit_rows)
    evidence_ok = all(bool(row["source_evidence"]["evidence_recovery_ok"]) for row in audit_rows)
    pending_ok = all(int(row["source_evidence"]["pending_unrecoverable_count"]) == 0 for row in audit_rows)
    zero = {
        "cross_chat_violations": sum(int(row["zero_tolerance"]["cross_chat_violations"]) for row in audit_rows),
        "time_or_same_segment_strong_relation_violations": sum(int(row["zero_tolerance"]["time_or_same_segment_strong_relation_violations"]) for row in audit_rows),
        "local_final_semantic_decisions": sum(int(row["zero_tolerance"]["local_final_semantic_decisions"]) for row in audit_rows),
    }
    zero_ok = not any(zero.values())
    scenarios_ok = all(value["status"] == "pass" for value in scenario_summary.values())
    evidence_expected_root_count = sum(
        int(row["baseline"]["evidence_ref_count"] > 0) for row in audit_rows
    )
    evidence_recovered_expected_root_count = sum(
        int(
            row["baseline"]["evidence_ref_count"] > 0
            and row["source_evidence"]["evidence_recovery_ok"]
        )
        for row in audit_rows
    )
    evidence_vacuous_root_count = len(audit_rows) - evidence_expected_root_count
    root_scope_ok = (
        len(audit_rows) == SELECTION_LIMIT
        and stream_meta["matched_materialized_root_count"] == SELECTION_LIMIT
        and stream_meta["matched_recovery_root_count"] == SELECTION_LIMIT
        and stream_meta["ambiguous_materialized_rows"] == 0
        and stream_meta["ambiguous_recovery_rows"] == 0
    )
    allow_deepseek_stage_a_pilot = bool(
        root_scope_ok
        and recall >= MIN_RECALL
        and total_irreversible == 0
        and source_ok
        and evidence_ok
        and stage_a_ok
        and pending_ok
        and paging_ok
        and scenarios_ok
        and zero_ok
    )
    errors: List[str] = []
    if not root_scope_ok:
        errors.append("SELECTED_ROOT_SCOPE_NOT_EXACTLY_20")
    if recall < MIN_RECALL:
        errors.append("CONTEXT_RECALL_BELOW_0_90")
    if total_irreversible:
        errors.append("IRREVERSIBLE_VALUE_LOSS")
    if not source_ok or not evidence_ok:
        errors.append("SOURCE_OR_EVIDENCE_NOT_RECOVERABLE")
    if not stage_a_ok:
        errors.append("STAGE_A_BUDGET_GATE")
    if not pending_ok:
        errors.append("PENDING_RECOVERY_GATE")
    if not paging_ok:
        errors.append("LINEAR_PAGE_GATE")
    if not scenarios_ok:
        errors.append("SCENARIO_RETENTION_GATE")
    if not zero_ok:
        errors.append("ZERO_TOLERANCE_GATE")

    audit_dir = artifact_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "status": "pass" if allow_deepseek_stage_a_pilot else "blocked_by_k9_gate",
        "scope": {
            "artifact_version": TARGET_ARTIFACT_VERSION,
            "artifact_ref": _opaque(_sha256_file(artifact_dir / "manifest.private.json"), "manifest"),
            "k5_baseline_version": K5_ARTIFACT_VERSION,
            "k5_baseline_ref": _opaque(baseline["manifest_sha256"], "k5_manifest"),
            "materialized_map_ref": _opaque(_sha256_file(materialized_path), "materialized_map"),
            "recovery_map_ref": _opaque(_sha256_file(recovery_path), "recovery_map"),
            "frozen_read": False,
            "gold_loaded": False,
            "provider_called": False,
            "body_free": True,
        },
        "sample": {
            "selected_root_count": len(audit_rows),
            "selected_root_limit": SELECTION_LIMIT,
            "matched_materialized_root_count": stream_meta["matched_materialized_root_count"],
            "matched_recovery_root_count": stream_meta["matched_recovery_root_count"],
            "materialized_map_rows_streamed": stream_meta["materialized_rows_streamed"],
            "recovery_map_rows_streamed": stream_meta["recovery_rows_streamed"],
            "selected_lineage_rows_retained": stream_meta["selected_lineage_rows_retained"],
            "ambiguous_materialized_rows": stream_meta["ambiguous_materialized_rows"],
            "ambiguous_recovery_rows": stream_meta["ambiguous_recovery_rows"],
            "scoring": "independent_k9_linear_material_review",
        },
        "comparison_to_k5": {
            "key_context_baseline": "46/46",
            "baseline_required_context_units": total_required,
            "preserved_context_units": total_preserved,
            "context_recall_rate": round(recall, 4),
            "minimum_threshold": MIN_RECALL,
            "known_distractor_baseline": "1/100",
            "known_unrelated_context_units": sum(int(info["known_distractors"]) for info in baseline["by_target"].values()),
            "reviewed_context_units": sum(int(info["reviewed_units"]) for info in baseline["by_target"].values()),
        },
        "context_recall": {
            "required_context_units": total_required,
            "preserved_context_units": total_preserved,
            "rate": round(recall, 4),
            "minimum_threshold": MIN_RECALL,
            "gate": "pass" if recall >= MIN_RECALL else "fail",
            "definition": "K5-reviewed key context is considered retained only when every baseline key-window message ref is present in K9 materialized/recovery maps",
        },
        "scenario_retention": scenario_summary,
        "distractor_review": {
            "known_unrelated_context_units": sum(int(info["known_distractors"]) for info in baseline["by_target"].values()),
            "reviewed_context_units": sum(int(info["reviewed_units"]) for info in baseline["by_target"].values()),
            "baseline_rate": 0.01,
            "not_promoted_to_strong_relation": zero["time_or_same_segment_strong_relation_violations"] == 0,
            "status": "pass" if zero["time_or_same_segment_strong_relation_violations"] == 0 else "fail",
        },
        "source_evidence_recovery": {
            "source_status": "pass" if source_ok else "fail",
            "evidence_status": "pass" if evidence_ok else "fail",
            "pending_status": "pass" if pending_ok else "fail",
            "source_recoverable_root_count": sum(bool(row["source_evidence"]["source_recovery_ok"]) for row in audit_rows),
            "evidence_recoverable_root_count": sum(bool(row["source_evidence"]["evidence_recovery_ok"]) for row in audit_rows),
            "evidence_expected_root_count": evidence_expected_root_count,
            "evidence_recovered_expected_root_count": evidence_recovered_expected_root_count,
            "evidence_vacuous_root_count": evidence_vacuous_root_count,
            "pending_unrecoverable_root_count": sum(int(row["source_evidence"]["pending_unrecoverable_count"]) > 0 for row in audit_rows),
        },
        "stage_a": {
            "complete_root_count": sum(bool(row["complete"]) for row in stage_rows),
            "selected_root_count": len(stage_rows),
            "all_complete": stage_a_ok,
            "max_input_token_proxy": max((int(row["max_input_token_proxy"]) for row in stage_rows), default=0),
            "max_user_token_proxy": max((int(row["max_user_token_proxy"]) for row in stage_rows), default=0),
            "limits": {
                "max_input_token_proxy": MAX_INPUT_TOKENS,
                "max_user_token_proxy": MAX_USER_TOKENS,
                "max_messages": MAX_MESSAGES,
                "max_candidates": MAX_CANDIDATES,
                "max_evidence": MAX_EVIDENCE,
            },
            "gate": "pass" if stage_a_ok else "fail",
        },
        "linear_paging": {
            "all_roots_linear": paging_ok,
            "max_observed_page_count": max((int(row["observed_page_count"]) for row in page_rows), default=0),
            "max_linear_page_bound": max((int(row["linear_page_bound"]) for row in page_rows), default=0),
            "max_cartesian_page_bound": max((int(row["cartesian_page_bound"]) for row in page_rows), default=0),
            "max_message_count": max((int(row["message_count"]) for row in page_rows), default=0),
            "max_candidate_count": max((int(row["candidate_count"]) for row in page_rows), default=0),
            "max_evidence_count": max((int(row["evidence_count"]) for row in page_rows), default=0),
            "gate": "pass" if paging_ok else "fail",
        },
        "zero_tolerance": {
            **zero,
            "cross_chat_local_final_time_only_strong_zero": zero_ok,
            "status": "pass" if zero_ok else "fail",
            "sampled_only": True,
        },
        "irreversible_loss": {
            "count": total_irreversible,
            "status": "pass" if total_irreversible == 0 else "fail",
        },
        "deepseek_stage_a_pilot": {
            "allow_deepseek_stage_a_pilot": allow_deepseek_stage_a_pilot,
            "provider_called": False,
            "reason": "all K9 key-context, recovery, Stage A budget, linear-page, scenario, and zero-tolerance gates pass" if allow_deepseek_stage_a_pilot else "one or more K9 gates failed",
        },
        "allow_deepseek_stage_a_pilot": allow_deepseek_stage_a_pilot,
        "errors": sorted(set(errors)),
        "privacy_checks": {
            "body_key_count": 0,
            "identity_key_count": 0,
            "frozen_path_read": False,
            "provider_calls": 0,
            "output_refs": "opaque_only",
        },
    }
    body_hits = _nested_key_scan([audit_rows, summary], {value.casefold() for value in BODY_KEYS})
    identity_hits = _nested_key_scan([audit_rows, summary], {value.casefold() for value in IDENTITY_KEYS})
    if body_hits or identity_hits:
        raise RuntimeError(f"K9 private audit output privacy check failed: body={body_hits[:3]} identity={identity_hits[:3]}")
    _write_jsonl(audit_dir / "human_audit.private.jsonl", audit_rows)
    _write_json(audit_dir / "audit_summary.private.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--k5-dir", type=Path, default=DEFAULT_K5_DIR)
    args = parser.parse_args()
    summary = run_audit(args.artifact_dir, args.k5_dir)
    print(
        json.dumps(
            {
                "ok": summary["status"] == "pass",
                "status": summary["status"],
                "selected_roots": summary["sample"]["selected_root_count"],
                "recall": summary["context_recall"]["rate"],
                "irreversible_loss": summary["irreversible_loss"]["count"],
                "allow_deepseek_stage_a_pilot": summary["allow_deepseek_stage_a_pilot"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
