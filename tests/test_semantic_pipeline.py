from dataclasses import replace
import json

import pytest

from wechat_bridge.semantic_pipeline import (
    EVENT_RELATIONS,
    RELATION_INSUFFICIENT_CONTEXT,
    RELATION_RELATED_EVENT,
    RELATION_SAME_EVENT,
    RELATION_SAME_TOPIC_ONLY,
    RELATION_UNRELATED,
    SHADOW_SOURCE,
    build_events,
    classify_claim_pair,
    legacy_messages_to_v2,
    run_semantic_pipeline,
    score_must_not_link,
    score_pairwise_relations,
    v2_result_to_legacy_preview,
)


def _message(message_id, content, minute, *, sender_id="speaker-a", sender_name="甲", chat_id="group-a"):
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "content": content,
        "timestamp": "2026-08-26T00:%02d:00+00:00" % minute,
        "message_type": "text",
        "is_group": True,
        "is_self": False,
    }


def _gold_messages():
    return [
        _message("gpt-reset", "GPT 最近又在不停重置。", 1, sender_name="王同学"),
        _message("gpt-reset-2", "GPT 最近又在不停重置。", 2, sender_id="speaker-b", sender_name="李同学"),
        _message("codex-reset", "Codex 在没有通知的情况下又重置了。", 3, sender_id="speaker-c", sender_name="周同学"),
        _message("relay-cost", "这个中转站没有性价比，价格太贵。", 4, sender_id="speaker-d", sender_name="陈同学"),
        _message("multica-usage", "我觉得 multica 消耗特别高，用量掉得很快。", 5, sender_id="speaker-e", sender_name="孙同学"),
        _message("wechat-risk", "微信发消息存在风控和合规风险。", 6, sender_id="speaker-f", sender_name="赵同学"),
        _message("github-register", "GitHub 注册时邮箱收不到验证码邮件。", 7, sender_id="speaker-g", sender_name="钱同学"),
        _message(
            "related-sites",
            "Linux.do、V2EX 和 Linux 相关网站也有人讨论注册流程。",
            8,
            sender_id="speaker-h",
            sender_name="吴同学",
        ),
    ]


def _claim(result, message_id, action):
    return next(
        item for item in result.claims
        if item.message_id == message_id and item.action == action
    )


def test_p0_pipeline_is_deterministic_under_input_order_changes():
    messages = _gold_messages()
    first = run_semantic_pipeline(messages)
    second = run_semantic_pipeline(list(reversed(messages)))

    assert first.analysis_run_id == second.analysis_run_id
    assert [item.mention_id for item in first.mentions] == [item.mention_id for item in second.mentions]
    assert [item.claim_id for item in first.claims] == [item.claim_id for item in second.claims]
    assert [item.event_id for item in first.events] == [item.event_id for item in second.events]
    assert [item.presentation_id for item in first.presentations] == [item.presentation_id for item in second.presentations]
    assert first.to_dict() == second.to_dict()
    json.dumps(first.to_dict(), ensure_ascii=False)


def test_structured_baseline_keeps_ai_service_issues_in_one_family_but_separate_events():
    result = run_semantic_pipeline(_gold_messages())
    selected = {
        _claim(result, "gpt-reset", "reset").claim_id,
        _claim(result, "codex-reset", "reset").claim_id,
        _claim(result, "relay-cost", "cost").claim_id,
        _claim(result, "multica-usage", "usage").claim_id,
    }
    event_by_claim = {
        claim_id: event.event_id
        for event in result.events
        for claim_id in event.claim_ids
    }

    assert len({event_by_claim[claim_id] for claim_id in selected}) == 4
    ai_family = next(item for item in result.topic_families if item.family_key == "ai_services")
    assert {event_by_claim[claim_id] for claim_id in selected}.issubset(set(ai_family.event_ids))
    assert not any(selected.issubset(set(event.claim_ids)) for event in result.events)


