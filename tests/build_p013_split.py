"""Build the private P0.3 final-evaluation split.

This builder is deliberately an offline/evaluator helper.  It combines the
adjudicated ``pre_release_v3`` graph with the already-redacted pilot-v2
message inventory, assigns complete chats to ``development`` or
``frozen_test``, and writes only under the gitignored ``data/`` tree.

The split is versioned independently from the older P0.1 projection.  The
v3 observability repair intentionally leaves no gold ``same_event`` rows, so
the private split gate requires the four observable relation labels only;
the final report records the fifth label as not applicable.
"""

from __future__ import annotations

from collections import Counter
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Set, Tuple

try:  # repository-root invocation
    from tests.build_p01_split import (
        COLLECTION_FILES,
        COLLECTIONS,
        RELATION_LABELS,
        _assign_records,
        _chat_metadata,
        _freeze_digest,
        _legacy_id_to_block,
        _load_json,
        _load_jsonl,
        _source_collections,
        _write_json,
        _write_jsonl,
        _coverage,
        _public_coverage,
    )
    from tests.split_validator import (
        canonical_json,
        sha256_bytes,
        sha256_file,
        validate_split_directories,
    )
except ImportError:  # pragma: no cover - direct script invocation
    from build_p01_split import (  # type: ignore
        COLLECTION_FILES,
        COLLECTIONS,
        RELATION_LABELS,
        _assign_records,
        _chat_metadata,
        _freeze_digest,
        _legacy_id_to_block,
        _load_json,
        _load_jsonl,
        _source_collections,
        _write_json,
        _write_jsonl,
        _coverage,
        _public_coverage,
    )
    from split_validator import (  # type: ignore
        canonical_json,
        sha256_bytes,
        sha256_file,
        validate_split_directories,
    )


SOURCE_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/adjudication/pre_release_v3"
)
PILOT_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/pilot_candidates.v2.private.jsonl"
)
LEGACY_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/adjudication/pre_release_v2"
)
DEFAULT_OUTPUT_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/p013_evaluation_split_v1"
)
SPLIT_VERSION = "wechat-2026-08-25-p013-evaluation-split-v1"
LEGACY_SPLIT_VERSION = "wechat-2026-08-25-p01-evaluation-split-v2"
SEED = 2026082501
TARGET_TEST_FRACTION = 0.30
FROZEN_AT = "2026-08-27T00:00:00+08:00"

# v3 deliberately has zero rows for this label after the observability audit.
# It must not be used as a split-coverage requirement or reported as a failed
# recall metric.  The remaining four labels remain required in the frozen set.
OBSERVABLE_RELATION_LABELS = tuple(
    label for label in RELATION_LABELS if label != "same_event"
)


def _subset_hash(chat_ids: Iterable[str], seed: int) -> int:
    key = f"{seed}:" + ",".join(sorted(chat_ids))
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)


def _choose_test_chats(
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    target_fraction: float = TARGET_TEST_FRACTION,
    seed: int = SEED,
) -> Set[str]:
    """Choose a representative held-out chat subset deterministically."""

    chats = sorted(metadata)
    target_messages = round(
        sum(int(item["message_count"]) for item in metadata.values())
        * target_fraction
    )
    required_types = {"direct", "group"}
    required_buckets = {"early", "morning", "afternoon", "evening", "late"}
    required_labels = set(OBSERVABLE_RELATION_LABELS)
    candidates: List[Tuple[Tuple[int, int, int, int], Tuple[str, ...]]] = []
    # The pilot contains only 14 chats, so exhaustive subset search is cheap
    # and preserves the exact P0.1 tie-breaking semantics.
    for mask in range(1, 1 << len(chats)):
        selected = tuple(
            chats[index] for index in range(len(chats)) if mask & (1 << index)
        )
        heldout = tuple(chat for chat in chats if chat not in selected)
        selected_meta = [metadata[chat] for chat in selected]
        heldout_meta = [metadata[chat] for chat in heldout]
        selected_types = {str(item["chat_type"]) for item in selected_meta}
        heldout_types = {str(item["chat_type"]) for item in heldout_meta}
        selected_buckets = set().union(
            *(item["time_buckets"] for item in selected_meta)
        )
        selected_labels = set().union(
            *(item["relation_labels"] for item in selected_meta)
        )
        selected_messages = sum(int(item["message_count"]) for item in selected_meta)
        selected_direct = sum(
            int(item["message_count"])
            for item in selected_meta
            if item["chat_type"] == "direct"
        )
        selected_group = selected_messages - selected_direct
        missing = 0
        missing += 1000 * len(required_types - selected_types)
        missing += 1000 * len(required_types - heldout_types)
        missing += 1000 * len(required_buckets - selected_buckets)
        missing += 1000 * len(required_labels - selected_labels)
        missing += 1000 if sum(int(item["mnl_count"]) for item in selected_meta) <= 0 else 0
        missing += 1000 if sum(int(item["context_count"]) for item in selected_meta) <= 0 else 0
        objective = (
            missing,
            abs(selected_messages - target_messages),
            abs(selected_direct - selected_group),
            _subset_hash(selected, seed),
        )
        candidates.append((objective, selected))
    if not candidates:
        raise RuntimeError("no feasible chat-level P0.3 split")
    return set(min(candidates, key=lambda item: item[0])[1])


