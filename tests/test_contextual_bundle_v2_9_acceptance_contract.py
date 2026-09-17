"""Synthetic v2.9 acceptance gates for bundle accounting and reactivation.

This test module is deliberately independent of production implementation and
of all private/frozen artifacts.  The fixtures contain only invented counts,
opaque cue labels, and status metadata.  It freezes the accounting contract so
candidate decisions, provider attempts, normalized outputs, and deferred work
cannot be silently collapsed into one metric.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, Mapping, Sequence

import pytest


MAX_SELECTED_BUNDLES = 14
MESSAGE_DENOMINATOR = 280
REQUIRED_EXPLICIT_COUNTERS = frozenset(
    {
        "provider_request_attempts",
        "successful_model_outputs",
        "failed_provider_attempts",
        "candidate_decisions",
        "budget_deferred",
    }
)
ZERO_TOLERANCE_COUNTERS = frozenset(
    {
        "cross_chat_relation_violations",
        "time_only_relation_violations",
        "same_segment_unsafe_strong",
        "silence_terminal_violations",
        "fallback_accepted",
    }
)


def _synthetic_run() -> dict[str, Any]:
    """Build one body-free run with the contract's expected accounting shape."""
    return {
        "messages": {"message_count": MESSAGE_DENOMINATOR},
        "counters": {
            "provider_request_attempts": 14,
            "successful_model_outputs": 8,
            "failed_provider_attempts": 5,
            "unfinished_provider_attempts": 1,
            "candidate_decisions": 848,
            "budget_deferred": 837,
            "deprecated_legacy_metrics": {
                "provider_calls_used": {
                    "value": 14,
                    "deprecated": True,
                    "replacement": "provider_request_attempts",
                },
                "semantic_model_calls": {
                    "value": 851,
                    "deprecated": True,
                    "replacement": "explicit request and decision counters",
                },
            },
        },
        "bundle_budget": {
            "selected_bundle_count": 14,
            "selected_bundle_limit": MAX_SELECTED_BUNDLES,
        },
        "activation_rows": [
            {
                "status": "pending",
                "channel": "pending_context",
                "activation_cues": ["cue-slot-a"],
                "recoverable_line_present": True,
            },
            {
                "status": "pending",
                "channel": "background",
                "activation_cues": ["cue-relation-b"],
                "recoverable_line_present": True,
            },
            {
                "status": "pending",
                "channel": "cold_recoverable",
                "activation_cues": ["cue-hash-c"],
                "recoverable_line_present": True,
            },
        ],
        "valuable_pending": [
            {
                "status": "pending",
                "channel": "pending_context",
                "information_value": "valuable",
                "discarded": False,
                "misclassified_as_unrelated": False,
                "reactivatable": True,
                "activation_cues": ["cue-slot-a"],
                "recoverable_line_present": True,
            },
            {
                "status": "pending",
                "channel": "cold_recoverable",
                "information_value": "valuable",
                "discarded": False,
                "misclassified_as_unrelated": False,
                "reactivatable": True,
                "activation_cues": ["cue-hash-c"],
                "recoverable_line_present": True,
            },
        ],
        "coverage": {
            "candidate_decision_rate_per_message": 848 / MESSAGE_DENOMINATOR,
            "selected_bundle_rate_per_message": 14 / MESSAGE_DENOMINATOR,
        },
        "compression": {
            "candidate_to_selected_ratio": 848 / 14,
            "message_to_selected_ratio": MESSAGE_DENOMINATOR / 14,
        },
        "zero_tolerance": {
            "cross_chat_relation_violations": 0,
            "time_only_relation_violations": 0,
            "same_segment_unsafe_strong": 0,
            "silence_terminal_violations": 0,
            "fallback_accepted": 0,
        },
    }


def _pending_or_cold(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if str(row.get("status") or "") == "pending"
        or str(row.get("channel") or "") in {"pending_context", "background", "cold_recoverable"}
    ]


def _activation_cue_coverage(rows: Sequence[Mapping[str, Any]]) -> float | None:
    eligible = _pending_or_cold(rows)
    if not eligible:
        return None
    covered = sum(bool(row.get("activation_cues")) for row in eligible)
    return covered / len(eligible)


