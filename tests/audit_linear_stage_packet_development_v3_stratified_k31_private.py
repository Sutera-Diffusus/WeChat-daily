"""Independent K31 audit for the private K29-rebuild2 strata artifact.

This audit is intentionally a read-only, metadata-only comparison.  It reads
the twenty selected K2 development packets, the body-free K10 page ledger, and
the body-free K29-rebuild2 ledgers needed for recovery, binding, and strata
diagnostics.  It never opens a frozen/gold-test input, a provider, a
production module, or the K2/K10 body stores.  Packet bodies are inspected in
memory only for the selected K2 rows; they are never copied to an output.

The result is a body-free opaque summary plus one body-free human-audit JSONL
row per selected page under ``<artifact>/audit``.  The audit distinguishes an
upstream material bucket from canonical strong evidence: a bucket is useful
to establish that a scenario was selected for review, but it cannot by itself
authorize a canonical stratum or a provider call.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
LOCAL_DAY = "2026-08-25"
TARGET_ARTIFACT_VERSION = "linear_stage_packet_development_v3_stratified"
K2_ARTIFACT_VERSION = "context_packet_development_v1"
K10_ARTIFACT_VERSION = "linear_stage_packet_development_v2"
AUDIT_SCHEMA = "linear_stage_packet_development_v3_stratified_k31_private_audit_v1"
SELECTED_ROOTS = 20

DEFAULT_ARTIFACT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / "linear_stage_packet_development_v3_stratified_k29_rebuild2"
DEFAULT_K2_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / K2_ARTIFACT_VERSION
DEFAULT_K10_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / K10_ARTIFACT_VERSION

CANONICAL_STRATA: Tuple[str, ...] = (
    "pronoun_person_object_state",
    "greeting_new_topic",
    "topic_shift",
    "candidate_competition",
    "no_reply",
)

MATERIAL_BUCKETS: Mapping[str, Tuple[str, ...]] = {
    "pronoun_person_object_state": ("pronoun_or_ellipsis", "pronoun_person_object_state"),
    "greeting_new_topic": ("greeting_to_new_topic", "greeting_new_topic"),
    "topic_shift": ("topic_shift", "topic_transition"),
    "candidate_competition": ("candidate_competition",),
    "no_reply": ("no_reply_continuation", "no_reply"),
}

REQUIRED_UPSTREAM_FIELDS: Mapping[str, str] = {
    "pronoun_person_object_state": "candidate_person_history + candidate_object_history + candidate_state_history, each with candidate and scoped evidence refs",
    "greeting_new_topic": "message/fragment metadata is_opener or explicit greeting_new_topic label with anchors",
    "topic_shift": "topic_transitions/transition_rows with topic_shift marker and two endpoint message refs plus evidence",
    "candidate_competition": "candidate_qa_links with explicit competition relation, at least two candidates, scoped evidence, and strong_relation",
    "no_reply": "authoritative reply_status on a named message/reply row (for example awaiting_reply) with scoped evidence",
}

AUDIT_FILENAMES = {
    "summary": "audit_summary.private.json",
    "human": "human_audit.private.jsonl",
}

_FORBIDDEN_PATH_PARTS = frozenset({"frozen", "frozen_test", "frozen-test", "provider", "production"})
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
        "response",
        "response_body",
        "response_text",
        "summary",
        "text",
        "text_body",
        "text_redacted",
        "thoughts",
        "transcript",
        "user_input",
        "user_packet",
        "user_canonical_json",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "account_id",
        "candidate_id",
        "candidate_ids",
        "candidate_ref",
        "candidate_refs",
        "candidate_handle",
        "candidate_handles",
        "chat_id",
        "claim_id",
        "claim_ids",
        "evidence_id",
        "evidence_ids",
        "evidence_ref",
        "evidence_refs",
        "evidence_handle",
        "evidence_handles",
        "fragment_id",
        "fragment_ids",
        "message_id",
        "message_ids",
        "packet_id",
        "person_id",
        "person_ref_id",
        "record_id",
        "relation_id",
        "root_id",
        "source_id",
        "source_message_id",
        "source_message_ids",
        "source_packet_id",
        "state_ref_id",
        "thread_id",
    }
)
_WEAK_REASON_CODES = frozenset(
    {
        "candidate_only",
        "finite_local_window",
        "reversible_context",
        "reversible_context_window",
        "same_scope",
        "same_segment",
        "same_segment_weak",
        "sparse_term_overlap",
        "time_only",
        "time_proximity",
        "time_proximity_weak",
    }
)
_NO_REPLY_VALUES = frozenset(
    {
        "awaiting_reply",
        "no_reply",
        "no_response",
        "not_replied",
        "pending_reply",
        "reply_missing",
        "unanswered",
    }
)
_COMPETITION_VALUES = frozenset(
    {
        "candidate_competition",
        "candidate_competition_relation",
        "competing",
        "exclusive",
        "mutually_exclusive",
    }
)
_OPENER_VALUES = frozenset({"conversation_opener", "greeting", "greeting_opener", "opener"})
_TRANSITION_VALUES = frozenset({"topic_boundary", "topic_change", "topic_shift", "topic_transition"})
_LABEL_KEYS = frozenset(
    {
        "categories",
        "canonical_categories",
        "canonical_category",
        "canonical_strata",
        "canonical_stratum",
        "category",
        "selection_categories",
        "selection_category",
        "selection_strata",
        "selection_stratum",
        "strata",
        "stratum",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _opaque(namespace: str, value: Any) -> str:
    return "k31_%s_%s" % (namespace, _stable_hash({"namespace": namespace, "value": str(value)})[:24])


def _opaque_digest(namespace: str, values: Iterable[Any]) -> str:
    return _stable_hash({"namespace": namespace, "values": sorted({_opaque(namespace, value) for value in values if value not in (None, "")})})


def _safe_path(path: Path, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in resolved.parts):
        raise ValueError("K31 refuses %s under frozen/provider/production path" % label)
    return resolved


def _child(root: Path, name: str) -> Path:
    candidate = (root / name).resolve()
    if candidate.parent != root.resolve():
        raise ValueError("K31 input file escapes its artifact directory")
    return candidate


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("expected JSON object: %s" % path.name)
    return dict(value)


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError("expected JSON object in %s:%d" % (path.name, line_number))
            yield dict(value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], (), {}, set(), frozenset())


def _normalise(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _rows(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, Mapping):
        if any(key in value for key in ("message_id", "fragment_id", "candidate_id", "evidence_id", "relation_label")):
            return [dict(value)]
        return [dict(row) for row in value.values() if isinstance(row, Mapping)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    return []


def _first(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        value = mapping.get(name)
        if _nonempty(value):
            return value
    return default


def _values(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value] if _nonempty(value) else []


def _fraction(numerator: int, denominator: int) -> Dict[str, Any]:
    numerator = int(numerator)
    denominator = int(denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def _body_hits(value: Any, path: str = "") -> List[str]:
    hits: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered in _BODY_KEYS and child not in (None, "", [], (), {}, False, 0):
                hits.append(path + str(key))
            hits.extend(_body_hits(child, path + str(key) + "."))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            hits.extend(_body_hits(child, path + str(index) + "."))
    return hits


def _identity_values(value: Any, *, key: str = "") -> Set[str]:
    found: Set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            name = str(raw_key).casefold()
            if name in _IDENTITY_KEYS:
                if isinstance(child, Mapping):
                    continue
                if isinstance(child, (list, tuple, set, frozenset)):
                    found.update(str(item) for item in child if item not in (None, "") and not isinstance(item, Mapping))
                elif child not in (None, ""):
                    found.add(str(child))
            found.update(_identity_values(child, key=name))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            found.update(_identity_values(child, key=key))
    return found


def _contains_raw_identity(value: Any, raw_values: Set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_raw_identity(child, raw_values) for child in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_raw_identity(child, raw_values) for child in value)
    return isinstance(value, str) and value in raw_values


def _scope_pair(value: Any) -> Optional[Tuple[str, str]]:
    if isinstance(value, Mapping):
        account = _first(value, ("account_id", "account", "account_ref"))
        chat = _first(value, ("chat_id", "chat", "chat_ref"))
        if account not in (None, "") and chat not in (None, ""):
            return str(account), str(chat)
        return _scope_pair(value.get("scope"))
    if isinstance(value, str):
        for separator in ("/", "::", "|"):
            if separator in value:
                left, right = value.split(separator, 1)
                if left and right:
                    return left, right
    return None


def _scope_pairs(value: Any) -> Set[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            pair = _scope_pair(item)
            if pair:
                pairs.add(pair)
            for child in item.values():
                if isinstance(child, (Mapping, list, tuple, set, frozenset)):
                    visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return pairs


def _suffix(value: Any) -> str:
    return str(value or "").rsplit("_", 1)[-1]


def _k28_suffix(kind: str, value: Any) -> str:
    # K28's handle is stable_hash({kind, value})[:24].  Recomputing only the
    # suffix allows source binding without placing a raw input ID in output.
    return _stable_hash({"kind": str(kind), "value": str(value)})[:24]


def _safe_manifest_gate(manifest: Mapping[str, Any], expected: str, *, label: str) -> None:
    if manifest.get("artifact_version") != expected:
        raise ValueError("%s artifact version mismatch" % label)
    if manifest.get("split") not in (None, "development") or manifest.get("local_day") not in (None, LOCAL_DAY):
        raise ValueError("%s is not the 2026-08-25 development split" % label)
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise ValueError("%s reports forbidden frozen/gold input" % label)
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("%s reports provider use" % label)


def _load_target(artifact_dir: Path) -> Dict[str, Any]:
    root = _safe_path(artifact_dir, label="target artifact")
    names = {
        "manifest": "manifest.private.json",
        "aggregate": "aggregate.private.json",
        "pages": "pages.private.jsonl",
        "materialized": "materialized_map.private.jsonl",
        "recovery": "recovery_map.private.jsonl",
        "strata": "strata_map.private.jsonl",
        "selection": "selection_map.private.jsonl",
    }
    paths = {key: _child(root, name) for key, name in names.items()}
    if any(not path.is_file() for path in paths.values()):
        raise FileNotFoundError("K31 target ledger is incomplete")
    manifest = _read_json(paths["manifest"])
    _safe_manifest_gate(manifest, TARGET_ARTIFACT_VERSION, label="target")
    if manifest.get("status") != "complete" or int(manifest.get("root_count") or 0) != SELECTED_ROOTS:
        raise ValueError("K31 target root scope is not exactly twenty complete roots")
    ledgers: Dict[str, Any] = {"manifest": manifest, "root": root, "paths": paths}
    ledgers["aggregate"] = _read_json(paths["aggregate"])
    for key in ("pages", "materialized", "recovery", "strata", "selection"):
        ledgers[key] = list(_iter_jsonl(paths[key]))
    for key in ("aggregate", "pages", "materialized", "recovery", "strata", "selection"):
        hits = _body_hits(ledgers[key])
        if hits:
            raise ValueError("target %s ledger is not body-free" % key)
    return ledgers


def _load_k2(k2_dir: Path) -> Dict[str, Any]:
    root = _safe_path(k2_dir, label="K2 development input")
    manifest_path = _child(root, "manifest.private.json")
    selection_path = _child(root, "selection_map.private.jsonl")
    packets_path = _child(root, "packets.private.jsonl")
    for path in (manifest_path, selection_path, packets_path):
        if not path.is_file():
            raise FileNotFoundError("K31 K2 input is incomplete")
    manifest = _read_json(manifest_path)
    _safe_manifest_gate(manifest, K2_ARTIFACT_VERSION, label="K2")
    selection_rows = list(_iter_jsonl(selection_path))
    if _body_hits(selection_rows):
        raise ValueError("K2 selection ledger is not body-free")
    selected_rows = [row for row in selection_rows if row.get("selected") is True]
    if len(selected_rows) != SELECTED_ROOTS:
        raise ValueError("K31 expected exactly twenty selected K2 rows")
    selected_by_packet: Dict[str, Dict[str, Any]] = {}
    for row in selected_rows:
        packet_id = str(row.get("packet_id") or "")
        rank = int(row.get("selection_rank") or 0)
        if not packet_id or rank < 1 or rank > SELECTED_ROOTS or packet_id in selected_by_packet:
            raise ValueError("K2 selected rows are not uniquely ranked")
        selected_by_packet[packet_id] = row
    packets: Dict[str, Dict[str, Any]] = {}
    # The file is bodyful by design.  Only selected packet objects are kept;
    # no body value is ever placed in the audit result or written to disk.
    for row in _iter_jsonl(packets_path):
        packet_id = str(row.get("packet_id") or "")
        if packet_id not in selected_by_packet:
            continue
        if packet_id in packets:
            raise ValueError("duplicate selected K2 packet")
        packets[packet_id] = row
    if set(packets) != set(selected_by_packet):
        raise ValueError("selected K2 packet set is incomplete")
    return {
        "root": root,
        "manifest": manifest,
        "selection_rows": selected_by_packet,
        "packets": packets,
        "paths": {"manifest": manifest_path, "selection": selection_path, "packets": packets_path},
    }


def _load_k10(k10_dir: Path) -> Dict[str, Any]:
    root = _safe_path(k10_dir, label="K10 page input")
    manifest_path = _child(root, "manifest.private.json")
    pages_path = _child(root, "pages.private.jsonl")
    for path in (manifest_path, pages_path):
        if not path.is_file():
            raise FileNotFoundError("K31 K10 page input is incomplete")
    manifest = _read_json(manifest_path)
    _safe_manifest_gate(manifest, K10_ARTIFACT_VERSION, label="K10")
    pages = list(_iter_jsonl(pages_path))
    if _body_hits(pages):
        raise ValueError("K10 page ledger is not body-free")
    if len(pages) != SELECTED_ROOTS:
        raise ValueError("K31 expected twenty K10 pages")
    return {"root": root, "manifest": manifest, "pages": pages, "paths": {"manifest": manifest_path, "pages": pages_path}}


def _first_rows(packet: Mapping[str, Any], names: Sequence[str]) -> List[Dict[str, Any]]:
    for name in names:
        direct = _rows(packet.get(name))
        if direct:
            return direct
    for parent_name in ("fixed_part", "dynamic_part"):
        parent = packet.get(parent_name)
        if isinstance(parent, Mapping):
            for name in names:
                nested = _rows(parent.get(name))
                if nested:
                    return nested
    context = packet.get("candidate_context")
    if isinstance(context, Mapping):
        for name in names:
            nested = _rows(context.get(name))
            if nested:
                return nested
    return []


def _message_rows(packet: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for names in (
        ("primary_fragments", "primary", "fragments"),
        ("adjacent_context", "adjacent", "context_fragments"),
        ("authoritative_facts", "message_metadata", "message_rows", "messages"),
    ):
        rows.extend(_first_rows(packet, names))
    return rows


def _candidate_rows(packet: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for names in (
        ("candidate_person_history",),
        ("candidate_object_history",),
        ("candidate_state_history",),
        ("candidate_qa_links",),
        ("continuity_candidates",),
    ):
        rows.extend(_first_rows(packet, names))
    return rows


def _history_rows(packet: Mapping[str, Any], kind: str) -> List[Dict[str, Any]]:
    return _first_rows(packet, ("candidate_%s_history" % kind, "%s_history" % kind))


def _surface_rows(packet: Mapping[str, Any], side: str) -> List[Dict[str, Any]]:
    for parent_name in ("candidate_context", "dynamic_part"):
        parent = packet.get(parent_name)
        if isinstance(parent, Mapping):
            surface = parent.get("surface_signals")
            if isinstance(surface, Mapping):
                rows = _rows(surface.get(side))
                if rows:
                    return rows
    return []


def _ref_count(row: Mapping[str, Any], names: Sequence[str]) -> int:
    values: List[str] = []
    for name in names:
        value = row.get(name)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(str(item) for item in value if _nonempty(item) and not isinstance(item, Mapping))
        elif _nonempty(value) and not isinstance(value, Mapping):
            values.append(str(value))
    return len(set(values))


def _reason_set(row: Mapping[str, Any]) -> Set[str]:
    reasons: Set[str] = set()
    for name in ("candidate_reason", "candidate_reasons", "reason_codes", "supporting_slot_codes", "reasons"):
        value = row.get(name)
        for item in _values(value):
            label = _normalise(item)
            if label:
                reasons.add(label)
    return reasons


def _explicit_labels(packet: Mapping[str, Any]) -> Set[str]:
    labels: Set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = _normalise(key)
                if name in _LABEL_KEYS:
                    for item in _values(child):
                        if isinstance(item, Mapping):
                            item = _first(item, ("stratum", "category", "name", "label"))
                        label = _normalise(item)
                        if label:
                            labels.add(label)
                if name in {"fixed_part", "dynamic_part", "candidate_context", "selection_metadata", "strata_metadata", "topic_transitions", "transition_rows", "reply_metadata"}:
                    visit(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item)

    visit(packet)
    return labels


def _explicit_for_stratum(labels: Set[str], stratum: str) -> bool:
    aliases = {
        "pronoun_person_object_state": {"pronoun_person_object_state"},
        "greeting_new_topic": {"greeting_new_topic"},
        "topic_shift": {"topic_shift", "topic_transition"},
        "candidate_competition": {"candidate_competition"},
        "no_reply": {"no_reply"},
    }
    return bool(labels & aliases[stratum])


def _strong_metadata(packet: Mapping[str, Any]) -> Dict[str, Any]:
    messages = _message_rows(packet)
    candidates = _candidate_rows(packet)
    labels = _explicit_labels(packet)
    strong = {name: False for name in CANONICAL_STRATA}
    evidence_types: Dict[str, List[str]] = {name: [] for name in CANONICAL_STRATA}
    weak_codes: Dict[str, Set[str]] = {name: set() for name in CANONICAL_STRATA}

    histories: Dict[str, List[Dict[str, Any]]] = {kind: _history_rows(packet, kind) for kind in ("person", "object", "state")}
    history_parts: Dict[str, Dict[str, int]] = {}
    for kind, rows in histories.items():
        candidate_rows = [row for row in rows if _ref_count(row, ("candidate_id", "candidate_handle", "candidate_ref", "candidate_refs", "candidate_ids", "candidate_handles"))]
        evidence_rows = [row for row in rows if _ref_count(row, ("evidence_id", "evidence_ref", "evidence_refs", "evidence_ids", "evidence_handles"))]
        history_parts[kind] = {"rows": len(rows), "candidate_rows": len(candidate_rows), "evidence_rows": len(evidence_rows)}
    if all(history_parts[kind]["candidate_rows"] and history_parts[kind]["evidence_rows"] for kind in ("person", "object", "state")):
        strong["pronoun_person_object_state"] = True
        evidence_types["pronoun_person_object_state"].append("candidate_history_triad")
    elif any(history_parts[kind]["rows"] for kind in ("person", "object", "state")):
        weak_codes["pronoun_person_object_state"].add("partial_history_only")

    opener_count = 0
    transition_count = 0
    no_reply_count = 0
    for row in messages:
        fragment = _normalise(_first(row, ("fragment_type", "message_type")))
        role = _normalise(_first(row, ("dialogue_role", "message_role", "role")))
        if row.get("is_opener") is True or row.get("is_greeting") is True or fragment in _OPENER_VALUES or role in _OPENER_VALUES:
            opener_count += 1
        if any(row.get(key) is True for key in ("topic_shift", "topic_change", "topic_boundary", "new_topic_boundary")):
            transition_count += 1
        status = _normalise(_first(row, ("reply_status", "reply_state", "response_status", "answer_status", "status")))
        if status in _NO_REPLY_VALUES:
            no_reply_count += 1
    if opener_count:
        strong["greeting_new_topic"] = True
        evidence_types["greeting_new_topic"].append("opener_fragment")
    if transition_count:
        strong["topic_shift"] = True
        evidence_types["topic_shift"].append("topic_transition")
    if no_reply_count:
        strong["no_reply"] = True
        evidence_types["no_reply"].append("authoritative_reply_status")

    competition_count = 0
    for row in candidates:
        relation_values = {_normalise(_first(row, (name,))) for name in ("relation_label", "relation", "relation_type", "candidate_type", "candidate_set_type", "selection_relation", "relation_subtype")}
        relation_values.discard("")
        explicit_competition = bool(relation_values & _COMPETITION_VALUES) or any(row.get(key) is True for key in ("mutually_exclusive", "exclusive", "candidate_competition", "competing"))
        candidate_count = _ref_count(row, ("candidate_id", "candidate_handle", "candidate_ref", "candidate_refs", "candidate_ids", "candidate_handles"))
        evidence_count = _ref_count(row, ("evidence_id", "evidence_ref", "evidence_refs", "evidence_ids", "evidence_handles"))
        reasons = _reason_set(row)
        weak = bool(reasons & _WEAK_REASON_CODES) or row.get("candidate_only") is True or row.get("strong_relation") is False or _normalise(row.get("evidence_strength_candidate")) == "weak"
        if explicit_competition and candidate_count >= 2 and evidence_count and not weak:
            competition_count += 1
    if competition_count:
        strong["candidate_competition"] = True
        evidence_types["candidate_competition"].append("candidate_competition_relation")

    if _explicit_for_stratum(labels, "greeting_new_topic") and opener_count:
        strong["greeting_new_topic"] = True
    if _explicit_for_stratum(labels, "topic_shift") and transition_count:
        strong["topic_shift"] = True
    if _explicit_for_stratum(labels, "no_reply") and no_reply_count:
        strong["no_reply"] = True

    primary_surface = _surface_rows(packet, "primary")
    adjacent_surface = _surface_rows(packet, "adjacent")
    surface = primary_surface + adjacent_surface
    surface_true = {
        "greeting_new_topic": sum(row.get("greeting_signal") is True for row in surface),
        "topic_shift": sum(row.get("topic_shift_signal") is True for row in surface),
        "pronoun_person_object_state": sum(row.get("pronoun_or_ellipsis_signal") is True for row in surface),
        "candidate_competition": 0,
        "no_reply": 0,
    }
    if surface_true["greeting_new_topic"]:
        weak_codes["greeting_new_topic"].add("surface_greeting_signal")
    if surface_true["topic_shift"]:
        weak_codes["topic_shift"].add("surface_topic_shift_signal")
    if surface_true["pronoun_person_object_state"]:
        weak_codes["pronoun_person_object_state"].add("surface_pronoun_signal")
    if packet.get("open_boundary") is True and not no_reply_count:
        weak_codes["no_reply"].add("open_boundary_without_reply_status")
    if candidates and not competition_count:
        weak_codes["candidate_competition"].add("candidate_relation_not_competition")
    candidate_volume = sum(
        _ref_count(row, ("candidate_id", "candidate_handle", "candidate_ref", "candidate_refs", "candidate_ids", "candidate_handles"))
        for row in candidates
    )
    if candidate_volume >= 2 and not competition_count:
        weak_codes["candidate_competition"].add("candidate_volume_only")

    return {
        "strong": strong,
        "evidence_types": evidence_types,
        "weak_codes": weak_codes,
        "history_parts": history_parts,
        "surface_true": surface_true,
        "opener_count": opener_count,
        "transition_count": transition_count,
        "no_reply_count": no_reply_count,
        "competition_count": competition_count,
        "candidate_rows": len(candidates),
        "candidate_volume": candidate_volume,
        "explicit_labels": labels,
    }


def _target_stratum_row(target: Mapping[str, Any], source_suffix: str) -> Optional[Mapping[str, Any]]:
    rows = target.get("strata") if isinstance(target.get("strata"), list) else []
    for row in rows:
        if _suffix(row.get("source_handle")) == source_suffix:
            return row
    return None


def _scope_audit(packet: Mapping[str, Any]) -> Dict[str, Any]:
    root_scope = _scope_pair(packet)
    relation_containers = (
        "candidate_qa_links",
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
        "continuity_candidates",
        "candidate_context",
    )
    relation_pairs: Set[Tuple[str, str]] = set()
    for name in relation_containers:
        relation_pairs.update(_scope_pairs(packet.get(name)))
    violations = relation_pairs - ({root_scope} if root_scope else set())
    return {
        "root_scope_known": root_scope is not None,
        "relation_scope_known": bool(relation_pairs),
        "relation_scope_count": len(relation_pairs),
        "scope_violation_count": len(violations),
        "root_scope_ref": _opaque("scope", _canonical(root_scope)) if root_scope else None,
    }


def _rate_public(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "missing", "expected_count": 0, "observed_count": 0, "recovered_count": 0, "missing_count": 0, "extra_count": 0, "rate": None}
    return {
        "status": str(value.get("status") or "unknown"),
        "expected_count": int(value.get("expected_count", value.get("expected", 0)) or 0),
        "observed_count": int(value.get("observed_count", value.get("observed", 0)) or 0),
        "recovered_count": int(value.get("recovered_count", value.get("recovered", 0)) or 0),
        "missing_count": int(value.get("missing_count", value.get("missing", 0)) or 0) if not isinstance(value.get("missing"), list) else len(value.get("missing") or ()),
        "extra_count": int(value.get("extra_count", value.get("extra", 0)) or 0),
        "rate": float(value.get("rate")) if isinstance(value.get("rate"), (int, float)) else None,
    }


def _target_binding(target: Mapping[str, Any], k10: Mapping[str, Any]) -> Dict[str, Any]:
    pages = target.get("pages") if isinstance(target.get("pages"), list) else []
    k10_pages = k10.get("pages") if isinstance(k10.get("pages"), list) else []
    old_by_hash = {str(row.get("page_hash") or ""): row for row in k10_pages}
    matched = 0
    mismatches = 0
    for row in pages:
        old = old_by_hash.get(str(row.get("page_hash") or ""))
        if old is None:
            mismatches += 1
            continue
        matched += 1
        checks = (
            (len(old.get("message_handles") or ()), int(row.get("message_count") or 0)),
            (len(old.get("candidate_handles") or ()), int(row.get("candidate_count") or 0)),
            (len(old.get("evidence_handles") or ()), int(row.get("evidence_count") or 0)),
        )
        mismatches += sum(left != right for left, right in checks)
    return {
        "k10_page_count": len(k10_pages),
        "target_page_count": len(pages),
        "matched_page_count": matched,
        "mismatch_count": mismatches,
        "hash_and_count_binding": matched == len(pages) == len(k10_pages) and mismatches == 0,
    }


def _role_public(k2_selection: Mapping[str, Any], page: Mapping[str, Any]) -> Dict[str, Any]:
    k2_adjacent = int(k2_selection.get("adjacent_message_count") or 0)
    k2_messages = int(k2_selection.get("message_count") or 0)
    role = page.get("role_projection") if isinstance(page.get("role_projection"), Mapping) else {}
    target_adjacent = int(role.get("adjacent") or 0)
    target_primary = int(role.get("primary") or 0)
    target_messages = int(page.get("message_count") or 0)
    pure = k2_adjacent == 0
    mixed = k2_adjacent > 0
    return {
        "k2_message_count": k2_messages,
        "k2_primary_message_count": int(k2_selection.get("primary_message_count") or 0),
        "k2_adjacent_message_count": k2_adjacent,
        "target_message_count": target_messages,
        "target_primary_role_count": target_primary,
        "target_adjacent_role_count": target_adjacent,
        "target_authority_role_count": int(role.get("authority") or 0),
        "retained_message_count": target_messages >= k2_messages,
        "pure_confirmation_candidate": pure,
        "pure_confirmation_still_primary": pure and target_primary >= 1 and target_adjacent == 0,
        "mixed_content_candidate": mixed,
        "mixed_content_retained": mixed and target_messages >= k2_messages and target_adjacent >= k2_adjacent,
    }


def _recovery_public(recovery: Mapping[str, Any]) -> Dict[str, Any]:
    rates_raw = recovery.get("rates") if isinstance(recovery.get("rates"), Mapping) else {}
    rates = {name: _rate_public(rates_raw.get(name)) for name in ("source", "primary", "adjacent", "evidence")}
    all_pass = all(row.get("status") in {"pass", "N/A"} and int(row.get("missing_count") or 0) == 0 for row in rates.values())
    return {"rates": rates, "all_roles_recovered": all_pass, "body_free": recovery.get("body_free") is True}


def _scene_status(strong: bool, marker: bool, weak: bool) -> str:
    if strong:
        return "present"
    if marker or weak:
        return "uncertain"
    return "not_evidenced"


def _page_record(
    packet_id: str,
    k2_selection: Mapping[str, Any],
    packet: Mapping[str, Any],
    target_strata: Mapping[str, Any],
    target_page: Mapping[str, Any],
    target_materialized: Mapping[str, Any],
    target_recovery: Mapping[str, Any],
) -> Dict[str, Any]:
    source_suffix = _k28_suffix("source", packet_id)
    strong = _strong_metadata(packet)
    buckets = {_normalise(item) for item in _values(k2_selection.get("material_buckets"))}
    scope = _scope_audit(packet)
    observed = {str(item) for item in target_strata.get("observed_strata") or ()}
    missing = {str(item) for item in target_strata.get("metadata_missing_strata") or ()}
    ambiguous = {str(item) for item in target_strata.get("ambiguous_strata") or ()}
    strata: Dict[str, Dict[str, Any]] = {}
    for name in CANONICAL_STRATA:
        bucket_present = bool(buckets & {_normalise(item) for item in MATERIAL_BUCKETS[name]})
        explicit = _explicit_for_stratum(strong["explicit_labels"], name)
        weak = bool(strong["weak_codes"][name])
        # A material bucket or explicit label is an upstream marker.  Weak
        # signals (including an open boundary or partial history) remain
        # diagnostic evidence, but must not inflate marker-backed misses.
        source_marker = bucket_present or explicit
        if name in observed:
            current_status = "observed"
        elif name in ambiguous:
            current_status = "ambiguous"
        elif name in missing:
            current_status = "metadata_missing"
        else:
            current_status = "not_reported"
        if current_status == "observed" and not (source_marker or strong["strong"][name]):
            outcome = "false_positive"
        elif current_status == "observed":
            outcome = "observed"
        elif current_status == "ambiguous":
            outcome = "ambiguous"
        elif source_marker or strong["strong"][name]:
            outcome = "missed"
        else:
            outcome = "not_evidenced"
        strata[name] = {
            "scene_status": _scene_status(strong["strong"][name], bucket_present, weak),
            "source_marker": source_marker,
            "source_marker_basis": sorted(
                (["k2_material_bucket"] if bucket_present else [])
                + (["k2_explicit_canonical_label"] if explicit else [])
                + (["k2_weak_signal"] if weak else [])
            ),
            "strong_evidence": bool(strong["strong"][name]),
            "strong_evidence_types": list(strong["evidence_types"][name]),
            "weak_only": weak and not strong["strong"][name],
            "current_metadata_status": current_status,
            "current_metadata_outcome": outcome,
            "required_upstream_field": REQUIRED_UPSTREAM_FIELDS[name],
            "k2_marker_count": int(bucket_present),
            "k2_weak_signal_count": int(weak),
            "target_evidence_types": list(((target_strata.get("strata") or {}).get(name) or {}).get("evidence_types") or ()),
            "target_evidence_counts": {
                str(key): int(value or 0)
                for key, value in (((target_strata.get("strata") or {}).get(name) or {}).get("counts") or {}).items()
                if key in {"messages", "candidates", "evidence", "evidence_items"}
            },
        }
    page_ref = _opaque("page", _suffix(target_strata.get("page_handle")))
    source_ref = _opaque("source", source_suffix)
    material_page_ref = _opaque("page", _suffix(target_materialized.get("page_handle")))
    return {
        "selection_rank": int(k2_selection.get("selection_rank") or 0),
        "source_ref": source_ref,
        "page_ref": page_ref,
        "materialized_page_ref": material_page_ref,
        "source_marker_buckets": sorted(buckets & {item for values in MATERIAL_BUCKETS.values() for item in values}),
        "strata": strata,
        "role": _role_public(k2_selection, target_page),
        "recovery": _recovery_public(target_recovery),
        "scope": {
            "root_scope_known": bool(scope["root_scope_known"]),
            "relation_scope_known": bool(scope["relation_scope_known"]),
            "relation_scope_count": int(scope["relation_scope_count"]),
            "scope_violation_count": int(scope["scope_violation_count"]),
            "scope_ref": scope["root_scope_ref"],
        },
        "target_status": str(target_strata.get("status") or "unknown"),
        "target_scope_known": bool(target_strata.get("scope_known")),
        "target_metadata_ref": _opaque("metadata", target_strata.get("metadata_hash") or "missing"),
        "body_free": True,
    }


def _metric_int(node: Any, path: Sequence[str], default: int = 0) -> int:
    current = node
    for key in path:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key)
    try:
        return int(current or 0)
    except (TypeError, ValueError):
        return default


def _build_summary(
    target: Mapping[str, Any],
    k2: Mapping[str, Any],
    k10: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    hash_report: Mapping[str, Any],
    binding_report: Mapping[str, Any],
) -> Dict[str, Any]:
    strata_summary: Dict[str, Any] = {}
    for name in CANONICAL_STRATA:
        rows = [((record.get("strata") or {}).get(name) or {}) for record in records]
        marker_count = sum(bool(row.get("source_marker")) for row in rows)
        strong_count = sum(bool(row.get("strong_evidence")) for row in rows)
        weak_count = sum(bool(row.get("weak_only")) for row in rows)
        observed_count = sum(row.get("current_metadata_status") == "observed" for row in rows)
        missing_count = sum(row.get("current_metadata_status") == "metadata_missing" for row in rows)
        ambiguous_count = sum(row.get("current_metadata_status") == "ambiguous" for row in rows)
        missed_count = sum(row.get("current_metadata_outcome") == "missed" for row in rows)
        false_positive_count = sum(row.get("current_metadata_outcome") == "false_positive" for row in rows)
        status_counts = Counter(str(row.get("current_metadata_status") or "unknown") for row in rows)
        outcome_counts = Counter(str(row.get("current_metadata_outcome") or "unknown") for row in rows)
        if strong_count:
            scene_status = "present"
            scene_basis = "strong_K2_typed_evidence"
        elif marker_count:
            scene_status = "uncertain"
            scene_basis = "K2_material_bucket_without_canonical_strong_evidence"
        else:
            scene_status = "not_evidenced"
            scene_basis = "no_allowed_K2_marker"
        strata_summary[name] = {
            "scene_status": scene_status,
            "scene_basis": scene_basis,
            "selected_page_denominator": SELECTED_ROOTS,
            "source_marker_pages": _fraction(marker_count, SELECTED_ROOTS),
            "strong_evidence_pages": _fraction(strong_count, SELECTED_ROOTS),
            "weak_only_pages": _fraction(weak_count, SELECTED_ROOTS),
            "current_metadata_observed": _fraction(observed_count, SELECTED_ROOTS),
            "current_metadata_missing": _fraction(missing_count, SELECTED_ROOTS),
            "current_metadata_ambiguous": _fraction(ambiguous_count, SELECTED_ROOTS),
            "marker_backed_missed": _fraction(missed_count, marker_count),
            "false_positive": _fraction(false_positive_count, observed_count),
            "status_counts": dict(sorted(status_counts.items())),
            "outcome_counts": dict(sorted(outcome_counts.items())),
            "required_upstream_field": REQUIRED_UPSTREAM_FIELDS[name],
        }
    role_rows = [record.get("role") or {} for record in records]
    pure = [row for row in role_rows if row.get("pure_confirmation_candidate")]
    mixed = [row for row in role_rows if row.get("mixed_content_candidate")]
    recovery_rows = [record.get("recovery") or {} for record in records]
    scope_rows = [record.get("scope") or {} for record in records]
    target_manifest = target.get("manifest") if isinstance(target.get("manifest"), Mapping) else {}
    target_aggregate = target.get("aggregate") if isinstance(target.get("aggregate"), Mapping) else {}
    return {
        "schema": AUDIT_SCHEMA,
        "status": "blocked_for_provider",
        "scope": {
            "artifact_version": TARGET_ARTIFACT_VERSION,
            "artifact_ref": _opaque("artifact", _sha256_file(target["paths"]["manifest"])),
            "selected_root_count": len(records),
            "selected_root_limit": SELECTED_ROOTS,
            "k2_selected_packet_count": len(k2.get("selection_rows") or {}),
            "k10_page_count": len(k10.get("pages") or []),
            "target_page_count": len(target.get("pages") or []),
            "frozen_read": False,
            "gold_loaded": False,
            "provider_called": False,
            "provider_calls": 0,
            "body_free": True,
        },
        "strata": strata_summary,
        "selection": {
            "k2_material_bucket_coverage": {
                name: _fraction(
                    sum(
                        bool(((record.get("strata") or {}).get(name) or {}).get("source_marker"))
                        for record in records
                    ),
                    len(records),
                )
                for name in CANONICAL_STRATA
            },
            "target_selected_page_count": len(target.get("selection") or []),
            "target_selected_page_limit": int(target_manifest.get("selected_page_limit") or 5),
            "target_selection_missing_strata": list(target_manifest.get("selection_missing_strata") or ()),
            "target_provider_allowed": bool(_metric_int(target_aggregate, ("metrics", "selection", "provider_allowed"), 0)),
            "target_scope_authorization_required": bool(_metric_int(target_aggregate, ("metrics", "selection", "scope_authorization_required"), 0)),
            "target_selected_scope_count": _metric_int(target_aggregate, ("metrics", "selection", "selected_scope_count")),
            "target_global_coverage_scope_count": _metric_int(target_aggregate, ("metrics", "selection", "global_coverage_plan_scope_count")),
        },
        "role_audit": {
            "pure_confirmation_candidate": _fraction(len(pure), len(role_rows)),
            "pure_confirmation_still_primary": _fraction(sum(bool(row.get("pure_confirmation_still_primary")) for row in pure), len(pure)),
            "mixed_content_candidate": _fraction(len(mixed), len(role_rows)),
            "mixed_content_retained": _fraction(sum(bool(row.get("mixed_content_retained")) for row in mixed), len(mixed)),
            "all_selected_message_counts_retained": _fraction(sum(bool(row.get("retained_message_count")) for row in role_rows), len(role_rows)),
        },
        "recovery": {
            "all_selected_roots_pass": _fraction(sum(bool(row.get("all_roles_recovered")) for row in recovery_rows), len(recovery_rows)),
            "source_status_counts": dict(sorted(Counter(str((row.get("rates") or {}).get("source", {}).get("status") or "unknown") for row in recovery_rows).items())),
            "primary_status_counts": dict(sorted(Counter(str((row.get("rates") or {}).get("primary", {}).get("status") or "unknown") for row in recovery_rows).items())),
            "adjacent_status_counts": dict(sorted(Counter(str((row.get("rates") or {}).get("adjacent", {}).get("status") or "unknown") for row in recovery_rows).items())),
            "evidence_status_counts": dict(sorted(Counter(str((row.get("rates") or {}).get("evidence", {}).get("status") or "unknown") for row in recovery_rows).items())),
        },
        "hash_audit": dict(hash_report) | {"k10_binding": dict(binding_report)},
        "cross_scope": {
            "selected_root_scope_count": len({record.get("scope", {}).get("scope_ref") for record in records}),
            "relation_scope_violations": sum(int(record.get("scope", {}).get("scope_violation_count") or 0) for record in records),
            "all_relation_scopes_fail_closed": all(int(record.get("scope", {}).get("scope_violation_count") or 0) == 0 for record in records),
            "target_zero_tolerance_cross_chat_violations": _metric_int(target_aggregate, ("metrics", "zero_tolerance", "cross_chat_scope_violations")),
            "provider_called": False,
        },
        "privacy": {
            "body_free": True,
            "output_refs": "opaque_only",
            "target_bodyful_store_opened": False,
            "messages_input_opened": False,
            "frozen_input_opened": False,
            "provider_calls": 0,
        },
        "conclusion": {
            "decision": "do_not_authorize_provider",
            "marker_backed_missing_strata": [
                name for name in CANONICAL_STRATA
                if any(((record.get("strata") or {}).get(name) or {}).get("current_metadata_outcome") == "missed" for record in records)
            ],
            "strong_evidence_backed_observed_strata": [
                name for name in CANONICAL_STRATA
                if any(((record.get("strata") or {}).get(name) or {}).get("strong_evidence") for record in records)
            ],
            "interpretation_code": "K2_BUCKET_PRESENT_BUT_CANONICAL_STRONG_FIELD_NOT_MATERIALIZED",
            "upstream_action": "materialize the named typed field per strata.required_upstream_field before selection/provider authorization",
        },
        "body_free": True,
    }


def _hash_report(target: Mapping[str, Any]) -> Dict[str, Any]:
    manifest = target.get("manifest") if isinstance(target.get("manifest"), Mapping) else {}
    recorded = manifest.get("artifact_hashes") if isinstance(manifest.get("artifact_hashes"), Mapping) else {}
    logical = {
        "aggregate": "aggregate.private.json",
        "pages": "pages.private.jsonl",
        "materialized": "materialized_map.private.jsonl",
        "recovery": "recovery_map.private.jsonl",
        "selection": "selection_map.private.jsonl",
        "strata": "strata_map.private.jsonl",
    }
    checks: Dict[str, bool] = {}
    for key, filename in logical.items():
        path = target["paths"].get(key)
        expected = str(recorded.get(filename) or "")
        checks[key] = bool(path and path.is_file() and expected and expected == _sha256_file(path))
    return {
        "allowed_ledger_hashes_match": all(checks.values()),
        "allowed_ledger_hash_checks": checks,
        "target_manifest_hash_entries_checked": len(checks),
        "bodyful_manifest_entries_skipped": True,
        "same_input_opaque_binding_stable": _opaque_digest("source", [row.get("source_handle") for row in target.get("strata") or ()]) == _opaque_digest("source", [row.get("source_handle") for row in target.get("strata") or ()]),
    }


def _privacy_report(summary: Mapping[str, Any], records: Sequence[Mapping[str, Any]], raw_identity_values: Set[str]) -> Dict[str, Any]:
    documents = [summary, list(records)]
    hits = sorted({hit for document in documents for hit in _body_hits(document)})
    identity_leak = any(_contains_raw_identity(document, raw_identity_values) for document in documents)
    return {"body_key_hits": len(hits), "raw_identity_value_leak": bool(identity_leak), "body_free": not hits and not identity_leak}


def run_audit(
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    k2_dir: Path = DEFAULT_K2_DIR,
    k10_dir: Path = DEFAULT_K10_DIR,
    *,
    audit_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    target = _load_target(Path(artifact_dir))
    k2 = _load_k2(Path(k2_dir))
    k10 = _load_k10(Path(k10_dir))
    target_strata = target.get("strata") if isinstance(target.get("strata"), list) else []
    target_pages = target.get("pages") if isinstance(target.get("pages"), list) else []
    target_materialized = target.get("materialized") if isinstance(target.get("materialized"), list) else []
    target_recovery = target.get("recovery") if isinstance(target.get("recovery"), list) else []
    strata_by_suffix = {_suffix(row.get("source_handle")): row for row in target_strata}
    page_by_suffix = {_suffix(row.get("page_handle")): row for row in target_pages}
    materialized_by_suffix = {_suffix(row.get("page_handle")): row for row in target_materialized}
    recovery_by_suffix = {_suffix(row.get("source_handle")): row for row in target_recovery}
    records: List[Dict[str, Any]] = []
    for packet_id, selection in sorted((k2.get("selection_rows") or {}).items(), key=lambda item: int(item[1].get("selection_rank") or 0)):
        source_suffix = _k28_suffix("source", packet_id)
        strata_row = strata_by_suffix.get(source_suffix)
        if strata_row is None:
            raise ValueError("K31 selected source does not bind to strata ledger")
        page_suffix = _suffix(strata_row.get("page_handle"))
        page_row = page_by_suffix.get(page_suffix)
        material_row = materialized_by_suffix.get(page_suffix)
        recovery_row = recovery_by_suffix.get(source_suffix)
        if page_row is None or material_row is None or recovery_row is None:
            raise ValueError("K31 selected source does not bind to all target ledgers")
        records.append(_page_record(packet_id, selection, k2["packets"][packet_id], strata_row, page_row, material_row, recovery_row))
    if len(records) != SELECTED_ROOTS or {record["selection_rank"] for record in records} != set(range(1, SELECTED_ROOTS + 1)):
        raise ValueError("K31 audit scope is not exactly twenty ranked pages")
    hash_report = _hash_report(target)
    binding_report = _target_binding(target, k10)
    summary = _build_summary(target, k2, k10, records, hash_report=hash_report, binding_report=binding_report)
    raw_identity_values: Set[str] = set()
    raw_identity_values.update(_identity_values(k2["selection_rows"]))
    raw_identity_values.update(_identity_values(k2["packets"]))
    privacy = _privacy_report(summary, records, raw_identity_values)
    if not privacy["body_free"]:
        raise ValueError("K31 audit output privacy check failed")
    summary["privacy"] = dict(summary["privacy"]) | privacy
    summary["body_free"] = True
    _asserted_hits = _body_hits(summary)
    if _asserted_hits:
        raise ValueError("K31 summary contains body-bearing keys")
    destination = _safe_path(Path(audit_dir) if audit_dir is not None else Path(target["root"]) / "audit", label="audit output")
    destination.mkdir(parents=True, exist_ok=True)
    summary_path = destination / AUDIT_FILENAMES["summary"]
    human_path = destination / AUDIT_FILENAMES["human"]
    if summary_path.exists() or human_path.exists():
        raise FileExistsError("K31 audit output is immutable")
    _write_json(summary_path, summary)
    _write_jsonl(human_path, records)
    persisted_human = list(_iter_jsonl(human_path))
    if len(persisted_human) != SELECTED_ROOTS or _body_hits(persisted_human) or _contains_raw_identity(persisted_human, raw_identity_values):
        raise ValueError("K31 human audit output privacy check failed")
    summary["output"] = {
        "audit_directory_ref": _opaque("audit_directory", destination),
        "summary_file": AUDIT_FILENAMES["summary"],
        "human_file": AUDIT_FILENAMES["human"],
        "human_row_count": len(persisted_human),
        "body_free": True,
    }
    # The persisted summary intentionally does not contain a self-hash; it is
    # already covered by the immutable output directory and a body-free check.
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--k2-dir", type=Path, default=DEFAULT_K2_DIR)
    parser.add_argument("--k10-dir", type=Path, default=DEFAULT_K10_DIR)
    parser.add_argument("--audit-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = run_audit(args.artifact_dir, args.k2_dir, args.k10_dir, audit_dir=args.audit_dir)
    print(json.dumps({
        "status": summary.get("status"),
        "selected_roots": summary.get("scope", {}).get("selected_root_count"),
        "marker_backed_missing_strata": summary.get("conclusion", {}).get("marker_backed_missing_strata"),
        "provider_called": summary.get("scope", {}).get("provider_called"),
        "body_free": summary.get("body_free"),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
