"""Independent K20 offline authorization contract for compact Stage-A v2.

The tests exercise only the in-memory protocol boundary.  They deliberately
do not import a runner/provider, read a development message, open frozen
data, or write an artifact.  A passing K20 result authorizes at most one new
synthetic health-only call; it never authorizes development, Stage B, Stage C,
or production use.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable, Iterable, Mapping

import pytest

from wechat_bridge import compact_stage_a_protocol as protocol


ACCOUNT = "account-k20-synthetic"
CHAT = "chat-k20-synthetic"
OTHER_ACCOUNT = "account-k20-other"
OTHER_CHAT = "chat-k20-other"
SCOPE = {"account_id": ACCOUNT, "chat_id": CHAT}
BODY_MARKER = "K20_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"
V1_PROTOCOL = "stage_a_topic_assignment_compact_v1"
V1_PROMPT = "stage_a_topic_assignment_compact_prompt_v1"


def _message(index: int, role: str = "primary", *, long: bool = False) -> dict[str, Any]:
    handle = f"{ACCOUNT}/{CHAT}|message|M{index:03d}"
    cue = f"{BODY_MARKER}-{index:02d}"
    if long:
        cue = cue.ljust(protocol.MAX_MESSAGE_CUE_CHARS, "x")
    return {
        "message_handle": handle,
        "text": cue,
        "role": role,
        "speaker": f"synthetic-speaker-{index}",
        "scope": dict(SCOPE),
    }


def _candidate(index: int, *, long: bool = False) -> dict[str, Any]:
    handle = f"{ACCOUNT}/{CHAT}|candidate|C{index:03d}"
    if long:
        handle += "x" * 70
    return {
        "candidate_handle": handle,
        "left_message": f"m{((index - 1) % 12) + 1}",
        "right_message": f"m{(index % 12) + 1}",
        "relation": "no_reply" if index == 20 else "continuity",
        "scope": dict(SCOPE),
        "text": BODY_MARKER,
    }


def _request(
    primary: int = 2,
    context: int = 0,
    candidates: int = 0,
    *,
    long: bool = False,
) -> dict[str, Any]:
    messages = [_message(index, "primary", long=long) for index in range(1, primary + 1)]
    first_context = primary + 1
    messages.extend(
        _message(index, "context", long=long)
        for index in range(first_context, first_context + context)
    )
    return protocol.build_compact_stage_a_request(
        SCOPE,
        messages,
        [_candidate(index, long=long) for index in range(1, candidates + 1)],
    )


def _topic(
    topic_id: str,
    primary: Iterable[str],
    context: Iterable[str] = (),
    uncertainty: str = "unknown",
) -> dict[str, Any]:
    return {
        "i": topic_id,
        "p": list(primary),
        "c": list(context),
        "u": uncertainty,
    }


def _assert_code(operation: Callable[[], Any], expected: str) -> None:
    with pytest.raises(protocol.CompactStageAProtocolError) as captured:
        operation()
    assert captured.value.code == expected


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _walk(child)


def _assert_body_free(value: Any) -> None:
    forbidden = {
        "body",
        "content",
        "message_text",
        "prompt",
        "raw",
        "raw_response",
        "response",
        "summary",
        "text",
        "transcript",
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert BODY_MARKER not in encoded
    for key, child in _walk(value):
        is_ref = key.casefold().endswith(("_ref", "_handle", "_handles"))
        if key.casefold() in forbidden and child not in (None, "", [], {}, ()) and not is_ref:
            raise AssertionError(f"body-bearing ledger field: {key}")


def test_primary_count_is_one_shared_rule_across_prompt_schema_and_validator() -> None:
    request_one = _request(primary=1, context=2, candidates=20)
    request_three = _request(primary=3, context=0, candidates=0)

    assert protocol.topic_limit_for_request(request_one) == 1
    assert protocol.topic_limit_for_request(request_three) == 3
    assert protocol.TOPIC_LIMIT_RULE in protocol.SYSTEM_PROMPT
    assert protocol.TOPIC_LIMIT_RULE in protocol.REQUEST_SCHEMA
    assert protocol.TOPIC_LIMIT_RULE in protocol.RESPONSE_SCHEMA
    assert "topic_count<=primary_count" in protocol.SYSTEM_PROMPT
    assert "topic_count<=primary_count" in protocol.REQUEST_SCHEMA
    assert "topic_count<=primary_count" in protocol.RESPONSE_SCHEMA

    valid = {"t": [_topic("t1", ["m1"], ["m2"])]}
    assert protocol.validate_compact_stage_a_output(valid, request_one) == valid
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(
            {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]},
            request_one,
        ),
        "output_topic_limit",
    )


def test_context_cannot_create_a_topic_and_primary_is_covered_exactly_once() -> None:
    request = _request(primary=2, context=2, candidates=3)
    valid = {
        "t": [
            _topic("topic-a", ["m1"], ["m3"]),
            _topic("topic-b", ["m2"], ["m4"]),
        ]
    }
    normalized = protocol.validate_compact_stage_a_output(valid, request)
    primary = [alias for topic in normalized["t"] for alias in topic["p"]]
    context = [alias for topic in normalized["t"] for alias in topic["c"]]
    assert primary == ["m1", "m2"]
    assert len(primary) == len(set(primary)) == 2
    assert context == ["m3", "m4"]
    assert len(context) == len(set(context)) == 2

    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(
            {"t": [_topic("topic-a", ["m1"]), _topic("topic-b", ["m2"]), _topic("topic-c", [], ["m3"])]},
            request,
        ),
        "output_topic_limit",
    )
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(
            {"t": [_topic("topic-a", ["m1"]), _topic("topic-b", ["m1"])]},
            request,
        ),
        "duplicate_primary_handle",
    )
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(
            {"t": [_topic("topic-a", ["m1"]) ]},
            request,
        ),
        "primary_coverage",
    )


def test_minimal_and_maximal_packets_respect_wire_budgets() -> None:
    minimal = _request(primary=1, context=1, candidates=0)
    minimal_response = {"t": [_topic("t1", ["m1"], ["m2"])]}
    assert protocol.validate_compact_stage_a_output(minimal_response, minimal) == minimal_response
    assert protocol.measure_wire_size(minimal).http_token_proxy <= protocol.MAX_INPUT_TOKEN_PROXY

    maximal = _request(primary=12, context=2, candidates=20, long=True)
    request_stats = protocol.measure_wire_size(maximal)
    response = protocol.build_max_size_response(maximal)
    output_stats = protocol.measure_output_size(response)
    assert request_stats.http_token_proxy <= 1600
    assert output_stats["token_proxy"] <= 400
    assert len([row for row in maximal["h"] if row["k"] == "m"]) == 14
    assert len([row for row in maximal["h"] if row["k"] == "c"]) == 20
    assert len(response["t"]) == 12
    assert protocol.topic_limit_for_request(maximal) == 12


def test_candidate_and_context_counts_do_not_change_primary_topic_limit() -> None:
    one = _request(primary=2, context=0, candidates=0)
    many = _request(primary=2, context=2, candidates=20)
    assert protocol.topic_limit_for_request(one) == protocol.topic_limit_for_request(many) == 2
    assert len(one["h"]) == 2
    assert len(many["h"]) == 24

    response = {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]}
    assert protocol.validate_compact_stage_a_output(response, one) == response
    assert protocol.validate_compact_stage_a_output(response, many) == response


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda value: value.update({"extra": True}), "output_keys"),
        (lambda value: value.update({"t": []}), "output_topics"),
        (lambda value: value["t"][0]["p"].clear(), "output_primary"),
        (lambda value: value["t"][0]["p"].append("m1"), "output_primary_duplicate"),
        (lambda value: value["t"][0]["p"].append("m-forged"), "output_primary_item"),
        (lambda value: value["t"][0]["c"].append("m-forged"), "output_context_item"),
        (lambda value: value["t"][0].update({"state": "unknown"}), "output_topic_keys"),
    ],
)
def test_extra_empty_duplicate_forged_and_stage_b_output_is_rejected(
    mutator: Callable[[dict[str, Any]], None], expected: str
) -> None:
    request = _request(primary=2, context=1)
    value = {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]}
    if expected in {"output_topics"}:
        value = {"t": [_topic("t1", ["m1"])]}
    elif expected in {"output_primary", "output_primary_duplicate", "output_primary_item"}:
        value = {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]}
    elif expected in {"output_context_item", "output_topic_keys"}:
        value = {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]}
    mutator(value)
    _assert_code(lambda: protocol.validate_compact_stage_a_output(value, request), expected)


def test_cross_scope_request_and_cross_scope_output_are_rejected() -> None:
    wrong_message = _message(1)
    wrong_message["message_handle"] = f"{OTHER_ACCOUNT}/{OTHER_CHAT}|message|M001"
    _assert_code(
        lambda: protocol.build_compact_stage_a_request(SCOPE, [wrong_message], []),
        "cross_scope_handle",
    )

    request = _request(primary=2, context=1)
    forged = {"t": [_topic("t1", ["m1"], ["m-other"]), _topic("t2", ["m2"])]}
    _assert_code(
        lambda: protocol.validate_compact_stage_a_output(forged, request),
        "output_context_item",
    )


def test_context_only_request_is_rejected_before_provider_boundary() -> None:
    _assert_code(
        lambda: protocol.build_compact_stage_a_request(
            SCOPE,
            [_message(1, "context")],
            [],
        ),
        "request_primary_messages_empty",
    )


def test_body_free_ledger_omits_cues_and_keeps_only_opaque_wire_facts() -> None:
    request = _request(primary=2, context=1, candidates=2)
    response = {"t": [_topic("t1", ["m1", "m2"], ["m3"])]}
    report = protocol.size_report(request, response)
    ledger = protocol.project_body_free_ledger(request, response, report)
    _assert_body_free(ledger)
    assert ledger["protocol_version"] == protocol.PROTOCOL_VERSION
    assert ledger["message_count"] == 3
    assert ledger["candidate_count"] == 2
    assert ledger["primary_count"] == 2
    assert ledger["topic_limit"] == 2
    assert set(ledger["message_handles"]) == {
        f"{ACCOUNT}/{CHAT}|message|M001",
        f"{ACCOUNT}/{CHAT}|message|M002",
        f"{ACCOUNT}/{CHAT}|message|M003",
    }


def test_v2_protocol_prompt_and_hash_namespace_cannot_reuse_v1_wire() -> None:
    request = _request(primary=1, context=1)
    assert protocol.PROTOCOL_VERSION != V1_PROTOCOL
    assert protocol.PROMPT_VERSION != V1_PROMPT
    assert request["v"] == protocol.PROTOCOL_VERSION

    legacy = deepcopy(request)
    legacy["v"] = V1_PROTOCOL
    assert protocol.stable_hash(legacy) != protocol.stable_hash(request)
    assert protocol.measure_full_http_messages(protocol.SYSTEM_PROMPT, legacy)["messages_sha256"] != protocol.measure_full_http_messages(protocol.SYSTEM_PROMPT, request)["messages_sha256"]
    prompt_hash_v1 = protocol.stable_hash({"protocol": V1_PROTOCOL, "prompt": V1_PROMPT, "request": request})
    prompt_hash_v2 = protocol.stable_hash({"protocol": protocol.PROTOCOL_VERSION, "prompt": protocol.PROMPT_VERSION, "request": request})
    assert prompt_hash_v1 != prompt_hash_v2
