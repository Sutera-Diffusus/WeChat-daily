import json

import pytest

from wechat_bridge.bundle_semantics import UNKNOWN
from wechat_bridge.semantic_wire import build_symbol_table
from wechat_bridge.semantic_wire_v3_compact import (
    CompactWireParseError,
    EVIDENCE_KEYS,
    V3_SCHEMA_VERSION,
    assemble_compact_frame,
    build_compact_request,
    compact_frame_exemplar,
    compact_wire_payload,
    parse_compact_frame,
)


def _request(max_claims=2):
    request = build_compact_request(
        [
            {
                "message_id": "message-a",
                "chat_id": "chat-a",
                "account_id": "account-a",
                "speaker_id": "person-a",
                "content": "synthetic first message",
            },
            {
                "message_id": "message-b",
                "chat_id": "chat-a",
                "account_id": "account-a",
                "speaker_id": "person-a",
                "content": "synthetic second message",
            },
        ],
        bundle_id="bundle-a",
        chat_id="chat-a",
        max_claims=max_claims,
        account_id="account-a",
    )
    return request, build_symbol_table(request)


def _unknown_claim(handle):
    return [
        handle,
        {"i": UNKNOWN, "t": "person", "r": "subject", "d": UNKNOWN, "e": []},
        [],
        [],
        [],
        [],
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
        [],
        {key: [] for key in EVIDENCE_KEYS},
    ]


def _frame(claims, relations=None, uncertainties=None):
    return {
        "v": V3_SCHEMA_VERSION,
        "q": claims,
        "r": list(relations or []),
        "u": list(uncertainties or []),
    }


def _text(frame):
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def test_single_unknown_exemplar_assembles_canonical_bundle():
    request, table = _request(1)
    parsed = parse_compact_frame(
        compact_frame_exemplar(1),
        symbol_table=table,
        max_claims=1,
    )
    bundles = assemble_compact_frame(parsed, request, symbol_table=table)
    assert len(bundles) == 1
    assert set(bundles[0]) == {
        "schema_version",
        "bundle_id",
        "message_ids",
        "speaker",
        "subject",
        "mentioned_person",
        "target",
        "object",
        "action",
        "claim_type",
        "state",
        "modality",
        "coreference_candidates",
        "context_relations",
        "uncertainties",
        "evidence",
        "metadata",
    }
    assert bundles[0]["speaker"]["id"] == "person-a"


def test_multi_claim_tuple_and_relation_are_locally_assembled():
    request, table = _request(2)
    evidence = {key: [] for key in EVIDENCE_KEYS}
    evidence["s"] = ["e0"]
    evidence["c"] = ["e0"]
    evidence["x"] = ["e0"]
    evidence["d"] = ["e0"]
    known = {"i": "p0", "t": "person", "r": "subject", "d": "explicit", "e": ["e0"]}
    first = [
        "k0",
        known,
        [],
        [],
        [],
        [],
        "fact",
        "ongoing",
        "certain",
        [],
        evidence,
    ]
    second = _unknown_claim("k1")
    relation = {"s": "k0", "t": "k1", "l": "continues", "w": "weak", "g": ["subject"], "e": ["e0"]}
    parsed = parse_compact_frame(
        _text(_frame([first, second], [relation])),
        symbol_table=table,
        max_claims=2,
    )
    bundles = assemble_compact_frame(parsed, request, symbol_table=table)
    assert len(bundles) == 2
    assert bundles[0]["bundle_id"] == "bundle-a::k0"
    assert bundles[0]["claim_type"] == "fact"
    assert bundles[0]["context_relations"][0]["target_bundle_id"] == "bundle-a::k1"
    assert all(bundle["metadata"]["claim_count"] == 2 for bundle in bundles)


def test_provider_payload_is_bounded_and_has_only_short_handles():
    _request_obj, table = _request(8)
    payload, size = compact_wire_payload(table, max_claims=8, max_chars=1800)
    assert size <= 1800
    assert payload["v"] == V3_SCHEMA_VERSION
    assert payload["b"] == "b0"
    assert payload["c"] == "c0"
    assert all(len(row) == 5 for row in payload["m"])
    assert all(str(row[0]).startswith("m") for row in payload["m"])
    assert all(not isinstance(item, str) or "message-a" not in item for item in json.dumps(payload))


@pytest.mark.parametrize(
    "mutator",
    [
        lambda frame: frame.update({"x": 0}),
        lambda frame: frame.pop("u"),
        lambda frame: frame["q"][0].pop(),
        lambda frame: frame["q"][0].__setitem__(6, "not-a-claim-type"),
        lambda frame: frame["q"][0][10].__setitem__("c", ["e9"]),
        lambda frame: frame["q"][0][1].__setitem__("i", "p9"),
        lambda frame: (frame["q"][0].__setitem__(6, "fact"), frame["q"][0][10].__setitem__("c", [])),
    ],
)
def test_strict_shape_enum_handle_and_grounding_rejections(mutator):
    request, table = _request(1)
    frame = json.loads(compact_frame_exemplar(1))
    mutator(frame)
    with pytest.raises(CompactWireParseError):
        parse_compact_frame(_text(frame), symbol_table=table, max_claims=1)


def test_duplicate_json_key_is_rejected_before_mapping():
    _request_obj, table = _request(1)
    text = '{"v":"semantic_wire_v3_compact","q":[],"r":[],"u":[],"u":[]}'
    with pytest.raises(CompactWireParseError, match="duplicate"):
        parse_compact_frame(text, symbol_table=table, max_claims=1)


def test_time_only_strong_relation_is_rejected():
    request, table = _request(2)
    frame = _frame(
        [_unknown_claim("k0"), _unknown_claim("k1")],
        [{"s": "k0", "t": "k1", "l": "continues", "w": "strong", "g": ["time"], "e": ["e0"]}],
    )
    with pytest.raises(CompactWireParseError, match="time_only"):
        parse_compact_frame(_text(frame), symbol_table=table, max_claims=2)
