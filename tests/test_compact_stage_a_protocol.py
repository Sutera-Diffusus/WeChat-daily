"""Synthetic/adversarial tests for the offline K15 Stage-A wire protocol."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from wechat_bridge.compact_stage_a_protocol import (
    MAX_INPUT_TOKEN_PROXY,
    MAX_OUTPUT_TOKENS,
    OUTPUT_TOP_KEYS,
    SYSTEM_PROMPT,
    CompactStageAProtocolError,
    build_compact_stage_a_request,
    build_max_size_response,
    canonical_json,
    compare_wire_sizes,
    measure_full_http_messages,
    measure_output_size,
    measure_wire_size,
    parse_compact_stage_a_output,
    resolve_compact_output,
    validate_compact_stage_a_output,
    validate_compact_stage_a_request,
)


SCOPE = {"account_id": "A", "chat_id": "C"}


def _message(index: int, text: str) -> dict[str, str]:
    return {
        "message_handle": f"A/C|message|M{index:03d}",
        "text": text,
        # This local field must never appear on the wire.  Scope/speaker are
        # authoritative local data, not model output.
        "speaker": f"private-speaker-{index}",
    }


def _candidate(index: int) -> str:
    return f"A/C|candidate|C{index:03d}"


def _request(messages: int = 4, candidates: int = 3) -> dict[str, Any]:
    return build_compact_stage_a_request(
        SCOPE,
        [_message(index, f"cue {index}") for index in range(1, messages + 1)],
        [_candidate(index) for index in range(1, candidates + 1)],
    )


def _output(*topics: Mapping[str, Any]) -> dict[str, Any]:
    return {"t": [dict(topic) for topic in topics]}


def _assert_code(call: Any, code: str) -> None:
    with pytest.raises(CompactStageAProtocolError, match=f"^{code}$"):
        call()


def test_greeting_new_topic_and_topic_shift_allow_multiple_topics() -> None:
    request = build_compact_stage_a_request(
        SCOPE,
        [
            _message(1, "早上好"),
            _message(2, "今天开会吗"),
            _message(3, "换个话题：周末天气"),
            _message(4, "需要带伞吗"),
        ],
    )
    result = validate_compact_stage_a_output(
        _output(
            {"i": "t1", "p": ["m1", "m2"], "c": [], "u": "certain"},
            {"i": "t2", "p": ["m3", "m4"], "c": ["m2"], "u": "uncertain"},
        ),
        request,
    )
    assert len(result["t"]) == 2
    assert {alias for topic in result["t"] for alias in topic["p"]} == {"m1", "m2", "m3", "m4"}


def test_no_reply_can_leave_context_empty_and_unknown() -> None:
    request = _request(messages=2, candidates=0)
    result = validate_compact_stage_a_output(
        _output({"i": "t1", "p": ["m1", "m2"], "c": [], "u": "unknown"}),
        request,
    )
    assert result["t"][0]["c"] == []
    assert result["t"][0]["u"] == "unknown"


def test_overmerge_is_a_valid_assignment_but_oversplit_duplicate_primary_is_not() -> None:
    request = _request(messages=4)
    # Semantics such as overmerge are audited separately; the wire contract
    # only guarantees that a syntactically valid partition is represented.
    validate_compact_stage_a_output(
        _output({"i": "t1", "p": ["m1", "m2", "m3", "m4"], "c": [], "u": "uncertain"}),
        request,
    )
    _assert_code(
        lambda: validate_compact_stage_a_output(
            _output(
                {"i": "t1", "p": ["m1", "m2"], "c": [], "u": "certain"},
                {"i": "t2", "p": ["m2", "m3", "m4"], "c": [], "u": "certain"},
            ),
            request,
        ),
        "duplicate_primary_handle",
    )


def test_duplicate_missing_and_forged_ids_fail_closed() -> None:
    request = _request(messages=3)
    cases = (
        ("duplicate_topic_id", _output({"i": "t1", "p": ["m1"], "c": [], "u": "unknown"}, {"i": "t1", "p": ["m2", "m3"], "c": [], "u": "unknown"})),
        ("primary_coverage", _output({"i": "t1", "p": ["m1", "m2"], "c": [], "u": "unknown"})),
        ("output_primary_handle_scope", _output({"i": "t1", "p": ["m1", "m999"], "c": [], "u": "unknown"})),
        ("output_context_handle_scope", _output({"i": "t1", "p": ["m1", "m2", "m3"], "c": ["c1"], "u": "unknown"})),
        ("output_uncertainty_enum", _output({"i": "t1", "p": ["m1", "m2", "m3"], "c": [], "u": "maybe"})),
    )
    for code, value in cases:
        _assert_code(lambda value=value: validate_compact_stage_a_output(value, request), code)

    _assert_code(
        lambda: parse_compact_stage_a_output(
            '{"t":{"i":"t1"}}',
            request,
        ),
        "output_topics",
    )
    _assert_code(
        lambda: parse_compact_stage_a_output(
            '{"t":[{"i":"t1","p":["m1","m2","m3"],"c":[],"u":"unknown"}],"extra":1}',
            request,
        ),
        "output_keys",
    )
    _assert_code(
        lambda: parse_compact_stage_a_output(
            '{"t":[{"i":"t1","p":["m1","m2","m3"],"c":[],"u":"unknown","u":"certain"}]}',
            request,
        ),
        "duplicate_json_key",
    )


def test_request_has_one_handle_table_no_verbose_aliases_or_speakers() -> None:
    request = _request()
    assert set(request) == {"v", "s", "h"}
    assert all(set(row) in ({"i", "k", "h", "r", "x"}, {"i", "k", "h"}) for row in request["h"])
    encoded = canonical_json(request)
    assert "materials" not in encoded
    assert "candidate_materials" not in encoded
    assert "evidence_handles" not in encoded
    assert "speaker" not in encoded
    # The candidate handles are in the request table, but the model output
    # vocabulary is message aliases only.
    response = build_max_size_response(request)
    encoded_response = canonical_json(response)
    assert all(f"c{index}" not in encoded_response for index in range(1, 4))


def test_scope_and_kind_are_authoritative_and_not_model_output() -> None:
    _assert_code(
        lambda: build_compact_stage_a_request(
            SCOPE,
            [{"message_handle": "OTHER/C|message|M001", "text": "x"}],
        ),
        "cross_scope_handle",
    )
    _assert_code(
        lambda: build_compact_stage_a_request(
            SCOPE,
            [{"message_handle": "A/C|candidate|C001", "text": "x"}],
        ),
        "handle_kind_mismatch",
    )


def test_worst_case_fourteen_messages_twenty_candidates_stays_under_both_limits() -> None:
    request = build_compact_stage_a_request(
        SCOPE,
        [_message(index, "x" * 80) for index in range(1, 15)],
        [_candidate(index) + ("x" * 70) for index in range(1, 21)],
    )
    stats = measure_wire_size(request)
    assert stats.http_token_proxy <= MAX_INPUT_TOKEN_PROXY
    assert stats.messages_chars == len(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": canonical_json(request)},
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    response = build_max_size_response(request)
    output_stats = measure_output_size(response)
    assert len(response["t"]) == 14
    assert output_stats["token_proxy"] <= MAX_OUTPUT_TOKENS
    assert validate_compact_stage_a_output(response, request) == response

    proof = measure_full_http_messages(SYSTEM_PROMPT, request)
    assert proof["system_chars"] == len(SYSTEM_PROMPT)
    assert proof["schema_chars"] > 0
    assert proof["http_token_proxy"] == stats.http_token_proxy


def test_max_size_response_is_deterministic_and_resolves_to_authoritative_handles() -> None:
    request = _request(messages=5, candidates=20)
    first = build_max_size_response(request)
    second = build_max_size_response(request)
    assert first == second
    resolved = resolve_compact_output(first, request)
    assert resolved["t"][0]["p"] == ["A/C|message|M001"]
    assert resolved["t"][0]["c"] == ["A/C|message|M002"]


def test_old_verbose_payload_is_incompatible_and_comparison_is_body_free() -> None:
    request = _request(messages=2, candidates=2)
    old = {
        "schema_version": "old",
        "stage": "A",
        "message_handles": ["A/C|message|M001", "A/C|message|M002"],
        "materials": [{"message_handle": "A/C|message|M001", "material": "duplicate body"}],
        "candidate_handles": ["A/C|candidate|C001", "A/C|candidate|C002"],
        "candidate_materials": [{"candidate_handle": "A/C|candidate|C001", "material": {"claims": ["body"]}}],
        "evidence_handles": ["A/C|evidence|E001"],
    }
    _assert_code(
        lambda: validate_compact_stage_a_output({"topics": []}, request),
        "output_keys",
    )
    report = compare_wire_sizes("old system " * 30, old, request)
    assert report["old"]["messages_chars"] > report["new"]["messages_chars"]
    assert report["reduction"]["messages_chars"] > 0
    assert "duplicate body" not in json.dumps(report, ensure_ascii=False)
