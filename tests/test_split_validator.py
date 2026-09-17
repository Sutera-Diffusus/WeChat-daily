"""Fully synthetic tests for the public evaluation-split validator.

The fixtures contain no private WeChat rows.  They exercise split integrity
using only pseudonymous IDs and metadata so future implementation work can run
these tests without gaining access to the frozen-test labels.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

from split_validator import (
    COLLECTIONS,
    canonical_json,
    sha256_bytes,
    validate_split_directories,
    validate_split_records,
)


def _common(record_id: str, record_type: str) -> dict:
    return {
        "record_id": record_id,
        "record_type": record_type,
        "schema_version": "synthetic_v1",
        "dataset_version": "synthetic-dataset-v1",
    }


def _fixture() -> dict:
    messages = [
        {
            **_common("M_DEV", "message"),
            "message_id": "M_DEV",
            "chat_id": "CHAT_DEV",
            "chat_type": "group",
            "block_id": "BLOCK_DEV",
            "block_order": 1,
            "dialogue_segment_id": "SEG_DEV",
            "message_role": "substantive",
            "time_bucket": "early",
            "sequence_in_chat": 1,
            "split": "development",
        },
        {
            **_common("M_CTX", "message"),
            "message_id": "M_CTX",
            "chat_id": "CHAT_DEV",
            "chat_type": "group",
            "block_id": "BLOCK_DEV",
            "block_order": 1,
            "dialogue_segment_id": "SEG_DEV",
            "message_role": "context_only",
            "time_bucket": "early",
            "sequence_in_chat": 2,
            "split": "development",
        },
        {
            **_common("M_TEST", "message"),
            "message_id": "M_TEST",
            "chat_id": "CHAT_TEST",
            "chat_type": "direct",
            "block_id": "BLOCK_TEST",
            "block_order": 1,
            "dialogue_segment_id": "SEG_TEST",
            "message_role": "substantive",
            "time_bucket": "late",
            "sequence_in_chat": 1,
            "split": "frozen_test",
        },
    ]
    mentions = [
        {
            **_common("MENTION_DEV", "mention"),
            "mention_id": "MENTION_DEV",
            "message_id": "M_DEV",
        },
        {
            **_common("MENTION_TEST", "mention"),
            "mention_id": "MENTION_TEST",
            "message_id": "M_TEST",
        },
    ]
    claims = [
        {
            **_common("CLAIM_DEV", "claim"),
            "claim_id": "CLAIM_DEV",
            "message_id": "M_DEV",
            "block_id": "BLOCK_DEV",
            "event_mention_ids": ["MENTION_DEV"],
        },
        {
            **_common("CLAIM_TEST", "claim"),
            "claim_id": "CLAIM_TEST",
            "message_id": "M_TEST",
            "block_id": "BLOCK_TEST",
            "event_mention_ids": ["MENTION_TEST"],
        },
    ]
    relations = [
        {
            **_common("REL_DEV", "relation"),
            "relation_id": "REL_DEV",
            "block_id": "BLOCK_DEV",
            "left_anchor_id": "CLAIM_DEV",
            "right_anchor_id": "CLAIM_DEV_2",
            "anchor_type": "claim",
            "label": "same_event",
            "must_not_link": False,
        },
        {
            **_common("REL_TEST", "relation"),
            "relation_id": "REL_TEST",
            "block_id": "BLOCK_TEST",
            "left_anchor_id": "CLAIM_TEST",
            "right_anchor_id": "CLAIM_TEST_2",
            "anchor_type": "claim",
            "label": "unrelated",
            "must_not_link": True,
        },
    ]
    clusters = [
        {
            **_common("EVENT_DEV", "cluster"),
            "cluster_id": "EVENT_DEV",
            "block_id": "BLOCK_DEV",
            "cluster_type": "event",
            "claim_ids": ["CLAIM_DEV"],
            "mention_ids": ["MENTION_DEV"],
            "member_message_ids": ["M_DEV"],
        },
        {
            **_common("EVENT_TEST", "cluster"),
            "cluster_id": "EVENT_TEST",
            "block_id": "BLOCK_TEST",
            "cluster_type": "event",
            "claim_ids": ["CLAIM_TEST"],
            "mention_ids": ["MENTION_TEST"],
            "member_message_ids": ["M_TEST"],
        },
    ]
    presentations = [
        {
            **_common("PRES_DEV", "presentation"),
            "presentation_id": "PRES_DEV",
            "block_id": "BLOCK_DEV",
            "source_cluster_ids": ["EVENT_DEV"],
            "source_claim_ids": ["CLAIM_DEV"],
        },
        {
            **_common("PRES_TEST", "presentation"),
            "presentation_id": "PRES_TEST",
            "block_id": "BLOCK_TEST",
            "source_cluster_ids": ["EVENT_TEST"],
            "source_claim_ids": ["CLAIM_TEST"],
        },
    ]
    empty = []
    return {
        "development": {
            "messages": messages[:2],
            "mentions": mentions[:1],
            "claims": claims[:1],
            "relations": empty[:],
            "clusters": clusters[:1],
            "presentations": presentations[:1],
            "adjudications": empty[:],
        },
        "frozen_test": {
            "messages": messages[2:],
            "mentions": mentions[1:],
            "claims": claims[1:],
            "relations": relations[1:],
            "clusters": clusters[1:],
            "presentations": presentations[1:],
            "adjudications": empty[:],
        },
    }


def _has_error(result, text: str) -> bool:
    return any(text in error for error in result.errors)


def test_synthetic_split_is_disjoint_and_reports_metadata_coverage():
    result = validate_split_records(_fixture())
    assert result.ok, result.errors
    assert result.summary["development"]["message_count"] == 2
    assert result.summary["frozen_test"]["message_count"] == 1
    assert result.summary["frozen_test"]["context_only_message_count"] == 0


def test_validator_rejects_block_claim_and_event_overlap():
    fixture = _fixture()
    # Duplicate the development block in the test split with a distinct row;
    # this should fail even when individual message IDs remain unique.
    duplicate_block = deepcopy(fixture["development"]["messages"][0])
    duplicate_block.update({"message_id": "M_DUP", "record_id": "M_DUP", "split": "frozen_test"})
    fixture["frozen_test"]["messages"].append(duplicate_block)
    result = validate_split_records(fixture)
    assert not result.ok
    assert _has_error(result, "block overlap across splits")
    assert _has_error(result, "same chat crosses splits")

    fixture = _fixture()
    duplicate_claim = deepcopy(fixture["development"]["claims"][0])
    duplicate_claim["split"] = "frozen_test"
    fixture["frozen_test"]["claims"].append(duplicate_claim)
    result = validate_split_records(fixture)
    assert not result.ok
    assert _has_error(result, "claims ID overlap across splits")

    fixture = _fixture()
    duplicate_event = deepcopy(fixture["development"]["clusters"][0])
    duplicate_event.update({"cluster_id": "EVENT_DEV", "record_id": "EVENT_DEV", "split": "frozen_test"})
    fixture["frozen_test"]["clusters"].append(duplicate_event)
    result = validate_split_records(fixture)
    assert not result.ok
    assert _has_error(result, "clusters ID overlap across splits")
    assert _has_error(result, "event overlap across splits")


def test_validator_rejects_cross_split_relation_and_adjacent_blocks():
    fixture = _fixture()
    cross = {
        **_common("REL_CROSS", "relation"),
        "relation_id": "REL_CROSS",
        "left_anchor_id": "CLAIM_DEV",
        "right_anchor_id": "CLAIM_TEST",
        "anchor_type": "claim",
        "label": "related_event",
        "must_not_link": False,
        "split": "frozen_test",
    }
    fixture["frozen_test"]["relations"].append(cross)
    result = validate_split_records(fixture)
    assert not result.ok
    assert _has_error(result, "relation crosses splits")

    fixture = _fixture()
    extra = deepcopy(fixture["development"]["messages"][0])
    extra.update(
        {
            "message_id": "M_ADJ",
            "record_id": "M_ADJ",
            "block_id": "BLOCK_ADJ",
            "block_order": 2,
            "split": "frozen_test",
        }
    )
    fixture["frozen_test"]["messages"].append(extra)
    result = validate_split_records(fixture, policy={"isolation_unit": "conversation_block"})
    assert not result.ok
    assert _has_error(result, "adjacent blocks cross splits")


def test_frozen_manifest_digest_and_access_boundary_are_enforced():
    fixture = _fixture()
    hashes = {"messages.private.jsonl": "synthetic"}
    counts = {"messages.private.jsonl": 1}
    frozen_manifest = {
        "split_version": "synthetic-split-v1",
        "dataset_version": "synthetic-dataset-v1",
        "status": "frozen_test",
        "labels_frozen": True,
        "frozen_at": "2026-08-26T00:00:00+08:00",
        "supersedes": None,
        "file_sha256": hashes,
        "record_counts_by_file": counts,
    }
    freeze_payload = {
        "dataset_version": frozen_manifest["dataset_version"],
        "file_sha256": hashes,
        "record_counts_by_file": counts,
        "split": "frozen_test",
        "split_version": frozen_manifest["split_version"],
    }
    frozen_manifest["freeze_digest"] = sha256_bytes(canonical_json(freeze_payload))
    manifests = {
        "development": {"split_version": "synthetic-split-v1", "dataset_version": "synthetic-dataset-v1"},
        "frozen_test": frozen_manifest,
    }
    aggregate = {
        "split_version": "synthetic-split-v1",
        "dataset_version": "synthetic-dataset-v1",
        "coverage_requirements": {},
        "frozen_test_labels_readable_by_implementation": False,
    }
    assert validate_split_records(fixture, manifests=manifests, aggregate_manifest=aggregate).ok

    broken = deepcopy(frozen_manifest)
    broken["freeze_digest"] = "0" * 64
    result = validate_split_records(
        fixture,
        manifests={**manifests, "frozen_test": broken},
        aggregate_manifest=aggregate,
    )
    assert not result.ok
    assert _has_error(result, "freeze_digest does not match")

    result = validate_split_records(
        fixture,
        manifests=manifests,
        aggregate_manifest={**aggregate, "frozen_test_labels_readable_by_implementation": True},
    )
    assert not result.ok
    assert _has_error(result, "exposes frozen-test labels")


def test_public_aggregate_rejects_frozen_label_distribution():
    aggregate = {
        "split_version": "synthetic-split-v1",
        "dataset_version": "synthetic-dataset-v1",
        "coverage": {"frozen_test": {"relation_labels": {"same_event": 1}}},
    }
    result = validate_split_records(_fixture(), aggregate_manifest=aggregate)
    assert not result.ok
    assert _has_error(result, "exposes label distribution")


def test_directory_validator_is_read_only_and_hash_checks(tmp_path: Path):
    fixture = _fixture()
    root = tmp_path / "split"
    for split in ("development", "frozen_test"):
        split_root = root / split
        split_root.mkdir(parents=True)
        for collection in COLLECTIONS:
            path = split_root / f"{collection}.private.jsonl"
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in fixture[split][collection]:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
        files = {"messages.private.jsonl": sha256_bytes((split_root / "messages.private.jsonl").read_bytes())}
        manifest = {
            "split_version": "synthetic-split-v1",
            "dataset_version": "synthetic-dataset-v1",
            "status": "frozen_test" if split == "frozen_test" else "development",
            "labels_frozen": split == "frozen_test",
            "frozen_at": "2026-08-26T00:00:00+08:00" if split == "frozen_test" else None,
            "supersedes": None,
            "file_sha256": files,
            "record_counts_by_file": {"messages.private.jsonl": len(fixture[split]["messages"])},
        }
        if split == "frozen_test":
            manifest["freeze_digest"] = sha256_bytes(canonical_json({
                "dataset_version": manifest["dataset_version"],
                "file_sha256": manifest["file_sha256"],
                "record_counts_by_file": manifest["record_counts_by_file"],
                "split": "frozen_test",
                "split_version": manifest["split_version"],
            }))
        (split_root / "manifest.json").write_bytes(canonical_json(manifest))
    aggregate = {
        "split_version": "synthetic-split-v1",
        "dataset_version": "synthetic-dataset-v1",
        "coverage_requirements": {},
        "frozen_test_labels_readable_by_implementation": False,
        "files": [],
    }
    (root / "aggregate_manifest.json").write_bytes(canonical_json(aggregate))
    # Empty aggregate file entries are valid for a synthetic smoke check; the
    # split validator still reads every contract file and checks its manifest.
    result = validate_split_directories(root)
    assert result.ok, result.errors
