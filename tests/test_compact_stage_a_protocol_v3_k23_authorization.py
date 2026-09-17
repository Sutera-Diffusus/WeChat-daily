"""Independent offline K23 authorization audit for the compact Stage-A v3 wire.

This file is deliberately a side-car audit, not a provider or runner test.  It
uses synthetic records only, reads only the two existing body-free v1/v2 audit
summaries, and keeps all provider/private/frozen/development access out of the
test process.  A green run authorizes at most one *new* synthetic health-only
call; it never authorizes development input, Stage B/C, or production.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from wechat_bridge import compact_stage_a_protocol_v3 as protocol
from wechat_bridge import compact_stage_a_protocol_health_v3 as health_v3
from wechat_bridge.persistent_call_budget import CallAuthorizationLedger, CallBudgetExceeded


ROOT = Path(__file__).resolve().parents[1]
LOCAL_DAY = "2026-08-25"
K23_AUTHORIZATION_ID = "K23_COMPACT_STAGE_A_HEALTH_V3"
SCOPE = {"account_id": "account-k23-synthetic", "chat_id": "chat-k23-synthetic"}
OTHER_SCOPE = {"account_id": "account-k23-other", "chat_id": "chat-k23-other"}
BODY_MARKER = "K23_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"

V1_AUDIT = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / "compact_stage_a_protocol_health_v1"
    / "audit"
    / "audit_summary.private.json"
)
V2_AUDIT = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / "compact_stage_a_protocol_health_v2"
    / "audit"
    / "audit_summary.private.json"
)

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "evidence_text",
        "html",
        "markdown",
        "message",
        "messages",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_response",
        "raw_text",
        "request",
        "response",
        "response_body",
        "text",
        "text_body",
        "user_input",
    }
)
REF_SUFFIXES = ("_ref", "_refs", "_handle", "_handles")
STAGE_B_FIELDS = frozenset(
    {
        "speaker",
        "subject",
        "mentioned_person",
        "target",
        "object",
        "action",
        "claim_type",
        "state",
        "modality",
        "coreference_candidates",
        "context_relations",
        "uncertainties",
        "evidence",
        "claim",
    }
)
FORBIDDEN_IMPORTS = frozenset(
    {
        "openai",
        "requests",
        "httpx",
        "urllib",
        "urllib.request",
        "wechat_bridge.linear_stage_a_development_pilot",
        "wechat_bridge.linear_stage_a_pilot",
    }
)


def _handle(kind: str, index: int, *, scope: Mapping[str, str] = SCOPE) -> str:
    noun = "message" if kind == "m" else "candidate"
    return f"{scope['account_id']}/{scope['chat_id']}|{noun}|k23-{index:03d}"


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


def _small_request() -> dict[str, Any]:
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


def _valid_response(request: Mapping[str, Any]) -> dict[str, Any]:
    primary = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
    context = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "c"]
    return _response(
        {
            "topic_id": "topic-alpha",
            "primary_message_ids": primary[: max(1, len(primary) // 2)],
            "context_message_ids": context[:1],
            "uncertainty": "unknown",
        },
        {
            "topic_id": "topic-beta",
            "primary_message_ids": primary[max(1, len(primary) // 2) :],
            "context_message_ids": context[1:2],
            "uncertainty": "uncertain",
        },
    )


def _expect_rejected(call: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(protocol.CompactStageAProtocolV3Error) as raised:
        call(*args, **kwargs)
    return raised.value.code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_body_free(value: Any) -> None:
    encoded = _json(value)
    assert BODY_MARKER not in encoded

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).casefold()
                if key in BODY_KEYS and child not in (None, "", [], {}, ()):
                    assert key.endswith(REF_SUFFIXES), f"body-bearing key escaped: {key}"
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)


def _read_previous_audit(path: Path) -> Mapping[str, Any]:
    # These are aggregate audit summaries only.  No artifact body, message,
    # provider response, frozen data, or development input is opened here.
    assert path.exists(), f"required prior body-free audit summary missing: {path}"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, Mapping)
    assert value.get("audit_status") == "pass"
    assert value.get("artifact_status") == "blocked"
    assert value.get("health_complete") is False
    privacy = value.get("privacy")
    assert isinstance(privacy, Mapping)
    assert privacy.get("body_free") is True
    assert all(privacy.get(key) == 0 for key in ("body_key_hits", "identity_key_hits", "reasoning_key_hits", "secret_key_hits"))
    next_step = value.get("next_step")
    assert isinstance(next_step, Mapping)
    assert next_step.get("allow_development_input") is False
    assert next_step.get("allow_stage_b") is False
    assert next_step.get("allow_stage_c") is False
    _assert_body_free(value)
    return value


def _module_import_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_v1_v2_body_free_failures_are_not_reused_as_v3_authorization() -> None:
    v1 = _read_previous_audit(V1_AUDIT)
    v2 = _read_previous_audit(V2_AUDIT)
    ids = {
        v1.get("authorization_ledger", {}).get("authorization_id"),
        v2.get("authorization_ledger", {}).get("authorization_id"),
    }
    assert K23_AUTHORIZATION_ID not in ids
    assert v1.get("next_step", {}).get("new_authorization_required") is True
    assert v2.get("next_step", {}).get("new_authorization_required") is True


def test_v3_is_explicit_full_name_stage_a_wire_with_minimal_exemplar() -> None:
    required = {
        "topics",
        "topic_id",
        "primary_message_ids",
        "context_message_ids",
        "uncertainty",
    }
    assert protocol.OUTPUT_TOP_KEYS == frozenset({"topics"})
    assert protocol.OUTPUT_TOPIC_KEYS == frozenset(required - {"topics"})
    assert required <= set(protocol.RESPONSE_SCHEMA.replace("{", " ").replace("}", " ").replace(":", " ").split()) | required
    assert "Minimal valid example" in protocol.SYSTEM_PROMPT
    assert "{t:" not in protocol.RESPONSE_SCHEMA
    request = protocol.build_compact_stage_a_request(
        SCOPE,
        [{"handle": _handle("m", 0), "role": "primary", "text": "one"}],
        [],
    )
    parsed = protocol.parse_compact_stage_a_output(protocol.MINIMAL_RESPONSE_EXAMPLE, request)
    assert set(parsed) == {"topics"}
    assert set(parsed["topics"][0]) == required - {"topics"}


def test_v3_worst_case_http_and_response_budgets_are_hard_limits() -> None:
    request = _request()
    assert protocol.validate_compact_stage_a_request(request) == request
    proof = protocol.max_size_proof(request)
    assert proof["message_count"] == 14
    assert proof["candidate_count"] == 20
    assert proof["request_within_1600"] is True
    assert proof["response_within_400"] is True
    assert proof["request"]["http_token_proxy"] <= 1600
    assert proof["response"]["token_proxy"] <= 400
    response = protocol.build_max_size_response(request)
    assert protocol.measure_output_size(response)["token_proxy"] <= 400


def test_topic_primary_context_and_no_reply_constraints_fail_closed() -> None:
    request = _small_request()
    valid = _response(
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
    checked = protocol.validate_compact_stage_a_output(valid, request)
    resolved = protocol.resolve_compact_output(checked, request)
    assert resolved["topics"][0]["primary_message_ids"] == [_handle("m", 1)]
    assert resolved["topics"][0]["context_message_ids"] == [_handle("m", 0)]
    assert protocol.topic_limit_for_request(request) == 2

    assert _expect_rejected(
        protocol.validate_compact_stage_a_output,
        _response(
            {
                "topic_id": "context-only",
                "primary_message_ids": ["m1"],
                "context_message_ids": [],
                "uncertainty": "unknown",
            }
        ),
        request,
    ) == "output_primary_item"
    assert _expect_rejected(
        protocol.validate_compact_stage_a_output,
        _response(
            {
                "topic_id": "too-many",
                "primary_message_ids": ["m2"],
                "context_message_ids": ["m1", "m4"],
                "uncertainty": "unknown",
            },
            {
                "topic_id": "duplicate-context",
                "primary_message_ids": ["m3"],
                "context_message_ids": ["m4"],
                "uncertainty": "unknown",
            },
        ),
        request,
    ) == "duplicate_context_handle"

    ledger = protocol.project_body_free_ledger(request, checked)
    assert ledger["candidate_count"] == 1
    assert ledger["topic_count"] == 2
    _assert_body_free(ledger)


def test_exact_keys_stage_b_fields_forgery_scope_and_duplicates_are_rejected() -> None:
    request = _small_request()
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
    for field in sorted(STAGE_B_FIELDS):
        malformed = {"topics": [{**valid["topics"][0], field: "forged"}, valid["topics"][1]]}
        assert _expect_rejected(protocol.validate_compact_stage_a_output, malformed, request) == "output_topic_keys"

    assert _expect_rejected(
        protocol.validate_compact_stage_a_output,
        _response(
            {**valid["topics"][0], "primary_message_ids": ["m99"]},
            valid["topics"][1],
        ),
        request,
    ) == "output_primary_item"
    assert _expect_rejected(
        protocol.validate_compact_stage_a_output,
        _response(valid["topics"][0], {**valid["topics"][1], "context_message_ids": ["m1"]}),
        request,
    ) == "duplicate_context_handle"
    assert _expect_rejected(
        protocol.validate_compact_stage_a_output,
        {"topics": [{**valid["topics"][0], "topic_id": "same"}, {**valid["topics"][1], "topic_id": "same"}]},
        request,
    ) == "duplicate_topic_id"
    duplicate_json = (
        '{"topics":[{"topic_id":"t1","primary_message_ids":["m2"],'
        '"context_message_ids":["m1"],"uncertainty":"unknown"}],"topics":[]}'
    )
    assert _expect_rejected(protocol.parse_compact_stage_a_output, duplicate_json, request) == "duplicate_json_key"

    cross_scope_message = [{"handle": _handle("m", 0, scope=OTHER_SCOPE), "role": "primary", "text": "x"}]
    assert _expect_rejected(protocol.build_compact_stage_a_request, SCOPE, cross_scope_message, []) == "cross_scope_handle"
    assert _expect_rejected(
        protocol.build_compact_stage_a_request,
        SCOPE,
        [{"handle": _handle("m", 0), "role": "primary", "text": "x"}],
        [{"handle": _handle("m", 0)}],
    ) == "handle_kind_mismatch"


def test_v3_cache_hashes_are_version_and_content_sensitive() -> None:
    request = _request(primary_count=2, context_count=1, candidate_count=1)
    context = protocol.cache_context(request, model_id="deepseek-v4-flash", ruleset_version="k23")
    key = protocol.cache_key(request, model_id="deepseek-v4-flash", ruleset_version="k23")
    assert context["cache_namespace"] == protocol.CACHE_NAMESPACE
    assert context["protocol_version"] == protocol.PROTOCOL_VERSION
    assert context["prompt_version"] == protocol.PROMPT_VERSION
    assert context["request_sha256"] == protocol.stable_hash(request)
    assert key == protocol.stable_hash(context)

    changed = json.loads(protocol.canonical_json(request))
    changed["h"][0]["x"] = "different cue"
    assert protocol.cache_key(changed, model_id="deepseek-v4-flash", ruleset_version="k23") != key
    assert protocol.cache_key(request, model_id="deepseek-v4-pro", ruleset_version="k23") != key
    assert protocol.cache_key(request, model_id="deepseek-v4-flash", ruleset_version="k24") != key
    old_keys = {
        protocol.stable_hash(
            {
                "cache_namespace": "compact-stage-a-v1",
                "protocol_version": "stage_a_topic_assignment_compact_v1",
                "prompt_version": "stage_a_topic_assignment_compact_prompt_v1",
                "request_sha256": protocol.stable_hash(request),
            }
        ),
        protocol.stable_hash(
            {
                "cache_namespace": "compact-stage-a-v2",
                "protocol_version": "stage_a_topic_assignment_compact_v2",
                "prompt_version": "stage_a_topic_assignment_compact_prompt_v2",
                "request_sha256": protocol.stable_hash(request),
            }
        ),
    }
    assert key not in old_keys


def test_v3_and_health_v3_are_offline_only_and_health_contract_is_explicit() -> None:
    protocol_names = _module_import_names(inspect.getsource(protocol))
    health_names = _module_import_names(inspect.getsource(health_v3))
    assert protocol_names.isdisjoint(FORBIDDEN_IMPORTS)
    assert health_names.isdisjoint(FORBIDDEN_IMPORTS)
    health_source = inspect.getsource(health_v3)
    assert ".read_text(" not in health_source
    # ``read_bytes`` is used only to hash files that this synthetic facade
    # has just written; it is not an input/development/private read.
    assert "open(" not in health_source
    assert health_v3.PROTOCOL_VERSION == protocol.PROTOCOL_VERSION
    assert health_v3.PROMPT_VERSION == protocol.PROMPT_VERSION
    assert health_v3.RESPONSE_FORMAT_MODE == "omitted"
    assert health_v3.THINKING_DISABLED is True
    assert health_v3.MAX_PROVIDER_CALLS == 1
    assert health_v3.MAX_RETRIES == 0
    assert health_v3.HEALTH_MAX_OUTPUT_TOKENS == 400

    request = health_v3.build_synthetic_health_request()
    content = health_v3.FakeCompactStageAHealthV3Model.valid_content(request)
    parsed = protocol.parse_compact_stage_a_output(content, request)
    assert parsed["topics"]
    fake = health_v3.FakeCompactStageAHealthV3Model()
    response = fake.complete(
        protocol.SYSTEM_PROMPT,
        request,
        max_output_tokens=400,
        extra_body={"thinking": {"type": "disabled"}},
    )
    assert fake.calls[0]["max_output_tokens"] == 400
    assert fake.calls[0]["extra_body_field_names"] == ["thinking"]
    assert response.finish_reason == "stop"
    assert response.reasoning_length == 0
    _assert_body_free(fake.calls)


def test_global_ledger_can_guard_the_new_authorization_without_touching_real_state(tmp_path: Path) -> None:
    request = _request(primary_count=1, context_count=0, candidate_count=1)
    input_hash = protocol.stable_hash(request)
    ledger = CallAuthorizationLedger.for_authorization(
        tmp_path / "authority",
        authorization_id=K23_AUTHORIZATION_ID,
        max_calls=1,
        provider="openai-compatible",
        model="deepseek-v4-flash",
        protocol=protocol.PROTOCOL_VERSION,
        settings_sha256=hashlib.sha256(b"synthetic-settings").hexdigest(),
        scope=SCOPE,
        input_sha256=input_hash,
        artifact_namespace="compact-stage-a-health-v3",
    )
    reservation = ledger.reserve(
        request,
        unit_ref="synthetic-health-only",
        attempt=0,
        input_tokens_estimate=protocol.MAX_INPUT_TOKEN_PROXY,
    )
    ledger.mark_failed(reservation, error_code="provider_invalid_json")
    reopened = CallAuthorizationLedger.for_authorization(
        tmp_path / "authority",
        authorization_id=K23_AUTHORIZATION_ID,
        max_calls=1,
        provider="openai-compatible",
        model="deepseek-v4-flash",
        protocol=protocol.PROTOCOL_VERSION,
        settings_sha256=hashlib.sha256(b"synthetic-settings").hexdigest(),
        scope=SCOPE,
        input_sha256=input_hash,
        artifact_namespace="compact-stage-a-health-v3",
    )
    with pytest.raises(CallBudgetExceeded):
        reopened.reserve(request, unit_ref="synthetic-health-only", attempt=1)
    snapshot = reopened.snapshot()
    assert snapshot["authorization_id"] == K23_AUTHORIZATION_ID
    assert snapshot["calls_used"] == 1
    assert snapshot["calls_remaining"] == 0
    assert snapshot["status_counts"] == {"failed": 1}
    assert snapshot["body_free"] is True
    _assert_body_free(snapshot)
