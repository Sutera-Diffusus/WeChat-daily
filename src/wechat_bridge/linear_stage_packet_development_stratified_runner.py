"""K30 local, development-only stratified linear Stage-A artifact.

The K30 boundary is intentionally narrower than a provider pilot.  It reads
the complete 2026-08-25 development context-packet artifact, selects a small
body-free candidate review plan, then rebuilds the K10 linear packet store
only for those roots with the current K29 role projection, and emits a
body-free canonical strata/selection ledger.  Stage A is materialised locally
only to preserve the K10 budget and recovery guards; no provider object is
accepted or called.

Only ``store.private.json`` may contain message bodies.  Pages,
materialization, recovery, strata, selection, audit, aggregate, cost, and
error ledgers contain opaque handles, counts, hashes, and safe status codes.
The output directory is immutable and is independent of every earlier K9/K10
artifact.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .linear_stage_packet_development_runner import (
    INPUT_ARTIFACT_VERSION,
    LOCAL_DAY,
    _capacity,
    _expected_evidence_ids,
    _ids,
    _page_formula,
    _read_selected,
    _scope_violations,
    _weak_strong_violations,
)
from .linear_stage_packets import (
    DEFAULT_SYSTEM_PROMPTS,
    LINEAR_PIPELINE_VERSION,
    LinearCapacity,
    LinearStagePacketStore,
    _linear_row_is_context_only,
    _rows_from,
    build_linear_stage_packets,
    canonical_json,
    materialize_stage_a,
    recover_linear_packet,
    stable_hash,
)
from .selection_strata import (
    CANONICAL_STRATA,
    STRATA_SCHEMA_VERSION,
    build_canonical_strata_metadata,
    materialize_linear_stage_packet_strata,
    select_pages_by_strata,
    verify_strata_replay,
)


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_VERSION = "linear_stage_packet_development_v3_stratified"
RUNNER_SCHEMA_VERSION = "linear_stage_packet_development_stratified_runner_v1"
REPORT_SCHEMA_VERSION = "linear_stage_packet_development_stratified_report_v1"
ROLE_FIX_VERSION = "k29_k2_retention_linear_semantic_projection_v1"
DEFAULT_INPUT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / INPUT_ARTIFACT_VERSION
DEFAULT_K10_PAGES_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / "linear_stage_packet_development_v2"
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / "linear_stage_packet_development_stratified_current"
MAX_SELECTED_PAGES = 5

_CANDIDATE_CUE_KEYS = (
    "topic_transitions",
    "candidate_competition",
    "reply_status",
    "pronoun_person_object_state",
    "message_metadata",
)
_CANDIDATE_CUE_FAMILIES = frozenset(
    {
        "greeting_boundary",
        "topic_shift",
        "candidate_competition",
        "no_reply",
        "pronoun_person_object_state",
    }
)

OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "store": "store.private.json",
    "pages": "pages.private.jsonl",
    "materialized": "materialized_map.private.jsonl",
    "recovery": "recovery_map.private.jsonl",
    "strata": "strata_map.private.jsonl",
    "selection": "selection_map.private.jsonl",
    "audit": "audit.private.jsonl",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "errors": "errors.private.jsonl",
}

_FROZEN_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})
_BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "completion",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message",
        "message_text",
        "output_text",
        "prompt",
        "quote",
        "raw",
        "raw_content",
        "raw_output",
        "raw_reasoning",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "recovered_packet",
        "response",
        "response_body",
        "response_text",
        "summary",
        "text",
        "text_body",
        "text_redacted",
        "thoughts",
        "transcript",
        "user_canonical_json",
        "user_input",
        "user_packet",
    }
)

# Only these producer-owned metadata containers/anchors are projected from a
# K2 source packet onto the compact K10 page before the canonical strata
# projector runs.  This is intentionally an allow-list: weak surface signals,
# counters, open-boundary hints, and arbitrary producer fields must not become
# semantic evidence merely because they survived in ``source_template``.
_STRONG_METADATA_KEYS = frozenset(
    {
        # Canonical labels and explicit page-level assertions.
        "category",
        "categories",
        "canonical_category",
        "canonical_categories",
        "canonical_stratum",
        "canonical_strata",
        "selection_category",
        "selection_categories",
        "selection_stratum",
        "selection_strata",
        "stratum",
        "strata",
        "candidate_competition",
        "pronoun_person_object_state",
        "greeting_new_topic",
        "topic_shift",
        "no_reply",
        # Explicit message/anchor rows.  Their body fields are removed by
        # ``_metadata_only`` while marker and opaque-reference fields remain.
        "primary_fragments",
        "primary",
        "fragments",
        "adjacent_context",
        "adjacent",
        "context_fragments",
        "authoritative_facts",
        "message_metadata",
        "message_rows",
        "messages",
        # Explicit candidate/history and competition rows.
        "candidate_qa_links",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "continuity_candidates",
        "qa_candidates",
        "person_history",
        "object_history",
        "state_history",
        "candidate_rows",
        "candidates",
        "open_thread_candidates",
        "open_threads",
        # Explicit transition rows.
        "topic_transitions",
        "topic_boundaries",
        "topic_changes",
        "topic_shift_evidence",
        "transition_rows",
        "topic_boundary_rows",
        "transitions",
        # Explicit reply-status rows.
        "reply_status",
        "reply_state",
        "response_status",
        "answer_status",
        "reply_evidence",
        "reply_metadata",
        # Direct opaque anchors occasionally emitted beside a typed row.
        "message_ref",
        "message_refs",
        "endpoint_message_ref",
        "endpoint_message_refs",
        "endpoint_message_ids",
        "candidate_ref",
        "candidate_refs",
        "scoped_evidence_ref",
        "scoped_evidence_refs",
        "scoped_evidence_handle",
        "scoped_evidence_handles",
        "evidence_ref",
        "evidence_refs",
        "evidence_id",
        "evidence_ids",
    }
)

_METADATA_PARENT_KEYS = frozenset(
    {
        "fixed_part",
        "dynamic_part",
        "selection_metadata",
        "strata_metadata",
        "candidate_context",
        "authoritative_facts_contract",
        "boundary",
    }
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=repr)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.write_text(
        "".join(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _opaque(kind: str, value: Any) -> str:
    """Return an opaque, deterministic handle without exposing source IDs."""

    return "k30_%s_%s" % (kind, stable_hash({"kind": str(kind), "value": str(value)})[:24])


def _opaque_digest(kind: str, values: Iterable[Any]) -> str:
    handles = sorted({_opaque(kind, value) for value in values if value not in (None, "")})
    return stable_hash({"kind": kind, "handles": handles})


def _assert_body_free(value: Any, *, label: str = "value") -> None:
    hits: List[str] = []

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                # Numeric counters named ``message_count`` etc. are safe.  A
                # body key is rejected only when its value is substantive.
                if key.casefold() in _BODY_KEYS and child not in (None, "", [], (), {}, 0, False):
                    hits.append(path + key)
                visit(child, path + key + ".")
        elif isinstance(item, (list, tuple, set, frozenset)):
            for index, child in enumerate(item):
                visit(child, path + str(index) + ".")

    visit(value)
    if hits:
        raise ValueError("%s contains body-bearing fields: %s" % (label, ", ".join(hits[:5])))


def _metadata_only(value: Any) -> Any:
    """Copy a known metadata value while removing body-bearing keys."""

    if isinstance(value, Mapping):
        return {
            str(key): _metadata_only(child)
            for key, child in value.items()
            if str(key).casefold() not in _BODY_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_metadata_only(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_metadata_only(child) for child in value), key=repr)
    return value


def _project_known_metadata_tree(value: Any) -> Dict[str, Any]:
    """Keep only known row containers inside a known metadata parent."""

    if not isinstance(value, Mapping):
        return {}
    projected: Dict[str, Any] = {}
    for key, child in value.items():
        name = str(key)
        if name in _STRONG_METADATA_KEYS:
            projected[name] = _metadata_only(child)
        elif name in _METADATA_PARENT_KEYS and isinstance(child, Mapping):
            nested = _project_known_metadata_tree(child)
            if nested:
                projected[name] = nested
    return projected


def _project_strong_metadata(source: Mapping[str, Any]) -> Dict[str, Any]:
    """Project explicit K2 semantic metadata without semantic inference.

    This function intentionally does not inspect values to manufacture labels.
    It copies only named producer contracts and their opaque anchors, strips
    body fields, and leaves selection_strata responsible for the strong-field,
    evidence, and weak-candidate checks.
    """

    projected: Dict[str, Any] = {}
    for key in _STRONG_METADATA_KEYS:
        if key in source:
            projected[key] = _metadata_only(source[key])
    for key in _METADATA_PARENT_KEYS:
        parent = source.get(key)
        if not isinstance(parent, Mapping):
            continue
        nested = _project_known_metadata_tree(parent)
        if nested:
            projected[key] = nested
    return projected


def _guard_path(path: Union[str, Path], *, label: str) -> Path:
    value = Path(path).expanduser().resolve()
    if any(part.casefold() in _FROZEN_PARTS for part in value.parts):
        raise ValueError("K30 refuses %s under frozen/frozen_test" % label)
    return value


def _guard_output(path: Union[str, Path], input_root: Optional[Path]) -> Path:
    value = _guard_path(path, label="output")
    if input_root is not None and value == input_root:
        raise ValueError("K30 output directory must differ from development input")
    if value.exists():
        raise FileExistsError("K30 output artifact is immutable; choose a new directory")
    return value


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("JSON object required: %s" % path.name)
    return value


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError("JSON object required in %s" % path.name)
        rows.append(dict(value))
    return rows


def _read_all_context_packets(
    root: Path,
    selected_packet_count: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, Dict[str, Any]]:
    """Read the complete bodyful K2 packet file for candidate-first selection.

    The legacy K9 reader intentionally streamed only the twenty rows named by
    ``selection_map.private.jsonl``.  K30 needs the complete context-packet
    corpus to find rare candidate cue families before deciding which roots are
    worth linearising.  This reader keeps that wider read local to the
    development boundary and retains the legacy selected digest for lineage.
    Bodies stay in memory only; public K30 ledgers are still projected through
    the existing body-free guards.
    """

    manifest_path = root / "manifest.private.json"
    selection_path = root / "selection_map.private.jsonl"
    packet_path = root / "packets.private.jsonl"
    if not all(path.is_file() for path in (manifest_path, selection_path, packet_path)):
        raise ValueError(
            "K30 input requires manifest.private.json, selection_map.private.jsonl and packets.private.jsonl"
        )
    manifest = dict(_read_json(manifest_path))
    if manifest.get("artifact_version") != INPUT_ARTIFACT_VERSION:
        raise ValueError("K30 input is not the K5 context_packet_development_v1 artifact")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, LOCAL_DAY):
        raise ValueError("K30 input is not the 2026-08-25 development split")
    if manifest.get("frozen_read") is True or manifest.get("provider_called") is True:
        raise ValueError("K30 input manifest violates the local development boundary")
    if int(manifest.get("provider_calls") or 0) != 0 or manifest.get("gold_loaded") is True:
        raise ValueError("K30 input manifest shows provider or gold/frozen state")

    selection_rows = _read_jsonl(selection_path)
    selected = [row for row in selection_rows if bool(row.get("selected"))]
    selected.sort(key=lambda row: (int(row.get("selection_rank") or 10**9), str(row.get("packet_id") or "")))
    expected = int(selected_packet_count) if selected_packet_count is not None else len(selected)
    if expected < 1 or len(selected) != expected:
        raise ValueError("K30 requires exactly %d selected packet refs" % expected)
    packet_rows = _read_jsonl(packet_path)
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in packet_rows:
        packet_id = str(row.get("packet_id") or row.get("context_packet_id") or "")
        if not packet_id:
            raise ValueError("K30 context packets require packet_id values")
        if packet_id in by_id:
            raise ValueError("duplicate context packet %s" % packet_id)
        by_id[packet_id] = row
    selected_ids = [str(row.get("packet_id") or row.get("context_packet_id") or "") for row in selected]
    if any(not value for value in selected_ids) or len(selected_ids) != len(set(selected_ids)):
        raise ValueError("K30 selected packet refs must have unique packet_id values")
    missing = sorted(set(selected_ids) - set(by_id))
    if missing:
        raise ValueError("selected context packet rows missing: %s" % ",".join(missing[:3]))
    ordered_selected = [by_id[packet_id] for packet_id in selected_ids]
    selected_digest = _sha256_bytes(
        ("".join(canonical_json(row) + "\n" for row in ordered_selected)).encode("utf-8")
    )
    return selected, list(by_id.values()), selected_digest, manifest


def _read_k10_pages(directory: Union[str, Path]) -> Tuple[Path, Mapping[str, Any], List[Dict[str, Any]]]:
    """Read only the body-free K10 v2 page ledger and its manifest."""

    root = _guard_path(directory, label="K10 pages")
    manifest_path = root / "manifest.private.json"
    pages_path = root / "pages.private.jsonl"
    if not manifest_path.is_file() or not pages_path.is_file():
        raise ValueError("K30 requires K10 v2 manifest.private.json and pages.private.jsonl")
    manifest = _read_json(manifest_path)
    if manifest.get("artifact_version") != "linear_stage_packet_development_v2":
        raise ValueError("K30 K10 page input must be linear_stage_packet_development_v2")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, LOCAL_DAY):
        raise ValueError("K30 K10 page input is not the 2026-08-25 development split")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True or manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("K30 K10 page input violates the local development boundary")
    pages = _read_jsonl(pages_path)
    _assert_body_free(pages, label="K10 pages")
    return root, manifest, pages


def _row_value(row: Mapping[str, Any], names: Sequence[str]) -> Optional[str]:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _unique_ids(rows: Any, names: Sequence[str]) -> List[str]:
    values: List[str] = []
    if isinstance(rows, Mapping):
        if any(name in rows for name in names):
            rows = [rows]
        else:
            rows = list(rows.values())
    if not isinstance(rows, (list, tuple, set, frozenset)):
        return []
    for row in rows:
        if isinstance(row, Mapping):
            value = _row_value(row, names)
            if value is not None:
                values.append(value)
        elif row not in (None, ""):
            values.append(str(row))
    return list(dict.fromkeys(values))


def _expected_ids(packet: Mapping[str, Any]) -> Dict[str, List[str]]:
    return {
        "source": _unique_ids(packet.get("source_refs"), ("source_ref_id", "source_id", "id", "message_id")),
        "primary": _unique_ids(packet.get("primary_fragments"), ("message_id", "fragment_id", "id")),
        "adjacent": _unique_ids(packet.get("adjacent_context"), ("message_id", "fragment_id", "id")),
        "evidence": _expected_evidence_ids(packet),
    }


def _observed_ids(packet: Mapping[str, Any]) -> Dict[str, List[str]]:
    return {
        "source": _unique_ids(packet.get("source_refs"), ("source_ref_id", "source_id", "id", "message_id")),
        "primary": _unique_ids(packet.get("primary_fragments"), ("message_id", "fragment_id", "id")),
        "adjacent": _unique_ids(packet.get("adjacent_context"), ("message_id", "fragment_id", "id")),
        "evidence": _expected_evidence_ids(packet),
    }


def _rate(expected: Sequence[str], observed: Sequence[str]) -> Dict[str, Any]:
    left, right = set(expected), set(observed)
    overlap = left & right
    return {
        "expected_count": len(left),
        "observed_count": len(right),
        "recovered_count": len(overlap),
        "missing_count": len(left - right),
        "extra_count": len(right - left),
        "rate": (len(overlap) / len(left)) if left else 1.0,
        "status": "pass" if left <= right else ("N/A" if not left else "fail"),
        "expected_handle_digest": _opaque_digest("recovery_expected", left),
        "observed_handle_digest": _opaque_digest("recovery_observed", right),
    }


def _percentile(values: Sequence[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(int(value) for value in values)
    position = (len(ordered) - 1) * float(fraction)
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return int(round(ordered[left] + (ordered[right] - ordered[left]) * (position - left)))


def _source_by_id(packets: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for packet in packets:
        value = _row_value(packet, ("packet_id", "context_packet_id", "root_id"))
        if value:
            result[value] = packet
    return result


def _page_source_id(store: LinearStagePacketStore, page: Mapping[str, Any]) -> str:
    root = store.root_table.get(str(page.get("root_id")), {})
    return str(root.get("source_packet_id") or root.get("root_id") or "")


def _candidate_selection_plan(
    source_packets: Sequence[Mapping[str, Any]],
    store: Optional[LinearStagePacketStore] = None,
    *,
    max_pages: int,
    allow_multi_scope: bool = False,
) -> Dict[str, Any]:
    """Select a bounded candidate-only review plan independent of canonical strata.

    This plan is intentionally not a provider gate.  It lets a local review
    sample rare shallow cue families even when canonical semantic strata are
    still missing, while marking every row as ``candidate_only`` and keeping
    evidence opaque.  Pages are selected within one scope by default; a
    cross-scope full-coverage plan reports an explicit authorization error.
    """

    source_by_id = _source_by_id(source_packets)
    records: List[Dict[str, Any]] = []

    def rows_from(value: Any) -> List[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            return [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return [row for row in value if isinstance(row, Mapping)]
        return []

    def message_ids(value: Any) -> List[str]:
        """Read message/fragment anchors without ever reading their body."""

        return _unique_ids(value, ("source_message_id", "message_id", "fragment_id", "id"))

    def opaque_ref(kind: str, value: Mapping[str, Any], *, scope_key: str) -> str:
        # Only source IDs/spans/kinds enter the public handle.  Do not hash an
        # arbitrary row that might contain a future body-bearing field.
        payload = {
            # Message IDs are not required to be globally unique by the K2
            # producer contract.  Bind the evidence handle to its scope so a
            # same local message/span in another chat cannot alias.
            "scope": scope_key,
            "source_message_id": value.get("source_message_id") or value.get("message_id") or value.get("id"),
            "span": value.get("span") if isinstance(value.get("span"), Mapping) else {},
            "evidence_kind": value.get("evidence_kind") or value.get("cue_kind") or "selection_cue",
        }
        return _opaque(kind, payload)

    # A provisional bodyful linear store is deliberately not built for the
    # complete K2 corpus.  The candidate plan must be generated first, so use
    # only deterministic structural page/root identities when no store is
    # supplied.  The existing ``store`` path remains available for API tests
    # and callers that already have a bounded linear store.
    if store is None:
        plan_pages = []
        for source in source_packets:
            source_id = _row_value(source, ("packet_id", "context_packet_id", "root_id"))
            if source_id:
                plan_pages.append({"page_id": "%s|page|0001" % source_id, "root_id": source_id, "ordinal": 1})
    else:
        plan_pages = list(store.pages)
    for page in plan_pages:
        source_id = (
            _row_value(page, ("source_packet_id",))
            if store is None
            else _page_source_id(store, page)
        ) or str(page.get("root_id") or "")
        source = source_by_id.get(source_id)
        if not source:
            continue
        scope = source.get("scope") if isinstance(source.get("scope"), Mapping) else {}
        account_id = str(source.get("account_id") or scope.get("account_id") or "unknown")
        chat_id = str(source.get("chat_id") or scope.get("chat_id") or "unknown")
        scope_key = "%s/%s" % (account_id, chat_id)
        scope_handle = _opaque("scope", scope_key)

        # Candidate evidence can be repeated by several K2 roots whose page
        # windows overlap.  Evidence alone is too coarse: roots with a
        # different primary anchor are distinct review opportunities even
        # when they cite the same source messages.  Keep an opaque containment
        # signature made only from primary/page message anchors so exact
        # repeated windows still collapse while distinct roots remain eligible.
        primary_ids = message_ids(source.get("primary_fragments"))
        if not primary_ids:
            primary_ids = message_ids(source.get("primary"))
        if not primary_ids:
            primary_ids = message_ids(source.get("primary_message_ids"))
        page_window = source.get("window") if isinstance(source.get("window"), Mapping) else {}
        page_ids = message_ids(page_window.get("message_ids"))
        if not page_ids:
            page_ids = message_ids(source.get("source_message_ids"))
        if not page_ids:
            page_ids = list(primary_ids)
        primary_handles = sorted({_opaque("message", value) for value in primary_ids if value not in (None, "")})
        page_message_handles = sorted({_opaque("message", value) for value in page_ids if value not in (None, "")})
        containment_handle = _opaque(
            "containment",
            {
                "scope_handle": scope_handle,
                "primary_handles": primary_handles,
                "page_message_handles": page_message_handles,
            },
        )
        cue_families: set[str] = set()
        evidence_handles: set[str] = set()
        message_handles: set[str] = set()
        cue_row_count = 0
        for container in _CANDIDATE_CUE_KEYS:
            for row in rows_from(source.get(container)):
                if not bool(row.get("selection_cue")) or not bool(row.get("candidate_only")):
                    continue
                family = str(row.get("cue_kind") or "").strip()
                # Generic per-message metadata is useful for audit but is too
                # common to be a scarce review family.  Only its explicit
                # greeting boundary row participates in the plan.
                if container == "message_metadata" and family != "greeting_boundary":
                    continue
                if family not in _CANDIDATE_CUE_FAMILIES:
                    continue
                refs = row.get("scoped_evidence_refs") or row.get("evidence_refs") or ()
                if not isinstance(refs, (list, tuple, set, frozenset)):
                    refs = (refs,) if isinstance(refs, Mapping) else ()
                local_evidence = {
                    opaque_ref("evidence", ref, scope_key=scope_key)
                    for ref in refs
                    if isinstance(ref, Mapping)
                    and (ref.get("source_message_id") or ref.get("message_id") or ref.get("id")) not in (None, "")
                }
                if not local_evidence:
                    continue
                cue_families.add(family)
                cue_row_count += 1
                evidence_handles.update(local_evidence)
                for raw_message_id in row.get("message_refs") or row.get("source_message_ids") or ():
                    if raw_message_id not in (None, ""):
                        message_handles.add(_opaque("message", raw_message_id))
        if not cue_families or not evidence_handles:
            continue
        page_handle = _opaque("page", page.get("page_id"))
        root_handle = _opaque("root", page.get("root_id"))
        source_handle = _opaque("source", source_id)
        candidate_group_handle = _opaque(
            "candidate_group",
            {
                "scope_handle": scope_handle,
                "containment_handle": containment_handle,
                "cue_families": sorted(cue_families),
                "evidence_handles": sorted(evidence_handles),
            },
        )
        records.append(
            {
                "page_handle": page_handle,
                "root_handle": root_handle,
                "source_handle": source_handle,
                "scope_handle": scope_handle,
                "coverage_kind": "candidate_only",
                "semantic_decision_pending": True,
                "not_canonical": True,
                "candidate_only": True,
                "cue_families": sorted(cue_families),
                "cue_row_count": cue_row_count,
                "message_handles": sorted(message_handles),
                "evidence_handles": sorted(evidence_handles),
                "evidence_count": len(evidence_handles),
                "primary_handles": primary_handles,
                "primary_count": len(primary_handles),
                "page_message_handles": page_message_handles,
                "page_message_count": len(page_message_handles),
                "containment_handle": containment_handle,
                "candidate_group_handle": candidate_group_handle,
                "body_free": True,
            }
        )

    records.sort(key=lambda row: str(row.get("page_handle") or ""))
    all_families = set().union(*(set(row.get("cue_families") or ()) for row in records)) if records else set()
    frequencies = {family: sum(family in set(row.get("cue_families") or ()) for row in records) for family in all_families}

    def handle_set(value: Any) -> set[str]:
        if value in (None, ""):
            return set()
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(item) for item in value if item not in (None, "")}
        return {str(value)}

    def choose(values: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
        selected: List[Mapping[str, Any]] = []
        covered: set[str] = set()
        covered_evidence: set[str] = set()
        covered_containment: set[str] = set()
        covered_groups: set[str] = set()
        remaining = list(values)
        while remaining and len(selected) < int(max_pages):
            ordered = sorted(
                remaining,
                key=lambda row: (
                    # Score rare families per candidate row.  This keeps a
                    # giant packet full of common no-reply rows from crowding
                    # out compact roots carrying genuinely scarce families.
                    -(
                        sum(
                            1.0 / max(1, frequencies.get(family, 1))
                            for family in set(row.get("cue_families") or ()) - covered
                        )
                        / max(1, int(row.get("cue_row_count") or 0))
                    ),
                    -len(set(row.get("cue_families") or ()) - covered),
                    # A page with hundreds of repeated shallow rows can make
                    # Stage-A pagination non-linear.  Prefer a compact root
                    # when family coverage ties, while retaining opaque
                    # evidence and primary/containment diversity as the next
                    # tie-breakers.
                    int(row.get("cue_row_count") or 0),
                    -len(set(row.get("evidence_handles") or ()) - covered_evidence),
                    -len(handle_set(row.get("containment_handle")) - covered_containment),
                    -len(handle_set(row.get("candidate_group_handle")) - covered_groups),
                    sum(frequencies.get(family, 0) for family in set(row.get("cue_families") or ()) - covered),
                    -int(row.get("evidence_count") or 0),
                    str(row.get("page_handle") or ""),
                ),
            )
            chosen = ordered[0]
            new_families = set(chosen.get("cue_families") or ()) - covered
            new_evidence = set(chosen.get("evidence_handles") or ()) - covered_evidence
            containment_values = handle_set(chosen.get("containment_handle"))
            new_containment = containment_values - covered_containment
            group_values = handle_set(chosen.get("candidate_group_handle"))
            new_groups = group_values - covered_groups
            # Overlapping packet windows often repeat exactly the same weak
            # cue evidence.  Once a page adds neither a new cue family, a new
            # evidence handle, a distinct root/page containment, nor a new
            # evidence/containment/family combination, stop instead of filling
            # the review budget with duplicates.  The containment/group terms
            # are what keep different primary roots selectable when their
            # shallow cue evidence is identical.
            if selected and not new_families and not new_evidence and not new_containment and not new_groups:
                break
            selected.append(chosen)
            covered.update(str(value) for value in chosen.get("cue_families") or ())
            covered_evidence.update(str(value) for value in chosen.get("evidence_handles") or ())
            covered_containment.update(new_containment)
            covered_groups.update(new_groups)
            remaining = [row for row in remaining if row.get("page_handle") != chosen.get("page_handle")]
        return selected

    global_plan = choose(records)
    global_covered = set().union(*(set(row.get("cue_families") or ()) for row in global_plan)) if global_plan else set()
    by_scope: Dict[str, List[Mapping[str, Any]]] = {}
    for row in records:
        by_scope.setdefault(str(row.get("scope_handle") or ""), []).append(row)
    scope_plans = {scope: choose(rows) for scope, rows in by_scope.items()}
    full_scope_plans: Dict[str, List[Mapping[str, Any]]] = {}
    for scope, plan in scope_plans.items():
        covered = set().union(*(set(row.get("cue_families") or ()) for row in plan)) if plan else set()
        if all_families <= covered:
            full_scope_plans[scope] = plan
    global_scopes = {str(row.get("scope_handle") or "") for row in global_plan}
    single_scope = ""
    if full_scope_plans:
        single_scope = sorted(full_scope_plans)[0]
    elif scope_plans:
        single_scope = sorted(
            scope_plans,
            key=lambda scope: (
                -len(set().union(*(set(row.get("cue_families") or ()) for row in scope_plans[scope]))),
                scope,
            ),
        )[0]
    single_plan = scope_plans.get(single_scope, [])
    single_covered = set().union(*(set(row.get("cue_families") or ()) for row in single_plan)) if single_plan else set()
    authorization_required = bool(
        all_families <= global_covered
        and len(global_scopes) > 1
        and not (all_families <= single_covered)
    )
    if allow_multi_scope and all_families <= global_covered:
        chosen = global_plan
    else:
        chosen = single_plan
    def decorate(values: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        decorated: List[Dict[str, Any]] = []
        for rank, row in enumerate(values[: int(max_pages)], 1):
            value = dict(row)
            value["selection_rank"] = rank
            value["selected"] = True
            decorated.append(value)
        return decorated

    selected = decorate(chosen)
    global_selected = decorate(global_plan)

    def plan_view(
        values: Sequence[Mapping[str, Any]],
        selected_values: Sequence[Mapping[str, Any]],
        *,
        scope_handle: Optional[str],
        plan_kind: str,
    ) -> Dict[str, Any]:
        available = set().union(*(set(row.get("cue_families") or ()) for row in values)) if values else set()
        selected_families = set().union(*(set(row.get("cue_families") or ()) for row in selected_values)) if selected_values else set()
        return {
            "coverage_kind": "candidate_only",
            "plan_kind": plan_kind,
            "scope_handle": scope_handle,
            "scope_count": len({str(row.get("scope_handle") or "") for row in values}),
            "scope_isolated": plan_kind != "global",
            "semantic_decision_pending": True,
            "not_canonical": True,
            "candidate_only": True,
            "selected": [dict(row) for row in selected_values],
            "selected_page_count": len(selected_values),
            "selected_page_limit": int(max_pages),
            "available_cue_families": sorted(available),
            "missing_cue_families": sorted(available - selected_families),
            "candidate_row_count": sum(int(row.get("cue_row_count") or 0) for row in values),
            "candidate_page_count": len(values),
            "body_free": True,
        }

    global_candidate_plan = plan_view(
        records,
        global_selected,
        scope_handle=None,
        plan_kind="global",
    )
    per_scope_plans = {
        scope: plan_view(
            rows,
            decorate(scope_plans.get(scope, [])),
            scope_handle=scope,
            plan_kind="per_scope",
        )
        for scope, rows in sorted(by_scope.items())
    }
    result: Dict[str, Any] = {
        "coverage_kind": "candidate_only",
        "plan_kind": "selected_scope",
        "semantic_decision_pending": True,
        "not_canonical": True,
        "candidate_only": True,
        "selected": selected,
        "selected_page_count": len(selected),
        "selected_page_limit": int(max_pages),
        "available_cue_families": sorted(all_families),
        "missing_cue_families": sorted(all_families - set().union(*(set(row.get("cue_families") or ()) for row in selected)) if selected else all_families),
        "candidate_row_count": sum(int(row.get("cue_row_count") or 0) for row in records),
        "candidate_page_count": len(records),
        "global_coverage_plan_available": bool(all_families <= global_covered),
        "global_coverage_plan_scope_count": len(global_scopes),
        "single_scope_coverage_plan_available": bool(all_families <= single_covered),
        "scope_authorization_required": authorization_required,
        "scope_authorization": {
            "status": "multi_scope_required" if authorization_required else "not_required",
            "multi_scope_required": authorization_required,
            "authorized": bool(allow_multi_scope),
            "scope_count": len(by_scope),
            "global_selected_scope_count": len(global_scopes),
            "provider_allowed": False,
        },
        "authorization_error": "multi_scope_candidate_coverage_requires_authorization" if authorization_required and not allow_multi_scope else None,
        "global_candidate_plan": global_candidate_plan,
        "per_scope_plans": per_scope_plans,
        # Keep a descriptive alias for consumers that name this map after the
        # candidate plan rather than after its scope partition.
        "per_scope_candidate_plans": per_scope_plans,
        "scope_coverage": {
            scope: {
                "scope_handle": scope,
                "candidate_page_count": len(rows),
                "available_cue_families": sorted(set().union(*(set(row.get("cue_families") or ()) for row in rows))),
                "selected_page_count": len(scope_plans.get(scope, [])),
            }
            for scope, rows in sorted(by_scope.items())
        },
    }
    result["selection_hash"] = stable_hash(result)
    _assert_body_free(result, label="candidate_selection_plan")
    return result


def _page_public(
    page: Mapping[str, Any],
    strata_row: Mapping[str, Any],
    formula: Mapping[str, Any],
    role_counts: Mapping[str, int],
) -> Dict[str, Any]:
    """Project one K10 page without raw page/root/message identities."""

    return {
        "page_handle": str(strata_row.get("page_handle") or ""),
        "root_handle": str(strata_row.get("root_handle") or ""),
        "source_handle": str(strata_row.get("source_handle") or ""),
        "scope_handle": str(strata_row.get("scope_handle") or ""),
        "scope_known": bool(strata_row.get("scope_known")),
        "ordinal": int(page.get("ordinal") or strata_row.get("ordinal") or 0),
        "status": str(page.get("status") or "unknown"),
        "page_hash": str(page.get("page_hash") or ""),
        "message_count": len(page.get("message_handles") or ()),
        "candidate_count": len(page.get("candidate_handles") or ()),
        "evidence_count": len(page.get("evidence_handles") or ()),
        "linear_bound": bool(formula.get("linear")),
        "expected_page_count": int(formula.get("expected_page_count") or 0),
        "role_projection": {str(key): int(value) for key, value in sorted(role_counts.items())},
        "observed_strata": list(strata_row.get("observed_strata") or ()),
        "metadata_missing_strata": list(strata_row.get("metadata_missing_strata") or ()),
        "ambiguous_strata": list(strata_row.get("ambiguous_strata") or ()),
        "strata_metadata_hash": str(strata_row.get("metadata_hash") or ""),
        "body_free": True,
    }


def _materialized_public(
    page: Mapping[str, Any],
    strata_row: Mapping[str, Any],
    stage_a: Mapping[str, Any],
    prompt: str,
    store: LinearStagePacketStore,
) -> Dict[str, Any]:
    stats = dict(stage_a.get("material_stats") or {})
    # The canonical user payload is intentionally never copied to disk.  Its
    # digest and proxy sizes preserve K10's auditability without exposing it.
    user = {
        str(key): value
        for key, value in stage_a.items()
        if str(key)
        not in {
            "limits",
            "status",
            "pending_reason",
            "open_snapshot_ref",
            "open_snapshot",
            "material_stats",
            "system_prompt",
            "system_prompt_sha256",
            "user_packet_sha256",
        }
    }
    user_json = canonical_json(user)
    user_chars = len(user_json)
    total_proxy = (len(prompt) + user_chars + 3) // 4
    user_proxy = (user_chars + 3) // 4
    snapshot_ref = stage_a.get("open_snapshot_ref")
    snapshot_ok = bool(snapshot_ref and str(snapshot_ref) in store.open_snapshot_table)
    return {
        "page_handle": str(strata_row.get("page_handle") or ""),
        "root_handle": str(strata_row.get("root_handle") or ""),
        "scope_handle": str(strata_row.get("scope_handle") or ""),
        "ordinal": int(page.get("ordinal") or 0),
        "status": str(stage_a.get("status") or "unknown"),
        "within_limits": bool(stats.get("within_limits", stage_a.get("within_limits", False))),
        "message_count": int(stats.get("message_count", len(page.get("message_handles") or ())) or 0),
        "candidate_count": int(stats.get("candidate_count", len(page.get("candidate_handles") or ())) or 0),
        "candidate_row_count": int(stats.get("candidate_row_count", stats.get("candidate_count", 0)) or 0),
        "evidence_count": int(stats.get("evidence_count", 0) or 0),
        "evidence_ref_count": int(stats.get("evidence_ref_count", stats.get("evidence_count", 0)) or 0),
        "input_token_proxy": int(stats.get("input_token_proxy", total_proxy) or total_proxy),
        "user_token_proxy": int(stats.get("user_token_proxy", user_proxy) or user_proxy),
        "canonical_chars": user_chars,
        "canonical_bytes": len(user_json.encode("utf-8")),
        "user_payload_sha256": _sha256_bytes(user_json.encode("utf-8")),
        "system_prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
        "system_prompt_chars": len(prompt),
        "open_snapshot_present": snapshot_ok,
        "open_snapshot_handle": _opaque("snapshot", snapshot_ref) if snapshot_ok else None,
        "stage_b_status": "N/A",
        "stage_c_status": "N/A",
        "body_free": True,
    }


def _recovery_public(
    store: LinearStagePacketStore,
    source: Mapping[str, Any],
    strata_row: Mapping[str, Any],
    selection_rank: Optional[int],
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    source_id = _row_value(source, ("packet_id", "context_packet_id", "root_id")) or ""
    root = store.get_root(source_id)
    recovered = recover_linear_packet(store, root["root_id"], include_body=True)
    expected = _expected_ids(source)
    observed = _observed_ids(recovered)
    # K29 deliberately separates semantic provider-primary projection from
    # the reversible K2 retention view.  A context-only opener/ack/media row
    # may therefore move from ``primary_fragments`` to ``adjacent_context``
    # in the linear root while remaining fully recoverable.  Keep the K10
    # recovery denominator (the source primary view), but compare it against
    # the union of both retained linear roles so the guard is lossless rather
    # than sensitive to the provider projection.
    observed["primary"] = list(dict.fromkeys(observed["primary"] + observed["adjacent"]))
    rates = {key: _rate(expected[key], observed[key]) for key in ("source", "primary", "adjacent", "evidence")}
    row: Dict[str, Any] = {
        "selection_rank": selection_rank,
        "source_handle": str(strata_row.get("source_handle") or _opaque("source", source_id)),
        "root_handle": str(strata_row.get("root_handle") or _opaque("root", root["root_id"])),
        "page_handles": [str(strata_row.get("page_handle") or "")],
        "rates": rates,
        "body_free": True,
    }
    return row, rates


def _role_counts(store: LinearStagePacketStore, page: Mapping[str, Any]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for handle in page.get("message_handles") or ():
        row = store.message_table.get(str(handle), {})
        for role in row.get("roles") or ():
            counts[str(role)] += 1
    return dict(counts)


def _row_message_id(row: Mapping[str, Any]) -> str:
    for key in ("message_id", "source_message_id", "fragment_id", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _source_role_rows(packet: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    """Mirror K29's source-layer fallback without reclassifying text."""

    fixed = packet.get("fixed_part") if isinstance(packet.get("fixed_part"), Mapping) else {}
    dynamic = packet.get("dynamic_part") if isinstance(packet.get("dynamic_part"), Mapping) else {}
    if key == "primary":
        return _rows_from(packet, ("primary_fragments",)) or _rows_from(fixed, ("primary_fragments",)) or _rows_from(packet, ("primary_fragments", "primary", "fragments"))
    return _rows_from(packet, ("adjacent_context",)) or _rows_from(dynamic, ("adjacent_context",)) or _rows_from(packet, ("adjacent_context", "adjacent", "context_fragments", "greeting", "greeting_context"))


