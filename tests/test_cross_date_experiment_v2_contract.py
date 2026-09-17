"""Targeted adversarial checks for the v2 cross-date semantic sampler."""

import json
import sqlite3
from pathlib import Path

import pytest

from src.wechat_bridge.cross_date_experiment import (
    ExperimentConfig,
    _normalize_semantic_output,
    _semantic_output_budget,
    run_experiment,
    sample_sqlite_dialogue_bundles,
    supplement_one_output_limit_failure,
    write_outputs,
)


def _make_messages_db(path: Path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "create table messages (id integer, chat_id text, content text, timestamp text, "
        "sender_name text, is_self integer, is_group integer, message_type text)"
    )
    connection.executemany("insert into messages values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_episode_chunks_are_non_overlapping_and_deduplicated(tmp_path: Path):
    rows = [
        (index, "chat-a", f"message-{index}", f"2026-08-20T10:{index:02d}:00", "peer", 0, 0, "text")
        for index in range(16)
    ]
    rows += [
        (100 + index, "chat-b", f"other-{index}", f"2026-08-20T11:{index:02d}:00", "peer", 0, 0, "text")
        for index in range(8)
    ]
    db = tmp_path / "messages.sqlite"
    _make_messages_db(db, rows)
    sampled = sample_sqlite_dialogue_bundles(db, ["2026-08-20"], packages_per_date=5, exclude=0)
    packages = sampled["2026-08-20"]
    assert packages
    assert all(5 <= len(package["messages"]) <= 12 for package in packages)
    sequences = [tuple(row["message_id"] for row in package["messages"]) for package in packages]
    assert len(sequences) == len(set(sequences))
    all_ids = [message_id for sequence in sequences for message_id in sequence]
    assert len(all_ids) == len(set(all_ids))


def test_media_core_does_not_promote_neighbor_and_skips_provider(tmp_path: Path):
    package = {
        "id": "p-media",
        "chat_id": "chat",
        "messages": [
            {"id": "1", "message_type": "image", "content": "[图片]", "role": "primary"},
            {"id": "2", "message_type": "text", "content": "邻近文本"},
            {"id": "3", "message_type": "text", "content": "另一条邻近文本"},
        ],
        "core": {"message_ids": ["1"]},
    }
    calls = []

    def provider(*_args, **_kwargs):
        calls.append(True)
        return {}

    payload = run_experiment(
        ExperimentConfig(dates=("2026-08-20",), packages_per_date=1, persistent_cap=1, source="v2-media", authorization_id="v2-media"),
        {"2026-08-20": [package]}, provider=provider, authority_root=tmp_path / "ledger",
    )
    row = payload["results"][0]
    assert not calls
    assert row["provider_call"] is False
    assert row["error_code"] == "request_primary_messages_empty"
    assert row["core_message_refs"] == []


def test_dynamic_budget_is_bounded_and_sent_to_provider(tmp_path: Path):
    assert _semantic_output_budget(1, 4)[0] == 8192
    assert _semantic_output_budget(1, 40)[0] == 8192
    seen = {}

    class FakeModel:
        model_id = "fake"
        source = "test"

        def complete(self, _prompt, request, *, max_output_tokens):
            seen["cap"] = max_output_tokens
            return json.dumps({
                "topics": [{"topic_id": "t1", "label": "主题", "primary_aliases": ["m1"], "context_aliases": ["m2"], "uncertainty": "low", "evidence_aliases": ["m1"]}],
                "people": [], "objects": [], "states": [], "overall_uncertainties": [],
            }, ensure_ascii=False)

    package = {"id": "p", "chat_id": "c", "messages": [
        {"id": "1", "content": "核心", "role": "primary"},
        {"id": "2", "content": "上下文", "role": "context"},
    ]}
    payload = run_experiment(
        ExperimentConfig(dates=("2026-08-20",), packages_per_date=1, persistent_cap=1, source="v2-budget", authorization_id="v2-budget"),
        {"2026-08-20": [package]}, provider=FakeModel(), authority_root=tmp_path / "ledger",
    )
    assert seen["cap"] == 8192
    assert payload["results"][0]["output_token_budget"] == 8192
    assert "semantic_output_budget_v3_configured_max" in payload["results"][0]["output_budget_reason"]
    assert payload["manifest"]["output_budget_configured"] == 8192
    assert payload["manifest"]["output_budget_effective"] == 8192
    assert payload["manifest"]["provider_capability_unknown"] is True


def test_semantic_budget_env_override_and_invalid_value_fail_closed(monkeypatch):
    monkeypatch.setenv("CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS", "1234")
    budget, reason = _semantic_output_budget(1, 4)
    assert budget == 1234
    assert "source=environment" in reason
    monkeypatch.setenv("CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS", "0")
    with pytest.raises(ValueError, match="invalid_cross_date_semantic_max_output_tokens"):
        _semantic_output_budget(1, 4)
    monkeypatch.setenv("CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS", "not-a-number")
    with pytest.raises(ValueError, match="invalid_cross_date_semantic_max_output_tokens"):
        _semantic_output_budget(1, 4)


def test_outputs_include_manifest_and_body_free_errors(tmp_path: Path):
    payload = run_experiment(
        ExperimentConfig(dates=("2026-08-20",), packages_per_date=1, persistent_cap=1, source="v2-output", authorization_id="v2-output"),
        {"2026-08-20": [{"id": "p", "messages": [{"id": "1", "message_type": "image", "content": "[图片]", "role": "primary"}]}]},
        provider=lambda *_args, **_kwargs: {}, authority_root=tmp_path / "ledger",
    )
    write_outputs(payload, tmp_path / "out")
    assert (tmp_path / "out" / "manifest.json").exists()
    errors = json.loads((tmp_path / "out" / "errors.json").read_text(encoding="utf-8"))
    assert errors["errors"][0]["error_code"] == "request_primary_messages_empty"
    assert "content" not in json.dumps(errors, ensure_ascii=False)


def test_validator_remains_strict_for_out_of_scope_evidence():
    model_input = [{"alias": "m1", "speaker": "我", "time": "10:00", "text": "核心", "role": "primary"}]
    try:
        _normalize_semantic_output({
            "topics": [{"topic_id": "t1", "label": "主题", "primary_aliases": ["m1"], "context_aliases": [], "uncertainty": "low", "evidence_aliases": ["m9"]}],
            "people": [], "objects": [], "states": [], "overall_uncertainties": [],
        }, model_input)
    except ValueError as exc:
        assert str(exc) == "semantic_alias_out_of_scope"
    else:
        raise AssertionError("out-of-scope evidence must fail closed")


def test_targeted_supplement_uses_one_independent_call_and_preserves_initial(tmp_path: Path):
    rows = [
        (index, "chat-a", f"message-{index}", f"2026-08-20T10:{index:02d}:00", "peer", 0, 0, "text")
        for index in range(5)
    ]
    db = tmp_path / "messages.sqlite"
    _make_messages_db(db, rows)
    samples = sample_sqlite_dialogue_bundles(db, ["2026-08-20"], packages_per_date=1, exclude=0)
    package_id = samples["2026-08-20"][0]["package_id"]
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({
        "source": "cross_date_deepseek_experiment_v2",
        "config": {
            "dates": ["2026-08-20"], "packages_per_date": 1, "exclude": 0,
        },
        "manifest": {
            "output_budget_version": "semantic_output_budget_v1",
            "output_budget_formula": "min(1200,max(600,220+110*primary_count+30*necessary_count))",
        },
        "authorization": {"authorization_id": "initial", "calls_used": 1},
        "provider_calls": 1, "provider_calls_total": 1,
        "results": [{
            "date": "2026-08-20", "package_id": package_id,
            "status": "pending", "provider_call": True, "unknown": 1,
            "error": "output_token_limit_exceeded",
            "error_code": "output_token_limit_exceeded",
            "output_token_budget": 660, "output_tokens": 660,
            "request_sha256": "a" * 64,
            "model_message_ids": [str(row[0]) for row in rows],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    calls = []

    class FakeModel:
        model_id = "fake"
        source = "test"

        def complete(self, _prompt, request, *, max_output_tokens):
            calls.append(max_output_tokens)
            assert request["core_aliases"] == ["m3"]
            return json.dumps({
                "topics": [{
                    "topic_id": "t1", "label": "主题", "primary_aliases": ["m3"],
                    "context_aliases": ["m1"], "uncertainty": "low",
                    "evidence_aliases": ["m3"],
                }],
                "people": [], "objects": [], "states": [],
                "overall_uncertainties": [],
            }, ensure_ascii=False)

    output = supplement_one_output_limit_failure(
        results_path,
        db,
        provider=FakeModel(),
        authority_root=tmp_path / "supplement-authority",
    )
    assert calls == [8192]
    result = json.loads(results_path.read_text(encoding="utf-8"))
    row = result["results"][0]
    assert row["status"] == "complete"
    assert row["initial"]["error_code"] == "output_token_limit_exceeded"
    assert row["history"][0]["phase"] == "initial"
    assert row["history"][1]["phase"] == "supplement"
    assert row["supplement"]["status"] == "complete"
    assert row["supplement"]["output_token_budget"] == 8192
    assert result["supplement"]["provider_calls"] == 1
    assert result["supplement"]["no_other_rows_rerun"] is True
    assert all(Path(path).exists() for path in output)
    errors = json.loads((tmp_path / "errors.json").read_text(encoding="utf-8"))
    assert errors["errors"][0]["phase"] == "initial"
    assert errors["errors"][0]["resolved_by_supplement"] is True