def _verify_pilot_alignment(
    source_messages: Sequence[Mapping[str, Any]],
    pilot_records: Sequence[Mapping[str, Any]],
) -> None:
    """Ensure the gold graph and pilot-v2 rows describe the same inventory."""

    gold_by_id = {str(row.get("message_id")): row for row in source_messages}
    pilot_by_id = {str(row.get("message_id")): row for row in pilot_records}
    if not gold_by_id or len(gold_by_id) != len(source_messages):
        raise ValueError("pre_release_v3 messages contain duplicate or empty IDs")
    if set(gold_by_id) != set(pilot_by_id):
        raise ValueError("pilot v2 and pre_release_v3 message IDs do not match")
    for message_id in sorted(gold_by_id):
        gold = gold_by_id[message_id]
        pilot = pilot_by_id[message_id]
        for field in ("chat_id", "chat_type", "block_id", "time_bucket"):
            gold_value = str(gold.get(field) or "")
            pilot_value = str(pilot.get(field) or "")
            if field == "block_id":
                pilot_value = str((pilot.get("pilot") or {}).get("block_id") or "")
            if gold_value != pilot_value:
                raise ValueError(
                    f"pilot v2 disagrees with pre_release_v3 for {message_id}:{field}"
                )


def _pilot_hashes(pilot_path: Path) -> Dict[str, str]:
    hashes = {"pilot_candidates.v2.private.jsonl": sha256_file(pilot_path)}
    for filename in ("pilot_manifest_patch.v2.json", "pilot_sampling_report.v2.json"):
        path = pilot_path.parent / filename
        if path.exists():
            hashes[filename] = sha256_file(path)
    return hashes


