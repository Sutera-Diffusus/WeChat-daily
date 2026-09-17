"""Synthetic Stage1 behaviour matrix for contextual_fragments.

These tests intentionally pass only public, synthetic message fields.  They
are a guardrail for extraction/provenance semantics and do not invoke the
production analysis, event clustering, title generation or card/UI code.
"""

from __future__ import annotations

from copy import deepcopy

from wechat_bridge.contextual_fragments import (
    ACTOR_ROLES,
    ARGUMENT_ROLES,
    CLAIM_TYPES,
    FRAGMENT_TYPES,
    LABEL_ANSWERS,
    LABEL_CONTRASTS,
    LABEL_CONTINUES,
    LABEL_ELABORATES,
    REL_OBJECT_INHERITANCE,
    REL_STATE_TRANSITION,
    ROLE_CONTEXT_ONLY,
    ROLE_CONVERSATION_OPENER,
    ROLE_SUBSTANTIVE,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_RESOLVED,
    STATE_UNKNOWN,
    RESOLUTION_EXPLICIT,
    RESOLUTION_INHERITED,
    RESOLUTION_UNKNOWN,
    MODALITY_VALUES,
    OBJECT_RESOLUTIONS,
    RELATION_LABELS,
    RELATION_STRENGTHS,
    STATE_VALUES,
    extract_context_fragments,
)


def _message(message_id, text, *, speaker="speaker-a", sequence=None, **extra):
    value = {
        "message_id": message_id,
        "account_id": "synthetic-account",
        "chat_id": "synthetic-chat",
        "speaker_id": speaker,
        "sender_name": speaker,
        "content": text,
        "message_type": "text",
    }
    if sequence is not None:
        value["sequence_in_chat"] = sequence
    value.update(extra)
    return value


def test_speaker_mentioned_and_subject_are_distinct_roles():
    result = extract_context_fragments([_message("m1", "张三说GPT失败了")])
    fragment = result.fragments[0]

    assert fragment.speaker_id == "speaker-a"
    assert fragment.speaker.role == "speaker"
    assert fragment.subject.surface_text == "张三"
    assert fragment.subject.role == "subject"
    assert fragment.subject.resolution == RESOLUTION_EXPLICIT
    assert fragment.mentioned_person_ids == ("ACTOR:张三",)
    assert fragment.mentioned_persons[0].role == "mentioned"
    assert fragment.speaker.actor_id != fragment.subject.actor_id


def test_explicit_inherited_and_unknown_objects_have_provenance():
    messages = [
        _message("m1", "GPT失败了", sequence=1),
        _message("m2", "后来恢复了", sequence=2),
        _message("m3", "它还好吗", sequence=3),
    ]
    segments = [
        {
            "segment_id": "segment-one",
            "message_ids": ["m1", "m2"],
            "substantive_message_ids": ["m1", "m2"],
        },
        {
            "segment_id": "segment-two",
            "message_ids": ["m3"],
            "substantive_message_ids": ["m3"],
        },
    ]
    result = extract_context_fragments(messages, segments)
    first, second, third = result.fragments

    assert first.object_resolution == RESOLUTION_EXPLICIT
    assert second.object_resolution == RESOLUTION_INHERITED
    assert second.objects[0].inherited_from_id == first.objects[0].ref_id
    assert second.context_message_ids == ("m1",)
    assert third.object_resolution == RESOLUTION_UNKNOWN
    assert third.object_id == "unknown"
    assert any(relation.subtype == REL_STATE_TRANSITION for relation in result.relations)


def test_greeting_is_retained_and_mixed_greeting_splits():
    result = extract_context_fragments(
        [_message("m1", "你好，接口还没好吗？", sequence=1)]
    )

    assert len(result.fragments) == 2
    opener, question = result.fragments
    assert opener.role == ROLE_CONVERSATION_OPENER
    assert opener.fragment_type == "conversation_opener"
    assert opener.is_opener is True
    assert question.role == ROLE_SUBSTANTIVE
    assert question.fragment_type == "question"
    assert question.intent == "question"
    assert question.object_resolution == RESOLUTION_EXPLICIT
    assert opener.evidence_text


