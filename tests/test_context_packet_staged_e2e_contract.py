"""Synthetic K2 ContextPacket -> staged DeepSeek E2E contract.

This file deliberately exercises the adapter boundary that is still being
implemented.  It does not read a private/frozen split and it never calls a
real provider.  The contract chosen here is intentionally small and explicit:

``wechat_bridge.staged_context_packet_adapter.adapt_context_packet`` accepts
the rich K2 ``context_packets.ContextPacket`` and returns the minimal K3
``staged_deepseek_analyzer.ContextPacket``.  The rich, reversible projection
is carried in the K3 packet's ``metadata`` under ``authoritative_facts`` and
``candidate_context``; it is therefore available to the staged model without
turning local candidate hints into semantic facts.

The tests are expected to be red until that adapter exists.  All payloads are
synthetic and use opaque-looking IDs only; no real message body or identity is
loaded by this module.
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from wechat_bridge.context_packets import ContextPacket as K2ContextPacket
from wechat_bridge.staged_deepseek_analyzer import (
    CLAIM_KEYS,
    ContextPacket as K3ContextPacket,
    FakeStageModel,
    StageCache,
    StageProtocolError,
    StagedDeepseekAnalyzer,
    summarize_ledger,
)

# This import is intentionally direct: a missing adapter is a red integration
# gate, not a reason to silently skip the E2E contract.
from wechat_bridge.staged_context_packet_adapter import adapt_context_packet


ACCOUNT = "account-synthetic"
CHAT = "chat-synthetic"
OTHER_CHAT = "chat-other-synthetic"


def _authoritative(message_id: str, speaker_id: str, sequence: int, *, chat_id: str = CHAT) -> dict[str, object]:
    return {
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": chat_id,
        "speaker_id": speaker_id,
        "direction": "outgoing" if speaker_id == "speaker-self" else "incoming",
        "message_type": "text",
        "sequence_in_chat": sequence,
        "event_time": "synthetic-%02d" % sequence,
        "reply_to_message_id": "m-question" if message_id == "m-answer" else None,
        "quote_edges": [],
        "metadata_authoritative": True,
    }


def _fragment(
    fragment_id: str,
    message_id: str,
    text: str,
    speaker_id: str,
    *,
    role: str = "substantive",
    fragment_type: str = "statement",
    object_id: str = "unknown",
    object_resolution: str = "unknown",
    state: str = "unknown",
    is_opener: bool = False,
) -> dict[str, object]:
    return {
        "fragment_id": fragment_id,
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "segment_id": "segment-synthetic",
        "text_redacted": text,
        "span": {"start": 0, "end": len(text)},
        "role": role,
        "fragment_type": fragment_type,
        "speaker_id": speaker_id,
        "mentioned_person_ids": ["person-lee"] if message_id == "m-answer" else [],
        "subject_id": "person-lee" if message_id == "m-answer" else "unknown",
        "object_id": object_id,
        "object_resolution": object_resolution,
        "state_candidate": state,
        "state_evidence": "explicit" if state != "unknown" else "unknown",
        "actions_candidate": ["report"] if message_id == "m-answer" else [],
        "intent_candidate": "question" if message_id == "m-question" else "statement",
        "is_opener": is_opener,
        "is_silent": False,
        "candidate_only": True,
        "evidence_refs": [{"type": "span", "id": "e-%s" % message_id, "message_id": message_id, "span": {"start": 0, "end": len(text)}}],
    }


def _candidate(left: str, right: str, kind: str, *reasons: str) -> dict[str, object]:
    return {
        "candidate_id": "candidate-%s-%s" % (left, right),
        "left_message_id": left,
        "right_message_id": right,
        "left_fragment_id": "f-%s" % left.removeprefix("m-"),
        "right_fragment_id": "f-%s" % right.removeprefix("m-"),
        "relation_label": "possibly_related",
        "relation_subtype": kind,
        "supporting_slot_codes": list(reasons),
        "candidate_reason": list(reasons),
        "confidence": "low",
        "evidence_refs": [
            {"type": "span", "id": "e-%s" % left, "message_id": left, "span": {"start": 0, "end": 1}},
            {"type": "span", "id": "e-%s" % right, "message_id": right, "span": {"start": 0, "end": 1}},
        ],
        "candidate_only": True,
    }


def _k2_packet(*, cross_chat: bool = False, weak_only: bool = False) -> K2ContextPacket:
    """Build one rich K2 packet with every retrieval view populated."""

    primary = (
        _fragment("f-question", "m-question", "question about object", "speaker-self", fragment_type="question", object_id="object-gpt", object_resolution="explicit"),
        _fragment("f-answer", "m-answer", "answer with state", "speaker-other", object_id="object-gpt", object_resolution="explicit", state="failed"),
    )
    adjacent = (
        _fragment("f-greeting", "m-greeting", "你好", "speaker-self", role="conversation_opener", fragment_type="conversation_opener", is_opener=True),
        _fragment("f-ack", "m-ack", "收到", "speaker-other", role="context_only", fragment_type="acknowledgement"),
    )
    facts = (
        _authoritative("m-question", "speaker-self", 1),
        _authoritative("m-answer", "speaker-other", 2),
        _authoritative("m-greeting", "speaker-self", 3),
        _authoritative("m-ack", "speaker-other", 4),
    )
    if cross_chat:
        foreign = _authoritative("m-foreign", "speaker-foreign", 5, chat_id=OTHER_CHAT)
        facts = facts + (foreign,)
    qa = (_candidate("m-question", "m-answer", "qa", "question_signal", "explicit_reply"),)
    people = (_candidate("m-answer", "m-greeting", "person_history", "shared_mentioned_person"),)
    objects = (_candidate("m-question", "m-answer", "object_history", "shared_object"),)
    states = (_candidate("m-question", "m-answer", "state_history", "state_change"),)
    if weak_only:
        qa = ()
        people = ()
        objects = ()
        states = ()
    dynamic_candidate_reasons = ("time_proximity_weak", "same_segment_weak") if weak_only else ("explicit_reply", "shared_object")
    return K2ContextPacket(
        packet_id="k2-packet-synthetic",
        account_id=ACCOUNT,
        chat_id=CHAT,
        anchor_fragment_id="f-question",
        anchor_bundle_id="bundle-synthetic",
        source_message_ids=("m-question", "m-answer"),
        claim_ids=("claim-synthetic",),
        primary_fragments=primary,
        authoritative_facts=facts,
        adjacent_context=adjacent,
        candidate_qa_links=qa,
        candidate_person_history=people,
        candidate_object_history=objects,
        candidate_state_history=states,
        open_thread_candidates=(
            {
                "thread_id": "thread-synthetic",
                "open_boundary": True,
                "unresolved_slot_codes": ["subject_unknown", "state_unknown"],
                "candidate_only": True,
            },
        ),
        activation_cues=(
            {"cue_type": "question_follow_up", "message_ids": ["m-question"], "replay_key": "cue-question"},
            {"cue_type": "explicit_reference", "message_ids": ["m-answer", "m-question"], "replay_key": "cue-reply"},
            {"cue_type": "object_history", "message_ids": ["m-question", "m-answer"], "replay_key": "cue-object"},
            {"cue_type": "state_history", "message_ids": ["m-answer"], "replay_key": "cue-state"},
        ),
        candidate_reason=dynamic_candidate_reasons,
        uncertainties=("subject_unknown", "open_boundary"),
        source_refs=(
            {"type": "message", "id": "m-question"},
            {"type": "message", "id": "m-answer"},
        ),
        evidence_refs=(
            {"type": "span", "id": "e-m-question", "message_id": "m-question", "span": {"start": 0, "end": 8}},
            {"type": "span", "id": "e-m-answer", "message_id": "m-answer", "span": {"start": 0, "end": 8}},
        ),
        fixed_part={"fixed_part_version": "fixed-synthetic", "source_message_ids": ["m-question", "m-answer"]},
        dynamic_part={"dynamic_part_version": "dynamic-synthetic", "candidate_reason": list(dynamic_candidate_reasons)},
    )


def _adapt(packet: K2ContextPacket) -> K3ContextPacket:
    result = adapt_context_packet(packet)
    assert isinstance(result, K3ContextPacket)
    return result


def _topic(topic_id: str = "topic-synthetic") -> dict[str, object]:
    return {
        "topic_id": topic_id,
        "primary_message_ids": ["m-question", "m-answer"],
        "context_message_ids": ["m-greeting", "m-ack"],
        "relation": "continuation",
        "uncertainties": [],
        "evidence_ids": ["e-m-question", "e-m-answer"],
    }


def _unknown_claim(evidence: str = "e-m-answer") -> dict[str, object]:
    return {
        "speaker": "unknown",
        "subject": "unknown",
        "mentioned": "unknown",
        "target": "unknown",
        "object": "unknown",
        "action": "unknown",
        "claim_type": "unknown",
        "state": "unknown",
        "modality": "unknown",
        "evidence_ids": [evidence],
        "uncertainties": ["needs_context"],
    }


def _responses(*, forged_speaker: bool = False, bad_stage_a: bool = False) -> dict[str, object]:
    topic = _topic()
    if bad_stage_a:
        topic = dict(topic)
        topic["primary_message_ids"] = ["m-not-in-packet"]
    claim = _unknown_claim()
    if forged_speaker:
        claim["speaker"] = "speaker-forged"
    return {
        "A": {"topics": [topic]},
        "B:topic-synthetic": {"topic_id": "topic-synthetic", "claims": [claim]},
        "C": {
            "accepted_claim_ids": ["c0_0"],
            "conflicts": [],
            "missing_context": [],
            "overmerge": [],
            "undermerge": [],
            "needs_more_context": [],
        },
    }


class _CaptureModel(FakeStageModel):
    """Fake provider that records only the structured request envelope."""

    def __init__(self, responses: dict[str, object], *, model_id: str = "synthetic-deepseek") -> None:
        super().__init__(responses, model_id=model_id)
        self.requests: list[dict[str, object]] = []

    def complete(self, stage, system_prompt, user_packet, *, max_output_tokens):
        self.requests.append(deepcopy(dict(user_packet)))
        return super().complete(stage, system_prompt, user_packet, max_output_tokens=max_output_tokens)


def test_adapter_projects_all_k2_context_views_and_authoritative_facts():
    staged = _adapt(_k2_packet())
    assert staged.scope == "%s/%s" % (ACCOUNT, CHAT)
    assert staged.message_ids == ("m-question", "m-answer")
    assert staged.context_message_ids == ("m-greeting", "m-ack")
    assert set(staged.evidence_ids) >= {"e-m-question", "e-m-answer"}
    assert {"speaker-self", "speaker-other", "person-lee", "object-gpt"} <= set(staged.entity_ids)

    model_packet = staged.to_model_packet()
    metadata = model_packet["metadata"]
    assert set(metadata) >= {"authoritative_facts", "candidate_context", "source_packet_id"}
    assert metadata["source_packet_id"] == "k2-packet-synthetic"
    facts = metadata["authoritative_facts"]
    candidates = metadata["candidate_context"]
    assert {row["message_id"] for row in facts["message_metadata"]} >= {"m-question", "m-answer", "m-greeting", "m-ack"}
    assert facts["message_metadata"][0]["metadata_authoritative"] is True
    for key in (
        "primary_fragments",
        "adjacent_context",
        "continuity_candidates",
        "qa_candidates",
        "person_history",
        "object_history",
        "state_history",
        "open_threads",
        "activation_cues",
        "candidate_reasons",
        "uncertainties",
    ):
        assert key in candidates, key
    assert any(item["message_id"] == "m-greeting" for item in candidates["adjacent_context"])
    assert any(item["message_id"] == "m-ack" for item in candidates["adjacent_context"])
    assert any(item["candidate_id"] == "candidate-m-question-m-answer" for item in candidates["qa_candidates"])


def test_adapter_projection_is_immutable_and_model_cannot_replace_authority():
    source = _k2_packet()
    before = deepcopy(source.to_dict())
    staged = _adapt(source)
    fake = _CaptureModel(_responses())
    result = StagedDeepseekAnalyzer(fake).analyze(staged)
    assert result.status == "complete"
    assert source.to_dict() == before

    # The staged request includes immutable facts; model output never gets an
    # opportunity to rewrite chat/speaker/message metadata in that section.
    request = fake.requests[0]["packet"]
    assert request["packet_id"] == staged.packet_id
    assert request["scope"] == staged.scope
    assert request["message_ids"] == list(staged.message_ids)
    assert request["metadata"]["authoritative_facts"]["message_metadata"][0]["speaker_id"] == "speaker-self"
    assert all("speaker_id" not in topic for topic in result.stage_a.payload["topics"])


def test_greeting_ack_and_every_candidate_view_reach_all_stage_requests():
    staged = _adapt(_k2_packet())
    fake = _CaptureModel(_responses())
    result = StagedDeepseekAnalyzer(fake).analyze(staged)
    assert result.status == "complete"
    assert [call["stage"] for call in fake.calls] == ["A", "B", "C"]
    assert len(fake.requests) == 3
    for request in fake.requests:
        packet = request["packet"]
        assert {"m-question", "m-answer", "m-greeting", "m-ack"} <= set(packet["message_ids"] + packet["context_message_ids"])
        metadata = packet["metadata"]
        assert metadata["authoritative_facts"]["message_metadata"]
        assert metadata["candidate_context"]["activation_cues"]
        assert metadata["candidate_context"]["open_threads"]
        assert metadata["candidate_context"]["uncertainties"]


def test_cross_chat_scope_is_hard_blocked_before_staged_provider_call():
    with pytest.raises((StageProtocolError, ValueError), match="cross|scope"):
        _adapt(_k2_packet(cross_chat=True))


def test_time_and_same_segment_are_weak_candidate_reasons_only():
    staged = _adapt(_k2_packet(weak_only=True))
    metadata = staged.to_model_packet()["metadata"]
    candidates = metadata["candidate_context"]
    assert "time_proximity_weak" in candidates["candidate_reasons"][0]["reason_codes"]
    assert "same_segment_weak" in candidates["candidate_reasons"][0]["reason_codes"]
    assert all(item.get("confidence") in {"low", "unknown"} for item in candidates["candidate_reasons"])
    serialized = json.dumps(metadata, ensure_ascii=False)
    assert "same_event" not in serialized
    assert "resolved" not in serialized
    assert not candidates["continuity_candidates"]


def test_three_stages_are_strictly_separated_and_claim_evidence_stays_in_scope():
    staged = _adapt(_k2_packet())
    fake = _CaptureModel(_responses())
    result = StagedDeepseekAnalyzer(fake).analyze(staged)
    assert result.stage_a.status == "complete"
    assert set(result.stage_a.payload) == {"topics"}
    assert result.stage_b["topic-synthetic"].status == "complete"
    claim = result.stage_b["topic-synthetic"].payload["claims"][0]
    assert set(claim) == CLAIM_KEYS
    assert claim["evidence_ids"] == ["e-m-answer"]
    assert result.stage_c.status == "complete"
    assert set(result.stage_c.payload) == {
        "accepted_claim_ids",
        "conflicts",
        "missing_context",
        "overmerge",
        "undermerge",
        "needs_more_context",
    }
    assert not {"speaker", "object", "state"} & set(result.stage_a.payload)


def test_model_authority_or_id_forgery_causes_pending_not_partial_acceptance():
    staged = _adapt(_k2_packet())
    fake = _CaptureModel(_responses(forged_speaker=True))
    result = StagedDeepseekAnalyzer(fake).analyze(staged)
    assert result.stage_a.status == "complete"
    assert result.stage_b["topic-synthetic"].status == "pending"
    assert result.stage_c.status == "pending"
    assert result.status == "pending"
    assert "stage_b_speaker_out_of_scope" in result.stage_b["topic-synthetic"].errors
    assert summarize_ledger(result.ledger)["provider_calls"] == 2


def test_stage_failure_keeps_complete_previous_stage_retries_only_pending_and_never_caches_failure():
    staged = _adapt(_k2_packet())
    responses = _responses()
    responses["B:topic-synthetic"] = [
        {"topic_id": "topic-synthetic", "claims": [{"bad": True}]},
        _responses()["B:topic-synthetic"],
    ]
    fake = _CaptureModel(responses)
    cache = StageCache()
    analyzer = StagedDeepseekAnalyzer(fake, cache=cache)
    first = analyzer.analyze(staged)
    assert first.stage_a.status == "complete"
    assert first.stage_b["topic-synthetic"].status == "pending"
    assert first.stage_c.status == "pending"
    assert cache.sizes() == {"A": 1, "B": 0, "C": 0}

    second = analyzer.analyze(staged, previous=first)
    assert second.status == "complete"
    assert second.stage_a.payload == first.stage_a.payload
    assert second.stage_b["topic-synthetic"].status == "complete"
    assert second.stage_c.status == "complete"
    assert [call["stage"] for call in fake.calls] == ["A", "B", "B", "C"]
    assert cache.sizes() == {"A": 1, "B": 1, "C": 1}


def test_ledger_is_body_free_and_cache_key_changes_with_packet_model_and_stage():
    staged = _adapt(_k2_packet())
    fake_a = _CaptureModel(_responses(), model_id="model-a")
    fake_b = _CaptureModel(_responses(), model_id="model-b")
    first = StagedDeepseekAnalyzer(fake_a).analyze(staged)
    second = StagedDeepseekAnalyzer(fake_b).analyze(staged)
    first_rows = [row.to_dict() for row in first.ledger]
    second_rows = [row.to_dict() for row in second.ledger]
    assert first_rows[0]["request_sha256"] != second_rows[0]["request_sha256"]
    assert len({row["request_sha256"] for row in first_rows}) == 3
    forbidden = {"content", "body", "raw", "response", "messages", "evidence"}
    for row in first_rows + second_rows:
        assert not forbidden & set(row)
        assert len(row["request_sha256"]) == 64
        assert len(row["user_packet_sha256"]) == 64
        assert len(row["system_prefix_sha256"]) == 64

    changed = _adapt(_k2_packet(weak_only=True))
    assert changed.packet_sha256 != staged.packet_sha256
    changed_result = StagedDeepseekAnalyzer(_CaptureModel(_responses())).analyze(changed)
    assert changed_result.ledger[0].request_sha256 != first.ledger[0].request_sha256


def test_invalid_stage_a_never_calls_b_or_c_and_keeps_packet_recoverable():
    staged = _adapt(_k2_packet())
    fake = _CaptureModel(_responses(bad_stage_a=True))
    result = StagedDeepseekAnalyzer(fake).analyze(staged)
    assert result.status == "pending"
    assert result.stage_a.status == "pending"
    assert result.stage_b == {}
    assert result.stage_c.status == "pending"
    assert [call["stage"] for call in fake.calls] == ["A"]
    assert result.packet_sha256 == staged.packet_sha256
