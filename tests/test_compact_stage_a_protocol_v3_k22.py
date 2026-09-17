"""Independent synthetic K22 contract for the descriptive Stage-A v3 wire.

The tests exercise only the in-memory request/response boundary.  They do not
import a runner or provider and they do not read private or frozen artifacts.
The request keeps authoritative handles in one local table while the response
uses the explicit ``topics``/``topic_id``/``primary_message_ids``/
``context_message_ids``/``uncertainty`` names so a provider cannot confuse the
protocol with the older one-letter v2 wire.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from wechat_bridge import compact_stage_a_protocol as v2
from wechat_bridge import compact_stage_a_protocol_v3 as protocol


ACCOUNT = "account-k22-synthetic"
CHAT = "chat-k22-synthetic"
OTHER_ACCOUNT = "account-k22-other"
OTHER_CHAT = "chat-k22-other"
SCOPE = {"account_id": ACCOUNT, "chat_id": CHAT}
BODY_MARKER = "K22_SYNTHETIC_BODY_MUST_NOT_REACH_LEDGER"


def _handle(kind: str, index: int, *, account: str = ACCOUNT, chat: str = CHAT) -> str:
    noun = "message" if kind == "m" else "candidate"
    return f"{account}/{chat}|{noun}|k22-{index:02d}"


def _request(*, primary_count: int = 12, context_count: int = 2, candidate_count: int = 20) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for index in range(primary_count):
        messages.append(
            {
                "handle": _handle("m", index),
                "role": "primary",
                "text": f"topic-{index} {BODY_MARKER}" if index == 0 else f"topic-{index}",
            }
        )
    for index in range(context_count):
        messages.append(
            {
                "handle": _handle("m", primary_count + index),
                "role": "context",
                "text": "你好" if index == 0 else "辛苦了",
            }
        )
    candidates = [{"handle": _handle("c", index)} for index in range(candidate_count)]
    return protocol.build_compact_stage_a_request(SCOPE, messages, candidates)


def _simple_request() -> dict[str, Any]:
    return protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 0), "role": "context", "text": "你好"},
            {"handle": _handle("m", 1), "role": "primary", "text": "GPT 重置是否可靠"},
            {"handle": _handle("m", 2), "role": "primary", "text": "LinuxDo 注册邮箱收不到"},
            {"handle": _handle("m", 3), "role": "context", "text": "辛苦了"},
        ],
        [{"handle": _handle("c", 0)}],
    )


def _response(*topics: Mapping[str, Any]) -> dict[str, Any]:
    return {"topics": [dict(topic) for topic in topics]}


def _expect_protocol_error(call: Any, *args: Any, **kwargs: Any) -> None:
    with pytest.raises(protocol.CompactStageAProtocolV3Error):
        call(*args, **kwargs)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_v3_prompt_and_response_use_explicit_topic_names() -> None:
    required = (
        "topics",
        "topic_id",
        "primary_message_ids",
        "context_message_ids",
        "uncertainty",
    )
    for token in required:
        assert token in protocol.SYSTEM_PROMPT
        assert token in protocol.RESPONSE_SCHEMA
    assert "Minimal valid example" in protocol.SYSTEM_PROMPT
    assert "request u=uncertain" in protocol.SYSTEM_PROMPT
    assert "unresolved candidate cues are not facts" in protocol.SYSTEM_PROMPT
    assert protocol.OUTPUT_TOP_KEYS == frozenset({"topics"})
    assert protocol.OUTPUT_TOPIC_KEYS == frozenset(
        {"topic_id", "primary_message_ids", "context_message_ids", "uncertainty"}
    )
    # The v2 one-letter response is not an accepted v3 surface.
    assert "{t:" not in protocol.RESPONSE_SCHEMA
    assert "short-key" not in protocol.SYSTEM_PROMPT.casefold()


def test_context_uniqueness_contract_is_global_and_body_free() -> None:
    """The prompt/schema/packet all expose one global context allocation rule."""

    rule = protocol.CONTEXT_UNIQUENESS_RULE
    assert protocol.CONTEXT_UNIQUENESS_FIELD == "context_unique"
    assert protocol.CONTEXT_UNIQUENESS_MODE == "global"
    assert rule in protocol.SYSTEM_PROMPT
    assert rule in protocol.REQUEST_SCHEMA
    assert rule in protocol.RESPONSE_SCHEMA
    assert "globally" in protocol.SYSTEM_PROMPT
    assert "allocated context set" in protocol.SYSTEM_PROMPT
    assert "ambiguous" in protocol.SYSTEM_PROMPT
    assert "omit" in protocol.SYSTEM_PROMPT
    assert "most relevant topic" in protocol.SYSTEM_PROMPT
    assert "uncertain" in protocol.SYSTEM_PROMPT
    assert protocol.MINIMAL_MULTI_TOPIC_VALID_EXAMPLE in protocol.SYSTEM_PROMPT
    assert protocol.MINIMAL_MULTI_TOPIC_VALID_EXAMPLE in protocol.RESPONSE_SCHEMA
    assert protocol.DUPLICATE_CONTEXT_COUNTEREXAMPLE in protocol.SYSTEM_PROMPT
    assert protocol.DUPLICATE_CONTEXT_COUNTEREXAMPLE in protocol.RESPONSE_SCHEMA

    request = _request(primary_count=2, context_count=1, candidate_count=1)
    assert request[protocol.CONTEXT_UNIQUENESS_FIELD] == protocol.CONTEXT_UNIQUENESS_MODE
    assert protocol.validate_compact_stage_a_request(request) == request
    assert protocol.CONTEXT_UNIQUENESS_ERROR_CODES <= protocol.KNOWN_VALIDATION_ERROR_CODES


def test_max_shape_context_allocation_is_unique_and_duplicate_is_fail_closed() -> None:
    """The deterministic 14/20 witness cannot allocate one context twice."""

    request = _request()
    response = protocol.build_max_size_response(request)
    context = [alias for topic in response["topics"] for alias in topic["context_message_ids"]]
    assert context
    assert len(context) == len(set(context))
    assert protocol.measure_wire_size(request).http_token_proxy <= 1600
    assert protocol.measure_output_size(response)["token_proxy"] <= 400

    duplicate = json.loads(protocol.canonical_json(response))
    first_context = duplicate["topics"][0]["context_message_ids"][0]
    duplicate["topics"][1]["context_message_ids"].append(first_context)
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
        protocol.validate_compact_stage_a_output(duplicate, request)
    assert exc_info.value.code == "duplicate_context_handle"
    assert exc_info.value.validation_categories == ("context_unique",)


def test_duplicate_context_counterexample_and_request_constraint_share_taxonomy() -> None:
    request = _request(primary_count=2, context_count=1, candidate_count=0)
    duplicate = json.loads(protocol.DUPLICATE_CONTEXT_COUNTEREXAMPLE)
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
        protocol.validate_compact_stage_a_output(duplicate, request)
    assert exc_info.value.code == "duplicate_context_handle"
    assert protocol.validation_categories_for_code("output_context_item") == ("context_unique",)
    assert protocol.validation_categories_for_code("output_context_duplicate") == ("context_unique",)
    assert protocol.validation_categories_for_code("duplicate_context_handle") == ("context_unique",)

    malformed_request = {**request, protocol.CONTEXT_UNIQUENESS_FIELD: "per_topic"}
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as request_error:
        protocol.validate_compact_stage_a_request(malformed_request)
    assert request_error.value.code == "request_context_unique"
    assert request_error.value.validation_categories == ("context_unique",)
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as builder_error:
        protocol.build_compact_stage_a_request(
            SCOPE,
            [
                {"handle": _handle("m", 0), "role": "primary", "text": "one"},
            ],
            context_unique="per_topic",
        )
    assert builder_error.value.code == "request_context_unique"


def test_worst_case_request_and_response_stay_within_wire_budgets() -> None:
    request = _request()
    validated_request = protocol.validate_compact_stage_a_request(request)
    assert validated_request == request
    stats = protocol.measure_wire_size(request)
    assert stats.http_token_proxy <= 1600
    assert stats.within_input_limit

    response = protocol.build_max_size_response(request)
    validated = protocol.validate_compact_stage_a_output(response, request)
    output_stats = protocol.measure_output_size(validated)
    assert output_stats["token_proxy"] <= 400
    assert len(validated["topics"]) == protocol.topic_limit_for_request(request)

    proof = protocol.max_size_proof(request)
    assert proof["request_within_1600"] is True
    assert proof["response_within_400"] is True
    assert proof["topic_limit"] == proof["primary_count"]

    # The unresolved-reference ceiling is an optional request field and must
    # not consume the v3 input/output budget guarantees.
    uncertain_request = {**request, "u": "uncertain"}
    assert protocol.validate_compact_stage_a_request(uncertain_request) == uncertain_request
    uncertain_stats = protocol.measure_wire_size(uncertain_request)
    assert uncertain_stats.http_token_proxy <= 1600
    uncertain_response = protocol.build_max_size_response(uncertain_request)
    assert protocol.measure_output_size(uncertain_response)["token_proxy"] <= 400
    uncertain_proof = protocol.max_size_proof(uncertain_request)
    assert uncertain_proof["request_within_1600"] is True
    assert uncertain_proof["response_within_400"] is True


def test_full_name_output_partition_preserves_context_and_no_reply_candidate() -> None:
    request = _simple_request()
    response = _response(
        {
            "topic_id": "gpt-reset",
            "primary_message_ids": ["m2"],
            "context_message_ids": ["m1"],
            "uncertainty": "unknown",
        },
        {
            "topic_id": "linuxdo-email",
            "primary_message_ids": ["m3"],
            "context_message_ids": ["m4"],
            "uncertainty": "uncertain",
        },
    )
    result = protocol.validate_compact_stage_a_output(response, request)
    assert set(result) == {"topics"}
    assert set(result["topics"][0]) == set(protocol.OUTPUT_TOPIC_KEYS)
    assert result["topics"][0]["primary_message_ids"] == ["m2"]
    assert result["topics"][0]["context_message_ids"] == ["m1"]
    assert result["topics"][1]["primary_message_ids"] == ["m3"]

    resolved = protocol.resolve_compact_output(result, request)
    assert resolved["topics"][0]["primary_message_ids"] == [_handle("m", 1)]
    assert resolved["topics"][0]["context_message_ids"] == [_handle("m", 0)]
    assert resolved["topics"][1]["primary_message_ids"] == [_handle("m", 2)]

    # The unanswered candidate remains in the request-local ledger even though
    # Stage A correctly does not invent a topic for a candidate row.
    ledger = protocol.project_body_free_ledger(request, result)
    assert ledger["candidate_count"] == 1
    assert ledger["candidate_handles"] == [_handle("c", 0)]
    assert BODY_MARKER not in _json(ledger)


def test_provider_primary_alias_requires_semantic_text_or_caption() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 0), "role": "primary", "text": "接口返回 502，需要排查"},
            {"handle": _handle("m", 1), "role": "primary", "text": "你好"},
            {"handle": _handle("m", 2), "role": "primary", "message_type": "image"},
            {"handle": _handle("m", 3), "role": "authority"},
            {
                "handle": _handle("m", 4),
                "role": "primary",
                "message_type": "image",
                "text": "",
                "caption": "截图显示登录接口返回 502",
            },
        ],
    )
    rows = request["h"]
    assert [row["r"] for row in rows[:5]] == ["p", "c", "c", "c", "p"]
    assert rows[4]["x"] == "截图显示登录接口返回 502"

    valid = _response(
        {
            "topic_id": "semantic",
            "primary_message_ids": ["m1", "m5"],
            "context_message_ids": ["m2", "m3", "m4"],
            "uncertainty": "unknown",
        }
    )
    assert protocol.validate_compact_stage_a_output(valid, request) == valid

    # A caller cannot launder a greeting into a primary row by changing only
    # the provider-facing role bit.  The validator keeps a stable diagnostic
    # even for this deliberately forged in-memory request.
    forged = {**request, "h": [dict(row) for row in request["h"]]}
    forged["h"][1]["r"] = "p"
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
        protocol.validate_compact_stage_a_output(
            _response(
                {
                    "topic_id": "forged-greeting",
                    "primary_message_ids": ["m2"],
                    "context_message_ids": [],
                    "uncertainty": "unknown",
                }
            ),
            forged,
        )
    assert exc_info.value.code == "primary_alias_not_semantic_eligible"
    assert exc_info.value.validation_categories == ("selection",)


def test_typed_media_and_empty_authority_cues_are_context_without_direct_caption() -> None:
    """Transport payloads/metadata cannot launder non-text rows into primary."""

    def handle(name: str) -> str:
        return f"{ACCOUNT}/{CHAT}|message|k22-{name}"

    rows = [
        {"handle": handle("xml"), "message_type": "image", "role": "primary", "content": "<image src='synthetic'/>"},
        {"handle": handle("image"), "message_type": "image", "role": "primary", "text": "[图片]"},
        {"handle": handle("file"), "message_type": "file", "role": "primary", "content": '{"type":"file","name":"synthetic"}'},
        {"handle": handle("card"), "message_type": "card", "role": "primary", "description": "card metadata"},
        {"handle": handle("system"), "message_type": "system", "role": "primary", "content": '{"system_event":"synthetic"}'},
        {"handle": handle("event"), "message_type": "event", "role": "primary", "is_placeholder": True, "content": "<event/>"},
        {"handle": handle("authority"), "role": "empty_authority", "description": "metadata description"},
        {"handle": handle("placeholder"), "role": "media_placeholder", "text": "<media placeholder>"},
        {"handle": handle("reaction"), "role": "reaction", "text": "👍"},
        {"handle": handle("caption"), "message_type": "image", "role": "mixed", "caption": "截图显示接口恢复"},
    ]
    request = protocol.build_compact_stage_a_request(SCOPE, rows)
    by_name = {row["h"].rsplit("|", 1)[-1].split("-", 1)[-1]: row for row in request["h"]}
    for name in ("xml", "image", "file", "card", "system", "event", "authority", "placeholder", "reaction"):
        assert by_name[name]["r"] == "c"
        assert by_name[name]["x"] == ""
    assert by_name["caption"]["r"] == "p"
    assert by_name["caption"]["x"] == "截图显示接口恢复"


def test_serialized_and_markup_cues_are_not_semantically_eligible() -> None:
    """The cue gate rejects raw XML/JSON even when the text is non-empty."""

    for cue in (
        "<message type='image'>synthetic</message>",
        '{"message_type":"file","body":"synthetic"}',
        "[图片]",
        "...",
    ):
        request = protocol.build_compact_stage_a_request(
            SCOPE,
            [
                {"handle": _handle("m", 200), "role": "primary", "text": "synthetic real topic"},
                {"handle": _handle("m", 201), "role": "primary", "text": cue},
            ],
        )
        rows = request["h"]
        assert rows[0]["r"] == "p"
        assert rows[1]["r"] == "c"


def test_unresolved_reference_ceiling_allows_only_non_certain_uncertainty() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [{"handle": _handle("m", 0), "role": "primary", "text": "这个接口现在是什么状态"}],
        [{"handle": _handle("c", 0)}],
        uncertainty_ceiling="uncertain",
    )
    assert request["u"] == "uncertain"
    certain = _response(
        {
            "topic_id": "overstated",
            "primary_message_ids": ["m1"],
            "context_message_ids": [],
            "uncertainty": "certain",
        }
    )
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
        protocol.validate_compact_stage_a_output(certain, request)
    assert exc_info.value.code == "uncertainty_overstated_unresolved_reference"
    assert exc_info.value.validation_categories == ("enum",)
    for level in ("unknown", "uncertain"):
        accepted = _response(
            {
                "topic_id": level,
                "primary_message_ids": ["m1"],
                "context_message_ids": [],
                "uncertainty": level,
            }
        )
        assert protocol.validate_compact_stage_a_output(accepted, request) == accepted


def test_primary_exactly_once_context_unique_and_topic_limit_is_derived() -> None:
    request = _request(primary_count=3, context_count=2, candidate_count=2)
    response = _response(
        {
            "topic_id": "t1",
            "primary_message_ids": ["m1", "m2"],
            "context_message_ids": ["m4"],
            "uncertainty": "certain",
        },
        {
            "topic_id": "t2",
            "primary_message_ids": ["m3"],
            "context_message_ids": ["m5"],
            "uncertainty": "unknown",
        },
    )
    result = protocol.validate_compact_stage_a_output(response, request)
    assert protocol.topic_limit_for_request(request) == 3
    assert len(result["topics"]) == 2
    primary = [item for topic in result["topics"] for item in topic["primary_message_ids"]]
    context = [item for topic in result["topics"] for item in topic["context_message_ids"]]
    assert sorted(primary) == ["m1", "m2", "m3"]
    assert len(context) == len(set(context)) == 2

    # Context-only messages can support a topic, but cannot create one.
    _expect_protocol_error(
        protocol.validate_compact_stage_a_output,
        _response(
            {
                "topic_id": "context-only",
                "primary_message_ids": ["m4"],
                "context_message_ids": [],
                "uncertainty": "unknown",
            }
        ),
        request,
    )


def test_v2_short_keys_extra_missing_duplicate_and_stage_b_fields_are_rejected() -> None:
    request = _simple_request()
    valid = _response(
        {
            "topic_id": "t1",
            "primary_message_ids": ["m2"],
            "context_message_ids": ["m1"],
            "uncertainty": "unknown",
        },
        {
            "topic_id": "t2",
            "primary_message_ids": ["m3"],
            "context_message_ids": ["m4"],
            "uncertainty": "unknown",
        },
    )
    _expect_protocol_error(
        protocol.validate_compact_stage_a_output,
        {"t": [{"i": "t1", "p": ["m2"], "c": ["m1"], "u": "unknown"}]},
        request,
    )
    for malformed in (
        {"topics": [{**valid["topics"][0], "extra": True}, valid["topics"][1]]},
        {"topics": [{key: value for key, value in valid["topics"][0].items() if key != "uncertainty"}, valid["topics"][1]]},
        {"topics": [{**valid["topics"][0], "speaker": "m2"}, valid["topics"][1]]},
        {"topics": [{**valid["topics"][0], "claim_type": "fact"}, valid["topics"][1]]},
    ):
        _expect_protocol_error(protocol.validate_compact_stage_a_output, malformed, request)

    duplicate_json = (
        '{"topics":[{"topic_id":"t1","primary_message_ids":["m2"],'
        '"context_message_ids":["m1"],"uncertainty":"unknown"}],'
        '"topics":[]}'
    )
    _expect_protocol_error(protocol.parse_compact_stage_a_output, duplicate_json, request)


def test_forged_alias_cross_scope_and_ambiguous_membership_fail_closed() -> None:
    request = _simple_request()
    valid_topic = {
        "topic_id": "t1",
        "primary_message_ids": ["m2"],
        "context_message_ids": ["m1"],
        "uncertainty": "unknown",
    }
    _expect_protocol_error(
        protocol.validate_compact_stage_a_output,
        _response({**valid_topic, "primary_message_ids": ["m99"]},
                  {"topic_id": "t2", "primary_message_ids": ["m3"], "context_message_ids": ["m4"], "uncertainty": "unknown"}),
        request,
    )
    _expect_protocol_error(
        protocol.validate_compact_stage_a_output,
        _response(valid_topic,
                  {"topic_id": "t2", "primary_message_ids": ["m3"], "context_message_ids": ["m1"], "uncertainty": "unknown"}),
        request,
    )
    _expect_protocol_error(
        protocol.validate_compact_stage_a_output,
        _response(valid_topic,
                  {"topic_id": "t2", "primary_message_ids": ["m2"], "context_message_ids": ["m4"], "uncertainty": "unknown"}),
        request,
    )

    _expect_protocol_error(
        protocol.build_compact_stage_a_request,
        SCOPE,
        [{"handle": _handle("m", 0, account=OTHER_ACCOUNT, chat=OTHER_CHAT), "role": "primary", "text": "cross scope"}],
        [],
    )


def test_versioned_cache_hashes_are_separate_and_request_handles_are_single_copy() -> None:
    request = _request(primary_count=2, context_count=1, candidate_count=1)
    request_text = protocol.canonical_json(request)
    for row in request["h"]:
        assert request_text.count(row["h"]) == 1
        assert len(row["i"]) <= 8

    v3_context = protocol.cache_context(request, model_id="deepseek-v4-flash", ruleset_version="k22")
    v3_key = protocol.cache_key(request, model_id="deepseek-v4-flash", ruleset_version="k22")
    assert v3_context["cache_version"] == protocol.CACHE_VERSION
    assert v3_context["cache_namespace"] == protocol.CACHE_NAMESPACE
    assert v3_context["protocol_version"] == protocol.PROTOCOL_VERSION
    assert v3_key == protocol.stable_hash(v3_context)

    # Historical v1/v2 material must not share the v3 cache namespace/key,
    # even when it points at the same synthetic request hash.
    v1_key = protocol.stable_hash(
        {
            "cache_namespace": "compact-stage-a-v1",
            "protocol_version": "stage_a_topic_assignment_compact_v1",
            "prompt_version": "stage_a_topic_assignment_compact_prompt_v1",
            "request_sha256": protocol.stable_hash(request),
        }
    )
    v2_request = v2.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 0), "role": "context", "text": "你好"},
            {"handle": _handle("m", 1), "role": "primary", "text": "topic"},
            {"handle": _handle("m", 2), "role": "primary", "text": "topic 2"},
        ],
        [{"handle": _handle("c", 0)}],
    )
    v2_key = protocol.stable_hash(
        {
            "cache_namespace": "compact-stage-a-v2",
            "protocol_version": v2.PROTOCOL_VERSION,
            "prompt_version": v2.PROMPT_VERSION,
            "request_sha256": v2.stable_hash(v2_request),
        }
    )
    assert len({v1_key, v2_key, v3_key}) == 3
    assert protocol.CACHE_NAMESPACE not in {"compact-stage-a-v1", "compact-stage-a-v2"}


def test_body_free_ledger_contains_only_handles_counts_hashes_and_validated_assignments() -> None:
    request = _simple_request()
    response = _response(
        {
            "topic_id": "t1",
            "primary_message_ids": ["m2"],
            "context_message_ids": ["m1"],
            "uncertainty": "unknown",
        },
        {
            "topic_id": "t2",
            "primary_message_ids": ["m3"],
            "context_message_ids": ["m4"],
            "uncertainty": "unknown",
        },
    )
    ledger = protocol.project_body_free_ledger(request, response)
    encoded = _json(ledger)
    assert BODY_MARKER not in encoded
    for forbidden in ("text", "cue", "content", "body", "speaker", "subject", "object", "claim", "state"):
        assert f'"{forbidden}"' not in encoded
    assert ledger["message_count"] == 4
    assert ledger["candidate_count"] == 1
    assert ledger["topic_count"] == 2
    assert len(ledger["request_sha256"]) == 64


def test_grouping_preflight_supports_same_topic_multiple_primaries_without_deciding() -> None:
    """Shared slots/reply edges produce compact hints, not topic assignments."""

    rows = [
        {
            "handle": _handle("m", 30),
            "role": "primary",
            "text": "部署状态是什么",
            "subject_id": "service-1",
            "object_id": "release-1",
            "action": "inspect",
        },
        {
            "handle": _handle("m", 31),
            "role": "primary",
            "text": "现在已经恢复了吗",
            "subject_id": "service-1",
            "object_id": "release-1",
            "action": "inspect",
            "reply_to_message_id": _handle("m", 30),
        },
        {
            "handle": _handle("m", 32),
            "role": "context",
            "text": "你好",
            "fragment_type": "greeting",
            "topic_shift": True,
        },
    ]
    candidate = {
        "handle": _handle("c", 30),
        "left_message_id": _handle("m", 30),
        "relation": "question_answer",
    }
    request = protocol.build_compact_stage_a_request(SCOPE, rows, [candidate])
    assert protocol.TOPIC_GROUPING_GUIDANCE in protocol.SYSTEM_PROMPT
    assert "do not create one topic per message" in protocol.SYSTEM_PROMPT
    assert "candidate cues are context hints only" in protocol.SYSTEM_PROMPT
    assert request["g"] == protocol.build_topic_candidate_hints(
        [dict(row, i="m%d" % (index + 1), k="m", h=row["handle"], r="p" if row.get("role") == "primary" else "c") for index, row in enumerate(rows)],
        [dict(candidate, i="c1", k="c", h=candidate["handle"])],
    )
    assert any(item["a"] == "m1" and item["b"] == "m2" for item in request["g"])
    assert any(item["r"] == "candidate_qa" for item in request["g"])

    preflight = protocol.preflight_compact_stage_a_request(request)
    assert preflight["model_decision_required"] is True
    assert preflight["hints_are_structural"] is True
    assert preflight["primary_aliases"] == ["m1", "m2"]
    response = _response(
        {
            "topic_id": "one-topic",
            "primary_message_ids": ["m1", "m2"],
            "context_message_ids": ["m3"],
            "uncertainty": "unknown",
        }
    )
    assert protocol.validate_compact_stage_a_output(response, request) == response


def test_context_handles_are_context_set_only_and_never_repaired_or_coerced() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 33), "role": "primary", "text": "真实主题"},
            {"handle": _handle("m", 34), "role": "context", "text": "收到"},
        ],
        [{"handle": _handle("c", 33)}],
    )
    valid = _response(
        {
            "topic_id": "t1",
            "primary_message_ids": ["m1"],
            "context_message_ids": ["m2"],
            "uncertainty": "unknown",
        }
    )
    assert protocol.validate_compact_stage_a_output(valid, request) == valid
    for illegal_context in ("m1", "c1"):
        with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
            protocol.validate_compact_stage_a_output(
                _response(
                    {
                        "topic_id": "illegal-context",
                        "primary_message_ids": ["m1"],
                        "context_message_ids": [illegal_context],
                        "uncertainty": "unknown",
                    }
                ),
                request,
            )
        assert exc_info.value.code == "output_context_item"


def test_grouping_hint_validator_rejects_invalid_duplicate_and_coerced_values() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 35), "role": "primary", "text": "主题一"},
            {"handle": _handle("m", 36), "role": "primary", "text": "主题二"},
        ],
    )
    for malformed, expected in (
        ({"g": ({"a": "m1", "b": "m2", "r": "same_subject"},)}, "request_grouping_hints"),
        ({"g": [{"a": "m1", "b": "m2", "r": "same_subject"}, {"a": "m1", "b": "m2", "r": "same_subject"}]}, "request_grouping_hint_duplicate"),
        ({"g": [{"a": "m1", "b": "m99", "r": "same_subject"}]}, "request_grouping_hint_alias"),
        ({"g": [{"a": 1, "b": "m2", "r": "same_subject"}]}, "request_grouping_hint_alias"),
    ):
        forged = {**request, **malformed}
        with pytest.raises(protocol.CompactStageAProtocolV3Error) as exc_info:
            protocol.validate_compact_stage_a_request(forged)
        assert exc_info.value.code == expected


def test_system_reaction_no_reply_pronoun_and_greeting_shift_stay_structural_context() -> None:
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [
            {"handle": _handle("m", 37), "role": "primary", "text": "它现在是什么状态", "subject_id": "service-2", "pronoun": True, "no_reply": True},
            {"handle": _handle("m", 38), "role": "primary", "text": "部署失败如何恢复", "subject_id": "service-3", "topic_shift": True},
            {"handle": _handle("m", 39), "role": "primary", "message_type": "system", "text": "系统通知"},
            {"handle": _handle("m", 40), "role": "primary", "message_type": "reaction", "text": "👍"},
            {"handle": _handle("m", 41), "role": "context", "text": "你好", "fragment_type": "greeting", "topic_shift": True},
        ],
    )
    by_alias = {row["i"]: row for row in request["h"] if row["k"] == "m"}
    assert by_alias["m1"]["r"] == "p"
    assert by_alias["m2"]["r"] == "p"
    assert by_alias["m3"]["r"] == "c"
    assert by_alias["m4"]["r"] == "c"
    assert by_alias["m5"]["r"] == "c"
    assert not any({item["a"], item["b"]} == {"m1", "m2"} for item in request.get("g", []))
    response = _response(
        {
            "topic_id": "pronoun",
            "primary_message_ids": ["m1"],
            "context_message_ids": ["m5"],
            "uncertainty": "unknown",
        },
        {
            "topic_id": "shift",
            "primary_message_ids": ["m2"],
            "context_message_ids": [],
            "uncertainty": "unknown",
        },
    )
    assert protocol.validate_compact_stage_a_output(response, request) == response


def test_dense_page_hints_are_sparse_family_witnesses_and_stay_under_http_budget() -> None:
    """A 24-plus-edge page keeps representative families without dropping rows."""

    def handle(kind: str, index: int) -> str:
        return _handle(kind, index)

    metadata = [
        {"subject_id": "subject-1"},
        {"subject_id": "subject-1"},
        {"object_id": "object-1"},
        {"object_id": "object-1"},
        {"action": "action-1"},
        {"action": "action-1"},
        {"subject_id": "state-subject", "state": "open"},
        {"subject_id": "state-subject", "state": "closed"},
        {"subject_id": "qa-subject", "fragment_type": "question", "relation": "question_answer"},
        {"subject_id": "qa-subject", "fragment_type": "answer", "relation": "question_answer"},
        {"subject_id": "reply-subject"},
        {"subject_id": "reply-subject", "reply_to_message_id": handle("m", 10)},
        {},
        {},
    ]
    messages = []
    for index, extra in enumerate(metadata):
        row = {
            "handle": handle("m", index),
            "role": "primary",
            "text": "topic-%02d-%s" % (index, "x" * 24),
        }
        row.update(extra)
        messages.append(row)
    candidates = [
        {
            "handle": handle("c", index),
            "left_message_id": handle("m", index % len(messages)),
            "relation": "question_answer" if index % 2 == 0 else "related",
        }
        for index in range(20)
    ]

    hints = protocol.build_topic_candidate_hints(messages, candidates)
    assert len(hints) <= protocol.MAX_GENERATED_TOPIC_GROUPING_HINTS
    assert len(hints) < protocol.MAX_TOPIC_GROUPING_HINTS
    relation_families = {relation for hint in hints for relation in hint["r"].split("+")}
    assert {
        "same_subject",
        "same_object",
        "same_action",
        "state_update",
        "qa_continuity",
        "reply_continuity",
        "candidate_context",
        "candidate_qa",
    } <= relation_families
    family_counts = {
        relation: sum(relation in hint["r"].split("+") for hint in hints)
        for relation in protocol.TOPIC_HINT_RELATION_ORDER
    }
    assert max(family_counts.values()) <= protocol.MAX_TOPIC_HINTS_PER_RELATION

    request = protocol.build_compact_stage_a_request(SCOPE, messages, candidates)
    emitted_hints = request.get(protocol.GROUPING_HINTS_FIELD, [])
    # The HTTP fit pass removes only the deterministic tail; source rows and
    # their bounded cues remain intact even when this deliberately long
    # synthetic page can afford only a subset of the structural hints.
    assert emitted_hints == hints[: len(emitted_hints)]
    assert len(emitted_hints) < len(hints)
    assert emitted_hints and "same_subject" in emitted_hints[0]["r"]
    assert protocol.measure_wire_size(request).http_token_proxy <= protocol.MAX_INPUT_TOKEN_PROXY


def test_placeholder_cues_stay_context_and_event_caption_exception_is_narrow() -> None:
    """Direct text cannot override an explicit placeholder or weak event role."""

    assert "reaction/system/event/media placeholders stay context" in protocol.SYSTEM_PROMPT
    assert "non-placeholder direct human caption" in protocol.SYSTEM_PROMPT
    rows = [
        {"handle": _handle("m", 42), "role": "primary", "text": "真实主题"},
        {
            "handle": _handle("m", 43),
            "message_type": "image",
            "role": "primary",
            "is_placeholder": True,
            "caption": "占位图上的文字不能建题",
        },
        {
            "handle": _handle("m", 44),
            "message_type": "event",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "placeholder": True,
            "caption": "占位事件上的文字不能建题",
        },
        {
            "handle": _handle("m", 45),
            "message_type": "event",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "caption": "权威人类事件说明可作主候选",
        },
        {
            "handle": _handle("m", 46),
            "message_type": "event",
            "role": "primary",
            "caption": "缺少权威角色的事件说明",
        },
        {
            "handle": _handle("m", 47),
            "message_type": "reaction",
            "role": "primary",
            "caption": "反应文字不能建题",
        },
    ]
    request = protocol.build_compact_stage_a_request(SCOPE, rows)
    by_alias = {row["i"]: row for row in request["h"]}
    assert by_alias["m1"]["r"] == "p"
    assert by_alias["m2"]["r"] == "c"
    assert by_alias["m3"]["r"] == "c"
    assert by_alias["m4"]["r"] == "p"
    assert by_alias["m5"]["r"] == "c"
    assert by_alias["m6"]["r"] == "c"
