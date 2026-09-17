"""Read-only, independent K9 metric reconciliation.

This module deliberately does not import the K9 runner, the K8 store, or the
existing K9 audit.  It reads the private K5 selection/baseline and the private
K9 ledgers, normalises transport-envelope duplication, and emits a body-free
report.  It never reads a frozen split, calls a provider, or mutates a source
artifact.  ``--output`` may be used to persist the body-free report in an
audit directory.

The important distinction in this report is between:

* one physical page row and repeated ``page_id`` metadata inside a materialised
  transport envelope;
* evidence-id recovery (32/32) and evidence-bearing-root coverage (16/16, with
  four intentionally vacuous roots); and
* complete Stage-A envelopes (19/19 within budget) and one pending envelope
  (2222/2204) that has a recoverable open snapshot.

The source artifact is intentionally body-bearing in private files.  Bodyful
values are only used in memory to derive opaque row references; no body value
is placed in the report.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
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
DEFAULT_OUTPUT = DEFAULT_ARTIFACT_DIR / "audit" / "reconciliation.private.json"

TARGET_ARTIFACT_VERSION = "linear_stage_packet_development_v1"
K5_ARTIFACT_VERSION = "context_packet_development_v1"
SCHEMA = "linear_stage_packet_k9_reconciliation_v1"
SELECTION_LIMIT = 20
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
        "user_canonical_json",
    }
)

# These names are forbidden in emitted values.  The reconciliation itself may
# inspect such keys in memory, but only one-way references can leave the tool.
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

LAYER_NAMES = (
    "primary_fragments",
    "adjacent_context",
    "authoritative_facts",
    "source_refs",
    "evidence_refs",
    "candidate_qa_links",
    "candidate_person_history",
    "candidate_object_history",
    "candidate_state_history",
    "continuity_candidates",
    "open_thread_candidates",
)
CANDIDATE_LAYERS = (
    "candidate_qa_links",
    "candidate_person_history",
    "candidate_object_history",
    "candidate_state_history",
    "continuity_candidates",
    "open_thread_candidates",
)
ENDPOINT_ACCOUNT_KEYS = (
    "left_account_id",
    "right_account_id",
    "question_account_id",
    "answer_account_id",
    "source_account_id",
)
ENDPOINT_CHAT_KEYS = (
    "left_chat_id",
    "right_chat_id",
    "question_chat_id",
    "answer_chat_id",
    "source_chat_id",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _opaque(namespace: str, value: Any) -> str:
    """Return a deterministic one-way reference with no raw value leakage."""

    digest = hashlib.sha256((namespace + "|" + _canonical(value)).encode("utf-8")).hexdigest()
    return f"{namespace}_{digest[:24]}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, Mapping):
        if all(isinstance(child, Mapping) for child in value.values()):
            return [dict(child) for child in value.values()]
        return []
    if isinstance(value, (list, tuple)):
        return [dict(child) for child in value if isinstance(child, Mapping)]
    return []


def _first(value: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        child = value.get(name)
        if child not in (None, ""):
            return child
    return None


def _values(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _scope_pair(value: Any) -> Optional[Tuple[str, str]]:
    """Normalise dict and ``ACCOUNT/CHAT`` scope encodings to one pair."""

    if isinstance(value, Mapping):
        account = _first(value, ("account_id", "account", "account_ref"))
        chat = _first(value, ("chat_id", "chat", "chat_ref"))
        if account not in (None, "") and chat not in (None, ""):
            return str(account), str(chat)
        return _scope_pair(value.get("scope"))
    if isinstance(value, str):
        for separator in ("/", "::"):
            if separator in value:
                left, right = value.split(separator, 1)
                if left and right:
                    return left, right
    return None


def _scope_pairs(value: Any) -> Set[Tuple[str, str]]:
    """Collect unique scope pairs, never treating repeated metadata as new scope."""

    result: Set[Tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            pair = _scope_pair(item)
            if pair:
                result.add(pair)
            for child in item.values():
                if isinstance(child, (Mapping, list, tuple, set, frozenset)):
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _id_value(row: Mapping[str, Any], names: Sequence[str]) -> Optional[str]:
    value = _first(row, names)
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        nested = _first(value, ("id", "ref", "key", "handle"))
        return str(nested) if nested not in (None, "") else None
    return str(value)


def _id_set(value: Any, names: Sequence[str]) -> Set[str]:
    result: Set[str] = set()
    for item in _values(value):
        if isinstance(item, Mapping):
            item_id = _id_value(item, names)
            if item_id:
                result.add(item_id)
        elif item not in (None, ""):
            result.add(str(item))
    return result


def _packet_id(packet: Mapping[str, Any]) -> Optional[str]:
    return _id_value(packet, ("packet_id", "context_packet_id", "root_id"))


def _evidence_ids(packet: Mapping[str, Any]) -> Set[str]:
    result: Set[str] = set()
    for row in _rows(packet.get("evidence_refs")):
        value = _id_value(row, ("evidence_id", "id", "ref_id", "source_ref_id"))
        if value:
            result.add(value)
    return result


def _iter_packet_layer_rows(packet: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for name in LAYER_NAMES:
        for row in _rows(packet.get(name)):
            yield row
    dynamic = packet.get("dynamic_part") if isinstance(packet.get("dynamic_part"), Mapping) else {}
    for name in CANDIDATE_LAYERS:
        for row in _rows(dynamic.get(name)):
            yield row


def _source_scope_mismatches(packet: Mapping[str, Any]) -> Tuple[int, int]:
    """Return (mismatches, checked assertions) for one selected K5 packet."""

    expected = _scope_pair(packet)
    if expected is None:
        return 1, 0
    account, chat = expected
    mismatches = 0
    checked = 0
    for row in _iter_packet_layer_rows(packet):
        row_pair = _scope_pair(row)
        if row_pair is not None:
            checked += 1
            mismatches += int(row_pair != expected)
        for key in ENDPOINT_ACCOUNT_KEYS:
            value = row.get(key)
            if value not in (None, "", "unknown"):
                checked += 1
                mismatches += int(str(value) != account)
        for key in ENDPOINT_CHAT_KEYS:
            value = row.get(key)
            if value not in (None, "", "unknown"):
                checked += 1
                mismatches += int(str(value) != chat)
    return mismatches, checked


def _nested_key_count(value: Any, key: str) -> int:
    count = 0
    if isinstance(value, Mapping):
        for name, child in value.items():
            if str(name).casefold() == key.casefold() and child not in (None, "", [], (), {}):
                count += 1
            count += _nested_key_count(child, key)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            count += _nested_key_count(child, key)
    return count


def _page_formula(page_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Recompute one-root linear paging from the physical pages ledger."""

    by_root: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in page_rows:
        root = str(row.get("root_id") or "")
        if root:
            by_root[root].append(row)
    roots: List[Dict[str, Any]] = []
    for root_id, rows_for_root in sorted(by_root.items()):
        page_ids = {str(row.get("page_id")) for row in rows_for_root if row.get("page_id") not in (None, "")}
        counts = {
            "messages": sum(len(_values(row.get("message_handles"))) for row in rows_for_root),
            "candidates": sum(len(_values(row.get("candidate_handles"))) for row in rows_for_root),
            "evidence": sum(len(_values(row.get("evidence_handles"))) for row in rows_for_root),
        }
        # The values above are page dimensions, not the root's total handles.
        # For the selected artifact every root has one page.  When a synthetic
        # fixture has multiple rows, sum dimensions to preserve the same bound.
        terms = {
            "messages": max(1, math.ceil(counts["messages"] / MAX_MESSAGES)),
            "candidates": max(1, math.ceil(counts["candidates"] / MAX_CANDIDATES)),
            "evidence": max(1, math.ceil(counts["evidence"] / MAX_EVIDENCE)),
        }
        expected = max(terms.values())
        max_dimensions = {
            key: max((len(_values(row.get(field))) for row in rows_for_root), default=0)
            for key, field in (
                ("messages", "message_handles"),
                ("candidates", "candidate_handles"),
                ("evidence", "evidence_handles"),
            )
        }
        chunk_ok = all(
            max_dimensions[key] <= limit
            for key, limit in (
                ("messages", MAX_MESSAGES),
                ("candidates", MAX_CANDIDATES),
                ("evidence", MAX_EVIDENCE),
            )
        )
        actual = len(page_ids)
        roots.append(
            {
                "root_ref": _opaque("root", root_id),
                "actual_page_count": actual,
                "expected_linear_page_count": expected,
                "cartesian_upper_bound": terms["messages"] * terms["candidates"] * terms["evidence"],
                "max_page_dimensions": max_dimensions,
                "chunk_limits_ok": chunk_ok,
                "linear": bool(actual == expected and actual <= terms["messages"] * terms["candidates"] * terms["evidence"] and chunk_ok),
                "row_ref": _opaque("page_row", rows_for_root[0]),
            }
        )
    numerator = sum(bool(row["linear"]) for row in roots)
    return {
        "definition": "Distinct physical rows in pages.private.jsonl grouped by root_id; expected=max(ceil(total page message handles/24), ceil(candidate handles/64), ceil(evidence handles/64), 1).",
        "denominator": len(roots),
        "numerator": numerator,
        "observed_page_count": sum(int(row["actual_page_count"]) for row in roots),
        "expected_linear_page_count": sum(int(row["expected_linear_page_count"]) for row in roots),
        "cartesian_upper_bound": sum(int(row["cartesian_upper_bound"]) for row in roots),
        "max_observed_page_count": max((int(row["actual_page_count"]) for row in roots), default=0),
        "max_linear_page_bound": max((int(row["expected_linear_page_count"]) for row in roots), default=0),
        "max_cartesian_page_bound": max((int(row["cartesian_upper_bound"]) for row in roots), default=0),
        "linear": bool(roots) and numerator == len(roots),
        "actual_fields": [
            "pages.private.jsonl[root_id,page_id,message_handles,candidate_handles,evidence_handles]",
            "manifest.private.json[page_count]",
            "aggregate.private.json[metrics.page_formula]",
            "store.private.json[page_count]",
        ],
        "roots": roots,
    }


