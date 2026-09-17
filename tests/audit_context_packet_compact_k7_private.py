"""Independent, private K7 review for compact context-packet material.

This is an audit tool, not a production component.  It is intentionally
independent from the K5 audit implementation and never calls a provider,
loads frozen/gold material, or reads the development message source.  The
compact artifact is accepted only after its manifest says that the exact
``context_packet_compact_development_v1`` run is complete.

The review is bounded to the same twenty K5 selected roots.  Selection and
recovery maps are read as JSONL streams and only rows belonging to those
roots (or their leaves) contribute to the review.  Body-bearing values may be
inspected in memory when a private recovery map contains them, but emitted
JSONL/JSON files contain only counts, verdicts, and one-way opaque refs.

The K5 baseline is used only to recover the selected-root lineage and the
already-reviewed 46 context units.  A missing root, message, source/evidence
ref, or envelope measurement is a conservative K7 failure.  ``allow_deepseek_pilot``
is true only when every selected leaf is recoverable, bounded, and passes all
zero-tolerance guards.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


DEFAULT_ARTIFACT_DIR = Path(
    "data/private/gold_standard/2026-08-25/context_packet_compact_development_v1"
)
DEFAULT_K5_DIR = Path(
    "data/private/gold_standard/2026-08-25/context_packet_development_v1"
)
TARGET_ARTIFACT_VERSION = "context_packet_compact_development_v1"
K5_ARTIFACT_VERSION = "context_packet_development_v1"
AUDIT_SCHEMA = "context_packet_compact_k7_private_audit_v1"
SELECTION_LIMIT = 20
MIN_RECALL = 0.90
MAX_TOKENS = 2000
MAX_MESSAGES = 24
MAX_CANDIDATES = 64
MAX_EVIDENCE = 64

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_text",
        "evidence_text",
        "fragment_text",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "surface_text",
        "summary",
        "text",
        "text_redacted",
        "title",
    }
)

# Output keys must not contain raw identifiers.  Opaque refs are emitted only
# under *_ref / *_refs names, and message/candidate/evidence *counts* are safe.
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
        "speaker_id",
        "source_id",
        "thread_id",
    }
)

MESSAGE_FIELDS = frozenset(
    {
        "message_ids",
        "all_message_ids",
        "source_message_ids",
        "context_message_ids",
        "window_message_ids",
        "retained_message_ids",
        "primary_message_ids",
        "adjacent_message_ids",
        "anchor_message_ids",
    }
)
PRIMARY_FIELDS = frozenset(
    {"primary_message_ids", "primary_ids", "primary_messages", "primary_refs", "primary_fragments"}
)
ADJACENT_FIELDS = frozenset(
    {"adjacent_message_ids", "adjacent_ids", "adjacent_messages", "adjacent_refs", "adjacent_context"}
)
CANDIDATE_FIELDS = frozenset(
    {
        "candidate_ids",
        "candidate_row_ids",
        "candidate_refs",
        "candidate_rows",
        "candidates",
        "continuity_candidates",
        "candidate_context",
        "candidate_view_ids",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "candidate_qa_links",
        "open_thread_candidates",
        "person_history",
        "object_history",
        "state_history",
        "qa_candidates",
        "open_threads",
    }
)
EVIDENCE_FIELDS = frozenset(
    {
        "evidence_ids",
        "evidence_ref_ids",
        "evidence_refs",
        "evidence",
        "source_refs",
        "source_ref_ids",
        "source_references",
        "all_source_refs",
    }
)
WEAK_REASON_CODES = frozenset(
    {"time_proximity_weak", "same_segment_weak", "time_only", "same_segment_only"}
)
TARGET_REF_FIELDS = frozenset(
    {"target_ref", "selection_ref", "root_ref", "selected_root_ref", "audit_ref", "k5_target_ref"}
)
ROOT_FIELDS = frozenset(
    {
        "root_packet_id",
        "root_id",
        "source_packet_id",
        "source_root_id",
        "source_id",
        "packet_id",
        "packet_ref",
        "root_ref",
        "parent_packet_id",
        "parent_id",
    }
)
LEAF_FIELDS = frozenset(
    {
        "leaf_packet_id",
        "leaf_id",
        "leaf_ref",
        "leaf_ids",
        "leaf_packet_ids",
        "subpacket_ids",
        "subpacket_refs",
        "leaves",
        "leaf_rows",
    }
)


def _safe_path(path: Path) -> Path:
    resolved = path.resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in resolved.parts):
        raise RuntimeError(f"K7 refuses frozen path: {resolved}")
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
    return f"{namespace}_{hashlib.sha256((namespace + '|' + raw).encode('utf-8')).hexdigest()[:20]}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _first(mapping: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
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
            key_text = str(key).casefold()
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in keys:
                found.append(child_path)
            found.extend(_nested_key_scan(child, keys, child_path))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            found.extend(_nested_key_scan(child, keys, f"{path}[{index}]"))
    return found


def _scope_pairs(value: Any) -> Set[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            account = _first(item, ("account_id", "account", "account_ref"))
            chat = _first(item, ("chat_id", "chat", "chat_ref"))
            if account not in (None, "") and chat not in (None, ""):
                pairs.add((str(account), str(chat)))
            scope = item.get("scope")
            if isinstance(scope, Mapping):
                sa = _first(scope, ("account_id", "account", "account_ref"))
                sc = _first(scope, ("chat_id", "chat", "chat_ref"))
                if sa not in (None, "") and sc not in (None, ""):
                    pairs.add((str(sa), str(sc)))
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return pairs


def _message_ids(value: Any, *, fields: Set[str] | frozenset[str] = MESSAGE_FIELDS) -> Set[str]:
    """Extract retained-window message IDs without treating candidate refs as window rows."""
    result: Set[str] = set()

    def visit(item: Any, key: str = "") -> None:
        key_text = key.casefold()
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                child_name = str(child_key)
                lowered = child_name.casefold()
                if lowered in fields:
                    for item_value in _values(child):
                        if isinstance(item_value, Mapping):
                            mid = _first(item_value, ("message_id", "id", "message_ref"))
                            if mid not in (None, ""):
                                result.add(str(mid))
                        elif item_value not in (None, ""):
                            result.add(str(item_value))
                elif lowered.endswith("_message_id") and not any(token in key_text for token in ("candidate", "evidence", "source_ref")):
                    if child not in (None, "") and not isinstance(child, (list, tuple, Mapping)):
                        result.add(str(child))
                # A fragment/message ref carries the ID in a mapping, while
                # candidate/evidence refs are intentionally not window rows.
                if lowered in {"primary_refs", "primary_fragments", "adjacent_refs", "adjacent_context", "messages", "message_refs", "window_refs"}:
                    visit_message_refs(child, result)
                elif lowered not in CANDIDATE_FIELDS and lowered not in EVIDENCE_FIELDS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, key_text)

    def visit_message_refs(item: Any, target: Set[str]) -> None:
        for row in _values(item):
            if isinstance(row, Mapping):
                mid = _first(row, ("message_id", "message_ref", "id"))
                if mid not in (None, ""):
                    target.add(str(mid))
            elif row not in (None, "") and not isinstance(row, (list, tuple)):
                target.add(str(row))

    visit(value)
    return result


def _message_ids_by_layer(value: Any, layer: str) -> Set[str]:
    result: Set[str] = set()
    keys = PRIMARY_FIELDS if layer == "primary" else ADJACENT_FIELDS

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in keys:
                    result.update(_message_ids({str(key): child}))
                elif isinstance(child, (Mapping, list, tuple)) and lowered not in CANDIDATE_FIELDS and lowered not in EVIDENCE_FIELDS:
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _ref_ids(value: Any, keys: Set[str] | frozenset[str]) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in keys:
                    for ref_value in _values(child):
                        if isinstance(ref_value, Mapping):
                            for nested_key in ("id", "ref", "key", "evidence_id", "candidate_id", "message_id", "fragment_id"):
                                nested = ref_value.get(nested_key)
                                if nested not in (None, ""):
                                    result.add(str(nested))
                        elif ref_value not in (None, ""):
                            result.add(str(ref_value))
                elif isinstance(child, (Mapping, list, tuple)):
                    visit(child, lowered)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, parent)

    visit(value)
    return result


def _all_ids(value: Any) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"packet_id", "root_packet_id", "parent_packet_id", "leaf_packet_id", "source_packet_id", "id", "ref", "key"}:
                    if not isinstance(child, (Mapping, list, tuple)) and child not in (None, ""):
                        result.add(str(child))
                elif isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _row_refs(row: Mapping[str, Any]) -> Set[str]:
    refs: Set[str] = set()
    for key in ROOT_FIELDS | LEAF_FIELDS | TARGET_REF_FIELDS:
        value = row.get(key)
        if isinstance(value, Mapping):
            refs.update(_all_ids(value))
        else:
            refs.update(_strings(value))
    refs.update(_strings(row.get("subpacket_ids")))
    refs.update(_strings(row.get("leaf_packet_ids")))
    refs.update(_strings(row.get("leaf_ids")))
    return refs


def _target_refs(row: Mapping[str, Any]) -> Set[str]:
    refs: Set[str] = set()
    for key in TARGET_REF_FIELDS:
        refs.update(_strings(row.get(key)))
    return refs


def _lineage_refs(row: Mapping[str, Any]) -> Tuple[Set[str], Set[str], Set[str]]:
    """Return (self, parents/roots, children/leaves) refs for a map row."""
    self_refs: Set[str] = set()
    parent_refs: Set[str] = set()
    child_refs: Set[str] = set()
    for key in ("packet_id", "leaf_packet_id", "leaf_id", "packet_ref", "leaf_ref"):
        self_refs.update(_strings(row.get(key)))
    for key in ("root_packet_id", "root_id", "source_packet_id", "source_root_id", "source_id", "root_ref", "parent_packet_id", "parent_id"):
        value = row.get(key)
        if value not in (None, ""):
            parent_refs.update(_strings(value))
    for key in ("subpacket_ids", "subpacket_refs", "leaf_ids", "leaf_packet_ids", "leaves"):
        value = row.get(key)
        if isinstance(value, Mapping):
            child_refs.update(_all_ids(value))
        else:
            child_refs.update(_strings(value))
    # A row with no explicit separate identity still has its packet ID as self.
    if not self_refs and row.get("packet_id") not in (None, ""):
        self_refs.add(str(row["packet_id"]))
    return self_refs, parent_refs, child_refs


def _status(row: Mapping[str, Any]) -> str:
    return str(_first(row, ("status", "material_status", "recovery_status", "state"), "unknown") or "unknown").casefold()


def _is_container(row: Mapping[str, Any]) -> bool:
    if row.get("is_container") is True:
        return True
    status = _status(row)
    return status in {"split", "container"} and bool(row.get("subpacket_ids") or row.get("leaf_packet_ids") or row.get("leaves"))


def _is_pending(row: Mapping[str, Any]) -> bool:
    return _status(row) in {"pending", "deferred", "blocked"} or bool(row.get("pending_reason"))


def _is_explicit_semantic_final(value: Any, key: str) -> bool:
    lowered = key.casefold()
    if lowered not in {
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
    strong_weak = 0
    local_final = 0

    def visit(item: Any) -> None:
        nonlocal cross_chat, strong_weak, local_final
        if isinstance(item, Mapping):
            reasons: Set[str] = set()
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"candidate_reason", "reason_codes", "supporting_slot_codes", "reasons"}:
                    reasons.update(str(value).casefold() for value in _strings(child))
                if _is_explicit_semantic_final(child, lowered):
                    local_final += 1
            strong = bool(_first(item, ("strong_relation", "is_strong", "strong"), False))
            relation = str(_first(item, ("relation_label", "relation", "relation_subtype"), "")).casefold()
            if strong or relation in {"resolved", "same_event", "strong"}:
                if reasons and reasons <= WEAK_REASON_CODES:
                    strong_weak += 1
            if any(str(key).casefold() in {"cross_chat", "cross_chat_violation", "cross_scope", "scope_mismatch"} and bool(child) for key, child in item.items()):
                cross_chat += 1
            # Explicit different scopes inside a relation row are a hard
            # violation even when the caller did not set a convenience flag.
            scopes = _scope_pairs(item)
            if len(scopes) > 1:
                cross_chat += 1
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return {
        "cross_chat_violations": cross_chat,
        "time_or_same_segment_strong_violations": strong_weak,
        "local_final_semantic_decisions": local_final,
    }


def _numeric_observation(value: Any, names: Sequence[str]) -> Optional[int]:
    if not isinstance(value, Mapping):
        return None
    for key in names:
        if key not in value:
            continue
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return int(raw)
        try:
            if isinstance(raw, str) and raw.strip():
                return int(float(raw))
        except ValueError:
            continue
    return None


def _list_count(value: Any, names: Sequence[str]) -> Optional[int]:
    if not isinstance(value, Mapping):
        return None
    for key in names:
        if key in value and isinstance(value[key], (list, tuple, set, frozenset)):
            return len(value[key])
    return None


def _envelope_metrics(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract explicit provider-envelope accounting from one recovery row."""
    token_names = (
        "token_proxy",
        "input_token_proxy",
        "estimated_token_proxy",
        "provider_token_proxy",
        "serialized_token_proxy",
        "estimated_tokens",
        "input_tokens",
        "token_count",
    )
    message_names = (
        "message_count",
        "messages_count",
        "window_message_count",
        "materialized_message_count",
        "provider_message_count",
    )
    candidate_names = (
        "candidate_count",
        "candidate_rows_count",
        "candidate_ref_count",
        "provider_candidate_count",
    )
    evidence_names = (
        "evidence_count",
        "evidence_refs_count",
        "evidence_ref_count",
        "provider_evidence_count",
    )
    rows: List[Mapping[str, Any]] = []

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, Mapping):
            lowered_keys = {str(name).casefold() for name in item}
            likely = (
                any(name in lowered_keys for name in token_names)
                or any(name in lowered_keys for name in message_names)
                or any(name in lowered_keys for name in candidate_names)
                or any(name in lowered_keys for name in evidence_names)
            )
            if likely:
                rows.append(item)
            for child_key, child in item.items():
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child, str(child_key).casefold())
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, key)

    visit(row)
    # A root row may report the four values directly.  Do not treat limits as
    # observations and do not count duplicate nested stats twice.
    deduped: List[Mapping[str, Any]] = []
    seen: Set[int] = set()
    for candidate in rows:
        marker = id(candidate)
        if marker not in seen:
            seen.add(marker)
            deduped.append(candidate)
    observations: List[Dict[str, Any]] = []
    for candidate in deduped:
        token = _numeric_observation(candidate, token_names)
        messages = _numeric_observation(candidate, message_names)
        candidates = _numeric_observation(candidate, candidate_names)
        evidence = _numeric_observation(candidate, evidence_names)
        if messages is None:
            messages = _list_count(candidate, ("messages", "message_ids", "all_message_ids"))
        if candidates is None:
            candidates = _list_count(candidate, ("candidates", "candidate_ids", "candidate_rows", "candidate_refs"))
        if evidence is None:
            evidence = _list_count(candidate, ("evidence", "evidence_ids", "evidence_refs"))
        observations.append(
            {
                "token_proxy": token,
                "message_count": messages,
                "candidate_count": candidates,
                "evidence_count": evidence,
            }
        )
    # Keep only rows that are at least partly envelope-accounted.  A complete
    # row still requires all four dimensions below; partial rows are reported
    # as unmeasured rather than silently passing.
    observations = [row for row in observations if any(value is not None for value in row.values())]
    return {"observations": observations, "count": len(observations)}


