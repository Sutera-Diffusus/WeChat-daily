"""Offline K17 contracts for the compact Stage-A synthetic health runner."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
from typing import Any, Mapping

import pytest

from wechat_bridge.compact_stage_a_protocol_health import (
    ARTIFACT_VERSION,
    FakeCompactStageAHealthModel,
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    PROMPT_VERSION,
    PROTOCOL_VERSION,
    RESPONSE_FORMAT_MODE,
    RUNNER_SCHEMA_VERSION,
    THINKING_DISABLED,
    build_synthetic_health_request,
    run_compact_stage_a_protocol_health,
)


# Keep the test independent of private/frozen data.  The marker exists only
# in a synthetic model response and must never enter persisted artifacts.
BODY_MARKER = "K17_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"
BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "message",
        "messages",
        "prompt",
        "raw",
        "raw_response",
        "response",
        "text",
        "user_input",
    }
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _assert_body_free(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert BODY_MARKER not in encoded
    bad: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key).casefold()
                is_ref = key_text.endswith(("_ref", "_refs", "_handle", "_handles"))
                if key_text in BODY_KEYS and child not in (None, "", [], {}, ()) and not is_ref:
                    bad.append(str(key))
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    assert not bad, bad


def _read_artifacts(output: Path) -> dict[str, Any]:
    manifest = json.loads((output / "manifest.private.json").read_text(encoding="utf-8"))
    aggregate = json.loads((output / "aggregate.private.json").read_text(encoding="utf-8"))
    cost = json.loads((output / "cost.private.json").read_text(encoding="utf-8"))
    diagnostic = json.loads((output / "diagnostic.private.json").read_text(encoding="utf-8"))
    ledger = [
        json.loads(line)
        for line in (output / "ledger.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    errors = [
        json.loads(line)
        for line in (output / "errors.private.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": diagnostic,
        "ledger": ledger,
        "errors": errors,
    }


def _assert_hashes(output: Path, artifacts: Mapping[str, Any]) -> None:
    manifest = artifacts["manifest"]
    expected = manifest["artifact_hashes"]
    for filename, digest in expected.items():
        assert _sha256_bytes((output / filename).read_bytes()) == digest


def _cross_process_health_worker(
    output_directory: str,
    authority_root: str,
    queue: Any,
) -> None:
    """Spawn-safe worker proving the authorization is not process-local."""

    model = FakeCompactStageAHealthModel()
    result = run_compact_stage_a_protocol_health(
        Path(output_directory),
        model=model,
        authority_root=Path(authority_root),
        authorization_id="K17_CROSS_PROCESS_CASE",
    )
    queue.put((result.provider_calls, result.error_code, len(model.calls)))


def test_valid_synthetic_health_is_one_call_and_strict_complete(tmp_path: Path) -> None:
    model = FakeCompactStageAHealthModel()
    output = tmp_path / "health"
    result = run_compact_stage_a_protocol_health(
        output,
        model=model,
        authority_root=tmp_path / "authority",
    )

    assert result.success is True
    assert result.status == "available"
    assert result.strict_complete is True
    assert result.provider_calls == 1
    assert result.retry_count == 0
    assert len(model.calls) == 1
    assert model.calls[0]["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert model.calls[0]["extra_body_field_names"] == ["thinking"]

    artifacts = _read_artifacts(output)
    manifest = artifacts["manifest"]
    aggregate = artifacts["aggregate"]
    assert manifest["artifact_version"] == ARTIFACT_VERSION
    assert manifest["schema_version"] == RUNNER_SCHEMA_VERSION
    assert manifest["provider"]["model"] == MODEL_ID
    assert manifest["protocol_version"] == PROTOCOL_VERSION
    assert manifest["prompt_version"] == PROMPT_VERSION
    assert manifest["response_format_mode"] == RESPONSE_FORMAT_MODE
    assert manifest["response_format_sent"] is False
    assert manifest["thinking_disabled"] is THINKING_DISABLED
    assert manifest["development_input_read"] is False
    assert manifest["private_input_read"] is False
    assert manifest["frozen_read"] is False
    assert manifest["gold_loaded"] is False
    assert aggregate["strict_result"]["strict_complete"] is True
    assert aggregate["strict_result"]["accepted_for_complete"] is True
    assert aggregate["diagnostic_candidate"]["accepted_for_complete"] is False
    assert artifacts["cost"]["provider_calls"] == 1
    assert artifacts["ledger"][0]["status"] == "complete"
    for value in artifacts.values():
        _assert_body_free(value)
    _assert_hashes(output, artifacts)


def test_default_path_is_blocked_without_any_model_call(tmp_path: Path) -> None:
    result = run_compact_stage_a_protocol_health(tmp_path / "no-model")
    assert result.status == "blocked"
    assert result.success is False
    assert result.provider_calls == 0
    assert result.error_code == "synthetic_model_required"
    artifacts = _read_artifacts(tmp_path / "no-model")
    assert artifacts["manifest"]["provider_called"] is False
    assert artifacts["aggregate"]["authorization_ledger"]["calls_used"] == 0
    assert artifacts["errors"][0]["error_code"] == "synthetic_model_required"
    for value in artifacts.values():
        _assert_body_free(value)


def test_invalid_or_truncated_candidate_never_becomes_complete(tmp_path: Path) -> None:
    request = build_synthetic_health_request()
    valid = FakeCompactStageAHealthModel.valid_content(request)
    model = FakeCompactStageAHealthModel(
        content=valid,
        output_tokens=MAX_OUTPUT_TOKENS,
        finish_reason="length",
    )
    output = tmp_path / "truncated"
    result = run_compact_stage_a_protocol_health(
        output,
        model=model,
        authority_root=tmp_path / "authority",
        authorization_id="K17_TRUNCATED_CASE",
    )
    assert result.success is False
    assert result.status == "blocked"
    assert result.strict_complete is False
    assert result.provider_calls == 1
    assert len(model.calls) == 1
    artifacts = _read_artifacts(output)
    assert artifacts["aggregate"]["diagnostic_candidate"]["schema_valid"] is True
    assert artifacts["aggregate"]["diagnostic_candidate"]["accepted_for_complete"] is False
    assert artifacts["aggregate"]["strict_result"]["strict_complete"] is False
    assert artifacts["aggregate"]["response_diagnostics"]["finish_reason"] == "length"
    assert artifacts["errors"][0]["error_code"] == "output_token_limit_exceeded"
    assert artifacts["ledger"][0]["status"] == "failed"


def test_malformed_json_is_body_free_and_has_no_retry(tmp_path: Path) -> None:
    model = FakeCompactStageAHealthModel(
        content='{"t":[{"i":"t1","p":["m1"],',
        input_tokens=11,
        output_tokens=17,
    )
    output = tmp_path / "invalid"
    result = run_compact_stage_a_protocol_health(
        output,
        model=model,
        authority_root=tmp_path / "authority",
        authorization_id="K17_INVALID_CASE",
    )
    assert result.provider_calls == 1
    assert len(model.calls) == 1
    assert result.success is False
    artifacts = _read_artifacts(output)
    assert artifacts["aggregate"]["strict_result"]["strict_parse_code"] != "ok"
    assert artifacts["aggregate"]["diagnostic_candidate"]["accepted_for_complete"] is False
    assert artifacts["aggregate"]["response_diagnostics"]["output_sha256"]
    for value in artifacts.values():
        _assert_body_free(value)


def test_authorization_ledger_is_global_across_output_directories(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    first_model = FakeCompactStageAHealthModel()
    first = run_compact_stage_a_protocol_health(
        tmp_path / "first",
        model=first_model,
        authority_root=authority,
        authorization_id="K17_GLOBAL_CASE",
    )
    assert first.success is True
    assert len(first_model.calls) == 1

    second_model = FakeCompactStageAHealthModel()
    second = run_compact_stage_a_protocol_health(
        tmp_path / "second",
        model=second_model,
        authority_root=authority,
        authorization_id="K17_GLOBAL_CASE",
    )
    assert second.success is False
    assert second.status == "blocked"
    assert second.provider_calls == 0
    assert second.error_code == "authorization_call_budget_exhausted"
    assert second_model.calls == []
    artifacts = _read_artifacts(tmp_path / "second")
    ledger = artifacts["aggregate"]["authorization_ledger"]
    assert ledger["calls_used"] == 1
    assert ledger["calls_remaining"] == 0
    assert ledger["rejection_count"] >= 1
    assert artifacts["errors"][0]["error_code"] == "authorization_call_budget_exhausted"


def test_authorization_ledger_is_atomic_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    authority = tmp_path / "authority"
    processes = [
        context.Process(
            target=_cross_process_health_worker,
            args=(str(tmp_path / ("process-%d" % index)), str(authority), queue),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert process.exitcode == 0
    assert sorted(results) == [(0, "authorization_call_budget_exhausted", 0), (1, None, 1)]


def test_settings_hash_is_observed_without_mutating_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model":"deepseek-v4-flash","synthetic":true}\n', encoding="utf-8")
    before = settings_path.read_bytes()
    model = FakeCompactStageAHealthModel()
    output = tmp_path / "settings"
    result = run_compact_stage_a_protocol_health(
        output,
        model=model,
        authority_root=tmp_path / "authority",
        authorization_id="K17_SETTINGS_CASE",
        settings_path=settings_path,
    )
    assert result.success is True
    assert settings_path.read_bytes() == before
    artifacts = _read_artifacts(output)
    assert artifacts["manifest"]["settings_unchanged"] is True
    assert artifacts["manifest"]["settings_before_sha256"] == _sha256_bytes(before)
    assert artifacts["manifest"]["settings_after_sha256"] == _sha256_bytes(before)


class _SecretRaisingModel:
    model_id = MODEL_ID
    source = "synthetic"
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int, extra_body: Mapping[str, Any]) -> Any:
        self.calls += 1
        raise RuntimeError("provider secret body K17_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED")


def test_provider_exception_is_sanitized_and_reservation_is_failed(tmp_path: Path) -> None:
    model = _SecretRaisingModel()
    output = tmp_path / "exception"
    result = run_compact_stage_a_protocol_health(
        output,
        model=model,
        authority_root=tmp_path / "authority",
        authorization_id="K17_EXCEPTION_CASE",
    )
    assert model.calls == 1
    assert result.success is False
    assert result.error_code == "provider_error"
    artifacts = _read_artifacts(output)
    assert artifacts["errors"][0]["error_code"] == "provider_error"
    assert artifacts["ledger"][0]["status"] == "failed"
    for value in artifacts.values():
        _assert_body_free(value)


class _SettingsMutatingModel(FakeCompactStageAHealthModel):
    def __init__(self, settings_path: Path) -> None:
        super().__init__()
        self._settings_path = settings_path

    def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int, extra_body: Mapping[str, Any]) -> Any:
        result = super().complete(
            system_prompt,
            request,
            max_output_tokens=max_output_tokens,
            extra_body=extra_body,
        )
        self._settings_path.write_text('{"mutated":true}\n', encoding="utf-8")
        return result


def test_settings_mutation_cannot_produce_complete_health(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"stable":true}\n', encoding="utf-8")
    model = _SettingsMutatingModel(settings_path)
    output = tmp_path / "mutated-settings"
    result = run_compact_stage_a_protocol_health(
        output,
        model=model,
        authority_root=tmp_path / "authority",
        authorization_id="K17_MUTATION_CASE",
        settings_path=settings_path,
    )
    assert result.success is False
    assert result.error_code == "settings_mutated"
    artifacts = _read_artifacts(output)
    assert artifacts["manifest"]["settings_unchanged"] is False
    assert artifacts["aggregate"]["strict_result"]["strict_complete"] is False
