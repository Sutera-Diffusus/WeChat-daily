"""Synthetic K6 compact-context-packet contract.

The K2 packet is intentionally a rich, overlapping projection: the same
message, fragment, candidate and evidence can occur in more than one packet.
K6 must centralise those records and leave packet rows as references.  These
tests never read a private/frozen split and never construct or call a model
provider; ``materialize_stage_packet`` is exercised only as a local shaping
operation for the existing K3 Stage A/B/C envelope.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Iterable, Mapping, Sequence

import pytest

from wechat_bridge.context_packets import ContextPacket as K2ContextPacket
from wechat_bridge.compact_context_packets import (
    CompactContextPacketStore,
    compact_context_packets,
    materialize_stage_packet,
)


ACCOUNT = "account-k6-synthetic"
CHAT = "chat-k6-synthetic"
OTHER_CHAT = "chat-k6-other"

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
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return value


def _mapping(value: Any, *, include_body: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if not callable(method):
        raise AssertionError(f"K6 value is not serializable: {type(value)!r}")
    if include_body:
        # K6 implementations may call the explicit opt-in either
        # include_body or include_content.  The compact contract itself is
        # independent of that spelling; materialized provider packets below
        # are the canonical body-bearing view.
        for kwargs in (
            {"include_body": True},
            {"include_bodies": True},
            {"include_content": True},
            {"include_model_packet": True},
        ):
            try:
                result = method(**kwargs)
            except TypeError:
                continue
            if isinstance(result, Mapping):
                return dict(result)
    result = method()
    if not isinstance(result, Mapping):
        raise AssertionError("K6 to_dict() must return a mapping")
    return dict(result)


def _table(value: Any, names: Sequence[str]) -> list[Mapping[str, Any]]:
    """Read one canonical table while tolerating the one-time API naming choice."""

    data = _mapping(value)
    sources = [value]
    nested = data.get("store")
    if nested is not None:
        sources.append(nested)
    for source in sources:
        source_data = _mapping(source)
        for name in names:
            table = source_data.get(name)
            if isinstance(table, Mapping):
                table = list(table.values())
            if isinstance(table, (list, tuple)):
                return [dict(item) for item in table if isinstance(item, Mapping)]
        for name in names:
            table = getattr(source, name, None)
            if isinstance(table, Mapping):
                table = list(table.values())
            if isinstance(table, (list, tuple)):
                return [dict(item) for item in table if isinstance(item, Mapping)]
    raise AssertionError(f"K6 serialization has none of tables {tuple(names)!r}: {sorted(data)}")


def _first(value: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return default


def _row_id(value: Mapping[str, Any], names: Sequence[str]) -> str:
    item = _first(value, names)
    if item is None:
        raise AssertionError(f"row has no id from {tuple(names)!r}: {value}")
    return str(item)


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
                # A content_ref_fields/content_handles entry is an index
                # reference, not a body-bearing field, even when it preserves
                # the source field name (e.g. ``content`` or ``text``).
                is_reference_field = parent_key.casefold() in {"content_ref_fields", "content_handles"}
                if key_text.casefold() in BODY_KEYS and not is_reference_field and child not in (None, "", (), [], {}):
                    forbidden.append(key_text)
                visit(child, parent_key=key_text)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent_key=parent_key)

    visit(value)
    assert not forbidden, f"default K6 serialization contains body fields: {forbidden[:8]}"


def _message(mid: str, body: str, *, chat_id: str = CHAT, sequence: int = 1, role: str = "substantive") -> dict[str, Any]:
    return {
        "message_id": mid,
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "speaker_id": "speaker-self" if sequence % 2 else "speaker-peer",
        "direction": "outgoing" if sequence % 2 else "incoming",
        "message_type": "text",
        "sequence_in_chat": sequence,
        "event_time": f"synthetic-{sequence:03d}",
        "time_offset_seconds": sequence * 7,
        "dialogue_segment_id": "segment-shared" if role == "substantive" else "segment-social",
        "reply_to_message_id": None,
        "metadata_authoritative": True,
        # K6 must extract this once into its central content table.  It is
        # deliberately also present in the K2 fragment below to model the
        # overlap that motivated the content store.
        "content": body,
    }


def _fragment(
    fid: str,
    mid: str,
    body: str,
    *,
    chat_id: str = CHAT,
    role: str = "substantive",
    fragment_type: str = "statement",
    sequence: int = 1,
    object_id: str = "object-k6",
    state: str = "unknown",
) -> dict[str, Any]:
    return {
        "fragment_id": fid,
        "message_id": mid,
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "segment_id": "segment-shared" if role == "substantive" else "segment-social",
        "text_redacted": body,
        "content": body,
        "span": {"start": 0, "end": len(body)},
        "role": role,
        "fragment_type": fragment_type,
        "speaker_id": "speaker-self" if sequence % 2 else "speaker-peer",
        "mentioned_person_ids": ["person-k6"],
        "subject_id": "person-k6",
        "object_id": object_id,
        "object_resolution": "explicit",
        "state_candidate": state,
        "state_evidence": "explicit" if state != "unknown" else "unknown",
        "intent_candidate": "question" if fragment_type == "question" else "statement",
        "actions_candidate": ["inspect"],
        "is_opener": role == "conversation_opener",
        "is_silent": role == "context_only",
        "candidate_only": True,
        "evidence_refs": [
            {
                "evidence_id": f"e-{mid}",
                "type": "span",
                "message_id": mid,
                "fragment_id": fid,
                "account_id": ACCOUNT,
                "chat_id": chat_id,
                "span": {"start": 0, "end": min(8, len(body))},
            }
        ],
    }


def _fact(mid: str, *, chat_id: str = CHAT, sequence: int = 1) -> dict[str, Any]:
    return {
        "message_id": mid,
        "registry_key": f"registry:{ACCOUNT}:{chat_id}:{mid}",
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "speaker_id": "speaker-self" if sequence % 2 else "speaker-peer",
        "direction": "outgoing" if sequence % 2 else "incoming",
        "message_type": "text",
        "sequence_in_chat": sequence,
        "event_time": f"synthetic-{sequence:03d}",
        "reply_to_message_id": "m-question" if mid == "m-answer" else None,
        "dialogue_segment_id": "segment-shared",
        "metadata_authoritative": True,
        "record_hash": f"record-{mid}",
    }


def _candidate(
    candidate_id: str,
    left: str,
    right: str,
    *,
    chat_id: str = CHAT,
    weak_only: bool = False,
) -> dict[str, Any]:
    reasons = ["time_proximity_weak", "same_segment_weak"] if weak_only else ["explicit_reply", "shared_object"]
    return {
        "candidate_id": candidate_id,
        "left_message_id": left,
        "right_message_id": right,
        "left_fragment_id": f"f-{left}",
        "right_fragment_id": f"f-{right}",
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "relation_label": "candidate_only",
        "relation_subtype": "continuity_candidate",
        "supporting_slot_codes": reasons,
        "candidate_reason": reasons,
        "confidence": "low" if weak_only else "medium",
        "strong_relation": False,
        "time_is_weak_only": True,
        "same_segment_is_weak_only": True,
        "evidence_refs": [
            {
                "evidence_id": "e-m-question",
                "type": "span",
                "message_id": left,
                "account_id": ACCOUNT,
                "chat_id": chat_id,
                "span": {"start": 0, "end": 8},
            },
            {
                "evidence_id": "e-m-answer",
                "type": "span",
                "message_id": right,
                "account_id": ACCOUNT,
                "chat_id": chat_id,
                "span": {"start": 0, "end": 8},
            },
        ],
        "source_refs": [
            {"type": "message", "id": left, "account_id": ACCOUNT, "chat_id": chat_id},
            {"type": "message", "id": right, "account_id": ACCOUNT, "chat_id": chat_id},
        ],
        "candidate_only": True,
    }


def _packet(
    packet_id: str,
    *,
    chat_id: str = CHAT,
    body_suffix: str = "",
    fixed_extra: str = "fixed-a",
    dynamic_extra: str = "dynamic-a",
    include_social: bool = True,
    weak_only: bool = False,
    large: bool = False,
) -> K2ContextPacket:
    # The shared rows deliberately occur in both packet windows.  The long
    # variant adds enough unique rows to exercise K6's hard materialization
    # boundary without constructing a provider request.
    count = 30 if large else 3
    primary_ids = ["m-question", "m-answer"] + ([f"m-{packet_id}-{i:02d}" for i in range(count)] if large else [])
    primary: list[dict[str, Any]] = [
        _fragment("f-m-question", "m-question", "请问对象状态？" + body_suffix, chat_id=chat_id, fragment_type="question", sequence=1),
        _fragment("f-m-answer", "m-answer", "对象当前状态已更新。" + body_suffix, chat_id=chat_id, sequence=2, state="updated"),
    ]
    if large:
        primary.extend(
            _fragment(
                f"f-{packet_id}-{i:02d}",
                mid,
                ("较长但仍为 synthetic 的正文 %s %03d " % (body_suffix or "window", i)) * 14,
                chat_id=chat_id,
                sequence=i + 3,
                object_id=f"object-{i:02d}",
                state="unknown" if i % 2 else "pending",
            )
            for i, mid in enumerate(primary_ids[2:])
        )
    adjacent: list[dict[str, Any]] = []
    adjacent_ids: list[str] = []
    if include_social:
        adjacent = [
            _fragment("f-greeting", "m-greeting", "你好", chat_id=chat_id, role="conversation_opener", fragment_type="conversation_opener", sequence=10),
            _fragment("f-ack", "m-ack", "收到", chat_id=chat_id, role="context_only", fragment_type="acknowledgement", sequence=11),
        ]
        adjacent_ids = ["m-greeting", "m-ack"]
    messages = [
        _message(mid, fragment_body, chat_id=chat_id, sequence=index + 1, role="substantive")
        for index, (mid, fragment_body) in enumerate(
            [(item["message_id"], item["content"]) for item in primary + adjacent]
        )
    ]
    facts = [_fact(mid, chat_id=chat_id, sequence=index + 1) for index, mid in enumerate(primary_ids + adjacent_ids)]
    candidate = _candidate("candidate-shared", "m-question", "m-answer", chat_id=chat_id, weak_only=weak_only)
    open_thread = {
        "thread_id": f"thread-{packet_id}",
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "open_boundary": True,
        "open_slot_codes": ["subject_unknown", "state_unknown"],
        "source_refs": [{"type": "message", "id": "m-question", "account_id": ACCOUNT, "chat_id": chat_id}],
        "evidence_refs": [{"evidence_id": "e-m-question", "message_id": "m-question", "account_id": ACCOUNT, "chat_id": chat_id}],
        "candidate_only": True,
    }
    return K2ContextPacket(
        packet_id=packet_id,
        account_id=ACCOUNT,
        chat_id=chat_id,
        anchor_fragment_id="f-m-question",
        anchor_bundle_id=f"bundle-{packet_id}",
        source_message_ids=tuple(primary_ids),
        claim_ids=(f"claim-{packet_id}",),
        primary_fragments=tuple(primary),
        authoritative_facts=tuple(facts),
        adjacent_context=tuple(adjacent),
        candidate_qa_links=(candidate,),
        candidate_person_history=(),
        candidate_object_history=(candidate,),
        candidate_state_history=(),
        open_thread_candidates=(open_thread,),
        activation_cues=(
            {"cue_type": "question_follow_up", "message_ids": ["m-question"], "replay_key": "cue-question"},
            {"cue_type": "explicit_reference", "message_ids": ["m-question", "m-answer"], "replay_key": "cue-reply"},
        ),
        candidate_reason=("time_proximity_weak", "same_segment_weak") if weak_only else ("explicit_reply",),
        uncertainties=("state_unknown",),
        source_refs=(
            {"type": "message", "id": "m-question", "account_id": ACCOUNT, "chat_id": chat_id},
            {"type": "message", "id": "m-answer", "account_id": ACCOUNT, "chat_id": chat_id},
        ),
        evidence_refs=(
            {"evidence_id": "e-m-question", "type": "span", "message_id": "m-question", "account_id": ACCOUNT, "chat_id": chat_id, "span": {"start": 0, "end": 8}},
            {"evidence_id": "e-m-answer", "type": "span", "message_id": "m-answer", "account_id": ACCOUNT, "chat_id": chat_id, "span": {"start": 0, "end": 8}},
            # An exact duplicate across packet windows gives the contract a
            # canonical evidence row whose singleton property is independent
            # of legitimate owner-specific evidence variants.
            {"evidence_id": "e-shared", "type": "span", "message_id": "m-question", "account_id": ACCOUNT, "chat_id": chat_id, "span": {"start": 0, "end": 8}},
        ),
        fixed_part={"fixed_part_version": "k6-fixed-v1", "scope": {"account_id": ACCOUNT, "chat_id": chat_id}, "marker": fixed_extra},
        dynamic_part={"dynamic_part_version": "k6-dynamic-v1", "marker": dynamic_extra, "status": "open"},
        packet_version="k6-k2-synthetic-v1",
    )


def _packet_rows(value: Any) -> list[Mapping[str, Any]]:
    return _table(value, ("packet_table", "packets", "packet_records", "packet_refs"))


def _content_rows(value: Any) -> list[Mapping[str, Any]]:
    return _table(value, ("content_table", "contents", "content_records", "messages", "message_table"))


def _fragment_rows(value: Any) -> list[Mapping[str, Any]]:
    return _table(value, ("fragment_table", "fragments", "fragment_records"))


def _candidate_rows(value: Any) -> list[Mapping[str, Any]]:
    return _table(value, ("candidate_row_table", "candidate_table", "candidates", "candidate_records"))


def _evidence_rows(value: Any) -> list[Mapping[str, Any]]:
    return _table(value, ("evidence_ref_table", "evidence_table", "evidence", "evidence_records"))


def _compact(packets: Sequence[K2ContextPacket], *, store: CompactContextPacketStore | None = None, **kwargs: Any) -> Any:
    options = {
        "max_input_token_proxy": 2000,
        "max_messages": 24,
        "max_candidate_rows": 64,
        "max_evidence_refs": 64,
    }
    options.update(kwargs)
    if store is not None:
        options["store"] = store
    return compact_context_packets(tuple(packets), **options)


def _materialize(value: Any, *, store: CompactContextPacketStore | None = None, **kwargs: Any) -> Any:
    """Materialize every selected leaf from a compact result.

    The frozen function materializes one ``(store, packet_id)`` pair.  A
    result may contain a split root, so the contract helper intentionally
    materializes each returned leaf and wraps the rows for the assertions
    below.  ``allow_over_capacity`` is only an inspection escape hatch for a
    pending/over-capacity leaf; the assertions still enforce the hard limits.
    """

    options = dict(kwargs)
    if store is not None:
        options["store"] = store
    packet_values = getattr(value, "packets", None)
    if packet_values is not None and not isinstance(value, (list, tuple)):
        rendered: list[Mapping[str, Any]] = []
        for packet in tuple(packet_values):
            try:
                row = materialize_stage_packet(value, packet, **options)
            except Exception as exc:
                # Pending leaves are not provider-sendable, but their open
                # snapshot/recovery contract still needs to be inspectable.
                # Re-run only with the explicit local inspection override.
                if "allow_over_capacity" in options or "capacity" in options:
                    raise
                row = materialize_stage_packet(value, packet, allow_over_capacity=True, **options)
            if isinstance(row, Mapping):
                rendered.append(dict(row))
        return {"packets": rendered}
    return materialize_stage_packet(value, **options)


def _materialized_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, (list, tuple)):
        return [_mapping(item) for item in value]
    data = _mapping(value)
    for name in ("packets", "stage_packets", "chunks", "parts", "materialized_packets"):
        rows = data.get(name)
        if isinstance(rows, (list, tuple)):
            return [_mapping(item) for item in rows]
    return [data]


def _message_ids(row: Mapping[str, Any]) -> list[str]:
    values = _first(row, ("message_ids", "all_message_ids", "source_message_ids"), ())
    if isinstance(values, (list, tuple)):
        ids = [str(item) for item in values if item]
        if ids:
            return ids
    context_ids = row.get("context_message_ids")
    if isinstance(context_ids, (list, tuple)):
        ids = [str(item) for item in context_ids if item]
        if ids:
            return ids
    messages = row.get("messages")
    if isinstance(messages, (list, tuple)):
        return [str(item.get("message_id")) for item in messages if isinstance(item, Mapping) and item.get("message_id")]
    return []


def _candidate_count(row: Mapping[str, Any]) -> int:
    values = _first(row, ("candidate_ids", "candidates", "candidate_refs"), ())
    if isinstance(values, (list, tuple, set, frozenset)):
        return len(values)
    nested = row.get("candidate_context")
    if isinstance(nested, Mapping):
        rows = nested.get("candidate_rows")
        if isinstance(rows, (list, tuple, set, frozenset)):
            return len(rows)
        ids = nested.get("candidate_ids")
        if isinstance(ids, (list, tuple, set, frozenset)):
            return len(ids)
    return int(row.get("candidate_count") or 0)


def _evidence_count(row: Mapping[str, Any]) -> int:
    values = _first(row, ("evidence_ids", "evidence", "evidence_refs"), ())
    return len(values) if isinstance(values, (list, tuple, set, frozenset)) else int(row.get("evidence_count") or 0)


def _token_proxy(row: Mapping[str, Any]) -> int:
    """Conservative provider-request proxy: canonical JSON chars / four."""

    return (len(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + 3) // 4


def test_global_content_and_fragment_tables_are_singletons_and_packets_only_reference() -> None:
    packets = (_packet("p-one"), _packet("p-two"))
    compact = _compact(packets)

    contents = _content_rows(compact)
    fragments = _fragment_rows(compact)
    packet_rows = _packet_rows(compact)

    content_ids = [_row_id(row, ("content_id", "message_id", "id")) for row in contents]
    fragment_ids = [_row_id(row, ("fragment_id", "id")) for row in fragments]
    assert len(content_ids) == len(set(content_ids))
    assert len(fragment_ids) == len(set(fragment_ids))
    for message_id in ("m-question", "m-answer"):
        refs = {
            str(ref)
            for row in fragments
            if row.get("message_id") == message_id
            for ref in ((row.get("content_ref_fields") or {}).values() if isinstance(row.get("content_ref_fields"), Mapping) else ())
        }
        assert len(refs) == 1, (message_id, refs)
        assert refs <= set(content_ids)
    assert fragment_ids.count("f-m-question") == 1
    assert fragment_ids.count("f-m-answer") == 1

    # A packet row is an index into the global tables.  It must not carry a
    # second copy of message/fragment content or nested records.
    assert packet_rows
    for row in packet_rows:
        assert not any(key.casefold() in BODY_KEYS for key, _ in _walk(row))
        for field in ("content", "messages", "fragments", "candidates", "evidence", "authoritative_facts"):
            assert field not in row, (field, row)
        for field in ("message_ids", "source_message_ids", "content_ids", "fragment_ids", "candidate_ids", "evidence_ids"):
            if field in row:
                assert all(not isinstance(item, Mapping) for item in row[field])


def test_candidate_and_evidence_tables_dedupe_and_preserve_authority_candidate_layers() -> None:
    compact = _compact((_packet("p-one"), _packet("p-two")))
    candidates = _candidate_rows(compact)
    evidence = _evidence_rows(compact)
    data = _mapping(compact)

    candidate_ids = [_row_id(row, ("candidate_id", "id")) for row in candidates]
    evidence_ids = [_row_id(row, ("evidence_id", "id")) for row in evidence]
    assert len(candidate_ids) == len(set(candidate_ids))
    assert candidate_ids.count("candidate-shared") == 1
    # Same canonical evidence repeated by both packet windows is stored once;
    # owner-specific variants are allowed to remain distinct refs.
    assert evidence_ids.count("e-shared") == 1
    assert any(item == "e-m-question" for item in evidence_ids)
    assert any(item == "e-m-answer" for item in evidence_ids)

    # The compact representation keeps authority and candidate views as
    # separate layers.  Accept either explicit top-level tables or their
    # namespaced projections, but never a candidate embedded in authority.
    authority = _table(compact, ("authoritative_fact_table", "authoritative_facts", "authority_table", "authority"))
    candidate_layer = _table(compact, ("candidate_row_table", "candidate_table", "candidate_context", "candidate_layer", "candidates"))
    authority_ids = {
        _row_id(row, ("message_id", "id"))
        for row in authority
        if isinstance(row, Mapping) and _first(row, ("message_id", "id")) is not None
    }
    candidate_layer_ids = {
        _row_id(row, ("candidate_id", "id"))
        for row in candidate_layer
        if isinstance(row, Mapping) and _first(row, ("candidate_id", "id")) is not None
    }
    assert "m-question" in authority_ids
    assert "m-answer" in authority_ids
    assert not authority_ids & candidate_layer_ids


def test_stage_materialization_is_bounded_and_keeps_greeting_ack_context_only() -> None:
    compact = _compact((_packet("p-one"),))
    materialized = _materialize(compact)
    rows = _materialized_rows(materialized)
    assert rows

    seen_message_ids: set[str] = set()
    for row in rows:
        ids = _message_ids(row)
        seen_message_ids.update(ids)
        assert len(ids) <= 24
        assert _candidate_count(row) <= 64
        assert _evidence_count(row) <= 64
        # Stage A/B/C are all carried by the same bounded provider envelope;
        # this is deliberately the same conservative proxy used by the
        # project contracts: canonical JSON characters divided by four.
        assert _token_proxy(row) <= 2000
        assert "messages" in row       # Stage A material
        assert "authoritative_facts" in row  # Stage B material
        assert "candidate_context" in row  # Stage C material

    assert {"m-question", "m-answer", "m-greeting", "m-ack"} <= seen_message_ids
    serialized = json.dumps(_jsonable(rows), ensure_ascii=False, sort_keys=True)
    assert "你好" in serialized
    assert "收到" in serialized
    assert "context_only" in serialized


def test_oversized_material_splits_or_stays_pending_with_recoverable_open_snapshot_and_source_refs() -> None:
    source = _packet("p-large", large=True)
    compact = _compact((source,))
    materialized = _materialize(compact)
    rows = _materialized_rows(materialized)
    source_ids = set(source.source_message_ids) | {"m-greeting", "m-ack"}
    materialized_ids = set().union(*(_message_ids(row) for row in rows)) if rows else set()

    if len(rows) > 1:
        assert materialized_ids >= source_ids
        for row in rows:
            status = str(row.get("status") or row.get("material_status") or "")
            assert status in {"open", "bounded", "complete", "split", "pending"}
            assert len(_message_ids(row)) <= 24
            assert _candidate_count(row) <= 64
            assert _evidence_count(row) <= 64
            if status != "pending":
                assert _token_proxy(row) <= 2000
    else:
        row = rows[0]
        status = str(row.get("status") or row.get("material_status") or "")
        assert status in {"pending", "split", "bounded", "complete"}
        # If the implementation elects not to split, pending is the only
        # safe outcome and its open snapshot must remain replayable.
        if len(_message_ids(row)) > 24 or _candidate_count(row) > 64 or _evidence_count(row) > 64:
            assert status == "pending"
        snapshot = _first(row, ("open_snapshot", "open_thread_snapshot", "open_snapshot_refs"))
        refs = _first(row, ("source_refs", "source_references", "all_source_refs"), ())
        assert snapshot is not None
        assert refs is not None
        ref_ids = {
            str(_first(ref, ("message_id", "id")))
            for ref in refs
            if isinstance(ref, Mapping) and _first(ref, ("message_id", "id")) is not None
        }
        assert source_ids <= ref_ids or source_ids <= set(_message_ids(row))


def test_scope_is_hard_isolated_and_time_or_segment_only_edges_remain_weak() -> None:
    local = _packet("p-local", weak_only=True)
    other = _packet("p-other", chat_id=OTHER_CHAT, weak_only=True)
    compact = _compact((local, other))

    candidates = _candidate_rows(compact)
    assert candidates
    for row in candidates:
        account = _first(row, ("account_id", "account"), ACCOUNT)
        chat = _first(row, ("chat_id", "chat"), CHAT)
        assert account == ACCOUNT
        assert chat in {CHAT, OTHER_CHAT}
        refs = list(_first(row, ("source_refs", "evidence_refs"), ()) or ())
        assert all(
            not isinstance(ref, Mapping)
            or (_first(ref, ("account_id", "account"), account) == account and _first(ref, ("chat_id", "chat"), chat) == chat)
            for ref in refs
        )
        reasons = set(_first(row, ("candidate_reason", "reason_codes", "supporting_slot_codes"), ()) or ())
        if reasons and reasons <= {"time_proximity_weak", "same_segment_weak"}:
            assert not bool(_first(row, ("strong_relation", "is_strong", "materialized_relation"), False))
            assert str(_first(row, ("relation_label", "relation", "status"), "candidate_only")) not in {"resolved", "same_event", "strong"}


def test_hash_tracks_content_fixed_dynamic_changes_and_replay_cache_hits() -> None:
    cache = CompactContextPacketStore()
    base = _compact((_packet("p-hash", body_suffix="-base"),), store=cache)
    replay = _compact((_packet("p-hash", body_suffix="-base"),), store=cache)

    def packet_hash(value: Any) -> str:
        store_value = getattr(value, "store", None)
        packet_index = getattr(store_value, "packet_index", None)
        if isinstance(packet_index, Mapping):
            roots = [
                item
                for item in packet_index.values()
                if getattr(item, "source_packet_id", None) == "p-hash" and not getattr(item, "parent_packet_id", None)
            ]
            if roots:
                root_hash = getattr(roots[0], "packet_hash", None) or getattr(roots[0], "hash", None)
                if root_hash:
                    return str(root_hash)
        packet_values = getattr(value, "packets", None)
        if packet_values:
            first_packet = tuple(packet_values)[0]
            direct_packet_hash = getattr(first_packet, "packet_hash", None) or getattr(first_packet, "hash", None)
            if direct_packet_hash:
                return str(direct_packet_hash)
        data = _mapping(value)
        direct = _first(data, ("packet_hash", "hash", "content_hash", "input_hash", "source_hash"))
        if direct:
            return str(direct)
        rows = _packet_rows(value)
        return _row_id(rows[0], ("packet_hash", "hash", "content_hash", "id"))

    assert packet_hash(base) == packet_hash(replay)
    replay_data = _mapping(replay)
    reported_hits = replay_data.get("cache_hits", replay_data.get("replay_cache_hits"))
    if reported_hits is not None:
        assert int(reported_hits) >= 1
    else:
        # The frozen API exposes replay through stable packet IDs and the
        # public fixed/dynamic/content cache namespaces rather than a counter.
        assert replay.store is cache
        assert replay.packet_ids == base.packet_ids
        replay_packet = base.packets[0]
        assert cache.cache.get("fixed", replay_packet.fixed_hash) is not None
        assert cache.cache.get("dynamic", replay_packet.dynamic_hash) is not None
        assert cache.cache.get("content", replay_packet.content_hash) is not None

    content_changed = _compact((_packet("p-hash", body_suffix="-changed-content"),))
    fixed_changed = _compact((_packet("p-hash", body_suffix="-base", fixed_extra="fixed-b"),))
    dynamic_changed = _compact((_packet("p-hash", body_suffix="-base", dynamic_extra="dynamic-b"),))
    assert packet_hash(base) != packet_hash(content_changed)
    assert packet_hash(base) != packet_hash(fixed_changed)
    assert packet_hash(base) != packet_hash(dynamic_changed)


def test_default_serialization_is_body_free_but_explicit_stage_materialization_can_carry_body() -> None:
    compact = _compact((_packet("p-body-free"),))
    _assert_body_free(_mapping(compact))
    stage = _materialize(compact)
    stage_data = _mapping(stage)
    # K6's default store projection is safe for ledgers/audits; body is only
    # present in the explicit provider-facing materialization.
    assert any(key.casefold() in BODY_KEYS for key, _ in _walk(stage_data))
