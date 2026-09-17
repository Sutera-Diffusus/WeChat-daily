"""Synthetic P0.1.1 governance counterexamples.

These tests exercise the fail-closed boundary/lineage gates without opening
or depending on any private/frozen split payload.
"""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from tests.build_p01_split import (
    DEFAULT_OUTPUT_ROOT,
    LEGACY_OUTPUT_ROOT,
    LEGACY_SPLIT_VERSION,
    SPLIT_VERSION,
    _mark_previous_version_superseded,
    _public_coverage,
)
from tests.test_semantic_gold import _synthetic_contract_and_predictions
from tests.test_semantic_pipeline_p01 import _message
from wechat_bridge.semantic_gold import validate_contract_dataset
from wechat_bridge.semantic_pipeline import (
    generate_candidate_pairs_p01,
    run_semantic_pipeline,
    run_semantic_pipeline_p01,
    validate_p01_invariants,
)


def _valid_p01_result():
    return run_semantic_pipeline_p01(
        [
            _message("governance-one", "GPT 重置了。", 0),
            _message("governance-two", "Codex 重置了。", 30),
        ],
        analysis_run_id="governance-run",
    )


def test_p011_rejects_mixed_run_p0_objects_and_external_provenance():
    result = _valid_p01_result()
    assert validate_p01_invariants(result)["passed"] is True

    mixed_run_claim = replace(result.claims[0], analysis_run_id="other-run")
    mixed_run_result = replace(
        result,
        claims=(mixed_run_claim,) + result.claims[1:],
    )
    report = validate_p01_invariants(mixed_run_result)
    assert report["passed"] is False
    assert any("analysis_run_id mismatch" in error for error in report["errors"])
    with pytest.raises(ValueError, match="analysis_run_id mismatch"):
        generate_candidate_pairs_p01(
            mixed_run_result.claims,
            result.analysis_run_id,
            result.created_at,
        )

    # P0 DTOs have the same wire shape but must not enter a P0.1 stage by
    # merely being re-labelled by a caller.
    p0_result = run_semantic_pipeline(
        [
            {
                "message_id": "p0-message",
                "chat_id": "p0-chat",
                "sender_id": "p0-sender",
                "sender_name": "合成人员",
                "content": "GPT 重置了。",
                "timestamp": "2026-08-26T09:00:00+00:00",
                "is_group": True,
                "is_self": False,
            }
        ],
        analysis_run_id="p0-run",
    )
    with pytest.raises(ValueError, match="not a P0.1 pipeline object"):
        generate_candidate_pairs_p01(
            p0_result.claims,
            p0_result.analysis_run_id,
            p0_result.created_at,
        )

    external_provenance = replace(
        result.claims[0],
        provenance=replace(
            result.claims[0].provenance,
            input_ids=result.claims[0].provenance.input_ids + ("external-input",),
        ),
    )
    external_result = replace(
        result,
        claims=(external_provenance,) + result.claims[1:],
    )
    report = validate_p01_invariants(external_result)
    assert report["passed"] is False
    assert any("external input_id" in error for error in report["errors"])

    malformed_graph = replace(
        result,
        claims=None,
        candidate_pairs=(),
        pair_decisions=(),
        events=(),
        topic_families=(),
        trends=(),
        presentations=(),
    )
    malformed_report = validate_p01_invariants(malformed_graph)
    assert malformed_report["passed"] is False
    assert any("claims must be a sequence" in error for error in malformed_report["errors"])


def test_p011_rejects_unscoped_or_mismatched_boundary_provenance():
    result = _valid_p01_result()
    claim = result.claims[0]
    wrong_segment = "segment:account=default|chat=other-chat|id=foreign"
    invalid_claim = replace(claim, dialogue_segment_id=wrong_segment)
    invalid_result = replace(result, claims=(invalid_claim,) + result.claims[1:])
    report = validate_p01_invariants(invalid_result)
    assert report["passed"] is False
    assert any("boundary" in error for error in report["errors"])