def test_context_only_ack_is_kept_without_state_or_object_resolution():
    result = extract_context_fragments(
        [
            _message("m1", "你好", sequence=1),
            _message("m2", "GPT失败了", sequence=2),
            _message("m3", "收到", sequence=3),
        ]
    )
    assert result.fragments[0].role == ROLE_CONVERSATION_OPENER
    assert result.fragments[1].role == ROLE_SUBSTANTIVE
    assert result.fragments[2].role == ROLE_CONTEXT_ONLY
    assert result.fragments[2].state == STATE_UNKNOWN
    assert result.fragments[2].object_resolution == RESOLUTION_UNKNOWN


def test_state_projection_and_modality_keep_question_from_claiming_resolution():
    result = extract_context_fragments(
        [
            _message("m1", "GPT怎么恢复？", sequence=1),
            _message("m2", "我觉得可能恢复了", sequence=2),
            _message("m3", "已经恢复了", sequence=3),
            _message("m4", "现在还没解决", sequence=4),
        ]
    )
    question, possible, resolved, blocked = result.fragments

    assert question.state == STATE_UNKNOWN
    assert question.modality == "unknown"
    assert possible.state == STATE_RESOLVED
    assert possible.modality == "possible"
    assert resolved.state == STATE_RESOLVED
    assert blocked.state == STATE_BLOCKED
    assert blocked.states[0].state_detail == "没解决"
    assert any(relation.relation == LABEL_ANSWERS for relation in result.relations)


def test_multiple_viewpoints_split_and_contrast_instead_of_merge():
    result = extract_context_fragments(
        [_message("m1", "张三说GPT失败了，但李四认为已经恢复", sequence=1)]
    )
    assert len(result.fragments) == 2
    left, right = result.fragments
    assert left.subject.surface_text == "张三"
    assert right.subject.surface_text == "李四"
    assert set(left.mentioned_person_ids) == {"ACTOR:张三"}
    assert set(right.mentioned_person_ids) == {"ACTOR:李四"}
    assert any(relation.relation == LABEL_CONTRASTS for relation in result.relations)
    assert all(relation.is_event_merge is False for relation in result.relations)


def test_question_answer_and_object_inheritance_are_context_edges_only():
    result = extract_context_fragments(
        [
            _message("q", "GPT怎么恢复？", speaker="a", sequence=1),
            _message("a", "已经恢复了", speaker="b", sequence=2),
        ]
    )
    question, answer = result.fragments
    assert question.fragment_type == "question"
    assert answer.fragment_type == "answer"
    assert answer.object_resolution == RESOLUTION_INHERITED
    assert any(relation.relation == LABEL_ANSWERS for relation in result.relations)
    assert not hasattr(result, "events")


def test_topic_shift_does_not_close_or_resolve_old_context():
    result = extract_context_fragments(
        [
            _message("m1", "GPT失败了", sequence=1),
            _message("m2", "换个话题，明天选课怎么办？", sequence=2),
        ]
    )
    first, second = result.fragments
    assert first.state == STATE_FAILED
    assert second.topic_boundary == "shift"
    assert second.state == STATE_UNKNOWN
    assert second.object_resolution == RESOLUTION_EXPLICIT
    assert any(relation.relation_type == "topic_shift" for relation in result.relations)
    assert all(relation.relation != "resolved" for relation in result.relations)


def test_silence_never_implies_resolution():
    result = extract_context_fragments(
        [
            _message("m1", "GPT失败了", sequence=1),
            _message("m2", "", sequence=2),
        ]
    )
    silent = result.fragments[1]
    assert silent.is_silent is True
    assert silent.state == STATE_UNKNOWN
    assert silent.states == ()
    assert silent.fragment_type == "unknown"
    assert all(state.state != STATE_RESOLVED for state in silent.states)


