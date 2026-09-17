"""Synthetic-only contract tests for the bounded K14 Stage-A development pilot."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from wechat_bridge.linear_stage_a_development_pilot import (
    ARTIFACT_VERSION,
    BODY_KEYS,
    DEFAULT_ARTIFACT_DIRECTORY,
    MAX_DEVELOPMENT_CALLS,
    MAX_OUTPUT_TOKENS,
    MAX_PROVIDER_CALLS,
    MODEL,
    RESPONSE_FORMAT_MODE,
    THINKING_DISABLED,
    run_linear_stage_a_development_pilot,
)
from wechat_bridge.linear_stage_a_protocol_diagnostic import (
    DiagnosticProviderConfig,
    DiagnosticProviderResponse,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _handle(account: str, chat: str, kind: str, value: str) -> str:
    return f"{account}/{chat}|{kind}|{value}"


def _build_health_artifact(root: Path) -> Path:
    """Create only the body-free K13 gate consumed by K14.

    This is deliberately written as a fixture instead of invoking K13: K14
    must reuse an existing health result and must not make another health
    request during this test.
    """

    root.mkdir(parents=True)
    _write_json(
        root / "manifest.private.json",
        {
            "artifact_version": "linear_stage_a_protocol_health_v2",
            "status": "available",
            "success": True,
            "diagnostic_only": True,
            "provider_calls": 1,
            "diagnostic_call_count": 1,
            "retry_count": 0,
            "development_input_read": False,
            "frozen_read": False,
            "production_state_written": False,
            "thinking_disabled": True,
            "provider": {
                "model": MODEL,
                "source": "synthetic-health",
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
            },
        },
    )
    _write_json(
        root / "aggregate.private.json",
        {
            "artifact_version": "linear_stage_a_protocol_health_v2",
            "status": "available",
            "success": True,
            "diagnostic_only": True,
            "development_input_read": False,
            "stage_a_development": False,
            "stage_b_pilot": False,
            "frozen_read": False,
            "production_state_written": False,
            "settings_unchanged": True,
            "strict_result": {
                "strict_complete": True,
                "parse_code": "ok",
                "validation_code": "ok",
            },
        },
    )
    return root


def _build_v2_input(root: Path, *, include_incomplete: bool = True) -> Path:
    """Build five complete pages, one per K10 stratum, plus one ignored page."""

    root.mkdir(parents=True)
    specs = (
        "greeting_new_topic",
        "no_reply",
        "pronoun_person_object_state",
        "topic_shift",
        "candidate_competition",
    )
    pages: list[dict[str, Any]] = []
    materialized: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    content_table: dict[str, dict[str, Any]] = {}

    for page_index, category in enumerate(specs):
        account = f"ACCOUNT_SYNTH_{page_index:02d}"
        chat = f"CHAT_SYNTH_{page_index:02d}"
        root_id = f"ROOT_SYNTH_{page_index:02d}"
        page_id = f"{root_id}|page|0001"
        message_handles = [_handle(account, chat, "message", f"M{page_index:02d}_{n}") for n in range(2)]
        evidence_handles = [_handle(account, chat, "evidence", f"E{page_index:02d}_{n}") for n in range(2)]
        candidate_count = 12 if category == "candidate_competition" else (3 if category == "pronoun_person_object_state" else 1)
        candidate_handles = [
            _handle(account, chat, "candidate", f"C{page_index:02d}_{n}")
            for n in range(candidate_count)
        ]
        page = {
            "candidate_handles": candidate_handles,
            "candidate_link_refs": list(candidate_handles),
            "evidence_handles": evidence_handles,
            "message_handles": message_handles,
            "ordinal": 1,
            "page_hash": f"page-hash-{page_index:02d}",
            "page_id": page_id,
            "root_id": root_id,
            "scope": {"account_id": account, "chat_id": chat},
            "source_packet_id": root_id,
            "status": "open",
        }
        pages.append(page)
        materialized.append(
            {
                "candidate_count": candidate_count,
                "candidate_row_count": candidate_count,
                "evidence_count": 0,
                "evidence_ref_count": 0,
                "input_token_proxy": 200,
                "material_stats": {
                    "candidate_count": candidate_count,
                    "candidate_row_count": candidate_count,
                    "input_token_proxy": 200,
                    "message_count": 2,
                    "user_token_proxy": 180,
                    "within_limits": True,
                },
                "message_count": 2,
                "page_id": page_id,
                "page_ordinal": 1,
                "root_id": root_id,
                "source_packet_id": root_id,
                "stage": "A",
                "stage_a": {
                    "message_handles": list(message_handles),
                    "candidate_link_refs": list(candidate_handles),
                    "material_stats": {"within_limits": True},
                    "status": "complete",
                },
                "stage_b_status": "N/A",
                "stage_c_status": "N/A",
                "status": "complete",
                "within_limits": True,
            }
        )
        for message_index, message_handle in enumerate(message_handles):
            content_handle = _handle(account, chat, "content", f"BODY_{page_index:02d}_{message_index}")
            identity: dict[str, Any] = {
                "segment_id": f"SEGMENT_{page_index:02d}_A",
                "reply_to_message_id": None,
                "is_opener": False,
                "fragment_type": "statement",
            }
            if category == "greeting_new_topic":
                identity["is_opener"] = True
                identity["fragment_type"] = "conversation_opener"
            if category == "topic_shift" and message_index == 1:
                identity["segment_id"] = f"SEGMENT_{page_index:02d}_B"
            messages.append(
                {
                    "message_handle": message_handle,
                    "message_id": f"M{page_index:02d}_{message_index}",
                    "roles": ["primary"] if message_index == 0 else ["adjacent"],
                    "identity_row": identity,
                    "content_handles": [content_handle],
                    "scope": {"account_id": account, "chat_id": chat},
                }
            )
            content_table[content_handle] = {
                "content_handle": content_handle,
                "content_id": f"BODY_{page_index:02d}_{message_index}",
                "body": f"SYNTHETIC PRIVATE BODY {page_index} {message_index}",
                "body_hash": f"body-hash-{page_index:02d}-{message_index}",
                "scope": {"account_id": account, "chat_id": chat},
            }
        for candidate_index, candidate_handle in enumerate(candidate_handles):
            views: list[str] = []
            reasons: list[str] = []
            if category == "pronoun_person_object_state":
                views = [[
                    "candidate_person_history",
                    "candidate_object_history",
                    "candidate_state_history",
                ][candidate_index]]
            if category == "no_reply":
                reasons = ["time_proximity_weak", "same_segment_weak"]
            candidates.append(
                {
                    "candidate_handle": candidate_handle,
                    "candidate_reason": reasons,
                    "evidence_handle_refs": [evidence_handles[candidate_index % 2]],
                    "message_ids": [f"M{page_index:02d}_0", f"M{page_index:02d}_1"],
                    "relation_subtype": "candidate_only",
                    "relation_label": "candidate_only",
                    "uncertainties": [],
                    "view_names": views,
                    "scope": {"account_id": account, "chat_id": chat},
                }
            )

    if include_incomplete:
        # This page is intentionally not eligible: K14 must consume complete
        # K10 pages only and must not silently turn it into a sixth call.
        account = "ACCOUNT_INCOMPLETE"
        chat = "CHAT_INCOMPLETE"
        root_id = "ROOT_INCOMPLETE"
        page_id = f"{root_id}|page|0001"
        page_handles = [_handle(account, chat, "message", "M0"), _handle(account, chat, "message", "M1")]
        pages.append(
            {
                "candidate_handles": [],
                "candidate_link_refs": [],
                "evidence_handles": [],
                "message_handles": page_handles,
                "ordinal": 1,
                "page_hash": "incomplete-page-hash",
                "page_id": page_id,
                "root_id": root_id,
                "scope": {"account_id": account, "chat_id": chat},
                "source_packet_id": root_id,
                "status": "open",
            }
        )
        materialized.append(
            {
                "candidate_count": 0,
                "candidate_row_count": 0,
                "evidence_count": 0,
                "evidence_ref_count": 0,
                "input_token_proxy": 20,
                "material_stats": {"within_limits": True},
                "message_count": 2,
                "page_id": page_id,
                "page_ordinal": 1,
                "root_id": root_id,
                "source_packet_id": root_id,
                "stage": "A",
                "stage_a": {"material_stats": {"within_limits": True}, "status": "pending"},
                "stage_b_status": "N/A",
                "stage_c_status": "N/A",
                "status": "pending",
                "within_limits": True,
            }
        )

    _write_json(
        root / "manifest.private.json",
        {
            "artifact_version": "linear_stage_packet_development_v2",
            "split": "development",
            "local_day": "2026-08-25",
            "frozen_read": False,
            "provider_called": False,
            "provider_calls": 0,
            "input_selected_digest": "synthetic-k10-v2-digest",
            "page_count": len(pages),
            "root_count": len(pages),
        },
    )
    _write_jsonl(root / "pages.private.jsonl", pages)
    _write_jsonl(root / "materialized_map.private.jsonl", materialized)
    _write_json(
        root / "store.private.json",
        {
            "schema_version": "synthetic-k10-store-v1",
            "messages": messages,
            "candidates": candidates,
            "content_table": content_table,
        },
    )
    return root


class SyntheticProvider:
    source = "synthetic-provider"
    model_id = MODEL
    configured = True

    def __init__(self, *, fail_call: int | None = None) -> None:
        self.fail_call = fail_call
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> DiagnosticProviderResponse:
        assert stage == "A"
        assert system_prompt
        self.calls.append({"packet": deepcopy(dict(user_packet)), "max_output_tokens": max_output_tokens})
        if self.fail_call == len(self.calls):
            return DiagnosticProviderResponse(
                content=json.dumps({"topics": []}),
                reasoning_content="SYNTHETIC SECRET REASONING",
                finish_reasons=("stop",),
                input_tokens=1200,
                output_tokens=10,
                usage_present=True,
                usage_keys=("prompt_tokens", "completion_tokens"),
                latency_ms=1.0,
                model=MODEL,
                source=self.source,
                response_fields=("choices", "content", "reasoning", "api_key"),
                request_id_present=True,
            )
        packet = user_packet
        output = {
            "topics": [
                {
                    "topic_id": "topic-1",
                    "message_handles": list(packet["message_handles"]),
                    "candidate_handles": list(packet["candidate_handles"]),
                    "evidence_handles": list(packet["evidence_handles"]),
                    "relation": "same_topic",
                }
            ]
        }
        return DiagnosticProviderResponse(
            content=json.dumps(output, ensure_ascii=False, sort_keys=True),
            reasoning_content="SYNTHETIC SECRET REASONING",
            finish_reasons=("stop",),
            input_tokens=1200,
            output_tokens=30,
            usage_present=True,
            usage_keys=("prompt_tokens", "completion_tokens"),
            latency_ms=1.0,
            model=MODEL,
            source=self.source,
            response_fields=("choices", "content", "reasoning", "api_key"),
            request_id_present=True,
        )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def _load_outputs(output: Path) -> list[Any]:
    values: list[Any] = []
    for path in output.iterdir():
        if path.suffix == ".json":
            values.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            values.extend(_jsonl(path))
    return values


def _assert_body_free_artifact(output: Path) -> None:
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    assert "SYNTHETIC PRIVATE BODY" not in all_text
    assert "SYNTHETIC SECRET REASONING" not in all_text
    assert "sk-test-secret" not in all_text
    forbidden = {str(key).casefold() for key in BODY_KEYS} | {
        "reasoning",
        "reasoning_content",
        "raw_response",
        "raw_output",
        "raw_reasoning",
        "secret",
    }
    assert not any(str(key).casefold() in forbidden for value in _load_outputs(output) for key in _walk_keys(value))


def _config() -> DiagnosticProviderConfig:
    return DiagnosticProviderConfig(
        model=MODEL,
        base_url="https://example.invalid",
        api_key=None,
        response_format_mode=RESPONSE_FORMAT_MODE,
        thinking_disabled=THINKING_DISABLED,
    )


def test_k14_synthetic_run_reuses_health_selects_five_strata_and_is_bounded(tmp_path: Path) -> None:
    health = _build_health_artifact(tmp_path / "linear_stage_a_protocol_health_v2")
    input_root = _build_v2_input(tmp_path / "linear_stage_packet_development_v2")
    provider = SyntheticProvider()
    output = tmp_path / DEFAULT_ARTIFACT_DIRECTORY.name

    result = run_linear_stage_a_development_pilot(
        input_root,
        output,
        health_artifact_directory=health,
        provider=provider,
        config=_config(),
    )

    assert result.success is True
    assert result.status == "complete"
    assert result.health_reused is True
    assert result.selected_page_count == MAX_DEVELOPMENT_CALLS == 5
    assert result.provider_calls == MAX_PROVIDER_CALLS == 5
    assert result.retry_count == 0
    assert len(provider.calls) == 5  # no K13 health call is made here
    assert all(call["max_output_tokens"] == MAX_OUTPUT_TOKENS == 400 for call in provider.calls)

    aggregate = json.loads((output / "aggregate.private.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.private.json").read_text(encoding="utf-8"))
    assert aggregate["artifact_version"] == ARTIFACT_VERSION
    assert aggregate["health_reused"] is True
    assert aggregate["health_provider_calls"] == 0
    assert aggregate["health_call_count"] == 0
    assert aggregate["development_input_read"] is True
    assert aggregate["provider_calls"] == 5
    assert aggregate["provider"]["model"] == MODEL
    assert aggregate["provider"]["response_format_mode"] == RESPONSE_FORMAT_MODE
    assert aggregate["provider"]["response_format_sent"] is False
    assert aggregate["thinking_disabled"] is True
    assert aggregate["extra_body"]["global_settings_mutated"] is False
    assert manifest["provider_called"] is True
    assert manifest["provider_calls"] == 5
    assert manifest["retry_count"] == 0
    assert manifest["stage_b_pilot"] is False
    assert manifest["stage_c_pilot"] is False
    assert manifest["frozen_read"] is False
    assert manifest["production_state_written"] is False

    selection = _jsonl(output / "selection.private.jsonl")
    assert len(selection) == 5
    assert {row["categories"][0] for row in selection} == {
        "greeting_new_topic",
        "no_reply",
        "pronoun_person_object_state",
        "topic_shift",
        "candidate_competition",
    }
    assert all(row["status"] == "complete" for row in selection)
    ledger = _jsonl(output / "ledger.private.jsonl")
    assert len(ledger) == 5
    assert all(row["provider_call"] is True for row in ledger)
    assert all(row["cache_hit"] is False for row in ledger)
    assert all(row["retry_count"] == 0 for row in ledger)
    assert all(row["response_format_mode"] == RESPONSE_FORMAT_MODE for row in ledger)
    assert all(row["thinking_disabled"] is True for row in ledger)
    assert len(_jsonl(output / "decisions.private.jsonl")) == 5
    _assert_body_free_artifact(output)


def test_k14_pending_is_not_cached_and_next_run_uses_only_one_provider_call(tmp_path: Path) -> None:
    health = _build_health_artifact(tmp_path / "linear_stage_a_protocol_health_v2")
    input_root = _build_v2_input(tmp_path / "linear_stage_packet_development_v2")
    cache: dict[str, Mapping[str, Any]] = {}

    first_provider = SyntheticProvider(fail_call=2)
    first_output = tmp_path / "linear_stage_a_development_pilot_v1_first"
    first = run_linear_stage_a_development_pilot(
        input_root,
        first_output,
        health_artifact_directory=health,
        provider=first_provider,
        cache=cache,
        config=_config(),
    )
    assert first.success is False
    assert first.status == "partial"
    assert first.provider_calls == 5
    assert len(first_provider.calls) == 5
    assert len(cache) == 4
    first_ledger = _jsonl(first_output / "ledger.private.jsonl")
    assert sum(row["status"] == "pending" for row in first_ledger) == 1
    assert sum(row["cache_hit"] for row in first_ledger) == 0
    assert len(_jsonl(first_output / "decisions.private.jsonl")) == 4
    _assert_body_free_artifact(first_output)

    second_provider = SyntheticProvider()
    second_output = tmp_path / "linear_stage_a_development_pilot_v1_second"
    second = run_linear_stage_a_development_pilot(
        input_root,
        second_output,
        health_artifact_directory=health,
        provider=second_provider,
        cache=cache,
        config=_config(),
    )
    assert second.success is True
    assert second.status == "complete"
    assert second.provider_calls == 1
    assert len(second_provider.calls) == 1
    second_ledger = _jsonl(second_output / "ledger.private.jsonl")
    assert len(second_ledger) == 5
    assert sum(row["cache_hit"] for row in second_ledger) == 4
    assert sum(row["provider_call"] for row in second_ledger) == 1
    assert len(cache) == 5
    _assert_body_free_artifact(second_output)


def test_k14_invalid_health_blocks_without_reading_or_calling_provider(tmp_path: Path) -> None:
    health = tmp_path / "linear_stage_a_protocol_health_v2"
    health.mkdir()
    _write_json(
        health / "manifest.private.json",
        {
            "artifact_version": "linear_stage_a_protocol_health_v2",
            "status": "blocked",
            "success": False,
            "diagnostic_only": True,
            "provider_calls": 1,
            "diagnostic_call_count": 1,
            "retry_count": 0,
            "development_input_read": False,
            "frozen_read": False,
            "production_state_written": False,
            "thinking_disabled": True,
            "provider": {
                "model": MODEL,
                "source": "synthetic-health",
                "response_format_mode": RESPONSE_FORMAT_MODE,
                "response_format_sent": False,
            },
        },
    )
    _write_json(
        health / "aggregate.private.json",
        {
            "status": "blocked",
            "success": False,
            "diagnostic_only": True,
            "development_input_read": False,
            "stage_a_development": False,
            "stage_b_pilot": False,
            "frozen_read": False,
            "production_state_written": False,
            "settings_unchanged": True,
            "strict_result": {"strict_complete": False, "parse_code": "provider_invalid_json", "validation_code": "not_run"},
        },
    )
    provider = SyntheticProvider()
    output = tmp_path / "linear_stage_a_development_pilot_v1"
    result = run_linear_stage_a_development_pilot(
        tmp_path / "missing-linear_stage_packet_development_v2",
        output,
        health_artifact_directory=health,
        provider=provider,
        config=_config(),
    )
    assert result.success is False
    assert result.status == "blocked"
    assert result.health_reused is False
    assert result.selected_page_count == 0
    assert result.provider_calls == 0
    assert provider.calls == []
    aggregate = json.loads((output / "aggregate.private.json").read_text(encoding="utf-8"))
    assert aggregate["development_input_read"] is False
    assert aggregate["provider_calls"] == 0
    assert _jsonl(output / "selection.private.jsonl") == []
    assert _jsonl(output / "decisions.private.jsonl") == []
    _assert_body_free_artifact(output)


def test_k14_output_directory_is_immutable(tmp_path: Path) -> None:
    health = _build_health_artifact(tmp_path / "linear_stage_a_protocol_health_v2")
    provider = SyntheticProvider()
    output = tmp_path / "linear_stage_a_development_pilot_v1"
    output.mkdir()
    with pytest.raises(FileExistsError, match="development_output_is_immutable"):
        run_linear_stage_a_development_pilot(
            tmp_path / "missing-linear_stage_packet_development_v2",
            output,
            health_artifact_directory=health,
            provider=provider,
            config=_config(),
        )
