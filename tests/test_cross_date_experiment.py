from pathlib import Path
import json
import sqlite3

from src.wechat_bridge.cross_date_experiment import (
    ExperimentConfig,
    _map_model_evidence,
    _normalize_semantic_output,
    _render_message_window,
    run_experiment,
    sample_sqlite,
    sample_sqlite_dialogue_bundles,
    write_outputs,
)


def test_sample_sqlite_is_read_only_and_applies_exclusion(tmp_path: Path):
    db = tmp_path / "sample.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table messages (id integer, date text, body text)")
    conn.executemany("insert into messages values (?, ?, ?)", [(i, "2026-08-20", f"m{i}") for i in range(30)])
    conn.commit(); conn.close()
    sample = sample_sqlite(db, ["2026-08-20"], packages_per_date=5, exclude=25)
    assert [row["id"] for row in sample["2026-08-20"]] == [25, 26, 27, 28, 29]


def test_run_continues_after_provider_failure_and_caps_results(tmp_path: Path):
    calls = []

    def provider(package, date):
        calls.append(package["id"])
        if package["id"] == 0:
            raise RuntimeError("boom")
        return {"status": "ok", "topic": "topic", "evidence": "evidence", "token": 7}

    config = ExperimentConfig(dates=("2026-08-20", "2026-08-21"), packages_per_date=5, persistent_cap=3)
    samples = {date: [{"id": i} for i in range(5)] for date in config.dates}
    payload = run_experiment(config, samples, provider=provider, authority_root=tmp_path)
    assert payload["total"] == 3
    assert len(calls) == 3
    assert payload["results"][0]["status"] == "error"
    assert payload["production_blocked"] is True


def test_outputs_include_review_controls(tmp_path: Path):
    payload = run_experiment(
        ExperimentConfig(dates=("2026-08-20",)),
        {"2026-08-20": [{"id": "p1"}]},
        provider=lambda package, date: {"status": "ok"},
        authority_root=tmp_path / "ledger",
    )
    json_path, html_path = write_outputs(payload, tmp_path / "out")
    assert json_path.exists() and html_path.exists()
    html = html_path.read_text(encoding="utf-8")
    assert "localStorage" in html
    assert "Export review JSON" in html


def test_future_sampling_builds_multi_message_same_scope_bundle(tmp_path: Path):
    db = tmp_path / "bundle.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table messages (id integer, date text, chat_id text, content text, timestamp text, sender_name text, is_self integer, is_group integer, message_type text)"
    )
    conn.executemany(
        "insert into messages values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (100 + i, "2026-08-20", "chat-1", f"turn-{i}", f"2026-08-20T10:0{i}:00", "peer", 0, 0, "text")
            for i in range(7)
        ],
    )
    conn.commit()
    conn.close()

    packages = sample_sqlite_dialogue_bundles(
        db,
        ["2026-08-20"],
        packages_per_date=1,
        exclude=0,
        context_radius=3,
    )
    package = packages["2026-08-20"][0]
    assert len(package["messages"]) >= 3
    assert len({message["chat_id"] for message in package["messages"]}) == 1
    assert package["core"]["message_ids"]
    assert package["necessary"]["message_ids"]
    assert package["recoverable"]["count"] >= 3
    assert len(package["dialogue_bundle"]["source_message_ids"]) >= 3
    assert package["dialogue_bundle"]["open_boundary"] is True


def test_model_evidence_aliases_bind_only_to_sent_input_refs():
    item = {
        "status": "complete",
        "model_input_message_ids": ["101", "102"],
        "model_input_aliases": {"m1": "101", "m2": "102"},
        "recoverable_context": {
            "refs": ["sqlite:messages:101", "sqlite:messages:102", "sqlite:messages:999"],
        },
        "topics": [{
            "topic_id": "t1",
            "primary_message_ids": ["m1"],
            "context_message_ids": ["m2"],
        }],
    }
    bound = _map_model_evidence(item)
    assert bound["evidence_status"] == "bound"
    assert bound["evidence_refs"] == ["sqlite:messages:101", "sqlite:messages:102"]
    assert bound["topics"][0]["evidence_status"] == "bound"

    unknown = _map_model_evidence({
        **item,
        "topics": [{"topic_id": "t1", "primary_message_ids": ["m9"]}],
    })
    assert unknown["evidence_status"] == "unknown"
    assert unknown["evidence_refs"] == []
    assert unknown["topics"][0]["evidence_status"] == "unknown"
    assert unknown["topics"][0]["unresolved_aliases"] == ["m9"]


def test_renderer_marks_only_unsent_recoverable_rows_gray():
    item = {
        "status": "complete",
        "provider_call": True,
        "package_id": "101",
        "model_input_message_ids": ["101"],
        "recoverable_context": {
            "refs": ["sqlite:messages:101", "sqlite:messages:102"],
        },
    }
    html = _render_message_window(item, [
        {"id": "101", "content": "sent", "model_seen": False},
        {"id": "102", "content": "recoverable", "model_seen": True},
    ])
    assert html.count("class='message model-seen'") == 1
    assert html.count("class='message context-only'") == 1


