"""K10 regressions for token-aware linear paging and evidence recovery."""

from __future__ import annotations

from typing import Any

from wechat_bridge.linear_stage_packets import (
    _scope_parts,
    build_linear_stage_packets,
    canonical_json,
    materialize_stage_a,
    recover_linear_packet,
)


ACCOUNT = "account-k10"
CHAT = "chat-k10"


def _message(message_id: str, body: str, sequence: int, *, role: str = "substantive") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "sequence_in_chat": sequence,
        "role": role,
        "fragment_type": "statement" if role == "substantive" else "conversation_opener",
        "content": body,
        "text_redacted": body,
    }


def _evidence(evidence_id: str, message_id: str, body: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "type": "span",
        "span": {"start": 0, "end": len(body)},
        "evidence_text": body,
    }


def _long_low_count_packet(*, candidate_count: int = 12, candidate_width: int = 800) -> dict[str, Any]:
    messages = [
        _message("m-question", "question body that must survive recovery", 1),
        _message("m-answer", "answer body that must survive recovery", 2),
        _message("m-greeting", "greeting body that must survive recovery", 3, role="conversation_opener"),
    ]
    evidence = [
        _evidence("e-%02d" % index, messages[index % len(messages)]["message_id"], "evidence body %02d" % index)
        for index in range(candidate_count)
    ]
    candidates = [
        {
            "candidate_id": "candidate-%02d-%s" % (index, "x" * candidate_width),
            "left_message_id": messages[index % len(messages)]["message_id"],
            "right_message_id": messages[(index + 1) % len(messages)]["message_id"],
            "account_id": ACCOUNT,
            "chat_id": CHAT,
            "relation_label": "possibly_related",
            "candidate_reason": ["explicit_reply", "shared_object"],
            "evidence_refs": [evidence[index]],
        }
        for index in range(candidate_count)
    ]
    return {
        "packet_id": "k10-long",
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "primary_fragments": messages[:2],
        "adjacent_context": [messages[2]],
        "evidence_refs": evidence,
        "candidate_qa_links": candidates,
        "source_refs": [
            {"source_ref_id": "source-question", "message_id": "m-question", "account_id": ACCOUNT, "chat_id": CHAT},
            {"source_ref_id": "source-answer", "message_id": "m-answer", "account_id": ACCOUNT, "chat_id": CHAT},
        ],
        "fixed_part": {"fixed_part_version": "k10-fixed-v1"},
        "dynamic_part": {"dynamic_part_version": "k10-dynamic-v1", "open_status": "open"},
    }


def test_low_count_long_handles_split_into_complete_linear_pages() -> None:
    source = _long_low_count_packet()
    store = build_linear_stage_packets((source,))
    root = store.roots[0]

    # Counts are below the ordinary 24/64/64 limits; token pressure alone
    # must be able to create more than one page without a cartesian product.
    assert len(root["message_handles"]) == 3
    assert len(root["candidate_handles"]) == 12
    assert len(root["evidence_handles"]) == 12
    assert len(root["page_refs"]) > 1
    assert root["page_count_bound"]["linear"] is True
    assert root["page_count_bound"]["token_page_bound"] > 1

    page_messages: list[str] = []
    page_candidates: list[str] = []
    page_evidence: list[str] = []
    for page_id in root["page_refs"]:
        page = store.get_page(page_id)
        page_messages.extend(page["message_handles"])
        page_candidates.extend(page["candidate_handles"])
        page_evidence.extend(page["evidence_handles"])
        material = materialize_stage_a(store, page_id)
        assert material["status"] == "complete"
        assert material["material_stats"]["user_token_proxy"] <= 1600
        assert material["material_stats"]["input_token_proxy"] <= 2000
        # Candidate handles are one scalar expression; topic membership is an
        # index into page arrays and does not repeat opaque handles.
        assert "candidate_handles" not in material
        assert all(isinstance(ref, str) for ref in material["candidate_link_refs"])
        assert all(
            isinstance(index, int)
            for topic in material["topic_map"].values()
            for field in ("message_indices", "candidate_indices", "evidence_indices")
            for index in topic[field]
        )
        assert "evidence body" not in canonical_json(material)

    assert page_messages == root["message_handles"]
    assert page_candidates == root["candidate_handles"]
    assert page_evidence == root["evidence_handles"]

    recovered = recover_linear_packet(store, root["root_id"])
    assert recovered["primary_fragments"][0]["content"] == "question body that must survive recovery"
    assert len(recovered["evidence_refs"]) == len(evidence := source["evidence_refs"])
    assert {row["evidence_id"] for row in recovered["evidence_refs"]} == {row["evidence_id"] for row in evidence}
    body_free = recover_linear_packet(store, root["root_id"], include_body=False)
    assert "question body that must survive recovery" not in canonical_json(body_free)


def test_nested_evidence_is_recovered_and_opaque_handle_is_not_scope() -> None:
    source = _long_low_count_packet(candidate_count=4, candidate_width=10)
    # Exercise the nested-only evidence path used by several K9 roots.
    source["evidence_refs"] = []
    source["scope"] = "%s/%s|candidate|opaque" % (ACCOUNT, CHAT)
    store = build_linear_stage_packets((source,))
    root = store.roots[0]

    recovered = recover_linear_packet(store, root["root_id"])
    assert {row["evidence_id"] for row in recovered["evidence_refs"]} == {
        row["evidence_id"] for row in source["candidate_qa_links"] for row in row["evidence_refs"]
    }
    assert root["scope"] == {"account_id": ACCOUNT, "chat_id": CHAT}
    assert _scope_parts("%s/%s|candidate|opaque" % (ACCOUNT, CHAT)) == (None, None)