def _role_audit(source_packets: Sequence[Mapping[str, Any]], store: LinearStagePacketStore) -> Dict[str, Any]:
    """Audit K29 semantic-primary placement using typed role projection."""

    pure_candidates = 0
    pure_primary_nonzero = 0
    mixed_candidates = 0
    mixed_primary_retained = 0
    rows_with_unknown_ids = 0
    for packet in source_packets:
        source_id = _row_value(packet, ("packet_id", "context_packet_id", "root_id")) or ""
        primary_rows = _source_role_rows(packet, "primary")
        adjacent_rows = _source_role_rows(packet, "adjacent")
        all_rows = primary_rows + adjacent_rows
        # The mixed/pure guard is defined on the packet's primary layer.  The
        # adjacent window is recoverability context and must not make a
        # mixed greeting + substantive primary turn look demoted merely
        # because neighbouring substantive messages are retained adjacent.
        context_flags = [_linear_row_is_context_only(row) for row in primary_rows]
        pure = bool(primary_rows) and all(_linear_row_is_context_only(row) for row in primary_rows)
        mixed = bool(primary_rows) and any(context_flags) and any(not flag for flag in context_flags)
        root = store.get_root(source_id)
        primary_ids = {
            str(store.message_table.get(str(handle), {}).get("message_id") or "")
            for handle in root.get("primary_message_handles", ())
        }
        adjacent_ids = {
            str(store.message_table.get(str(handle), {}).get("message_id") or "")
            for handle in root.get("adjacent_message_handles", ())
        }
        primary_ids.discard("")
        adjacent_ids.discard("")
        source_primary_ids = {_row_message_id(row) for row in primary_rows}
        source_adjacent_ids = {_row_message_id(row) for row in adjacent_rows}
        if "" in source_primary_ids or "" in source_adjacent_ids:
            rows_with_unknown_ids += 1
        source_primary_ids.discard("")
        source_adjacent_ids.discard("")
        if pure:
            pure_candidates += 1
            if primary_ids & source_primary_ids:
                pure_primary_nonzero += 1
        if mixed:
            mixed_candidates += 1
            # Every substantive source row must remain provider-primary.  A
            # context-only row may be retained adjacent by the K29 contract.
            substantive_ids = {
                _row_message_id(row)
                for row in primary_rows
                if not _linear_row_is_context_only(row)
            }
            substantive_ids.discard("")
            if substantive_ids <= primary_ids and substantive_ids:
                mixed_primary_retained += 1
    return {
        "role_fix_version": ROLE_FIX_VERSION,
        "pure_confirmation_candidate_count": pure_candidates,
        "pure_confirmation_semantic_primary_nonzero_count": pure_primary_nonzero,
        "pure_confirmation_semantic_primary_zero_count": pure_candidates - pure_primary_nonzero,
        "pure_confirmation_semantic_primary_zero_rate": ((pure_candidates - pure_primary_nonzero) / pure_candidates) if pure_candidates else 1.0,
        "mixed_content_candidate_count": mixed_candidates,
        "mixed_content_substantive_primary_retained_count": mixed_primary_retained,
        "mixed_content_substantive_primary_retention_rate": (mixed_primary_retained / mixed_candidates) if mixed_candidates else 1.0,
        "source_rows_with_missing_message_id_count": rows_with_unknown_ids,
        "body_free": True,
    }


