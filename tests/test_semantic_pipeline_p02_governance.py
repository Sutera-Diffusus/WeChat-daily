"""Synthetic P0.2 governance counterexamples.

The fixtures in this module are deliberately local and synthetic.  They only
exercise the P0.2 run/lineage gates; no private split, frozen annotation, or
production analysis path is involved.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from wechat_bridge.semantic_pipeline import (
    P01_PIPELINE_VERSION,
    P01_RULESET_VERSION,
    P02_PIPELINE_VERSION,
    P02_RULESET_VERSION,
    build_events_p02,
    classify_candidate_pairs_p02,
    classify_claim_pair_p02,
    derive_presentations_p02,
    derive_topic_families_p02,
    derive_trends_p02,
    extract_mentions_and_claims_p02,
    generate_candidate_pairs_p02,
    run_semantic_pipeline,
    run_semantic_pipeline_p01,
    run_semantic_pipeline_p02,
    validate_p02_invariants,
)


START = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def _message(message_id, content, seconds, *, block_id, segment_id, **extra):
    value = {
        "data_origin": "synthetic",
        "message_id": message_id,
        "account_id": "synthetic-account",
        "chat_id": "synthetic-chat",
        "sender_id": "synthetic-sender",
        "sender_name": "合成人员",
        "content": content,
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(),
        "message_type": "text",
        "is_group": True,
        "is_self": False,
        "block_id": block_id,
        "dialogue_segment_id": segment_id,
    }
    value.update(extra)
    return value


def _valid_result():
    return run_semantic_pipeline_p02(
        [
            _message("one", "GPT 重置了。", 0, block_id="block-one", segment_id="segment-one"),
            _message("two", "GPT 重置了。", 30, block_id="block-two", segment_id="segment-two"),
            _message(
                "reply",
                "GPT 重置了。",
                60,
                block_id="block-three",
                segment_id="segment-three",
                reply_to_message_id="one",
            ),
        ],
        analysis_run_id="synthetic-p02-run",
    )


def test_p02_invariants_and_downstream_stage_reject_run_and_version_mixes():
    result = _valid_result()
    assert validate_p02_invariants(result)["passed"] is True

    tampered_claim = replace(result.claims[0], analysis_run_id="other-run")
    tampered = replace(result, claims=(tampered_claim,) + result.claims[1:])
    report = validate_p02_invariants(tampered)
    assert report["passed"] is False
    assert any("analysis_run_id mismatch" in error for error in report["errors"])
    with pytest.raises(ValueError, match="analysis_run_id mismatch"):
        generate_candidate_pairs_p02(
            tampered.claims,
            result.analysis_run_id,
            result.created_at,
        )

    # P0 and P0.1 DTOs share the semantic wire classes, so stage metadata is
    # the fail-closed discriminator.  Relabeling neither is implicit here.
    p0 = run_semantic_pipeline(
        [_message("p0", "GPT 重置了。", 0, block_id="block-p0", segment_id="segment-p0")],
        analysis_run_id="synthetic-p0-run",
    )
    p01 = run_semantic_pipeline_p01(
        [_message("p01", "GPT 重置了。", 0, block_id="block-p01", segment_id="segment-p01")],
        analysis_run_id="synthetic-p01-run",
    )
    for claims, run_id, expected_marker in (
        (p0.claims, p0.analysis_run_id, "pipeline_version mismatch"),
        (p01.claims, p01.analysis_run_id, "pipeline_version mismatch"),
    ):
        with pytest.raises(ValueError, match=expected_marker):
            generate_candidate_pairs_p02(claims, run_id, p0.created_at)

    mixed_ruleset = replace(
        result.claims[0],
        ruleset_version=P01_RULESET_VERSION,
        provenance=replace(
            result.claims[0].provenance,
            parameters_version=P01_RULESET_VERSION,
        ),
    )
    with pytest.raises(ValueError, match="ruleset_version mismatch"):
        generate_candidate_pairs_p02(
            (mixed_ruleset,) + result.claims[1:],
            result.analysis_run_id,
            result.created_at,
        )

    # Keep the constants imported above in the test's executable contract:
    # a P02 object must carry the one matching pipeline/ruleset pair.
    assert result.pipeline_version == P02_PIPELINE_VERSION
    assert result.ruleset_version == P02_RULESET_VERSION
    assert P01_PIPELINE_VERSION != P02_PIPELINE_VERSION


def test_p02_invariants_reject_external_lineage_even_when_id_is_known():
    result = _valid_result()
    claim = result.claims[0]
    other_message_id = next(item.message_id for item in result.messages if item.message_id != claim.message_id)

    # A caller must not be able to expand source metadata and provenance in
    # tandem.  The extra ID is a real message ID, but not a source of this
    # claim, so it is still external to the claim's stage lineage.
    expanded_claim = replace(
        claim,
        source_message_ids=claim.source_message_ids + (other_message_id,),
        provenance=replace(
            claim.provenance,
            input_ids=claim.provenance.input_ids + (other_message_id,),
        ),
    )
    expanded = replace(result, claims=(expanded_claim,) + result.claims[1:])
    report = validate_p02_invariants(expanded)
    assert report["passed"] is False
    assert any("message-local" in error or "external input_id" in error for error in report["errors"])


def test_p02_invariants_distinguish_unspecified_from_explicit_empty_eligibility():
    result = _valid_result()

    assert validate_p02_invariants(result)["passed"] is True

    report = validate_p02_invariants(result, event_eligible_message_ids=[])
    assert report["passed"] is False
    assert any(error.startswith("claim_context_leak:") for error in report["errors"])
    assert any(error.startswith("event_context_leak:") for error in report["errors"])
    assert any(error.startswith("presentation_context_leak:") for error in report["errors"])


def test_p02_extraction_rejects_unscoped_message_boundary():
    result = _valid_result()
    bad_message = replace(result.messages[0], dialogue_segment_id="segment-one")
    with pytest.raises(ValueError, match="unscoped boundary"):
        extract_mentions_and_claims_p02(
            (bad_message,) + result.messages[1:],
            result.analysis_run_id,
            result.created_at,
        )


def test_p02_every_derived_stage_keeps_its_input_governance_gate():
    result = _valid_result()
    claim_by_id = {item.claim_id: item for item in result.claims}
    first_claim = result.claims[0]
    second_claim = result.claims[1]

    bad_claim = replace(first_claim, analysis_run_id="other-run")
    with pytest.raises(ValueError, match="analysis_run_id mismatch"):
        classify_claim_pair_p02(
            bad_claim,
            second_claim,
            result.analysis_run_id,
            result.created_at,
        )
    with pytest.raises(ValueError, match="analysis_run_id mismatch"):
        classify_candidate_pairs_p02(
            (bad_claim,) + result.claims[1:],
            result.analysis_run_id,
            result.created_at,
        )

    bad_decision = replace(result.pair_decisions[0], schema_version="other-schema")
    with pytest.raises(ValueError, match="schema_version mismatch"):
        build_events_p02(
            result.claims,
            (bad_decision,) + result.pair_decisions[1:],
            result.analysis_run_id,
            result.created_at,
        )

    bad_event = replace(result.events[0], pipeline_version=P01_PIPELINE_VERSION)
    with pytest.raises(ValueError, match="pipeline_version mismatch"):
        derive_topic_families_p02(
            (bad_event,) + result.events[1:],
            result.claims,
            result.analysis_run_id,
            result.created_at,
        )

    bad_family = replace(
        result.topic_families[0],
        ruleset_version=P01_RULESET_VERSION,
    )
    with pytest.raises(ValueError, match="ruleset_version mismatch"):
        derive_trends_p02(
            result.events,
            (bad_family,) + result.topic_families[1:],
            result.claims,
            result.analysis_run_id,
            result.created_at,
        )

    bad_event_for_presentation = replace(result.events[0], analysis_run_id="other-run")
    with pytest.raises(ValueError, match="analysis_run_id mismatch"):
        derive_presentations_p02(
            (bad_event_for_presentation,) + result.events[1:],
            result.claims,
            result.analysis_run_id,
            result.created_at,
        )

    # Keep this mapping material: it documents the graph used by the test and
    # makes accidental fixture changes fail loudly rather than silently
    # switching to an unrelated claim pair.
    assert first_claim.claim_id in claim_by_id
    assert second_claim.claim_id in claim_by_id