def _stage_a_metric(materialized_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Separate complete-budget accounting from pending diagnostics."""

    complete = [row for row in materialized_rows if str(row.get("status") or "").casefold() == "complete"]
    pending = [
        row
        for row in materialized_rows
        if str(row.get("status") or "").casefold() in {"pending", "open", "deferred", "blocked", "over_capacity"}
    ]

    def within(row: Mapping[str, Any]) -> bool:
        return bool(
            int(row.get("input_token_proxy") or 0) <= MAX_INPUT_TOKENS
            and int(row.get("user_token_proxy") or 0) <= MAX_USER_TOKENS
            and int(row.get("message_count") or 0) <= MAX_MESSAGES
            and int(row.get("candidate_count") or 0) <= MAX_CANDIDATES
            and int(row.get("evidence_count") or 0) <= MAX_EVIDENCE
        )

    complete_within = [row for row in complete if within(row)]
    pending_snapshot_ok = [
        row
        for row in pending
        if row.get("open_snapshot_ok") is True
        or bool(row.get("open_snapshot_ref"))
        or bool(row.get("open_snapshot"))
    ]
    return {
        "definition": "Complete-budget rate = complete Stage-A rows within all limits / complete Stage-A rows. Pending rows are a separate deferred population and must carry a replayable open snapshot.",
        "complete_denominator": len(complete),
        "complete_within_limits_numerator": len(complete_within),
        "complete_within_limits": bool(complete) and len(complete_within) == len(complete),
        "selected_row_count": len(materialized_rows),
        "pending_row_count": len(pending),
        "pending_snapshot_denominator": len(pending),
        "pending_snapshot_numerator": len(pending_snapshot_ok),
        "pending_snapshots_ok": len(pending_snapshot_ok) == len(pending),
        "max_complete_input_token_proxy": max((int(row.get("input_token_proxy") or 0) for row in complete), default=0),
        "max_complete_user_token_proxy": max((int(row.get("user_token_proxy") or 0) for row in complete), default=0),
        "max_all_row_input_token_proxy": max((int(row.get("input_token_proxy") or 0) for row in materialized_rows), default=0),
        "max_all_row_user_token_proxy": max((int(row.get("user_token_proxy") or 0) for row in materialized_rows), default=0),
        "limits": {
            "max_input_token_proxy": MAX_INPUT_TOKENS,
            "max_user_token_proxy": MAX_USER_TOKENS,
            "max_messages": MAX_MESSAGES,
            "max_candidates": MAX_CANDIDATES,
            "max_evidence": MAX_EVIDENCE,
        },
        "complete_row_ref": _opaque("complete_stage_a_row", complete[0]) if complete else None,
        "pending_row_ref": _opaque("pending_stage_a_row", pending[0]) if pending else None,
    }


def _evidence_metric(
    selected_packets: Sequence[Mapping[str, Any]],
    recovery_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    expected_by_root: Dict[str, Set[str]] = {}
    for packet in selected_packets:
        packet_id = _packet_id(packet)
        if packet_id:
            expected_by_root[packet_id] = _evidence_ids(packet)
    recovered_by_root: Dict[str, Set[str]] = {}
    for row in recovery_rows:
        root = str(row.get("source_packet_id") or row.get("root_id") or "")
        rates = row.get("rates") if isinstance(row.get("rates"), Mapping) else {}
        evidence_rate = rates.get("evidence") if isinstance(rates.get("evidence"), Mapping) else {}
        recovered_packet = row.get("recovered_packet") if isinstance(row.get("recovered_packet"), Mapping) else {}
        observed_ids = _evidence_ids(recovered_packet)
        # Prefer the recovered packet's top-level evidence_refs for the
        # independent ID check.  The rate fields are retained as a fallback for
        # a compact synthetic row and as a cross-check, but nested candidate
        # evidence is never traversed because it repeats the same IDs.
        expected_count = int(evidence_rate.get("expected") or 0)
        recovered_count = int(evidence_rate.get("recovered") or 0)
        recovered_by_root[root] = observed_ids or {f"count:{index}" for index in range(recovered_count)}
        if root not in expected_by_root:
            expected_by_root[root] = {f"count:{index}" for index in range(expected_count)}

    expected_ids = sum(len(values) for values in expected_by_root.values())
    recovered_ids = sum(
        min(len(expected_by_root.get(root, set())), len(recovered_by_root.get(root, set())))
        for root in expected_by_root
    )
    bearing_roots = {root for root, values in expected_by_root.items() if values}
    covered_bearing_roots = {
        root
        for root in bearing_roots
        if len(recovered_by_root.get(root, set())) >= len(expected_by_root[root])
    }
    return {
        "id_recovery": {
            "definition": "Per-root distinct baseline evidence IDs recovered, summed across roots; repeated IDs across roots remain separate root-scoped units.",
            "denominator": expected_ids,
            "numerator": recovered_ids,
            "rate": recovered_ids / expected_ids if expected_ids else 1.0,
            "actual_fields": [
                "K5 packets.private.jsonl[evidence_refs]",
                "K9 recovery_map.private.jsonl[rates.evidence.expected,recovered]",
                "aggregate.private.json[metrics.recovery_rates.evidence]",
            ],
        },
        "bearing_root_coverage": {
            "definition": "Roots with at least one baseline evidence ID that recover every root-scoped evidence ID.",
            "denominator": len(bearing_roots),
            "numerator": len(covered_bearing_roots),
            "selected_root_denominator": len(expected_by_root),
            "vacuous_root_count": len(expected_by_root) - len(bearing_roots),
            "rate": len(covered_bearing_roots) / len(bearing_roots) if bearing_roots else 1.0,
            "actual_fields": [
                "K5 packets.private.jsonl[evidence_refs]",
                "K9 recovery_map.private.jsonl[rates.evidence]",
            ],
        },
        "minimal_ref": _opaque("evidence_root", sorted(expected_by_root)[0]) if expected_by_root else None,
    }


def _body_free_scan(value: Any, path: str = "") -> List[str]:
    hits: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).casefold()
            current = f"{path}.{key_text}" if path else key_text
            if key_text in BODY_KEYS and child not in (None, "", [], (), {}):
                hits.append(current)
            hits.extend(_body_free_scan(child, current))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            hits.extend(_body_free_scan(child, f"{path}[{index}]"))
    return hits


def _identity_scan(value: Any, path: str = "") -> List[str]:
    hits: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).casefold()
            current = f"{path}.{key_text}" if path else key_text
            if key_text in IDENTITY_KEYS and child not in (None, "", [], (), {}):
                hits.append(current)
            hits.extend(_identity_scan(child, current))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            hits.extend(_identity_scan(child, f"{path}[{index}]"))
    return hits


def _assert_safe_report(report: Mapping[str, Any]) -> None:
    body_hits = _body_free_scan(report)
    identity_hits = _identity_scan(report)
    if body_hits or identity_hits:
        raise RuntimeError(
            "reconciliation report privacy check failed: "
            f"body={body_hits[:3]} identity={identity_hits[:3]}"
        )


def _validate_roots(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
    materialized: Sequence[Mapping[str, Any]],
    recovery: Sequence[Mapping[str, Any]],
    selection: Sequence[Mapping[str, Any]],
    store: Mapping[str, Any],
) -> Dict[str, Any]:
    selected_root_refs = {
        str(row.get("source_packet_id") or row.get("root_id") or "")
        for row in selection
        if row.get("source_packet_id") or row.get("root_id")
    }
    page_roots = {str(row.get("root_id") or "") for row in pages if row.get("root_id")}
    materialized_roots = {str(row.get("root_id") or "") for row in materialized if row.get("root_id")}
    recovery_roots = {
        str(row.get("source_packet_id") or row.get("root_id") or "")
        for row in recovery
        if row.get("source_packet_id") or row.get("root_id")
    }
    store_roots = {
        str(row.get("source_packet_id") or row.get("root_id") or "")
        for row in _rows(store.get("roots"))
        if row.get("source_packet_id") or row.get("root_id")
    }
    exact = all(
        len(rows) == SELECTION_LIMIT
        and len(root_set) == SELECTION_LIMIT
        and root_set == selected_root_refs
        for rows, root_set in (
            (pages, page_roots),
            (materialized, materialized_roots),
            (recovery, recovery_roots),
        )
    ) and len(store_roots) == SELECTION_LIMIT and store_roots == selected_root_refs
    return {
        "definition": "The selected K5 queue, K9 selection map, pages, materialized map, recovery map, and store must each cover the same twenty root lineage refs.",
        "denominator": SELECTION_LIMIT,
        "selection_numerator": len(selected_root_refs),
        "pages_numerator": len(page_roots),
        "materialized_numerator": len(materialized_roots),
        "recovery_numerator": len(recovery_roots),
        "store_numerator": len(store_roots),
        "exact": exact,
        "actual_fields": [
            "K5 audit_queue.private.jsonl[target_ref,packet_id]",
            "K9 selection_map.private.jsonl[source_packet_id,root_id]",
            "K9 pages.private.jsonl[root_id]",
            "K9 materialized_map.private.jsonl[root_id]",
            "K9 recovery_map.private.jsonl[source_packet_id,root_id]",
            "K9 store.private.json[roots]",
            "manifest.private.json[selected_packet_count,root_count]",
            "aggregate.private.json[metrics.selected_packet_count,root_count]",
        ],
    }


def reconcile(
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    k5_dir: Path = DEFAULT_K5_DIR,
) -> Dict[str, Any]:
    artifact_dir = Path(artifact_dir).resolve()
    k5_dir = Path(k5_dir).resolve()
    for path in (artifact_dir, k5_dir):
        if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in path.parts):
            raise RuntimeError(f"K9 reconciliation refuses frozen path: {path}")

    manifest_path = artifact_dir / "manifest.private.json"
    aggregate_path = artifact_dir / "aggregate.private.json"
    cost_path = artifact_dir / "cost.private.json"
    pages_path = artifact_dir / "pages.private.jsonl"
    materialized_path = artifact_dir / "materialized_map.private.jsonl"
    recovery_path = artifact_dir / "recovery_map.private.jsonl"
    selection_path = artifact_dir / "selection_map.private.jsonl"
    store_path = artifact_dir / "store.private.json"
    required_artifact_files = (
        manifest_path,
        aggregate_path,
        cost_path,
        pages_path,
        materialized_path,
        recovery_path,
        selection_path,
        store_path,
    )
    if not all(path.is_file() for path in required_artifact_files):
        missing = [str(path.name) for path in required_artifact_files if not path.is_file()]
        raise FileNotFoundError(f"K9 artifact files missing: {missing}")

    manifest = _load_json(manifest_path)
    aggregate = _load_json(aggregate_path)
    cost = _load_json(cost_path)
    pages = list(_iter_jsonl(pages_path))
    materialized = list(_iter_jsonl(materialized_path))
    recovery = list(_iter_jsonl(recovery_path))
    selection = list(_iter_jsonl(selection_path))
    store = _load_json(store_path)

    if manifest.get("artifact_version") != TARGET_ARTIFACT_VERSION:
        raise ValueError("K9 artifact version mismatch")
    if str(manifest.get("status") or "").casefold() != "complete":
        raise ValueError("K9 artifact manifest is not complete")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise ValueError("K9 artifact reports frozen/gold reads")
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("K9 artifact reports provider calls")

    k5_manifest_path = k5_dir / "manifest.private.json"
    queue_path = k5_dir / "audit_queue.private.jsonl"
    human_path = k5_dir / "audit" / "human_audit.private.jsonl"
    packet_path = k5_dir / "packets.private.jsonl"
    if not all(path.is_file() for path in (k5_manifest_path, queue_path, human_path, packet_path)):
        raise FileNotFoundError("K5 baseline files are incomplete")
    k5_manifest = _load_json(k5_manifest_path)
    if k5_manifest.get("artifact_version") != K5_ARTIFACT_VERSION:
        raise ValueError("K5 baseline version mismatch")
    if k5_manifest.get("frozen_read") is True or k5_manifest.get("gold_loaded") is True or k5_manifest.get("provider_called") is True:
        raise ValueError("K5 baseline reports forbidden reads/calls")
    queue = list(_iter_jsonl(queue_path))
    human = list(_iter_jsonl(human_path))
    queue_by_packet = {
        str(row.get("packet_id")): row
        for row in queue
        if row.get("packet_id") not in (None, "") and bool(row.get("target_ref"))
    }
    selected_packets: List[Dict[str, Any]] = []
    selected_packet_ids = set(queue_by_packet)
    for packet in _iter_jsonl(packet_path):
        if str(packet.get("packet_id") or "") in selected_packet_ids:
            selected_packets.append(packet)
    selected_packets.sort(key=lambda row: int(queue_by_packet[str(row["packet_id"])].get("selection_rank") or 0))

    page_metric = _page_formula(pages)
    stage_metric = _stage_a_metric(materialized)
    evidence_metric = _evidence_metric(selected_packets, recovery)
    lineage_metric = _validate_roots(manifest, aggregate, pages, materialized, recovery, selection, store)

    source_scope_mismatches = 0
    source_scope_assertions = 0
    for packet in selected_packets:
        mismatches, checked = _source_scope_mismatches(packet)
        source_scope_mismatches += mismatches
        source_scope_assertions += checked

    output_scope_mismatches = 0
    output_scope_assertions = 0
    root_scope_by_packet = {
        _packet_id(packet): _scope_pair(packet)
        for packet in selected_packets
        if _packet_id(packet) is not None and _scope_pair(packet) is not None
    }
    for row in pages:
        root = str(row.get("root_id") or row.get("source_packet_id") or "")
        expected = root_scope_by_packet.get(root)
        observed = _scope_pairs(row)
        if expected is not None:
            output_scope_assertions += 1
            output_scope_mismatches += sum(pair != expected for pair in observed)
    for row in materialized:
        root = str(row.get("root_id") or row.get("source_packet_id") or "")
        expected = root_scope_by_packet.get(root)
        observed = _scope_pairs(row)
        if expected is not None:
            output_scope_assertions += 1
            output_scope_mismatches += sum(pair != expected for pair in observed)
    for row in recovery:
        root = str(row.get("root_id") or row.get("source_packet_id") or "")
        expected = root_scope_by_packet.get(root)
        observed = _scope_pairs(row)
        if expected is not None:
            output_scope_assertions += 1
            output_scope_mismatches += sum(pair != expected for pair in observed)

    cross_chat_metric = {
        "definition": "A cross-chat violation exists only when a normalised explicit account/chat pair or endpoint scope differs from its selected root pair; repeated equal scope metadata is one assertion, not many violations.",
        "denominator": source_scope_assertions + output_scope_assertions,
        "numerator": source_scope_mismatches + output_scope_mismatches,
        "source_layer_denominator": source_scope_assertions,
        "source_layer_numerator": source_scope_mismatches,
        "output_row_denominator": output_scope_assertions,
        "output_row_numerator": output_scope_mismatches,
        "zero": source_scope_mismatches + output_scope_mismatches == 0,
        "actual_fields": [
            "K5 selected packets.private.jsonl[account_id,chat_id,scope,layer endpoint account/chat fields]",
            "K9 pages.private.jsonl[scope]",
            "K9 materialized_map.private.jsonl[scope and nested envelope scopes]",
            "K9 recovery_map.private.jsonl[recovered_packet scopes]",
            "aggregate.private.json[metrics.zero_tolerance.cross_chat_scope_violations]",
        ],
        "minimal_valid_duplicate_scope_ref": _opaque("repeated_same_scope_row", materialized[0]) if materialized else None,
    }

    # Reproduce the shape of the known stale page over-count without exposing
    # the underlying page/root identifiers.  It counts metadata occurrences
    # from three transport surfaces rather than distinct physical pages.
    nested_page_occurrence_components = {
        "materialized_map.page_id_occurrences": sum("page_id" in row for row in materialized),
        "recovery_map.page_refs_occurrences": sum(len(_values(row.get("page_refs"))) for row in recovery),
        "materialized_map.pending_open_snapshot.page_id_occurrences": sum(
            isinstance(row.get("open_snapshot"), Mapping) and row["open_snapshot"].get("page_id") not in (None, "")
            for row in materialized
        ),
    }
    nested_page_occurrences = sum(nested_page_occurrence_components.values())
    page_metric["legacy_occurrence_count_diagnostic"] = {
        "observed": nested_page_occurrences,
        "components": nested_page_occurrence_components,
        "why_not_page_count": "These are repeated metadata occurrences across materialized/recovery envelopes; only distinct pages.private.jsonl rows define page count.",
        "minimal_ref": _opaque("legacy_page_occurrence", materialized[0]) if materialized else None,
    }

    # Surface-level file checks are intentionally hashes/booleans only.
    artifact_hash_checks: Dict[str, bool] = {}
    declared_hashes = manifest.get("artifact_hashes") if isinstance(manifest.get("artifact_hashes"), Mapping) else {}
    for name, declared in declared_hashes.items():
        path = artifact_dir / str(name)
        artifact_hash_checks[str(name)] = path.is_file() and _sha256(path) == str(declared)

    aggregate_metrics = aggregate.get("metrics") if isinstance(aggregate.get("metrics"), Mapping) else {}
    aggregate_zero = aggregate_metrics.get("zero_tolerance") if isinstance(aggregate_metrics.get("zero_tolerance"), Mapping) else {}
    aggregate_page_formula = aggregate_metrics.get("page_formula") if isinstance(aggregate_metrics.get("page_formula"), Mapping) else {}
    aggregate_recovery = aggregate_metrics.get("recovery_rates") if isinstance(aggregate_metrics.get("recovery_rates"), Mapping) else {}

    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "reconciliation_status": "pass" if lineage_metric["exact"] and page_metric["linear"] and cross_chat_metric["zero"] and evidence_metric["id_recovery"]["numerator"] == evidence_metric["id_recovery"]["denominator"] else "fail",
        "scope": {
            "artifact_version": TARGET_ARTIFACT_VERSION,
            "artifact_manifest_ref": _opaque("manifest", _sha256(manifest_path)),
            "k5_manifest_ref": _opaque("k5_manifest", _sha256(k5_manifest_path)),
            "selected_root_count": len(selected_packets),
            "selected_root_limit": SELECTION_LIMIT,
            "frozen_read": False,
            "gold_loaded": False,
            "provider_called": False,
            "body_free": True,
        },
        "surfaces": {
            "manifest": {
                "status": manifest.get("status"),
                "selected_packet_count": int(manifest.get("selected_packet_count") or 0),
                "root_count": int(manifest.get("root_count") or 0),
                "page_count": int(manifest.get("page_count") or 0),
                "pending_stage_a_count": int(manifest.get("pending_stage_a_count") or 0),
                "artifact_hashes_match": all(artifact_hash_checks.values()) if artifact_hash_checks else False,
                "actual_fields": ["manifest.private.json[status,selected_packet_count,root_count,page_count,pending_stage_a_count,artifact_hashes]"],
            },
            "aggregate": {
                "status": aggregate.get("status"),
                "selected_packet_count": int(aggregate.get("selected_packet_count") or 0),
                "page_formula_actual_total": int(aggregate_page_formula.get("actual_total") or 0),
                "page_formula_expected_total": int(aggregate_page_formula.get("expected_total") or 0),
                "page_formula_linear": bool(aggregate_page_formula.get("linear")),
                "cross_chat_scope_violations": int(aggregate_zero.get("cross_chat_scope_violations") or 0),
                "evidence_expected": int((aggregate_recovery.get("evidence") or {}).get("expected") or 0) if isinstance(aggregate_recovery.get("evidence"), Mapping) else 0,
                "evidence_recovered": int((aggregate_recovery.get("evidence") or {}).get("recovered") or 0) if isinstance(aggregate_recovery.get("evidence"), Mapping) else 0,
                "actual_fields": ["aggregate.private.json[status,selected_packet_count,metrics.page_formula,metrics.zero_tolerance,metrics.recovery_rates]"],
            },
            "cost": {
                "stage_a_page_count": int(((cost.get("stage_a") or {}).get("page_count") or 0)) if isinstance(cost.get("stage_a"), Mapping) else 0,
                "stage_a_input_max_all_rows": int(((cost.get("stage_a") or {}).get("input_token_proxy_max") or 0)) if isinstance(cost.get("stage_a"), Mapping) else 0,
                "stage_a_user_max_all_rows": int(((cost.get("stage_a") or {}).get("user_token_proxy_max") or 0)) if isinstance(cost.get("stage_a"), Mapping) else 0,
                "actual_fields": ["cost.private.json[stage_a.page_count,input_token_proxy_max,user_token_proxy_max]"],
            },
            "pages": {
                "row_count": len(pages),
                "distinct_page_id_count": len({str(row.get("page_id")) for row in pages if row.get("page_id") not in (None, "")}),
                "distinct_root_count": len({str(row.get("root_id")) for row in pages if row.get("root_id")}),
                "actual_fields": ["pages.private.jsonl[root_id,page_id,message_handles,candidate_handles,evidence_handles]"],
            },
            "materialized": {
                "row_count": len(materialized),
                "status_counts": dict(sorted(Counter(str(row.get("status") or "unknown") for row in materialized).items())),
                "distinct_root_count": len({str(row.get("root_id")) for row in materialized if row.get("root_id")}),
                "actual_fields": ["materialized_map.private.jsonl[root_id,page_id,status,input_token_proxy,user_token_proxy,message_count,candidate_count,evidence_count,within_limits,open_snapshot_ref]"],
            },
            "recovery": {
                "row_count": len(recovery),
                "distinct_root_count": len({str(row.get("source_packet_id") or row.get("root_id")) for row in recovery if row.get("source_packet_id") or row.get("root_id")}),
                "actual_fields": ["recovery_map.private.jsonl[source_packet_id,root_id,page_refs,rates.evidence,recovered_packet]"],
            },
            "selection": {
                "row_count": len(selection),
                "distinct_root_count": len({str(row.get("source_packet_id") or row.get("root_id")) for row in selection if row.get("source_packet_id") or row.get("root_id")}),
                "actual_fields": ["selection_map.private.jsonl[selection_rank,source_packet_id,root_id,page_refs,page_count]"],
            },
            "store": {
                "root_count": int(store.get("root_count") or 0),
                "page_count": int(store.get("page_count") or 0),
                "message_table_count": len(_rows(store.get("messages"))),
                "candidate_table_count": len(_rows(store.get("candidates"))),
                "evidence_table_count": len(_rows(store.get("evidence"))),
                "content_table_count": len(store.get("content_table") or {}) if isinstance(store.get("content_table"), Mapping) else 0,
                "open_snapshot_count": len(store.get("open_snapshots") or {}) if isinstance(store.get("open_snapshots"), Mapping) else 0,
                "actual_fields": ["store.private.json[root_count,page_count,roots,pages,messages,candidates,evidence,content_table,open_snapshots]"],
            },
        },
        "metrics": {
            "lineage": lineage_metric,
            "page_linearity": page_metric,
            "cross_chat_scope": cross_chat_metric,
            "evidence_recovery": evidence_metric,
            "stage_a_budget": stage_metric,
        },
        "claim_reconciliation": {
            "page_count": {
                "runner_claim": 20,
                "audit_claim": 41,
                "authoritative_numerator": page_metric["numerator"],
                "authoritative_denominator": page_metric["denominator"],
                "authoritative_max_observed": page_metric["max_observed_page_count"],
                "authoritative_linear": page_metric["linear"],
                "implementation_error": "Audit occurrence-level traversal counted repeated page metadata across transport/recovery envelopes; page count is distinct physical pages.private.jsonl rows grouped by root_id.",
                "opaque_refs": [page_metric["legacy_occurrence_count_diagnostic"]["minimal_ref"]],
            },
            "cross_chat": {
                "runner_claim": 0,
                "audit_claim": 504,
                "authoritative_numerator": cross_chat_metric["numerator"],
                "authoritative_denominator": cross_chat_metric["denominator"],
                "authoritative_zero": cross_chat_metric["zero"],
                "implementation_error": "Audit treated repeated nested scope metadata as separate violations instead of comparing normalised scope pairs against the selected root.",
                "opaque_refs": [cross_chat_metric["minimal_valid_duplicate_scope_ref"]],
            },
            "evidence": {
                "runner_claim": "32/32 evidence IDs",
                "audit_claim": "16/20 roots",
                "authoritative_id_numerator": evidence_metric["id_recovery"]["numerator"],
                "authoritative_id_denominator": evidence_metric["id_recovery"]["denominator"],
                "authoritative_bearing_root_numerator": evidence_metric["bearing_root_coverage"]["numerator"],
                "authoritative_bearing_root_denominator": evidence_metric["bearing_root_coverage"]["denominator"],
                "vacuous_root_count": evidence_metric["bearing_root_coverage"]["vacuous_root_count"],
                "implementation_error": "No data error when both labels are explicit: the runner uses root-scoped evidence-ID units, while the audit uses evidence-bearing roots. The misleading form is 16/20 when it is labelled as ID recall.",
                "opaque_refs": [evidence_metric["minimal_ref"]],
            },
            "stage_a": {
                "runner_claim": "19 complete + 1 pending; complete budget pass",
                "audit_claim": "complete 0; max input 2222; max user 2204",
                "authoritative_complete_numerator": stage_metric["complete_within_limits_numerator"],
                "authoritative_complete_denominator": stage_metric["complete_denominator"],
                "authoritative_pending_count": stage_metric["pending_row_count"],
                "authoritative_complete_max_input": stage_metric["max_complete_input_token_proxy"],
                "authoritative_complete_max_user": stage_metric["max_complete_user_token_proxy"],
                "all_row_max_input_diagnostic": stage_metric["max_all_row_input_token_proxy"],
                "all_row_max_user_diagnostic": stage_metric["max_all_row_user_token_proxy"],
                "implementation_error": "Audit used all envelope occurrences/statuses for the complete gate and max, so the pending deferred row contaminated complete-budget statistics; it also failed to discover the authoritative top-level materialized rows in the stale complete-0 output.",
                "opaque_refs": [stage_metric["complete_row_ref"], stage_metric["pending_row_ref"]],
            },
        },
        "pilot_decision": {
            "strict_all_selected_pages": "blocked",
            "strict_all_selected_reason": "one selected page is pending and over the complete budget, although its open snapshot is recoverable",
            "scoped_complete_pages": stage_metric["complete_within_limits_numerator"],
            "scoped_complete_page_denominator": stage_metric["complete_denominator"],
            "scoped_stage_a_pilot": "allowed_only_for_complete_pages",
            "recommendation": "Do not call a 20-page all-selected Stage-A pilot. A 19-page scoped pilot is eligible only if the pilot scope explicitly excludes the pending page; resolve/retry the pending page before claiming K9-wide Stage-A readiness.",
            "pending_ref": stage_metric["pending_row_ref"],
        },
        "privacy_checks": {
            "body_key_count": 0,
            "identity_key_count": 0,
            "frozen_path_read": False,
            "provider_calls": 0,
            "output_refs": "opaque_only",
            "artifact_hash_checks": artifact_hash_checks,
        },
    }
    _assert_safe_report(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--k5-dir", type=Path, default=DEFAULT_K5_DIR)
    parser.add_argument("--output", type=Path, default=None, help="optional body-free JSON output path")
    args = parser.parse_args()
    report = reconcile(args.artifact_dir, args.k5_dir)
    if args.output is not None:
        output = Path(args.output).resolve()
        if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in output.parts):
            raise RuntimeError(f"reconciliation output refuses frozen path: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "reconciliation_status": report["reconciliation_status"],
                "lineage_exact": report["metrics"]["lineage"]["exact"],
                "page_linear": report["metrics"]["page_linearity"]["linear"],
                "cross_chat_zero": report["metrics"]["cross_chat_scope"]["zero"],
                "evidence_ids": [
                    report["metrics"]["evidence_recovery"]["id_recovery"]["numerator"],
                    report["metrics"]["evidence_recovery"]["id_recovery"]["denominator"],
                ],
                "complete_stage_a": [
                    report["metrics"]["stage_a_budget"]["complete_within_limits_numerator"],
                    report["metrics"]["stage_a_budget"]["complete_denominator"],
                ],
                "pending_stage_a": report["metrics"]["stage_a_budget"]["pending_row_count"],
                "strict_all_selected_pilot": report["pilot_decision"]["strict_all_selected_pages"],
                "scoped_pilot": report["pilot_decision"]["scoped_stage_a_pilot"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["reconciliation_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