def test_multiple_antecedents_do_not_allow_unsafe_pronoun_inheritance():
    result = extract_context_fragments(
        [
            _message("m1", "GPT和Codex都失败了", sequence=1),
            _message("m2", "它后来恢复了", sequence=2),
        ]
    )
    assert len(result.fragments[0].objects) >= 2
    assert result.fragments[1].object_resolution == RESOLUTION_UNKNOWN


def test_time_is_weak_and_never_creates_a_time_only_relation():
    same_object = extract_context_fragments(
        [
            _message("m1", "GPT失败了", sequence=1, time_offset_seconds=0),
            _message("m2", "后来恢复了", sequence=2, time_offset_seconds=1),
        ]
    )
    transition = next(
        relation
        for relation in same_object.relations
        if relation.subtype == REL_STATE_TRANSITION
    )
    assert transition.time_evidence == "weak"
    assert "time_proximity_weak" in transition.supporting_signals

    unrelated = extract_context_fragments(
        [
            _message("m1", "GPT失败了", sequence=1, time_offset_seconds=0),
            _message("m2", "GitHub登录成功", sequence=2, time_offset_seconds=1),
        ],
        [
            {"segment_id": "s1", "message_ids": ["m1"]},
            {"segment_id": "s2", "message_ids": ["m2"]},
        ],
    )
    assert unrelated.relations == ()


def test_public_only_input_is_not_mutated_or_read_from_private_fields():
    messages = [
        _message(
            "m1",
            "普通内容",
            sequence=1,
            private_text="GPT失败了",
            frozen_label={"object": "GPT"},
            raw_message={"content": "GPT失败了"},
        )
    ]
    original = deepcopy(messages)
    result = extract_context_fragments(messages)
    assert messages == original
    assert result.fragments[0].object_resolution == RESOLUTION_UNKNOWN
    assert result.fragments[0].state == STATE_UNKNOWN


def test_stage1_contract_enums_and_serialization_are_canonical():
    assert STATE_VALUES == {"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"}
    assert MODALITY_VALUES == {"certain", "probable", "possible", "required", "desired", "unknown"}
    assert OBJECT_RESOLUTIONS == {"explicit", "inherited", "unknown"}
    assert RELATION_LABELS == {
        "continues",
        "elaborates",
        "answers",
        "contrasts",
        "topic_shift",
        "possibly_related",
        "insufficient",
    }
    assert RELATION_STRENGTHS == {"strong", "medium", "weak", "none"}
    assert ACTOR_ROLES == {"speaker", "mentioned_person", "subject"}
    assert "object" in ARGUMENT_ROLES and "unknown" in ARGUMENT_ROLES
    assert CLAIM_TYPES == {"fact", "opinion", "question", "suggestion", "hypothesis"}
    assert "conversation_opener" in FRAGMENT_TYPES

    result = extract_context_fragments(
        [
            _message("q", "GPT怎么恢复？", speaker="a", sequence=1),
            _message("a", "已经恢复了", speaker="b", sequence=2),
        ]
    )
    payload = result.to_dict()
    assert payload["schema_version"] == "semantic_v2"
    assert payload["context_schema_version"] == "dialogue_context_v1"
    assert payload["fragments"][0]["fragment_text_redacted"]
    assert payload["fragments"][0]["claim_ids"]
    assert payload["persons"]
    assert payload["arguments"]
    assert payload["claims"]
    assert payload["relations"]
    assert payload["relations"][0]["relation_type"] in RELATION_LABELS
    assert payload["relations"][0]["label"] in RELATION_LABELS
    assert payload["relations"][0]["evidence_strength"] in RELATION_STRENGTHS
    assert payload["relations"][0]["confidence"] in {"high", "medium", "low"}
    assert "events" not in payload
