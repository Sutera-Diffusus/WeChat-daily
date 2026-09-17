"""Synthetic K30 contracts for the local stratified linear Stage-A artifact."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from wechat_bridge.linear_stage_packet_development_stratified_runner import (
    ARTIFACT_VERSION,
    MAX_SELECTED_PAGES,
    OUTPUT_FILENAMES,
    _candidate_selection_plan,
    run_linear_stage_packet_development_stratified_from_mappings,
)
from wechat_bridge.linear_stage_packet_development_runner import _capacity
from wechat_bridge.linear_stage_packets import build_linear_stage_packets


def _message(message_id: str, body: str, sequence: int, *, fragment_type: str = "statement") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "fragment_id": "f-" + message_id,
        "account_id": "k30-account",
        "chat_id": "k30-chat",
        "sequence_in_chat": sequence,
        "role": "substantive",
        "fragment_type": fragment_type,
        "text_redacted": body,
        "content": body,
    }


def _packet(index: int, *, canonical: str | None = None) -> dict[str, Any]:
    packet_id = "k30-packet-%02d" % index
    first = _message("k30-m-%02d-a" % index, "question body K30_PRIVATE_%02d" % index, 1)
    second = _message("k30-m-%02d-b" % index, "answer body K30_PRIVATE_%02d" % index, 2)
    evidence = [
        {"evidence_id": "k30-e-%02d-a" % index, "message_id": first["message_id"], "evidence_text": "private evidence"},
        {"evidence_id": "k30-e-%02d-b" % index, "message_id": second["message_id"], "evidence_text": "private evidence"},
    ]
    candidates = [
        {
            "candidate_id": "k30-c-%02d-%s" % (index, suffix),
            "left_message_id": first["message_id"],
            "right_message_id": second["message_id"],
            "evidence_refs": evidence,
            "account_id": "k30-account",
            "chat_id": "k30-chat",
            "relation_label": "possibly_related",
        }
        for suffix in ("person", "object", "state")
    ]
    packet: dict[str, Any] = {
        "packet_id": packet_id,
        "account_id": "k30-account",
        "chat_id": "k30-chat",
        "primary_fragments": [first, second],
        "evidence_refs": evidence,
        "candidate_person_history": [dict(candidates[0], view="candidate_person_history")],
        "candidate_object_history": [dict(candidates[1], view="candidate_object_history")],
        "candidate_state_history": [dict(candidates[2], view="candidate_state_history")],
        "source_refs": [{"source_ref_id": "k30-source-%02d" % index, "message_id": first["message_id"]}],
        "fixed_part": {"fixed_part_version": "k30-fixed-v1"},
        "dynamic_part": {"dynamic_part_version": "k30-dynamic-v1"},
    }
    if canonical:
        packet["categories"] = [canonical]
        packet["candidate_handles"] = ["k30-candidate-a", "k30-candidate-b"]
    return packet


def _selection(packets: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "packet_id": packet["packet_id"],
            "selection_rank": index + 1,
            "selected": True,
            "packet_hash": hashlib.sha256(str(packet["packet_id"]).encode()).hexdigest(),
        }
        for index, packet in enumerate(packets)
    ]


def _assert_public_body_free(value: Any) -> None:
    forbidden = {"body", "content", "evidence_text", "message_text", "prompt", "raw_text", "text_redacted", "user_packet", "user_canonical_json"}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in forbidden and child not in (None, "", [], {}, (), False, 0):
                    raise AssertionError("body field escaped: %s" % key)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)


def test_k30_mapping_run_is_local_bounded_and_stratified(tmp_path: Path) -> None:
    packets = [_packet(index) for index in range(20)]
    result = run_linear_stage_packet_development_stratified_from_mappings(
        packets,
        _selection(packets),
        tmp_path / ARTIFACT_VERSION,
        selected_packet_count=20,
    )
    assert result.status == "complete"
    assert result.provider_calls == 0
    assert result.selected_packet_count == result.root_count == 20
    assert result.page_count == 20
    assert 0 <= result.selected_page_count <= MAX_SELECTED_PAGES
    assert result.aggregate["metrics"]["provider_gate"]["calls"] == 0
    assert result.aggregate["metrics"]["replay"]["idempotent"] is True
    assert result.aggregate["metrics"]["page_formula"]["linear"] is True
    assert result.aggregate["metrics"]["recovery_rates"]["primary"]["status"] == "pass"

    for key in ("aggregate", "cost", "manifest"):
        payload = json.loads((tmp_path / ARTIFACT_VERSION / OUTPUT_FILENAMES[key]).read_text(encoding="utf-8"))
        _assert_public_body_free(payload)
    for key in ("pages", "materialized", "recovery", "strata", "selection", "audit"):
        rows = [json.loads(line) for line in (tmp_path / ARTIFACT_VERSION / OUTPUT_FILENAMES[key]).read_text(encoding="utf-8").splitlines() if line.strip()]
        _assert_public_body_free(rows)

    # The private store is the only output allowed to retain the message body.
    store_text = (tmp_path / ARTIFACT_VERSION / OUTPUT_FILENAMES["store"]).read_text(encoding="utf-8")
    assert "K30_PRIVATE_00" in store_text
    aggregate_text = (tmp_path / ARTIFACT_VERSION / OUTPUT_FILENAMES["aggregate"]).read_text(encoding="utf-8")
    assert "K30_PRIVATE_00" not in aggregate_text


def test_k30_canonical_strata_rows_are_opaque_and_hash_bound(tmp_path: Path) -> None:
    packets = [_packet(index, canonical="greeting_new_topic" if index == 0 else None) for index in range(20)]
    result = run_linear_stage_packet_development_stratified_from_mappings(
        packets,
        _selection(packets),
        tmp_path / "k30-hash",
        selected_packet_count=20,
    )
    rows = [json.loads(line) for line in (tmp_path / "k30-hash" / OUTPUT_FILENAMES["strata"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 20
    assert all(row["body_free"] is True for row in rows)
    for row in rows:
        assert row["page_handle"].startswith("k28_page_")
        assert row["root_handle"].startswith("k28_root_")
        assert row["scope_handle"].startswith("k28_scope_")
        assert len(row["metadata_hash"]) == 64
        for stratum in row["strata"].values():
            assert all(handle.startswith("k28_") for key in ("message_handles", "candidate_handles", "evidence_handles") for handle in stratum[key])
    # Missing/observed state remains explicit and never becomes a provider
    # call or a body-bearing selection row.
    assert "greeting_new_topic" in result.strata["available_strata"] or "greeting_new_topic" in result.strata["missing_strata"]
    assert result.selection["selected_page_count"] <= MAX_SELECTED_PAGES


def test_k30_artifact_hashes_and_immutability_contract(tmp_path: Path) -> None:
    packets = [_packet(index) for index in range(2)]
    refs = _selection(packets)
    result = run_linear_stage_packet_development_stratified_from_mappings(packets, refs, tmp_path / "first", selected_packet_count=2)
    manifest = json.loads((tmp_path / "first" / OUTPUT_FILENAMES["manifest"]).read_text(encoding="utf-8"))
    for filename, digest in manifest["artifact_hashes"].items():
        assert hashlib.sha256((tmp_path / "first" / filename).read_bytes()).hexdigest() == digest
    # A second invocation must choose a new immutable directory.
    try:
        run_linear_stage_packet_development_stratified_from_mappings(packets, refs, tmp_path / "first", selected_packet_count=2)
    except FileExistsError:
        pass
    else:
        raise AssertionError("K30 artifact directory must be immutable")


def test_k30_candidate_plan_keeps_distinct_primary_containment_and_cue_combo() -> None:
    """Evidence reuse only collapses an identical root/page/cue combination."""

    def candidate_packet(packet_id: str, primary_id: str, cue_kind: str) -> dict[str, Any]:
        packet = deepcopy(_packet(0))
        packet["packet_id"] = packet_id
        packet["scope"] = {"account_id": "k30-account", "chat_id": "k30-chat"}
        packet["account_id"] = "k30-account"
        packet["chat_id"] = "k30-chat"
        primary = _message(primary_id, "private primary", 1)
        context = _message("k30-shared-context", "private context", 2)
        packet["primary_fragments"] = [primary]
        packet["adjacent_context"] = [context]
        packet["source_message_ids"] = [primary_id, context["message_id"]]
        packet["window"] = {"message_ids": [primary_id, context["message_id"]], "scale": "W1"}
        packet["source_refs"] = [{"source_ref_id": "source-" + packet_id, "message_id": primary_id}]
        packet["evidence_refs"] = [{"evidence_id": "shared-claim", "message_id": context["message_id"]}]
        row = {
            "selection_cue": True,
            "candidate_only": True,
            "semantic_decision_pending": True,
            "strong_relation": False,
            "cue_kind": cue_kind,
            "message_refs": [context["message_id"]],
            "source_message_ids": [context["message_id"]],
            "scoped_evidence_refs": [
                {
                    "source_message_id": context["message_id"],
                    "span": {"start": 0, "end": 2},
                    # The source claim is shared across roots; containment,
                    # not this handle, distinguishes different primaries.
                    "evidence_kind": "shared_source_claim",
                }
            ],
        }
        packet["reply_status" if cue_kind == "no_reply" else cue_kind] = [row]
        return packet

    packets = [
        candidate_packet("candidate-a-1", "primary-a", "no_reply"),
        candidate_packet("candidate-a-duplicate", "primary-a", "no_reply"),
        candidate_packet("candidate-b-1", "primary-b", "no_reply"),
        candidate_packet("candidate-b-duplicate", "primary-b", "no_reply"),
        candidate_packet("candidate-a-pronoun", "primary-a", "pronoun_person_object_state"),
    ]
    store = build_linear_stage_packets(packets, capacity=_capacity(None))
    plan = _candidate_selection_plan(packets, store, max_pages=MAX_SELECTED_PAGES)

    assert plan["candidate_page_count"] == 5
    assert plan["candidate_row_count"] == 5
    assert plan["selected_page_count"] == 3
    assert all(row["candidate_only"] and row["semantic_decision_pending"] for row in plan["selected"])
    assert all(row["body_free"] for row in plan["selected"])
    # One page is retained for each distinct primary containment, while the
    # pronoun cue combination remains selectable on the first containment.
    assert len({row["containment_handle"] for row in plan["selected"]}) == 2
    assert len({row["candidate_group_handle"] for row in plan["selected"]}) == 3


def test_k30_candidate_first_plan_scans_all_packets_and_partitions_scopes() -> None:
    families = (
        ("message_metadata", "greeting_boundary"),
        ("topic_transitions", "topic_shift"),
        ("candidate_competition", "candidate_competition"),
        ("reply_status", "no_reply"),
        ("pronoun_person_object_state", "pronoun_person_object_state"),
    )
    packets: list[dict[str, Any]] = []
    for index, (container, family) in enumerate(families):
        packet = _packet(index)
        message_id = packet["primary_fragments"][0]["message_id"]
        packet[container] = [
            {
                "selection_cue": True,
                "candidate_only": True,
                "semantic_decision_pending": True,
                "cue_kind": family,
                "message_refs": [message_id],
                "scoped_evidence_refs": [
                    {
                        "source_message_id": message_id,
                        "span": {"start": 0, "end": 1},
                        "evidence_kind": family,
                    }
                ],
            }
        ]
        packets.append(packet)

    plan = _candidate_selection_plan(packets, None, max_pages=MAX_SELECTED_PAGES)
    assert set(plan["available_cue_families"]) == {family for _, family in families}
    assert plan["selected_page_count"] <= MAX_SELECTED_PAGES
    assert plan["global_candidate_plan"]["candidate_page_count"] == len(packets)
    assert plan["per_scope_plans"]
    assert plan["scope_authorization"]["status"] == "not_required"
    _assert_public_body_free(plan)