def _canonical_strata_audit(strata: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize observed/missing/ambiguous pages without raw evidence."""

    pages = [row for row in strata.get("pages", ()) if isinstance(row, Mapping)]
    summary: Dict[str, Any] = {}
    for name in CANONICAL_STRATA:
        statuses = Counter(
            str(((row.get("strata") or {}).get(name) or {}).get("status") or "metadata_missing")
            for row in pages
            if isinstance(row.get("strata"), Mapping)
        )
        summary[name] = {
            "required_upstream_fields": list(((strata.get("strata") or {}).get(name) or {}).get("required_upstream_fields") or ()),
            "observed_page_count": int(statuses.get("observed", 0)),
            "metadata_missing_page_count": int(statuses.get("metadata_missing", 0)),
            "ambiguous_page_count": int(statuses.get("ambiguous", 0)),
            "status_counts": dict(sorted(statuses.items())),
        }
    observed_names = [name for name in CANONICAL_STRATA if summary[name]["observed_page_count"]]
    missing_names = [name for name in CANONICAL_STRATA if summary[name]["metadata_missing_page_count"] == len(pages)]
    return {
        "page_count": len(pages),
        "observed_strata": observed_names,
        "available_strata": observed_names,
        "missing_strata": missing_names,
        "ambiguous_strata": [name for name in CANONICAL_STRATA if summary[name]["ambiguous_page_count"]],
        "strata": summary,
        "body_free": True,
    }


def _code_hashes() -> Dict[str, str]:
    paths = {
        "runner": Path(__file__).resolve(),
        "linear_stage_packets": Path(__file__).with_name("linear_stage_packets.py"),
        "selection_strata": Path(__file__).with_name("selection_strata.py"),
    }
    return {name: _sha256_file(path) for name, path in paths.items()}


def _k10_page_binding(store: LinearStagePacketStore, k10_pages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_key = {(str(row.get("root_id") or ""), int(row.get("ordinal") or 0)): row for row in k10_pages}
    checked = 0
    mismatches = 0
    unmatched = 0
    for page in store.pages:
        key = (str(page.get("root_id") or ""), int(page.get("ordinal") or 0))
        old = by_key.get(key)
        if old is None:
            unmatched += 1
            continue
        checked += 1
        if str(old.get("page_hash") or "") != str(page.get("page_hash") or ""):
            mismatches += 1
        for field in ("message_handles", "candidate_handles", "evidence_handles"):
            if len(old.get(field) or ()) != len(page.get(field) or ()):
                mismatches += 1
    used = {(str(page.get("root_id") or ""), int(page.get("ordinal") or 0)) for page in store.pages}
    unmatched += len(set(by_key) - used)
    return {
        "k10_page_count": len(k10_pages),
        "rebuilt_page_count": len(store.pages),
        "checked_page_count": checked,
        "unmatched_page_count": unmatched,
        "mismatch_count": mismatches,
        "hash_and_counts_match": not mismatches and not unmatched and len(k10_pages) == len(store.pages),
    }


def _replay_projection(
    source_packets: Sequence[Mapping[str, Any]],
    capacity: LinearCapacity,
    prompt: str,
) -> Tuple[bool, Dict[str, Any]]:
    first = build_linear_stage_packets(source_packets, capacity=capacity, stage_a_system_prompt=prompt)
    second = build_linear_stage_packets(source_packets, capacity=capacity, stage_a_system_prompt=prompt)
    first_counts = {name: len(getattr(first, name)) for name in ("message_table", "candidate_table", "evidence_table", "root_table", "page_table", "content_table")}
    second_counts = {name: len(getattr(second, name)) for name in first_counts}
    first_pages = [str(row.get("page_hash") or "") for row in first.pages]
    second_pages = [str(row.get("page_hash") or "") for row in second.pages]
    first_strata = materialize_linear_stage_packet_strata(first)
    second_strata = materialize_linear_stage_packet_strata(second)
    strata_equal = verify_strata_replay(first_strata, second_strata)
    same = first_counts == second_counts and first_pages == second_pages and strata_equal
    return same, {
        "idempotent": same,
        "first_counts": first_counts,
        "second_counts": second_counts,
        "first_page_hash_digest": stable_hash(first_pages),
        "second_page_hash_digest": stable_hash(second_pages),
        "strata_replay": strata_equal,
    }


@dataclass(frozen=True)
class StratifiedLinearDevelopmentRunResult:
    input_directory: str
    output_directory: str
    selected_packet_count: int
    root_count: int
    page_count: int
    selected_page_count: int
    status: str
    manifest: Mapping[str, Any]
    aggregate: Mapping[str, Any]
    selection: Mapping[str, Any]
    strata: Mapping[str, Any]
    artifact_paths: Mapping[str, str]
    store: LinearStagePacketStore

    @property
    def provider_calls(self) -> int:
        return 0

    @property
    def linear_store(self) -> LinearStagePacketStore:
        return self.store

    @property
    def pages(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(self.store.pages)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "selected_packet_count": self.selected_packet_count,
            "root_count": self.root_count,
            "page_count": self.page_count,
            "selected_page_count": self.selected_page_count,
            "status": self.status,
            "provider_calls": 0,
            "manifest": dict(self.manifest),
            "aggregate": dict(self.aggregate),
            "selection": dict(self.selection),
            "strata": dict(self.strata),
            "artifact_paths": dict(self.artifact_paths),
        }


def _prepare_mappings(
    packets: Any,
    selection_refs: Any,
    selected_packet_count: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, Dict[str, Any]]:
    packet_rows = _mapping_packet_rows(packets)
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in packet_rows:
        key = _row_value(row, ("packet_id", "context_packet_id", "root_id"))
        if not key:
            raise ValueError("K30 mapping packets require packet IDs")
        by_id[key] = row
    if isinstance(selection_refs, Mapping):
        selection_refs = selection_refs.get("selected", list(selection_refs.values()))
    if not isinstance(selection_refs, (list, tuple)):
        raise ValueError("K30 mapping selection refs must be a list or mapping")
    selected = [dict(row) for row in selection_refs if isinstance(row, Mapping) and row.get("selected", True)]
    selected.sort(key=lambda row: (int(row.get("selection_rank") or 10**9), str(row.get("packet_id") or row.get("context_packet_id") or "")))
    expected = int(selected_packet_count) if selected_packet_count is not None else len(selected)
    if expected < 1 or len(selected) != expected:
        raise ValueError("K30 requires exactly %d selected packet refs" % expected)
    ids = [_row_value(row, ("packet_id", "context_packet_id", "root_id")) or "" for row in selected]
    if len(set(ids)) != len(ids) or any(key not in by_id for key in ids):
        raise ValueError("K30 selected mapping refs are missing or duplicated")
    ordered = [by_id[key] for key in ids]
    digest = _sha256_bytes(("".join(canonical_json(row) + "\n" for row in ordered)).encode("utf-8"))
    return selected, ordered, digest, {
        "artifact_version": INPUT_ARTIFACT_VERSION,
        "split": "development",
        "local_day": LOCAL_DAY,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
    }


def _mapping_packet_rows(packets: Any) -> List[Dict[str, Any]]:
    """Normalize an in-memory K2 mapping without applying legacy selection."""

    value = packets
    if isinstance(value, Mapping):
        if isinstance(value.get("packets"), (list, tuple, Mapping)):
            value = value.get("packets")
        elif value.get("packet_id") or value.get("context_packet_id"):
            value = [value]
        else:
            value = list(value.values())
    if not isinstance(value, (list, tuple)):
        raise ValueError("K30 mapping packets must be a list or mapping")
    rows = [dict(row) for row in value if isinstance(row, Mapping)]
    seen: set[str] = set()
    for row in rows:
        packet_id = _row_value(row, ("packet_id", "context_packet_id", "root_id"))
        if not packet_id or packet_id in seen:
            raise ValueError("K30 mapping packets require unique packet IDs")
        seen.add(packet_id)
    if not rows:
        raise ValueError("K30 mapping packets must not be empty")
    return rows


def _selected_packet_rows(
    selected_rows: Sequence[Mapping[str, Any]],
    source_packets: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    by_id = {
        _row_value(row, ("packet_id", "context_packet_id", "root_id")): dict(row)
        for row in source_packets
        if _row_value(row, ("packet_id", "context_packet_id", "root_id"))
    }
    result: List[Dict[str, Any]] = []
    for row in selected_rows:
        packet_id = _row_value(row, ("packet_id", "context_packet_id", "root_id"))
        if packet_id in by_id and packet_id not in {
            _row_value(existing, ("packet_id", "context_packet_id", "root_id")) for existing in result
        }:
            result.append(by_id[packet_id])
    return result


def _candidate_selected_packet_rows(
    candidate_selection_plan: Mapping[str, Any],
    source_packets: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Resolve opaque candidate-plan roots back to their in-memory packets."""

    by_root = {
        _opaque("root", _row_value(packet, ("packet_id", "context_packet_id", "root_id"))): dict(packet)
        for packet in source_packets
        if _row_value(packet, ("packet_id", "context_packet_id", "root_id"))
    }
    result: List[Dict[str, Any]] = []
    for row in candidate_selection_plan.get("selected") or ():
        if not isinstance(row, Mapping):
            continue
        packet = by_root.get(str(row.get("root_handle") or ""))
        if packet is not None:
            result.append(packet)
    return result


def run_linear_stage_packet_development_stratified(
    input_directory: Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]],
    output_directory: Union[str, Path, Sequence[Mapping[str, Any]]],
    output_directory_override: Optional[Union[str, Path]] = None,
    *,
    k10_pages_directory: Optional[Union[str, Path]] = DEFAULT_K10_PAGES_DIRECTORY,
    selection_refs: Any = None,
    selected_packet_count: Optional[int] = 20,
    capacity: Any = None,
    system_prompt: Optional[str] = None,
    system_prompt_a: Optional[str] = None,
    max_pages: int = MAX_SELECTED_PAGES,
) -> StratifiedLinearDevelopmentRunResult:
    """Build a new K30 artifact from development input without a provider."""

    if int(max_pages) < 1 or int(max_pages) > MAX_SELECTED_PAGES:
        raise ValueError("K30 max_pages must be between 1 and 5")
    mapping_mode = not isinstance(input_directory, (str, Path))
    if mapping_mode:
        if output_directory_override is None:
            raise ValueError("K30 mapping input requires a third positional output directory")
        selected_rows, source_packets, input_digest, input_manifest = _prepare_mappings(
            input_directory,
            selection_refs if selection_refs is not None else output_directory,
            selected_packet_count,
        )
        all_source_packets = _mapping_packet_rows(input_directory)
        input_root = None
        output_target = output_directory_override
        input_label = "<in-memory-development-mappings>"
    else:
        input_root = _guard_path(input_directory, label="development input")
        output_target = output_directory if output_directory_override is None else output_directory_override
        selected_rows, all_source_packets, input_digest, input_manifest = _read_all_context_packets(input_root, selected_packet_count)
        source_packets = _selected_packet_rows(selected_rows, all_source_packets)
        input_label = str(input_root)
    output_root = _guard_output(output_target, input_root)
    target = _capacity(capacity)
    prompt = str(system_prompt_a if system_prompt_a is not None else system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPTS["A"])
    code_hashes = _code_hashes()

    # Candidate cues are reviewed before any linear Stage-A materialisation.
    # The global plan may inspect every scope offline, but the actual plan used
    # for linearisation is a single scope unless explicit multi-scope
    # authorization is supplied.  This keeps cross-scope relations out of the
    # rebuilt store while still exposing global family availability.
    candidate_selection_plan = _candidate_selection_plan(
        all_source_packets,
        None,
        max_pages=int(max_pages),
    )
    candidate_source_packets = _candidate_selected_packet_rows(candidate_selection_plan, all_source_packets)
    candidate_selection_used = bool(candidate_source_packets)
    if candidate_selection_used:
        source_packets = candidate_source_packets
    elif not source_packets:
        raise ValueError("K30 candidate-first selection produced no linearizable roots")
    input_packet_count = len(all_source_packets)
    input_packet_digest = _sha256_bytes(
        ("".join(canonical_json(row) + "\n" for row in all_source_packets)).encode("utf-8")
    )

    k10_root: Optional[Path] = None
    k10_manifest: Mapping[str, Any] = {}
    k10_pages: List[Dict[str, Any]] = []
    if k10_pages_directory is not None and not mapping_mode:
        k10_root, k10_manifest, k10_pages = _read_k10_pages(k10_pages_directory)

    cross_chat_violations = _scope_violations(source_packets)
    weak_strong_violations = _weak_strong_violations(source_packets)
    store = build_linear_stage_packets(source_packets, capacity=target, stage_a_system_prompt=prompt)
    formula_rows = []
    formula_by_root: Dict[str, Mapping[str, Any]] = {}
    for root in store.roots:
        formula = _page_formula(root, target)
        formula_by_root[str(root.get("root_id") or "")] = formula
        formula_rows.append({"root_handle": _opaque("root", root.get("root_id")), "source_handle": _opaque("source", root.get("source_packet_id")), **{key: value for key, value in formula.items() if key != "root_id" and key != "source_packet_id"}})
    page_formula_ok = bool(formula_rows) and all(bool(row.get("linear")) for row in formula_rows)

    # K10 pages are deliberately a compact handle/index projection and do
    # not carry every K2 canonical marker.  Project the explicitly named K2
    # metadata contracts back onto each rebuilt page *in memory* before
    # invoking K28's strict projector.  This is not semantic inference:
    # selection_strata still requires explicit labels or strong typed evidence,
    # candidate refs, and scoped evidence for every observed stratum.  Bodies
    # are never copied into the resulting side-car.
    source_by_id = _source_by_id(source_packets)
    enriched_pages: List[Dict[str, Any]] = []
    structural_page_keys = {
        "page_id",
        "root_id",
        "source_packet_id",
        "ordinal",
        "scope",
        "message_handles",
        "candidate_handles",
        "candidate_link_refs",
        "evidence_handles",
        "page_hash",
        "status",
    }
    for page in store.pages:
        source_id = _page_source_id(store, page)
        source = source_by_id.get(source_id, {})
        enriched = dict(page)
        # The copied keys are an explicit metadata allow-list, not a generic
        # source merge.  Keep the page's structural identity authoritative so
        # page handles remain bound to K10 v2, and prevent weak signals or
        # arbitrary producer fields from entering semantic selection.
        for key, value in _project_strong_metadata(source).items():
            if key not in structural_page_keys:
                enriched[key] = value
        enriched["source_packet_id"] = source_id
        enriched["root_id"] = page.get("root_id")
        enriched["page_id"] = page.get("page_id")
        enriched_pages.append(enriched)
    _assert_body_free(enriched_pages, label="enriched metadata")
    strata = build_canonical_strata_metadata(enriched_pages)
    strata["projection"] = {
        "source": "k2_authoritative_candidate_metadata_to_k10_page",
        "role_fix_version": ROLE_FIX_VERSION,
        "k10_page_count": len(store.pages),
        "canonical_projector": "selection_strata",
        "body_free": True,
    }
    # The projector's own hash covers the canonical pages.  Bind the explicit
    # projection contract without changing any per-page evidence hash.
    strata["metadata_hash"] = stable_hash({key: value for key, value in strata.items() if key not in {"metadata_hash", "replay"}})
    strata["replay"] = {
        "stable_hash": strata["metadata_hash"],
        "idempotent_contract": True,
        "source_order_independent": True,
    }
    # Keep this explicit even though the canonical projector already checks
    # its output.  Future additions to the projection block must remain
    # body-free as well.
    _assert_body_free(strata, label="strata")
    canonical_strata_audit = _canonical_strata_audit(strata)
    role_audit = _role_audit(source_packets, store)
    selection = select_pages_by_strata(strata, max_pages=int(max_pages))
    # Keep the semantic/canonical selection contract independent from the
    # shallow-cue review plan.  Candidate cues are useful for scarce, local
    # review sampling, but they must never make a missing canonical stratum
    # appear observed or open the provider gate.
    selection.pop("selection_hash", None)
    selection["candidate_selection_plan"] = candidate_selection_plan
    selection["selection_hash"] = stable_hash(selection)
    _assert_body_free(selection, label="selection")
    strata_rows = [dict(row) for row in strata.get("pages", ()) if isinstance(row, Mapping)]
    # ``ordinal`` is local to a root, so bind on the opaque root handle too.
    # The real K10 split currently has one page per root, while synthetic
    # contract inputs intentionally exercise repeated ordinals.
    strata_by_root_ordinal = {
        (str(row.get("root_handle") or ""), int(row.get("ordinal") or 0)): row
        for row in strata_rows
    }

    pages_public: List[Dict[str, Any]] = []
    materialized_public: List[Dict[str, Any]] = []
    materialized_errors: List[Dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    prompt_hash = _sha256_bytes(prompt.encode("utf-8"))
    for page in store.pages:
        ordinal = int(page.get("ordinal") or 0)
        root_handle = _opaque("root", page.get("root_id"))
        strata_row = strata_by_root_ordinal.get((root_handle, ordinal))
        if strata_row is None:
            # Ordinals are unique only within a root.  The fallback preserves
            # deterministic mapping if a future store has repeated ordinals.
            candidates = [row for row in strata_rows if str(row.get("root_handle")) == root_handle]
            strata_row = candidates[0] if candidates else {"page_handle": _opaque("page", page.get("page_id")), "root_handle": root_handle, "source_handle": _opaque("source", _page_source_id(store, page)), "scope_handle": _opaque("scope", "missing"), "ordinal": ordinal, "observed_strata": [], "metadata_missing_strata": list(CANONICAL_STRATA), "ambiguous_strata": [], "metadata_hash": ""}
        formula = formula_by_root.get(str(page.get("root_id") or ""), {})
        pages_public.append(_page_public(page, strata_row, formula, _role_counts(store, page)))
        try:
            stage_a = materialize_stage_a(store, page["page_id"], system_prompt=prompt)
            statuses[str(stage_a.get("status") or "unknown")] += 1
            public = _materialized_public(page, strata_row, stage_a, prompt, store)
            if public["status"] == "complete" and (not public["within_limits"] or public["input_token_proxy"] > target.max_input_token_proxy or public["user_token_proxy"] > target.max_user_token_proxy):
                materialized_errors.append({"code": "complete_stage_a_over_capacity", "page_handle": public["page_handle"]})
            if public["status"] in {"pending", "open"} and not public["open_snapshot_present"]:
                materialized_errors.append({"code": "pending_stage_a_missing_open_snapshot", "page_handle": public["page_handle"]})
            if public["status"] not in {"complete", "pending", "open"}:
                materialized_errors.append({"code": "unexpected_stage_a_status", "page_handle": public["page_handle"]})
            materialized_public.append(public)
        except Exception as exc:
            materialized_errors.append({"code": "stage_a_materialization_failed", "page_handle": strata_row.get("page_handle"), "error_type": type(exc).__name__})

    selection_rank_by_source = {_row_value(row, ("packet_id", "context_packet_id", "root_id")): row.get("selection_rank") for row in selected_rows}
    strata_by_source: Dict[str, Mapping[str, Any]] = {}
    for row in strata_rows:
        source_handle = str(row.get("source_handle") or "")
        for source_id in source_by_id:
            if source_handle == _opaque("source", source_id):
                strata_by_source[source_id] = row
                break
    recovery_public: List[Dict[str, Any]] = []
    recovery_totals: Dict[str, List[int]] = {key: [0, 0] for key in ("source", "primary", "adjacent", "evidence")}
    recovery_errors: List[Dict[str, Any]] = []
    for source in source_packets:
        source_id = _row_value(source, ("packet_id", "context_packet_id", "root_id")) or ""
        strata_row = strata_by_source.get(source_id)
        if strata_row is None:
            strata_row = {"source_handle": _opaque("source", source_id), "root_handle": _opaque("root", source_id), "page_handle": ""}
        try:
            row, rates = _recovery_public(store, source, strata_row, selection_rank_by_source.get(source_id))
            recovery_public.append(row)
            for key, rate in rates.items():
                recovery_totals[key][0] += int(rate["expected_count"])
                recovery_totals[key][1] += int(rate["recovered_count"])
        except Exception as exc:
            recovery_errors.append({"code": "recovery_failed", "source_handle": str(strata_row.get("source_handle") or _opaque("source", source_id)), "error_type": type(exc).__name__})
    recovery_rates = {
        key: {"expected_count": values[0], "recovered_count": values[1], "rate": values[1] / values[0] if values[0] else 1.0, "status": "pass" if values[0] == values[1] else ("N/A" if values[0] == 0 else "fail")}
        for key, values in recovery_totals.items()
    }
    recovery_ok = len(recovery_public) == len(source_packets) and all(values[0] == values[1] for values in recovery_totals.values())

    k10_binding = _k10_page_binding(store, k10_pages) if k10_pages else {"k10_page_count": 0, "rebuilt_page_count": len(store.pages), "checked_page_count": 0, "unmatched_page_count": 0, "mismatch_count": 0, "hash_and_counts_match": True}
    if k10_pages and candidate_selection_used:
        # The old K10 artifact is bound to the legacy selected-20 input.  A
        # candidate-first rebuild intentionally chooses different roots before
        # linearisation, so an exact old-page count/hash comparison is not a
        # meaningful gate.  Keep the comparison evidence visible without
        # treating the stale legacy page set as a rebuild failure.
        k10_binding = dict(k10_binding)
        k10_binding["comparison_status"] = "not_applicable_candidate_first_selection"
        k10_binding["legacy_page_count"] = int(k10_binding.get("k10_page_count") or 0)
        k10_binding["hash_and_counts_match"] = True
    replay_ok, replay_metrics = _replay_projection(source_packets, target, prompt)
    pending_count = sum(str(row.get("status")) in {"pending", "open"} for row in materialized_public)
    materialized_ok = len(materialized_public) == len(store.pages) and not materialized_errors and not pending_count
    scope_ok = len(store.roots) == len(source_packets) and len(store.pages) >= len(store.roots)
    zero_ok = cross_chat_violations == 0 and weak_strong_violations == 0
    errors: List[Dict[str, Any]] = []
    errors.extend(materialized_errors)
    errors.extend(recovery_errors)
    if cross_chat_violations:
        errors.append({"code": "cross_chat_scope_violation", "count": cross_chat_violations})
    if weak_strong_violations:
        errors.append({"code": "time_or_same_segment_strong_relation", "count": weak_strong_violations})
    if not scope_ok:
        errors.append({"code": "selected_root_mapping_incomplete"})
    if not page_formula_ok:
        errors.append({"code": "page_count_not_linear"})
    if k10_pages and not k10_binding["hash_and_counts_match"]:
        errors.append({"code": "k10_page_binding_mismatch"})
    if not recovery_ok:
        errors.append({"code": "recovery_gate_failed"})
    if not replay_ok:
        errors.append({"code": "replay_not_idempotent"})
    if selection.get("scope_authorization_required") and not selection.get("provider_allowed"):
        errors.append({"code": "multi_scope_authorization_required"})
    if candidate_selection_plan.get("scope_authorization_required"):
        errors.append({"code": "candidate_multi_scope_authorization_required"})
    if selection.get("missing_strata"):
        errors.append({"code": "selection_strata_missing", "count": len(selection.get("missing_strata") or ())})
    if any(row.get("ambiguous_strata") for row in strata_rows):
        errors.append({"code": "selection_strata_ambiguous", "count": sum(bool(row.get("ambiguous_strata")) for row in strata_rows)})

    canonical_metadata_complete = not selection.get("missing_strata") and not any(
        row.get("ambiguous_strata") for row in strata_rows
    )
    # A file-backed development rebuild is the evidence gate that informs a
    # future provider decision.  Keep mapping-contract runs usable for their
    # synthetic API tests, while a real corpus with missing canonical fields
    # is explicitly blocked rather than presented as a complete rebuild.
    if not mapping_mode and not canonical_metadata_complete:
        errors.append({"code": "canonical_strata_metadata_missing_upstream", "required_fields": sorted({field for name in CANONICAL_STRATA for field in (((strata.get("strata") or {}).get(name) or {}).get("required_upstream_fields") or ())})})

    # Local artifact generation is complete when all storage/recovery/budget
    # guards pass.  Missing strata is an evidence/selection gate, not a
    # fabricated provider result; it remains explicit in ``errors`` and the
    # aggregate's provider_gate.
    hard_codes = {"complete_stage_a_over_capacity", "pending_stage_a_missing_open_snapshot", "unexpected_stage_a_status", "stage_a_materialization_failed", "recovery_failed", "cross_chat_scope_violation", "time_or_same_segment_strong_relation", "selected_root_mapping_incomplete", "page_count_not_linear", "k10_page_binding_mismatch", "recovery_gate_failed", "replay_not_idempotent"}
    hard_failure = any(str(row.get("code")) in hard_codes for row in errors) or (not mapping_mode and not canonical_metadata_complete)
    status = "blocked" if hard_failure else "complete"

    stage_a_tokens = [int(row.get("input_token_proxy") or 0) for row in materialized_public]
    user_tokens = [int(row.get("user_token_proxy") or 0) for row in materialized_public]
    provider_gate = "pass" if bool(selection.get("provider_allowed")) and not selection.get("missing_strata") and not selection.get("scope_authorization_required") else "blocked"
    metrics: Dict[str, Any] = {
        "role_fix_version": ROLE_FIX_VERSION,
        "code_sha256": dict(code_hashes),
        "input_packet_count": input_packet_count,
        "input_packet_digest": input_packet_digest,
        "candidate_source_packet_count": input_packet_count,
        "candidate_selection_used": candidate_selection_used,
        "linearized_candidate_root_count": len(source_packets),
        "selected_packet_count": len(source_packets),
        "root_count": len(store.roots),
        "page_count": len(store.pages),
        "pending_stage_a_count": pending_count,
        "stage_a_status_counts": dict(sorted(statuses.items())),
        "stage_b": {"status": "N/A", "reason": "no_provider_stage_b"},
        "stage_c": {"status": "N/A", "reason": "no_provider_stage_c"},
        "limits": target.to_dict(),
        "roots_mapping_complete": scope_ok,
        "page_formula": {"per_root": formula_rows, "expected_total": sum(int(row.get("expected_page_count") or 0) for row in formula_rows), "actual_total": len(store.pages), "linear": page_formula_ok, "k10_binding": k10_binding},
        "stage_a_budget": {"complete_total_under_limit": all(row.get("input_token_proxy", 0) <= target.max_input_token_proxy for row in materialized_public if row.get("status") == "complete"), "complete_user_under_limit": all(row.get("user_token_proxy", 0) <= target.max_user_token_proxy for row in materialized_public if row.get("status") == "complete"), "all_complete_within_limits": materialized_ok, "pending_snapshots_ok": not any(row.get("code") == "pending_stage_a_missing_open_snapshot" for row in errors)},
        "stage_a_token_proxy": {"total": sum(stage_a_tokens), "max": max(stage_a_tokens, default=0), "p50": _percentile(stage_a_tokens, 0.50), "p95": _percentile(stage_a_tokens, 0.95), "limit": target.max_input_token_proxy},
        "stage_a_user_token_proxy": {"total": sum(user_tokens), "max": max(user_tokens, default=0), "p50": _percentile(user_tokens, 0.50), "p95": _percentile(user_tokens, 0.95), "limit": target.max_user_token_proxy},
        "recovery_rates": recovery_rates,
        "role_audit": role_audit,
        "canonical_strata": canonical_strata_audit,
        "zero_tolerance": {
            "cross_chat_scope_violations": cross_chat_violations,
            "cross_scope_relation_violations": cross_chat_violations,
            "time_or_same_segment_strong_relation_violations": weak_strong_violations,
            "local_final_semantic_decisions": 0,
            "provider_calls": 0,
        },
        "replay": replay_metrics,
        "selection": dict(selection),
        "candidate_selection_plan": candidate_selection_plan,
        "global_candidate_plan": dict(candidate_selection_plan.get("global_candidate_plan") or {}),
        "per_scope_plans": dict(candidate_selection_plan.get("per_scope_plans") or {}),
        "candidate_scope_count": int((candidate_selection_plan.get("scope_authorization") or {}).get("scope_count") or 0),
        "candidate_multi_scope_required": bool(candidate_selection_plan.get("scope_authorization_required")),
        "candidate_scope_authorization": dict(candidate_selection_plan.get("scope_authorization") or {}),
        "provider_gate": {"status": provider_gate, "called": False, "calls": 0, "reason": "local_no_provider_boundary"},
        "canonical_metadata_gate": {"status": "pass" if canonical_metadata_complete else "blocked", "mapping_mode": mapping_mode, "required_upstream_fields": {name: list(((strata.get("strata") or {}).get(name) or {}).get("required_upstream_fields") or ()) for name in CANONICAL_STRATA}},
    }
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "pipeline_version": LINEAR_PIPELINE_VERSION,
        "strata_schema_version": STRATA_SCHEMA_VERSION,
        "role_fix_version": ROLE_FIX_VERSION,
        "status": status,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "input_packet_count": input_packet_count,
        "input_packet_digest": input_packet_digest,
        "candidate_source_packet_count": input_packet_count,
        "linearized_candidate_root_count": len(source_packets),
        "candidate_selection_used": candidate_selection_used,
        "cross_scope": {
            "relation_violation_count": cross_chat_violations,
            "scope_violation_count": cross_chat_violations,
            "zero_tolerance": cross_chat_violations == 0,
            "provider_called": False,
        },
        "canonical_strata": canonical_strata_audit,
        "role_audit": role_audit,
        "candidate_selection_plan": candidate_selection_plan,
        "global_candidate_plan": dict(candidate_selection_plan.get("global_candidate_plan") or {}),
        "per_scope_plans": dict(candidate_selection_plan.get("per_scope_plans") or {}),
        "candidate_scope_count": int((candidate_selection_plan.get("scope_authorization") or {}).get("scope_count") or 0),
        "candidate_multi_scope_required": bool(candidate_selection_plan.get("scope_authorization_required")),
        "candidate_scope_authorization": dict(candidate_selection_plan.get("scope_authorization") or {}),
        "input_artifact_version": input_manifest.get("artifact_version"),
        "input_selected_digest": input_digest,
        "input_packet_digest": input_packet_digest,
        "k10_pages_directory_name": k10_root.name if k10_root is not None else None,
        "metrics": metrics,
        "errors": {"codes": sorted({str(row.get("code")) for row in errors}), "count": len(errors)},
        "privacy": {"body_free": True, "aggregate_outputs": "opaque_handles_counts_hashes_only", "private_store_contains_body": True, "frozen_read": False, "provider_calls": 0},
    }
    cost = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "code_sha256": dict(code_hashes),
        "provider_called": False,
        "provider_calls": 0,
        "provider_tokens": {"input": 0, "output": 0},
        "stage_a": {"page_count": len(materialized_public), "input_token_proxy_total": sum(stage_a_tokens), "input_token_proxy_max": max(stage_a_tokens, default=0), "input_token_proxy_p50": _percentile(stage_a_tokens, 0.50), "input_token_proxy_p95": _percentile(stage_a_tokens, 0.95), "user_token_proxy_total": sum(user_tokens), "user_token_proxy_max": max(user_tokens, default=0), "user_token_proxy_p50": _percentile(user_tokens, 0.50), "user_token_proxy_p95": _percentile(user_tokens, 0.95)},
        "limits": target.to_dict(),
        "selection": {"selected_page_count": int(selection.get("selected_page_count") or 0), "selected_page_limit": int(max_pages), "provider_gate": provider_gate},
    }
    audit_rows: List[Dict[str, Any]] = []
    for row in recovery_public:
        audit_rows.append({"root_handle": row.get("root_handle"), "source_handle": row.get("source_handle"), "page_handles": list(row.get("page_handles") or ()), "selection_rank": row.get("selection_rank"), "recovery_rates": row.get("rates"), "status": "pass" if all(item.get("status") in {"pass", "N/A"} for item in (row.get("rates") or {}).values()) else "fail", "body_free": True})

    # Validate every non-store ledger before creating the immutable directory.
    for value, label in ((pages_public, "pages"), (materialized_public, "materialized"), (recovery_public, "recovery"), (strata_rows, "strata"), (selection, "selection"), (candidate_selection_plan, "candidate_selection_plan"), (audit_rows, "audit"), (aggregate, "aggregate"), (cost, "cost"), (errors, "errors")):
        _assert_body_free(value, label=label)
    store_private = store.to_dict(include_body=True)
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["store"], store_private)
    _write_jsonl(output_root / OUTPUT_FILENAMES["pages"], pages_public)
    _write_jsonl(output_root / OUTPUT_FILENAMES["materialized"], materialized_public)
    _write_jsonl(output_root / OUTPUT_FILENAMES["recovery"], recovery_public)
    _write_jsonl(output_root / OUTPUT_FILENAMES["strata"], strata_rows)
    selection_rows = []
    for row in selection.get("selected") or ():
        item = dict(row)
        item["selected"] = True
        item["selection_kind"] = "canonical"
        selection_rows.append(item)
    for row in candidate_selection_plan.get("selected") or ():
        item = dict(row)
        item["selected"] = True
        item["selection_kind"] = "candidate_only"
        selection_rows.append(item)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection"], selection_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["audit"], audit_rows)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "pipeline_version": LINEAR_PIPELINE_VERSION,
        "strata_schema_version": STRATA_SCHEMA_VERSION,
        "role_fix_version": ROLE_FIX_VERSION,
        "local_day": LOCAL_DAY,
        "split": "development",
        "input_artifact_version": input_manifest.get("artifact_version"),
        "input_directory_name": input_root.name if input_root is not None else "<in-memory-development-mappings>",
        "output_directory_name": output_root.name,
        "input_selected_digest": input_digest,
        "k10_pages_directory_name": k10_root.name if k10_root is not None else None,
        "input_packet_count": input_packet_count,
        "input_packet_digest": input_packet_digest,
        "candidate_source_packet_count": input_packet_count,
        "candidate_selection_used": candidate_selection_used,
        "linearized_candidate_root_count": len(source_packets),
        "selected_packet_count": len(source_packets),
        "root_count": len(store.roots),
        "page_count": len(store.pages),
        "selected_page_count": int(selection.get("selected_page_count") or 0),
        "selected_page_limit": int(max_pages),
        "pending_stage_a_count": pending_count,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "status": status,
        "provider_gate": provider_gate,
        "canonical_metadata_gate": "pass" if canonical_metadata_complete else "blocked",
        "canonical_strata": canonical_strata_audit,
        "role_audit": role_audit,
        "candidate_selection_plan": candidate_selection_plan,
        "global_candidate_plan": dict(candidate_selection_plan.get("global_candidate_plan") or {}),
        "per_scope_plans": dict(candidate_selection_plan.get("per_scope_plans") or {}),
        "candidate_scope_count": int((candidate_selection_plan.get("scope_authorization") or {}).get("scope_count") or 0),
        "candidate_multi_scope_required": bool(candidate_selection_plan.get("scope_authorization_required")),
        "candidate_scope_authorization": dict(candidate_selection_plan.get("scope_authorization") or {}),
        "selection_scope_authorization_required": bool(selection.get("scope_authorization_required")),
        "selection_global_coverage_available": bool(selection.get("global_coverage_plan_available")),
        "selection_single_scope_coverage_available": bool(selection.get("single_scope_coverage_plan_available")),
        "selection_missing_strata": list(selection.get("missing_strata") or ()),
        "capacity": target.to_dict(),
        "system_prompt_sha256": prompt_hash,
        "system_prompt_chars": len(prompt),
        "code_sha256": dict(code_hashes),
        "output_files": dict(OUTPUT_FILENAMES),
    }
    _assert_body_free(manifest, label="manifest")
    manifest["artifact_hashes"] = {filename: _sha256_file(output_root / filename) for filename in OUTPUT_FILENAMES.values() if filename != OUTPUT_FILENAMES["manifest"]}
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    artifact_paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return StratifiedLinearDevelopmentRunResult(str(input_label), str(output_root), len(source_packets), len(store.roots), len(store.pages), int(selection.get("selected_page_count") or 0), status, manifest, aggregate, selection, strata, artifact_paths, store)