def test_gold_typed_event_seed_refs_are_explicit_and_plain_ambiguity_fails():
    dataset, _ = _synthetic_contract_and_predictions()
    cluster_id = dataset["clusters"][0]["cluster_id"]
    base_adjudication = {
        "schema_version": dataset["manifest"]["schema_version"],
        "dataset_version": dataset["manifest"]["dataset_version"],
        "record_id": "ADJ_EVENT_SEED",
        "annotation_status": "adjudicated",
        "provenance": deepcopy(dataset["messages"][0]["provenance"]),
        "adjudication_id": "ADJ_EVENT_SEED",
        "record_type": "cluster",
        "anchor_id": "cluster:%s" % cluster_id,
        "evidence_ids": ["event_seed:%s" % cluster_id],
    }
    explicit = deepcopy(dataset)
    explicit["adjudications"] = [base_adjudication]
    explicit_result = validate_contract_dataset(explicit)
    assert explicit_result.ok, explicit_result.errors

    ambiguous = deepcopy(dataset)
    ambiguous_adjudication = deepcopy(base_adjudication)
    ambiguous_adjudication["record_id"] = "ADJ_EVENT_SEED_AMBIGUOUS"
    ambiguous_adjudication["adjudication_id"] = "ADJ_EVENT_SEED_AMBIGUOUS"
    ambiguous_adjudication["evidence_ids"] = [cluster_id]
    ambiguous["adjudications"] = [ambiguous_adjudication]
    ambiguous_result = validate_contract_dataset(ambiguous)
    assert not ambiguous_result.ok
    assert any(
        "evidence_ids[0] is ambiguous" in error
        for error in ambiguous_result.errors
    )

    cross_type = deepcopy(dataset)
    cross_type["messages"][0]["message_id"] = "SHARED_TYPED_ID"
    cross_type["claims"][0]["claim_id"] = "SHARED_TYPED_ID"
    cross_type_result = validate_contract_dataset(cross_type)
    assert not cross_type_result.ok
    assert any(
        "cross-type duplicate typed ID SHARED_TYPED_ID" in error
        for error in cross_type_result.errors
    )

    event_seed_collision = deepcopy(dataset)
    event_seed_collision["clusters"][0]["event_seed_id"] = event_seed_collision["claims"][0]["claim_id"]
    event_seed_result = validate_contract_dataset(event_seed_collision)
    assert not event_seed_result.ok
    assert any(
        "cross-type duplicate typed ID %s" % event_seed_collision["claims"][0]["claim_id"] in error
        for error in event_seed_result.errors
    )


def test_v2_public_aggregate_has_no_label_derived_metadata_and_supersedes_v1(tmp_path):
    assert DEFAULT_OUTPUT_ROOT.name.endswith("_v2")
    assert LEGACY_OUTPUT_ROOT.name.endswith("_v1")
    assert SPLIT_VERSION.endswith("-v2")
    assert LEGACY_SPLIT_VERSION.endswith("-v1")

    rows = {
        name: []
        for name in (
            "messages",
            "mentions",
            "claims",
            "relations",
            "clusters",
            "presentations",
            "adjudications",
        )
    }
    rows["messages"] = [
        {
            "message_id": "MESSAGE_SYNTHETIC",
            "chat_id": "CHAT_SYNTHETIC",
            "chat_type": "group",
            "time_bucket": "early",
        }
    ]
    rows["relations"] = [{"label": "same_event", "must_not_link": True}]
    public = _public_coverage(rows)
    public_text = json.dumps(public, ensure_ascii=False)
    for forbidden in (
        "relation_labels",
        "must_not_link_count",
        "label_counts",
        "same_event",
    ):
        assert forbidden not in public_text

    previous_root = tmp_path / "p01_evaluation_split_v1"
    previous_root.mkdir()
    sentinel = previous_root / "frozen_test" / "messages.private.jsonl"
    sentinel.parent.mkdir()
    sentinel.write_text("immutable synthetic payload\n", encoding="utf-8")
    aggregate_path = previous_root / "aggregate_manifest.json"
    aggregate_path.write_text(
        json.dumps(
            {
                "artifact": "p01_evaluation_split",
                "split_version": LEGACY_SPLIT_VERSION,
                "status": "active",
                "aggregate_sha256": "",
            }
        ),
        encoding="utf-8",
    )
    _mark_previous_version_superseded(
        previous_root,
        superseded_by=SPLIT_VERSION,
    )
    superseded = json.loads(aggregate_path.read_text(encoding="utf-8"))
    assert superseded["status"] == "superseded"
    assert superseded["superseded_by"] == SPLIT_VERSION
    assert sentinel.read_text(encoding="utf-8") == "immutable synthetic payload\n"
