"""Synthetic K25 contract tests.

No provider, development artifact, or frozen path is opened by this suite.
The fake model is deliberately injected so a regression cannot accidentally
turn the test into a network call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from wechat_bridge.compact_stage_a_development_pilot_v3 import (
    ADAPTER_FIX_AUTHORIZATION_ID,
    ALLOWED_AUTHORIZATION_IDS,
    AUTHORIZATION_ID,
    AUTHORITY_BOUND_AUTHORIZATION_ID,
    AUTHORITY_BOUND_CANONICAL_MAPPING,
    AUTHORITY_BOUND_SETTINGS_SHA256,
    AUTHORITY_BOUND_SCOPE,
    GUARDED_AUTHORIZATION_ID,
    GUARD_VALIDATION_ARTIFACT_VERSION,
    GUARD_VALIDATION_AUTHORIZATION_ID,
    GUARD_VALIDATION_ERROR_TAXONOMY,
    GUARD_VALIDATION_MAX_PROVIDER_CALLS,
    GUARD_VALIDATION_MAX_SELECTED_PAGES,
    GUARD_VALIDATION_PROMPT_VERSION,
    GUARD_VALIDATION_SUBSET_SHA256,
    GUARD_VALIDATION_TAXONOMY_VERSION,
    CATEGORY_NAMES,
    CompactStageAResponse,
    CompactStageADevelopmentError,
    PageRun,
    PRIMARY_CONTEXT_FIX_AUTHORIZATION_ID,
    error_category_for_code,
    SYSTEM_PROMPT,
    INPUT_ARTIFACT_VERSION,
    _build_request,
    _normalize_mapping_pages,
    _merge_context_packet_bodies,
    _page_has_unresolved_pronoun_candidate,
    _validate_current_linear_audit_root,
    _invoke_model,
    _safe_error_code,
    _safe_public_page,
    _validate_provider_output,
    _guard_subset_hash,
    _selection_plan,
    _synthetic_authority_projection_audit,
    run_compact_stage_a_development_pilot_v3,
    validate_guard_validation_output,
    validation_categories_for_error_code,
)
from wechat_bridge.compact_stage_a_protocol_v3 import (
    CompactStageAProtocolV3Error,
    build_compact_stage_a_request,
    measure_output_size,
    measure_wire_size,
    stable_hash,
    validate_compact_stage_a_output,
)
from wechat_bridge.staged_deepseek_analyzer import OpenAICompatibleStageModel


SCOPE = {"account_id": "k25-account", "chat_id": "k25-chat"}
BODY_MARKER = "K25_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"


def _handle(kind: str, index: int) -> str:
    noun = "message" if kind == "m" else "candidate"
    return f"{SCOPE['account_id']}/{SCOPE['chat_id']}|{noun}|k25-{index:03d}"


def _pages(*, count: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        category = CATEGORY_NAMES[index % len(CATEGORY_NAMES)]
        rows.append(
            {
                "page_id": f"page-k25-{index}",
                "root_id": f"root-k25-{index}",
                "source_packet_id": f"packet-k25-{index}",
                "page_hash": f"{index + 1:064x}",
                "scope": dict(SCOPE),
                "message_handles": [_handle("m", index)],
                "primary_message_handles": [_handle("m", index)],
                # The provider-facing role guard requires a semantic cue.  Keep
                # the body synthetic and local to this fixture; the pilot's
                # public artifacts remain body-free.
                "message_rows": [
                    {
                        "message_handle": _handle("m", index),
                        "message_type": "text",
                        "text": f"synthetic topic {index}",
                    }
                ],
                "candidate_handles": [_handle("c", index)],
                "categories": [category],
                "status": "complete",
            }
        )
    return rows


def _authority_pages() -> list[dict[str, Any]]:
    """Materialize the exact body-bearing synthetic source side of K30.

    The test still keeps message bodies local to the fake model boundary; the
    authority contract only sees these opaque source identities and hashes.
    """

    rows = _pages()
    for page, expected in zip(rows, AUTHORITY_BOUND_CANONICAL_MAPPING):
        def authority_handle(value: Any) -> str:
            suffix = str(value).split("|", 1)[-1]
            return f"{AUTHORITY_BOUND_SCOPE['account_id']}/{AUTHORITY_BOUND_SCOPE['chat_id']}|{suffix}"

        for key in ("message_handles", "primary_message_handles", "candidate_handles"):
            page[key] = [authority_handle(value) for value in page.get(key, ())]
        for row in page.get("message_rows", ()):
            if isinstance(row, dict) and row.get("message_handle"):
                row["message_handle"] = authority_handle(row["message_handle"])
        page.update(
            {
                "page_id": str(expected["source_page_ref"]),
                "root_id": str(expected["source_root_ref"]),
                "source_packet_id": str(expected["source_source_ref"]),
                "page_hash": str(expected["source_page_hash"]),
                "scope": dict(AUTHORITY_BOUND_SCOPE),
                "_selection_rank": int(expected["selection_rank"]),
                "_source_handle": str(expected["source_source_ref"]),
                "_scope_handle": str(expected["source_scope_ref"]),
            }
        )
    return rows


class FakeModel:
    model_id = "deepseek-v4-flash"
    source = "synthetic-k25"

    def __init__(self, *, invalid_on: int | None = None) -> None:
        self.calls = 0
        self.requests: list[Mapping[str, Any]] = []
        self.invalid_on = invalid_on

    def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> Any:
        assert system_prompt
        assert max_output_tokens == 400
        self.calls += 1
        self.requests.append(request)
        if self.invalid_on == self.calls:
            return CompactStageAResponse(
                content='{"topics":[',
                input_tokens=100,
                output_tokens=30,
                latency_ms=1.0,
            )
        primary = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
        context = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "c"]
        return CompactStageAResponse(
            content={
                "topics": [
                    {
                        "topic_id": "k25-topic",
                        "primary_message_ids": primary,
                        "context_message_ids": context,
                        "uncertainty": "unknown",
                    }
                ]
            },
            input_tokens=100,
            output_tokens=30,
            latency_ms=1.0,
        )


def _assert_body_free(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert BODY_MARKER not in encoded
    body_keys = {"body", "content", "message", "prompt", "raw", "response", "text", "user_input"}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in body_keys and child not in (None, "", [], {}):
                    raise AssertionError(f"body key escaped: {key}")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)


def test_development_injection_passes_thinking_disable_only_to_capable_adapter() -> None:
    calls: list[dict[str, Any]] = []

    class Adapter:
        def complete(
            self,
            system_prompt: str,
            request: Mapping[str, Any],
            *,
            max_output_tokens: int,
            extra_body: Mapping[str, Any],
        ) -> str:
            calls.append(
                {
                    "system_prompt": system_prompt,
                    "request": dict(request),
                    "max_output_tokens": max_output_tokens,
                    "extra_body": dict(extra_body),
                }
            )
            return '{"topics":[]}'

    class MinimalFake:
        def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> str:
            assert system_prompt == SYSTEM_PROMPT
            assert request == {"packet": "synthetic"}
            assert max_output_tokens == 400
            return '{"topics":[]}'

    request = {"packet": "synthetic"}
    _invoke_model(Adapter(), request)
    _invoke_model(MinimalFake(), request)
    assert calls == [
        {
            "system_prompt": SYSTEM_PROMPT,
            "request": request,
            "max_output_tokens": 400,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    ]


def test_development_runner_records_adapter_failure_telemetry_without_output_body(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class InvalidCompletions:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return type(
                "Response",
                (),
                {
                    "id": "synthetic-invalid",
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type(
                                    "Message",
                                    (),
                                    {
                                        "content": "not-json",
                                        "reasoning_content": "hidden-synthetic-reasoning",
                                    },
                                )(),
                            },
                        )()
                    ],
                    "usage": type("Usage", (), {"prompt_tokens": 17, "completion_tokens": 19})(),
                },
            )()

    model = OpenAICompatibleStageModel(
        model="deepseek-v4-flash",
        client=type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": InvalidCompletions()})()},
        )(),
    )
    result = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "artifact",
        model=model,
        authority_root=tmp_path / "authority",
        settings_sha256="6" * 64,
    )

    assert result.success is False
    assert result.pending_count == 5
    assert len(calls) == 5
    assert all(call["extra_body"] == {"thinking": {"type": "disabled"}} for call in calls)
    diagnostics = [
        json.loads(line)
        for line in Path(result.artifact_paths["diagnostics"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(diagnostics) == 5
    assert all(row["status"] == "pending" for row in diagnostics)
    assert all(row["error_code"] == "provider_invalid_json" for row in diagnostics)
    assert all(row["input_tokens"] == 17 for row in diagnostics)
    assert all(row["output_tokens"] == 19 for row in diagnostics)
    assert all(row["content_length"] == len("not-json") for row in diagnostics)
    assert all(row["reasoning_length"] == len("hidden-synthetic-reasoning") for row in diagnostics)
    assert all(row["output_sha256"] for row in diagnostics)
    encoded = (tmp_path / "artifact" / "diagnostics.private.jsonl").read_text(encoding="utf-8")
    assert "not-json" not in encoded
    assert "hidden-synthetic-reasoning" not in encoded


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("provider_invalid_json", "json"),
        ("provider_nonstandard_json", "json"),
        ("provider_duplicate_json_key", "json"),
        ("output_topic_keys", "schema_keys"),
        ("duplicate_topic_id", "schema_keys"),
        ("provider_response_shape", "schema_keys"),
        ("request_alias_invalid", "alias"),
        ("output_primary_item", "primary_exactly_once"),
        ("primary_alias_not_semantic_eligible", "selection"),
        ("primary_alias_not_source_primary", "selection"),
        ("duplicate_context_handle", "context_unique"),
        ("output_topic_limit", "topic_limit"),
        ("cross_scope_handle", "cross_scope"),
        ("stage_b_evidence_out_of_scope", "evidence"),
        ("output_uncertainty_enum", "enum"),
        ("uncertainty_overstated_unresolved_reference", "enum"),
        ("model_call_failed", "exception"),
        ("provider_response_metadata", "exception"),
        ("stage_call_failed", "exception"),
    ],
)
def test_known_validation_codes_have_stable_body_free_categories(code: str, category: str) -> None:
    assert _safe_error_code(code) == code
    assert validation_categories_for_error_code(code) == (category,)
    assert error_category_for_code(code) == category


def test_candidate_only_pronoun_page_sets_uncertainty_ceiling_without_candidate_fact() -> None:
    page = _pages(count=1)[0]
    page.update(
        {
            "categories": ["pronoun_person_object_state"],
            "candidate_only": True,
            "not_canonical": True,
            "semantic_decision_pending": True,
        }
    )
    # Candidate-shaped fields alone are deliberately insufficient to establish
    # a canonical typed resolution.
    store = {
        "candidates": [
            {
                "candidate_handle": _handle("c", 0),
                "person": "candidate-person",
                "object": "candidate-object",
                "state": "candidate-state",
            }
        ]
    }
    assert _page_has_unresolved_pronoun_candidate(page, {}, store) is True
    request = _build_request(page, store)
    assert request["u"] == "uncertain"
    candidate_rows = [row for row in request["h"] if row["k"] == "c"]
    assert len(candidate_rows) == 1
    assert set(candidate_rows[0]) == {"i", "k", "h"}

    certain_model_output = {
        "topics": [
            {
                "topic_id": "candidate-only",
                "primary_message_ids": ["m1"],
                "context_message_ids": [],
                "uncertainty": "certain",
            }
        ]
    }
    with pytest.raises(CompactStageAProtocolV3Error) as exc_info:
        validate_compact_stage_a_output(certain_model_output, request)
    assert exc_info.value.code == "uncertainty_overstated_unresolved_reference"

    # An explicit canonical typed record removes only the ceiling; the
    # candidate remains a candidate row and cannot become a fact/primary.
    canonical_page = dict(page)
    canonical_page.update(
        {
            "canonical_metadata_status": "canonical",
            "typed_evidence_slots": {
                "person": "resolved",
                "object": "resolved",
                "state": "resolved",
            },
        }
    )
    assert _page_has_unresolved_pronoun_candidate(canonical_page, {}, store) is False
    canonical_request = _build_request(canonical_page, store)
    assert "u" not in canonical_request


def test_v3_validator_exposes_shape_category_without_accepting_extra_keys() -> None:
    request = build_compact_stage_a_request(
        SCOPE,
        [{"handle": _handle("m", 0), "role": "primary", "text": "synthetic"}],
    )
    invalid = {
        "topics": [
            {
                "topic_id": "t1",
                "primary_message_ids": ["m1"],
                "context_message_ids": [],
                "uncertainty": "unknown",
                "extra": "must reject",
            }
        ]
    }
    with pytest.raises(CompactStageAProtocolV3Error) as exc_info:
        validate_compact_stage_a_output(invalid, request)
    assert exc_info.value.code == "output_topic_keys"
    assert exc_info.value.validation_categories == ("schema_keys",)


def test_source_primary_binding_rejects_rank3_context_promotions_and_keeps_cues() -> None:
    """Rank3-equivalent synthetic role mapping stays source-primary-bound."""

    def message(name: str) -> str:
        return f"{SCOPE['account_id']}/{SCOPE['chat_id']}|message|{name}"

    rows = [
        {
            "message_handle": message("rank3-primary"),
            "message_id": "rank3-primary",
            "role": "substantive",
            "text": "synthetic substantive topic",
        },
        {
            "message_handle": message("rank3-authority"),
            "message_id": "rank3-authority",
            "role": "authority",
            "text": "synthetic authority cue",
        },
        {
            "message_handle": message("rank3-caption"),
            "message_id": "rank3-caption",
            "message_type": "image",
            "role": "mixed",
            "text": "",
            "caption": "synthetic caption cue",
        },
        {
            "message_handle": message("rank3-mixed"),
            "message_id": "rank3-mixed",
            "role": "mixed",
            "text": "你好，我想询问 synthetic topic",
        },
        {
            "message_handle": message("rank3-adjacent"),
            "message_id": "rank3-adjacent",
            "role": "adjacent",
            "text": "synthetic adjacent cue",
        },
        {
            "message_handle": message("rank3-candidate"),
            "message_id": "rank3-candidate",
            "role": "candidate",
            "text": "synthetic candidate cue",
        },
        {
            "message_handle": message("rank3-anchor"),
            "message_id": "rank3-anchor",
            "role": "primary",
            "text": "synthetic non-source anchor cue",
        },
    ]
    page: dict[str, Any] = {
        "page_id": "rank3-page",
        "root_id": "rank3-root",
        "scope": dict(SCOPE),
        "message_handles": [row["message_handle"] for row in rows],
        # Exercise the linear/materialized id form instead of relying on a
        # copied provider handle list.
        "primary_message_ids": ["rank3-primary", "rank3-caption", "rank3-mixed"],
        "authority_message_ids": ["rank3-authority"],
        "adjacent_message_ids": ["rank3-adjacent"],
        "candidate_message_ids": ["rank3-candidate"],
        "message_rows": rows,
    }

    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[message("rank3-primary")]["r"] == "p"
    assert by_handle[message("rank3-caption")]["r"] == "p"
    assert by_handle[message("rank3-caption")]["x"] == "synthetic caption cue"
    assert by_handle[message("rank3-mixed")]["r"] == "p"
    for name in ("rank3-authority", "rank3-adjacent", "rank3-candidate", "rank3-anchor"):
        assert by_handle[message(name)]["r"] == "c"
    assert {row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"} == {"m1", "m3", "m4"}
    assert page["_source_primary_eligible"] is True
    assert len(page["_source_primary_sha256"]) == 64

    run = PageRun(
        page=page,
        request=request,
        request_sha256="a" * 64,
        categories=("pronoun_person_object_state",),
        status="pending",
        payload=None,
        resolved_payload=None,
        error_code="primary_alias_not_source_primary",
        cache_hit=False,
        provider_call=False,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0.0,
        model="synthetic",
        source="synthetic",
        cue_codes=("pronoun_person_object_state",),
    )
    assert run.source_primary_eligible is True
    assert run.source_primary_sha256 == page["_source_primary_sha256"]
    assert run.to_dict()["source_primary_eligible"] is True
    assert run.diagnostic_dict()["source_primary_sha256"] == page["_source_primary_sha256"]
    public = _safe_public_page(page, run.categories, 3, run.status, request, run.request_sha256)
    assert public["source_primary_eligible"] is True
    assert public["source_primary_sha256"] == page["_source_primary_sha256"]
    assert public["message_count"] == len(rows)
    _assert_body_free(public)
    forged = {
        "topics": [
            {
                "topic_id": "rank3-forged",
                "primary_message_ids": ["m2"],
                "context_message_ids": ["m1"],
                "uncertainty": "unknown",
            }
        ]
    }
    with pytest.raises(CompactStageADevelopmentError) as raised:
        _validate_provider_output(forged, request)
    assert raised.value.code == "primary_alias_not_source_primary"


def test_substantive_adjacent_units_are_promoted_and_can_share_one_topic() -> None:
    """Eight semantic units remain provider-primary even when only six are source-primary."""

    def message(index: int) -> str:
        return f"{SCOPE['account_id']}/{SCOPE['chat_id']}|message|substantive-{index:02d}"

    rows = [
        {
            "message_handle": message(index),
            "message_id": f"substantive-{index:02d}",
            "role": "mixed" if index % 2 else "substantive",
            "text": f"项目状态问题 {index}？",
        }
        for index in range(8)
    ]
    page: dict[str, Any] = {
        "page_id": "substantive-page",
        "root_id": "substantive-root",
        "scope": dict(SCOPE),
        "message_handles": [row["message_handle"] for row in rows],
        "primary_message_ids": [f"substantive-{index:02d}" for index in range(6)],
        "adjacent_message_ids": [f"substantive-{index:02d}" for index in range(6, 8)],
        "message_rows": rows,
    }
    request = _build_request(page, {})
    primary = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
    assert len(primary) == 8
    assert measure_wire_size(request).http_token_proxy <= 1600
    response = {
        "topics": [
            {
                "topic_id": "one-topic",
                "primary_message_ids": primary,
                "context_message_ids": [],
                "uncertainty": "unknown",
            }
        ]
    }
    assert measure_output_size(response)["token_proxy"] <= 400
    assert validate_compact_stage_a_output(response, request) == response
    telemetry = page["_role_projection_telemetry"]
    assert telemetry["source_primary_count"] == 6
    assert telemetry["projected_primary_count"] == 8
    assert telemetry["projected_context_count"] == 0
    assert telemetry["substantive_adjacent_promoted_count"] == 2
    assert telemetry["topic_limit"] == 8


def test_mixed_short_cues_and_media_caption_are_primary_but_social_rows_are_context() -> None:
    rows = [
        {"message_handle": _handle("m", 20), "role": "conversation_opener", "text": "你好"},
        {"message_handle": _handle("m", 21), "role": "context_only", "text": "收到"},
        {"message_handle": _handle("m", 22), "role": "context_only", "text": "它现在？"},
        {"message_handle": _handle("m", 23), "role": "context_only", "text": "确认项目上线状态"},
        {"message_handle": _handle("m", 24), "role": "mixed", "message_type": "image", "text": "", "fragment_type": "event", "caption": "截图显示接口恢复"},
        {"message_handle": _handle("m", 25), "role": "mixed", "message_type": "image", "text": "[图片]"},
        {"message_handle": _handle("m", 26), "role": "reaction", "text": "👍"},
        {"message_handle": _handle("m", 27), "role": "context_only", "text": "..."},
    ]
    page: dict[str, Any] = {
        "page_id": "mixed-page",
        "root_id": "mixed-root",
        "scope": dict(SCOPE),
        "message_handles": [row["message_handle"] for row in rows],
        "primary_message_handles": [_handle("m", 22), _handle("m", 23), _handle("m", 24)],
        "message_rows": rows,
    }
    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[_handle("m", 20)]["r"] == "c"
    assert by_handle[_handle("m", 21)]["r"] == "c"
    assert by_handle[_handle("m", 22)]["r"] == "p"
    assert by_handle[_handle("m", 23)]["r"] == "p"
    assert by_handle[_handle("m", 24)]["r"] == "p"
    assert by_handle[_handle("m", 25)]["r"] == "c"
    assert by_handle[_handle("m", 26)]["r"] == "c"
    assert by_handle[_handle("m", 27)]["r"] == "c"


def test_projection_keeps_media_and_empty_authority_context_without_human_caption() -> None:
    """Page projection ignores XML/raw/metadata cues and keeps real captions."""

    rows = [
        {"message_handle": _handle("m", 30), "message_type": "image", "role": "primary", "content": "<image src='synthetic'/>"},
        {"message_handle": _handle("m", 31), "message_type": "file", "role": "primary", "content": '{"type":"file","name":"synthetic"}'},
        {"message_handle": _handle("m", 32), "message_type": "card", "role": "primary", "description": "card metadata"},
        {"message_handle": _handle("m", 33), "message_type": "system", "role": "primary", "content": '{"system_event":"synthetic"}'},
        {"message_handle": _handle("m", 34), "message_type": "event", "role": "primary", "is_placeholder": True, "content": "<event/>"},
        {"message_handle": _handle("m", 35), "role": "empty_authority", "description": "metadata description"},
        {"message_handle": _handle("m", 36), "role": "media_placeholder", "text": "[图片]"},
        {"message_handle": _handle("m", 37), "role": "reaction", "text": "👍"},
        {"message_handle": _handle("m", 38), "message_type": "image", "role": "mixed", "caption": "截图显示接口恢复"},
        {"message_handle": _handle("m", 39), "message_type": "file", "role": "mixed", "text": "请检查上传失败原因"},
    ]
    page: dict[str, Any] = {
        "page_id": "media-authority-projection-page",
        "root_id": "media-authority-projection-root",
        "scope": dict(SCOPE),
        "message_handles": [row["message_handle"] for row in rows],
        "primary_message_handles": [row["message_handle"] for row in rows],
        "message_rows": rows,
    }

    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    for index in range(30, 38):
        row = by_handle[_handle("m", index)]
        assert row["r"] == "c"
        assert row["x"] == ""
    assert by_handle[_handle("m", 38)]["r"] == "p"
    assert by_handle[_handle("m", 38)]["x"] == "截图显示接口恢复"
    assert by_handle[_handle("m", 39)]["r"] == "p"
    assert by_handle[_handle("m", 39)]["x"] == "请检查上传失败原因"
    telemetry = page["_role_projection_telemetry"]
    assert telemetry["projected_primary_count"] == 2
    assert telemetry["projected_context_count"] == 8


def test_projection_placeholder_source_primary_direct_cue_stays_context() -> None:
    """A source-primary placeholder cannot use the caption exception."""

    rows = [
        {
            "message_handle": _handle("m", 40),
            "message_type": "event",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "placeholder": True,
            "caption": "占位事件标题",
        },
        {
            "message_handle": _handle("m", 41),
            "message_type": "event",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "caption": "权威人类事件标题",
        },
        {
            "message_handle": _handle("m", 42),
            "message_type": "event",
            "role": "primary",
            "caption": "没有权威角色的事件标题",
        },
    ]
    handles = [row["message_handle"] for row in rows]
    page: dict[str, Any] = {
        "page_id": "placeholder-caption-page",
        "root_id": "placeholder-caption-root",
        "scope": dict(SCOPE),
        "message_handles": handles,
        "primary_message_handles": handles,
        "message_rows": rows,
    }
    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[handles[0]]["r"] == "c"
    assert by_handle[handles[1]]["r"] == "p"
    assert by_handle[handles[2]]["r"] == "c"


def test_authority_bound_overlap_event_caption_and_ellipsis_promote_without_reopening_barriers() -> None:
    """Bound source-primary semantic units survive typed K2 overlays."""

    def message(name: str) -> str:
        return f"{SCOPE['account_id']}/{SCOPE['chat_id']}|message|authority-{name}"

    rows = [
        {
            "message_handle": message("substantive"),
            "message_id": "authority-substantive",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "message_type": "text",
            "fragment_type": "statement",
            "text": "项目上线状态？",
            "span": {"start": 0, "end": 7},
        },
        {
            "message_handle": message("mixed"),
            "message_id": "authority-mixed",
            "role": "mixed",
            "roles": ["primary", "authority"],
            "message_type": "text",
            "fragment_type": "statement",
            "text": "请检查接口恢复",
            "span": {"start": 8, "end": 15},
        },
        {
            "message_handle": message("ellipsis"),
            "message_id": "authority-ellipsis",
            "role": "ellipsis",
            "roles": ["primary", "authority"],
            "message_type": "text",
            "fragment_type": "ellipsis",
            "text": "它现在？",
            "span": {"start": 16, "end": 20},
        },
        {
            "message_handle": message("event"),
            "message_id": "authority-event",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "text": "",
            "span": {"start": 21, "end": 26},
        },
        {
            "message_handle": message("media"),
            "message_id": "authority-media",
            "role": "context_only",
            "roles": ["primary", "authority"],
            "message_type": "image",
            "fragment_type": "media",
            "text": "[图片]",
            "span": {"start": 27, "end": 30},
        },
        {
            "message_handle": message("system"),
            "message_id": "authority-system",
            "role": "context_only",
            "roles": ["primary", "authority"],
            "message_type": "system",
            "fragment_type": "media",
            "content": "<event/>",
            "span": {"start": 31, "end": 36},
        },
        {
            "message_handle": message("merged"),
            "message_id": "authority-merged",
            "role": "substantive",
            "roles": ["primary", "authority"],
            "message_type": "text",
            "fragment_type": "statement",
            "text": "candidate activation cue",
            "merged_candidate_cue_present": True,
            "span": {"start": 37, "end": 43},
        },
    ]
    handles = [row["message_handle"] for row in rows]
    page: dict[str, Any] = {
        "page_id": "authority-bound-projection-page",
        "root_id": "authority-bound-projection-root",
        "scope": dict(SCOPE),
        "message_handles": handles,
        "primary_message_handles": handles,
        "authority_message_handles": handles,
    }
    store = {"messages": rows}
    packet = {
        "primary_fragments": [
            {
                "message_id": "authority-event",
                "role": "substantive",
                "fragment_type": "media",
                "text_redacted": "事件显示接口恢复",
                "span": {"start": 0, "end": 8},
            }
        ],
        "authoritative_facts": [
            {"message_id": "authority-event", "message_type": "system"}
        ],
    }
    merged = _merge_context_packet_bodies(store, {"synthetic-packet": packet})
    request = _build_request(page, merged)
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}

    for name in ("substantive", "mixed", "ellipsis", "event"):
        assert by_handle[message(name)]["r"] == "p"
    for name in ("media", "system", "merged"):
        assert by_handle[message(name)]["r"] == "c"
        assert by_handle[message(name)]["x"] == ""
    assert page["_role_projection_telemetry"]["projected_primary_count"] == 4
    assert page["_role_projection_telemetry"]["unbound_substantive_candidate_not_promoted_count"] >= 1


def test_projection_preserves_primary_order_under_the_fourteen_message_cap() -> None:
    """The compact cap keeps substantive source rows before recoverable context."""

    rows = []
    handles = []
    for index in range(20):
        handle = _handle("m", 60 + index)
        handles.append(handle)
        rows.append(
            {
                "message_handle": handle,
                "message_id": f"cap-{index}",
                "role": "substantive" if index < 2 else "context_only",
                "message_type": "text",
                "text": f"项目状态问题 {index}？" if index < 2 else "收到",
                "span": {"start": index, "end": index + 1},
            }
        )
    page: dict[str, Any] = {
        "page_id": "cap-order-page",
        "root_id": "cap-order-root",
        "scope": dict(SCOPE),
        "message_handles": handles,
        "primary_message_handles": handles[:2],
        "adjacent_message_handles": handles[2:],
    }
    request = _build_request(page, {"messages": rows})
    message_rows = [row for row in request["h"] if row["k"] == "m"]
    assert len(message_rows) == 14
    assert [row["h"] for row in message_rows[:2]] == handles[:2]
    assert all(row["r"] == "p" for row in message_rows[:2])
    assert all(row["r"] == "c" for row in message_rows[2:])


def test_nested_merged_cue_cannot_promote_without_an_independent_caption() -> None:
    merged_handle = _handle("m", 40)
    bound_handle = _handle("m", 41)
    rows = [
        {
            "message_handle": merged_handle,
            "identity_row": {
                "message_id": "nested-merged-message",
                "role": "substantive",
                "merged_candidate_cue": True,
                "text": "candidate activation cue",
            },
        },
        {"message_handle": bound_handle, "role": "primary", "text": "real topic question"},
    ]
    page: dict[str, Any] = {
        "page_id": "nested-merged-page",
        "root_id": "nested-merged-root",
        "scope": dict(SCOPE),
        "message_handles": [merged_handle, bound_handle],
        "primary_message_handles": [merged_handle, bound_handle],
        "message_rows": rows,
    }

    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[merged_handle]["r"] == "c"
    assert by_handle[bound_handle]["r"] == "p"
    telemetry = page["_role_projection_telemetry"]
    assert telemetry["unbound_substantive_candidate_not_promoted_count"] >= 1


def test_nested_authoritative_identity_deduplicates_fragment_handles() -> None:
    first_handle = _handle("m", 42)
    second_handle = _handle("m", 43)
    rows = [
        {
            "message_handle": first_handle,
            "identity_row": {"message_id": "same-authoritative-message", "role": "substantive", "text": "same topic"},
        },
        {
            "message_handle": second_handle,
            "identity_row": {"message_id": "same-authoritative-message", "role": "substantive", "text": "same topic"},
        },
    ]
    page: dict[str, Any] = {
        "page_id": "nested-dedup-page",
        "root_id": "nested-dedup-root",
        "scope": dict(SCOPE),
        "message_handles": [first_handle, second_handle],
        "primary_message_handles": [first_handle, second_handle],
        "message_rows": rows,
    }

    request = _build_request(page, {})
    message_rows = [row for row in request["h"] if row["k"] == "m"]
    assert len(message_rows) == 1
    assert message_rows[0]["h"] == first_handle
    assert page["_role_projection_telemetry"]["projected_primary_count"] == 1


def test_unbound_substantive_message_is_context_with_body_free_telemetry() -> None:
    bound_handle = _handle("m", 44)
    unbound_handle = _handle("m", 45)
    rows = [
        {"message_handle": bound_handle, "role": "primary", "text": "bound topic question"},
        {"message_handle": unbound_handle, "role": "substantive", "text": "unbound candidate question"},
    ]
    page: dict[str, Any] = {
        "page_id": "unbound-telemetry-page",
        "root_id": "unbound-telemetry-root",
        "scope": dict(SCOPE),
        "message_handles": [bound_handle, unbound_handle],
        "primary_message_handles": [bound_handle],
        "message_rows": rows,
    }

    request = _build_request(page, {})
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[bound_handle]["r"] == "p"
    assert by_handle[unbound_handle]["r"] == "c"
    telemetry = page["_role_projection_telemetry"]
    assert telemetry["unbound_substantive_candidate_not_promoted_count"] == 1
    assert telemetry["body_free"] is True


def test_authoritative_scope_conflict_blocks_message_promotion() -> None:
    conflicted_handle = _handle("m", 46)
    bound_handle = _handle("m", 47)
    rows = [
        {"message_handle": conflicted_handle, "message_id": "scope-conflict", "role": "substantive", "text": "wrong scope cue"},
        {"message_handle": bound_handle, "role": "primary", "text": "bound topic question"},
    ]
    page: dict[str, Any] = {
        "page_id": "scope-conflict-page",
        "root_id": "scope-conflict-root",
        "scope": dict(SCOPE),
        "message_handles": [conflicted_handle, bound_handle],
        "primary_message_handles": [conflicted_handle, bound_handle],
        "message_rows": rows,
    }
    store = {
        "messages": [
            {
                "message_handle": conflicted_handle,
                "message_id": "scope-conflict",
                "scope": {"account_id": "other-account", "chat_id": "other-chat"},
                "role": "substantive",
                "text": "wrong scope cue",
            }
        ]
    }

    request = _build_request(page, store)
    by_handle = {row["h"]: row for row in request["h"] if row["k"] == "m"}
    assert by_handle[conflicted_handle]["r"] == "c"
    assert by_handle[bound_handle]["r"] == "p"
    assert "authoritative_message_binding_missing" in page["_role_projection_telemetry"]["role_conflict_reasons"]


def test_runner_preserves_known_validation_code_in_diagnostics_and_ledger(tmp_path: Path) -> None:
    class InvalidShapeModel:
        model_id = "deepseek-v4-flash"
        source = "synthetic-invalid-shape"

        def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> Any:
            primary = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
            return {
                "topics": [
                    {
                        "topic_id": "t1",
                        "primary_message_ids": primary,
                        "context_message_ids": [],
                        "uncertainty": "unknown",
                        "extra": "synthetic-invalid-shape",
                    }
                ],
            }

    result = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "artifact",
        model=InvalidShapeModel(),
        authority_root=tmp_path / "authority",
        settings_sha256="b" * 64,
    )
    assert result.success is False
    assert result.pending_count == 5
    diagnostics = [
        json.loads(line)
        for line in Path(result.artifact_paths["diagnostics"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(row["error_code"] == "output_topic_keys" for row in diagnostics)
    assert all(row["validation_categories"] == ["schema_keys"] for row in diagnostics)
    assert result.aggregate["response_diagnostics"]["error_code_counts"] == {"output_topic_keys": 5}
    assert result.aggregate["response_diagnostics"]["validation_category_counts"] == {"schema_keys": 5}
    assert result.aggregate["response_diagnostics"]["unknown_error_count"] == 0
    ledger_rows = [
        json.loads(line)
        for line in Path(result.artifact_paths["ledger"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(row["error_code"] == "output_topic_keys" for row in ledger_rows)


def test_runner_wraps_unclassified_exception_as_body_free_exception_code(tmp_path: Path) -> None:
    class ExplodingModel:
        model_id = "deepseek-v4-flash"
        source = "synthetic-exception"

        def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> Any:
            raise RuntimeError("synthetic exception body must not escape")

    result = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "artifact",
        model=ExplodingModel(),
        authority_root=tmp_path / "authority",
        settings_sha256="c" * 64,
    )
    diagnostics_text = Path(result.artifact_paths["diagnostics"]).read_text(encoding="utf-8")
    assert "synthetic exception body" not in diagnostics_text
    diagnostics = [json.loads(line) for line in diagnostics_text.splitlines() if line.strip()]
    assert all(row["error_code"] == "model_call_failed" for row in diagnostics)
    assert all(row["validation_categories"] == ["exception"] for row in diagnostics)
    assert result.aggregate["response_diagnostics"]["unknown_error_count"] == 0


def test_k25_consumes_at_most_five_calls_and_emits_complete_only_decisions(tmp_path: Path) -> None:
    model = FakeModel()
    result = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "artifact",
        model=model,
        authority_root=tmp_path / "authority",
        settings_sha256="1" * 64,
    )
    assert model.calls == 5
    assert result.provider_calls == 5
    assert result.selected_page_count == 5
    assert result.pending_count == 0
    assert result.success is True
    assert result.aggregate["authorization_ledger"]["authorization_id"] == AUTHORIZATION_ID
    assert result.aggregate["authorization_ledger"]["calls_used"] == 5
    assert result.aggregate["decisions"]["complete"] == 5
    for path in result.artifact_paths.values():
        assert Path(path).is_file()
        _assert_body_free(json.loads(Path(path).read_text(encoding="utf-8")) if not path.endswith(".jsonl") else [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()])


def test_k25_persistent_authorization_blocks_rerun_in_new_output(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    first_model = FakeModel()
    first = run_compact_stage_a_development_pilot_v3(
        _pages(), tmp_path / "first", model=first_model, authority_root=authority, settings_sha256="2" * 64
    )
    second_model = FakeModel()
    second = run_compact_stage_a_development_pilot_v3(
        _pages(), tmp_path / "second", model=second_model, authority_root=authority, settings_sha256="2" * 64
    )
    assert first.provider_calls == 5
    assert second_model.calls == 0
    assert second.provider_calls == 0
    assert second.status == "partial"
    assert set(second.aggregate["errors"]["codes"]) == {"authorization_history_detected"}
    assert second.aggregate["authorization_ledger"]["calls_used"] == 5


def test_k25_schema_failure_is_pending_without_retry_and_preserves_cue(tmp_path: Path) -> None:
    model = FakeModel(invalid_on=2)
    result = run_compact_stage_a_development_pilot_v3(
        _pages(), tmp_path / "artifact", model=model, authority_root=tmp_path / "authority", settings_sha256="3" * 64
    )
    assert model.calls == 5
    assert result.pending_count == 1
    assert result.aggregate["cost"]["retry_count"] == 0
    decisions = [json.loads(line) for line in Path(result.artifact_paths["decisions"]).read_text(encoding="utf-8").splitlines()]
    pending = [row for row in decisions if row["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["cue_preserved"] is True
    assert pending[0]["retry_count"] == 0


def test_k25_missing_strata_is_explicit_and_not_fabricated(tmp_path: Path) -> None:
    pages = _pages(count=2)
    pages[0]["categories"] = ["greeting_new_topic"]
    pages[1]["categories"] = ["topic_shift"]
    model = FakeModel()
    result = run_compact_stage_a_development_pilot_v3(
        pages, tmp_path / "artifact", model=model, authority_root=tmp_path / "authority", settings_sha256="4" * 64
    )
    assert result.selected_page_count == 2
    assert set(result.missing_strata) == {"candidate_competition", "pronoun_person_object_state", "no_reply"}
    assert model.calls == 2
    assert "selection_strata_missing" in result.aggregate["errors"]["codes"]
    assert result.success is False


def test_k25_refuses_frozen_input_and_does_not_construct_a_provider(tmp_path: Path) -> None:
    with pytest.raises(CompactStageADevelopmentError):
        run_compact_stage_a_development_pilot_v3(
            tmp_path / "frozen" / "linear_stage_packet_development_v2",
            tmp_path / "artifact",
            authority_root=tmp_path / "authority",
        )


def test_k25_path_input_reads_only_complete_v2_pages(tmp_path: Path) -> None:
    input_root = tmp_path / "linear_stage_packet_development_v2"
    input_root.mkdir()
    manifest = {
        "artifact_version": "linear_stage_packet_development_v2",
        "split": "development",
        "local_day": "2026-08-25",
        "status": "complete",
        "provider_called": False,
        "provider_calls": 0,
        "frozen_read": False,
        "gold_loaded": False,
    }
    pages = _pages(count=5)
    materialized = [{
        "page_id": row["page_id"],
        "root_id": row["root_id"],
        "status": "complete",
        "stage_a": {"status": "complete"},
        "stage_b_status": "N/A",
        "stage_c_status": "N/A",
        "within_limits": True,
    } for row in pages]
    store = {
        "messages": [
            {"message_handle": row["message_handles"][0], "roles": ["primary"], "body": BODY_MARKER}
            for row in pages
        ],
        "candidates": [],
    }
    (input_root / "manifest.private.json").write_text(json.dumps(manifest), encoding="utf-8")
    (input_root / "pages.private.jsonl").write_text("\n".join(json.dumps(row) for row in pages) + "\n", encoding="utf-8")
    (input_root / "materialized_map.private.jsonl").write_text("\n".join(json.dumps(row) for row in materialized) + "\n", encoding="utf-8")
    (input_root / "store.private.json").write_text(json.dumps(store), encoding="utf-8")
    model = FakeModel()
    result = run_compact_stage_a_development_pilot_v3(
        input_root, tmp_path / "artifact", model=model, authority_root=tmp_path / "authority", settings_sha256="5" * 64
    )
    assert result.selected_page_count == 5
    assert model.calls == 5
    assert result.aggregate["input_artifact_version"] == "linear_stage_packet_development_v2"


def test_adapter_fix_authorization_is_isolated_and_persistent(tmp_path: Path) -> None:
    """The repair budget gets five calls without touching the legacy budget."""

    authority = tmp_path / "authority"
    old_model = FakeModel()
    old = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "old-artifact",
        model=old_model,
        authority_root=authority,
        settings_sha256="8" * 64,
        authorization_id=AUTHORIZATION_ID,
    )
    new_model = FakeModel()
    repaired = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "repair-artifact",
        model=new_model,
        authority_root=authority,
        settings_sha256="8" * 64,
        authorization_id=ADAPTER_FIX_AUTHORIZATION_ID,
    )

    assert old_model.calls == repaired.provider_calls == 5
    assert new_model.calls == repaired.provider_calls == 5
    assert old.aggregate["authorization_id"] == AUTHORIZATION_ID
    assert repaired.aggregate["authorization_id"] == ADAPTER_FIX_AUTHORIZATION_ID
    assert old.aggregate["authorization_ledger"]["calls_used"] == 5
    assert repaired.aggregate["authorization_ledger"]["calls_used"] == 5
    assert old.aggregate["authorization_ledger"]["authorization_id"] != repaired.aggregate["authorization_ledger"]["authorization_id"]
    contract = repaired.aggregate["authorization_contract"]
    assert contract["authorization_id"] == ADAPTER_FIX_AUTHORIZATION_ID
    assert contract["selection_sha256"]
    assert contract["audit_summary_sha256"]
    assert contract["context_input_sha256"]
    assert contract["adapter_code_sha256"]
    assert len(contract["code_sha256"]) == 64
    assert contract["protocol"]
    assert contract["settings_sha256"] == "8" * 64
    assert contract["model"] == "deepseek-v4-flash"
    assert contract["scope_sha256"]
    assert contract["max_provider_calls"] == 5
    assert contract["per_page_provider_call_limit"] == 1
    assert contract["max_retries"] == 0
    assert ALLOWED_AUTHORIZATION_IDS == frozenset(
        {
            AUTHORIZATION_ID,
            ADAPTER_FIX_AUTHORIZATION_ID,
                GUARD_VALIDATION_AUTHORIZATION_ID,
                GUARDED_AUTHORIZATION_ID,
                AUTHORITY_BOUND_AUTHORIZATION_ID,
            }
        )

    rerun_model = FakeModel()
    rerun = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "repair-rerun",
        model=rerun_model,
        authority_root=authority,
        settings_sha256="8" * 64,
        authorization_id=ADAPTER_FIX_AUTHORIZATION_ID,
    )
    assert rerun_model.calls == 0
    assert rerun.provider_calls == 0
    assert rerun.aggregate["authorization_ledger"]["calls_used"] == 5
    assert rerun.aggregate["errors"]["codes"] == ["authorization_history_detected"]


def test_authorization_allowlist_rejects_arbitrary_id_before_provider(tmp_path: Path) -> None:
    class MustNotCall:
        model_id = "deepseek-v4-flash"
        source = "synthetic-no-provider"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("unknown authorization must not reach provider")

    model = MustNotCall()
    output = tmp_path / "arbitrary-output"
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            _pages(),
            output,
            model=model,
            authority_root=tmp_path / "authority",
            settings_sha256="9" * 64,
            authorization_id="CALLER_SUPPLIED_ARBITRARY_ID",
        )
    assert exc_info.value.code == "authorization_binding_mismatch"
    assert model.calls == 0
    assert not output.exists()


def test_primary_context_fix_authorization_is_fail_closed_until_dedicated_contract(tmp_path: Path) -> None:
    model = FakeModel()
    output = tmp_path / "primary-context-fix"
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            _pages(),
            output,
            model=model,
            authority_root=tmp_path / "authority",
            settings_sha256="9" * 64,
            authorization_id=PRIMARY_CONTEXT_FIX_AUTHORIZATION_ID,
        )
    assert exc_info.value.code == "authorization_binding_mismatch"
    assert model.calls == 0
    assert not output.exists()


def test_adapter_fix_binding_mismatch_is_rejected_without_a_call(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    first = FakeModel()
    run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "first",
        model=first,
        authority_root=authority,
        settings_sha256="a" * 64,
        authorization_id=ADAPTER_FIX_AUTHORIZATION_ID,
    )
    second = FakeModel()
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            _pages(),
            tmp_path / "mismatch",
            model=second,
            authority_root=authority,
            settings_sha256="b" * 64,
            authorization_id=ADAPTER_FIX_AUTHORIZATION_ID,
        )
    assert exc_info.value.code == "authorization_binding_mismatch"
    assert second.calls == 0
    assert not (tmp_path / "mismatch").exists()


def test_guarded_authorization_uses_new_five_call_ledger_and_binds_guard_code(tmp_path: Path) -> None:
    """The guarded pilot is five calls, isolated from every prior ledger."""

    authority = tmp_path / "authority"
    old_model = FakeModel()
    old = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "old",
        model=old_model,
        authority_root=authority,
        settings_sha256="7" * 64,
        authorization_id=AUTHORIZATION_ID,
    )
    old_ledger = dict(old.aggregate["authorization_ledger"])

    guarded_model = FakeModel()
    guarded = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "guarded",
        model=guarded_model,
        authority_root=authority,
        settings_sha256="7" * 64,
        authorization_id=GUARDED_AUTHORIZATION_ID,
    )
    assert guarded_model.calls == guarded.provider_calls == 5
    assert guarded.selected_page_count == 5
    assert guarded.success is True
    assert guarded.aggregate["authorization_id"] == GUARDED_AUTHORIZATION_ID
    assert guarded.aggregate["guarded_authorization"] is True
    assert guarded.aggregate["provider"]["call_limit"] == 5
    assert guarded.aggregate["provider"]["retry_count"] == 0
    assert guarded.aggregate["provider"]["sdk_calls"] == 0
    assert guarded.aggregate["no_supplement"] is True
    assert guarded.aggregate["supplement_calls"] == 0
    contract = guarded.aggregate["authorization_contract"]
    for key in (
        "selection_sha256",
        "audit_summary_sha256",
        "human_audit_sha256",
        "context_input_sha256",
        "adapter_sha256",
        "adapter_code_sha256",
        "guard_code_sha256",
        "scope_sha256",
        "input_binding_sha256",
    ):
        assert contract[key]
    assert contract["model"] == "deepseek-v4-flash"
    assert contract["protocol"]
    assert contract["settings_sha256"] == "7" * 64
    assert contract["max_provider_calls"] == 5
    assert contract["selected_page_limit"] == 5
    assert contract["per_page_provider_call_limit"] == 1
    assert contract["max_retries"] == 0
    assert contract["sdk_calls"] == 0
    assert contract["no_supplement"] is True

    rerun_model = FakeModel()
    rerun = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "guarded-rerun",
        model=rerun_model,
        authority_root=authority,
        settings_sha256="7" * 64,
        authorization_id=GUARDED_AUTHORIZATION_ID,
    )
    assert rerun_model.calls == rerun.provider_calls == 0
    assert rerun.aggregate["authorization_ledger"]["calls_used"] == 5
    assert rerun.aggregate["errors"]["codes"] == ["authorization_history_detected"]

    # Opening the old authorization after the guarded run must retain its
    # original five-call history and must not consume another provider call.
    old_rerun_model = FakeModel()
    old_rerun = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "old-rerun",
        model=old_rerun_model,
        authority_root=authority,
        settings_sha256="7" * 64,
        authorization_id=AUTHORIZATION_ID,
    )
    assert old_rerun_model.calls == old_rerun.provider_calls == 0
    assert old_rerun.aggregate["authorization_ledger"] == old_ledger


def test_guarded_authorization_binding_mismatch_blocks_provider(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    first = FakeModel()
    run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "first",
        model=first,
        authority_root=authority,
        settings_sha256="8" * 64,
        authorization_id=GUARDED_AUTHORIZATION_ID,
    )
    second = FakeModel()
    output = tmp_path / "mismatch"
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            _pages(),
            output,
            model=second,
            authority_root=authority,
            settings_sha256="9" * 64,
            authorization_id=GUARDED_AUTHORIZATION_ID,
        )
    assert exc_info.value.code == "authorization_binding_mismatch"
    assert second.calls == 0
    assert not output.exists()


def _assert_historical_authority_bound_rejected(
    tmp_path: Path,
    *,
    pages: list[dict[str, Any]],
    output_name: str = "historical-authority-bound",
    model: Any | None = None,
    expected_codes: set[str] | None = None,
    **kwargs: Any,
) -> CompactStageADevelopmentError:
    """The consumed authority-bound id must fail closed after code drift."""

    authority = tmp_path / "authority"
    output = tmp_path / output_name
    authority_existed = authority.exists()
    caller = model if model is not None else FakeModel()
    settings_sha256 = kwargs.pop("settings_sha256", AUTHORITY_BOUND_SETTINGS_SHA256)
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            pages,
            output,
            model=caller,
            authority_root=authority,
            settings_sha256=settings_sha256,
            authorization_id=AUTHORITY_BOUND_AUTHORIZATION_ID,
            **kwargs,
        )
    accepted_codes = expected_codes or {"authority_projection_code_drift"}
    assert exc_info.value.code in accepted_codes
    assert getattr(caller, "calls", 0) == 0
    assert not output.exists()
    if not authority_existed:
        assert not authority.exists()
    return exc_info.value


def test_authority_bound_authorization_contract_is_exact_five_call_and_body_free(tmp_path: Path) -> None:
    """A consumed historical authority id cannot be reused after code drift."""

    _assert_historical_authority_bound_rejected(
        tmp_path,
        pages=_authority_pages(),
    )


def test_authority_bound_without_injected_provider_keeps_ledger_history_zero(tmp_path: Path) -> None:
    _assert_historical_authority_bound_rejected(
        tmp_path,
        pages=_authority_pages(),
        output_name="authority-bound-no-provider",
    )


def test_authority_bound_authorization_isolated_from_old_ledger_and_artifact(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    old_model = FakeModel()
    old = run_compact_stage_a_development_pilot_v3(
        _pages(),
        tmp_path / "old-artifact",
        model=old_model,
        authority_root=authority,
        settings_sha256="b" * 64,
        authorization_id=AUTHORIZATION_ID,
    )
    new_model = FakeModel()
    _assert_historical_authority_bound_rejected(
        tmp_path,
        pages=_authority_pages(),
        output_name="authority-bound-artifact",
        model=new_model,
    )
    assert old_model.calls == 5
    assert new_model.calls == 0
    assert old.aggregate["authorization_id"] == AUTHORIZATION_ID
    assert old.aggregate["authorization_ledger"]["calls_used"] == 5


def test_authority_bound_cross_output_directory_reuses_persistent_history(tmp_path: Path) -> None:
    first_model = FakeModel()
    _assert_historical_authority_bound_rejected(
        tmp_path,
        pages=_authority_pages(),
        output_name="first-output",
        model=first_model,
    )
    second_model = FakeModel()
    _assert_historical_authority_bound_rejected(
        tmp_path,
        pages=_authority_pages(),
        output_name="second-output",
        model=second_model,
    )
    assert first_model.calls == second_model.calls == 0


@pytest.mark.parametrize("drift", ["projection_hash", "page", "settings", "scope"])
def test_authority_bound_hash_page_settings_scope_drift_fails_closed(tmp_path: Path, drift: str) -> None:
    changed_pages = _authority_pages()
    changed_settings = AUTHORITY_BOUND_SETTINGS_SHA256
    projection_hashes = None
    if drift == "projection_hash":
        projection_hashes = {"authority_projection_code_sha256": "e" * 64}
    elif drift == "page":
        changed_pages[0]["page_hash"] = "f" * 64
    elif drift == "settings":
        changed_settings = "e" * 64
    elif drift == "scope":
        for page in changed_pages:
            page["scope"] = {"account_id": SCOPE["account_id"], "chat_id": "different-chat"}
    second_model = FakeModel()
    _assert_historical_authority_bound_rejected(
        tmp_path,
        pages=changed_pages,
        output_name="second-" + drift,
        model=second_model,
        settings_sha256=changed_settings,
        authority_projection_hashes=projection_hashes,
        expected_codes={
            "authority_projection_code_drift",
            "authority_projection_hash_mismatch",
            "authority_projection_page_drift",
            "authority_projection_scope_drift",
        },
    )


def _authority_audit_summary() -> dict[str, Any]:
    """Create the same body-free synthetic sidecar used by the runner."""

    normalized, store = _normalize_mapping_pages(_authority_pages(), {})
    plan = _selection_plan(normalized, store)
    selection_sha256 = stable_hash(plan.to_dict(selected_page_limit=5))
    return json.loads(
        json.dumps(
            _synthetic_authority_projection_audit(
                plan.selected,
                selection_sha256=selection_sha256,
            )
        )
    )


def _assert_authority_rejected_before_ledger(
    tmp_path: Path,
    *,
    pages: list[dict[str, Any]],
    summary: Mapping[str, Any] | None = None,
    hashes: Mapping[str, Any] | None = None,
) -> CompactStageADevelopmentError:
    """Run one adversarial preparation and assert no output/provider path."""

    output = tmp_path / "rejected-output"
    authority = tmp_path / "authority"
    model = FakeModel()
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            pages,
            output,
            model=model,
            authority_root=authority,
            settings_sha256=AUTHORITY_BOUND_SETTINGS_SHA256,
            authorization_id=AUTHORITY_BOUND_AUTHORIZATION_ID,
            authority_projection_audit_summary=summary,
            authority_projection_hashes=hashes,
        )
    assert model.calls == 0
    assert not output.exists()
    assert not authority.exists()
    return exc_info.value


def test_authority_bound_arbitrary_five_pages_fail_before_ledger(tmp_path: Path) -> None:
    error = _assert_authority_rejected_before_ledger(tmp_path, pages=_pages())
    assert error.code in {"authority_projection_page_drift", "authority_projection_scope_drift"}


@pytest.mark.parametrize("mutation", ["swap", "order", "duplicate", "missing", "extra"])
def test_authority_bound_canonical_mapping_shape_fail_closed_before_ledger(
    tmp_path: Path,
    mutation: str,
) -> None:
    summary = _authority_audit_summary()
    rows = summary["pages"]
    if mutation == "swap":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "order":
        rows[:] = [rows[0], rows[2], rows[1], rows[3], rows[4]]
    elif mutation == "duplicate":
        rows[3] = json.loads(json.dumps(rows[2]))
    elif mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append(json.loads(json.dumps(rows[-1])))
    error = _assert_authority_rejected_before_ledger(
        tmp_path,
        pages=_authority_pages(),
        summary=summary,
    )
    assert error.code in {"authority_projection_page_drift", "authority_projection_scope_drift"}


@pytest.mark.parametrize("field", ["page_ref", "root_ref", "source_ref", "scope_ref"])
def test_authority_bound_sidecar_identity_drift_fail_closed_before_ledger(
    tmp_path: Path,
    field: str,
) -> None:
    summary = _authority_audit_summary()
    summary["pages"][0][field] = "forged-" + field
    error = _assert_authority_rejected_before_ledger(
        tmp_path,
        pages=_authority_pages(),
        summary=summary,
    )
    assert error.code in {"authority_projection_page_drift", "authority_projection_scope_drift"}


def test_authority_bound_hash_assertion_is_not_authoritative(tmp_path: Path) -> None:
    error = _assert_authority_rejected_before_ledger(
        tmp_path,
        pages=_authority_pages(),
        hashes={"authority_projection_code_sha256": "f" * 64},
    )
    assert error.code == "authority_projection_hash_mismatch"


def test_authority_bound_source_mapping_scope_replacement_fails_before_ledger(tmp_path: Path) -> None:
    pages = _authority_pages()
    for page in pages:
        page["_scope_handle"] = "k30_scope_forged"
    error = _assert_authority_rejected_before_ledger(tmp_path, pages=pages)
    assert error.code == "authority_projection_scope_drift"


@pytest.mark.parametrize(
    "lineage_key",
    [
        "selection_sha256",
        "context_input_sha256",
        "projection_code_sha256",
        "audit_summary_sha256",
        "human_audit_sha256",
    ],
)
def test_authority_bound_sidecar_lineage_hash_drift_fails_before_ledger(
    tmp_path: Path,
    lineage_key: str,
) -> None:
    summary = _authority_audit_summary()
    summary.setdefault("input_lineage", {})[lineage_key] = "f" * 64
    error = _assert_authority_rejected_before_ledger(
        tmp_path,
        pages=_authority_pages(),
        summary=summary,
    )
    assert error.code in {
        "authority_projection_page_drift",
        "authority_projection_hash_mismatch",
        "authority_projection_code_drift",
    }


@pytest.mark.parametrize("mutation", ["swap", "order", "duplicate"])
def test_authority_bound_source_mapping_identity_shape_fails_before_ledger(
    tmp_path: Path,
    mutation: str,
) -> None:
    pages = _authority_pages()
    if mutation == "swap":
        first, second = dict(pages[0]), dict(pages[1])
        first_rank, second_rank = pages[0]["_selection_rank"], pages[1]["_selection_rank"]
        pages[0], pages[1] = second, first
        pages[0]["_selection_rank"], pages[1]["_selection_rank"] = first_rank, second_rank
    elif mutation == "order":
        pages[0]["page_id"], pages[1]["page_id"] = pages[1]["page_id"], pages[0]["page_id"]
    elif mutation == "duplicate":
        pages[1]["page_id"] = pages[0]["page_id"]
    error = _assert_authority_rejected_before_ledger(tmp_path, pages=pages)
    assert error.code == "authority_projection_page_drift"


def test_only_current_linear_audit_directory_is_an_authorization_source(tmp_path: Path) -> None:
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        _validate_current_linear_audit_root(tmp_path / "artifact" / "audit")
    assert exc_info.value.code == "audit_invalid"
    current = tmp_path / INPUT_ARTIFACT_VERSION / "audit"
    current.mkdir(parents=True)
    assert _validate_current_linear_audit_root(current) == current.resolve()


def _guard_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Build body-bearing synthetic pages plus body-free audit/replay metadata."""

    pages = _pages(count=3)
    guards = (
        ("pending_unknown_error", "candidate_competition", "provider_error", "exception"),
        ("no_body_primary", "pronoun_person_object_state", "primary_alias_not_semantic_eligible", "selection"),
        ("unresolved_pronoun_certain", "pronoun_person_object_state", "uncertainty_overstated_unresolved_reference", "enum"),
    )
    subset: list[dict[str, Any]] = []
    human: list[dict[str, Any]] = []
    complete: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for index, page in enumerate(pages, 1):
        guard_id, family, error_code, category = guards[index - 1]
        page["page_id"] = f"guard-page-{index}"
        page["root_id"] = f"guard-root-{index}"
        page["_selection_rank"] = index
        page["_source_handle"] = f"guard-source-{index}"
        page["_scope_handle"] = "guard-scope-k25"
        page["categories"] = [family]
        if index == 3:
            page.update(
                {
                    "_candidate_only": True,
                    "_not_canonical": True,
                    "_semantic_decision_pending": True,
                }
            )
        row = {
            "selection_rank": index,
            "page_ref": page["page_id"],
            "root_ref": page["root_id"],
            "source_ref": page["_source_handle"],
            "scope_ref": page["_scope_handle"],
            "cue_family": family,
            "guard_id": guard_id,
            "expected_error_code": error_code,
            "expected_category": category,
        }
        subset.append(row)
        human.append(
            {
                "selection_rank": index,
                "page_ref": page["page_id"],
                "root_ref": page["root_id"],
                "source_ref": page["_source_handle"],
                "evidence_binding": {
                    "candidate_only": True,
                    "not_canonical": True,
                    "semantic_decision_pending": True,
                },
            }
        )
        if index == 1:
            pending.append(
                {
                    "rank": index,
                    "page_ref": page["page_id"],
                    "root_ref": page["root_id"],
                    "source_ref": page["_source_handle"],
                    "replay_status": "N/A",
                    "reason": "Historical provider result was pending/unknown_error; no semantic replay attempted.",
                }
            )
        else:
            complete.append(
                {
                    "rank": index,
                    "page_ref": page["page_id"],
                    "root_ref": page["root_id"],
                    "source_ref": page["_source_handle"],
                    "synthetic_fixture": {
                        "observed_code": error_code,
                        "observed_category": category,
                        "guard_match": True,
                    },
                }
            )
    replay = {
        "schema": "compact_stage_a_adapter_fix_guards_replay_v1",
        "body_free": True,
        "validator": {
            "protocol_version": "stage_a_topic_assignment_compact_v3",
            "guard_categories": {
                "primary_alias_not_semantic_eligible": "selection",
                "uncertainty_overstated_unresolved_reference": "enum",
            },
        },
        "replay": {"complete_pages": complete, "pending_pages": pending},
        "conclusion": {
            "new_authorization_issued": False,
            "provider_calls": 0,
            "rerun_performed": False,
            "raw_payload_read": False,
            "frozen_read": False,
            "gold_loaded": False,
            "production_read": False,
        },
    }
    summary = {"body_free": True, "privacy": {"body_free": True}}
    return pages, subset, summary, {"human": human, "replay": replay}


