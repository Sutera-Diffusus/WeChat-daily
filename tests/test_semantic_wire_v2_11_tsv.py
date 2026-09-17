import pytest

from wechat_bridge.semantic_wire import build_symbol_table
from wechat_bridge.semantic_wire_v3_compact import build_compact_request
from wechat_bridge.semantic_wire_v2_11_tsv import (
    TsvWireParseError,
    TSV_SCHEMA_VERSION,
    assemble_tsv_frame,
    parse_tsv_frame,
    tsv_frame_exemplar,
)


def _request(max_claims=8):
    request = build_compact_request(
        [
            {
                "message_id": "message-a",
                "chat_id": "chat-a",
                "account_id": "account-a",
                "speaker_id": "person-a",
                "content": "synthetic first",
            },
            {
                "message_id": "message-b",
                "chat_id": "chat-a",
                "account_id": "account-a",
                "speaker_id": "person-a",
                "content": "synthetic second",
            },
        ],
        bundle_id="bundle-a",
        chat_id="chat-a",
        max_claims=max_claims,
        account_id="account-a",
    )
    return request, build_symbol_table(request)


def _claim(
    subject="unknown",
    mentioned="",
    target="unknown",
    obj="unknown",
    action="unknown",
    claim_type="unknown",
    state="unknown",
    modality="unknown",
    evidence="",
    uncertainty="none",
):
    return chr(9).join(
        [
            "CLAIM",
            subject,
            mentioned,
            target,
            obj,
            action,
            claim_type,
            state,
            modality,
            evidence,
            uncertainty,
        ]
    )


def test_unknown_tsv_exemplar_is_one_complete_canonical_bundle():
    request, table = _request(8)
    frame = parse_tsv_frame(tsv_frame_exemplar(), symbol_table=table, max_claims=8)
    assert frame["schema_version"] == TSV_SCHEMA_VERSION
    bundles = assemble_tsv_frame(frame, request, symbol_table=table)
    assert len(bundles) == 1
    assert len(bundles[0]) == 17
    assert bundles[0]["claim_type"] == "unknown"


def test_multiple_claims_and_relation_assemble_locally():
    request, table = _request(8)
    text = chr(10).join(
        [
            _claim(
                subject="p0",
                action="ask",
                claim_type="question",
                state="ongoing",
                modality="certain",
                evidence="e0",
            ),
            _claim(),
            "REL" + chr(9) + "0" + chr(9) + "1" + chr(9) + "answers" + chr(9) + "weak" + chr(9) + "subject" + chr(9) + "e0",
        ]
    )
    frame = parse_tsv_frame(text, symbol_table=table, max_claims=8)
    bundles = assemble_tsv_frame(frame, request, symbol_table=table)
    assert len(bundles) == 2
    assert bundles[0]["subject"]["id"] == "person-a"
    assert bundles[0]["claim_type"] == "question"
    assert bundles[0]["context_relations"][0]["target_bundle_id"] == "bundle-a::k1"


@pytest.mark.parametrize(
    "text",
    [
        _claim() + chr(9) + "extra",
        "prose",
        _claim(subject="p9", evidence="e0"),
        _claim(subject="p0"),
        _claim(action="free_text", evidence="e0"),
        _claim(claim_type="not-an-enum", evidence="e0"),
        _claim(subject="p0", evidence="e9"),
        _claim() + chr(10) + _claim(),
        _claim() + chr(10) + "REL" + chr(9) + "0" + chr(9) + "2" + chr(9) + "answers" + chr(9) + "weak" + chr(9) + "subject" + chr(9) + "e0",
        _claim() + chr(10) + "REL" + chr(9) + "0" + chr(9) + "0" + chr(9) + "answers" + chr(9) + "weak" + chr(9) + "subject" + chr(9) + "e0",
        _claim() + chr(10) + "REL" + chr(9) + "0" + chr(9) + "0" + chr(9) + "answers" + chr(9) + "strong" + chr(9) + "time" + chr(9) + "e0",
    ],
)
def test_tsv_protocol_rejects_shape_handles_enums_scope_and_time_only(text):
    _request_obj, table = _request(8)
    with pytest.raises(TsvWireParseError):
        parse_tsv_frame(text, symbol_table=table, max_claims=8)


def test_claim_after_relation_and_duplicate_relation_are_rejected():
    _request_obj, table = _request(8)
    relation = "REL" + chr(9) + "0" + chr(9) + "1" + chr(9) + "answers" + chr(9) + "weak" + chr(9) + "subject" + chr(9) + "e0"
    with pytest.raises(TsvWireParseError):
        parse_tsv_frame(
            _claim() + chr(10) + relation + chr(10) + _claim(),
            symbol_table=table,
            max_claims=8,
        )
    with pytest.raises(TsvWireParseError):
        parse_tsv_frame(
            _claim() + chr(10) + _claim(subject="p0", evidence="e0") + chr(10) + relation + chr(10) + relation,
            symbol_table=table,
            max_claims=8,
        )
