"""Synthetic regressions for the Stage1 development precision fixes.

The fixtures in this file are deliberately invented and contain no private
development records.  They exercise only the public context extractor; in
particular, a timestamp or a dialogue-segment marker is never treated as a
semantic answer/link signal on its own.
"""

from __future__ import annotations

from wechat_bridge.contextual_fragments import (
    LABEL_ANSWERS,
    LABEL_CONTRASTS,
    LABEL_CONTINUES,
    LABEL_ELABORATES,
    RESOLUTION_EXPLICIT,
    RESOLUTION_UNKNOWN,
    ROLE_CONTEXT_ONLY,
    ROLE_CONVERSATION_OPENER,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_ONGOING,
    extract_context_fragments,
)


def _message(message_id: str, text: str, *, speaker: str = "synthetic-speaker", sequence: int = 1, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "message_id": message_id,
        "account_id": "synthetic-account",
        "chat_id": "synthetic-chat",
        "speaker_id": speaker,
        "sender_name": speaker,
        "content": text,
        "message_type": "text",
        "sequence_in_chat": sequence,
    }
    value.update(extra)
    return value


def _segment(message_ids: list[str], *, context_ids: list[str] | None = None, opener_ids: list[str] | None = None) -> list[dict[str, object]]:
    context_ids = context_ids or []
    opener_ids = opener_ids or []
    return [
        {
            "segment_id": "synthetic-segment",
            "message_ids": message_ids,
            "context_message_ids": context_ids,
            "opener_message_ids": opener_ids,
            "substantive_message_ids": [item for item in message_ids if item not in context_ids and item not in opener_ids],
        }
    ]


def _between(result, left: str, right: str):
    return [item for item in result.relations if item.source_message_ids == (left, right)]


def test_v2_person_roles_keep_speaker_subject_and_mentioned_person_separate():
    result = extract_context_fragments(
        [
            _message(
                "roles",
                "张三说GPT失败了",
                speaker="speaker-with-same-surface",
            )
        ]
    )
    fragment = result.fragments[0]
    actor_roles = {item.role for item in fragment.actor_refs}

    assert fragment.speaker.role == "speaker"
    assert fragment.subject.role == "subject"
    assert fragment.subject.resolution == RESOLUTION_EXPLICIT
    assert fragment.mentioned_persons
    assert all(item.role == "mentioned" for item in fragment.mentioned_persons)
    assert {"speaker", "subject", "mentioned"}.issubset(actor_roles)
    assert fragment.speaker.actor_id != fragment.subject.actor_id
    # The serialized Person projection uses the canonical public role name.
    roles = {item.role for item in result.persons if item.fragment_id == fragment.fragment_id}
    assert {"speaker", "subject", "mentioned_person"}.issubset(roles)


def test_v2_public_object_annotation_preserves_explicit_object_identity():
    result = extract_context_fragments(
        [
            _message(
                "explicit-object",
                "Synthetic service failed",
                object={
                    "object_id": "OBJECT_SYNTHETIC_SERVICE",
                    "surface": "Synthetic service",
                    "span_start": 0,
                    "span_end": 16,
                },
            )
        ]
    )
    fragment = result.fragments[0]
    assert fragment.object_resolution == RESOLUTION_EXPLICIT
    assert fragment.object_id == "OBJECT_SYNTHETIC_SERVICE"
    assert fragment.objects[0].object_id == "OBJECT_SYNTHETIC_SERVICE"
    assert fragment.objects[0].source == "message.object"


def test_v2_pronoun_does_not_choose_the_latest_of_competing_prior_objects():
    result = extract_context_fragments(
        [
            _message("first-object", "GPT失败了", sequence=1),
            _message("second-object", "Codex失败了", sequence=2),
            _message("ambiguous-tail", "它后来恢复了", sequence=3),
        ]
    )
    tail = result.fragments[-1]
    assert result.fragments[0].object_resolution == RESOLUTION_EXPLICIT
    assert result.fragments[1].object_resolution == RESOLUTION_EXPLICIT
    assert tail.object_resolution == RESOLUTION_UNKNOWN
    assert tail.object_id == "unknown"
    assert tail.object_inherited_from_id is None