def test_guard_validation_runs_only_explicit_audited_three_page_subset_and_blocks_rerun(tmp_path: Path) -> None:
    pages, subset, summary, evidence = _guard_fixture()

    class GuardFakeModel:
        model_id = "deepseek-v4-flash"
        source = "synthetic-guard"

        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> Any:
            self.calls += 1
            self.prompts.append(system_prompt)
            if self.calls == 1:
                return {"error_code": "provider_error"}
            primary = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
            context = [row["i"] for row in request["h"] if row["k"] == "m" and row["r"] == "c"]
            uncertainty = "unknown"
            if request.get("u") == "uncertain":
                uncertainty = "unknown"
            return {
                "topics": [
                    {
                        "topic_id": f"guard-topic-{self.calls}",
                        "primary_message_ids": primary,
                        "context_message_ids": context,
                        "uncertainty": uncertainty,
                    }
                ]
            }

    authority = tmp_path / "authority"
    model = GuardFakeModel()
    result = run_compact_stage_a_development_pilot_v3(
        pages,
        tmp_path / "guard-artifact",
        model=model,
        authority_root=authority,
        settings_sha256="d" * 64,
        authorization_id=GUARD_VALIDATION_AUTHORIZATION_ID,
        guard_validation_subset=subset,
        guard_audit_summary=summary,
        guard_human_audit=evidence["human"],
        guards_replay=evidence["replay"],
    )
    assert model.calls == GUARD_VALIDATION_MAX_PROVIDER_CALLS == 3
    assert result.provider_calls == 3
    assert result.selected_page_count == GUARD_VALIDATION_MAX_SELECTED_PAGES == 3
    assert result.pending_count == 1
    assert result.aggregate["artifact_version"] == GUARD_VALIDATION_ARTIFACT_VERSION
    assert result.aggregate["guard_prompt_version"] == GUARD_VALIDATION_PROMPT_VERSION
    assert result.aggregate["guard_taxonomy_version"] == GUARD_VALIDATION_TAXONOMY_VERSION
    assert result.aggregate["guard_subset_sha256"] == _guard_subset_hash(subset)
    assert result.aggregate["lineage"]["guards_replay_sha256"]
    assert all(GUARD_VALIDATION_PROMPT_VERSION.split("_")[0] in prompt for prompt in model.prompts)
    assert result.aggregate["provider"]["call_limit"] == 3
    assert result.aggregate["provider"]["retry_count"] == 0
    assert result.aggregate["provider"]["sdk_calls"] == 0

    rerun_model = GuardFakeModel()
    rerun = run_compact_stage_a_development_pilot_v3(
        pages,
        tmp_path / "guard-rerun",
        model=rerun_model,
        authority_root=authority,
        settings_sha256="d" * 64,
        authorization_id=GUARD_VALIDATION_AUTHORIZATION_ID,
        guard_validation_subset=subset,
        guard_audit_summary=summary,
        guard_human_audit=evidence["human"],
        guards_replay=evidence["replay"],
    )
    assert rerun_model.calls == rerun.provider_calls == 0
    assert rerun.aggregate["errors"]["codes"] == ["authorization_history_detected"]

    with pytest.raises(CompactStageADevelopmentError) as mismatch:
        run_compact_stage_a_development_pilot_v3(
            pages,
            tmp_path / "guard-mismatch",
            model=GuardFakeModel(),
            authority_root=authority,
            settings_sha256="e" * 64,
            authorization_id=GUARD_VALIDATION_AUTHORIZATION_ID,
            guard_validation_subset=subset,
            guard_audit_summary=summary,
            guard_human_audit=evidence["human"],
            guards_replay=evidence["replay"],
        )
    assert mismatch.value.code == "authorization_binding_mismatch"


