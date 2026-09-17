"""Build the private P0.3 final-evaluation split from ``pre_release_v4``.

This thin versioned wrapper reuses the proven whole-chat allocator from the
P0.3 split builder while adapting only the v4 typed adjudication references
needed to resolve their owning chat.  The source gold rows are never rewritten;
the generated projection receives only split metadata.  All output remains in
the gitignored private ``data/`` tree.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping, Sequence

# ``python tests/build_p014_split.py`` puts ``tests/`` (rather than the
# repository root) first on sys.path.  Add the root so the versioned helper can
# import the existing test-only builders as a package.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests import build_p013_split as _base
from tests import build_p01_split as _legacy
from tests.split_validator import (
    COLLECTION_FILES,
    COLLECTIONS,
    canonical_json,
    sha256_bytes,
    sha256_file,
    validate_split_directories,
)

try:
    from wechat_bridge.semantic_gold import validate_contract_directory
except ModuleNotFoundError:  # pragma: no cover - direct script convenience
    import sys

    _SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))
    from wechat_bridge.semantic_gold import validate_contract_directory


SOURCE_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/adjudication/pre_release_v4"
)
PILOT_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/pilot_candidates.v2.private.jsonl"
)
LEGACY_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/adjudication/pre_release_v2"
)
DEFAULT_OUTPUT_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/p014_evaluation_split_v1"
)
SPLIT_VERSION = "wechat-2026-08-25-p014-evaluation-split-v1"
SUPERSEDES_SPLIT_VERSION = "wechat-2026-08-25-p013-evaluation-split-v1"
SEED = _base.SEED
TARGET_TEST_FRACTION = _base.TARGET_TEST_FRACTION
FROZEN_AT = "2026-08-27T00:00:00+08:00"

OBSERVABLE_RELATION_LABELS = tuple(_base.OBSERVABLE_RELATION_LABELS)


def _typed_id(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(
            value.get("id")
            or value.get("typed_id")
            or value.get("value")
            or ""
        ).strip()
    return str(value or "").strip()


def _normalised_assignment_collections(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, List[Mapping[str, Any]]]:
    """Make v4 typed adjudication refs readable to the legacy allocator.

    This copy exists only while assigning rows.  The original v4 rows are
    restored before they are written to the split projection.
    """

    output: Dict[str, List[Mapping[str, Any]]] = {
        name: [dict(row) for row in rows]
        for name, rows in collections.items()
    }
    converted: List[Mapping[str, Any]] = []
    for source_row in output.get("adjudications", []):
        row = dict(source_row)
        anchor_id = _typed_id(row.get("anchor_id"))
        if anchor_id:
            row["anchor_id"] = anchor_id
        evidence_refs = row.get("evidence_refs")
        if isinstance(evidence_refs, list):
            row["evidence_ids"] = [
                identifier
                for identifier in (_typed_id(item) for item in evidence_refs)
                if identifier
            ]
        converted.append(row)
    output["adjudications"] = converted
    return output


def _assign_records_v4(
    collections: Mapping[str, Sequence[Mapping[str, Any]]],
    chat_split: Mapping[str, str],
    message_to_chat: Mapping[str, str],
    claim_to_chat: Mapping[str, str],
    legacy_id_to_block: Mapping[str, str],
) -> Dict[str, Dict[str, List[Mapping[str, Any]]]]:
    """Allocate v4 rows and restore their exact source shape plus metadata."""

    original_by_id: Dict[str, Dict[str, Mapping[str, Any]]] = {
        collection: {
            _legacy._row_id(collection, row): row
            for row in rows
            if _legacy._row_id(collection, row)
        }
        for collection, rows in collections.items()
    }
    assigned = _legacy._assign_records(
        _normalised_assignment_collections(collections),
        chat_split,
        message_to_chat,
        claim_to_chat,
        legacy_id_to_block,
    )
    output: Dict[str, Dict[str, List[Mapping[str, Any]]]] = {
        split: {collection: [] for collection in COLLECTIONS}
        for split in ("development", "frozen_test")
    }
    for split, split_rows in assigned.items():
        for collection, rows in split_rows.items():
            for assigned_row in rows:
                row_id = _legacy._row_id(collection, assigned_row)
                source_row = original_by_id[collection].get(row_id, assigned_row)
                updated = deepcopy(dict(source_row))
                updated["split"] = split
                updated["evaluation_split_version"] = SPLIT_VERSION
                if split == "frozen_test":
                    updated["annotation_status"] = "frozen"
                output[split][collection].append(updated)
            output[split][collection].sort(
                key=lambda row: _legacy._row_id(collection, row)
            )
    return output


def _coverage_requirements() -> Dict[str, Dict[str, Any]]:
    return {
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


def _refresh_aggregate_manifest(
    output_root: Path,
    aggregate: Mapping[str, Any],
) -> Dict[str, Any]:
    """Rewrite lineage/version metadata after the shared allocator finishes."""

    updated = dict(aggregate)
    updated["artifact"] = "p014_evaluation_split"
    updated["split_version"] = SPLIT_VERSION
    updated["supersedes"] = SUPERSEDES_SPLIT_VERSION
    updated["source_gold_artifact"] = "pre_release_v4"
    updated["assignment_method"] = "exhaustive_chat_subset_with_sha256_tiebreak_whole_chat"
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
                        len(_base._load_jsonl(path))
                        if filename.endswith(".jsonl")
                        else None
                    ),
                }
            )
    updated["files"] = files
    updated["aggregate_sha256"] = ""
    updated["aggregate_sha256"] = sha256_bytes(
        canonical_json(
            {
                key: value
                for key, value in updated.items()
                if key != "aggregate_sha256"
            }
        )
    )
    (output_root / "aggregate_manifest.json").write_bytes(canonical_json(updated))
    return updated


def _refresh_split_manifests(output_root: Path) -> None:
    for split in ("development", "frozen_test"):
        path = output_root / split / "manifest.json"
        manifest = dict(_base._load_json(path))
        manifest["artifact"] = "p014_evaluation_split"
        manifest["split_version"] = SPLIT_VERSION
        manifest["known_limitations"] = [
            "privacy gate is inherited from the private pre-release artifact",
            "the frozen test is private and must not be used for rule tuning",
            "pre_release_v4 has zero observable gold same_event rows; that class is N/A",
        ]
        if split == "frozen_test":
            manifest["freeze_digest"] = _base._freeze_digest(manifest)
        path.write_bytes(canonical_json(manifest))


def _write_validation_report(output_root: Path) -> Mapping[str, Any]:
    result = validate_split_directories(
        output_root,
        coverage_requirements=_coverage_requirements(),
        policy={"isolation_unit": "same_chat"},
    )
    if not result.ok:
        raise RuntimeError("generated p014 split failed validation: " + "; ".join(result.errors))
    report = result.to_dict()
    summary = report.get("summary")
    if isinstance(summary, dict):
        for split_summary in summary.values():
            if isinstance(split_summary, dict):
                split_summary.pop("relation_labels", None)
                split_summary.pop("must_not_link_count", None)
    path = output_root / "validation_report.json"
    path.write_bytes(canonical_json(report))
    return report


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

    contract = validate_contract_directory(source_root)
    if not contract.ok:
        raise RuntimeError("pre_release_v4 contract validation failed: " + "; ".join(contract.errors))

    previous = {
        "assign": _base._assign_records,
        "split_version": _base.SPLIT_VERSION,
    }
    _base._assign_records = _assign_records_v4
    _base.SPLIT_VERSION = SPLIT_VERSION
    try:
        aggregate = _base.build(
            source_root=source_root,
            pilot_path=pilot_path,
            output_root=output_root,
            legacy_root=legacy_root,
            seed=seed,
            target_fraction=target_fraction,
            validate=False,
        )
    finally:
        _base._assign_records = previous["assign"]
        _base.SPLIT_VERSION = previous["split_version"]

    _refresh_split_manifests(output_root)
    aggregate = _refresh_aggregate_manifest(output_root, aggregate)
    if validate:
        validation = _write_validation_report(output_root)
        aggregate = dict(aggregate)
        aggregate["strict_contract"] = {
            "validator": "wechat_bridge.semantic_gold.validate_contract_directory",
            "source": "pre_release_v4",
            "passed": True,
            "error_count": 0,
        }
        aggregate["split_graph"] = {
            "validator": "tests.split_validator.validate_split_directories",
            "passed": bool(validation.get("ok")),
            "error_count": len(validation.get("errors") or []),
        }
        aggregate["aggregate_sha256"] = ""
        aggregate["aggregate_sha256"] = sha256_bytes(
            canonical_json(
                {
                    key: value
                    for key, value in aggregate.items()
                    if key != "aggregate_sha256"
                }
            )
        )
        (output_root / "aggregate_manifest.json").write_bytes(canonical_json(aggregate))
    return aggregate


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="build the private P0.3 p014 evaluation split")
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
    "LEGACY_ROOT",
    "DEFAULT_OUTPUT_ROOT",
    "SPLIT_VERSION",
    "build",
    "main",
]