def test_claims_preserve_speaker_object_type_and_locatable_evidence():
    result = run_semantic_pipeline(_gold_messages())
    usage = _claim(result, "multica-usage", "usage")

    assert usage.speaker_id == "speaker-e"
    assert usage.speaker_name == "孙同学"
    assert usage.target_entity_ids == ("service:multica",)
    assert usage.claim_type == "opinion"
    source = next(item for item in result.messages if item.message_id == usage.message_id)
    evidence = usage.evidence_span
    assert source.content[evidence.span_start:evidence.span_end] == evidence.evidence_text
    assert evidence.evidence_text == usage.claim_text


def test_one_message_with_multiple_issues_produces_separate_claims_and_events():
    result = run_semantic_pipeline(
        [_message("mixed", "GPT 不停重置，而且用中转站没有性价比。", 1)]
    )
    reset = _claim(result, "mixed", "reset")
    cost = _claim(result, "mixed", "cost")
    decision = classify_claim_pair(reset, cost, result.analysis_run_id, result.created_at)

    assert reset.target_entity_ids == ("service:gpt",)
    assert cost.target_entity_ids == ("service:relay",)
    assert decision.relation == RELATION_SAME_TOPIC_ONLY
    assert {"different_core_entity", "different_action"}.issubset(decision.hard_conflict_reasons)
    assert len(result.events) == 2
    assert len(result.presentations) == 2


def test_github_registration_email_failure_and_related_sites_remain_auditable():
    result = run_semantic_pipeline(_gold_messages())
    registration = _claim(result, "github-register", "register")
    email_failure = _claim(result, "github-register", "email_delivery")
    sites_registration = _claim(result, "related-sites", "register")

    assert set(registration.target_entity_ids) == {"platform:github", "channel:email"}
    assert set(email_failure.target_entity_ids) == {"platform:github", "channel:email"}
    assert set(sites_registration.target_entity_ids) == {
        "site:linux_do", "site:v2ex", "site:linux_related"
    }
    assert classify_claim_pair(
        registration, email_failure, result.analysis_run_id, result.created_at
    ).relation == RELATION_RELATED_EVENT
    assert classify_claim_pair(
        registration, sites_registration, result.analysis_run_id, result.created_at
    ).relation == RELATION_SAME_TOPIC_ONLY


def test_pair_classifier_can_return_all_five_relations():
    messages = _gold_messages() + [
        _message("unknown-reset", "最近又在不停重置。", 9, sender_id="speaker-i"),
        _message("gpt-cost", "GPT 的价格太贵，没有性价比。", 10, sender_id="speaker-j"),
    ]
    next(item for item in messages if item["message_id"] == "gpt-reset-2")["reply_to_message_id"] = "gpt-reset"
    result = run_semantic_pipeline(messages)
    gpt_reset = _claim(result, "gpt-reset", "reset")
    gpt_reset_2 = _claim(result, "gpt-reset-2", "reset")
    gpt_cost = _claim(result, "gpt-cost", "cost")
    relay_cost = _claim(result, "relay-cost", "cost")
    wechat_risk = _claim(result, "wechat-risk", "risk")
    unknown_reset = _claim(result, "unknown-reset", "reset")

    decisions = [
        classify_claim_pair(gpt_reset, gpt_reset_2, result.analysis_run_id, result.created_at),
        classify_claim_pair(gpt_reset, gpt_cost, result.analysis_run_id, result.created_at),
        classify_claim_pair(gpt_reset, relay_cost, result.analysis_run_id, result.created_at),
        classify_claim_pair(gpt_reset, wechat_risk, result.analysis_run_id, result.created_at),
        classify_claim_pair(gpt_reset, unknown_reset, result.analysis_run_id, result.created_at),
    ]
    assert {item.relation for item in decisions} == EVENT_RELATIONS
    assert decisions[0].relation == RELATION_SAME_EVENT
    assert decisions[1].relation == RELATION_RELATED_EVENT
    assert decisions[2].relation == RELATION_SAME_TOPIC_ONLY
    assert decisions[3].relation == RELATION_UNRELATED
    assert decisions[4].relation == RELATION_INSUFFICIENT_CONTEXT


