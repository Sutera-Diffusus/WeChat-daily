"""Synthetic and protocol regressions for the isolated K11 Stage-A pilot."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from wechat_bridge.linear_stage_a_pilot import (
    BODY_KEYS,
    CATEGORY_NAMES,
    OUTPUT_TOPIC_KEYS,
    REQUEST_FORBIDDEN_KEYS,
    REQUEST_TOP_KEYS,
    PilotProviderConfig,
    DeepSeekStageAProvider,
    _health_request,
    run_linear_stage_a_pilot,
    validate_stage_a_output,
    validate_stage_a_request,
)
from wechat_bridge.staged_deepseek_analyzer import StageModelResponse, StageProviderError


def _handle(account: str, chat: str, kind: str, value: str) -> str:
    return "%s/%s|%s|%s" % (account, chat, kind, value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _valid_output(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
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


def _build_synthetic_v2(root: Path) -> Path:
    """Build six complete v2 pages, each with deliberately distinct strata."""

    root.mkdir(parents=True)
    pages: list[dict[str, Any]] = []
    materialized: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    content_table: dict[str, dict[str, Any]] = {}
    category_specs = (
        "competition",
        "pronoun",
        "shift",
        "greeting",
        "no_reply",
        "filler",
    )
    for page_index, category in enumerate(category_specs):
        account = "ACCOUNT_SYNTH_%02d" % page_index
        chat = "CHAT_SYNTH_%02d" % page_index
        root_id = "ROOT_SYNTH_%02d" % page_index
        page_id = "%s|page|0001" % root_id
        message_handles = [_handle(account, chat, "message", "M%02d_%d" % (page_index, n)) for n in range(2)]
        evidence_handles = [_handle(account, chat, "evidence", "E%02d_%d" % (page_index, n)) for n in range(2)]
        if category == "competition":
            candidate_count = 12
        elif category == "pronoun":
            candidate_count = 3
        else:
            candidate_count = 1
        candidate_handles = [
            _handle(account, chat, "candidate", "C%02d_%d" % (page_index, n))
            for n in range(candidate_count)
        ]
        pages.append(
            {
                "candidate_handles": candidate_handles,
                "candidate_link_refs": list(candidate_handles),
                "evidence_handles": evidence_handles,
                "message_handles": message_handles,
                "ordinal": 1,
                "page_hash": "page-hash-%02d" % page_index,
                "page_id": page_id,
                "root_id": root_id,
                "scope": {"account_id": account, "chat_id": chat},
                "source_packet_id": root_id,
                "status": "open",
            }
        )
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
            content_handle = _handle(account, chat, "content", "BODY_%02d_%d" % (page_index, message_index))
            identity: dict[str, Any] = {
                "segment_id": "SEGMENT_%02d_A" % page_index,
                "reply_to_message_id": None,
                "is_opener": False,
                "fragment_type": "statement",
            }
            if category == "shift" and message_index == 1:
                identity["segment_id"] = "SEGMENT_%02d_B" % page_index
            if category == "greeting":
                identity["is_opener"] = True
                identity["fragment_type"] = "conversation_opener"
            messages.append(
                {
                    "message_handle": message_handle,
                    "message_id": "M%02d_%d" % (page_index, message_index),
                    "roles": ["primary"] if message_index == 0 else ["adjacent"],
                    "identity_row": identity,
                    "content_handles": [content_handle],
                    "scope": {"account_id": account, "chat_id": chat},
                }
            )
            content_table[content_handle] = {
                "content_handle": content_handle,
                "content_id": "BODY_%02d_%d" % (page_index, message_index),
                "body": "synthetic body %02d %d should never be persisted" % (page_index, message_index),
                "body_hash": "body-hash-%02d-%d" % (page_index, message_index),
                "scope": {"account_id": account, "chat_id": chat},
            }
        for candidate_index, candidate_handle in enumerate(candidate_handles):
            views: list[str] = []
            reasons: list[str] = []
            if category == "competition":
                views = ["view-%02d" % candidate_index]
            elif category == "pronoun":
                views = [[
                    "candidate_person_history",
                    "candidate_object_history",
                    "candidate_state_history",
                ][candidate_index]]
            elif category == "no_reply":
                reasons = ["time_proximity_weak", "same_segment_weak"]
            candidates.append(
                {
                    "candidate_handle": candidate_handle,
                    "candidate_reason": reasons,
                    "evidence_handle_refs": [evidence_handles[candidate_index % 2]],
                    "message_ids": ["M%02d_0" % page_index, "M%02d_1" % page_index],
                    "relation_subtype": "candidate_only",
                    "relation_label": "candidate_only",
                    "uncertainties": [],
                    "view_names": views,
                    "scope": {"account_id": account, "chat_id": chat},
                }
            )
    manifest = {
        "artifact_version": "linear_stage_packet_development_v2",
        "split": "development",
        "local_day": "2026-08-25",
        "frozen_read": False,
        "provider_called": False,
        "provider_calls": 0,
        "input_selected_digest": "synthetic-input-digest",
        "page_count": len(pages),
        "root_count": len(pages),
    }
    _write_json(root / "manifest.private.json", manifest)
    _write_jsonl(root / "pages.private.jsonl", pages)
    _write_jsonl(root / "materialized_map.private.jsonl", materialized)
    _write_json(
        root / "store.private.json",
        {
            "schema_version": "synthetic-store-v1",
            "messages": messages,
            "candidates": candidates,
            "content_table": content_table,
        },
    )
    return root


class SyntheticProvider:
    source = "synthetic-provider"
    model_id = "synthetic-stage-a"

    def __init__(self, *, configured: bool = True, fail_development: bool = False) -> None:
        self.configured = configured
        self.fail_development = fail_development
        self.calls: list[dict[str, Any]] = []

    def complete(self, stage: str, system_prompt: str, user_packet: Mapping[str, Any], *, max_output_tokens: int) -> StageModelResponse:
        del stage, system_prompt
        packet = deepcopy(dict(user_packet))
        self.calls.append({"packet": packet, "max_output_tokens": max_output_tokens})
        if len(self.calls) > 1 and self.fail_development and len(self.calls) == 2:
            return StageModelResponse(
                payload={"topics": []},
                input_tokens=10,
                output_tokens=10,
                latency_ms=1.0,
                model=self.model_id,
                request_id="synthetic-invalid",
            )
        return StageModelResponse(
            payload=_valid_output(packet),
            input_tokens=20,
            output_tokens=30,
            latency_ms=1.0,
            model=self.model_id,
            request_id="synthetic-%d" % len(self.calls),
        )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_no_forbidden_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            assert str(key).casefold() not in {str(item).casefold() for item in REQUEST_FORBIDDEN_KEYS}
            _assert_no_forbidden_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_forbidden_keys(child)


def test_health_gates_read_and_selects_five_strata(tmp_path: Path) -> None:
    input_root = _build_synthetic_v2(tmp_path / "linear_stage_packet_development_v2")
    provider = SyntheticProvider()
    output_root = tmp_path / "linear_stage_a_pilot_v1"
    result = run_linear_stage_a_pilot(input_root, output_root, provider=provider)

    assert result.success is True
    assert result.status == "complete"
    assert result.selected_page_count == 5
    assert result.provider_calls == 6
    assert len(provider.calls) == 6
    assert provider.calls[0]["packet"]["page_id"] == "SYNTHETIC_HEALTH_PAGE"
    assert provider.calls[0]["max_output_tokens"] == 300
    assert all(call["max_output_tokens"] == 300 for call in provider.calls[1:])
    assert all(set(call["packet"]) == set(REQUEST_TOP_KEYS) for call in provider.calls)
    for call in provider.calls:
        validate_stage_a_request(call["packet"])
        _assert_no_forbidden_keys(call["packet"])

    aggregate = json.loads((output_root / "aggregate.private.json").read_text(encoding="utf-8"))
    assert aggregate["development_input_read"] is True
    assert aggregate["topic_coverage"]["pages"] == {"selected": 5, "complete": 5, "pending": 0}
    assert all(aggregate["topic_coverage"]["category_flags"][name]["selected_pages"] == 1 for name in CATEGORY_NAMES)
    assert aggregate["evidence_bindings"]["bound_handles"] == aggregate["evidence_bindings"]["expected_handles"]
    assert aggregate["cost"]["provider_calls"] == 6
    assert aggregate["cache_prefix_stability"]["stable"] is True
    ledger = _jsonl(output_root / "ledger.private.jsonl")
    decisions = _jsonl(output_root / "decisions.private.jsonl")
    assert len(ledger) == 6
    assert len(decisions) == 5
    assert all(row["status"] == "complete" for row in ledger)
    assert all(row["cache_hit"] is False for row in ledger)
    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in output_root.iterdir())
    assert "synthetic body" not in artifact_text
    assert "message_text" not in artifact_text
    assert "raw_response" not in artifact_text
    for path in output_root.iterdir():
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else _jsonl(path)
        assert not any(str(key).casefold() in {str(item).casefold() for item in BODY_KEYS} for key in _walk_keys(loaded))


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def test_health_failure_writes_blocked_artifact_without_reading_input(tmp_path: Path) -> None:
    provider = SyntheticProvider(configured=False)
    output_root = tmp_path / "linear_stage_a_pilot_v1"
    result = run_linear_stage_a_pilot(tmp_path / "missing-linear_stage_packet_development_v2", output_root, provider=provider)

    assert result.success is False
    assert result.status == "blocked"
    assert result.provider_calls == 0
    assert provider.calls == []
    aggregate = json.loads((output_root / "aggregate.private.json").read_text(encoding="utf-8"))
    assert aggregate["development_input_read"] is False
    assert aggregate["health"]["error_code"] == "provider_unconfigured"
    assert _jsonl(output_root / "selection.private.jsonl") == []
    assert _jsonl(output_root / "decisions.private.jsonl") == []


def test_failed_development_is_pending_and_not_cached(tmp_path: Path) -> None:
    input_root = _build_synthetic_v2(tmp_path / "linear_stage_packet_development_v2")
    cache: dict[str, Mapping[str, Any]] = {}
    failing_provider = SyntheticProvider(fail_development=True)
    first_output = tmp_path / "linear_stage_a_pilot_v1_first"
    first = run_linear_stage_a_pilot(input_root, first_output, provider=failing_provider, cache=cache)
    assert first.status == "partial"
    assert first.success is False
    assert first.provider_calls == 6
    assert len(cache) == 4
    assert len(_jsonl(first_output / "decisions.private.jsonl")) == 4
    assert "stage_a_output_topics" in first.aggregate["errors"]["codes"]
    first_ledger = _jsonl(first_output / "ledger.private.jsonl")
    assert sum(row["status"] == "pending" for row in first_ledger) == 1
    assert sum(row["cache_hit"] for row in first_ledger) == 0

    recovering_provider = SyntheticProvider()
    second_output = tmp_path / "linear_stage_a_pilot_v1_second"
    second = run_linear_stage_a_pilot(input_root, second_output, provider=recovering_provider, cache=cache)
    assert second.success is True
    assert second.provider_calls == 2  # fresh health plus the previously pending page
    assert len(recovering_provider.calls) == 2
    second_ledger = _jsonl(second_output / "ledger.private.jsonl")
    assert sum(row["cache_hit"] for row in second_ledger) == 4
    assert len(cache) == 5


def test_validator_rejects_canonical_17_field_or_handle_gaps() -> None:
    packet = _health_request()
    valid = _valid_output(packet)
    validate_stage_a_output(valid, packet)
    malformed = deepcopy(valid)
    malformed["topics"][0]["speaker"] = "forbidden"
    with pytest.raises(ValueError):
        validate_stage_a_output(malformed, packet)
    malformed = deepcopy(valid)
    malformed["topics"][0]["message_handles"] = []
    with pytest.raises(ValueError):
        validate_stage_a_output(malformed, packet)


def test_deepseek_adapter_uses_json_mode_without_changing_schema() -> None:
    packet = _health_request()
    output = _valid_output(packet)
    calls: list[dict[str, Any]] = []

    class Completions:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(
                id="synthetic-response",
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    config = PilotProviderConfig(model="deepseek-test", base_url="https://example.invalid", api_key="dummy")
    response = DeepSeekStageAProvider(config, client=client).complete(
        "A", "system", packet, max_output_tokens=300
    )
    assert response.payload == output
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 300
    assert "17-field" not in calls[0]["messages"][0]["content"]
    assert "api_key" not in calls[0]


def test_settings_snapshot_is_redacted_from_public_provider_config(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    _write_json(
        settings_path,
        {
            "ai": {
                "model": "deepseek-test",
                "base_url": "https://example.invalid",
                "api_key": "sk-test-secret-value",
            }
        },
    )
    config = PilotProviderConfig.from_workbench_settings(settings_path)
    public = config.public_dict()
    assert config.configured is True
    assert public["model"] == "deepseek-test"
    assert public["api_key_configured"] is True
    assert "api_key" not in public
    assert "sk-test-secret-value" not in json.dumps(public)
