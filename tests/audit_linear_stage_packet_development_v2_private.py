"""Independent private K10 audit for the completed linear stage-packet v2.

The audit is deliberately standalone.  It reads only the completed v2
manifest and JSONL maps plus the already-reviewed K5 development baseline; it
does not import the v1 audit, the production packet implementation, a runner,
or a provider.  K9/v1 artifacts are never used as an input.  The materialized,
recovery, physical-page, and selection maps are streamed and rebound to the
same twenty K5 selected roots.  Emitted identifiers are one-way opaque refs;
packet bodies and raw identity values never leave memory.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple


DEFAULT_ARTIFACT_DIR = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_packet_development_v2"
)
DEFAULT_K5_DIR = Path(
    "data/private/gold_standard/2026-08-25/context_packet_development_v1"
)
TARGET_VERSION = "linear_stage_packet_development_v2"
K5_VERSION = "context_packet_development_v1"
AUDIT_SCHEMA = "linear_stage_packet_k10_v2_private_audit_v1"
SELECTED_ROOTS = 20
MIN_RECALL = 0.90
MAX_INPUT = 2000
MAX_USER = 1600
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
        "system_prompt",
        "user_packet",
        "user_canonical_json",
    }
)

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
        "root_id",
        "source_packet_id",
        "packet_id",
        "root_ref",
        "source_ref",
        "packet_ref",
        "root_hash",
        "source_hash",
        "packet_hash",
        "source_packet_hash",
        "selection_rank",
    }
)

MESSAGE_KEYS = frozenset(
    {
        "message_id",
        "source_message_id",
        "message_ref",
        "message_handle",
        "message_ids",
        "message_refs",
        "message_handles",
        "source_message_ids",
        "primary_message_ids",
        "primary_message_handles",
        "adjacent_message_ids",
        "adjacent_message_handles",
        "primary_fragments",
        "adjacent_context",
        "messages",
    }
)

EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "evidence_ref_id",
        "evidence_handle",
        "evidence_handles",
        "evidence_ids",
        "evidence_refs",
        "evidence_ref_ids",
        "evidence_handle_refs",
        "evidence",
    }
)

CANDIDATE_KEYS = frozenset(
    {
        "candidate_id",
        "candidate_handle",
        "candidate_ids",
        "candidate_handles",
        "candidate_links",
        "candidate_link_refs",
        "candidate_context",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "continuity_candidates",
        "open_thread_candidates",
    }
)

PRIMARY_KEYS = frozenset(
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

ADJACENT_KEYS = frozenset(
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

WEAK_REASON_CODES = frozenset(
    {
        "same_segment_weak",
        "time_proximity_weak",
        "same_scope",
        "finite_local_window",
        "candidate_only",
        "reversible_context",
        "sparse_term_overlap",
        "dialogue_bundle_candidate",
        "reversible_context_window",
    }
)


def _safe_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in resolved.parts):
        raise RuntimeError(f"K10 v2 audit refuses frozen path: {resolved}")
    return resolved


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object in {path}")
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
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


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
                continue
    return None


def _normalize_ref(value: Any) -> Set[str]:
    if value in (None, "") or isinstance(value, Mapping):
        return set()
    text = str(value)
    result = {text}
    for marker in ("|message|", "|evidence|", "|candidate|"):
        if marker in text:
            _left, right = text.split(marker, 1)
            if right:
                result.add(right)
    return result


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
        nested = value.get("scope")
        return _scope_parts(nested)
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


def _direct_rows(value: Mapping[str, Any], names: Sequence[str]) -> List[Dict[str, Any]]:
    for name in names:
        child = value.get(name)
        if isinstance(child, (list, tuple)):
            return [dict(item) for item in child if isinstance(item, Mapping)]
        if isinstance(child, Mapping) and all(isinstance(item, Mapping) for item in child.values()):
            return [dict(item) for item in child.values()]
    return []


def _message_id(value: Mapping[str, Any]) -> Optional[str]:
    raw = _first(value, ("message_id", "source_message_id", "message_ref", "message_handle", "id"))
    return str(raw) if raw not in (None, "") else None


def _evidence_id(value: Mapping[str, Any]) -> Optional[str]:
    raw = _first(value, ("evidence_id", "evidence_ref_id", "evidence_handle", "id", "ref"))
    return str(raw) if raw not in (None, "") else None


def _reason_values(value: Mapping[str, Any]) -> Set[str]:
    result: Set[str] = set()
    for name in ("candidate_reason", "candidate_reasons", "reason_codes", "supporting_slot_codes", "reasons"):
        result.update(str(item).casefold() for item in _values(value.get(name)))
    return result


def _baseline_window(packet: Mapping[str, Any]) -> Dict[str, Any]:
    primary = _direct_rows(packet, ("primary_fragments", "primary", "fragments"))
    adjacent = _direct_rows(packet, ("adjacent_context", "adjacent", "context_fragments", "greeting_context"))
    primary_ids: Set[str] = set()
    adjacent_ids: Set[str] = set()
    for row in primary:
        if _message_id(row):
            primary_ids.update(_normalize_ref(_message_id(row)))
    for row in adjacent:
        if _message_id(row):
            adjacent_ids.update(_normalize_ref(_message_id(row)))
    distractor_ids: Set[str] = set()
    for row in adjacent:
        distance = row.get("time_distance_seconds")
        try:
            far = distance is not None and float(distance) > 300
        except (TypeError, ValueError):
            far = False
        explicit = any(bool(row.get(name)) for name in ("unrelated", "is_unrelated", "distractor", "old_state_context"))
        if far or explicit or ("unrelated" in _reason_values(row)) or ("old_state" in _reason_values(row)):
            if _message_id(row):
                distractor_ids.update(_normalize_ref(_message_id(row)))
    evidence_ids: Set[str] = set()
    for row in _direct_rows(packet, ("evidence_refs", "evidence", "evidence_references")):
        if _evidence_id(row):
            evidence_ids.update(_normalize_ref(_evidence_id(row)))
    source_ids: Set[str] = set()
    for row in _direct_rows(packet, ("source_refs", "sources", "source_references")):
        raw = _first(row, ("source_ref_id", "source_id", "raw_message_ref", "record_hash", "id", "ref"))
        if raw not in (None, ""):
            source_ids.update(_normalize_ref(raw))
    window_ids = primary_ids | adjacent_ids
    return {
        "primary_ids": primary_ids,
        "adjacent_ids": adjacent_ids,
        "window_ids": window_ids,
        "distractor_ids": distractor_ids,
        "key_ids": window_ids - distractor_ids,
        "evidence_ids": evidence_ids,
        "source_ids": source_ids,
        "scope": _scope_pairs(packet),
    }


def _load_k5_baseline(k5_dir: Path) -> Dict[str, Any]:
    k5_dir = _safe_path(k5_dir)
    manifest_path = k5_dir / "manifest.private.json"
    queue_path = k5_dir / "audit_queue.private.jsonl"
    human_path = k5_dir / "audit" / "human_audit.private.jsonl"
    packets_path = k5_dir / "packets.private.jsonl"
    for path in (manifest_path, queue_path, human_path, packets_path):
        if not path.is_file():
            raise FileNotFoundError(f"K5 baseline file missing: {path}")
    manifest = _load_json(manifest_path)
    if manifest.get("artifact_version") != K5_VERSION:
        raise ValueError("K5 baseline version mismatch")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True or manifest.get("provider_called") is True:
        raise ValueError("K5 baseline reports forbidden reads/calls")
    queue = {str(row.get("target_ref")): row for row in _iter_jsonl(queue_path) if row.get("target_ref")}
    human = {str(row.get("target_ref")): row for row in _iter_jsonl(human_path) if row.get("target_ref")}
    if len(queue) != SELECTED_ROOTS or len(human) != SELECTED_ROOTS or set(queue) != set(human):
        raise ValueError("K5 baseline does not contain exactly 20 selected roots")
    packet_ids = {str(row.get("packet_id")) for row in queue.values()}
    packets = {}
    for row in _iter_jsonl(packets_path):
        packet_id = str(row.get("packet_id") or "")
        if packet_id in packet_ids:
            packets[packet_id] = row
    if set(packets) != packet_ids:
        raise ValueError("K5 selected packet set is incomplete")
    by_target: Dict[str, Dict[str, Any]] = {}
    for target, queue_row in queue.items():
        human_row = human[target]
        packet = packets[str(queue_row["packet_id"])]
        context = human_row.get("context_review") if isinstance(human_row.get("context_review"), Mapping) else {}
        distractor = human_row.get("distractor_review") if isinstance(human_row.get("distractor_review"), Mapping) else {}
        candidate = human_row.get("candidate_review") if isinstance(human_row.get("candidate_review"), Mapping) else {}
        by_target[target] = {
            "target": target,
            "packet_id": str(queue_row["packet_id"]),
            "packet_hash": str(queue_row.get("packet_hash") or ""),
            "rank": int(queue_row.get("selection_rank") or human_row.get("sample_ordinal") or 0),
            "sample_ordinal": int(human_row.get("sample_ordinal") or queue_row.get("selection_rank") or 0),
            "bucket": tuple(str(item) for item in (queue_row.get("bucket") or [])),
            "window": _baseline_window(packet),
            "required": int(context.get("required_context_units") or 0),
            "preserved": int(context.get("preserved_context_units") or 0),
            "reviewed": int(distractor.get("reviewed_window_units") or 0),
            "distractors": int(distractor.get("known_unrelated_count") or 0),
            "baseline_candidates": {
                "person": int(candidate.get("person_candidate_count") or 0),
                "object": int(candidate.get("object_candidate_count") or 0),
                "state": int(candidate.get("state_candidate_count") or 0),
                "qa": int(candidate.get("qa_candidate_count") or 0),
            },
        }
    return {
        "manifest": manifest,
        "manifest_sha256": _sha256_file(manifest_path),
        "queue_sha256": _sha256_file(queue_path),
        "human_sha256": _sha256_file(human_path),
        "packets_sha256": _sha256_file(packets_path),
        "by_target": by_target,
    }


def _assert_manifest(artifact_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Path]]:
    artifact_dir = _safe_path(artifact_dir)
    path = artifact_dir / "manifest.private.json"
    if not path.is_file():
        raise FileNotFoundError(f"K10 v2 manifest is missing: {path}")
    manifest = _load_json(path)
    if manifest.get("artifact_version") != TARGET_VERSION or manifest.get("output_version") != "v2":
        raise ValueError("refusing to audit a non-K10 v2 artifact")
    if str(manifest.get("status") or "").casefold() != "complete":
        raise ValueError("K10 v2 manifest is not complete")
    if manifest.get("local_day") not in (None, "2026-08-25") or manifest.get("split") not in (None, "development"):
        raise ValueError("K10 v2 manifest has the wrong development scope")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise ValueError("K10 v2 manifest reports frozen/gold reads")
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("K10 v2 manifest reports provider calls")
    if manifest.get("root_count") != SELECTED_ROOTS or manifest.get("selected_packet_count") != SELECTED_ROOTS:
        raise ValueError("K10 v2 manifest does not contain exactly 20 roots")
    if manifest.get("input_artifact_version") not in (None, K5_VERSION):
        raise ValueError("K10 v2 input is not the K5 development artifact")
    names = {
        "materialized": "materialized_map.private.jsonl",
        "recovery": "recovery_map.private.jsonl",
        "pages": "pages.private.jsonl",
        "selection": "selection_map.private.jsonl",
    }
    paths: Dict[str, Path] = {}
    for key, default in names.items():
        output_files = manifest.get("output_files") if isinstance(manifest.get("output_files"), Mapping) else {}
        name = str(output_files.get(key) or default)
        candidate = artifact_dir / name
        if not candidate.is_file() or candidate.resolve().parent != artifact_dir.resolve():
            raise FileNotFoundError(f"K10 v2 {key} map missing: {candidate}")
        paths[key] = candidate
    hashes = manifest.get("artifact_hashes") if isinstance(manifest.get("artifact_hashes"), Mapping) else {}
    hash_failures: List[str] = []
    for name, expected in hashes.items():
        candidate = artifact_dir / str(name)
        if not candidate.is_file() or candidate.resolve().parent != artifact_dir.resolve():
            hash_failures.append(str(name))
            continue
        if str(expected) != _sha256_file(candidate):
            hash_failures.append(str(name))
    manifest["_hash_failures"] = hash_failures
    return manifest, paths


def _row_lineage_aliases(row: Mapping[str, Any]) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in LINEAGE_KEYS:
                    if lowered == "selection_rank":
                        try:
                            result.add(str(int(child)))
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(child, Mapping):
                        raw = _first(child, ("id", "ref", "key", "handle", "hash"))
                        result.update(_normalize_ref(raw))
                    else:
                        for part in _values(child):
                            if isinstance(part, Mapping):
                                result.update(_normalize_ref(_first(part, ("id", "ref", "key", "handle", "hash"))))
                            else:
                                result.update(_normalize_ref(part))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(row)
    return result


def _bind_map(path: Path, baseline: Mapping[str, Any]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    by_target = baseline["by_target"]
    aliases: Dict[str, Set[str]] = defaultdict(set)
    ranks: Dict[str, str] = {}
    for target, info in by_target.items():
        for value in (target, info["packet_id"], info.get("packet_hash")):
            if value in (None, ""):
                continue
            for alias in _normalize_ref(value):
                aliases[alias].add(target)
        if int(info.get("rank") or 0):
            ranks[str(int(info["rank"]))] = target
    bound: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    streamed = 0
    unmatched = 0
    ambiguous = 0
    for row in _iter_jsonl(path):
        streamed += 1
        matched: Set[str] = set()
        for alias in _row_lineage_aliases(row):
            matched.update(aliases.get(alias, ()))
        if not matched:
            rank = row.get("selection_rank")
            try:
                matched.add(ranks[str(int(rank))])
            except (KeyError, TypeError, ValueError):
                pass
        if not matched:
            unmatched += 1
            continue
        if len(matched) > 1:
            ambiguous += 1
            continue
        bound[next(iter(matched))].append(row)
    return bound, {"streamed": streamed, "unmatched": unmatched, "ambiguous": ambiguous}


def _extract_message_ids(value: Any) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"message_id", "source_message_id", "message_ref", "message_handle"}:
                    if isinstance(child, Mapping):
                        result.update(_normalize_ref(_message_id(child)))
                    else:
                        result.update(_normalize_ref(child))
                elif lowered in MESSAGE_KEYS or lowered in PRIMARY_KEYS or lowered in ADJACENT_KEYS:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            raw = _message_id(part)
                            if raw:
                                result.update(_normalize_ref(raw))
                        else:
                            result.update(_normalize_ref(part))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    visit(value)
    return result


def _extract_evidence_ids(value: Any) -> Set[str]:
    result: Set[str] = set()

    def visit(item: Any, parent: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"evidence_id", "evidence_ref_id", "evidence_handle"}:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            result.update(_normalize_ref(_evidence_id(part)))
                        else:
                            result.update(_normalize_ref(part))
                elif lowered in {"evidence_ids", "evidence_ref_ids", "evidence_handles"}:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            result.update(_normalize_ref(_evidence_id(part)))
                        else:
                            result.update(_normalize_ref(part))
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child, lowered)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent)

    visit(value)
    return result


def _flat_reference_ids(value: Any, kind: str) -> Set[str]:
    """Read the canonical id from direct lists or nested reference rows."""

    result: Set[str] = set()
    for part in _values(value):
        if isinstance(part, Mapping):
            if kind == "evidence":
                raw = _first(part, ("evidence_id", "evidence_ref_id", "id", "ref"))
            else:
                raw = _first(part, ("source_ref_id", "source_id", "registry_key", "id", "ref", "record_hash"))
            if isinstance(raw, Mapping):
                raw = _first(raw, ("message_id", "source_message_id", "id", "ref", "record_hash"))
            result.update(_normalize_ref(raw))
        else:
            result.update(_normalize_ref(part))
    return result


def _reference_check(recovery_rows: Sequence[Mapping[str, Any]], baseline_ids: Set[str], kind: str) -> Dict[str, Any]:
    expected: Set[str] = set()
    observed: Set[str] = set()
    nested: Set[str] = set()
    expected_count = 0
    recovered_count = 0
    missing_rate = 0
    statuses: List[str] = []
    for row in recovery_rows:
        expected_map = row.get("expected_ids") if isinstance(row.get("expected_ids"), Mapping) else {}
        observed_map = row.get("observed_ids") if isinstance(row.get("observed_ids"), Mapping) else {}
        expected_parts = expected_map.get(kind, [])
        observed_parts = observed_map.get(kind, [])
        expected.update(_flat_reference_ids(expected_parts, kind))
        observed.update(_flat_reference_ids(observed_parts, kind))
        packet = row.get("recovered_packet") if isinstance(row.get("recovered_packet"), Mapping) else {}
        nested.update(_flat_reference_ids(packet.get("evidence_refs" if kind == "evidence" else "source_refs"), kind))
        rate = row.get("rates", {}).get(kind) if isinstance(row.get("rates"), Mapping) and isinstance(row.get("rates", {}).get(kind), Mapping) else {}
        expected_count += int(_numeric(rate, ("expected", "total", "declared")) or 0)
        recovered_count += int(_numeric(rate, ("recovered", "retained", "materialized", "actual")) or 0)
        missing_rate += len(_values(rate.get("missing")))
        statuses.append(str(rate.get("status") or "").casefold())
    baseline_missing = baseline_ids - nested
    direct_missing = expected - observed
    nested_missing = expected - nested
    direct_exact = expected == observed
    nested_exact = expected == nested
    rate_ok = bool(recovery_rows) and not missing_rate and all(status == "pass" for status in statuses) and recovered_count >= expected_count
    ok = bool(recovery_rows) and direct_exact and nested_exact and not baseline_missing and rate_ok
    return {
        "expected_id_count": len(expected),
        "observed_direct_id_count": len(observed),
        "observed_nested_id_count": len(nested),
        "expected_rate_count": expected_count,
        "recovered_rate_count": recovered_count,
        "direct_missing_count": len(direct_missing),
        "nested_missing_count": len(nested_missing),
        "baseline_missing_count": len(baseline_missing),
        "baseline_expected_id_count": len(baseline_ids),
        "direct_nested_100_percent": ok,
        "rate_status_ok": rate_ok,
    }


def _candidate_layers(value: Any) -> Dict[str, int]:
    counts = {key: 0 for key in ("person", "object", "state", "qa", "open", "continuity")}
    seen: Dict[str, Set[str]] = {key: set() for key in counts}

    def classify(name: str, row: Mapping[str, Any]) -> str:
        lowered = name.casefold()
        row_keys = " ".join(str(key).casefold() for key in row)
        if "person" in lowered or "person" in row_keys:
            return "person"
        if "object" in lowered or "object" in row_keys:
            return "object"
        if "state" in lowered or "state" in row_keys:
            return "state"
        if "qa" in lowered or "reply" in lowered or str(row.get("relation_subtype") or "").casefold() in {"qa", "reply", "question_answer"}:
            return "qa"
        if "open" in lowered or bool(row.get("open_boundary")):
            return "open"
        return "continuity"

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in CANDIDATE_KEYS or "candidate" in lowered:
                    for part in _values(child):
                        if isinstance(part, Mapping):
                            layer = classify(lowered, part)
                            raw = _first(part, ("candidate_id", "candidate_handle", "relation_id", "id"))
                            marker = str(raw) if raw not in (None, "") else _canonical(_body_free(part))
                        elif part not in (None, ""):
                            layer = classify(lowered, {})
                            marker = str(part)
                        else:
                            continue
                        if marker not in seen[layer]:
                            seen[layer].add(marker)
                            counts[layer] += 1
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return counts


def _open_boundary(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered == "open_boundary" and (child is True or str(child).casefold() in {"true", "open", "pending"}):
                return True
            if lowered == "status" and str(child).casefold() == "open":
                return True
            if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS and _open_boundary(child):
                return True
    elif isinstance(value, (list, tuple, set, frozenset)):
        return any(_open_boundary(child) for child in value)
    return False


def _zero_tolerance(value: Any) -> Dict[str, int]:
    cross_scope = 0
    weak_strong = 0
    local_final = 0

    def visit(item: Any) -> None:
        nonlocal cross_scope, weak_strong, local_final
        if isinstance(item, Mapping):
            reasons = _reason_values(item)
            relation = str(_first(item, ("relation_label", "relation", "relation_subtype"), "")).casefold()
            strong = any(bool(item.get(key)) for key in ("strong_relation", "is_strong", "strong")) or relation in {"resolved", "same_event", "strong"}
            if strong and reasons and reasons <= WEAK_REASON_CODES:
                weak_strong += 1
            for key, child in item.items():
                lowered = str(key).casefold()
                if lowered in {"final_local_semantic_decision", "local_final_semantic_decision", "semantic_final", "final_semantic_decision"}:
                    if child is True or str(child).casefold() in {"true", "resolved", "strong", "same_event", "complete", "final"}:
                        local_final += 1
                if lowered in {"cross_chat", "cross_chat_violation", "cross_scope", "scope_mismatch"} and bool(child):
                    cross_scope += 1
                if isinstance(child, (Mapping, list, tuple, set, frozenset)) and lowered not in BODY_KEYS:
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return {
        "cross_chat_violations": cross_scope,
        "time_or_weak_only_strong_violations": weak_strong,
        "local_final_semantic_decisions": local_final,
    }


def _stage_a(row: Mapping[str, Any]) -> Dict[str, Any]:
    stats = row.get("material_stats") if isinstance(row.get("material_stats"), Mapping) else {}
    values = {
        "input_token_proxy": _numeric(stats, ("input_token_proxy", "total_token_proxy")),
        "user_token_proxy": _numeric(stats, ("user_token_proxy", "user_input_token_proxy")),
        "message_count": _numeric(stats, ("message_count", "messages_count")),
        "candidate_count": _numeric(stats, ("candidate_count", "candidate_row_count")),
        "evidence_count": _numeric(stats, ("evidence_count", "evidence_ref_count")),
    }
    within = bool(row.get("within_limits")) and bool(stats.get("within_limits", True))
    complete = (
        str(row.get("stage") or "").casefold() in {"a", "stage_a", "stage-a"}
        and str(row.get("status") or "").casefold() == "complete"
        and within
        and all(value is not None for value in values.values())
        and values["input_token_proxy"] <= MAX_INPUT
        and values["user_token_proxy"] <= MAX_USER
        and values["message_count"] <= MAX_MESSAGES
        and values["candidate_count"] <= MAX_CANDIDATES
        and values["evidence_count"] <= MAX_EVIDENCE
    )
    return {**values, "complete": complete, "within_limits": within}


def _physical_page_metrics(
    page_rows: Sequence[Mapping[str, Any]],
    selection_rows: Sequence[Mapping[str, Any]],
    materialized_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    page_by_id: Dict[str, Dict[str, int]] = {}
    duplicate_page_ids = 0
    for row in page_rows:
        page_id = str(_first(row, ("page_id", "page_ref"), ""))
        if not page_id:
            continue
        counts = {
            "message": len(set(str(x) for x in _values(row.get("message_handles")))) ,
            "candidate": len(set(str(x) for x in _values(row.get("candidate_handles")))),
            "evidence": len(set(str(x) for x in _values(row.get("evidence_handles")))),
        }
        if page_id in page_by_id:
            duplicate_page_ids += 1
        else:
            page_by_id[page_id] = counts
    expected_page_count = max(
        (_numeric(row, ("page_count", "physical_page_count")) or 0) for row in selection_rows
    ) if selection_rows else 0
    observed_page_count = len(page_by_id)
    totals = {key: sum(value[key] for value in page_by_id.values()) for key in ("message", "candidate", "evidence")}
    linear_bound = max(
        math.ceil(totals["message"] / MAX_MESSAGES) if totals["message"] else 1,
        math.ceil(totals["candidate"] / MAX_CANDIDATES) if totals["candidate"] else 1,
        math.ceil(totals["evidence"] / MAX_EVIDENCE) if totals["evidence"] else 1,
    )
    cartesian_bound = (
        (math.ceil(totals["message"] / MAX_MESSAGES) if totals["message"] else 1)
        * (math.ceil(totals["candidate"] / MAX_CANDIDATES) if totals["candidate"] else 1)
        * (math.ceil(totals["evidence"] / MAX_EVIDENCE) if totals["evidence"] else 1)
    )
    chunk_ok = all(
        value["message"] <= MAX_MESSAGES and value["candidate"] <= MAX_CANDIDATES and value["evidence"] <= MAX_EVIDENCE
        for value in page_by_id.values()
    ) and bool(page_by_id)
    material_page_counts: Dict[str, Dict[str, int]] = {}
    for row in materialized_rows:
        page_id = str(_first(row, ("page_id", "page_ref"), ""))
        counts = row.get("page_counts") if isinstance(row.get("page_counts"), Mapping) else {}
        if page_id:
            material_page_counts[page_id] = {
                "message": int(counts.get("messages") or 0),
                "candidate": int(counts.get("candidates") or 0),
                "evidence": int(counts.get("evidence") or 0),
            }
    accounting_match = bool(material_page_counts) and all(
        page_id in page_by_id and material_page_counts[page_id] == counts
        for page_id, counts in material_page_counts.items()
    ) and set(material_page_counts) == set(page_by_id)
    linear_ok = bool(page_by_id) and expected_page_count == observed_page_count == linear_bound and observed_page_count <= cartesian_bound and chunk_ok and accounting_match and duplicate_page_ids == 0
    return {
        "expected_page_count": expected_page_count,
        "observed_page_count": observed_page_count,
        "linear_page_bound": linear_bound,
        "cartesian_page_bound": cartesian_bound,
        "message_count": totals["message"],
        "candidate_count": totals["candidate"],
        "evidence_count": totals["evidence"],
        "max_page_message_count": max((v["message"] for v in page_by_id.values()), default=0),
        "max_page_candidate_count": max((v["candidate"] for v in page_by_id.values()), default=0),
        "max_page_evidence_count": max((v["evidence"] for v in page_by_id.values()), default=0),
        "page_chunk_limits_ok": chunk_ok,
        "materialized_page_accounting_match": accounting_match,
        "duplicate_page_id_count": duplicate_page_ids,
        "linear_no_cartesian_explosion": linear_ok,
    }


def _scenario(info: Mapping[str, Any], key_ok: bool, adjacent_ok: bool, evidence_ok: bool, layers: Mapping[str, int], open_boundary: bool) -> Dict[str, str]:
    buckets = set(info["bucket"])
    result: Dict[str, str] = {}
    result["greeting_to_new_topic"] = "pass" if "greeting_to_new_topic" not in buckets or key_ok else "fail"
    result["no_reply_continuation"] = "pass" if "no_reply_continuation" not in buckets or key_ok else "fail"
    result["pronoun_or_ellipsis"] = "pass" if "pronoun_or_ellipsis" not in buckets or key_ok else "fail"
    result["topic_shift"] = "pass" if "topic_shift" not in buckets or key_ok else "fail"
    result["adjacent_context_only"] = "pass" if not info["window"]["adjacent_ids"] or adjacent_ok else "fail"
    result["media_or_context_only"] = "pass" if "media_or_context_only" not in buckets or evidence_ok else "fail"
    result["person_history"] = "pass" if "person_history" not in buckets or layers.get("person", 0) > 0 else "fail"
    result["object_history"] = "pass" if "object_history" not in buckets or layers.get("object", 0) > 0 else "fail"
    result["state_update"] = "pass" if "state_update" not in buckets or layers.get("state", 0) > 0 else "fail"
    result["candidate_competition"] = "pass" if "candidate_competition" not in buckets or sum(layers.values()) > 0 else "fail"
    result["long_gap_open_boundary"] = "pass" if "long_gap_open_boundary" not in buckets or open_boundary else "fail"
    return result


def _audit_root(info: Mapping[str, Any], materialized: Sequence[Mapping[str, Any]], recovery: Sequence[Mapping[str, Any]], pages: Sequence[Mapping[str, Any]], selection: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    all_rows = list(materialized) + list(recovery) + list(pages) + list(selection)
    retained_messages = _extract_message_ids(all_rows)
    missing_key = info["window"]["key_ids"] - retained_messages
    missing_adjacent = info["window"]["adjacent_ids"] - retained_messages
    retained_key_count = len(info["window"]["key_ids"] & retained_messages)
    key_count = len(info["window"]["key_ids"])
    recall_ratio = retained_key_count / key_count if key_count else 1.0
    preserved_units = int(info["required"]) if not missing_key else int(int(info["required"]) * recall_ratio)
    key_ok = not missing_key
    adjacent_ok = not missing_adjacent
    evidence = _reference_check(recovery, info["window"]["evidence_ids"], "evidence")
    source = _reference_check(recovery, info["window"]["source_ids"], "source")
    evidence_ok = bool(evidence["direct_nested_100_percent"])
    source_ok = bool(source["direct_nested_100_percent"])
    stage_rows = [_stage_a(row) for row in materialized]
    stage_complete = bool(stage_rows) and all(bool(row["complete"]) for row in stage_rows)
    layers = _candidate_layers(all_rows)
    open_boundary = _open_boundary(all_rows)
    scopes = set().union(*(_scope_pairs(row) for row in all_rows)) if all_rows else set()
    expected_scopes = set(info["window"]["scope"])
    scope_violations = len(scopes - expected_scopes)
    zero = _zero_tolerance(all_rows)
    zero["cross_chat_violations"] += scope_violations
    page = _physical_page_metrics(pages, selection, materialized)
    scenarios = _scenario(info, key_ok, adjacent_ok, evidence_ok, layers, open_boundary)
    errors: List[str] = []
    if not materialized:
        errors.append("MATERIALIZED_ROOT_NOT_FOUND")
    if not recovery:
        errors.append("RECOVERY_ROOT_NOT_FOUND")
    if not pages or not selection:
        errors.append("PHYSICAL_PAGE_MAP_NOT_FOUND")
    if missing_key:
        errors.append("BASELINE_KEY_CONTEXT_LOST")
    if info["required"] and preserved_units / max(1, int(info["required"])) < MIN_RECALL:
        errors.append("CONTEXT_RECALL_BELOW_0_90")
    if not evidence_ok:
        errors.append("DIRECT_NESTED_EVIDENCE_NOT_100_PERCENT")
    if not source_ok:
        errors.append("SOURCE_REF_NOT_RECOVERABLE")
    if not stage_complete:
        errors.append("STAGE_A_BUDGET_GATE")
    if not page["linear_no_cartesian_explosion"]:
        errors.append("PHYSICAL_PAGE_LINEAR_GATE")
    if zero["cross_chat_violations"]:
        errors.append("CROSS_CHAT_SCOPE_GATE")
    if zero["time_or_weak_only_strong_violations"]:
        errors.append("TIME_ONLY_STRONG_GATE")
    if zero["local_final_semantic_decisions"]:
        errors.append("LOCAL_FINAL_SEMANTIC_GATE")
    errors.extend(f"SCENARIO_{name.upper()}" for name, status in scenarios.items() if status == "fail")
    status = "pass" if not errors else "fail"
    page_ids = [str(_first(row, ("page_id", "page_ref"), "")) for row in pages if _first(row, ("page_id", "page_ref"), "")]
    return {
        "record_type": "linear_stage_packet_k10_v2_private_material_audit",
        "sample_ordinal": int(info["sample_ordinal"]),
        "target_ref": _opaque(info["target"], "target"),
        "root_ref": _opaque(info["packet_id"], "root"),
        "status": status,
        "materialized_row_count": len(materialized),
        "recovery_row_count": len(recovery),
        "physical_page_row_count": len(pages),
        "selection_row_count": len(selection),
        "page_refs": sorted(_opaque(page_id, "page") for page_id in set(page_ids)),
        "baseline": {
            "required_context_units": int(info["required"]),
            "baseline_preserved_context_units": int(info["preserved"]),
            "known_distractor_count": int(info["distractors"]),
            "reviewed_context_units": int(info["reviewed"]),
            "key_window_message_count": len(info["window"]["key_ids"]),
            "window_message_count": len(info["window"]["window_ids"]),
            "evidence_ref_count": len(info["window"]["evidence_ids"]),
        },
        "context_recall": {
            "required_context_units": int(info["required"]),
            "preserved_context_units": preserved_units,
            "rate": round(preserved_units / int(info["required"]), 4) if info["required"] else "N/A",
            "retained_key_message_count": retained_key_count,
            "missing_key_message_count": len(missing_key),
            "missing_key_opaque_refs": sorted(_opaque(value, "message") for value in missing_key),
            "minimum_threshold": MIN_RECALL,
        },
        "retention": {
            "retained_message_count": len(retained_messages),
            "adjacent_context_only_preserved": adjacent_ok,
            "evidence_preserved": evidence_ok,
            "missing_adjacent_count": len(missing_adjacent),
            "missing_evidence_count": evidence["baseline_missing_count"],
            "irreversible_loss_count": len(missing_key) + evidence["baseline_missing_count"],
        },
        "scenario_retention": scenarios,
        "candidate_layers": layers,
        "source_evidence": {
            "source_recovery_ok": source_ok,
            "evidence_recovery_ok": evidence_ok,
            "source": source,
            "evidence": evidence,
        },
        "stage_a": {
            "complete": stage_complete,
            "envelope_count": len(stage_rows),
            "max_input_token_proxy": max((int(row["input_token_proxy"] or 0) for row in stage_rows), default=0),
            "max_user_token_proxy": max((int(row["user_token_proxy"] or 0) for row in stage_rows), default=0),
            "max_message_count": max((int(row["message_count"] or 0) for row in stage_rows), default=0),
            "max_candidate_count": max((int(row["candidate_count"] or 0) for row in stage_rows), default=0),
            "max_evidence_count": max((int(row["evidence_count"] or 0) for row in stage_rows), default=0),
            "limits": {"max_input_token_proxy": MAX_INPUT, "max_user_token_proxy": MAX_USER, "max_messages": MAX_MESSAGES, "max_candidates": MAX_CANDIDATES, "max_evidence": MAX_EVIDENCE},
        },
        "physical_paging": page,
        "distractor_review": {
            "known_unrelated_count": int(info["distractors"]),
            "reviewed_context_units": int(info["reviewed"]),
            "rate": round(int(info["distractors"]) / int(info["reviewed"]), 4) if info["reviewed"] else "N/A",
            "not_promoted_to_strong_relation": zero["time_or_weak_only_strong_violations"] == 0,
        },
        "zero_tolerance": zero,
        "error_codes": sorted(set(errors)),
    }


def run_audit(artifact_dir: Path = DEFAULT_ARTIFACT_DIR, k5_dir: Path = DEFAULT_K5_DIR) -> Dict[str, Any]:
    artifact_dir = _safe_path(Path(artifact_dir))
    k5_dir = _safe_path(Path(k5_dir))
    manifest, paths = _assert_manifest(artifact_dir)
    baseline = _load_k5_baseline(k5_dir)
    bound_maps: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    bind_meta: Dict[str, Dict[str, int]] = {}
    for key, path in paths.items():
        bound_maps[key], bind_meta[key] = _bind_map(path, baseline)
    ordered_targets = sorted(baseline["by_target"], key=lambda target: (baseline["by_target"][target]["sample_ordinal"], target))
    audit_rows: List[Dict[str, Any]] = []
    for target in ordered_targets:
        info = baseline["by_target"][target]
        audit_rows.append(
            _audit_root(
                info,
                bound_maps["materialized"].get(target, ()),
                bound_maps["recovery"].get(target, ()),
                bound_maps["pages"].get(target, ()),
                bound_maps["selection"].get(target, ()),
            )
        )
    total_required = sum(int(row["context_recall"]["required_context_units"]) for row in audit_rows)
    total_preserved = sum(int(row["context_recall"]["preserved_context_units"]) for row in audit_rows)
    total_recall = total_preserved / total_required if total_required else 0.0
    irreversible = sum(int(row["retention"]["irreversible_loss_count"]) for row in audit_rows)
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
    stage_ok = bool(stage_rows) and all(bool(row["complete"]) for row in stage_rows)
    page_rows = [row["physical_paging"] for row in audit_rows]
    page_ok = bool(page_rows) and all(bool(row["linear_no_cartesian_explosion"]) for row in page_rows)
    evidence_ok = all(bool(row["source_evidence"]["evidence_recovery_ok"]) for row in audit_rows)
    source_ok = all(bool(row["source_evidence"]["source_recovery_ok"]) for row in audit_rows)
    zero = {
        "cross_chat_violations": sum(int(row["zero_tolerance"]["cross_chat_violations"]) for row in audit_rows),
        "time_or_weak_only_strong_violations": sum(int(row["zero_tolerance"]["time_or_weak_only_strong_violations"]) for row in audit_rows),
        "local_final_semantic_decisions": sum(int(row["zero_tolerance"]["local_final_semantic_decisions"]) for row in audit_rows),
    }
    zero_ok = not any(zero.values())
    scenarios_ok = all(item["status"] == "pass" for item in scenario_summary.values())
    scope_ok = (
        len(audit_rows) == SELECTED_ROOTS
        and all(bind_meta[key]["unmatched"] == 0 and bind_meta[key]["ambiguous"] == 0 for key in paths)
        and all(len(bound_maps[key].get(target, ())) > 0 for key in paths for target in ordered_targets)
    )
    hash_ok = not manifest.get("_hash_failures")
    allow = bool(scope_ok and hash_ok and total_recall >= MIN_RECALL and irreversible == 0 and stage_ok and page_ok and evidence_ok and source_ok and scenarios_ok and zero_ok)
    errors: List[str] = []
    if not scope_ok:
        errors.append("SELECTED_ROOT_SCOPE_NOT_EXACTLY_20")
    if not hash_ok:
        errors.append("ARTIFACT_HASH_GATE")
    if total_recall < MIN_RECALL:
        errors.append("CONTEXT_RECALL_BELOW_0_90")
    if irreversible:
        errors.append("IRREVERSIBLE_VALUE_LOSS")
    if not stage_ok:
        errors.append("STAGE_A_BUDGET_GATE")
    if not page_ok:
        errors.append("PHYSICAL_PAGE_LINEAR_GATE")
    if not evidence_ok:
        errors.append("DIRECT_NESTED_EVIDENCE_GATE")
    if not source_ok:
        errors.append("SOURCE_RECOVERY_GATE")
    if not scenarios_ok:
        errors.append("SCENARIO_RETENTION_GATE")
    if not zero_ok:
        errors.append("ZERO_TOLERANCE_GATE")
    audit_dir = artifact_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "status": "pass" if allow else "blocked_by_k10_v2_gate",
        "allow_deepseek_stage_a_pilot": allow,
        "scope": {
            "artifact_version": TARGET_VERSION,
            "artifact_ref": _opaque(_sha256_file(artifact_dir / "manifest.private.json"), "manifest"),
            "k5_baseline_version": K5_VERSION,
            "k5_baseline_ref": _opaque(baseline["manifest_sha256"], "k5_manifest"),
            "frozen_read": False,
            "gold_loaded": False,
            "provider_called": False,
            "body_free": True,
            "v1_linear_artifact_used": False,
        },
        "sample": {
            "selected_root_count": len(audit_rows),
            "selected_root_limit": SELECTED_ROOTS,
            "map_rows_streamed": {key: bind_meta[key]["streamed"] for key in paths},
            "map_rows_unmatched": {key: bind_meta[key]["unmatched"] for key in paths},
            "map_rows_ambiguous": {key: bind_meta[key]["ambiguous"] for key in paths},
            "selected_lineage_rows_retained": sum(len(bound_maps[key].get(target, ())) for key in paths for target in ordered_targets),
            "scoring": "independent_k10_v2_linear_material_review",
        },
        "comparison_to_k5": {
            "key_context_baseline": "46/46",
            "baseline_required_context_units": total_required,
            "preserved_context_units": total_preserved,
            "context_recall_rate": round(total_recall, 4),
            "minimum_threshold": MIN_RECALL,
            "known_distractor_baseline": "1/100",
            "known_unrelated_context_units": sum(int(info["distractors"]) for info in baseline["by_target"].values()),
            "reviewed_context_units": sum(int(info["reviewed"]) for info in baseline["by_target"].values()),
        },
        "context_recall": {
            "required_context_units": total_required,
            "preserved_context_units": total_preserved,
            "rate": round(total_recall, 4),
            "minimum_threshold": MIN_RECALL,
            "gate": "pass" if total_recall >= MIN_RECALL else "fail",
        },
        "scenario_retention": scenario_summary,
        "source_evidence_recovery": {
            "source_status": "pass" if source_ok else "fail",
            "evidence_status": "pass" if evidence_ok else "fail",
            "source_recoverable_root_count": sum(bool(row["source_evidence"]["source_recovery_ok"]) for row in audit_rows),
            "evidence_direct_nested_100_percent_root_count": sum(bool(row["source_evidence"]["evidence_recovery_ok"]) for row in audit_rows),
            "evidence_expected_rate_id_total": sum(int(row["source_evidence"]["evidence"]["expected_id_count"]) for row in audit_rows),
            "evidence_observed_direct_id_total": sum(int(row["source_evidence"]["evidence"]["observed_direct_id_count"]) for row in audit_rows),
            "evidence_observed_nested_id_total": sum(int(row["source_evidence"]["evidence"]["observed_nested_id_count"]) for row in audit_rows),
            "evidence_baseline_missing_total": sum(int(row["source_evidence"]["evidence"]["baseline_missing_count"]) for row in audit_rows),
        },
        "stage_a": {
            "complete_root_count": sum(bool(row["complete"]) for row in stage_rows),
            "selected_root_count": len(stage_rows),
            "all_complete": stage_ok,
            "max_input_token_proxy": max((int(row["max_input_token_proxy"]) for row in stage_rows), default=0),
            "max_user_token_proxy": max((int(row["max_user_token_proxy"]) for row in stage_rows), default=0),
            "limits": {"max_input_token_proxy": MAX_INPUT, "max_user_token_proxy": MAX_USER, "max_messages": MAX_MESSAGES, "max_candidates": MAX_CANDIDATES, "max_evidence": MAX_EVIDENCE},
            "gate": "pass" if stage_ok else "fail",
        },
        "physical_paging": {
            "all_roots_linear": page_ok,
            "max_observed_page_count": max((int(row["observed_page_count"]) for row in page_rows), default=0),
            "max_expected_page_count": max((int(row["expected_page_count"]) for row in page_rows), default=0),
            "max_linear_page_bound": max((int(row["linear_page_bound"]) for row in page_rows), default=0),
            "max_cartesian_page_bound": max((int(row["cartesian_page_bound"]) for row in page_rows), default=0),
            "max_message_count": max((int(row["message_count"]) for row in page_rows), default=0),
            "max_candidate_count": max((int(row["candidate_count"]) for row in page_rows), default=0),
            "max_evidence_count": max((int(row["evidence_count"]) for row in page_rows), default=0),
            "gate": "pass" if page_ok else "fail",
        },
        "zero_tolerance": {**zero, "cross_chat_local_final_time_only_strong_zero": zero_ok, "status": "pass" if zero_ok else "fail"},
        "irreversible_loss": {"count": irreversible, "status": "pass" if irreversible == 0 else "fail"},
        "hash_checks": {
            "artifact_hashes_match": hash_ok,
            "failure_count": len(manifest.get("_hash_failures") or []),
            "audit_body_free": True,
        },
        "errors": sorted(set(errors)),
        "privacy_checks": {
            "body_key_count": 0,
            "identity_key_count": 0,
            "frozen_path_read": False,
            "provider_calls": 0,
            "output_refs": "opaque_only",
        },
    }
    body_hits = _nested_key_scan([audit_rows, summary], {key.casefold() for key in BODY_KEYS})
    identity_hits = _nested_key_scan([audit_rows, summary], {key.casefold() for key in IDENTITY_KEYS})
    if body_hits or identity_hits:
        raise RuntimeError(f"K10 v2 audit output privacy failure: body={body_hits[:3]} identity={identity_hits[:3]}")
    _write_jsonl(audit_dir / "human_audit.private.jsonl", audit_rows)
    _write_json(audit_dir / "audit_summary.private.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--k5-dir", type=Path, default=DEFAULT_K5_DIR)
    args = parser.parse_args()
    summary = run_audit(args.artifact_dir, args.k5_dir)
    print(json.dumps({
        "allow_deepseek_stage_a_pilot": summary["allow_deepseek_stage_a_pilot"],
        "context_recall": summary["context_recall"]["rate"],
        "irreversible_loss": summary["irreversible_loss"]["count"],
        "ok": summary["status"] == "pass",
        "selected_roots": summary["sample"]["selected_root_count"],
        "status": summary["status"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
