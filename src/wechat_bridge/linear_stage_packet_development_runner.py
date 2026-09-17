"""K9 development runner for the K8 linear stage-packet store.

The runner is intentionally a small, local artifact harness.  Its only input
is the K5 ``context_packet_development_v1`` manifest, selected mapping, and
the matching private packet rows.  It does not traverse a frozen split, read
the K7 compact artifact, invoke a provider, or write production state.

The linear store owns bodies in a private content table and exposes roots and
pages as handles.  Stage A is materialized locally for every page so the
request budget is measurable.  There is no real Stage A/B model output in K9;
Stage B and C are therefore recorded as ``N/A`` rather than fabricated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .linear_stage_packets import (
    DEFAULT_MAX_CANDIDATE_ROWS,
    DEFAULT_MAX_EVIDENCE_REFS,
    DEFAULT_MAX_INPUT_TOKEN_PROXY,
    DEFAULT_MAX_MESSAGES,
    DEFAULT_MAX_USER_TOKEN_PROXY,
    DEFAULT_SYSTEM_PROMPTS,
    LINEAR_PIPELINE_VERSION,
    LinearCapacity,
    LinearStagePacketStore,
    build_linear_stage_packets,
    canonical_json,
    materialize_stage_a,
    recover_linear_packet,
    stable_hash,
)


RUNNER_SCHEMA_VERSION = "linear_stage_packet_development_runner_v1"
RUNNER_SCHEMA_VERSION_V2 = "linear_stage_packet_development_runner_v2"
ARTIFACT_VERSION = "linear_stage_packet_development_v1"
ARTIFACT_VERSION_V2 = "linear_stage_packet_development_v2"
INPUT_ARTIFACT_VERSION = "context_packet_development_v1"
LOCAL_DAY = "2026-08-25"
SELECTION_LIMIT = 20


# The names are deliberately explicit: these are not K5/K7 files and the
# private files that may contain bodies are kept separate from the ledgers.
OUTPUT_FILENAMES: Dict[str, str] = {
    "store": "store.private.json",
    "pages": "pages.private.jsonl",
    "materialized": "materialized_map.private.jsonl",
    "recovery": "recovery_map.private.jsonl",
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "errors": "errors.private.jsonl",
    "selection": "selection_map.private.jsonl",
}


_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "messagebody",
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
_WEAK_REASON_KEYS = frozenset(
    {
        "time_proximity_weak",
        "same_segment_weak",
        "same_segment_only",
        "time_proximity",
        "time_only",
        "time_proximity_only",
        "same_segment",
        "temporal_proximity",
        "temporal_only",
        "same_dialogue_segment",
    }
)
_STRONG_RELATION_LABELS = frozenset({"same_event", "resolved", "strong", "materialized"})
_FROZEN_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=str)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[Any]) -> None:
    path.write_text(
        "".join(
            json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _output_metadata(output_version: Any) -> Tuple[str, str, str]:
    """Resolve immutable v1/v2 labels without changing the v1 default."""

    value = str(output_version or "v1").strip().casefold()
    if value in {"v1", "1", ARTIFACT_VERSION.casefold(), RUNNER_SCHEMA_VERSION.casefold()}:
        return "v1", ARTIFACT_VERSION, RUNNER_SCHEMA_VERSION
    if value in {"v2", "2", ARTIFACT_VERSION_V2.casefold(), RUNNER_SCHEMA_VERSION_V2.casefold()}:
        return "v2", ARTIFACT_VERSION_V2, RUNNER_SCHEMA_VERSION_V2
    raise ValueError("output_version must be v1 or v2")


def _code_hashes() -> Dict[str, str]:
    """Bind the artifact to the exact runner and linear core source files."""

    runner_path = Path(__file__).resolve()
    core_path = runner_path.with_name("linear_stage_packets.py")
    return {
        "runner": _sha256_file(runner_path),
        "linear_stage_packets": _sha256_file(core_path),
    }


def _assert_body_free(value: Any, *, label: str = "value") -> None:
    """Fail closed for the body-free K9 ledgers.

    A field ending in ``_hash`` or ``_ref`` is metadata.  Only non-empty body
    fields are rejected; empty K5 placeholders are safe to retain.
    """

    hits: List[str] = []

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key.casefold() in _BODY_KEYS and child not in (None, "", (), [], {}):
                    hits.append(path + key)
                visit(child, path + key + ".")
        elif isinstance(item, (list, tuple, set, frozenset)):
            for index, child in enumerate(item):
                visit(child, path + str(index) + ".")

    visit(value)
    if hits:
        raise ValueError("%s contains body-bearing fields: %s" % (label, ", ".join(hits[:5])))


def _guard_input(root: Union[str, Path]) -> Path:
    path = Path(root)
    if not path.is_dir():
        raise ValueError("K9 K5 development input directory is missing")
    if any(part.casefold() in _FROZEN_PARTS for part in path.resolve().parts):
        raise ValueError("K9 runner refuses frozen input")
    return path.resolve()


def _guard_output(root: Union[str, Path], input_root: Path) -> Path:
    path = Path(root)
    resolved = path.resolve()
    if any(part.casefold() in _FROZEN_PARTS for part in resolved.parts):
        raise ValueError("K9 runner refuses frozen output")
    if resolved == input_root:
        raise ValueError("K9 output directory must differ from K5 input")
    if resolved.exists():
        raise FileExistsError("K9 output artifact is immutable; choose a new directory")
    return resolved


def _as_mapping(value: Any, *, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be a JSON object" % label)
    return {str(key): value[key] for key in value}


def _first(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        child = value.get(name)
        if child not in (None, ""):
            return child
    return None


def _rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, Mapping):
        if all(isinstance(child, Mapping) for child in value.values()):
            return [dict(child) for child in value.values()]
        return []
    if isinstance(value, (list, tuple)):
        return [dict(child) for child in value if isinstance(child, Mapping)]
    return []


def _row_id(row: Mapping[str, Any], names: Sequence[str]) -> Optional[str]:
    value = _first(row, *names)
    return str(value) if value is not None else None


def _unique(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _ids(rows: Any, names: Sequence[str]) -> List[str]:
    output: List[str] = []
    for row in _rows(rows):
        value = _row_id(row, names)
        if value is not None:
            output.append(value)
    return _unique(output)


def _ref_ids(value: Any, names: Sequence[str]) -> List[str]:
    """Read mapping or scalar refs without interpreting opaque handles."""

    if isinstance(value, Mapping):
        values: Sequence[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = tuple(value)
    elif value not in (None, ""):
        values = (value,)
    else:
        values = ()
    output: List[str] = []
    for item in values:
        if isinstance(item, Mapping):
            found = _row_id(item, names)
            if found is not None:
                output.append(found)
        elif item not in (None, ""):
            output.append(str(item))
    return _unique(output)


_CANDIDATE_LAYER_NAMES = frozenset(
    {
        "candidate_qa_links",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "continuity_candidates",
        "open_thread_candidates",
    }
)


def _expected_evidence_ids(packet: Mapping[str, Any]) -> List[str]:
    """Union direct and candidate-nested evidence identities from K5."""

    names = ("evidence_id", "evidence_ref_id", "evidence_handle", "id", "ref_id", "message_id")
    output: List[str] = []
    for key in ("evidence_refs", "evidence", "evidence_references"):
        output.extend(_ref_ids(packet.get(key), names))
    dynamic = packet.get("dynamic_part") if isinstance(packet.get("dynamic_part"), Mapping) else {}
    for key in ("evidence_refs", "evidence", "evidence_references"):
        output.extend(_ref_ids(dynamic.get(key), names))
    for name, row in _iter_packet_layer_rows(packet):
        if name not in _CANDIDATE_LAYER_NAMES:
            continue
        for key in ("evidence_refs", "evidence", "evidence_references"):
            output.extend(_ref_ids(row.get(key), names))
    return _unique(output)


def _read_selected(
    root: Path,
    selected_packet_count: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, Dict[str, Any]]:
    """Read only the K5 manifest, selected map, and selected packet rows."""

    manifest_path = root / "manifest.private.json"
    selection_path = root / "selection_map.private.jsonl"
    packet_path = root / "packets.private.jsonl"
    if not all(path.is_file() for path in (manifest_path, selection_path, packet_path)):
        raise ValueError(
            "K9 input requires manifest.private.json, selection_map.private.jsonl and packets.private.jsonl"
        )

    manifest = _as_mapping(json.loads(manifest_path.read_text(encoding="utf-8")), label="K5 manifest")
    if manifest.get("artifact_version") != INPUT_ARTIFACT_VERSION:
        raise ValueError("K9 input is not the K5 context_packet_development_v1 artifact")
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, LOCAL_DAY):
        raise ValueError("K9 input is not the 2026-08-25 development split")
    if manifest.get("frozen_read") is True or manifest.get("provider_called") is True:
        raise ValueError("K9 input manifest violates the local development boundary")
    if int(manifest.get("provider_calls") or 0) != 0 or manifest.get("gold_loaded") is True:
        raise ValueError("K9 input manifest shows provider or gold/frozen state")

    selection_rows: List[Dict[str, Any]] = []
    for line in selection_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = _as_mapping(json.loads(line), label="K5 selection row")
        selection_rows.append(row)
    selected = [row for row in selection_rows if bool(row.get("selected"))]
    selected.sort(key=lambda row: (int(row.get("selection_rank") or 10**9), str(row.get("packet_id") or "")))
    expected_count = int(selected_packet_count) if selected_packet_count is not None else len(selected)
    if expected_count < 1:
        raise ValueError("selected_packet_count must be positive")
    if len(selected) != expected_count:
        raise ValueError("K9 requires exactly %d selected packet refs" % expected_count)
    packet_ids = [str(row.get("packet_id") or "") for row in selected]
    if any(not value for value in packet_ids) or len(packet_ids) != len(set(packet_ids)):
        raise ValueError("K9 selected packet refs must have unique packet_id values")
    ranks = [int(row.get("selection_rank") or 0) for row in selected]
    if len(ranks) != len(set(ranks)) or any(rank < 1 for rank in ranks):
        raise ValueError("K9 selected packet refs must have unique positive selection ranks")

    # Stream the large packet file.  Only rows whose packet_id is selected are
    # parsed into the runner's working set; non-selected K5 rows are ignored.
    wanted = set(packet_ids)
    packets: Dict[str, Dict[str, Any]] = {}
    with packet_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping):
                continue
            packet_id = str(row.get("packet_id") or "")
            if packet_id not in wanted:
                continue
            if packet_id in packets:
                raise ValueError("duplicate selected packet %s" % packet_id)
            packets[packet_id] = dict(row)
    missing = sorted(wanted - set(packets))
    if missing:
        raise ValueError("selected packet rows missing: %s" % ",".join(missing[:3]))
    ordered = [packets[packet_id] for packet_id in packet_ids]
    selected_digest = _sha256_bytes(
        ("".join(canonical_json(row) + "\n" for row in ordered)).encode("utf-8")
    )
    return selected, ordered, selected_digest, manifest


def _prepare_mapping_input(
    packets: Any,
    selection_refs: Any,
    selected_packet_count: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, Dict[str, Any]]:
    """Prepare an in-memory K5 mapping input for synthetic/local callers.

    K9's file runner intentionally reads only the three K5 files.  Tests and
    small local harnesses can use this equivalent boundary directly: a packet
    mapping/list plus the selected mapping refs.  No artifact directory is
    consulted in this mode.
    """

    if isinstance(packets, Mapping):
        if isinstance(packets.get("packets"), (list, tuple, Mapping)):
            packets = packets.get("packets")
        elif packets.get("packet_id") or packets.get("context_packet_id"):
            packets = [packets]
        else:
            # A named packet table ({packet_id: packet}) is convenient for
            # callers that already indexed the K5 private rows.
            packets = list(packets.values())
    if not isinstance(packets, (list, tuple)):
        raise ValueError("K9 mappings input must be a packet mapping/list")
    packet_rows = [dict(row) for row in packets if isinstance(row, Mapping)]
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in packet_rows:
        packet_id = str(row.get("packet_id") or row.get("context_packet_id") or "")
        if not packet_id:
            raise ValueError("K9 packet mapping is missing packet_id")
        if packet_id in by_id:
            raise ValueError("duplicate K9 packet mapping %s" % packet_id)
        by_id[packet_id] = row

    if isinstance(selection_refs, Mapping):
        if isinstance(selection_refs.get("selected"), (list, tuple)):
            selection_refs = selection_refs["selected"]
        else:
            selection_refs = list(selection_refs.values())
    if not isinstance(selection_refs, (list, tuple)):
        raise ValueError("K9 selection_refs must be a mapping/list")
    selected = [dict(row) for row in selection_refs if isinstance(row, Mapping) and row.get("selected", True)]
    selected.sort(key=lambda row: (int(row.get("selection_rank") or 10**9), str(row.get("packet_id") or "")))
    expected_count = int(selected_packet_count) if selected_packet_count is not None else len(selected)
    if expected_count < 1 or len(selected) != expected_count:
        raise ValueError("K9 requires exactly %d selected packet refs" % expected_count)
    packet_ids = [str(row.get("packet_id") or row.get("context_packet_id") or "") for row in selected]
    if any(not packet_id for packet_id in packet_ids) or len(packet_ids) != len(set(packet_ids)):
        raise ValueError("K9 selected mapping refs must have unique packet_id values")
    missing = sorted(set(packet_ids) - set(by_id))
    if missing:
        raise ValueError("selected K9 packet mappings missing: %s" % ",".join(missing[:3]))
    ordered = [by_id[packet_id] for packet_id in packet_ids]
    digest = _sha256_bytes(("".join(canonical_json(row) + "\n" for row in ordered)).encode("utf-8"))
    manifest = {
        "artifact_version": INPUT_ARTIFACT_VERSION,
        "split": "development",
        "local_day": LOCAL_DAY,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "selection": {"selected_packet_count": expected_count, "selected_packet_limit": expected_count},
    }
    return selected, ordered, digest, manifest


def _capacity(value: Any) -> LinearCapacity:
    if value is None:
        return LinearCapacity(
            max_input_token_proxy=DEFAULT_MAX_INPUT_TOKEN_PROXY,
            max_user_token_proxy=DEFAULT_MAX_USER_TOKEN_PROXY,
            max_messages=DEFAULT_MAX_MESSAGES,
            max_candidate_rows=DEFAULT_MAX_CANDIDATE_ROWS,
            max_evidence_refs=DEFAULT_MAX_EVIDENCE_REFS,
        )
    if isinstance(value, LinearCapacity):
        return value
    if isinstance(value, Mapping):
        return LinearCapacity(
            max_input_token_proxy=int(value.get("max_input_token_proxy", DEFAULT_MAX_INPUT_TOKEN_PROXY)),
            max_user_token_proxy=int(value.get("max_user_token_proxy", DEFAULT_MAX_USER_TOKEN_PROXY)),
            max_messages=int(value.get("max_messages", DEFAULT_MAX_MESSAGES)),
            max_candidate_rows=int(value.get("max_candidate_rows", DEFAULT_MAX_CANDIDATE_ROWS)),
            max_evidence_refs=int(value.get("max_evidence_refs", DEFAULT_MAX_EVIDENCE_REFS)),
        )
    raise TypeError("capacity must be LinearCapacity or mapping")


def _scope_pair(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(value, Mapping):
        return None, None
    scope = value.get("scope") if isinstance(value.get("scope"), Mapping) else {}
    account = _first(value, "account_id", "account") or _first(scope, "account_id", "account")
    chat = _first(value, "chat_id", "chat") or _first(scope, "chat_id", "chat")
    return (str(account) if account not in (None, "") else None, str(chat) if chat not in (None, "") else None)


def _iter_packet_layer_rows(packet: Mapping[str, Any]) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    for name in (
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
    ):
        for row in _rows(packet.get(name)):
            yield name, row
    dynamic = packet.get("dynamic_part") if isinstance(packet.get("dynamic_part"), Mapping) else {}
    for name in (
        "candidate_qa_links",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "continuity_candidates",
        "open_thread_candidates",
    ):
        for row in _rows(dynamic.get(name)):
            yield name, row


def _scope_violations(packets: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for packet in packets:
        account, chat = _scope_pair(packet)
        if account is None or chat is None:
            continue
        expected = (account, chat)
        for _, row in _iter_packet_layer_rows(packet):
            row_scope = _scope_pair(row)
            if row_scope[0] not in (None, "unknown", account) or row_scope[1] not in (None, "unknown", chat):
                count += 1
            for account_key in ("left_account_id", "right_account_id", "question_account_id", "answer_account_id", "source_account_id"):
                if row.get(account_key) not in (None, "", "unknown", expected[0]):
                    count += 1
            for chat_key in ("left_chat_id", "right_chat_id", "question_chat_id", "answer_chat_id", "source_chat_id"):
                if row.get(chat_key) not in (None, "", "unknown", expected[1]):
                    count += 1
    return count


def _candidate_reasons(row: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("candidate_reason", "candidate_reasons", "supporting_slot_codes", "reason_codes", "reasons"):
        child = row.get(key)
        if isinstance(child, str):
            values.append(child.casefold())
        elif isinstance(child, (list, tuple, set, frozenset)):
            values.extend(str(item).casefold() for item in child)
    return _unique(values)


def _weak_strong_violations(packets: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for packet in packets:
        for name, row in _iter_packet_layer_rows(packet):
            if not name.endswith("history") and name not in {"candidate_qa_links", "continuity_candidates", "open_thread_candidates"}:
                continue
            reasons = set(_candidate_reasons(row))
            if not reasons or not reasons <= _WEAK_REASON_KEYS:
                continue
            strong = any(bool(row.get(key)) for key in ("strong_relation", "is_strong", "materialized_relation"))
            label = str(_first(row, "relation_label", "relation", "status") or "").casefold()
            if strong or label in _STRONG_RELATION_LABELS:
                count += 1
    return count


def _remove_transport_fields(value: Mapping[str, Any]) -> Dict[str, Any]:
    # ``material_stats`` intentionally describes this exact user payload in
    # the linear core.  Keep this list synchronized with _finish in that
    # module so the runner's independent accounting is reproducible.
    transport = {
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
    return {str(key): value for key, value in value.items() if str(key) not in transport}


def _materialized_row(
    store: LinearStagePacketStore,
    page: Mapping[str, Any],
    stage_a: Mapping[str, Any],
    system_prompt: str,
) -> Dict[str, Any]:
    stats = dict(stage_a.get("material_stats") or {})
    user = _remove_transport_fields(stage_a)
    user_canonical = canonical_json(user)
    user_chars = len(user_canonical)
    user_proxy = (user_chars + 3) // 4
    total_proxy = (len(system_prompt) + user_chars + 3) // 4
    row: Dict[str, Any] = {
        "stage": "A",
        "root_id": str(page.get("root_id") or ""),
        "source_packet_id": str(store.root_table.get(str(page.get("root_id")), {}).get("source_packet_id") or ""),
        "page_id": str(page.get("page_id") or ""),
        "page_ordinal": int(page.get("ordinal") or 0),
        "status": str(stage_a.get("status") or "unknown"),
        "system_prompt": system_prompt,
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "system_prompt_chars": len(system_prompt),
        "user_canonical_json": user_canonical,
        "user_canonical_sha256": hashlib.sha256(user_canonical.encode("utf-8")).hexdigest(),
        "canonical_chars": user_chars,
        "canonical_bytes": len(user_canonical.encode("utf-8")),
        "input_token_proxy": total_proxy,
        "user_token_proxy": user_proxy,
        "message_count": int(stats.get("message_count", len(page.get("message_handles", ()))) or 0),
        "candidate_count": int(stats.get("candidate_count", len(page.get("candidate_handles", ()))) or 0),
        "candidate_row_count": int(stats.get("candidate_row_count", stats.get("candidate_count", 0)) or 0),
        "evidence_count": int(stats.get("evidence_count", 0) or 0),
        "evidence_ref_count": int(stats.get("evidence_ref_count", stats.get("evidence_count", 0)) or 0),
        "page_counts": {
            "messages": len(page.get("message_handles", ())),
            "candidates": len(page.get("candidate_handles", ())),
            "evidence": len(page.get("evidence_handles", ())),
        },
        "within_limits": bool(
            total_proxy <= store.capacity.max_input_token_proxy
            and user_proxy <= store.capacity.max_user_token_proxy
            and int(stats.get("message_count", 0) or 0) <= store.capacity.max_messages
            and int(stats.get("candidate_count", 0) or 0) <= store.capacity.max_candidate_rows
            and int(stats.get("evidence_count", 0) or 0) <= store.capacity.max_evidence_refs
        ),
        "material_stats": stats,
        # Stage B/C are intentionally not materialized in K9.  The values are
        # a status marker, not provider-shaped fake responses.
        "stage_b_status": "N/A",
        "stage_c_status": "N/A",
        "stage_a": dict(stage_a),
        "user_packet": user,
    }
    if "open_snapshot_ref" in stage_a:
        row["open_snapshot_ref"] = stage_a["open_snapshot_ref"]
    if "open_snapshot" in stage_a:
        row["open_snapshot"] = stage_a["open_snapshot"]
    return row


def _classify_context(row: Mapping[str, Any]) -> Optional[str]:
    values = " ".join(
        str(row.get(key) or "").casefold()
        for key in ("role", "fragment_type", "dialogue_role", "kind", "type")
    )
    if any(token in values for token in ("greeting", "opener", "conversation_open", "welcome")):
        return "greeting"
    if any(token in values for token in ("acknowledg", "ack", "received", "receipt")):
        return "ack"
    return None


def _rate(expected: Sequence[str], observed: Sequence[str]) -> Dict[str, Any]:
    expected_set, observed_set = set(expected), set(observed)
    recovered = len(expected_set & observed_set)
    return {
        "expected": len(expected_set),
        "recovered": recovered,
        "missing": sorted(expected_set - observed_set),
        "rate": recovered / len(expected_set) if expected_set else 1.0,
        "status": "pass" if expected_set <= observed_set else ("N/A" if not expected_set else "fail"),
    }


def _percentile(values: Sequence[int], fraction: float) -> int:
    """Small deterministic percentile helper for the private aggregate."""

    if not values:
        return 0
    ordered = sorted(int(value) for value in values)
    position = (len(ordered) - 1) * float(fraction)
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return int(round(ordered[left] + (ordered[right] - ordered[left]) * (position - left)))


def _recovery_row(
    store: LinearStagePacketStore,
    source: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    source_id = str(source.get("packet_id") or source.get("context_packet_id") or "")
    root = store.get_root(source_id)
    recovered = recover_linear_packet(store, root["root_id"], include_body=True)
    expected: Dict[str, List[str]] = {
        "source": _ids(source.get("source_refs"), ("source_ref_id", "id", "message_id")),
        "primary": _ids(source.get("primary_fragments"), ("message_id", "fragment_id", "id")),
        "adjacent": _ids(source.get("adjacent_context"), ("message_id", "fragment_id", "id")),
        # K5 can carry evidence directly on the root or nested inside a
        # candidate relation.  Both forms are authoritative expected refs.
        "evidence": _expected_evidence_ids(source),
    }
    recovered_rows: Dict[str, Any] = {
        "source": recovered.get("source_refs") or (),
        "primary": recovered.get("primary_fragments") or (),
        "adjacent": recovered.get("adjacent_context") or (),
        "evidence": recovered.get("evidence_refs") or (),
    }
    observed = {
        key: _ids(recovered_rows[key], ("source_ref_id", "evidence_id", "id", "message_id", "fragment_id", "ref_id"))
        for key in recovered_rows
    }
    expected["greeting"] = [
        str(_first(row, "message_id", "fragment_id", "id"))
        for name in ("primary_fragments", "adjacent_context")
        for row in _rows(source.get(name))
        if _classify_context(row) == "greeting"
    ]
    expected["ack"] = [
        str(_first(row, "message_id", "fragment_id", "id"))
        for name in ("primary_fragments", "adjacent_context")
        for row in _rows(source.get(name))
        if _classify_context(row) == "ack"
    ]
    recovered_context = list(recovered_rows["primary"]) + list(recovered_rows["adjacent"])
    observed["greeting"] = [
        str(_first(row, "message_id", "fragment_id", "id"))
        for row in recovered_context
        if _classify_context(row) == "greeting"
    ]
    observed["ack"] = [
        str(_first(row, "message_id", "fragment_id", "id"))
        for row in recovered_context
        if _classify_context(row) == "ack"
    ]
    rates = {key: _rate(expected[key], observed[key]) for key in ("source", "primary", "adjacent", "greeting", "ack", "evidence")}
    row: Dict[str, Any] = {
        "selection_rank": selection.get("selection_rank"),
        "source_packet_id": source_id,
        "root_id": root["root_id"],
        "page_refs": list(root.get("page_refs", ())),
        "rates": rates,
        "expected_ids": expected,
        "observed_ids": observed,
        "recovered_packet": recovered,
    }
    return row, rates


def _page_formula(root: Mapping[str, Any], capacity: LinearCapacity) -> Dict[str, Any]:
    counts = {
        "messages": len(root.get("message_handles", ())),
        "candidates": len(root.get("candidate_handles", ())),
        "evidence": len(root.get("evidence_handles", ())),
    }
    terms = {
        "messages": max(1, math.ceil(counts["messages"] / capacity.max_messages)),
        "candidates": max(1, math.ceil(counts["candidates"] / capacity.max_candidate_rows)),
        "evidence": max(1, math.ceil(counts["evidence"] / capacity.max_evidence_refs)),
    }
    expected = max(terms.values())
    cartesian = terms["messages"] * terms["candidates"] * terms["evidence"]
    actual = len(root.get("page_refs", ()))
    return {
        "counts": counts,
        "ceil_terms": terms,
        "expected_page_count": expected,
        "actual_page_count": actual,
        "cartesian_page_count": cartesian,
        "linear": actual == expected and actual <= cartesian,
    }


@dataclass(frozen=True)
class LinearDevelopmentRunResult:
    input_directory: str
    output_directory: str
    selected_packet_count: int
    root_count: int
    page_count: int
    pending_count: int
    status: str
    manifest: Mapping[str, Any]
    aggregate: Mapping[str, Any]
    artifact_paths: Mapping[str, str]
    store: LinearStagePacketStore
    materialized_results: Tuple[Mapping[str, Any], ...]
    recovery_results: Tuple[Mapping[str, Any], ...]

    @property
    def linear_store(self) -> LinearStagePacketStore:
        return self.store

    @property
    def pages(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(self.store.pages)

    @property
    def materialized(self) -> Tuple[Mapping[str, Any], ...]:
        return self.materialized_results

    @property
    def recovery(self) -> Tuple[Mapping[str, Any], ...]:
        return self.recovery_results

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "selected_packet_count": self.selected_packet_count,
            "root_count": self.root_count,
            "page_count": self.page_count,
            "pending_count": self.pending_count,
            "status": self.status,
            "manifest": dict(self.manifest),
            "aggregate": dict(self.aggregate),
            "artifact_paths": dict(self.artifact_paths),
        }


def run_linear_stage_packet_development(
    input_directory: Union[str, Path, Mapping[str, Any], Sequence[Mapping[str, Any]]],
    output_directory: Union[str, Path, Sequence[Mapping[str, Any]]],
    output_directory_override: Optional[Union[str, Path]] = None,
    *,
    selection_refs: Any = None,
    selected_packet_count: Optional[int] = SELECTION_LIMIT,
    capacity: Any = None,
    system_prompt: Optional[str] = None,
    system_prompt_a: Optional[str] = None,
    output_version: str = "v1",
) -> LinearDevelopmentRunResult:
    """Run local linear Stage A materialization over selected K5 rows.

    ``output_version`` is an artifact-label switch only.  The default remains
    the immutable K9/v1 contract; v2 additionally requires every Stage A page
    to complete and binds the ledgers to the current runner/core source hash.
    """

    resolved_output_version, artifact_version, runner_schema_version = _output_metadata(output_version)
    code_hashes = _code_hashes()

    # Two equivalent local boundaries are supported:
    #   path, output_path                         (real K5 artifact runner)
    #   packet_mappings, selection_refs, output_path (synthetic/local runner)
    mapping_mode = not isinstance(input_directory, (str, Path))
    if mapping_mode:
        if output_directory_override is None:
            raise ValueError("K9 mapping input requires output_directory as the third positional argument")
        selected_rows, source_packets, input_hash, input_manifest = _prepare_mapping_input(
            input_directory,
            selection_refs if selection_refs is not None else output_directory,
            selected_packet_count,
        )
        input_root = None
        input_directory_label = "<in-memory-k5-mappings>"
        output_target = output_directory_override
    else:
        input_root = _guard_input(input_directory)
        output_target = output_directory if output_directory_override is None else output_directory_override
        output_root = _guard_output(output_target, input_root)
        selected_rows, source_packets, input_hash, input_manifest = _read_selected(
            input_root, selected_packet_count
        )
        input_directory_label = str(input_root)
    if mapping_mode:
        output_root = _guard_output(output_target, Path("<k9-in-memory-input>"))
    target = _capacity(capacity)
    prompt = str(system_prompt_a if system_prompt_a is not None else system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPTS["A"])

    cross_chat = _scope_violations(source_packets)
    weak_strong = _weak_strong_violations(source_packets)
    # Build exactly the selected K5 rows.  The K8 builder performs the hard
    # scope checks and never needs a provider or another artifact as input.
    store = build_linear_stage_packets(source_packets, capacity=target)

    formula_rows: List[Dict[str, Any]] = []
    for root in store.roots:
        formula = _page_formula(root, target)
        formula_rows.append({"root_id": root["root_id"], "source_packet_id": root.get("source_packet_id"), **formula})

    materialized_rows: List[Dict[str, Any]] = []
    materialized_errors: List[Dict[str, Any]] = []
    stage_statuses: Counter[str] = Counter()
    for page in store.pages:
        try:
            stage_a = materialize_stage_a(store, page["page_id"], system_prompt=prompt)
            row = _materialized_row(store, page, stage_a, prompt)
            status = str(stage_a.get("status") or "unknown")
            stage_statuses[status] += 1
            stats = row["material_stats"]
            if status == "complete":
                if not row["within_limits"] or row["input_token_proxy"] > target.max_input_token_proxy or row["user_token_proxy"] > target.max_user_token_proxy:
                    materialized_errors.append({"code": "complete_stage_a_over_capacity", "page_id": page["page_id"], "status": status, "stats": stats})
            elif status in {"pending", "open"}:
                snapshot_ref = stage_a.get("open_snapshot_ref")
                snapshot_ok = bool(snapshot_ref and str(snapshot_ref) in store.open_snapshot_table)
                row["open_snapshot_ok"] = snapshot_ok
                if not snapshot_ok:
                    materialized_errors.append({"code": "pending_stage_a_missing_open_snapshot", "page_id": page["page_id"], "status": status})
            else:
                materialized_errors.append({"code": "unexpected_stage_a_status", "page_id": page["page_id"], "status": status})
            materialized_rows.append(row)
        except Exception as exc:
            materialized_errors.append({"code": "stage_a_materialization_failed", "page_id": page.get("page_id"), "error_type": type(exc).__name__})

    recovery_rows: List[Dict[str, Any]] = []
    recovery_totals: Dict[str, List[int]] = {key: [0, 0] for key in ("source", "primary", "adjacent", "greeting", "ack", "evidence")}
    recovery_errors: List[Dict[str, Any]] = []
    source_by_id = {str(packet.get("packet_id")): packet for packet in source_packets}
    for selection, source in zip(selected_rows, source_packets):
        try:
            row, rates = _recovery_row(store, source, selection)
        except Exception as exc:
            recovery_errors.append({"code": "recovery_failed", "source_packet_id": source.get("packet_id"), "error_type": type(exc).__name__})
            continue
        recovery_rows.append(row)
        for key, rate in rates.items():
            recovery_totals[key][0] += int(rate["expected"])
            recovery_totals[key][1] += int(rate["recovered"])
    recovery_rates = {
        key: {
            "expected": values[0],
            "recovered": values[1],
            "rate": values[1] / values[0] if values[0] else 1.0,
            "status": "pass" if values[0] == values[1] else ("N/A" if values[0] == 0 else "fail"),
        }
        for key, values in recovery_totals.items()
    }

    roots_by_source = {str(root.get("source_packet_id")): root for root in store.roots}
    selection_out: List[Dict[str, Any]] = []
    for selection in selected_rows:
        source_id = str(selection.get("packet_id") or "")
        root = roots_by_source.get(source_id)
        selection_out.append(
            {
                "selection_rank": selection.get("selection_rank"),
                "source_packet_id": source_id,
                "source_ref": source_id,
                "source_packet_hash": selection.get("packet_hash"),
                "root_id": root.get("root_id") if root else None,
                "page_refs": list(root.get("page_refs", ())) if root else [],
                "page_count": len(root.get("page_refs", ())) if root else 0,
                "complete": bool(root),
            }
        )

    # Replay into the same process-local store.  No rows or cache entries may
    # be appended by an identical replay.
    table_names = ("message_table", "content_table", "candidate_table", "evidence_table", "root_table", "page_table")
    before_counts = {name: len(getattr(store, name)) for name in table_names}
    before_root_hashes = [str(root.get("packet_hash")) for root in store.roots]
    before_page_hashes = [str(page.get("page_hash")) for page in store.pages]
    before_cache = {
        ("content_cache" if name == "content" else name): len(store.cache[name])
        for name in ("fixed", "dynamic", "content")
    }
    build_linear_stage_packets(source_packets, capacity=target, store=store)
    after_counts = {name: len(getattr(store, name)) for name in table_names}
    after_cache = {
        ("content_cache" if name == "content" else name): len(store.cache[name])
        for name in ("fixed", "dynamic", "content")
    }
    replay_idempotent = before_counts == after_counts and before_cache == after_cache and before_root_hashes == [str(root.get("packet_hash")) for root in store.roots] and before_page_hashes == [str(page.get("page_hash")) for page in store.pages]
    replay_hits = {name: int(before_cache[name]) for name in before_cache}

    # Body-bearing private values are intentionally kept only in these
    # private artifacts.  The ledgers below are checked before writing.
    store_private = store.to_dict(include_body=True)
    store_index = store.to_dict(include_body=False)
    pages_out = [
        {
            "page_id": page.get("page_id"),
            "root_id": page.get("root_id"),
            "source_packet_id": roots_by_source.get(str(store.root_table.get(str(page.get("root_id")), {}).get("source_packet_id")), {}).get("source_packet_id"),
            "ordinal": page.get("ordinal"),
            "scope": page.get("scope"),
            "message_handles": list(page.get("message_handles", ())),
            "candidate_handles": list(page.get("candidate_handles", ())),
            "candidate_link_refs": list(page.get("candidate_link_refs", ())),
            "evidence_handles": list(page.get("evidence_handles", ())),
            "page_hash": page.get("page_hash"),
            "status": page.get("status"),
        }
        for page in store.pages
    ]

    pending_count = sum(1 for row in materialized_rows if str(row.get("status")) in {"pending", "open"})
    stage_a_total_tokens = [int(row.get("input_token_proxy") or 0) for row in materialized_rows]
    stage_a_user_tokens = [int(row.get("user_token_proxy") or 0) for row in materialized_rows]
    page_formula_ok = bool(formula_rows) and all(bool(row.get("linear")) for row in formula_rows)
    roots_mapping_complete = len(selection_out) == len(source_packets) and all(bool(row.get("complete")) for row in selection_out)
    recovery_ok = all(values[0] == values[1] for values in recovery_totals.values()) and len(recovery_rows) == len(source_packets)
    pending_snapshots_ok = not any(error.get("code") == "pending_stage_a_missing_open_snapshot" for error in materialized_errors)

    source_bytes = sum(len(canonical_json(packet).encode("utf-8")) for packet in source_packets)
    index_bytes = len(canonical_json(store_index).encode("utf-8"))
    private_store_bytes = len(canonical_json(store_private).encode("utf-8"))
    pages_bytes = len(canonical_json(pages_out).encode("utf-8"))
    materialized_bytes = len(canonical_json(materialized_rows).encode("utf-8"))
    recovery_bytes = len(canonical_json(recovery_rows).encode("utf-8"))
    private_bundle_bytes = private_store_bytes + pages_bytes + materialized_bytes + recovery_bytes
    volume_ratio = private_bundle_bytes / source_bytes if source_bytes else 0.0
    # A private, bodyful recovery map naturally repeats some metadata.  Eight
    # times the selected source is a deliberately generous anti-explosion
    # ceiling while still catching accidental cartesian duplication.
    volume_ok = bool(source_bytes) and volume_ratio <= 8.0

    hard_errors: List[Dict[str, Any]] = []
    hard_errors.extend(materialized_errors)
    hard_errors.extend(recovery_errors)
    if cross_chat:
        hard_errors.append({"code": "cross_chat_scope_violation", "count": cross_chat})
    if weak_strong:
        hard_errors.append({"code": "time_or_same_segment_strong_relation", "count": weak_strong})
    if not roots_mapping_complete:
        hard_errors.append({"code": "selected_root_mapping_incomplete"})
    if not page_formula_ok:
        hard_errors.append({"code": "page_count_not_linear"})
    if not recovery_ok:
        hard_errors.append({"code": "recovery_gate_failed"})
    if not replay_idempotent:
        hard_errors.append({"code": "replay_not_idempotent"})
    if not volume_ok:
        hard_errors.append({"code": "private_volume_explosion", "ratio": round(volume_ratio, 4)})
    if resolved_output_version == "v2" and pending_count:
        hard_errors.append({"code": "stage_a_pending_v2", "count": pending_count})
    if resolved_output_version == "v2" and len(source_packets) != SELECTION_LIMIT:
        hard_errors.append({"code": "v2_requires_twenty_selected_roots", "count": len(source_packets)})

    status = "blocked" if hard_errors else "complete"
    metrics: Dict[str, Any] = {
        "output_version": resolved_output_version,
        "code_sha256": dict(code_hashes),
        "selected_packet_count": len(source_packets),
        "root_count": len(store.root_table),
        "page_count": len(store.page_table),
        "pending_stage_a_count": pending_count,
        "stage_a_status_counts": dict(sorted(stage_statuses.items())),
        "stage_b": {"status": "N/A", "reason": "no_real_stage_a_output"},
        "stage_c": {"status": "N/A", "reason": "no_real_stage_a_or_b_output"},
        "limits": target.to_dict(),
        "roots_mapping_complete": roots_mapping_complete,
        "page_formula": {
            "per_root": formula_rows,
            "expected_total": sum(int(row["expected_page_count"]) for row in formula_rows),
            "actual_total": len(store.page_table),
            "linear": page_formula_ok,
            "cartesian_upper_bound": sum(int(row["cartesian_page_count"]) for row in formula_rows),
        },
        "stage_a_budget": {
            "complete_total_under_2000": all(row.get("input_token_proxy", 0) <= target.max_input_token_proxy for row in materialized_rows if row.get("status") == "complete"),
            "complete_user_under_1600": all(row.get("user_token_proxy", 0) <= target.max_user_token_proxy for row in materialized_rows if row.get("status") == "complete"),
            "all_complete_within_limits": all(bool(row.get("within_limits")) for row in materialized_rows if row.get("status") == "complete"),
            "pending_snapshots_ok": pending_snapshots_ok,
        },
        "stage_a_token_proxy": {
            "total": sum(stage_a_total_tokens),
            "max": max(stage_a_total_tokens, default=0),
            "p50": _percentile(stage_a_total_tokens, 0.50),
            "p95": _percentile(stage_a_total_tokens, 0.95),
            "limit": target.max_input_token_proxy,
        },
        "stage_a_user_token_proxy": {
            "total": sum(stage_a_user_tokens),
            "max": max(stage_a_user_tokens, default=0),
            "p50": _percentile(stage_a_user_tokens, 0.50),
            "p95": _percentile(stage_a_user_tokens, 0.95),
            "limit": target.max_user_token_proxy,
        },
        "recovery_rates": recovery_rates,
        "zero_tolerance": {
            "cross_chat_scope_violations": cross_chat,
            "time_or_same_segment_strong_relation_violations": weak_strong,
            "cross_chat_strong": cross_chat,
            "time_only_strong": weak_strong,
        },
        "replay": {
            "idempotent": replay_idempotent,
            "before_counts": before_counts,
            "after_counts": after_counts,
            "cache_entries": after_cache,
            "replay_cache_hits": replay_hits,
        },
        "volume": {
            "selected_source_bytes": source_bytes,
            "store_index_bytes": index_bytes,
            "private_store_bytes": private_store_bytes,
            "pages_bytes": pages_bytes,
            "materialized_bytes": materialized_bytes,
            "recovery_bytes": recovery_bytes,
            "private_bundle_bytes": private_bundle_bytes,
            "private_bundle_to_source_ratio": volume_ratio,
            "gate": "pass" if volume_ok else "fail",
        },
        "selection": {
            "selected_count": len(selection_out),
            "mapped_count": sum(bool(row.get("complete")) for row in selection_out),
            "all_roots_mapped": roots_mapping_complete,
        },
        "provider": {"called": False, "calls": 0, "tokens": {"input": 0, "output": 0}},
    }
    aggregate: Dict[str, Any] = {
        "artifact_version": artifact_version,
        "runner_schema_version": runner_schema_version,
        "output_version": resolved_output_version,
        "code_sha256": dict(code_hashes),
        "pipeline_version": LINEAR_PIPELINE_VERSION,
        "status": status,
        "selected_packet_count": len(source_packets),
        "development_input_read": True,
        "frozen_read": False,
        "provider_called": False,
        "provider_calls": 0,
        "input_artifact_version": input_manifest.get("artifact_version"),
        "input_selected_digest": input_hash,
        "metrics": metrics,
    }
    cost: Dict[str, Any] = {
        "artifact_version": artifact_version,
        "runner_schema_version": runner_schema_version,
        "output_version": resolved_output_version,
        "code_sha256": dict(code_hashes),
        "provider_called": False,
        "provider_calls": 0,
        "provider_tokens": {"input": 0, "output": 0},
        "stage_a": {
            "page_count": len(materialized_rows),
            "input_token_proxy_total": sum(stage_a_total_tokens),
            "input_token_proxy_max": max(stage_a_total_tokens, default=0),
            "input_token_proxy_p50": _percentile(stage_a_total_tokens, 0.50),
            "input_token_proxy_p95": _percentile(stage_a_total_tokens, 0.95),
            "user_token_proxy_total": sum(stage_a_user_tokens),
            "user_token_proxy_max": max(stage_a_user_tokens, default=0),
            "user_token_proxy_p50": _percentile(stage_a_user_tokens, 0.50),
            "user_token_proxy_p95": _percentile(stage_a_user_tokens, 0.95),
        },
        "limits": target.to_dict(),
        "volume": metrics["volume"],
        "replay": metrics["replay"],
    }
    manifest: Dict[str, Any] = {
        "artifact_version": artifact_version,
        "runner_schema_version": runner_schema_version,
        "output_version": resolved_output_version,
        "code_sha256": dict(code_hashes),
        "input_artifact_version": INPUT_ARTIFACT_VERSION,
        "pipeline_version": LINEAR_PIPELINE_VERSION,
        "local_day": LOCAL_DAY,
        "split": "development",
        "input_directory_name": input_root.name if input_root is not None else "<in-memory-k5-mappings>",
        "output_directory_name": output_root.name,
        "input_selected_digest": input_hash,
        "selected_packet_count": len(source_packets),
        "root_count": len(store.root_table),
        "page_count": len(store.page_table),
        "pending_stage_a_count": pending_count,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "status": status,
        "capacity": target.to_dict(),
        "system_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "system_prompt_chars": len(prompt),
        "output_files": dict(OUTPUT_FILENAMES),
    }

    # Every body-free projection is validated before anything is written.
    _assert_body_free(manifest, label="manifest")
    _assert_body_free(aggregate, label="aggregate")
    _assert_body_free(cost, label="cost")
    _assert_body_free(hard_errors, label="errors")
    _assert_body_free(selection_out, label="selection")

    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["store"], store_private)
    _write_jsonl(output_root / OUTPUT_FILENAMES["pages"], pages_out)
    _write_jsonl(output_root / OUTPUT_FILENAMES["materialized"], materialized_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["recovery"], recovery_rows)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], hard_errors)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection"], selection_out)
    manifest["artifact_hashes"] = {
        filename: _sha256_file(output_root / filename)
        for filename in OUTPUT_FILENAMES.values()
        if filename != OUTPUT_FILENAMES["manifest"]
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)

    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return LinearDevelopmentRunResult(
        input_directory=input_directory_label,
        output_directory=str(output_root),
        selected_packet_count=len(source_packets),
        root_count=len(store.root_table),
        page_count=len(store.page_table),
        pending_count=pending_count,
        status=status,
        manifest=manifest,
        aggregate=aggregate,
        artifact_paths=paths,
        store=store,
        materialized_results=tuple(materialized_rows),
        recovery_results=tuple(recovery_rows),
    )


# Naming aliases used by the K9 workstream and by small local scripts.
run_k9_development_linear_stage_packets = run_linear_stage_packet_development
run_development_linear_stage_packets = run_linear_stage_packet_development
run_linear_stage_packets_development = run_linear_stage_packet_development


def run_linear_stage_packet_development_from_mappings(
    packet_mappings: Any,
    selection_refs: Any,
    output_directory: Union[str, Path],
    *,
    selected_packet_count: Optional[int] = SELECTION_LIMIT,
    capacity: Any = None,
    system_prompt: Optional[str] = None,
    system_prompt_a: Optional[str] = None,
    output_version: str = "v1",
) -> LinearDevelopmentRunResult:
    """Explicit spelling of the in-memory K5 mappings boundary."""

    return run_linear_stage_packet_development(
        packet_mappings,
        selection_refs,
        output_directory,
        selected_packet_count=selected_packet_count,
        capacity=capacity,
        system_prompt=system_prompt,
        system_prompt_a=system_prompt_a,
        output_version=output_version,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-directory",
        type=Path,
        default=Path("data/private/gold_standard/2026-08-25/context_packet_development_v1"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("data/private/gold_standard/2026-08-25/linear_stage_packet_development_v1"),
    )
    parser.add_argument("--selected-packet-count", type=int, default=SELECTION_LIMIT)
    parser.add_argument("--output-version", choices=("v1", "v2"), default="v1")
    args = parser.parse_args(argv)
    result = run_linear_stage_packet_development(
        args.input_directory,
        args.output_directory,
        selected_packet_count=args.selected_packet_count,
        output_version=args.output_version,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "selected_packet_count": result.selected_packet_count,
                "root_count": result.root_count,
                "page_count": result.page_count,
                "pending_count": result.pending_count,
                "output_directory": result.output_directory,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARTIFACT_VERSION",
    "ARTIFACT_VERSION_V2",
    "INPUT_ARTIFACT_VERSION",
    "LOCAL_DAY",
    "OUTPUT_FILENAMES",
    "RUNNER_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION_V2",
    "LinearDevelopmentRunResult",
    "run_development_linear_stage_packets",
    "run_k9_development_linear_stage_packets",
    "run_linear_stage_packet_development",
    "run_linear_stage_packet_development_from_mappings",
    "run_linear_stage_packets_development",
]
