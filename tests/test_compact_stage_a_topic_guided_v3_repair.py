"""Offline/adversarial coverage for the topic-guided Stage-A v3 repair.

These tests stop at the request/parse boundary.  They do not open provider,
frozen, gold, production, or authorization-ledger paths.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from wechat_bridge import compact_stage_a_protocol_v3 as protocol
from wechat_bridge.compact_stage_a_development_pilot_v3 import (
    TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY,
    _build_request,
    _select_provider_context_view,
    context_recovery_snapshot,
)
from wechat_bridge.staged_deepseek_analyzer import (
    ContextPacket,
    StageProviderError,
    context_validation_telemetry as staged_context_validation_telemetry,
)


SCOPE = {"account_id": "topic-guided-account", "chat_id": "topic-guided-chat"}


def _handle(kind: str, index: int) -> str:
    noun = "message" if kind == "m" else "candidate"
    return f"{SCOPE['account_id']}/{SCOPE['chat_id']}|{noun}|repair-{kind}-{index:02d}"


def _request() -> dict[str, Any]:
    return protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 0), "role": "primary", "text": "主消息"},
            {"handle": _handle("m", 1), "role": "context", "text": "上下文"},
        ],
        [{"handle": _handle("c", 0)}],
    )


def _body_free_telemetry(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert "repair-secret-body" not in encoded
    assert "m1" not in encoded
    assert "c1" not in encoded
    assert "raw" not in value
    assert "body" not in value
    assert "alias" not in value


def test_protocol_context_error_taxonomy_is_counted_and_body_free() -> None:
    request = _request()
    malformed = {
        "topics": [
            {
                "topic_id": "t1",
                "primary_message_ids": ["m1"],
                "context_message_ids": ["m2", "m2", "m1", "c1", "unknown", 7],
                "uncertainty": "unknown",
            },
            {
                "topic_id": "t2",
                "primary_message_ids": ["m1"],
                "context_message_ids": ["m2"],
                "uncertainty": "unknown",
            },
        ],
        "raw": "repair-secret-body",
    }

    # The telemetry parser intentionally remains useful even when strict
    # validation rejects the first structural error it encounters.
    telemetry = protocol.context_validation_telemetry(malformed, request)
    counts = telemetry["error_counts"]
    assert counts["duplicate_within_topic"] >= 1
    assert counts["duplicate_across_topics"] >= 1
    assert counts["primary_as_context"] == 1
    assert counts["candidate_or_unknown_alias"] == 2
    assert counts["primary_context_overlap"] == 1
    assert counts["invalid_type"] == 1
    assert all(telemetry["error_flags"].values())
    _body_free_telemetry(telemetry)

    with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
        protocol.validate_compact_stage_a_output(malformed, request)
    error = exc_info.value
    assert error.context_error_counts["duplicate_within_topic"] >= 1
    assert error.context_error_flags["candidate_or_unknown_alias"] is True
    _body_free_telemetry(error.context_telemetry)


def test_parse_and_stage_provider_error_classify_invalid_type_without_payload() -> None:
    request = _request()
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
        protocol.parse_compact_stage_a_output("repair-secret-body", request)
    assert exc_info.value.context_error_counts["invalid_type"] == 1
    assert exc_info.value.context_error_flags["invalid_type"] is True
    _body_free_telemetry(exc_info.value.context_telemetry)

    telemetry = staged_context_validation_telemetry(
        {"topics": [{"primary_message_ids": ["m0"], "context_message_ids": ["m0", 1]}]},
        ContextPacket(
            packet_id="packet-repair",
            scope="scope-repair",
            message_ids=("m0",),
            context_message_ids=("m1",),
        ),
    )
    assert telemetry["error_counts"]["primary_as_context"] == 1
    assert telemetry["error_counts"]["invalid_type"] == 1
    provider_error = StageProviderError(
        "provider_invalid_json",
        context_telemetry=telemetry,
    )
    assert str(provider_error) == "provider_invalid_json"
    assert provider_error.context_error_flags["primary_as_context"] is True
    _body_free_telemetry(provider_error.context_telemetry)


def test_calibration_is_conservative_and_versioned() -> None:
    proxy = 1331
    calibrated = protocol.calibrate_token_proxy(proxy)
    assert calibrated >= max(proxy * 1.31, proxy + 432)
    assert calibrated == 1763
    assert protocol.within_calibrated_input_limit(proxy) is False
    assert protocol.TOKEN_CALIBRATION_VERSION


def test_context_selection_is_deterministic_prioritized_and_recoverable() -> None:
    scope = dict(SCOPE)

    def entry(kind: str, index: int, role: str, families: tuple[str, ...], identity: str | None = None) -> dict[str, Any]:
        handle = _handle(kind, index)
        return {
            "handle": handle,
            "identity": identity or handle,
            "source_index": index,
            "families": families,
            "provider_message": {
                "handle": handle,
                "role": role,
                "text": ("primary cue " if role == "primary" else "context cue ") + str(index),
                "message_type": "text" if role == "primary" else "system",
                "semantic_role": "substantive" if role == "primary" else "context",
            },
        }

    primary = [entry("m", index, "primary", ("fallback",)) for index in range(8)]
    contexts = [
        entry("m", 8, "context", ("reply",)),
        entry("m", 9, "context", ("quote",)),
        entry("m", 10, "context", ("qa",)),
        entry("m", 11, "context", ("opening",)),
        entry("m", 12, "context", ("adjacent",)),
        entry("m", 13, "context", ("fallback",)),
        entry("m", 14, "context", ("fallback",), identity="duplicate-context"),
        entry("m", 15, "context", ("fallback",), identity="duplicate-context"),
        entry("m", 16, "context", ("fallback",)),
        entry("m", 17, "context", ("fallback",)),
    ]
    first_request, first_selection = _select_provider_context_view(
        scope=scope,
        primary_entries=primary,
        context_entries=contexts,
        candidates=[],
        uncertainty_ceiling=None,
    )
    second_request, second_selection = _select_provider_context_view(
        scope=scope,
        primary_entries=primary,
        context_entries=contexts,
        candidates=[],
        uncertainty_ceiling=None,
    )
    assert first_request == second_request
    assert first_selection == second_selection
    assert first_selection["context_kept"] < first_selection["context_total"]
    assert first_selection["context_coverage"] < 1.0
    assert first_selection["deduplicated_context_count"] >= 1
    assert first_selection["reasons"]["kept"]
    assert first_selection["reasons"]["deferred"]
    assert first_selection["primary_never_deferred"] is True
    assert first_selection["context_role"] == "c"
    assert first_selection["calibrated_within_limit"] is True
    assert first_selection["calibrated_proxy"] <= protocol.CALIBRATED_INPUT_TOKEN_PROXY_LIMIT
    assert tuple(TOPIC_GUIDED_CONTEXT_SELECTION_PRIORITY[:5]) == (
        "reply", "quote", "qa", "topic_boundary", "opening"
    )
    assert sum(row["k"] == "m" and row["r"] == "p" for row in first_request["h"]) == 8
    assert all(
        row["r"] == "c"
        for row in first_request["h"]
        if row["k"] == "m" and row["r"] != "p"
    )


def test_page_recovery_snapshot_retains_complete_context_handles_without_deleting_primary() -> None:
    handles = [_handle("m", index) for index in range(18)]
    rows = [
        {
            "message_handle": handle,
            "role": "primary" if index < 8 else "context_only",
            "text": "primary cue" if index < 8 else "context cue",
        }
        for index, handle in enumerate(handles)
    ]
    page: dict[str, Any] = {
        "page_id": "repair-page",
        "root_id": "repair-root",
        "scope": dict(SCOPE),
        "message_handles": handles,
        "primary_message_handles": handles[:8],
        "message_rows": rows,
    }
    request = _build_request(page, {})
    snapshot = context_recovery_snapshot(page)
    assert len(snapshot["complete_context_handles"]) == 10
    assert len(snapshot["kept_context_handles"]) < len(snapshot["complete_context_handles"])
    assert len(snapshot["deferred_context_handles"]) > 0
    assert snapshot["source_ref_count"] == 18
    assert snapshot["body_free"] is True
    assert sum(row["k"] == "m" and row["r"] == "p" for row in request["h"]) == 8
    assert all(row["r"] in {"p", "c"} for row in request["h"] if row["k"] == "m")


def test_context_authority_caption_stays_strict_context_and_barrier_counts_zero() -> None:
    """A legal caption cannot cross a context+authority overlap without source-primary binding."""

    primary = _handle("m", 18)
    overlap = _handle("m", 19)
    rows = [
        {
            "message_handle": primary,
            "message_id": "primary-18",
            "role": "primary",
            "message_type": "text",
            "fragment_type": "statement",
            "text": "真实主题",
        },
        {
            "message_handle": overlap,
            "message_id": "context-authority-19",
            "role": "context_only",
            "roles": ["context", "authority"],
            "message_type": "text",
            "fragment_type": "statement",
            "text": "direct caption remains context",
            "authority_rows": [
                {
                    "message_id": "context-authority-19",
                    "message_type": "text",
                    "dialogue_role": "context",
                    "metadata_authoritative": True,
                    "span": {"start": 0, "end": 5},
                }
            ],
        },
    ]
    page: dict[str, Any] = {
        "page_id": "context-authority-overlap-page",
        "root_id": "context-authority-overlap-root",
        "scope": dict(SCOPE),
        "message_handles": [primary, overlap],
        "primary_message_handles": [primary],
        "adjacent_message_handles": [overlap],
        "authority_message_handles": [overlap],
        "message_rows": rows,
    }

    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[primary]["r"] == "p"
    assert by_handle[overlap]["r"] == "c"
    assert by_handle[overlap]["x"] == ""
    telemetry = page["_role_projection_telemetry"]
    assert telemetry["barrier_mispromotion_zero"] is True
    assert telemetry["media_mispromotion_count"] == 0
    assert telemetry["empty_authority_mispromotion_count"] == 0
    assert telemetry["unbound_primary_promotion_count"] == 0


def test_connected_components_are_merge_priors_and_topic_shift_is_only_split_witness() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 30), "role": "primary", "text": "同一主题一"},
            {"handle": _handle("m", 31), "role": "primary", "text": "同一主题二"},
            {"handle": _handle("m", 32), "role": "primary", "text": "第三消息"},
        ],
        topic_hints=[
            {"a": "m1", "b": "m2", "r": "same_subject"},
            {"a": "m2", "b": "m3", "r": "same_action"},
        ],
    )
    merged = protocol.build_candidate_connected_components(request)
    assert [part["primary_message_ids"] for part in merged["components"]] == [["m1", "m2", "m3"]]
    assert merged["merge_prior"] == "connected_components_merge_by_default"
    assert merged["candidate_only"] is True
    assert merged["model_decision_required"] is True
    assert merged["final_topic_assignment"] is False
    assert merged["under_uncertainty"] == "prefer_merge"
    assert merged["no_one_message_per_topic"] is True
    assert all("topic_id" not in part for part in merged["components"])

    split_request = protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 40), "role": "primary", "text": "旧主题"},
            {"handle": _handle("m", 41), "role": "primary", "text": "新主题"},
        ],
        topic_hints=[{"a": "m1", "b": "m2", "r": "topic_shift"}],
    )
    split = protocol.build_candidate_connected_components(split_request)
    assert [part["primary_message_ids"] for part in split["components"]] == [["m1"], ["m2"]]
    assert split["split_evidence_count"] == 1
    assert split["split_evidence_families"] == ["topic_shift"]
    assert split["final_topic_assignment"] is False