def build(
    source_root: Path = SOURCE_ROOT,
    pilot_path: Path = PILOT_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    legacy_root: Path = LEGACY_ROOT,
    *,
    seed: int = SEED,
    target_fraction: float = TARGET_TEST_FRACTION,
    validate: bool = True,
) -> Mapping[str, Any]:
    source_root = Path(source_root)
    pilot_path = Path(pilot_path)
    output_root = Path(output_root)
    legacy_root = Path(legacy_root)
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite existing private split: {output_root}")
    source_manifest = _load_json(source_root / "manifest.json")
    collections = _source_collections(source_root)
    pilot_records = _load_jsonl(pilot_path)
    _verify_pilot_alignment(collections["messages"], pilot_records)
    metadata, message_to_chat, claim_to_chat = _chat_metadata(collections)
    test_chats = _choose_test_chats(
        metadata, target_fraction=target_fraction, seed=seed
    )
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
            legacy_root,
            annotation_roots=(
                source_root.parents[1] / "annotations" / "A",
                source_root.parents[1] / "annotations" / "B",
            ),
        ),
    )
    source_hashes = {
        "gold": {
            str(path.relative_to(source_root)).replace("\\", "/"): sha256_file(path)
            for path in [source_root / "manifest.json"]
            + [source_root / filename for filename in COLLECTION_FILES.values()]
        },
        "pilot": _pilot_hashes(pilot_path),
    }

    coverage_requirements = {
        "frozen_test": {
            "chat_types": ["direct", "group"],
            "time_buckets": ["early", "morning", "afternoon", "evening", "late"],
            "relation_labels": list(OBSERVABLE_RELATION_LABELS),
            "must_not_link": True,
            "context_only": True,
            "min_messages": 1,
        },
        "development": {"chat_types": ["direct", "group"], "min_messages": 1},
    }
    public_coverage_requirements = {
        split: {
            key: value
            for key, value in requirements.items()
            if key not in {"relation_labels", "must_not_link"}
        }
        for split, requirements in coverage_requirements.items()
    }

    split_manifests: Dict[str, Dict[str, Any]] = {}
    output_root.mkdir(parents=True, exist_ok=False)
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
        split_manifest: Dict[str, Any] = {
            "artifact": "p013_evaluation_split",
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
            "source_manifest_sha256": source_hashes["gold"]["manifest.json"],
            "source_snapshot_fingerprint_hmac": source_manifest.get("source_snapshot_fingerprint_hmac"),
            "pilot_candidates_sha256": source_hashes["pilot"]["pilot_candidates.v2.private.jsonl"],
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
            "coverage_requirements": coverage_requirements[split],
            "record_counts_by_file": record_counts,
            "file_sha256": file_hashes,
            "label_counts": {"relations": split_coverage["relation_labels"]},
            "must_not_link_count": split_coverage["must_not_link_count"],
            "known_limitations": [
                "privacy gate is inherited from the private pre-release artifact",
                "the frozen test is private and must not be used for rule tuning",
                "the v3 observable-instance relation coverage excludes the absent class",
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
                    "record_count": len(_load_jsonl(path)) if filename.endswith(".jsonl") else None,
                }
            )
    assignment_payload = {
        "seed": seed,
        "target_test_fraction": target_fraction,
        "isolation_unit": "same_chat",
        "chat_count": len(chat_split),
        "frozen_chat_count": len(test_chats),
        "frozen_message_count": len(assigned["frozen_test"]["messages"]),
    }
    aggregate: Dict[str, Any] = {
        "artifact": "p013_evaluation_split",
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
        "coverage_requirements": public_coverage_requirements,
        "coverage": {
            split: _public_coverage(assigned[split])
            for split in ("development", "frozen_test")
        },
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
    aggregate["aggregate_sha256"] = sha256_bytes(
        canonical_json(
            {key: value for key, value in aggregate.items() if key != "aggregate_sha256"}
        )
    )
    _write_json(output_root / "aggregate_manifest.json", aggregate)

    if validate:
        result = validate_split_directories(
            output_root,
            coverage_requirements=coverage_requirements,
            policy={"isolation_unit": "same_chat"},
        )
        if not result.ok:
            raise RuntimeError("generated split failed validation: " + "; ".join(result.errors))
        validation_report = deepcopy(result.to_dict())
        report_summary = validation_report.get("summary")
        if isinstance(report_summary, MutableMapping):
            for split_summary in report_summary.values():
                if isinstance(split_summary, MutableMapping):
                    split_summary.pop("relation_labels", None)
                    split_summary.pop("must_not_link_count", None)
        _write_json(output_root / "validation_report.json", validation_report)
    return aggregate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build the private P0.3 evaluation split")
    parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--pilot", type=Path, default=PILOT_ROOT)
    parser.add_argument("--legacy-source", type=Path, default=LEGACY_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--test-fraction", type=float, default=TARGET_TEST_FRACTION)
    args = parser.parse_args(argv)
    aggregate = build(
        source_root=args.source,
        pilot_path=args.pilot,
        legacy_root=args.legacy_source,
        output_root=args.output,
        seed=args.seed,
        target_fraction=args.test_fraction,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "split_version": aggregate["split_version"],
                "development": aggregate["coverage"]["development"],
                "frozen_test": aggregate["coverage"]["frozen_test"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "SOURCE_ROOT",
    "PILOT_ROOT",
    "DEFAULT_OUTPUT_ROOT",
    "SPLIT_VERSION",
    "SEED",
    "build",
    "main",
]