def test_v2_context_only_bridge_is_skipped_for_question_answer_search():
    messages = [
        _message("question", "GPT怎么恢复？", speaker="questioner", sequence=1),
        _message("bridge", "我先记一下", speaker="listener", sequence=2, dialogue_role=ROLE_CONTEXT_ONLY),
        _message("answer", "已经恢复了", speaker="listener", sequence=3),
    ]
    result = extract_context_fragments(
        messages,
        _segment(["question", "bridge", "answer"], context_ids=["bridge"]),
    )

    assert not any(item.relation == LABEL_ANSWERS for item in _between(result, "question", "bridge"))
    assert any(item.relation == LABEL_ANSWERS for item in _between(result, "question", "answer"))


def test_v2_same_segment_contrast_marker_without_semantic_support_is_not_a_link():
    result = extract_context_fragments(
        [
            _message("question", "GPT怎么恢复？", sequence=1, time_offset_seconds=100),
            _message("unrelated", "但是明天选课安排已确定", sequence=2, time_offset_seconds=101),
        ],
        _segment(["question", "unrelated"]),
    )
    relations = _between(result, "question", "unrelated")

    assert not any(item.relation in {LABEL_ANSWERS, LABEL_CONTINUES, LABEL_ELABORATES, LABEL_CONTRASTS} for item in relations)
    assert not any("contrast_marker" in item.supporting_signals and item.evidence_strength == "strong" for item in relations)


def test_v2_no_reply_shared_object_alone_is_insufficient_for_answer():
    result = extract_context_fragments(
        [
            _message("question", "GPT怎么样？", speaker="questioner", sequence=1),
            _message("statement", "GPT在线", speaker="listener", sequence=2),
        ]
    )
    answer_edges = [item for item in _between(result, "question", "statement") if item.relation == LABEL_ANSWERS]

    assert answer_edges == []


def test_v2_terminal_and_ongoing_states_remain_canonical_and_subjectless():
    result = extract_context_fragments(
        [
            _message("failed", "GPT失败了", sequence=1),
            _message("ongoing", "GPT进行中", sequence=2),
            _message("cancelled", "GPT已取消", sequence=3),
        ]
    )
    assert [item.state for item in result.fragments] == [STATE_FAILED, STATE_ONGOING, STATE_CANCELLED]
    assert all(item.subject.resolution == RESOLUTION_UNKNOWN for item in result.fragments)


def test_v2_greeting_after_substantive_stays_an_opener_and_blocks_old_object():
    result = extract_context_fragments(
        [
            _message("old", "GPT失败了", sequence=1),
            _message("greeting", "你好", sequence=2, dialogue_role=ROLE_CONVERSATION_OPENER),
            _message("new", "明天选课怎么办？", sequence=3),
        ],
        _segment(["old", "greeting", "new"], opener_ids=["greeting"]),
    )
    old, greeting, new = result.fragments
    assert greeting.role == ROLE_CONVERSATION_OPENER
    assert new.object_resolution == RESOLUTION_EXPLICIT
    assert new.object_id != old.object_id
    assert not any(item.relation in {LABEL_ANSWERS, LABEL_CONTINUES, LABEL_ELABORATES} for item in _between(result, "greeting", "new"))


def test_v2_time_only_proximity_never_creates_a_relation():
    result = extract_context_fragments(
        [
            _message("left", "GPT失败了", sequence=1, time_offset_seconds=0),
            _message("right", "明天选课安排已确定", sequence=2, time_offset_seconds=1),
        ],
        _segment(["left"],),
    )
    # The messages intentionally have different fallback segments when only
    # one is listed above; time cannot manufacture an edge across that topic.
    assert result.relations == ()
