"""Build the local P0.1 development/frozen-test projection.

This is an offline helper for the private, already-redacted pilot artifact.
It deliberately lives under ``tests`` rather than the application package and
does not import or modify production code.  The generated directory is below
``data/`` (ignored by Git) and contains no source database or raw identity.

The default source is the local ``pre_release_v2`` projection.  The builder
assigns whole chats to a split.  That stronger-than-adjacent isolation rule
means no same-chat context, adjacent block, or event evidence can cross the
evaluation boundary while still allowing an exact 70/30 message split for the
current pilot.  Tie breaking is SHA-256 based and therefore independent of
filesystem iteration order.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import argparse
from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

try:  # Running from the repository root.
    from tests.split_validator import (
        COLLECTION_FILES,
        COLLECTIONS,
        RELATION_LABELS,
        canonical_json,
        sha256_bytes,
        sha256_file,
        validate_split_directories,
    )
except ImportError:  # pragma: no cover - direct ``python tests/...`` support.
    from split_validator import (  # type: ignore
        COLLECTION_FILES,
        COLLECTIONS,
        RELATION_LABELS,
        canonical_json,
        sha256_bytes,
        sha256_file,
        validate_split_directories,
    )


SOURCE_ROOT = Path("data/private/gold_standard/2026-08-25/working/adjudication/pre_release_v2")
LEGACY_SOURCE_ROOT = Path("data/private/gold_standard/2026-08-25/working/adjudication/pre_release")
DEFAULT_OUTPUT_ROOT = Path("data/private/gold_standard/2026-08-25/working/p01_evaluation_split_v2")
LEGACY_OUTPUT_ROOT = Path("data/private/gold_standard/2026-08-25/working/p01_evaluation_split_v1")
SPLIT_VERSION = "wechat-2026-08-25-p01-evaluation-split-v2"
LEGACY_SPLIT_VERSION = "wechat-2026-08-25-p01-evaluation-split-v1"
SEED = 2026082501
TARGET_TEST_FRACTION = 0.30
FROZEN_AT = "2026-08-26T00:00:00+08:00"


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_jsonl(path: Path) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"{path}:{line_number} must be an object")
            rows.append(value)
    return rows


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_json(row))


def _row_id(collection: str, row: Mapping[str, Any]) -> str:
    field = {
        "messages": "message_id",
        "mentions": "mention_id",
        "claims": "claim_id",
        "relations": "relation_id",
        "clusters": "cluster_id",
        "presentations": "presentation_id",
        "adjudications": "adjudication_id",
    }[collection]
    return str(row.get(field) or row.get("record_id") or "")


def _ids(row: Mapping[str, Any], field: str) -> Tuple[str, ...]:
    value = row.get(field)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        return (str(value),)
    return tuple(str(item) for item in value if item is not None and str(item))


def _block_id(row: Mapping[str, Any]) -> str:
    value = row.get("block_id") or row.get("conversation_block_id")
    if value:
        return str(value)
    pilot = row.get("pilot")
    if isinstance(pilot, Mapping) and pilot.get("block_id"):
        return str(pilot["block_id"])
    return ""


def _context_only(row: Mapping[str, Any]) -> bool:
    return any(
        str(row.get(field) or "").casefold()
        in {"context", "context_only", "conversation_opener", "event_context_only"}
        for field in ("message_role", "dialogue_role", "dialogue_event_role", "event_role")
    )


def _source_collections(source_root: Path) -> Dict[str, List[Mapping[str, Any]]]:
    return {
        collection: _load_jsonl(source_root / filename)
        for collection, filename in COLLECTION_FILES.items()
    }


def _chat_metadata(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, str]]:
    messages = list(collections["messages"])
    claims = list(collections["claims"])
    message_to_chat = {
        str(row.get("message_id")): str(row.get("chat_id"))
        for row in messages
        if row.get("message_id") and row.get("chat_id")
    }
    claim_to_chat = {
        str(row.get("claim_id")): message_to_chat.get(str(row.get("message_id")), "")
        for row in claims
        if row.get("claim_id")
    }
    by_chat: Dict[str, Dict[str, Any]] = {}
    for row in messages:
        chat_id = str(row.get("chat_id") or "")
        if not chat_id:
            raise ValueError("message without chat_id")
        meta = by_chat.setdefault(
            chat_id,
            {
                "message_count": 0,
                "chat_type": str(row.get("chat_type") or ""),
                "time_buckets": set(),
                "context_count": 0,
                "block_ids": set(),
                "relation_labels": set(),
                "mnl_count": 0,
            },
        )
        if meta["chat_type"] != str(row.get("chat_type") or ""):
            raise ValueError(f"chat_type changes inside {chat_id}")
        meta["message_count"] += 1
        if row.get("time_bucket"):
            meta["time_buckets"].add(str(row["time_bucket"]))
        if _context_only(row):
            meta["context_count"] += 1
        if _block_id(row):
            meta["block_ids"].add(_block_id(row))
    for row in collections["relations"]:
        left = str(row.get("left_anchor_id") or "")
        right = str(row.get("right_anchor_id") or "")
        chat_ids = {claim_to_chat.get(left, ""), claim_to_chat.get(right, "")}
        chat_ids.discard("")
        if not chat_ids:
            block = _block_id(row)
            chat_ids = {
                chat_id
                for chat_id, meta in by_chat.items()
                if block and block in meta["block_ids"]
            }
        if len(chat_ids) != 1:
            raise ValueError(f"relation {_row_id('relations', row)} does not resolve to one chat")
        chat_id = next(iter(chat_ids))
        by_chat[chat_id]["relation_labels"].add(str(row.get("label") or ""))
        by_chat[chat_id]["mnl_count"] += int(row.get("must_not_link") is True)
    return by_chat, message_to_chat, claim_to_chat


def _subset_hash(chat_ids: Iterable[str], seed: int) -> int:
    key = f"{seed}:" + ",".join(sorted(chat_ids))
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)


def _choose_test_chats(
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    target_fraction: float = TARGET_TEST_FRACTION,
    seed: int = SEED,
) -> Set[str]:
    chats = sorted(metadata)
    target_messages = round(sum(int(item["message_count"]) for item in metadata.values()) * target_fraction)
    required_types = {"direct", "group"}
    required_buckets = {"early", "morning", "afternoon", "evening", "late"}
    required_labels = set(RELATION_LABELS)
    candidates: List[Tuple[Tuple[int, int, int, int], Tuple[str, ...]]] = []
    for mask in range(1, 1 << len(chats)):
        selected = tuple(chats[index] for index in range(len(chats)) if mask & (1 << index))
        heldout = tuple(chat for chat in chats if chat not in selected)
        selected_meta = [metadata[chat] for chat in selected]
        heldout_meta = [metadata[chat] for chat in heldout]
        selected_types = {str(item["chat_type"]) for item in selected_meta}
        heldout_types = {str(item["chat_type"]) for item in heldout_meta}
        selected_buckets = set().union(*(item["time_buckets"] for item in selected_meta))
        selected_labels = set().union(*(item["relation_labels"] for item in selected_meta))
        selected_messages = sum(int(item["message_count"]) for item in selected_meta)
        selected_direct = sum(
            int(item["message_count"]) for item in selected_meta if item["chat_type"] == "direct"
        )
        selected_group = selected_messages - selected_direct
        missing = 0
        missing += 1000 * len(required_types - selected_types)
        missing += 1000 * len(required_types - heldout_types)
        missing += 1000 * len(required_buckets - selected_buckets)
        missing += 1000 * len(required_labels - selected_labels)
        missing += 1000 if sum(int(item["mnl_count"]) for item in selected_meta) <= 0 else 0
        missing += 1000 if sum(int(item["context_count"]) for item in selected_meta) <= 0 else 0
        # Exact message target comes before type balance; type balance then
        # prefers a stable, representative set among otherwise equal choices.
        objective = (
            missing,
            abs(selected_messages - target_messages),
            abs(selected_direct - selected_group),
            _subset_hash(selected, seed),
        )
        candidates.append((objective, selected))
    if not candidates:
        raise RuntimeError("no feasible chat-level split satisfies coverage requirements")
    return set(min(candidates, key=lambda item: item[0])[1])


def _legacy_id_to_block(legacy_root: Path, annotation_roots: Sequence[Path] = ()) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    roots = [legacy_root] + [Path(root) for root in annotation_roots]
    for root in roots:
        if not root.exists():
            continue
        paths = [root / filename for filename in COLLECTION_FILES.values()]
        # A annotation stream and B's combined stream carry several typed
        # rows in one file.  Their IDs are needed only to place adjudication
        # audit records, never to expose annotation content.
        paths.extend(
            path
            for path in root.glob("*.private.jsonl")
            if path not in paths
        )
        for path in paths:
            if not path.exists():
                continue
            for row in _load_jsonl(path):
                block = _block_id(row)
                if not block:
                    continue
                candidate_ids = {
                    str(row.get(field))
                    for field in (
                        "record_id",
                        "message_id",
                        "mention_id",
                        "claim_id",
                        "relation_id",
                        "cluster_id",
                        "presentation_id",
                        "adjudication_id",
                    )
                    if row.get(field)
                }
                for identifier in candidate_ids:
                    mapping[identifier] = block
    return mapping


def _assign_records(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
    chat_split: Mapping[str, str],
    message_to_chat: Mapping[str, str],
    claim_to_chat: Mapping[str, str],
    legacy_id_to_block: Mapping[str, str],
) -> Dict[str, Dict[str, List[Mapping[str, Any]]]]:
    by_id: Dict[str, Dict[str, Mapping[str, Any]]] = {
        collection: {
            _row_id(collection, row): row
            for row in collections[collection]
            if _row_id(collection, row)
        }
        for collection in COLLECTIONS
    }
    block_to_chat: Dict[str, str] = {}
    for row in collections["messages"]:
        block = _block_id(row)
        chat = str(row.get("chat_id") or "")
        if block and chat:
            prior = block_to_chat.setdefault(block, chat)
            if prior != chat:
                raise ValueError(f"block {block} appears in multiple chats")

    def split_for_chat_ids(chat_ids: Iterable[str], owner: str) -> str:
        found = {chat_split.get(chat_id, "") for chat_id in chat_ids if chat_id}
        found.discard("")
        if len(found) != 1:
            raise ValueError(f"{owner} resolves to multiple or unknown chats")
        return next(iter(found))

    def row_split(collection: str, row: Mapping[str, Any]) -> str:
        owner = f"{collection} {_row_id(collection, row)}"
        chat_ids: Set[str] = set()
        block = _block_id(row)
        if block and block in block_to_chat:
            chat_ids.add(block_to_chat[block])
        if collection == "messages":
            chat_ids.add(str(row.get("chat_id") or ""))
        elif collection == "mentions":
            chat_ids.add(message_to_chat.get(str(row.get("message_id") or ""), ""))
        elif collection == "claims":
            chat_ids.add(message_to_chat.get(str(row.get("message_id") or ""), ""))
        elif collection == "relations":
            chat_ids.update(
                claim_to_chat.get(str(value), "")
                for value in (row.get("left_anchor_id"), row.get("right_anchor_id"))
            )
            chat_ids.update(message_to_chat.get(value, "") for value in _ids(row, "evidence_message_ids"))
        elif collection == "clusters":
            chat_ids.update(
                claim_to_chat.get(value, "") for value in _ids(row, "claim_ids")
            )
            chat_ids.update(
                message_to_chat.get(value, "") for value in _ids(row, "member_message_ids")
            )
        elif collection == "presentations":
            for value in _ids(row, "source_claim_ids"):
                chat_ids.add(claim_to_chat.get(value, ""))
            for value in _ids(row, "source_cluster_ids"):
                cluster = by_id["clusters"].get(value)
                if cluster is not None:
                    cluster_block = _block_id(cluster)
                    if cluster_block in block_to_chat:
                        chat_ids.add(block_to_chat[cluster_block])
                    for message_id in _ids(cluster, "member_message_ids"):
                        chat_ids.add(message_to_chat.get(message_id, ""))
        elif collection == "adjudications":
            ids = list(_ids(row, "evidence_ids")) + list(_ids(row, "anchor_id"))
            for value in ids:
                chat_ids.add(message_to_chat.get(value, ""))
                chat_ids.add(claim_to_chat.get(value, ""))
                target_block = legacy_id_to_block.get(value, "")
                if target_block:
                    chat_ids.add(block_to_chat.get(target_block, ""))
                target_block = legacy_id_to_block.get(value, "")
                if target_block:
                    chat_ids.add(block_to_chat.get(target_block, ""))
            # ``record_id`` is useful for a legacy presentation adjudication
            # only through the old presentation->block crosswalk.
            own_id = _row_id(collection, row)
            target_block = legacy_id_to_block.get(own_id, "")
            if target_block:
                chat_ids.add(block_to_chat.get(target_block, ""))
        chat_ids.discard("")
        # A block is the reliable fallback for sparse derived rows.  For
        # adjudications the legacy crosswalk handles the 60 old presentation
        # IDs that are absent from the v2 graph.
        if not chat_ids and block and block in block_to_chat:
            chat_ids.add(block_to_chat[block])
        return split_for_chat_ids(chat_ids, owner)

    output: Dict[str, Dict[str, List[Mapping[str, Any]]]] = {
        split: {collection: [] for collection in COLLECTIONS}
        for split in ("development", "frozen_test")
    }
    for collection in COLLECTIONS:
        for row in collections[collection]:
            split = row_split(collection, row)
            updated = deepcopy(dict(row))
            updated["split"] = split
            updated["evaluation_split_version"] = SPLIT_VERSION
            if split == "frozen_test":
                updated["annotation_status"] = "frozen"
            output[split][collection].append(updated)
    for split in output:
        for collection in COLLECTIONS:
            output[split][collection].sort(key=lambda row: _row_id(collection, row))
    return output


def _source_hashes(source_root: Path) -> Dict[str, str]:
    paths = [source_root / "manifest.json"] + [source_root / filename for filename in COLLECTION_FILES.values()]
    return {str(path.relative_to(source_root)).replace("\\", "/"): sha256_file(path) for path in paths}


def _freeze_digest(manifest: Mapping[str, Any]) -> str:
    payload = {
        "dataset_version": manifest.get("dataset_version"),
        "file_sha256": manifest.get("file_sha256") or {},
        "record_counts_by_file": manifest.get("record_counts_by_file") or {},
        "split": "frozen_test",
        "split_version": manifest.get("split_version"),
    }
    return sha256_bytes(canonical_json(payload))


def _coverage(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, Any]:
    messages = list(rows["messages"])
    relations = list(rows["relations"])
    clusters = list(rows["clusters"])
    return {
        "message_count": len(messages),
        "block_count": len({_block_id(row) for collection in COLLECTIONS for row in rows[collection] if _block_id(row)}),
        "chat_count": len({str(row.get("chat_id")) for row in messages if row.get("chat_id")}),
        "chat_type": dict(sorted(Counter(str(row.get("chat_type")) for row in messages if row.get("chat_type")).items())),
        "time_bucket": dict(sorted(Counter(str(row.get("time_bucket")) for row in messages if row.get("time_bucket")).items())),
        "relation_labels": dict(sorted(Counter(str(row.get("label")) for row in relations if row.get("label")).items())),
        "must_not_link_count": sum(1 for row in relations if row.get("must_not_link") is True),
        "context_only_message_count": sum(1 for row in messages if _context_only(row)),
        "event_count": sum(1 for row in clusters if str(row.get("cluster_type")) == "event"),
        "mention_count": len(rows["mentions"]),
        "claim_count": len(rows["claims"]),
        "relation_count": len(rows["relations"]),
        "cluster_count": len(rows["clusters"]),
        "presentation_count": len(rows["presentations"]),
        "adjudication_count": len(rows["adjudications"]),
    }


def _public_coverage(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, Any]:
    """Return aggregate coverage safe for implementation/public use.

    Split directories and their manifests are private evaluator inputs and may
    retain detailed label evidence for offline QA.  The aggregate manifest is
    a public metadata surface, so it must never disclose relation labels,
    label histograms, or label-derived counts.  Keep only scale and unlabeled
    sampling/coverage dimensions here.
    """

    coverage = _coverage(rows)
    for field in ("relation_labels", "must_not_link_count"):
        coverage.pop(field, None)
    return coverage


def _mark_previous_version_superseded(
    previous_root: Path,
    *,
    superseded_by: str,
) -> None:
    """Mark an existing prior aggregate without touching its split payloads.

    The frozen-test files are immutable evaluator inputs.  Only the old
    top-level aggregate metadata is updated, preserving the v1 artifact for
    audit/reproducibility while making the active lineage unambiguous.
    """

    aggregate_path = Path(previous_root) / "aggregate_manifest.json"
    if not aggregate_path.is_file():
        return
    try:
        aggregate = dict(_load_json(aggregate_path))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    aggregate["status"] = "superseded"
    aggregate["superseded_by"] = superseded_by
    aggregate["superseded_at"] = FROZEN_AT
    aggregate["aggregate_sha256"] = ""
    aggregate["aggregate_sha256"] = sha256_bytes(
        canonical_json({key: value for key, value in aggregate.items() if key != "aggregate_sha256"})
    )
    _write_json(aggregate_path, aggregate)


def build(
    source_root: Path = SOURCE_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    legacy_root: Path = LEGACY_SOURCE_ROOT,
    *,
    seed: int = SEED,
    target_fraction: float = TARGET_TEST_FRACTION,
    validate: bool = True,
) -> Mapping[str, Any]:
    source_root = Path(source_root)
    output_root = Path(output_root)
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite existing private split: {output_root}")
    source_manifest = _load_json(source_root / "manifest.json")
    collections = _source_collections(source_root)
    metadata, message_to_chat, claim_to_chat = _chat_metadata(collections)
    test_chats = _choose_test_chats(metadata, target_fraction=target_fraction, seed=seed)
    chat_split = {
        chat_id: "frozen_test" if chat_id in test_chats else "development"
        for chat_id in sorted(metadata)
    }
    assigned = _assign_records(
        collections,
        chat_split,
        message_to_chat,
        claim_to_chat,
        _legacy_id_to_block(
            Path(legacy_root),
            annotation_roots=(
                Path(source_root).parents[1] / "annotations" / "A",
                Path(source_root).parents[1] / "annotations" / "B",
            ),
        ),
    )
    source_hashes = _source_hashes(source_root)

    coverage_requirements = {
        "frozen_test": {
            "chat_types": ["direct", "group"],
            "time_buckets": ["early", "morning", "afternoon", "evening", "late"],
            "relation_labels": list(RELATION_LABELS),
            "must_not_link": True,
            "context_only": True,
            "min_messages": 1,
        },
        "development": {"chat_types": ["direct", "group"], "min_messages": 1},
    }
    # The detailed requirement set is private and is used only by the offline
    # split gate.  Never copy relation-label or MNL requirements to a public
    # manifest consumed by an implementation.
    public_coverage_requirements = {
        split: {
            key: value
            for key, value in required.items()
            if key not in {"relation_labels", "must_not_link"}
        }
        for split, required in coverage_requirements.items()
    }
    split_manifests: Dict[str, Dict[str, Any]] = {}
    for split in ("development", "frozen_test"):
        split_root = output_root / split
        split_root.mkdir(parents=True, exist_ok=False)
        file_hashes: Dict[str, str] = {}
        record_counts: Dict[str, int] = {}
        for collection, filename in COLLECTION_FILES.items():
            path = split_root / filename
            _write_jsonl(path, assigned[split][collection])
            file_hashes[filename] = sha256_file(path)
            record_counts[filename] = len(assigned[split][collection])
        split_coverage = _coverage(assigned[split])
        split_manifest = {
            "artifact": "p01_evaluation_split",
            "split": split,
            "split_version": SPLIT_VERSION,
            "dataset_version": str(source_manifest.get("dataset_version") or ""),
            "source_dataset_version": str(source_manifest.get("dataset_version") or ""),
            "schema_version": str(source_manifest.get("schema_version") or "gold_semantic_v1"),
            "annotation_guide_version": str(source_manifest.get("annotation_guide_version") or ""),
            "status": "frozen_test" if split == "frozen_test" else "development",
            "labels_frozen": split == "frozen_test",
            "frozen_at": FROZEN_AT if split == "frozen_test" else None,
            "supersedes": None,
            "external_sharing_allowed": False,
            "release_eligible": False,
            "privacy_scan_status": str(source_manifest.get("privacy_scan_status") or "inherited_private_only"),
            "source_manifest_sha256": source_hashes["manifest.json"],
            "source_snapshot_fingerprint_hmac": source_manifest.get("source_snapshot_fingerprint_hmac"),
            "sampling_seed": seed,
            "target_test_fraction": target_fraction,
            "isolation_unit": "same_chat",
            "split_policy": {
                "state": "frozen_test" if split == "frozen_test" else "development",
                "unit": "conversation_chat",
                "same_chat_cross_split": False,
                "same_dialogue_segment_cross_split": False,
                "adjacent_block_cross_split": False,
                "labels_readable_by_implementation": split != "frozen_test",
            },
            "coverage": split_coverage,
            "coverage_counts": split_coverage,
            "record_counts_by_file": record_counts,
            "file_sha256": file_hashes,
            "label_counts": {"relations": split_coverage["relation_labels"]},
            "must_not_link_count": split_coverage["must_not_link_count"],
            "known_limitations": [
                "privacy gate is inherited from the private pre-release artifact",
                "the frozen test is private and must not be used for rule tuning",
            ],
        }
        if split == "frozen_test":
            split_manifest["freeze_digest"] = _freeze_digest(split_manifest)
        _write_json(split_root / "manifest.json", split_manifest)
        split_manifests[split] = split_manifest

    files: List[Dict[str, Any]] = []
    for split in ("development", "frozen_test"):
        split_root = output_root / split
        for filename in ("manifest.json",) + tuple(COLLECTION_FILES.values()):
            path = split_root / filename
            files.append(
                {
                    "path": f"{split}/{filename}",
                    "sha256": sha256_file(path),
                    "record_count": (
                        len(_load_jsonl(path))
                        if filename.endswith(".jsonl")
                        else None
                    ),
                }
            )
    assignment_payload = {
        "seed": seed,
        "target_test_fraction": target_fraction,
        "isolation_unit": "same_chat",
        "chat_assignments": sorted(chat_split.items()),
    }
    aggregate = {
        "artifact": "p01_evaluation_split",
        "status": "active",
        "split_version": SPLIT_VERSION,
        "supersedes": LEGACY_SPLIT_VERSION,
        "dataset_version": str(source_manifest.get("dataset_version") or ""),
        "source_dataset_version": str(source_manifest.get("dataset_version") or ""),
        "schema_version": str(source_manifest.get("schema_version") or "gold_semantic_v1"),
        "sampling_seed": seed,
        "target_test_fraction": target_fraction,
        "isolation_unit": "same_chat",
        "assignment_method": "exhaustive_chat_subset_with_sha256_tiebreak",
        "assignment_hash": sha256_bytes(canonical_json(assignment_payload)),
        "source_files": source_hashes,
        # Public aggregate metadata intentionally contains no relation label
        # values or label distributions.  Private split manifests retain the
        # detailed evaluator coverage and are not an implementation input.
        "coverage_requirements": public_coverage_requirements,
        "coverage": {split: _public_coverage(assigned[split]) for split in ("development", "frozen_test")},
        "splits": {
            split: {
                "path": split,
                "status": split_manifests[split]["status"],
                "labels_frozen": split_manifests[split]["labels_frozen"],
                "frozen_at": split_manifests[split]["frozen_at"],
            }
            for split in ("development", "frozen_test")
        },
        "files": files,
        "overlap_checks": {
            "block_overlap": [],
            "claim_overlap": [],
            "event_overlap": [],
            "relation_overlap": [],
            "message_overlap": [],
        },
        "frozen_test_label_access": {
            "labels_frozen": True,
            "labels_readable_by_implementation": False,
            "implementation_default_split": "development",
            "public_manifest_contains_labels": False,
            "storage": "private_gitignored_directory",
        },
        "aggregate_sha256": "",
    }
    aggregate["aggregate_sha256"] = sha256_bytes(canonical_json({key: value for key, value in aggregate.items() if key != "aggregate_sha256"}))
    _write_json(output_root / "aggregate_manifest.json", aggregate)

    if validate:
        result = validate_split_directories(
            output_root,
            coverage_requirements=coverage_requirements,
            policy={"isolation_unit": "same_chat"},
        )
        if not result.ok:
            raise RuntimeError("generated split failed validation: " + "; ".join(result.errors))
        validation_path = output_root / "validation_report.json"
        # The report sits beside the public aggregate manifest.  Keep its
        # metadata-only summary free of relation histograms as well; the
        # private split manifests remain the sole detailed evaluator surface.
        validation_report = deepcopy(result.to_dict())
        report_summary = validation_report.get("summary")
        if isinstance(report_summary, MutableMapping):
            for split_summary in report_summary.values():
                if isinstance(split_summary, MutableMapping):
                    split_summary.pop("relation_labels", None)
                    split_summary.pop("must_not_link_count", None)
        _write_json(validation_path, validation_report)
    # Keep the previous v1 artifact in place for auditability, but make the
    # version lineage explicit after the v2 artifact has been written.
    previous_root = output_root.parent / LEGACY_OUTPUT_ROOT.name
    if previous_root.resolve() != output_root.resolve():
        _mark_previous_version_superseded(previous_root, superseded_by=SPLIT_VERSION)
    return aggregate


def main() -> int:
    parser = argparse.ArgumentParser(description="build the private P0.1 evaluation split v2")
    parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--legacy-source", type=Path, default=LEGACY_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--test-fraction", type=float, default=TARGET_TEST_FRACTION)
    args = parser.parse_args()
    aggregate = build(
        source_root=args.source,
        legacy_root=args.legacy_source,
        output_root=args.output,
        seed=args.seed,
        target_fraction=args.test_fraction,
    )
    coverage = aggregate["coverage"]
    print(
        json.dumps(
            {
                "output": str(args.output),
                "split_version": aggregate["split_version"],
                "development": coverage["development"],
                "frozen_test": coverage["frozen_test"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