def _candidate_layer_presence(value: Any) -> Dict[str, int]:
    counts = {"person": 0, "object": 0, "state": 0, "qa": 0, "open": 0, "continuity": 0}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"candidate_person_history", "person_history", "person_candidates"}:
                    counts["person"] += len(_values(child))
                elif lowered in {"candidate_object_history", "object_history", "object_candidates"}:
                    counts["object"] += len(_values(child))
                elif lowered in {"candidate_state_history", "state_history", "state_candidates"}:
                    counts["state"] += len(_values(child))
                elif lowered in {"candidate_qa_links", "qa_candidates", "qa_links"}:
                    counts["qa"] += len(_values(child))
                elif lowered in {"open_thread_candidates", "open_threads"}:
                    counts["open"] += len(_values(child))
                elif lowered in {"continuity_candidates", "candidate_rows", "candidates", "candidate_context"}:
                    counts["continuity"] += len(_values(child))
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return counts


def _baseline_packet_window(packet: Mapping[str, Any]) -> Dict[str, Any]:
    primary_ids = _message_ids_by_layer(packet, "primary")
    adjacent_ids = _message_ids_by_layer(packet, "adjacent")
    if not primary_ids:
        primary_ids = set(_strings(packet.get("source_message_ids")))
    all_ids = primary_ids | adjacent_ids
    distractors: Set[str] = set()
    for item in _values(packet.get("adjacent_context")):
        if not isinstance(item, Mapping):
            continue
        distance = item.get("time_distance_seconds")
        try:
            far = distance is not None and float(distance) > 300
        except (TypeError, ValueError):
            far = False
        reasons = set(str(value).casefold() for value in _strings(item.get("candidate_reason")))
        if far or ("time_proximity_weak" in reasons and "same_segment_weak" not in reasons):
            mid = _first(item, ("message_id", "id"))
            if mid not in (None, ""):
                distractors.add(str(mid))
    return {
        "primary_ids": primary_ids,
        "adjacent_ids": adjacent_ids,
        "window_ids": all_ids,
        "distractor_ids": distractors,
        "key_ids": all_ids - distractors,
        "candidate_layers": _candidate_layer_presence(packet),
        "scope_pairs": _scope_pairs(packet),
    }


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
        target_ref = str(row.get("target_ref") or "")
        packet_id = str(row.get("packet_id") or "")
        if target_ref and packet_id:
            queue[target_ref] = row
    human: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(human_path):
        target_ref = str(row.get("target_ref") or "")
        if target_ref:
            human[target_ref] = row
    if len(queue) != SELECTION_LIMIT or len(human) != SELECTION_LIMIT or set(queue) != set(human):
        raise ValueError("K5 baseline does not contain exactly the same 20 selected roots")
    root_ids = {str(row["packet_id"]) for row in queue.values()}
    packets: Dict[str, Dict[str, Any]] = {}
    # The K5 packet file is private and body-bearing.  It is streamed once;
    # only the twenty selected root rows are retained in memory.
    for row in _iter_jsonl(packet_path):
        packet_id = str(row.get("packet_id") or "")
        if packet_id in root_ids:
            packets[packet_id] = row
    if set(packets) != root_ids:
        missing = sorted(root_ids - set(packets))
        raise ValueError(f"K5 selected root packets missing: {missing[:3]}")
    by_target: Dict[str, Dict[str, Any]] = {}
    for target_ref, queue_row in queue.items():
        packet = packets[str(queue_row["packet_id"])]
        human_row = human[target_ref]
        context = human_row.get("context_review") if isinstance(human_row.get("context_review"), Mapping) else {}
        distractor = human_row.get("distractor_review") if isinstance(human_row.get("distractor_review"), Mapping) else {}
        window = _baseline_packet_window(packet)
        required = int(context.get("required_context_units") or 0)
        by_target[target_ref] = {
            "target_ref": target_ref,
            "sample_ordinal": int(human_row.get("sample_ordinal") or queue_row.get("selection_rank") or 0),
            "packet_id": str(queue_row["packet_id"]),
            "packet_hash": str(queue_row.get("packet_hash") or ""),
            "bucket": tuple(str(value) for value in (queue_row.get("bucket") or [])),
            "scope": _scope_pairs(packet) or _scope_pairs(queue_row),
            "required_units": required,
            "baseline_preserved_units": int(context.get("preserved_context_units") or 0),
            "known_distractors": int(distractor.get("known_unrelated_count") or 0),
            "window": window,
            "packet": packet,
        }
    return {
        "manifest": manifest,
        "manifest_sha256": _sha256_file(manifest_path),
        "queue_sha256": _sha256_file(queue_path),
        "human_sha256": _sha256_file(human_path),
        "packet_sha256": _sha256_file(packet_path),
        "by_target": by_target,
    }


