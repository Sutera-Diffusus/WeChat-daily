"""K19 synthetic contracts for the compact Stage-A topic cardinality gate.

These tests are deliberately offline.  They do not read a private artifact,
open frozen data, invoke a runner, or call a provider.  The health failure
that motivated K19 had one primary row and one context row; the provider
returned too many topics.  The safe contract is therefore derived from the
primary rows, not from the total message count or from a configurable guess.
"""

from __future__ import annotations

from typing import Any

import pytest

from wechat_bridge import compact_stage_a_protocol as protocol
from wechat_bridge.compact_stage_a_protocol_health import build_synthetic_health_request


SCOPE = {"account_id": "account-k19-synthetic", "chat_id": "chat-k19-synthetic"}


def _message(index: int, role: str = "primary") -> dict[str, Any]:
    return {
        "message_handle": (
            f"{SCOPE['account_id']}/{SCOPE['chat_id']}|message|M{index:03d}"
        ),
        "text": f"synthetic cue {index}",
        "role": role,
    }


def _request(primary: int = 2, context: int = 0) -> dict[str, Any]:
    messages = [_message(index, "primary") for index in range(1, primary + 1)]
    start = primary + 1
    messages.extend(
        _message(index, "context")
        for index in range(start, start + context)
    )
    return protocol.build_compact_stage_a_request(SCOPE, messages, [])


def _topic(topic_id: str, primary: list[str], context: list[str] | None = None) -> dict[str, Any]:
    return {
        "i": topic_id,
        "p": list(primary),
        "c": list(context or []),
        "u": "unknown",
    }


def _assert_code(call: Any, expected: str) -> None:
    with pytest.raises(protocol.CompactStageAProtocolError) as captured:
        call()
    assert captured.value.code == expected


def test_health_shape_has_one_primary_and_one_derived_topic_slot() -> None:
    request = build_synthetic_health_request()
    rows = request["h"]
    primary = [row["i"] for row in rows if row["k"] == "m" and row["r"] == "p"]
    context = [row["i"] for row in rows if row["k"] == "m" and row["r"] == "c"]

    assert primary == ["m1"]
    assert context == ["m2"]
    assert protocol.topic_limit_for_request(request) == len(primary) == 1
    assert "topic_count<=primary_count" in protocol.TOPIC_LIMIT_RULE
    assert protocol.TOPIC_LIMIT_RULE in protocol.SYSTEM_PROMPT
    assert "primary_count" in protocol.REQUEST_SCHEMA
    assert "topic_count<=primary_count" in protocol.REQUEST_SCHEMA
    assert "topic_count<=primary_count" in protocol.RESPONSE_SCHEMA


def test_minimum_request_accepts_one_nonempty_primary_topic_only() -> None:
    request = _request(primary=1, context=1)
    valid = {"t": [_topic("t1", ["m1"], ["m2"])]}
    assert protocol.validate_compact_stage_a_output(valid, request) == valid

    # The context row cannot create a second topic.  This is the K18 failure
    # shape and must remain blocked rather than being silently reinterpreted.
    too_many = {
        "t": [
            _topic("t1", ["m1"]),
            _topic("t2", ["m1"]),
        ]
    }
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(too_many, request),
        "output_topic_limit",
    )

def test_each_topic_requires_a_primary_and_empty_primary_is_rejected() -> None:
    request = _request(primary=1)
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(
            {"t": [_topic("t1", [])]},
            request,
        ),
        "output_primary",
    )


def test_maximum_fourteen_primary_messages_allow_at_most_fourteen_topics() -> None:
    request = _request(primary=14)
    assert protocol.topic_limit_for_request(request) == 14

    maximum = protocol.build_max_size_response(request)
    assert len(maximum["t"]) == 14
    assert protocol.measure_output_size(maximum)["token_proxy"] <= protocol.MAX_OUTPUT_TOKENS
    assert protocol.measure_wire_size(request).http_token_proxy <= protocol.MAX_INPUT_TOKEN_PROXY

    extra_topic = {
        "t": [_topic(f"t{index}", ["m1"]) for index in range(1, 16)]
    }
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(extra_topic, request),
        "output_topic_limit",
    )


def test_duplicate_primary_is_rejected_even_when_topic_count_is_within_limit() -> None:
    request = _request(primary=2)
    duplicate = {
        "t": [
            _topic("t1", ["m1"]),
            _topic("t2", ["m1"]),
        ]
    }
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(duplicate, request),
        "duplicate_primary_handle",
    )


def test_request_with_only_context_rows_fails_before_provider_boundary() -> None:
    _assert_code(
        lambda: protocol.build_compact_stage_a_request(
            SCOPE,
            [_message(1, "context")],
            [],
        ),
        "request_primary_messages_empty",
    )


def test_topic_limit_is_not_based_on_total_messages_or_candidates() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [_message(1, "primary"), _message(2, "context"), _message(3, "context")],
        [
            f"{SCOPE['account_id']}/{SCOPE['chat_id']}|candidate|C001",
            f"{SCOPE['account_id']}/{SCOPE['chat_id']}|candidate|C002",
        ],
    )
    assert len(request["h"]) == 5
    assert protocol.topic_limit_for_request(request) == 1
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(
            {"t": [_topic("t1", ["m1"]), _topic("t2", ["m1"])]},
            request,
        ),
        "output_topic_limit",
    )
