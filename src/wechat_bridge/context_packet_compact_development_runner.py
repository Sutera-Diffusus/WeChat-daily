"""K7 development-only compact context-packet runner.

This runner is deliberately a thin, local harness around the K6 compact
projector.  Its input is the K5 development artifact (the selected rows in
``selection_map.private.jsonl`` and their matching private packet rows).  It
does not load frozen data, call a provider, or touch production state.

The K5 packet contains intentionally duplicated fixed/dynamic/candidate
mirrors.  Before passing a selected row to K6 we keep only the direct K2
layers.  This makes the compact store the authority for each body and keeps
materialization bounded without changing the source selection.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .compact_context_packets import (
    CompactCapacity,
    CompactContextPacketError,
    CompactContextPacketResult,
    CompactContextPacketStore,
    CompactPacketCache,
    canonical_json,
    compact_context_packets,
    materialize_stage_packet,
)


RUNNER_SCHEMA_VERSION = "context_packet_compact_development_runner_v1"
ARTIFACT_VERSION = "context_packet_compact_development_v1"
INPUT_ARTIFACT_VERSION = "context_packet_development_v1"
LOCAL_DAY = "2026-08-25"
SELECTION_LIMIT = 20

OUTPUT_FILENAMES: Dict[str, str] = {
    "store": "compact_store.private.json",
    "packets": "packets.private.jsonl",
    "materialized": "materialized_map.private.jsonl",
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "errors": "errors.private.jsonl",
    "selection": "selection_map.private.jsonl",
    "recovery": "recovery_map.private.jsonl",
}

_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "evidence_text",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_redacted",
    }
)
_WEAK_REASONS = frozenset({"time_proximity_weak", "same_segment_weak"})


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=str)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Any]) -> None:
    path.write_text(
        "".join(json.dumps(_jsonable(v), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for v in values),
        encoding="utf-8",
    )


def _body_free(value: Any, *, label: str) -> None:
    """Fail closed for K7 ledger projections."""

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in _BODY_KEYS and child not in (None, "", (), [], {}):
                    raise ValueError("%s contains body-bearing key %s" % (label, key))
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)


def _unique_rows(values: Any, *id_keys: str) -> List[Dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        row = {str(k): _jsonable(v) for k, v in value.items()}
        identity = next((str(row[k]) for k in id_keys if row.get(k) not in (None, "")), canonical_json(row))
        if identity in seen:
            continue
        seen.add(identity)
        output.append(row)
    return output


def normalize_selected_packet(packet: Mapping[str, Any]) -> Dict[str, Any]:
    """Remove K5 mirrors while retaining all direct K2 material layers."""

    if not isinstance(packet, Mapping):
        raise TypeError("selected packet must be a mapping")
    primary = _unique_rows(packet.get("primary_fragments"), "fragment_id", "message_id")
    if not primary:
        raise ValueError("selected packet has no primary_fragments")
    adjacent = _unique_rows(packet.get("adjacent_context"), "fragment_id", "message_id")
    facts = _unique_rows(packet.get("authoritative_facts"), "message_id", "registry_key")
    evidence = _unique_rows(packet.get("evidence_refs"), "evidence_id", "id", "message_id")
    sources = _unique_rows(packet.get("source_refs"), "source_ref_id", "id", "message_id")
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for key, ids in (
        ("candidate_qa_links", ("candidate_id", "left_message_id", "right_message_id")),
        ("candidate_person_history", ("candidate_id", "person_ref_id")),
        ("candidate_object_history", ("candidate_id", "object_ref_id")),
        ("candidate_state_history", ("candidate_id", "state_ref_id")),
    ):
        candidates[key] = _unique_rows(packet.get(key), *ids)
    open_threads = _unique_rows(packet.get("open_thread_candidates"), "candidate_id", "thread_id", "id")
    cues = _unique_rows(packet.get("activation_cues"), "replay_key", "cue_id", "activation_cue_id")
    scope = packet.get("scope") if isinstance(packet.get("scope"), Mapping) else {
        "account_id": packet.get("account_id", "unknown"),
        "chat_id": packet.get("chat_id", "unknown"),
    }
    # Keep only scalar/hash/window metadata in fixed and dynamic parts.  The
    # direct arrays above are the canonical source layers for K6.
    fixed = {
        "fixed_part_version": "k7-fixed-v1",
        "scope": dict(scope),
        "source_fixed_hash": str(packet.get("fixed_hash") or ""),
        "anchor_fragment_ids": list(packet.get("anchor_fragment_ids") or ()),
        "anchor_claim_ids": list(packet.get("anchor_claim_ids") or packet.get("claim_ids") or ()),
        "source_message_ids": [str(row.get("message_id")) for row in primary if row.get("message_id")],
    }
    dynamic = {
        "dynamic_part_version": "k7-dynamic-v1",
        "scope": dict(scope),
        "source_dynamic_hash": str(packet.get("dynamic_hash") or ""),
        "window_scale": packet.get("window_scale") or (packet.get("window") or {}).get("scale", "unknown"),
        "boundary": _jsonable(packet.get("boundary") or {}),
        "status": str(packet.get("status") or "open"),
        "open_boundary": bool(packet.get("open_boundary", True)),
    }
    result: Dict[str, Any] = {
        "packet_id": str(packet.get("packet_id") or packet.get("context_packet_id") or ""),
        "context_packet_id": str(packet.get("context_packet_id") or packet.get("packet_id") or ""),
        "packet_version": str(packet.get("packet_version") or packet.get("context_packet_version") or "k7-k2-v1"),
        "account_id": str(packet.get("account_id") or scope.get("account_id") or "unknown"),
        "chat_id": str(packet.get("chat_id") or scope.get("chat_id") or "unknown"),
        "scope": dict(scope),
        "anchor_fragment_ids": list(packet.get("anchor_fragment_ids") or ()),
        "anchor_claim_ids": list(packet.get("anchor_claim_ids") or packet.get("claim_ids") or ()),
        "claim_ids": list(packet.get("claim_ids") or ()),
        "primary_fragments": primary,
        "adjacent_context": adjacent,
        "authoritative_facts": facts,
        "evidence_refs": evidence,
        "source_refs": sources,
        **candidates,
        "open_thread_candidates": open_threads,
        "activation_cues": cues,
        "candidate_reason": list(packet.get("candidate_reason") or ()),
        "uncertainties": list(packet.get("uncertainties") or ()),
        "boundary": _jsonable(packet.get("boundary") or {}),
        "fixed_part": fixed,
        "dynamic_part": dynamic,
        "candidate_only": True,
        "open_boundary": True,
        "source_provenance": {
            "source_packet_hash": str(packet.get("packet_hash") or packet.get("hash") or ""),
            "source_fixed_hash": str(packet.get("fixed_hash") or ""),
            "source_dynamic_hash": str(packet.get("dynamic_hash") or ""),
        },
    }
    if not result["packet_id"]:
        raise ValueError("selected packet has no packet_id")
    return result


def _guard_input(root: Union[str, Path]) -> Path:
    path = Path(root)
    if not path.is_dir():
        raise ValueError("K7 development input directory is missing")
    if any(str(part).casefold() in {"frozen", "frozen_test", "frozen-test"} for part in path.resolve().parts):
        raise ValueError("K7 runner refuses frozen input")
    return path


def _read_selected(root: Path, selected_count: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, Dict[str, Any]]:
    manifest_path = root / "manifest.private.json"
    selection_path = root / "selection_map.private.jsonl"
    packet_path = root / "packets.private.jsonl"
    if not (manifest_path.is_file() and selection_path.is_file() and packet_path.is_file()):
        raise ValueError("K7 input requires manifest.private.json, selection_map.private.jsonl and packets.private.jsonl")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("artifact_version") != INPUT_ARTIFACT_VERSION or manifest.get("split") != "development" or manifest.get("local_day") != LOCAL_DAY:
        raise ValueError("K7 input is not the 2026-08-25 development K5 artifact")
    if manifest.get("frozen_read") is True or manifest.get("provider_called") is True:
        raise ValueError("K7 input manifest violates local development boundary")
    selection_rows = [json.loads(line) for line in selection_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [row for row in selection_rows if bool(row.get("selected"))]
    selected.sort(key=lambda row: (int(row.get("selection_rank") or 10**9), str(row.get("packet_id") or "")))
    if len(selected) != int(selected_count):
        raise ValueError("K7 requires exactly %d selected packet refs" % int(selected_count))
    ids = {str(row.get("packet_id")) for row in selected}
    packets: Dict[str, Dict[str, Any]] = {}
    for line in packet_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        packet_id = str(row.get("packet_id") or "")
        if packet_id in ids:
            if packet_id in packets:
                raise ValueError("duplicate selected packet %s" % packet_id)
            packets[packet_id] = row
    missing = sorted(ids - set(packets))
    if missing:
        raise ValueError("selected packet rows missing: %s" % ",".join(missing[:3]))
    ordered = [packets[str(row["packet_id"])] for row in selected]
    digest = hashlib.sha256("".join(canonical_json(row) + "\n" for row in ordered).encode("utf-8")).hexdigest()
    return selected, ordered, digest, manifest


def _percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    position = (len(ordered) - 1) * fraction
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return int(round(ordered[left] + (ordered[right] - ordered[left]) * (position - left)))


def _row_ids(rows: Any, *keys: str) -> Tuple[str, ...]:
    output: List[str] = []
    if not isinstance(rows, (list, tuple)):
        return ()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                output.append(str(value))
                break
    return tuple(dict.fromkeys(output))


def _rate(expected: Sequence[str], observed: Sequence[str]) -> Dict[str, Any]:
    want, got = set(expected), set(observed)
    return {"expected": len(want), "recovered": len(want & got), "rate": (len(want & got) / len(want) if want else 1.0)}


@dataclass(frozen=True)
class CompactDevelopmentRunResult:
    input_directory: str
    output_directory: str
    selected_packet_count: int
    root_count: int
    leaf_count: int
    pending_count: int
    status: str
    manifest: Mapping[str, Any]
    aggregate: Mapping[str, Any]
    artifact_paths: Mapping[str, str]
    result: CompactContextPacketResult

    @property
    def materialized(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(self.aggregate.get("materialized_envelopes", ()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "selected_packet_count": self.selected_packet_count,
            "root_count": self.root_count,
            "leaf_count": self.leaf_count,
            "pending_count": self.pending_count,
            "status": self.status,
            "manifest": dict(self.manifest),
            "aggregate": dict(self.aggregate),
            "artifact_paths": dict(self.artifact_paths),
        }


def run_compact_context_packet_development(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    selected_packet_count: int = SELECTION_LIMIT,
    capacity: Optional[CompactCapacity] = None,
) -> CompactDevelopmentRunResult:
    """Compact exactly the selected development packets with the local K6 API."""

    input_root = _guard_input(input_directory)
    output_root = Path(output_directory)
    if output_root.exists():
        raise FileExistsError("K7 output is immutable; choose a new directory")
    if any(str(part).casefold() in {"frozen", "frozen_test", "frozen-test"} for part in output_root.resolve().parts):
        raise ValueError("K7 runner refuses frozen output")
    selected_rows, source_packets, input_hash, input_manifest = _read_selected(input_root, int(selected_packet_count))
    normalized = [normalize_selected_packet(packet) for packet in source_packets]
    target = capacity or CompactCapacity(max_input_token_proxy=2000, max_messages=24, max_candidate_rows=64, max_evidence_refs=64)
    cache = CompactPacketCache()
    compact = compact_context_packets(
        normalized,
        capacity=target,
        cache=cache,
        store=CompactContextPacketStore(capacity=target, cache=cache),
        packet_version="k7-compact-v1",
    )
    leaves = tuple(compact.packets)
    roots = tuple(packet for packet in compact.store.packet_index.values() if not packet.parent_packet_id)
    pending = tuple(packet for packet in leaves if packet.status == "pending")
    errors: List[Dict[str, Any]] = []
    materialized_meta: List[Dict[str, Any]] = []
    materialized: List[Mapping[str, Any]] = []
    proxies: List[int] = []
    for packet in leaves:
        cap = compact.store.capacity_for(packet, capacity=target)
        if packet.status == "pending" or not cap.ok:
            errors.append({"code": "capacity_exceeded", "packet_id": packet.packet_id, "source_packet_id": packet.source_packet_id, "status": packet.status, "stats": cap.stats.to_dict()})
            continue
        try:
            envelope = materialize_stage_packet(compact, packet, capacity=target)
        except Exception as exc:  # local diagnostics; never fall through with over-capacity data
            errors.append({"code": "materialization_failed", "packet_id": packet.packet_id, "error_type": type(exc).__name__})
            continue
        canonical_chars = len(canonical_json(envelope))
        token_proxy = (canonical_chars + 3) // 4
        stats = envelope.get("material_stats", {}) if isinstance(envelope, Mapping) else {}
        row = {
            "packet_id": packet.packet_id,
            "source_packet_id": packet.source_packet_id,
            "status": packet.status,
            "canonical_chars": canonical_chars,
            "canonical_bytes": len(canonical_json(envelope).encode("utf-8")),
            "input_token_proxy": token_proxy,
            "message_count": int(stats.get("message_count", 0) or 0),
            "candidate_row_count": int(stats.get("candidate_row_count", 0) or 0),
            "evidence_ref_count": int(stats.get("evidence_ref_count", 0) or 0),
            "fixed_hash": packet.fixed_hash,
            "dynamic_hash": packet.dynamic_hash,
            "content_hash": packet.content_hash,
            "packet_hash": packet.packet_hash,
        }
        materialized_meta.append(row)
        materialized.append(envelope)
        proxies.append(token_proxy)

    # A replay against the same process-local cache must retain all hash
    # namespaces and packet IDs; it never invokes provider code.
    replay_before = {name: len(getattr(cache, name)) for name in ("fixed", "dynamic", "content")}
    replay = compact_context_packets(normalized, capacity=target, cache=cache, store=compact.store, packet_version="k7-compact-v1")
    replay_hits = {name: sum(1 for packet in replay.packets if getattr(packet, "%s_hash" % name, "") in getattr(cache, name)) for name in ("fixed", "dynamic", "content")}

    recovery_rows: List[Dict[str, Any]] = []
    recovery_totals: Dict[str, List[int]] = {key: [0, 0] for key in ("source", "evidence", "primary", "adjacent", "greeting", "ack", "open_snapshot")}
    for source, selected in zip(source_packets, selected_rows):
        source_id = str(source.get("packet_id"))
        root = next((item for item in roots if item.source_packet_id == source_id), None)
        recovered = compact.store.recover_packet(root or source_id, include_body=True) if root is not None else {}
        expected = {
            "source": _row_ids(source.get("source_refs"), "source_ref_id", "id", "message_id"),
            "evidence": _row_ids(source.get("evidence_refs"), "evidence_id", "id", "message_id"),
            "primary": _row_ids(source.get("primary_fragments"), "message_id", "fragment_id"),
            "adjacent": _row_ids(source.get("adjacent_context"), "message_id", "fragment_id"),
        }
        got = {
            "source": _row_ids(recovered.get("source_refs"), "source_ref_id", "id", "message_id"),
            "evidence": _row_ids(recovered.get("evidence_refs"), "evidence_id", "id", "message_id"),
            "primary": _row_ids(recovered.get("primary_fragments"), "message_id", "fragment_id"),
            "adjacent": _row_ids(recovered.get("adjacent_context"), "message_id", "fragment_id"),
        }
        expected["greeting"] = tuple(str(row.get("message_id")) for row in list(source.get("primary_fragments") or ()) + list(source.get("adjacent_context") or ()) if isinstance(row, Mapping) and ("greeting" in str(row.get("role") or "").casefold() or "你好" in str(row.get("text_redacted") or row.get("content") or "")))
        expected["ack"] = tuple(str(row.get("message_id")) for row in list(source.get("primary_fragments") or ()) + list(source.get("adjacent_context") or ()) if isinstance(row, Mapping) and ("ack" in str(row.get("role") or "").casefold() or "收到" in str(row.get("text_redacted") or row.get("content") or "")))
        recovered_rows = list(recovered.get("primary_fragments") or ()) + list(recovered.get("adjacent_context") or ())
        got["greeting"] = tuple(str(row.get("message_id")) for row in recovered_rows if isinstance(row, Mapping) and ("greeting" in str(row.get("role") or "").casefold() or "你好" in str(row.get("text_redacted") or row.get("content") or "")))
        got["ack"] = tuple(str(row.get("message_id")) for row in recovered_rows if isinstance(row, Mapping) and ("ack" in str(row.get("role") or "").casefold() or "收到" in str(row.get("text_redacted") or row.get("content") or "")))
        rates = {key: _rate(expected[key], got[key]) for key in expected}
        rates["open_snapshot"] = {"expected": 1, "recovered": int(bool(root and root.open_snapshot_ref in compact.store.open_snapshot_table)), "rate": 1.0 if root and root.open_snapshot_ref in compact.store.open_snapshot_table else 0.0}
        for key, value in rates.items():
            recovery_totals[key][0] += int(value["expected"])
            recovery_totals[key][1] += int(value["recovered"])
        recovery_rows.append({"selection_rank": selected.get("selection_rank"), "source_packet_id": source_id, "root_packet_id": root.packet_id if root else None, "leaf_packet_ids": [item.packet_id for item in leaves if item.source_packet_id == source_id], "rates": rates})
    recovery_rates = {key: {"expected": total[0], "recovered": total[1], "rate": (total[1] / total[0] if total[0] else 1.0)} for key, total in recovery_totals.items()}

    weak_strong = 0
    cross_chat = 0
    for packet in normalized:
        scope = (str(packet.get("account_id")), str(packet.get("chat_id")))
        for key in ("candidate_qa_links", "candidate_person_history", "candidate_object_history", "candidate_state_history"):
            for candidate in packet.get(key, ()):
                if not isinstance(candidate, Mapping):
                    continue
                if bool(candidate.get("strong_relation")) and set(str(v) for v in (candidate.get("candidate_reason") or ())) <= _WEAK_REASONS:
                    weak_strong += 1
                for ref in list(candidate.get("source_refs") or ()) + list(candidate.get("evidence_refs") or ()):
                    if isinstance(ref, Mapping) and (str(ref.get("account_id") or scope[0]), str(ref.get("chat_id") or scope[1])) != scope and ref.get("account_id") and ref.get("chat_id"):
                        cross_chat += 1

    source_bytes = sum(len(canonical_json(packet).encode("utf-8")) for packet in source_packets)
    index_value = compact.store.to_dict(include_content=False)
    private_value = compact.store.to_dict(include_content=True)
    index_bytes = len(canonical_json(index_value).encode("utf-8"))
    private_bytes = len(canonical_json(private_value).encode("utf-8"))
    metrics = {
        "root_count": len(roots),
        "leaf_count": len(leaves),
        "pending_count": len(pending),
        "materialized_envelope_count": len(materialized),
        "materialized_token_proxy": {"max": max(proxies) if proxies else 0, "p50": _percentile(proxies, 0.50), "p95": _percentile(proxies, 0.95), "limit": target.max_input_token_proxy, "all_within_limit": bool(proxies) and max(proxies) <= target.max_input_token_proxy and not pending},
        "limits": target.to_dict(),
        "recovery_rates": recovery_rates,
        "zero_tolerance": {"cross_chat_violations": cross_chat, "time_same_segment_strong_relation_violations": weak_strong},
        "hashes": {"content_count": len(compact.store.content_table), "fixed_count": len(cache.fixed), "dynamic_count": len(cache.dynamic), "replay_cache_hits": {"fixed": replay_hits["fixed"], "dynamic": replay_hits["dynamic"], "content_cache": replay_hits["content"]}, "replay_cache_before": {"fixed": replay_before["fixed"], "dynamic": replay_before["dynamic"], "content_cache": replay_before["content"]}},
        "compression": {"before_bytes": source_bytes, "before_tokens": (source_bytes + 3) // 4, "after_index_bytes": index_bytes, "after_index_tokens": (index_bytes + 3) // 4, "after_private_bytes": private_bytes, "after_private_tokens": (private_bytes + 3) // 4, "dedupe_multiplier": (source_bytes / private_bytes if private_bytes else 0.0)},
        "materialized_envelopes": tuple(materialized_meta),
    }
    status = "blocked" if errors or not metrics["materialized_token_proxy"]["all_within_limit"] or cross_chat or weak_strong else "complete"
    errors = errors + ([{"code": "zero_tolerance_violation", "cross_chat_violations": cross_chat, "time_same_segment_strong_relation_violations": weak_strong}] if cross_chat or weak_strong else [])
    selection_out = []
    for selected in selected_rows:
        source_id = str(selected.get("packet_id"))
        selection_out.append({"selection_rank": selected.get("selection_rank"), "source_packet_id": source_id, "source_ref": source_id, "root_packet_id": next((item.packet_id for item in roots if item.source_packet_id == source_id), None), "leaf_packet_ids": [item.packet_id for item in leaves if item.source_packet_id == source_id], "source_packet_hash": selected.get("packet_hash"), "complete": bool(next((item for item in roots if item.source_packet_id == source_id), None))})
    manifest: Dict[str, Any] = {"artifact_version": ARTIFACT_VERSION, "runner_schema_version": RUNNER_SCHEMA_VERSION, "input_artifact_version": INPUT_ARTIFACT_VERSION, "local_day": LOCAL_DAY, "split": "development", "input_sha256": input_hash, "selected_packet_count": len(source_packets), "development_input_read": True, "frozen_read": False, "provider_called": False, "provider_calls": 0, "status": status, "output_files": dict(OUTPUT_FILENAMES)}
    aggregate = {"artifact_version": ARTIFACT_VERSION, "runner_schema_version": RUNNER_SCHEMA_VERSION, "status": status, "selected_packet_count": len(source_packets), "development_input_read": True, "frozen_read": False, "provider_called": False, "provider_calls": 0, "metrics": metrics, "selection_refs_complete": len(selection_out) == len(source_packets), "input_manifest_artifact": input_manifest.get("artifact_version")}
    cost = {"artifact_version": ARTIFACT_VERSION, "provider_called": False, "provider_calls": 0, "provider_tokens": {"input": 0, "output": 0}, "compression": metrics["compression"], "replay_cache": metrics["hashes"]}
    _body_free(manifest, label="manifest")
    _body_free(aggregate, label="aggregate")
    _body_free(cost, label="cost")
    _body_free(errors, label="errors")
    _body_free(selection_out, label="selection")
    _body_free(recovery_rows, label="recovery")
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["store"], private_value)
    _write_jsonl(output_root / OUTPUT_FILENAMES["packets"], [packet.to_dict() for packet in compact.store.packet_index.values()])
    _write_jsonl(output_root / OUTPUT_FILENAMES["materialized"], materialized_meta)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection"], selection_out)
    _write_jsonl(output_root / OUTPUT_FILENAMES["recovery"], recovery_rows)
    manifest["artifact_hashes"] = {key: hashlib.sha256((output_root / filename).read_bytes()).hexdigest() for key, filename in OUTPUT_FILENAMES.items() if key != "manifest"}
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return CompactDevelopmentRunResult(str(input_root), str(output_root), len(source_packets), len(roots), len(leaves), len(pending), status, manifest, aggregate, paths, replay)


# Discoverable aliases for callers using the K7 workstream wording.
run_k7_development_compact_context_packets = run_compact_context_packet_development
run_development_context_packet_compact = run_compact_context_packet_development

__all__ = [
    "ARTIFACT_VERSION",
    "INPUT_ARTIFACT_VERSION",
    "OUTPUT_FILENAMES",
    "CompactDevelopmentRunResult",
    "normalize_selected_packet",
    "run_compact_context_packet_development",
    "run_development_context_packet_compact",
    "run_k7_development_compact_context_packets",
]