def test_independent_same_slot_reports_within_24_hours_do_not_merge_without_linkage():
    messages = [
        _message("first", "GPT 最近又在不停重置。", 1, sender_id="speaker-a", sender_name="甲"),
        _message("second", "GPT 最近又在不停重置。", 2, sender_id="speaker-b", sender_name="乙"),
        _message("third", "GPT 最近又在不停重置。", 3, sender_id="speaker-a", sender_name="甲"),
    ]
    result = run_semantic_pipeline(messages)
    claims = [_claim(result, message_id, "reset") for message_id in ("first", "second", "third")]

    assert len(result.events) == 3
    for left, right in ((claims[0], claims[1]), (claims[0], claims[2])):
        decision = classify_claim_pair(left, right, result.analysis_run_id, result.created_at)
        assert decision.relation == RELATION_RELATED_EVENT
        assert "missing_explicit_event_linkage" in decision.uncertainties

    messages[1]["reply_to_message_id"] = "first"
    replied = run_semantic_pipeline(messages[:2])
    reply_decision = classify_claim_pair(
        _claim(replied, "first", "reset"),
        _claim(replied, "second", "reset"),
        replied.analysis_run_id,
        replied.created_at,
    )
    assert reply_decision.relation == RELATION_SAME_EVENT
    assert len(replied.events) == 1


def test_claim_mentions_are_clause_local_and_inheritance_uses_only_nearest_antecedent():
    result = run_semantic_pipeline(
        [_message("repeated", "GPT 重置过一次，GPT 价格太贵，后来又重置了。", 1)]
    )
    entity_mentions = sorted(
        (
            item for item in result.mentions
            if item.message_id == "repeated"
            and item.mention_type == "entity"
            and item.normalized_id == "service:gpt"
        ),
        key=lambda item: item.span_start,
    )
    reset_claims = sorted(
        (item for item in result.claims if item.action == "reset"),
        key=lambda item: item.evidence_span.span_start,
    )
    cost_claim = _claim(result, "repeated", "cost")
    first_entity, second_entity = entity_mentions
    first_reset, inherited_reset = reset_claims

    assert first_entity.mention_id in first_reset.event_mention_ids
    assert second_entity.mention_id not in first_reset.event_mention_ids
    assert cost_claim.event_mention_ids.count(second_entity.mention_id) == 1
    assert first_entity.mention_id not in cost_claim.event_mention_ids
    assert second_entity.mention_id in inherited_reset.event_mention_ids
    assert first_entity.mention_id not in inherited_reset.event_mention_ids
    assert "target_inherited_from_previous_clause" in inherited_reset.uncertainties
    assert second_entity.mention_id in inherited_reset.provenance.input_ids
    assert first_entity.mention_id not in inherited_reset.provenance.input_ids
    inherited_evidence = {
        (item.span_start, item.span_end, item.evidence_text)
        for item in inherited_reset.evidence_refs
    }
    assert (
        second_entity.span_start,
        second_entity.span_end,
        second_entity.evidence_text,
    ) in inherited_evidence
    assert (
        first_entity.span_start,
        first_entity.span_end,
        first_entity.evidence_text,
    ) not in inherited_evidence


def test_time_mentions_are_locatable_and_long_gaps_require_explicit_reply():
    older = _message("old", "GPT 今天又在不停重置。", 1)
    newer = _message("new", "GPT 今天又在不停重置。", 2)
    newer["timestamp"] = "2026-09-05T00:02:00+00:00"
    separated = run_semantic_pipeline([older, newer])
    old_claim = _claim(separated, "old", "reset")
    new_claim = _claim(separated, "new", "reset")
    decision = classify_claim_pair(
        old_claim, new_claim, separated.analysis_run_id, separated.created_at
    )

    assert decision.relation == RELATION_RELATED_EVENT
    assert "time_window_conflict" in decision.hard_conflict_reasons
    time_mention = next(
        item for item in separated.mentions
        if item.message_id == "old" and item.mention_type == "time"
    )
    source = next(item for item in separated.messages if item.message_id == "old")
    assert source.content[time_mention.span_start:time_mention.span_end] == "今天"

    newer["reply_to_message_id"] = "old"
    replied = run_semantic_pipeline([older, newer])
    reply_decision = classify_claim_pair(
        _claim(replied, "old", "reset"),
        _claim(replied, "new", "reset"),
        replied.analysis_run_id,
        replied.created_at,
    )
    assert reply_decision.relation == RELATION_SAME_EVENT
    assert "long_range_explicit_reply" in reply_decision.supporting_slots


