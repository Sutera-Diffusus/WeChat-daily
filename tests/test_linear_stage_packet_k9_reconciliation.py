"""Synthetic regression tests for the independent K9 reconciliation helpers."""

from __future__ import annotations

from reconcile_linear_stage_packet_k9_private import (
    _evidence_metric,
    _nested_key_count,
    _page_formula,
    _scope_pairs,
    _stage_a_metric,
    _assert_safe_report,
)


def test_page_formula_counts_physical_pages_not_repeated_transport_metadata() -> None:
    page_rows = [
        {
            "root_id": "root-a",
            "page_id": "root-a|page|0001",
            "message_handles": [f"m-{index}" for index in range(24)],
            "candidate_handles": [f"c-{index}" for index in range(64)],
            "evidence_handles": [f"e-{index}" for index in range(64)],
        },
        {
            "root_id": "root-a",
            "page_id": "root-a|page|0002",
            "message_handles": [f"m-{index}" for index in range(24, 48)],
            "candidate_handles": [f"c-{index}" for index in range(64, 128)],
            "evidence_handles": [f"e-{index}" for index in range(64, 128)],
        },
    ]
    result = _page_formula(page_rows)
    assert result["denominator"] == 1
    assert result["numerator"] == 1
    assert result["observed_page_count"] == 2
    assert result["expected_linear_page_count"] == 2
    assert result["cartesian_upper_bound"] == 8
    assert result["linear"] is True

    # A materialised envelope may repeat the same page id under its stage-A
    # and user-packet wrappers.  Those occurrences are diagnostic metadata,
    # never additional physical pages.
    envelope = {
        "page_id": "root-a|page|0001",
        "stage_a": {"page_id": "root-a|page|0001"},
        "user_packet": {"page_id": "root-a|page|0001"},
    }
    assert _nested_key_count(envelope, "page_id") == 3


def test_scope_pairs_normalise_repeated_scope_without_parsing_handle_slashes() -> None:
    value = {
        "scope": {"account_id": "account-a", "chat_id": "chat-a"},
        "page_id": "account-a/chat-a|page|0001",
        "stage_a": {"scope": {"account_id": "account-a", "chat_id": "chat-a"}},
        "user_packet": {"scope": "account-a/chat-a"},
        "message_handles": ["account-a/chat-a|message|m-1"],
    }
    assert _scope_pairs(value) == {("account-a", "chat-a")}
    assert _scope_pairs({"scope": {"account_id": "account-a", "chat_id": "chat-b"}}) == {
        ("account-a", "chat-b")
    }


def test_evidence_reconciliation_keeps_id_and_root_denominators_separate() -> None:
    packets = [
        {"packet_id": "root-a", "evidence_refs": [{"evidence_id": "e-a"}, {"evidence_id": "e-b"}]},
        {"packet_id": "root-b", "evidence_refs": []},
    ]
    recovery = [
        {
            "source_packet_id": "root-a",
            "rates": {"evidence": {"expected": 2, "recovered": 2}},
            "recovered_packet": {"evidence_refs": [{"evidence_id": "e-a"}, {"evidence_id": "e-b"}]},
        },
        {
            "source_packet_id": "root-b",
            "rates": {"evidence": {"expected": 0, "recovered": 0}},
            "recovered_packet": {"evidence_refs": []},
        },
    ]
    result = _evidence_metric(packets, recovery)
    assert result["id_recovery"]["numerator"] == 2
    assert result["id_recovery"]["denominator"] == 2
    assert result["bearing_root_coverage"]["numerator"] == 1
    assert result["bearing_root_coverage"]["denominator"] == 1
    assert result["bearing_root_coverage"]["selected_root_denominator"] == 2
    assert result["bearing_root_coverage"]["vacuous_root_count"] == 1


def test_stage_a_complete_budget_excludes_pending_but_requires_pending_snapshot() -> None:
    result = _stage_a_metric(
        [
            {
                "status": "complete",
                "input_token_proxy": 1800,
                "user_token_proxy": 1400,
                "message_count": 2,
                "candidate_count": 3,
                "evidence_count": 0,
            },
            {
                "status": "pending",
                "input_token_proxy": 2222,
                "user_token_proxy": 2204,
                "message_count": 2,
                "candidate_count": 3,
                "evidence_count": 0,
                "open_snapshot_ok": True,
            },
        ]
    )
    assert result["complete_within_limits_numerator"] == 1
    assert result["complete_denominator"] == 1
    assert result["complete_within_limits"] is True
    assert result["pending_row_count"] == 1
    assert result["pending_snapshots_ok"] is True
    assert result["max_complete_input_token_proxy"] == 1800
    assert result["max_all_row_input_token_proxy"] == 2222


def test_synthetic_reconciliation_output_has_no_body_or_identity_keys() -> None:
    report = {
        "schema": "synthetic",
        "opaque_ref": "row_deadbeef",
        "count": 1,
        "nested": {"linear": True},
    }
    _assert_safe_report(report)