def _semantic_fake_result():
    return {
        "topics": [{
            "topic_id": "t1",
            "label": "会议安排",
            "primary_aliases": ["m2"],
            "context_aliases": ["m1", "m3"],
            "uncertainty": "low",
            "evidence_aliases": ["m2"],
        }],
        "people": [{
            "name_or_unknown": "小王",
            "role": "对方",
            "evidence_aliases": ["m1"],
        }],
        "objects": [{
            "name_or_unknown": "会议",
            "role": "事项",
            "evidence_aliases": ["m2"],
        }],
        "states": [{
            "subject": "小王",
            "object": "会议",
            "state": "已确认",
            "modality": "肯定",
            "evidence_aliases": ["m2"],
        }],
        "overall_uncertainties": [],
    }


def test_semantic_fake_model_flows_to_results_and_html(tmp_path: Path):
    package = {
        "id": "p1",
        "account_id": "acct",
        "chat_id": "chat",
        "messages": [
            {"id": "101", "content": "小王问会议时间", "role": "context", "timestamp": "10:00", "sender_name": "小王"},
            {"id": "102", "content": "会议定在三点", "role": "primary", "timestamp": "10:01", "sender_name": "我", "is_self": 1},
            {"id": "103", "content": "好的", "role": "context", "timestamp": "10:02", "sender_name": "小王"},
        ],
    }

    def fake_provider(package, date):
        return _semantic_fake_result()

    config = ExperimentConfig(
        dates=("2026-08-20",), packages_per_date=1, persistent_cap=1,
        source="cross_date_semantic_fake", authorization_id="fake-semantic-v1",
    )
    payload = run_experiment(config, {"2026-08-20": [package]}, provider=fake_provider, authority_root=tmp_path / "ledger")
    assert payload["results"][0]["status"] == "complete"
    row = payload["results"][0]
    assert row["topics"][0]["label"] == "会议安排"
    assert row["people"][0]["name_or_unknown"] == "小王"
    assert row["states"][0]["state"] == "已确认"
    assert row["evidence_status"] == "bound"
    _, html_path = write_outputs(
        payload,
        tmp_path / "out",
        context_windows={("2026-08-20", "p1"): package["messages"]},
    )
    page = html_path.read_text(encoding="utf-8")
    assert "会议安排" in page
    assert "小王" in page
    assert "已确认" in page
    assert "会议定在三点" in page
    assert page.count("message model-seen evidence-hit") > 0
    assert page.count("class='message-alias'") == 3
    assert "结论引用" in page
    assert "人物：小王（对方；证据别名：m1）" in page
    assert "对象：会议（事项；证据别名：m2）" in page


def test_semantic_without_topics_is_not_complete():
    model_input = [
        {"alias": "m1", "speaker": "我", "time": "10:00", "text": "hello", "role": "primary"},
        {"alias": "m2", "speaker": "对方", "time": "10:01", "text": "ok", "role": "context"},
    ]
    try:
        _normalize_semantic_output({
            "topics": [], "people": [], "objects": [], "states": [], "overall_uncertainties": [],
        }, model_input)
    except ValueError as exc:
        assert str(exc) == "semantic_topics_empty"
    else:
        raise AssertionError("empty topics must fail closed")


def test_semantic_complete_provider_path_sends_bundle_and_keeps_structured_fields(tmp_path: Path):
    seen = {}

    class FakeModel:
        model_id = "fake-semantic-model"
        source = "fake-provider"

        def complete(self, system_prompt, request, *, max_output_tokens):
            seen["prompt"] = system_prompt
            seen["request"] = request
            return json.dumps(_semantic_fake_result(), ensure_ascii=False)

    package = {
        "id": "p2",
        "account_id": "acct",
        "chat_id": "chat",
        "messages": [
            {"id": "201", "content": "先问时间", "role": "context"},
            {"id": "202", "content": "明天下午", "role": "primary"},
            {"id": "203", "content": "收到", "role": "context"},
        ],
    }
    config = ExperimentConfig(
        dates=("2026-08-20",), packages_per_date=1, persistent_cap=1,
        source="cross_date_semantic_fake_complete", authorization_id="fake-semantic-complete-v1",
    )
    payload = run_experiment(config, {"2026-08-20": [package]}, provider=FakeModel(), authority_root=tmp_path / "ledger")
    assert len(seen["request"]["messages"]) == 3
    assert {row["role"] for row in seen["request"]["messages"]} == {"primary", "context"}
    assert payload["results"][0]["status"] == "complete"
    assert payload["results"][0]["topics"][0]["label"] == "会议安排"