def test_event_topic_and_presentation_invariants_do_not_expand_evidence():
    result = run_semantic_pipeline(_gold_messages())
    claims = {item.claim_id: item for item in result.claims}
    events = {item.event_id: item for item in result.events}

    for event in result.events:
        expected_messages = tuple(sorted({claims[value].message_id for value in event.claim_ids}))
        expected_evidence = {
            (entry.message_id, entry.span_start, entry.span_end, entry.evidence_text)
            for claim_id in event.claim_ids
            for entry in claims[claim_id].evidence_refs
        }
        actual_evidence = {
            (entry.message_id, entry.span_start, entry.span_end, entry.evidence_text)
            for entry in event.evidence_refs
        }
        assert event.claim_ids
        assert event.source_message_ids == expected_messages
        assert actual_evidence == expected_evidence

    for family in result.topic_families:
        assert family.event_ids
        assert set(family.event_ids).issubset(events)

    assert len({item.event_id for item in result.presentations}) == len(result.presentations)
    for card in result.presentations:
        event = events[card.event_id]
        assert card.supported_claim_ids == event.claim_ids
        assert card.title_support_claim_ids == event.claim_ids
        assert {value for sentence in card.sentences for value in sentence.claim_ids} == set(event.claim_ids)
        assert {value for sentence in card.sentences for value in sentence.message_ids} == set(event.source_message_ids)
        assert card.source_message_ids == event.source_message_ids
        assert card.evidence_refs == event.evidence_refs
        assert card.source == SHADOW_SOURCE


def test_zero_evidence_and_missing_message_identity_are_rejected():
    with pytest.raises(ValueError, match="message_id"):
        legacy_messages_to_v2([{"content": "GPT 重置了"}])

    result = run_semantic_pipeline([_message("one", "GPT 又重置了。", 1)])
    invalid = replace(result.claims[0], source_message_ids=(), evidence_refs=())
    with pytest.raises(ValueError, match="source evidence"):
        build_events(
            [invalid], [], result.analysis_run_id, result.created_at
        )


def test_shadow_preview_is_explicit_and_never_claims_production_source():
    result = run_semantic_pipeline(_gold_messages())
    preview = v2_result_to_legacy_preview(result)

    assert preview["source"] == SHADOW_SOURCE
    assert preview["production_connected"] is False
    assert preview["read_only"] is True
    assert preview["cards"]
    assert "event_briefs" not in preview
    assert "ai_assisted" not in json.dumps(preview, ensure_ascii=False)


def test_scoring_skeleton_reports_pairwise_errors_and_must_not_link_violations():
    messages = _gold_messages()
    next(item for item in messages if item["message_id"] == "gpt-reset-2")["reply_to_message_id"] = "gpt-reset"
    result = run_semantic_pipeline(messages)
    gpt_one = _claim(result, "gpt-reset", "reset")
    gpt_two = _claim(result, "gpt-reset-2", "reset")
    relay = _claim(result, "relay-cost", "cost")
    gold = {
        (gpt_one.claim_id, gpt_two.claim_id): RELATION_SAME_EVENT,
        (gpt_one.claim_id, relay.claim_id): RELATION_SAME_TOPIC_ONLY,
    }
    pair_score = score_pairwise_relations(result.pair_decisions, gold)
    link_score = score_must_not_link(
        result.events, [(gpt_one.claim_id, relay.claim_id)]
    )

    assert pair_score["pairwise_precision"] == 1.0
    assert pair_score["pairwise_recall"] == 1.0
    assert pair_score["over_merge_count"] == 0
    assert pair_score["over_split_count"] == 0
    assert link_score["violation_count"] == 0