def _request_ledger_counts(
    requests: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Count synthetic ledgers using the v2.9 definitions."""
    provider_rows = [row for row in requests if row.get("source") == "provider"]
    attempts = [
        row
        for row in provider_rows
        if row.get("status") in {"started", "complete", "failed"}
    ]
    return {
        "provider_request_attempts": len(attempts),
        "successful_model_outputs": sum(
            row.get("source") == "model_wire" and row.get("status") == "complete"
            for row in requests
        ),
        "failed_provider_attempts": sum(row.get("status") == "failed" for row in provider_rows),
        "unfinished_provider_attempts": sum(row.get("status") == "started" for row in provider_rows),
        "candidate_decisions": len(decisions),
        "budget_deferred": sum(
            row.get("source") == "budget"
            and row.get("status") == "pending"
            for row in decisions
        ),
    }


def _assert_v29_ready(run: Mapping[str, Any]) -> None:
    counters = run["counters"]
    assert REQUIRED_EXPLICIT_COUNTERS <= set(counters)

    selected = int(run["bundle_budget"]["selected_bundle_count"])
    selected_limit = int(run["bundle_budget"]["selected_bundle_limit"])
    message_count = int(run["messages"]["message_count"])
    assert message_count == MESSAGE_DENOMINATOR
    assert selected <= selected_limit <= MAX_SELECTED_BUNDLES
    assert selected != int(counters["candidate_decisions"])

    attempts = int(counters["provider_request_attempts"])
    successful = int(counters["successful_model_outputs"])
    failed = int(counters["failed_provider_attempts"])
    unfinished = int(counters["unfinished_provider_attempts"])
    assert attempts == successful + failed + unfinished
    assert int(counters["candidate_decisions"]) >= attempts
    assert 0 <= int(counters["budget_deferred"]) <= int(counters["candidate_decisions"])

    cue_coverage = _activation_cue_coverage(run["activation_rows"])
    assert cue_coverage is not None
    assert cue_coverage == pytest.approx(1.0)

    for metric_group in (run["coverage"], run["compression"]):
        assert metric_group
        for value in metric_group.values():
            assert isinstance(value, (int, float))
            assert isfinite(float(value))
            assert float(value) >= 0

    for row in run["valuable_pending"]:
        if row.get("information_value") != "valuable" or row.get("status") != "pending":
            continue
        assert row.get("discarded") is not True
        assert row.get("misclassified_as_unrelated") is not True
        assert row.get("reactivatable") is True
        assert bool(row.get("activation_cues"))
        assert row.get("recoverable_line_present") is True

    for key in ZERO_TOLERANCE_COUNTERS:
        assert int(run["zero_tolerance"].get(key, -1)) == 0


def test_pending_and_cold_activation_cue_coverage_is_100_percent() -> None:
    run = _synthetic_run()
    assert _activation_cue_coverage(run["activation_rows"]) == pytest.approx(1.0)
    missing_cue = deepcopy(run)
    missing_cue["activation_rows"][1]["activation_cues"] = []
    assert _activation_cue_coverage(missing_cue["activation_rows"]) == pytest.approx(2 / 3)
    with pytest.raises(AssertionError):
        _assert_v29_ready(missing_cue)


def test_selected_bundle_count_is_bounded_at_14_for_280_messages() -> None:
    run = _synthetic_run()
    selected = run["bundle_budget"]["selected_bundle_count"]
    assert run["messages"]["message_count"] == 280
    assert selected <= 14
    assert selected != run["counters"]["candidate_decisions"]


def test_candidate_decisions_and_provider_calls_are_separate_ledgers() -> None:
    run = _synthetic_run()
    requests = [
        {"source": "provider", "status": "complete"},
        {"source": "provider", "status": "failed"},
        {"source": "provider", "status": "started"},
        {"source": "provider", "status": "pending"},
        {"source": "model_wire", "status": "complete"},
        {"source": "budget", "status": "pending"},
    ]
    decisions = [
        {"source": "budget", "status": "pending"},
        {"source": "rule", "status": "complete"},
    ]
    counts = _request_ledger_counts(requests, decisions)
    assert counts == {
        "provider_request_attempts": 3,
        "successful_model_outputs": 1,
        "failed_provider_attempts": 1,
        "unfinished_provider_attempts": 1,
        "candidate_decisions": 2,
        "budget_deferred": 1,
    }
    assert run["counters"]["candidate_decisions"] != run["counters"]["provider_request_attempts"]
    assert run["counters"]["provider_request_attempts"] == 14


def test_coverage_and_compression_metrics_are_numeric_and_recomputable() -> None:
    run = _synthetic_run()
    _assert_v29_ready(run)
    assert run["coverage"]["candidate_decision_rate_per_message"] == pytest.approx(848 / 280)
    assert run["compression"]["candidate_to_selected_ratio"] == pytest.approx(848 / 14)
    assert run["compression"]["message_to_selected_ratio"] == pytest.approx(280 / 14)


def test_valuable_pending_is_not_discarded_and_has_a_reactivation_path() -> None:
    run = _synthetic_run()
    _assert_v29_ready(run)
    broken = deepcopy(run)
    broken["valuable_pending"][0]["reactivatable"] = False
    with pytest.raises(AssertionError):
        _assert_v29_ready(broken)


def test_zero_tolerance_guards_remain_zero() -> None:
    run = _synthetic_run()
    _assert_v29_ready(run)
    broken = deepcopy(run)
    broken["zero_tolerance"]["time_only_relation_violations"] = 1
    with pytest.raises(AssertionError):
        _assert_v29_ready(broken)


def test_legacy_counts_are_retained_as_deprecated_facts_only() -> None:
    run = _synthetic_run()
    counters = run["counters"]
    legacy = counters["deprecated_legacy_metrics"]
    assert legacy["provider_calls_used"]["value"] == 14
    assert legacy["semantic_model_calls"]["value"] == 851
    assert legacy["provider_calls_used"]["deprecated"] is True
    assert legacy["semantic_model_calls"]["deprecated"] is True
    assert legacy["semantic_model_calls"]["value"] != counters["provider_request_attempts"]
