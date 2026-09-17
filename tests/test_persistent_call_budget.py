"""Synthetic K16 contracts for the durable pre-provider call guard.

These tests intentionally use temporary synthetic ledgers only.  No project
artifact, private data, frozen data, or provider is opened or called.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from wechat_bridge.persistent_call_budget import (
    AuthorizationBindingMismatch,
    CallAuthorizationLedger,
    CallBudgetExceeded,
    LedgerPathError,
    ReservationRejected,
    ReservationStateError,
    ledger_path_for,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ledger(path: Path, *, max_calls: int = 3, model: str = "deepseek-v4-flash") -> CallAuthorizationLedger:
    return CallAuthorizationLedger(
        path,
        authorization_id="K16_SYNTH_AUTH_01",
        max_calls=max_calls,
        provider="openai-compatible",
        model=model,
        protocol="linear-stage-a-compact-v1",
        settings_sha256=_digest("synthetic-settings-v1"),
        scope={"account_id": "ACCOUNT_SYNTH", "chat_id": "CHAT_SYNTH"},
        input_sha256=_digest("synthetic-input-v2"),
        artifact_namespace="synthetic-run-v1",
    )


def _process_reserve_worker(path_string: str, worker_index: int, queue: Any) -> None:
    """Spawn-safe worker used to prove cross-process atomic reservation."""

    try:
        ledger = _ledger(Path(path_string), max_calls=3)
        # This body exists only in the worker's memory and is intentionally
        # hashed by the ledger; it must never appear in the SQLite file.
        reservation = ledger.reserve(
            {"messages": [{"content": "synthetic secret %d" % worker_index}]},
            unit_ref="opaque-unit-%d" % worker_index,
        )
        queue.put(("reserved", reservation.ordinal))
    except CallBudgetExceeded:
        queue.put(("rejected", None))
    except Exception as exc:  # pragma: no cover - surfaced by parent assertion
        queue.put(("error", type(exc).__name__))


def test_reservation_is_atomic_and_every_provider_outcome_consumes_a_slot(tmp_path: Path) -> None:
    path = tmp_path / "authorization.sqlite3"
    ledger = _ledger(path, max_calls=3)

    failed = ledger.reserve({"unit": "failed"}, unit_ref="unit-failed")
    ledger.mark_started(failed)
    ledger.mark_failed(failed, error_code="provider_invalid_json", input_tokens=8, output_tokens=3)

    pending = ledger.reserve({"unit": "pending"}, unit_ref="unit-pending", attempt=1)
    ledger.mark_started(pending)
    ledger.mark_pending(pending, error_code="provider_timeout", input_tokens=9, output_tokens=0)

    # A crash between reserve and mark_started still consumes the third slot.
    abandoned_after_crash = ledger.reserve({"unit": "crashed"}, unit_ref="unit-crashed")
    assert abandoned_after_crash.ordinal == 3
    assert ledger.calls_used == 3

    with pytest.raises(CallBudgetExceeded) as exc_info:
        ledger.reserve({"unit": "must-not-call"}, unit_ref="unit-four")
    assert exc_info.value.code == "authorization_call_budget_exhausted"
    assert ledger.calls_used == 3
    assert [row["status"] for row in ledger.records()] == ["failed", "pending", "reserved"]
    assert ledger.snapshot()["status_counts"] == {"failed": 1, "pending": 1, "reserved": 1}
    assert ledger.snapshot()["rejection_count"] == 1


def test_same_authorization_reopens_across_instances_and_binding_cannot_drift(tmp_path: Path) -> None:
    path = tmp_path / "authorization.sqlite3"
    first = _ledger(path, max_calls=2)
    first.reserve({"unit": "one"}, unit_ref="one")

    reopened = _ledger(path, max_calls=2)
    assert reopened.calls_used == 1
    reopened.reserve({"unit": "two"}, unit_ref="two")
    assert first.calls_used == 2

    for changed in (
        {"model": "deepseek-v4-pro"},
        {"scope": {"account_id": "ACCOUNT_OTHER", "chat_id": "CHAT_SYNTH"}},
        {"artifact_namespace": "synthetic-run-v2"},
    ):
        kwargs = {
            "max_calls": 2,
            "model": changed.get("model", "deepseek-v4-flash"),
        }
        if "scope" in changed:
            # Construct directly to exercise the changed scope binding.
            kwargs["scope"] = changed["scope"]
        if "artifact_namespace" in changed:
            kwargs["artifact_namespace"] = changed["artifact_namespace"]
        with pytest.raises(AuthorizationBindingMismatch):
            CallAuthorizationLedger(
                path,
                authorization_id="K16_SYNTH_AUTH_01",
                provider="openai-compatible",
                protocol="linear-stage-a-compact-v1",
                settings_sha256=_digest("synthetic-settings-v1"),
                input_sha256=_digest("synthetic-input-v2"),
                **kwargs,
            )

    assert reopened.calls_used == 2


def test_cross_process_reservation_is_atomic_and_limit_is_global(tmp_path: Path) -> None:
    path = tmp_path / "cross-process.sqlite3"
    _ledger(path, max_calls=3)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_process_reserve_worker, args=(str(path), index, queue))
        for index in range(5)
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

    assert sum(result[0] == "reserved" for result in results) == 3
    assert sum(result[0] == "rejected" for result in results) == 2
    assert not any(result[0] == "error" for result in results)
    reopened = _ledger(path, max_calls=3)
    assert reopened.calls_used == 3
    assert sorted(row["ordinal"] for row in reopened.records()) == [1, 2, 3]


def test_request_is_hashed_not_persisted_and_export_is_body_free(tmp_path: Path) -> None:
    path = tmp_path / "body-free.sqlite3"
    ledger = _ledger(path, max_calls=1)
    secret = "SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"
    reservation = ledger.reserve(
        {"messages": [{"message_id": "m1", "content": secret}]},
        unit_ref="opaque-page-1",
    )
    ledger.mark_complete(reservation, input_tokens=10, output_tokens=4, latency_ms=1.5)
    exported = ledger.export_jsonl(tmp_path / "ledger.jsonl")

    database_bytes = path.read_bytes()
    export_text = exported.read_text(encoding="utf-8")
    assert secret.encode("utf-8") not in database_bytes
    assert secret not in export_text
    assert "messages" not in export_text
    rows = [json.loads(line) for line in export_text.splitlines() if line.strip()]
    assert rows[0]["status"] == "complete"
    assert rows[0]["request_sha256"] == hashlib.sha256(
        json.dumps(
            {"messages": [{"message_id": "m1", "content": secret}]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot = ledger.snapshot()
    assert snapshot["body_free"] is True
    assert snapshot["binding"]["scope_sha256"] == _digest(
        '{"account_id":"ACCOUNT_SYNTH","chat_id":"CHAT_SYNTH"}'
    )


def test_preflight_rejection_does_not_consume_provider_slot_but_hard_limit_does(tmp_path: Path) -> None:
    path = tmp_path / "preflight.sqlite3"
    ledger = CallAuthorizationLedger(
        path,
        authorization_id="K16_SYNTH_AUTH_02",
        max_calls=1,
        provider="openai-compatible",
        model="deepseek-v4-flash",
        protocol="linear-stage-a-compact-v1",
        settings_sha256=_digest("settings"),
        scope={"account_id": "A", "chat_id": "C"},
        max_input_tokens=4,
    )
    with pytest.raises(ReservationRejected) as rejected:
        ledger.reserve({"long": "synthetic"}, input_tokens_estimate=5)
    assert rejected.value.code == "input_token_limit_exceeded"
    assert ledger.calls_used == 0
    reservation = ledger.reserve({"short": "ok"}, input_tokens_estimate=1)
    assert ledger.calls_used == 1
    with pytest.raises(CallBudgetExceeded):
        ledger.reserve({"short": "second"}, input_tokens_estimate=1)
    assert ledger.calls_used == 1
    assert [row["code"] for row in ledger.rejections()] == [
        "input_token_limit_exceeded",
        "authorization_call_budget_exhausted",
    ]
    ledger.mark_failed(reservation, error_code="provider_exception_with_secret_should_be_sanitized")
    assert ledger.records()[0]["error_code"] == "provider_error"


def test_invalid_state_transitions_and_stable_derived_path(tmp_path: Path) -> None:
    derived1 = ledger_path_for(tmp_path / "authority", "K16_SYNTH_AUTH_03")
    derived2 = ledger_path_for(tmp_path / "authority", "K16_SYNTH_AUTH_03")
    assert derived1 == derived2
    ledger = CallAuthorizationLedger(
        derived1,
        authorization_id="K16_SYNTH_AUTH_03",
        max_calls=1,
        provider="p",
        model="m",
        protocol="proto",
        scope={"account_id": "A", "chat_id": "C"},
    )
    reservation = ledger.reserve(request_sha256=_digest("request"))
    ledger.mark_started(reservation)
    with pytest.raises(ReservationStateError):
        ledger.mark_started(reservation)
    ledger.mark_complete(reservation)
    with pytest.raises(ReservationStateError):
        ledger.mark_failed(reservation, error_code="late_failure")

    with pytest.raises(LedgerPathError):
        CallAuthorizationLedger(
            tmp_path / "frozen" / "ledger.sqlite3",
            authorization_id="K16_SYNTH_AUTH_04",
            max_calls=1,
        )