def _output_file(artifact_dir: Path, manifest: Mapping[str, Any], logical: str, candidates: Sequence[str]) -> Path:
    output_files = manifest.get("output_files")
    names: List[str] = list(candidates)
    if isinstance(output_files, Mapping):
        for key, value in output_files.items():
            lowered = str(key).casefold()
            if logical in lowered or logical in str(value).casefold():
                names.append(str(value))
    for name in names:
        path = artifact_dir / name
        if path.is_file() and path.resolve().parent == artifact_dir.resolve():
            return path
    raise FileNotFoundError(f"compact artifact has no {logical} map")


def _assert_target_manifest(artifact_dir: Path) -> Tuple[Dict[str, Any], Path]:
    artifact_dir = _safe_path(artifact_dir)
    manifest_path = artifact_dir / "manifest.private.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"target compact artifact not complete: {manifest_path}")
    manifest = _load_json(manifest_path)
    if str(manifest.get("artifact_version") or "") != TARGET_ARTIFACT_VERSION:
        raise ValueError("refusing to audit a non-K7 compact artifact")
    status = str(manifest.get("status") or "").casefold()
    if not status.startswith("complete"):
        raise ValueError(f"target compact artifact is not complete: {status or 'missing status'}")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise ValueError("target compact artifact reports frozen/gold reads")
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("target compact artifact reports provider calls")
    selection = manifest.get("selection") if isinstance(manifest.get("selection"), Mapping) else {}
    if int(selection.get("selected_packet_count") or manifest.get("selected_packet_count") or 0) != SELECTION_LIMIT:
        raise ValueError("target compact artifact selection is not exactly 20 packets")
    selection_path = _output_file(
        artifact_dir,
        manifest,
        "selection",
        ("selection_map.private.jsonl", "selection_mapping.private.jsonl", "compact_selection_map.private.jsonl"),
    )
    return manifest, selection_path