def test_guard_validator_and_subset_taxonomy_are_fail_closed() -> None:
    assert GUARD_VALIDATION_SUBSET_SHA256
    assert GUARD_VALIDATION_ERROR_TAXONOMY["no_body_primary"]["category"] == "selection"
    request = build_compact_stage_a_request(
        SCOPE,
        [{"message_handle": _handle("m", 1), "message_type": "text", "text": "synthetic guard cue"}],
        [{"candidate_handle": _handle("c", 1)}],
        uncertainty_ceiling="uncertain",
    )
    with pytest.raises(CompactStageADevelopmentError) as error:
        validate_guard_validation_output(
            {"error_code": "uncertainty_overstated_unresolved_reference"},
            request,
            guard_id="unresolved_pronoun_certain",
        )
    assert error.value.code == "uncertainty_overstated_unresolved_reference"
    valid = validate_guard_validation_output(
        {
            "topics": [
                {
                    "topic_id": "guard-valid",
                    "primary_message_ids": ["m1"],
                    "context_message_ids": [],
                    "uncertainty": "unknown",
                }
            ]
        },
        request,
        guard_id="unresolved_pronoun_certain",
    )
    assert valid["topics"][0]["uncertainty"] == "unknown"


def test_guard_validation_rejects_an_unaudited_opaque_page_before_model(tmp_path: Path) -> None:
    pages, subset, summary, evidence = _guard_fixture()
    unaudited = [dict(row) for row in subset]
    unaudited[0]["page_ref"] = "guard-page-not-audited"

    class MustNotCall:
        model_id = "deepseek-v4-flash"
        source = "synthetic-guard"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("unaudited page must not reach provider")

    model = MustNotCall()
    with pytest.raises(CompactStageADevelopmentError) as error:
        run_compact_stage_a_development_pilot_v3(
            pages,
            tmp_path / "unaudited",
            model=model,
            authority_root=tmp_path / "authority",
            settings_sha256="f" * 64,
            authorization_id=GUARD_VALIDATION_AUTHORIZATION_ID,
            guard_validation_subset=unaudited,
            guard_audit_summary=summary,
            guard_human_audit=evidence["human"],
            guards_replay=evidence["replay"],
        )
    assert error.value.code in {"guard_subset_invalid", "guard_audit_invalid"}
    assert model.calls == 0