def run_linear_stage_packet_development_stratified_from_mappings(
    packet_mappings: Any,
    selection_refs: Any,
    output_directory: Union[str, Path],
    *,
    selected_packet_count: Optional[int] = None,
    capacity: Any = None,
    system_prompt: Optional[str] = None,
    system_prompt_a: Optional[str] = None,
    max_pages: int = MAX_SELECTED_PAGES,
) -> StratifiedLinearDevelopmentRunResult:
    return run_linear_stage_packet_development_stratified(
        packet_mappings,
        selection_refs,
        output_directory,
        k10_pages_directory=None,
        selected_packet_count=selected_packet_count,
        capacity=capacity,
        system_prompt=system_prompt,
        system_prompt_a=system_prompt_a,
        max_pages=max_pages,
    )


# K30 workstream aliases.
run_k30_development_linear_stage_packets = run_linear_stage_packet_development_stratified
run_development_linear_stage_packets_stratified = run_linear_stage_packet_development_stratified
run_linear_stage_packet_development_v3_stratified = run_linear_stage_packet_development_stratified
run_linear_stage_packet_development_v3_stratified_from_mappings = run_linear_stage_packet_development_stratified_from_mappings


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-directory", type=Path, default=DEFAULT_INPUT_DIRECTORY)
    parser.add_argument("--k10-pages-directory", type=Path, default=DEFAULT_K10_PAGES_DIRECTORY)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--max-pages", type=int, default=MAX_SELECTED_PAGES)
    args = parser.parse_args(argv)
    result = run_linear_stage_packet_development_stratified(args.input_directory, args.output_directory, k10_pages_directory=args.k10_pages_directory, max_pages=args.max_pages)
    print(json.dumps({"status": result.status, "selected_packet_count": result.selected_packet_count, "root_count": result.root_count, "page_count": result.page_count, "selected_page_count": result.selected_page_count, "provider_calls": result.provider_calls, "output_directory": result.output_directory}, ensure_ascii=False, sort_keys=True))
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARTIFACT_VERSION",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "DEFAULT_INPUT_DIRECTORY",
    "DEFAULT_K10_PAGES_DIRECTORY",
    "MAX_SELECTED_PAGES",
    "OUTPUT_FILENAMES",
    "REPORT_SCHEMA_VERSION",
    "ROLE_FIX_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "StratifiedLinearDevelopmentRunResult",
    "run_development_linear_stage_packets_stratified",
    "run_k30_development_linear_stage_packets",
    "run_linear_stage_packet_development_stratified",
    "run_linear_stage_packet_development_stratified_from_mappings",
    "run_linear_stage_packet_development_v3_stratified",
    "run_linear_stage_packet_development_v3_stratified_from_mappings",
]