def _stream_target_maps(artifact_dir: Path, manifest: Mapping[str, Any], selection_path: Path, baseline: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Stream map rows once, retaining only selected-root lineage metadata."""
    baseline_by_target = baseline["by_target"]
    root_to_target: Dict[str, str] = {}
    hash_to_target: Dict[str, str] = {}
    target_refs_by_value: Dict[str, str] = {}
    for target_ref, info in baseline_by_target.items():
        root_to_target[str(info["packet_id"])] = target_ref
        if info.get("packet_hash"):
            hash_to_target[str(info["packet_hash"])] = target_ref
        target_refs_by_value[target_ref] = target_ref

    rows_by_ref: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    direct: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    map_rows = 0
    for row in _iter_jsonl(selection_path):
        map_rows += 1
        refs = _row_refs(row) | _target_refs(row)
        for ref in refs:
            rows_by_ref[ref].append(row)
        matched: Set[str] = set()
        row_target_refs = _target_refs(row)
        matched.update(target_refs_by_value[ref] for ref in row_target_refs if ref in target_refs_by_value)
        matched.update(root_to_target[ref] for ref in refs if ref in root_to_target)
        matched.update(hash_to_target[ref] for ref in refs if ref in hash_to_target)
        # A row may expose a source/root value nested under a small lineage map.
        for value in _all_ids(row):
            if value in root_to_target:
                matched.add(root_to_target[value])
            if value in hash_to_target:
                matched.add(hash_to_target[value])
        for target_ref in matched:
            direct[target_ref].append(row)

    selected_rows: Dict[str, List[Dict[str, Any]]] = {}
    lineage_counts: Dict[str, int] = {}
    for target_ref, info in baseline_by_target.items():
        seed = list(direct.get(target_ref, ()))
        root_id = str(info["packet_id"])
        # Include rows reachable by parent/root/subpacket references.  This is
        # metadata-only closure; unrelated map rows never enter selected_rows.
        selected: List[Dict[str, Any]] = []
        selected_keys: Set[int] = set()
        known_refs: Set[str] = {root_id, str(info.get("packet_hash") or ""), target_ref}
        queue: deque[Dict[str, Any]] = deque(seed)
        while queue:
            row = queue.popleft()
            marker = id(row)
            if marker in selected_keys:
                continue
            selected_keys.add(marker)
            selected.append(row)
            self_refs, parent_refs, child_refs = _lineage_refs(row)
            known_refs.update(self_refs | parent_refs | child_refs)
            for ref in self_refs | child_refs:
                for linked in rows_by_ref.get(ref, ()):
                    if id(linked) not in selected_keys:
                        queue.append(linked)
            for ref in parent_refs:
                if ref in known_refs:
                    for linked in rows_by_ref.get(ref, ()):
                        if id(linked) not in selected_keys:
                            queue.append(linked)
        selected_rows[target_ref] = selected
        lineage_counts[target_ref] = len(selected)

    recovery_path = _output_file(
        artifact_dir,
        manifest,
        "recovery",
        (
            "recovery_map.private.jsonl",
            "recover_map.private.jsonl",
            "recovery_mapping.private.jsonl",
            "materialization_map.private.jsonl",
        ),
    )
    recovery_rows_by_target: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    recovery_rows = 0
    known_leaf_refs: Dict[str, str] = {}
    for target_ref, rows in selected_rows.items():
        for row in rows:
            self_refs, parent_refs, child_refs = _lineage_refs(row)
            for ref in self_refs | child_refs | parent_refs:
                if ref:
                    known_leaf_refs[ref] = target_ref
    for row in _iter_jsonl(recovery_path):
        recovery_rows += 1
        refs = _row_refs(row) | _target_refs(row)
        matched: Set[str] = set()
        for ref in _target_refs(row):
            if ref in baseline_by_target:
                matched.add(ref)
        for ref in refs:
            if ref in known_leaf_refs:
                matched.add(known_leaf_refs[ref])
        # Recovery maps sometimes identify a root only through source packet ID.
        for target_ref, info in baseline_by_target.items():
            if str(info["packet_id"]) in refs or str(info.get("packet_hash") or "") in refs:
                matched.add(target_ref)
        for target_ref in matched:
            recovery_rows_by_target[target_ref].append(row)

    return (
        selected_rows,
        recovery_rows_by_target,
        {
            "selection_map_path": selection_path,
            "recovery_map_path": recovery_path,
            "selection_map_rows": map_rows,
            "recovery_map_rows": recovery_rows,
            "lineage_counts": lineage_counts,
        },
    )


def _target_leaf_rows(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    leaves: List[Mapping[str, Any]] = []
    for row in rows:
        if not _is_container(row):
            leaves.append(row)
    # If a map contains only a root leaf, preserve it; if it contains explicit
    # root + children, the container is excluded from provider/recovery counts.
    return leaves or list(rows)


def _row_message_ids(rows: Sequence[Mapping[str, Any]]) -> Set[str]:
    result: Set[str] = set()
    for row in rows:
        result.update(_message_ids(row))
    return result


def _row_layer_ids(rows: Sequence[Mapping[str, Any]], layer: str) -> Set[str]:
    result: Set[str] = set()
    for row in rows:
        result.update(_message_ids_by_layer(row, layer))
    return result


def _row_ref_counts(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    source = 0
    evidence = 0
    candidate = 0
    for row in rows:
        source += len(_ref_ids(row, {"source_refs", "source_ref_ids", "source_references", "all_source_refs"}))
        evidence += len(_ref_ids(row, {"evidence_refs", "evidence_ref_ids", "evidence_ids", "evidence"}))
        candidate += len(_ref_ids(row, {"candidate_ids", "candidate_row_ids", "candidate_refs", "candidates"}))
        nested = row.get("candidate_view_ids")
        if isinstance(nested, Mapping):
            candidate += sum(len(_strings(value)) for value in nested.values())
    return {"source": source, "evidence": evidence, "candidate": candidate}


def _recoverability(rows: Sequence[Mapping[str, Any]], recovery_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    all_rows = list(rows) + list(recovery_rows)
    source_ids: Set[str] = set()
    evidence_ids: Set[str] = set()
    explicit_recoverable: List[bool] = []
    snapshots = 0
    for row in all_rows:
        source_ids.update(_ref_ids(row, {"source_refs", "source_ref_ids", "source_references", "all_source_refs"}))
        evidence_ids.update(_ref_ids(row, {"evidence_refs", "evidence_ref_ids", "evidence_ids", "evidence"}))
        for key in ("recoverable", "recovery_ok", "round_trip_ok", "source_recoverable", "body_recoverable"):
            if key in row:
                explicit_recoverable.append(row.get(key) is True)
        if any(row.get(key) not in (None, "", [], {}) for key in ("open_snapshot", "open_snapshot_ref", "open_thread_snapshot", "open_snapshot_refs")):
            snapshots += 1
    status_values = [_status(row) for row in all_rows]
    pending_count = sum(value in {"pending", "deferred", "blocked"} for value in status_values)
    recovery_ok = bool(all_rows) and (not explicit_recoverable or all(explicit_recoverable))
    return {
        "map_row_count": len(recovery_rows),
        "source_ref_count": len(source_ids),
        "evidence_ref_count": len(evidence_ids),
        "explicit_recoverable_count": len(explicit_recoverable),
        "explicit_recoverable_pass_count": sum(explicit_recoverable),
        "recovery_ok": recovery_ok,
        "open_snapshot_row_count": snapshots,
        "pending_leaf_count": pending_count,
    }


def _scope_check(rows: Sequence[Mapping[str, Any]], baseline_scope: Set[Tuple[str, str]]) -> int:
    if not baseline_scope:
        return 0
    violations = 0
    for row in rows:
        for pair in _scope_pairs(row):
            if pair not in baseline_scope:
                violations += 1
    return violations


def _scenario_verdicts(
    info: Mapping[str, Any],
    leaves: Sequence[Mapping[str, Any]],
    retained_ids: Set[str],
    candidates: Mapping[str, int],
) -> Dict[str, str]:
    window = info["window"]
    buckets = set(info["bucket"])
    key_ids = set(window["key_ids"])
    adjacent_ids = set(window["adjacent_ids"])
    missing_key = key_ids - retained_ids
    adjacent_ok = not (adjacent_ids - retained_ids)
    result: Dict[str, str] = {}
    result["greeting_to_new_topic"] = "pass" if "greeting_to_new_topic" not in buckets or not missing_key else "fail"
    result["no_reply_continuation"] = "pass" if "no_reply_continuation" not in buckets or not missing_key else "fail"
    result["adjacent_context_only"] = "pass" if not adjacent_ids or adjacent_ok else "fail"
    result["media_or_context_only"] = "pass" if "media_or_context_only" not in buckets or not missing_key else "fail"
    result["person_history"] = "pass" if "person_history" not in buckets or candidates.get("person", 0) > 0 else "fail"
    result["object_history"] = "pass" if "object_history" not in buckets or candidates.get("object", 0) > 0 else "fail"
    result["state_update"] = "pass" if "state_update" not in buckets or candidates.get("state", 0) > 0 else "fail"
    result["pronoun_or_ellipsis"] = "pass" if "pronoun_or_ellipsis" not in buckets or not missing_key else "fail"
    result["topic_shift"] = "pass" if "topic_shift" not in buckets or not missing_key else "fail"
    result["long_gap_open_boundary"] = "pass" if "long_gap_open_boundary" not in buckets or any(
        _is_pending(row) or row.get("open_snapshot") or row.get("open_snapshot_ref") or row.get("boundary") for row in leaves
    ) else "fail"
    result["candidate_competition"] = "pass" if "candidate_competition" not in buckets or candidates.get("continuity", 0) + candidates.get("open", 0) + candidates.get("person", 0) + candidates.get("object", 0) + candidates.get("state", 0) + candidates.get("qa", 0) > 0 else "fail"
    return result


def _audit_one(info: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], recovery_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    leaves = _target_leaf_rows(rows)
    leaf_refs: List[str] = []
    for row in leaves:
        self_refs, _, _ = _lineage_refs(row)
        leaf_refs.append(_opaque(sorted(self_refs)[0] if self_refs else _canonical(row), "leaf"))
    retained_ids = _row_message_ids(leaves + list(recovery_rows))
    baseline_window = info["window"]
    key_ids = set(baseline_window["key_ids"])
    missing_key_ids = key_ids - retained_ids
    key_ratio = 1.0 if not key_ids else len(key_ids & retained_ids) / len(key_ids)
    required = int(info["required_units"])
    preserved = required if not missing_key_ids else int(required * key_ratio)
    # Any missing baseline window clue is treated as irreversible in this
    # private review, even when a weaker proxy could still infer it.
    irreversible = len(missing_key_ids)
    ref_counts = _row_ref_counts(leaves + list(recovery_rows))
    candidate_presence = {key: 0 for key in ("person", "object", "state", "qa", "open", "continuity")}
    for row in leaves + list(recovery_rows):
        observed = _candidate_layer_presence(row)
        for key in candidate_presence:
            candidate_presence[key] += observed[key]
    envelope_observations: List[Dict[str, Any]] = []
    for row in leaves + list(recovery_rows):
        envelope_observations.extend(_envelope_metrics(row)["observations"])
    envelope_errors: List[str] = []
    for observation in envelope_observations:
        if any(observation[key] is None for key in ("token_proxy", "message_count", "candidate_count", "evidence_count")):
            envelope_errors.append("ENVELOPE_ACCOUNTING_INCOMPLETE")
    bounds_ok = bool(envelope_observations) and not envelope_errors
    max_values = {
        key: max((int(item[key]) for item in envelope_observations if item[key] is not None), default=0)
        for key in ("token_proxy", "message_count", "candidate_count", "evidence_count")
    }
    if any(max_values[key] > limit for key, limit in (("token_proxy", MAX_TOKENS), ("message_count", MAX_MESSAGES), ("candidate_count", MAX_CANDIDATES), ("evidence_count", MAX_EVIDENCE))):
        bounds_ok = False
        envelope_errors.append("PROVIDER_ENVELOPE_OVER_BUDGET")
    boundary = _boundary_stats(leaves + list(recovery_rows))
    scope_violations = _scope_check(leaves + list(recovery_rows), set(info["scope"]))
    boundary["cross_chat_violations"] += scope_violations
    recoverability = _recoverability(leaves, recovery_rows)
    scenarios = _scenario_verdicts(info, leaves, retained_ids, candidate_presence)
    errors: List[str] = []
    if not rows:
        errors.append("SELECTED_ROOT_NOT_FOUND")
    if not leaves:
        errors.append("NO_SELECTED_LEAF")
    if missing_key_ids:
        errors.append("BASELINE_WINDOW_MESSAGE_LOST")
    if required and preserved / required < MIN_RECALL:
        errors.append("CONTEXT_RECALL_BELOW_THRESHOLD")
    if not recoverability["recovery_ok"]:
        errors.append("RECOVERY_NOT_CONFIRMED")
    if ref_counts["source"] == 0 or ref_counts["evidence"] == 0:
        errors.append("SOURCE_OR_EVIDENCE_REF_MISSING")
    if not bounds_ok:
        errors.extend(envelope_errors or ["PROVIDER_ENVELOPE_UNMEASURED"])
    if boundary["cross_chat_violations"]:
        errors.append("CROSS_CHAT_SCOPE_VIOLATION")
    if boundary["time_or_same_segment_strong_violations"]:
        errors.append("TIME_OR_SEGMENT_STRONG_RELATION")
    if boundary["local_final_semantic_decisions"]:
        errors.append("LOCAL_FINAL_SEMANTIC_DECISION")
    errors.extend(f"SCENARIO_{key.upper()}" for key, value in scenarios.items() if value == "fail")
    status = "pass" if not errors else "fail"
    root_value = str(info["packet_id"])
    return {
        "record_type": "context_packet_compact_k7_material_audit",
        "sample_ordinal": 0,  # filled deterministically by caller
        "target_ref": str(info["target_ref"]),
        "root_ref": _opaque(root_value, "root"),
        "leaf_refs": sorted(set(leaf_refs)),
        "leaf_count": len(leaves),
        "lineage_row_count": len(rows),
        "status": status,
        "baseline": {
            "bucket_count": len(info["bucket"]),
            "required_context_units": required,
            "baseline_preserved_context_units": int(info["baseline_preserved_units"]),
            "known_distractor_count": int(info["known_distractors"]),
            "window_message_count": len(baseline_window["window_ids"]),
            "key_window_message_count": len(key_ids),
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
            "retained_primary_message_count": len(_row_layer_ids(leaves + list(recovery_rows), "primary")),
            "retained_adjacent_message_count": len(_row_layer_ids(leaves + list(recovery_rows), "adjacent")),
            "adjacent_context_only_preserved": not (set(baseline_window["adjacent_ids"]) - retained_ids),
            "irreversible_loss_count": irreversible,
        },
        "scenario_retention": scenarios,
        "candidate_layers": candidate_presence,
        "source_evidence": {
            "source_ref_count": ref_counts["source"],
            "evidence_ref_count": ref_counts["evidence"],
            "candidate_ref_count": ref_counts["candidate"],
            **recoverability,
        },
        "provider_envelopes": {
            "envelope_count": len(envelope_observations),
            "max_token_proxy": max_values["token_proxy"],
            "max_message_count": max_values["message_count"],
            "max_candidate_count": max_values["candidate_count"],
            "max_evidence_count": max_values["evidence_count"],
            "limits": {
                "max_token_proxy": MAX_TOKENS,
                "max_messages": MAX_MESSAGES,
                "max_candidates": MAX_CANDIDATES,
                "max_evidence": MAX_EVIDENCE,
            },
            "all_within_limits": bounds_ok,
            "accounting_error_count": len(envelope_errors),
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
    manifest, selection_path = _assert_target_manifest(artifact_dir)
    baseline = _load_k5_baseline(k5_dir)
    selected_rows, recovery_rows, stream_meta = _stream_target_maps(artifact_dir, manifest, selection_path, baseline)
    audit_rows: List[Dict[str, Any]] = []
    ordered_targets = sorted(
        baseline["by_target"],
        key=lambda value: (int(baseline["by_target"][value].get("sample_ordinal") or 0), value),
    )
    for ordinal, target_ref in enumerate(ordered_targets, 1):
        # K5 target refs themselves remain the stable lineage key; the ordinal
        # is only a private audit display order.
        row = _audit_one(
            baseline["by_target"][target_ref],
            selected_rows.get(target_ref, ()),
            recovery_rows.get(target_ref, ()),
        )
        row["sample_ordinal"] = ordinal
        audit_rows.append(row)

    total_required = sum(int(row["context_recall"]["required_context_units"]) for row in audit_rows)
    total_preserved = sum(int(row["context_recall"]["preserved_context_units"]) for row in audit_rows)
    total_irreversible = sum(int(row["retention"]["irreversible_loss_count"]) for row in audit_rows)
    scenario_names = sorted({name for row in audit_rows for name in row["scenario_retention"]})
    scenario_summary = {
        name: {
            "sample_count": sum(name in row["scenario_retention"] and row["scenario_retention"][name] != "N/A" for row in audit_rows),
            "pass_count": sum(row["scenario_retention"].get(name) == "pass" for row in audit_rows),
            "fail_count": sum(row["scenario_retention"].get(name) == "fail" for row in audit_rows),
            "status": "pass" if all(row["scenario_retention"].get(name) != "fail" for row in audit_rows) else "fail",
        }
        for name in scenario_names
    }
    envelope_rows = [row["provider_envelopes"] for row in audit_rows]
    envelope_count = sum(int(row["envelope_count"]) for row in envelope_rows)
    all_within_limits = all(bool(row["all_within_limits"]) for row in envelope_rows) and envelope_count > 0
    zero = {
        "cross_chat_violations": sum(int(row["zero_tolerance"]["cross_chat_violations"]) for row in audit_rows),
        "time_or_same_segment_strong_relation_violations": sum(int(row["zero_tolerance"]["time_or_same_segment_strong_relation_violations"]) for row in audit_rows),
        "local_final_semantic_decisions": sum(int(row["zero_tolerance"]["local_final_semantic_decisions"]) for row in audit_rows),
        "irreversible_value_loss": total_irreversible,
    }
    recall = total_preserved / total_required if total_required else 0.0
    matched_roots = sum(bool(selected_rows.get(target_ref)) for target_ref in baseline["by_target"])
    recovery_ok = all(bool(row["source_evidence"]["recovery_ok"]) for row in audit_rows)
    source_evidence_ok = all(
        int(row["source_evidence"]["source_ref_count"]) > 0 and int(row["source_evidence"]["evidence_ref_count"]) > 0
        for row in audit_rows
    )
    scenarios_ok = all(value["status"] == "pass" for value in scenario_summary.values())
    zero_ok = not any(zero.values())
    root_scope_ok = matched_roots == SELECTION_LIMIT and len(audit_rows) == SELECTION_LIMIT
    allow_deepseek = bool(
        root_scope_ok
        and recall >= MIN_RECALL
        and total_irreversible == 0
        and recovery_ok
        and source_evidence_ok
        and all_within_limits
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
    if not recovery_ok:
        errors.append("RECOVERY_NOT_COMPLETE")
    if not source_evidence_ok:
        errors.append("SOURCE_OR_EVIDENCE_NOT_RECOVERABLE")
    if not all_within_limits:
        errors.append("PROVIDER_ENVELOPE_BUDGET_GATE")
    if not scenarios_ok:
        errors.append("SCENARIO_RETENTION_GATE")
    if not zero_ok:
        errors.append("ZERO_TOLERANCE_GATE")

    audit_dir = artifact_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "status": "pass" if allow_deepseek else "blocked_by_k7_gate",
        "scope": {
            "artifact_version": TARGET_ARTIFACT_VERSION,
            "artifact_ref": _opaque(_sha256_file(artifact_dir / "manifest.private.json"), "manifest"),
            "k5_baseline_version": K5_ARTIFACT_VERSION,
            "k5_baseline_ref": _opaque(baseline["manifest_sha256"], "k5_manifest"),
            "selection_map_ref": _opaque(_sha256_file(stream_meta["selection_map_path"]), "selection_map"),
            "recovery_map_ref": _opaque(_sha256_file(stream_meta["recovery_map_path"]), "recovery_map"),
            "frozen_read": False,
            "gold_loaded": False,
            "provider_called": False,
            "body_free": True,
        },
        "sample": {
            "selected_root_count": len(audit_rows),
            "selected_root_limit": SELECTION_LIMIT,
            "matched_root_count": matched_roots,
            "selection_map_rows_streamed": int(stream_meta["selection_map_rows"]),
            "recovery_map_rows_streamed": int(stream_meta["recovery_map_rows"]),
            "lineage_rows_retained_for_selected_roots": sum(int(value) for value in stream_meta["lineage_counts"].values()),
            "k5_baseline_context_units": total_required,
            "k5_baseline_preserved_context_units": sum(int(info["baseline_preserved_units"]) for info in baseline["by_target"].values()),
            "scoring": "independent_k7_material_review",
        },
        "context_recall": {
            "baseline": f"{total_required}/{total_required}",
            "preserved_context_units": total_preserved,
            "required_context_units": total_required,
            "rate": round(recall, 4),
            "minimum_threshold": MIN_RECALL,
            "gate": "pass" if recall >= MIN_RECALL else "fail",
            "definition": "K5-reviewed context units conservatively retained when all baseline key window refs remain in selected compact leaves/recovery rows",
        },
        "scenario_retention": scenario_summary,
        "distractor_review": {
            "k5_known_distractor_units": sum(int(info["known_distractors"]) for info in baseline["by_target"].values()),
            "distractor_is_not_promoted": zero["time_or_same_segment_strong_relation_violations"] == 0,
            "status": "pass" if zero["time_or_same_segment_strong_relation_violations"] == 0 else "fail",
        },
        "source_evidence_recovery": {
            "status": "pass" if recovery_ok and source_evidence_ok else "fail",
            "recovery_ok_root_count": sum(bool(row["source_evidence"]["recovery_ok"]) for row in audit_rows),
            "source_evidence_complete_root_count": sum(
                int(row["source_evidence"]["source_ref_count"]) > 0 and int(row["source_evidence"]["evidence_ref_count"]) > 0
                for row in audit_rows
            ),
        },
        "provider_envelopes": {
            "selected_leaf_envelope_count": envelope_count,
            "all_within_limits": all_within_limits,
            "max_token_proxy": max((int(row["max_token_proxy"]) for row in envelope_rows), default=0),
            "max_message_count": max((int(row["max_message_count"]) for row in envelope_rows), default=0),
            "max_candidate_count": max((int(row["max_candidate_count"]) for row in envelope_rows), default=0),
            "max_evidence_count": max((int(row["max_evidence_count"]) for row in envelope_rows), default=0),
            "limits": {
                "max_token_proxy": MAX_TOKENS,
                "max_messages": MAX_MESSAGES,
                "max_candidates": MAX_CANDIDATES,
                "max_evidence": MAX_EVIDENCE,
            },
            "unmeasured_or_incomplete_leaf_count": sum(int(row["accounting_error_count"]) > 0 for row in envelope_rows),
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
        "deepseek_pilot": {
            "allow_deepseek_pilot": allow_deepseek,
            "provider_called": False,
            "reason": "all K7 gates pass" if allow_deepseek else "one or more K7 material, recovery, budget, scenario, or zero-tolerance gates failed",
        },
        "allow_deepseek_pilot": allow_deepseek,
        "errors": sorted(set(errors)),
        "lineage": {
            "selection_map_rows_streamed": int(stream_meta["selection_map_rows"]),
            "recovery_map_rows_streamed": int(stream_meta["recovery_map_rows"]),
            "selection_map_file": stream_meta["selection_map_path"].name,
            "recovery_map_file": stream_meta["recovery_map_path"].name,
            "k5_queue_ref": _opaque(baseline["queue_sha256"], "k5_queue"),
            "k5_human_audit_ref": _opaque(baseline["human_sha256"], "k5_human_audit"),
        },
        "privacy_checks": {
            "body_key_count": 0,
            "identity_key_count": 0,
            "frozen_path_read": False,
            "provider_calls": 0,
        },
    }
    for ordinal, row in enumerate(audit_rows, 1):
        row["sample_ordinal"] = ordinal
    output_values = [audit_rows, summary]
    body_hits = _nested_key_scan(output_values, {value.casefold() for value in BODY_KEYS})
    identity_hits = _nested_key_scan(output_values, {value.casefold() for value in IDENTITY_KEYS})
    if body_hits or identity_hits:
        raise RuntimeError(f"K7 output privacy check failed: body={body_hits[:3]} identity={identity_hits[:3]}")
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
                "allow_deepseek_pilot": summary["allow_deepseek_pilot"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
