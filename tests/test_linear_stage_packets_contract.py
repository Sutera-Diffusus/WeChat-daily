"""Independent synthetic K8 contract for linear staged packet storage.

This file deliberately exercises only the public K8 module.  It does not read
private/frozen/provider fixtures and never starts a model/provider call.  The
rows below are opaque synthetic records so that the contract can be run while
the implementation is still under development.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from collections.abc import Mapping
from typing import Any, Iterable, Optional, Sequence

import pytest

from wechat_bridge.linear_stage_packets import (
    LinearStagePacketStore,
    build_linear_stage_packets,
    materialize_stage_a,
    materialize_stage_b,
    materialize_stage_c,
    recover_linear_packet,
)


ACCOUNT = "account-k8-synthetic"
CHAT = "chat-k8-synthetic"
OTHER_CHAT = "chat-k8-other-synthetic"

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "evidence_text",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_redacted",
    }
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=str)
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        if isinstance(result, Mapping):
            return dict(result)
    raise AssertionError(f"expected a mapping-like K8 value, got {type(value)!r}")


def _table(store: Any, name: str) -> list[dict[str, Any]]:
    value = getattr(store, name, None)
    if isinstance(value, Mapping):
        return [dict(item) for item in value.values() if isinstance(item, Mapping)]
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    raise AssertionError(f"K8 store has no readable {name} table")


def _store_dict(store: Any) -> dict[str, Any]:
    method = getattr(store, "to_dict", None)
    assert callable(method), "K8 store must expose body-free to_dict serialization"
    result = method()
    assert isinstance(result, Mapping)
    return dict(result)


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _walk(child)


def _assert_body_free(value: Any) -> None:
    forbidden: list[str] = []

    def visit(item: Any, *, parent_key: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key)
                # A content-handle/ref field is an index, not a body field.
                reference_parent = parent_key.casefold() in {"content_handles", "content_ref_fields"}
                if key_text.casefold() in BODY_KEYS and not reference_parent and child not in (None, "", (), [], {}):
                    forbidden.append(key_text)
                visit(child, parent_key=key_text)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent_key=parent_key)

    visit(value)
    assert not forbidden, f"K8 default serialization/materialization contains body fields: {forbidden[:8]}"


def _first(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _ids(rows: Iterable[Mapping[str, Any]], names: Sequence[str]) -> list[str]:
    result: list[str] = []
    for row in rows:
        value = _first(row, names)
        if value is not None:
            result.append(str(value))
    return result


def _fragment(
    message_id: str,
    body: str,
    *,
    account_id: str = ACCOUNT,
    chat_id: str = CHAT,
    sequence: int = 1,
    role: str = "substantive",
    fragment_type: str = "statement",
    segment_id: str = "segment-main",
) -> dict[str, Any]:
    return {
        "fragment_id": f"fragment-{message_id}",
        "message_id": message_id,
        "account_id": account_id,
        "chat_id": chat_id,
        "speaker_id": "speaker-self" if sequence % 2 else "speaker-peer",
        "direction": "outgoing" if sequence % 2 else "incoming",
        "message_type": "text",
        "sequence_in_chat": sequence,
        "event_time": f"synthetic-{sequence:04d}",
        "time_offset_seconds": sequence * 7,
        "dialogue_segment_id": segment_id,
        "segment_id": segment_id,
        "role": role,
        "fragment_type": fragment_type,
        "span": {"start": 0, "end": len(body)},
        "text_redacted": body,
        "content": body,
        "candidate_only": True,
    }


def _evidence(
    evidence_id: str,
    message_id: str,
    body: str,
    *,
    account_id: str = ACCOUNT,
    chat_id: str = CHAT,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "type": "span",
        "message_id": message_id,
        "account_id": account_id,
        "chat_id": chat_id,
        "span": {"start": 0, "end": len(body)},
        "evidence_text": body,
    }


def _candidate(
    candidate_id: str,
    left_message_id: str,
    right_message_id: str,
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    topic_id: str = "topic-main",
    account_id: str = ACCOUNT,
    chat_id: str = CHAT,
    reasons: Sequence[str] = ("explicit_reply", "shared_object"),
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "topic_id": topic_id,
        "left_message_id": left_message_id,
        "right_message_id": right_message_id,
        "account_id": account_id,
        "chat_id": chat_id,
        "relation_label": "possibly_related",
        "relation_subtype": "continuity_candidate",
        "candidate_reason": list(reasons),
        "supporting_slot_codes": list(reasons),
        "confidence": "medium" if "explicit_reply" in reasons else "low",
        "candidate_only": True,
        "evidence_refs": [deepcopy(dict(row)) for row in evidence_rows],
    }


def _packet(
    packet_id: str,
    *,
    fixed_marker: str = "fixed-a",
    dynamic_marker: str = "dynamic-a",
    body_suffix: str = "",
    chat_id: str = CHAT,
    source_refs: Optional[Sequence[Mapping[str, Any]]] = None,
    candidate_reasons: Sequence[str] = ("explicit_reply", "shared_object"),
) -> dict[str, Any]:
    question = _fragment("m-question", "synthetic question" + body_suffix, chat_id=chat_id, sequence=1, fragment_type="question")
    answer = _fragment("m-answer", "synthetic answer" + body_suffix, chat_id=chat_id, sequence=2, segment_id="segment-main")
    greeting = _fragment(
        "m-greeting",
        "synthetic greeting" + body_suffix,
        chat_id=chat_id,
        sequence=3,
        role="conversation_opener",
        fragment_type="conversation_opener",
        segment_id="segment-social",
    )
    acknowledgement = _fragment(
        "m-ack",
        "synthetic acknowledgement" + body_suffix,
        chat_id=chat_id,
        sequence=4,
        role="context_only",
        fragment_type="acknowledgement",
        segment_id="segment-social",
    )
    evidence_rows = [
        _evidence("e-question", "m-question", "evidence for question" + body_suffix, chat_id=chat_id),
        _evidence("e-answer", "m-answer", "evidence for answer" + body_suffix, chat_id=chat_id),
        _evidence("e-greeting", "m-greeting", "evidence for greeting" + body_suffix, chat_id=chat_id),
        _evidence("e-ack", "m-ack", "evidence for acknowledgement" + body_suffix, chat_id=chat_id),
    ]
    if source_refs is None:
        source_refs = (
            {"source_ref_id": "source-question", "message_id": "source-message-question", "kind": "message", "account_id": ACCOUNT, "chat_id": chat_id},
            {"source_ref_id": "source-answer", "message_id": "source-message-answer", "kind": "message", "account_id": ACCOUNT, "chat_id": chat_id},
        )
    return {
        "packet_id": packet_id,
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "anchor_message_id": "m-question",
        "primary_fragments": [question, answer],
        "adjacent_context": [greeting, acknowledgement],
        "evidence_refs": evidence_rows,
        "source_refs": [deepcopy(dict(row)) for row in source_refs],
        "candidate_qa_links": [
            _candidate("candidate-question-answer", "m-question", "m-answer", evidence_rows[:2], chat_id=chat_id, reasons=candidate_reasons)
        ],
        "fixed_part": {
            "fixed_part_version": "k8-fixed-v1",
            "fixed_marker": fixed_marker,
            "authority_scope": {"account_id": ACCOUNT, "chat_id": chat_id},
        },
        "dynamic_part": {
            "dynamic_part_version": "k8-dynamic-v1",
            "dynamic_marker": dynamic_marker,
            "open_status": "open",
        },
        "topics": [
            {
                "topic_id": "topic-main",
                "message_ids": ["m-question", "m-answer"],
                "evidence_ids": ["e-question", "e-answer"],
            },
            {
                "topic_id": "topic-social",
                "message_ids": ["m-greeting", "m-ack"],
                "evidence_ids": ["e-greeting", "e-ack"],
            },
        ],
        "packet_version": "k8-synthetic-v1",
    }


def _sized_packet(packet_id: str, *, messages: int = 25, candidates: int = 65, evidence: int = 65) -> dict[str, Any]:
    primary = [
        _fragment(f"{packet_id}-m-{index:03d}", f"message-{index:03d}", sequence=index + 1)
        for index in range(messages)
    ]
    evidence_rows = [
        _evidence(f"{packet_id}-e-{index:03d}", primary[index % messages]["message_id"], f"evidence-{index:03d}")
        for index in range(evidence)
    ]
    candidate_rows = [
        _candidate(
            f"{packet_id}-c-{index:03d}",
            primary[index % messages]["message_id"],
            primary[(index + 1) % messages]["message_id"],
            [evidence_rows[index]],
        )
        for index in range(candidates)
    ]
    return {
        "packet_id": packet_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "primary_fragments": primary,
        "adjacent_context": [],
        "evidence_refs": evidence_rows,
        "candidate_qa_links": candidate_rows,
        "source_refs": [],
        "topics": [
            {
                "topic_id": "topic-main",
                "message_ids": [row["message_id"] for row in primary],
                "evidence_ids": [row["evidence_id"] for row in evidence_rows],
            }
        ],
        "fixed_part": {"fixed_part_version": "k8-fixed-v1", "fixed_marker": "fixed-sized"},
        "dynamic_part": {"dynamic_part_version": "k8-dynamic-v1", "dynamic_marker": "dynamic-sized", "open_status": "open"},
    }


def _root(store: Any, index: int = 0) -> dict[str, Any]:
    roots = getattr(store, "roots", None)
    assert roots is not None
    rows = list(roots.values()) if isinstance(roots, Mapping) else list(roots)
    assert rows and isinstance(rows[index], Mapping)
    return dict(rows[index])


def _pages(store: Any) -> list[dict[str, Any]]:
    pages = getattr(store, "pages", None)
    assert pages is not None
    rows = list(pages.values()) if isinstance(pages, Mapping) else list(pages)
    return [dict(row) for row in rows]


def _payload(result: Any) -> dict[str, Any]:
    data = _mapping(result)
    for name in ("user_packet", "user", "packet"):
        nested = data.get(name)
        if isinstance(nested, Mapping):
            return dict(nested)
    return {
        key: value
        for key, value in data.items()
        if key
        not in {
            "material_stats",
            "limits",
            "status",
            "system_prompt",
            "system_prompt_sha256",
            "user_packet_sha256",
        }
    }


def _stats(result: Any) -> dict[str, Any]:
    data = _mapping(result)
    stats = data.get("material_stats", data.get("stats"))
    assert isinstance(stats, Mapping), f"K8 materializer must expose material stats: {sorted(data)}"
    return dict(stats)


def _status(result: Any) -> str:
    data = _mapping(result)
    return str(data.get("status", data.get("material_status", data.get("state", ""))))


def _snapshot(result: Any) -> Any:
    data = _mapping(result)
    for name in ("open_snapshot", "open_thread_snapshot", "open_snapshot_refs", "snapshot"):
        if name in data:
            return data[name]
    for name in ("packet", "user_packet", "user"):
        nested = data.get(name)
        if isinstance(nested, Mapping):
            for snapshot_name in ("open_snapshot", "open_thread_snapshot", "open_snapshot_refs", "snapshot"):
                if snapshot_name in nested:
                    return nested[snapshot_name]
    return None


def _assert_material_stats(result: Any, system_prompt: str, *, max_messages: int, max_candidates: int, max_evidence: int) -> None:
    payload = _payload(result)
    stats = _stats(result)
    user_chars = len(_canonical(payload))
    expected_user = (user_chars + 3) // 4
    expected_total = (len(system_prompt) + user_chars + 3) // 4
    assert stats["canonical_chars"] == user_chars
    assert stats["user_token_proxy"] == expected_user
    assert stats["input_token_proxy"] == expected_total
    assert expected_user <= 1600
    assert expected_total <= 2000
    assert stats["message_count"] <= max_messages
    assert stats.get("candidate_count", stats.get("candidate_row_count", 0)) <= max_candidates
    assert stats.get("evidence_count", stats.get("evidence_ref_count", 0)) <= max_evidence


def test_public_api_builds_global_deduplicated_tables_and_ref_only_roots_and_pages() -> None:
    assert LinearStagePacketStore is not None
    assert callable(build_linear_stage_packets)
    assert callable(materialize_stage_a)
    assert callable(materialize_stage_b)
    assert callable(materialize_stage_c)
    assert callable(recover_linear_packet)

    store = build_linear_stage_packets((_packet("root-one"), _packet("root-two")))
    assert isinstance(store, LinearStagePacketStore)

    message_rows = _table(store, "message_table")
    candidate_rows = _table(store, "candidate_table")
    evidence_rows = _table(store, "evidence_table")
    content_rows = _table(store, "content_table")
    assert len(_ids(message_rows, ("message_id",))) == len(set(_ids(message_rows, ("message_id",))))
    assert len(_ids(candidate_rows, ("candidate_id",))) == len(set(_ids(candidate_rows, ("candidate_id",))))
    assert len(_ids(evidence_rows, ("evidence_id",))) == len(set(_ids(evidence_rows, ("evidence_id",))))
    assert len(_ids(content_rows, ("content_handle", "content_id"))) == len(set(_ids(content_rows, ("content_handle", "content_id"))))
    # The same records occur in both roots, but each global table owns one row.
    assert _ids(candidate_rows, ("candidate_id",)).count("candidate-question-answer") == 1
    assert _ids(evidence_rows, ("evidence_id",)).count("e-question") == 1
    assert _ids(evidence_rows, ("evidence_id",)).count("e-answer") == 1

    serialized = _store_dict(store)
    _assert_body_free(serialized)
    roots = serialized.get("roots")
    pages = serialized.get("pages")
    assert isinstance(roots, list) and len(roots) == 2
    assert isinstance(pages, list) and len(pages) == 2
    for root in roots:
        assert isinstance(root, Mapping)
        assert isinstance(root.get("page_refs"), list)
        assert all(not isinstance(ref, Mapping) for ref in root["page_refs"])
        assert not {"primary_fragments", "adjacent_context", "authoritative_facts", "candidates", "evidence"} & set(root)
        for field in ("message_handles", "candidate_handles", "evidence_handles"):
            if field in root:
                assert all(not isinstance(ref, Mapping) for ref in root[field])
    for page in pages:
        assert isinstance(page, Mapping)
        assert not {"messages", "primary_fragments", "adjacent_context", "candidates", "evidence"} & set(page)
        for field in ("message_handles", "candidate_handles", "candidate_link_refs", "evidence_handles"):
            if field in page:
                assert all(not isinstance(ref, Mapping) for ref in page[field])


def test_page_count_is_linear_max_across_24m_64c_64e_limits_not_cartesian() -> None:
    packets = (_sized_packet("large-one"), _sized_packet("large-two"))
    store = build_linear_stage_packets(packets)
    roots = [_root(store, index) for index in range(2)]

    assert all(len(root["message_handles"]) == 25 for root in roots)
    assert all(len(root["candidate_handles"]) == 65 for root in roots)
    assert all(len(root["evidence_handles"]) == 65 for root in roots)
    # Each root needs max(ceil(25/24), ceil(65/64), ceil(65/64)) == 2 pages.
    assert len(_pages(store)) == 4
    cartesian_count = sum(
        math.ceil(len(root["message_handles"]) / 24)
        * math.ceil(len(root["candidate_handles"]) / 64)
        * math.ceil(len(root["evidence_handles"]) / 64)
        for root in roots
    )
    assert len(_pages(store)) < cartesian_count
    for page in _pages(store):
        assert len(page["message_handles"]) <= 24
        assert len(page["candidate_handles"]) <= 64
        assert len(page["evidence_handles"]) <= 64
    assert all(len(root["page_refs"]) == 2 for root in roots)


def test_stages_are_separated_a_is_topic_material_b_is_related_and_c_repeats_no_body() -> None:
    store = build_linear_stage_packets((_packet("stage-root"),))
    root = _root(store)
    page_id = root["page_refs"][0]

    stage_a = materialize_stage_a(store, page_id, system_prompt="synthetic-system-A")
    stage_b = materialize_stage_b(store, page_id, topic_ids=["topic-main"], system_prompt="synthetic-system-B")
    stage_c = materialize_stage_c(store, stage_a, stage_b, system_prompt="synthetic-system-C")

    a_data = _mapping(stage_a)
    b_data = _mapping(stage_b)
    c_data = _mapping(stage_c)
    assert a_data.get("stage") == "A"
    assert a_data.get("candidate_link_refs") is not None
    assert "evidence" not in a_data and "evidence_handles" not in a_data
    assert "authoritative_facts" not in a_data
    assert set(a_data.get("message_handles", ())) <= set(root["message_handles"])
    assert set(a_data.get("candidate_handles", ())) <= set(root["candidate_handles"])
    _assert_body_free(stage_a)

    assert b_data.get("stage") == "B"
    b_message_ids = {str(row.get("message_id")) for row in b_data.get("messages", ()) if isinstance(row, Mapping)}
    b_evidence_ids = {str(row.get("evidence_id")) for row in b_data.get("evidence", ()) if isinstance(row, Mapping)}
    assert b_message_ids == {"m-question", "m-answer"}
    assert b_evidence_ids == {"e-question", "e-answer"}
    assert not {"m-greeting", "m-ack"} & b_message_ids
    assert not {"e-greeting", "e-ack"} & b_evidence_ids
    _assert_body_free(stage_b)

    assert c_data.get("stage") == "C"
    assert "messages" not in c_data and "evidence" not in c_data
    _assert_body_free(stage_c)

    _assert_material_stats(stage_a, "synthetic-system-A", max_messages=24, max_candidates=64, max_evidence=64)
    _assert_material_stats(stage_b, "synthetic-system-B", max_messages=24, max_candidates=64, max_evidence=64)
    _assert_material_stats(stage_c, "synthetic-system-C", max_messages=24, max_candidates=64, max_evidence=64)


def test_recovery_is_lossless_for_source_primary_adjacent_greeting_ack_and_evidence() -> None:
    source = _packet("recover-root")
    store = build_linear_stage_packets((source,))
    recovered = recover_linear_packet(store, "recover-root")
    assert isinstance(recovered, Mapping)

    assert [row["source_ref_id"] for row in recovered["source_refs"]] == ["source-question", "source-answer"]
    assert [row["message_id"] for row in recovered["primary_fragments"]] == ["m-question", "m-answer"]
    assert [row["message_id"] for row in recovered["adjacent_context"]] == ["m-greeting", "m-ack"]
    assert recovered["adjacent_context"][0]["fragment_type"] == "conversation_opener"
    assert recovered["adjacent_context"][1]["fragment_type"] == "acknowledgement"
    assert {row["evidence_id"] for row in recovered["evidence_refs"]} == {
        "e-question",
        "e-answer",
        "e-greeting",
        "e-ack",
    }
    assert recovered["primary_fragments"][0]["content"] == "synthetic question"
    assert recovered["adjacent_context"][0]["content"] == "synthetic greeting"
    assert {row["evidence_text"] for row in recovered["evidence_refs"]} >= {
        "evidence for question",
        "evidence for answer",
    }

    body_free = recover_linear_packet(store, "recover-root", include_body=False)
    _assert_body_free(body_free)
    assert "synthetic question" not in _canonical(body_free)
    assert "evidence for question" not in _canonical(body_free)


def test_over_capacity_returns_pending_open_snapshot_without_truncating_handles() -> None:
    store = build_linear_stage_packets(
        (_packet("pending-root"),),
        capacity={
            "max_input_token_proxy": 32,
            "max_user_token_proxy": 16,
            "max_messages": 24,
            "max_candidate_rows": 64,
            "max_evidence_refs": 64,
        },
    )
    root = _root(store)
    page_id = root["page_refs"][0]
    result = materialize_stage_a(store, page_id, system_prompt="synthetic-system-that-forces-a-pending-open-snapshot")
    assert _status(result) in {"pending", "open"}
    assert _snapshot(result) is not None
    serialized = _canonical(result)
    for handle in root["message_handles"] + root["candidate_handles"] + root["evidence_handles"]:
        assert str(handle) in serialized, f"over-capacity result truncated handle {handle!r}"
    assert _root(store)["status"] == "open"
    assert page_id in _root(store)["page_refs"]


def test_scope_is_hard_isolated_and_time_or_same_segment_only_edges_are_not_strong() -> None:
    foreign = _packet("foreign-root")
    foreign["primary_fragments"][0]["chat_id"] = OTHER_CHAT
    with pytest.raises(ValueError, match="chat|scope"):
        build_linear_stage_packets((foreign,))

    weak = _packet("weak-root", candidate_reasons=("time_proximity", "same_segment"))
    weak_store = build_linear_stage_packets((weak,))
    weak_candidate_ids = _ids(_table(weak_store, "candidate_table"), ("candidate_id",))
    assert "candidate-question-answer" not in weak_candidate_ids
    assert "candidate-question-answer" not in _canonical(_store_dict(weak_store))

    strong = _packet("strong-root", candidate_reasons=("explicit_reply", "shared_object"))
    strong_store = build_linear_stage_packets((strong,))
    candidates = _table(strong_store, "candidate_table")
    assert "candidate-question-answer" in _ids(candidates, ("candidate_id",))
    strong_row = next(row for row in candidates if row.get("candidate_id") == "candidate-question-answer")
    assert strong_row.get("relation_label") not in {"same_event", "resolved"}


def test_fixed_dynamic_content_hashes_and_replay_cache_are_sensitive() -> None:
    base = build_linear_stage_packets((_packet("hash-root", body_suffix="-base"),))
    replay = build_linear_stage_packets((_packet("hash-root", body_suffix="-base"),))
    base_root = _root(base)
    replay_root = _root(replay)
    assert replay_root["root_id"] == base_root["root_id"]
    for name in ("fixed_hash", "dynamic_hash", "content_hash", "packet_hash", "cache_key"):
        assert replay_root[name] == base_root[name]
    for namespace, key in (
        ("fixed", "fixed_hash"),
        ("dynamic", "dynamic_hash"),
        ("content", "content_hash"),
    ):
        assert base_root[key] in base.cache[namespace]
        assert replay_root[key] in replay.cache[namespace]

    # A replay into one process-local store reuses the same content-addressed
    # root/cache entries instead of appending a second copy of the packet.
    replay_store = LinearStagePacketStore()
    build_linear_stage_packets((_packet("replay-root", body_suffix="-base"),), store=replay_store)
    counts_before = {
        name: len(getattr(replay_store, name))
        for name in ("message_table", "content_table", "candidate_table", "evidence_table", "root_table", "page_table")
    }
    build_linear_stage_packets((_packet("replay-root", body_suffix="-base"),), store=replay_store)
    counts_after = {
        name: len(getattr(replay_store, name))
        for name in counts_before
    }
    assert counts_after == counts_before
    assert len(replay_store.cache["fixed"]) == 1
    assert len(replay_store.cache["dynamic"]) == 1
    assert len(replay_store.cache["content"]) == 1

    fixed = build_linear_stage_packets((_packet("hash-root", body_suffix="-base", fixed_marker="fixed-b"),))
    dynamic = build_linear_stage_packets((_packet("hash-root", body_suffix="-base", dynamic_marker="dynamic-b"),))
    content = build_linear_stage_packets((_packet("hash-root", body_suffix="-changed-content"),))
    fixed_root = _root(fixed)
    dynamic_root = _root(dynamic)
    content_root = _root(content)

    assert fixed_root["fixed_hash"] != base_root["fixed_hash"]
    assert fixed_root["dynamic_hash"] == base_root["dynamic_hash"]
    assert fixed_root["content_hash"] == base_root["content_hash"]
    assert dynamic_root["dynamic_hash"] != base_root["dynamic_hash"]
    assert dynamic_root["fixed_hash"] == base_root["fixed_hash"]
    assert dynamic_root["content_hash"] == base_root["content_hash"]
    assert content_root["content_hash"] != base_root["content_hash"]
    assert content_root["fixed_hash"] == base_root["fixed_hash"]
    assert content_root["dynamic_hash"] == base_root["dynamic_hash"]
    assert len({fixed_root["packet_hash"], dynamic_root["packet_hash"], content_root["packet_hash"], base_root["packet_hash"]}) == 4
