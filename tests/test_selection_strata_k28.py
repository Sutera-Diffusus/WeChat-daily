"""Synthetic K28 canonical selection-strata contract tests.

Only invented metadata is used.  The tests do not open the checked-in K10
private store, frozen data, or a provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from wechat_bridge.selection_strata import (
    CANONICAL_STRATA,
    STRATA_OUTPUT_FILENAMES,
    build_canonical_strata_metadata,
    diagnose_k10_v2_artifact,
    materialize_linear_stage_packet_strata,
    select_pages_by_strata,
    verify_strata_replay,
    write_canonical_strata_sidecar,
)
from wechat_bridge.linear_stage_packets import build_linear_stage_packets


def _base(page_id: str, *, account: str = "k28-account", chat: str = "k28-chat") -> dict[str, Any]:
    return {
        "page_id": page_id,
        "root_id": "root-" + page_id,
        "source_packet_id": "packet-" + page_id,
        "scope": {"account_id": account, "chat_id": chat},
        "message_handles": ["message-" + page_id, "message-" + page_id + "-b"],
        "primary_message_handles": ["message-" + page_id],
        "candidate_handles": ["candidate-" + page_id + "-a", "candidate-" + page_id + "-b"],
        "evidence_handles": ["evidence-" + page_id],
        "status": "complete",
    }


def _history_packet(page_id: str) -> dict[str, Any]:
    return {
        "packet_id": "packet-" + page_id,
        "account_id": "k28-account",
        "chat_id": "k28-chat",
        "primary_fragments": [{"message_id": "m-" + page_id}, {"message_id": "m-" + page_id + "-b"}],
        "candidate_person_history": [{
            "candidate_id": "person-" + page_id,
            "left_message_id": "m-" + page_id,
            "right_message_id": "m-" + page_id + "-b",
            "evidence_refs": [{"evidence_id": "ep-" + page_id}],
        }],
        "candidate_object_history": [{
            "candidate_id": "object-" + page_id,
            "left_message_id": "m-" + page_id,
            "right_message_id": "m-" + page_id + "-b",
            "evidence_refs": [{"evidence_id": "eo-" + page_id}],
        }],
        "candidate_state_history": [{
            "candidate_id": "state-" + page_id,
            "left_message_id": "m-" + page_id,
            "right_message_id": "m-" + page_id + "-b",
            "evidence_refs": [{"evidence_id": "es-" + page_id}],
        }],
    }


def _body_free(value: Any) -> None:
    forbidden = {
        "analysis", "body", "content", "evidence_text", "message", "message_text", "prompt",
        "quote", "raw", "raw_text", "response", "response_text", "summary", "text",
        "text_redacted", "user_packet", "user_canonical_json",
    }

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in forbidden and child not in (None, "", [], {}, ()):
                    raise AssertionError("body-bearing key escaped: %s" % key)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)


def test_k28_page_can_record_multiple_strata_with_strong_evidence() -> None:
    page = _base("multi")
    page["categories"] = ["greeting_new_topic", "topic_shift"]
    page["messages"] = [
        {"message_id": "m-multi", "fragment_type": "conversation_opener", "is_opener": True},
        {"message_id": "m-multi-b", "topic_shift": True},
    ]
    page["reply_status"] = "awaiting_reply"
    report = build_canonical_strata_metadata([page])
    row = report["pages"][0]
    assert set(row["observed_strata"]) >= {"greeting_new_topic", "topic_shift", "no_reply"}
    assert row["strata"]["greeting_new_topic"]["status"] == "observed"
    assert row["strata"]["topic_shift"]["status"] == "observed"
    assert row["strata"]["no_reply"]["status"] == "observed"
    assert row["strata"]["no_reply"]["evidence_types"] == ["authoritative_reply_status"]
    assert all(item["counts"]["messages"] >= 1 for item in row["strata"].values() if item["status"] == "observed")
    _body_free(report)


def test_k28_rejects_context_ack_as_opener_and_adversative_as_topic_shift() -> None:
    page = _base("audit-negative")
    page["messages"] = [
        {"message_id": "ack-audit", "dialogue_role": "context_only", "is_opener": True},
        {"message_id": "topic-audit", "message_type": "text"},
    ]
    page["topic_transitions"] = [{
        "message_refs": ["ack-audit", "topic-audit"],
        "evidence_refs": ["evidence-audit"],
        "topic_shift": True,
        "transition_reason": "但是",
    }]
    report = build_canonical_strata_metadata([page])
    row = report["pages"][0]
    assert "greeting_new_topic" not in row["observed_strata"]
    assert "topic_shift" not in row["observed_strata"]
    _body_free(report)


def test_k28_history_triad_requires_evidence_and_weak_signals_do_not_classify() -> None:
    strong = _history_packet("history")
    weak = {
        **_base("weak"),
        "candidate_reasons": ["time_proximity_weak", "same_segment_weak"],
        "candidate_views": ["candidate_person_history", "candidate_object_history", "candidate_state_history"],
        "reply_count": 0,
    }
    report = build_canonical_strata_metadata([strong, weak])
    strong_row = next(row for row in report["pages"] if row["source_handle"])
    assert "pronoun_person_object_state" in strong_row["observed_strata"]
    weak_row = next(row for row in report["pages"] if row["status"] == "metadata_missing")
    assert weak_row["observed_strata"] == []
    assert set(weak_row["metadata_missing_strata"]) == set(CANONICAL_STRATA)
    _body_free(report)


def test_k28_explicit_competition_requires_two_candidates_and_evidence() -> None:
    good = _base("competition")
    good["candidate_competition"] = True
    good["candidate_handles"] = ["candidate-a", "candidate-b"]
    good["evidence_handles"] = ["evidence-competition"]
    bad = _base("competition-bad")
    bad["candidate_competition"] = True
    bad["candidate_handles"] = ["only-one"]
    bad["evidence_handles"] = []
    report = build_canonical_strata_metadata([good, bad])
    assert report["pages"][0]["strata"]["candidate_competition"]["status"] == "observed"
    assert report["pages"][1]["strata"]["candidate_competition"]["status"] == "metadata_missing"


def test_k28_scope_constraint_blocks_provider_when_global_coverage_spans_chats() -> None:
    pages = []
    for index, name in enumerate(CANONICAL_STRATA[:3]):
        page = _base("scope-a-%d" % index, chat="chat-a")
        page["categories"] = [name]
        pages.append(page)
    for index, name in enumerate(CANONICAL_STRATA[3:]):
        page = _base("scope-b-%d" % index, chat="chat-b")
        page["categories"] = [name]
        pages.append(page)
    metadata = build_canonical_strata_metadata(pages)
    selection = select_pages_by_strata(metadata, max_pages=5)
    assert selection["global_coverage_plan_available"] is True
    assert selection["global_coverage_plan_scope_count"] == 2
    assert selection["single_scope_coverage_plan_available"] is False
    assert selection["scope_authorization_required"] is True
    assert selection["provider_allowed"] is False
    authorized = select_pages_by_strata(metadata, max_pages=5, allow_multi_scope=True)
    assert authorized["provider_allowed"] is True
    assert len(authorized["selected"]) == 5
    _body_free(selection)


def test_k28_linear_store_projection_uses_opaque_handles_and_is_replay_stable() -> None:
    packet = _history_packet("linear")
    store = build_linear_stage_packets([packet])
    first = materialize_linear_stage_packet_strata(store)
    second = materialize_linear_stage_packet_strata(store)
    assert verify_strata_replay(first, second)
    assert "pronoun_person_object_state" in first["available_strata"]
    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "packet-linear" not in encoded
    assert "m-linear" not in encoded
    _body_free(first)


def test_k28_sidecar_is_body_free_and_immutable(tmp_path: Path) -> None:
    page = _base("sidecar")
    page["categories"] = ["greeting_new_topic"]
    report = build_canonical_strata_metadata([page])
    selection = select_pages_by_strata(report)
    paths = write_canonical_strata_sidecar(report, tmp_path / "k28-sidecar", selection=selection)
    assert set(paths) == set(STRATA_OUTPUT_FILENAMES)
    for path in paths.values():
        assert Path(path).is_file()
        _body_free(json.loads(Path(path).read_text(encoding="utf-8")) if not path.endswith(".jsonl") else [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()])


def test_k28_k10_diagnosis_reads_metadata_only_and_requires_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "linear_stage_packet_development_v2"
    root.mkdir()
    (root / "manifest.private.json").write_text(json.dumps({
        "artifact_version": "linear_stage_packet_development_v2",
        "split": "development",
        "local_day": "2026-08-25",
        "frozen_read": False,
        "provider_called": False,
        "provider_calls": 0,
    }), encoding="utf-8")
    page = _base("k10")
    page.pop("categories", None)
    (root / "pages.private.jsonl").write_text(json.dumps(page) + "\n", encoding="utf-8")
    (root / "materialized_map.private.jsonl").write_text(json.dumps({"page_id": "k10", "status": "complete"}) + "\n", encoding="utf-8")
    (root / "selection_map.private.jsonl").write_text(json.dumps({"page_id": "k10", "selected": True}) + "\n", encoding="utf-8")
    # A bodyful store is intentionally absent.  The diagnostic must not need it.
    diagnosis = diagnose_k10_v2_artifact(root)
    assert diagnosis["store_read"] is False
    assert diagnosis["private_body_read"] is False
    assert diagnosis["available_strata"] == []
    assert diagnosis["can_offline_reconstruct"] is False
    assert diagnosis["rebuild_development_artifact_required"] is True
    assert diagnosis["corpus_absence_vs_upstream_nonmaterialization_distinguishable"] is False
    _body_free(diagnosis)
