from copy import deepcopy

import pytest

from wechat_bridge.stage1_context import (
    CONTEXT_RELATION_LABELS,
    EVIDENCE_STRENGTH_VALUES,
    FRAGMENT_TYPES,
    MODALITY_VALUES,
    NA,
    OBJECT_RESOLUTIONS,
    STATE_VALUES,
    load_development_gold_directory,
    project_stage1_gold,
    project_stage1_predictions,
    score_stage1,
    summarize_stage1_projection,
    validate_stage1_projection,
)


def _message(message_id="MESSAGE_SYNTH_001", split="development"):
    return {
        "message_id": message_id,
        "account_id": "ACCOUNT_SYNTH",
        "chat_id": "CHAT_SYNTH",
        "chat_type": "direct",
        "speaker_id": "PERSON_SYNTH_A",
        "direction": "inbound",
        "message_type": "text",
        "local_day": "2026-08-25",
        "time_offset_seconds": 0,
        "time_bucket": "morning",
        "sequence_in_chat": 0,
        "reply_to_message_id": None,
        "redacted_text": "合成正文不应进入投影",
        "split": split,
    }


def _evidence(message_id="MESSAGE_SYNTH_001", start=0, end=2):
    return [{"type": "message", "id": message_id, "span": {"start": start, "end": end}}]


def _full_dataset():
    messages = [_message()]
    fragments = [
        {
            "fragment_id": "FRAGMENT_SYNTH_001",
            "message_id": "MESSAGE_SYNTH_001",
            "span_start": 0,
            "span_end": 2,
            "fragment_text_redacted": "合成片段正文",
            "fragment_type": "question",
            "speaker_id": "PERSON_SYNTH_A",
            "mentioned_person_ids": ["unknown"],
            "subject_id": "PERSON_SYNTH_A",
            "subject_type": "person",
            "object_id": "OBJECT_SYNTH_A",
            "object_resolution": "explicit",
            "object_evidence_refs": _evidence(),
            "state": "ongoing",
            "state_evidence": "explicit",
            "closure_reason": "unknown",
            "claim_ids": ["CLAIM_SYNTH_001"],
            "evidence_refs": _evidence(),
            "context_message_ids": [],
        },
        {
            "fragment_id": "FRAGMENT_SYNTH_002",
            "message_id": "MESSAGE_SYNTH_001",
            "span_start": 2,
            "span_end": 4,
            "fragment_text_redacted": "合成片段正文二",
            "fragment_type": "answer",
            "speaker_id": "PERSON_SYNTH_A",
            "mentioned_person_ids": [],
            "subject_id": "PERSON_SYNTH_A",
            "subject_type": "person",
            "object_id": "OBJECT_SYNTH_A",
            "object_resolution": "inherited",
            "object_evidence_refs": _evidence(start=2, end=4),
            "object_inherited_from_id": "FRAGMENT_SYNTH_001",
            "state": "resolved",
            "state_evidence": "explicit",
            "closure_reason": "resolved",
            "claim_ids": ["CLAIM_SYNTH_002"],
            "evidence_refs": _evidence(start=2, end=4),
            "context_message_ids": ["MESSAGE_SYNTH_001"],
        },
    ]
    persons = [
        {
            "person_ref_id": "PERSON_REF_SYNTH_001",
            "person_id": "PERSON_SYNTH_A",
            "resolution": "explicit",
            "role": "speaker",
            "message_id": "MESSAGE_SYNTH_001",
            "fragment_id": "FRAGMENT_SYNTH_001",
            "span_start": None,
            "span_end": None,
            "source": "message_metadata",
            "evidence_refs": _evidence(),
            "confidence": "high",
        },
        {
            "person_ref_id": "PERSON_REF_SYNTH_002",
            "person_id": "PERSON_SYNTH_B",
            "resolution": "explicit",
            "role": "mentioned_person",
            "message_id": "MESSAGE_SYNTH_001",
            "fragment_id": "FRAGMENT_SYNTH_001",
            "span_start": 0,
            "span_end": 1,
            "source": "text",
            "evidence_refs": _evidence(),
            "confidence": "medium",
        },
    ]
    arguments = [
        {
            "argument_id": "ARG_SYNTH_001",
            "fragment_id": "FRAGMENT_SYNTH_001",
            "claim_id": "CLAIM_SYNTH_001",
            "role": "object",
            "entity_id": "OBJECT_SYNTH_A",
            "entity_type": "object",
            "resolution": "explicit",
            "evidence_refs": _evidence(),
            "confidence": "high",
        }
    ]
    mentions = [
        {
            "mention_id": "MENTION_SYNTH_001",
            "message_id": "MESSAGE_SYNTH_001",
            "fragment_id": "FRAGMENT_SYNTH_001",
            "mention_type": "entity",
            "span_start": 0,
            "span_end": 1,
            "surface_redacted": "合成实体",
            "normalized_id": "OBJECT_SYNTH_A",
            "normalized_type": "object",
            "entity_role": "object",
            "attributes": {},
            "certainty": "asserted",
        }
    ]
    claims = [
        {
            "claim_id": "CLAIM_SYNTH_001",
            "message_id": "MESSAGE_SYNTH_001",
            "fragment_id": "FRAGMENT_SYNTH_001",
            "speaker_id": "PERSON_SYNTH_A",
            "mentioned_person_ids": ["PERSON_SYNTH_B"],
            "subject_id": "PERSON_SYNTH_A",
            "subject_type": "person",
            "object_id": "OBJECT_SYNTH_A",
            "object_resolution": "explicit",
            "object_evidence_refs": _evidence(),
            "claim_type": "question",
            "claim_text_redacted": "合成断言正文",
            "target_entity_ids": ["OBJECT_SYNTH_A"],
            "event_mention_ids": ["MENTION_SYNTH_001"],
            "evidence_spans": [{"start": 0, "end": 2}],
            "stance": "neutral",
            "polarity": "neutral",
            "modality": "possible",
            "state": "ongoing",
            "state_evidence": "explicit",
            "closure_reason": "unknown",
            "attribution": "direct",
            "timestamp_message_id": "MESSAGE_SYNTH_001",
            "context_message_ids": [],
        },
        {
            "claim_id": "CLAIM_SYNTH_002",
            "message_id": "MESSAGE_SYNTH_001",
            "fragment_id": "FRAGMENT_SYNTH_002",
            "speaker_id": "PERSON_SYNTH_A",
            "mentioned_person_ids": [],
            "subject_id": "PERSON_SYNTH_A",
            "subject_type": "person",
            "object_id": "OBJECT_SYNTH_A",
            "object_resolution": "inherited",
            "object_evidence_refs": _evidence(start=2, end=4),
            "object_inherited_from_id": "FRAGMENT_SYNTH_001",
            "claim_type": "fact",
            "claim_text_redacted": "合成断言正文二",
            "target_entity_ids": ["OBJECT_SYNTH_A"],
            "event_mention_ids": [],
            "evidence_spans": [{"start": 2, "end": 4}],
            "stance": "neutral",
            "polarity": "positive",
            "modality": "certain",
            "state": "resolved",
            "state_evidence": "explicit",
            "closure_reason": "resolved",
            "attribution": "direct",
            "timestamp_message_id": "MESSAGE_SYNTH_001",
            "context_message_ids": ["MESSAGE_SYNTH_001"],
        },
    ]
    context_relations = [
        {
            "context_relation_id": "CONTEXT_REL_SYNTH_001",
            "left_anchor_id": "FRAGMENT_SYNTH_001",
            "right_anchor_id": "FRAGMENT_SYNTH_002",
            "anchor_type": "fragment",
            "label": "answers",
            "supporting_slot_codes": ["qa_intent", "shared_core_object"],
            "conflicting_slot_codes": [],
            "evidence_refs": _evidence(start=0, end=4),
            "evidence_message_ids": ["MESSAGE_SYNTH_001"],
            "explicit_reply_present": False,
            "time_distance_seconds": 1,
            "time_evidence": "weak",
            "evidence_strength": "medium",
            "confidence": "high",
        }
    ]
    return {
        "messages": messages,
        "fragments": fragments,
        "persons": persons,
        "arguments": arguments,
        "mentions": mentions,
        "claims": claims,
        "discourse_threads": [],
        "context_relations": context_relations,
    }


def test_contract_constants_match_stage1_contract():
    assert OBJECT_RESOLUTIONS == {"explicit", "inherited", "unknown"}
    assert STATE_VALUES == {"unknown", "planned", "ongoing", "resolved", "failed", "cancelled"}
    assert MODALITY_VALUES == {"certain", "probable", "possible", "required", "desired", "unknown"}
    assert len(CONTEXT_RELATION_LABELS) == 7
    assert EVIDENCE_STRENGTH_VALUES == {"strong", "medium", "weak", "none"}
    assert "conversation_opener" in FRAGMENT_TYPES


def test_gold_projection_is_development_only_and_body_free():
    projection = project_stage1_gold(_full_dataset())
    assert projection.scope == "development"
    assert projection.validation.ok is True
    assert "redacted_text" not in projection.collection("messages")[0]
    assert "fragment_text_redacted" not in projection.collection("fragments")[0]
    assert "claim_text_redacted" not in projection.collection("claims")[0]
    assert "surface_redacted" not in projection.collection("mentions")[0]
    assert "events" not in projection.to_dict()
    assert "presentations" not in projection.to_dict()
    summary = summarize_stage1_projection(projection)
    assert summary["scope"] == "development"
    assert summary["record_counts"]["claims"] == 2
    assert summary["label_counts"]["context_relations.label"] == {"answers": 1}


def test_projection_rejects_frozen_or_mixed_split_without_opening_body():
    frozen = _full_dataset()
    frozen["messages"][0]["split"] = "frozen_test"
    with pytest.raises(ValueError, match="development"):
        project_stage1_gold(frozen)

    mixed = _full_dataset()
    mixed["messages"].append(_message("MESSAGE_SYNTH_002", split="frozen_test"))
    with pytest.raises(ValueError, match="development"):
        project_stage1_gold(mixed)


def test_development_loader_reads_only_known_jsonl_and_projects_body_free(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    development.joinpath("messages.private.jsonl").write_text(
        '{"message_id":"MESSAGE_SYNTH_001","split":"development","redacted_text":"private body"}\n',
        encoding="utf-8",
    )
    development.joinpath("fragments.private.jsonl").write_text(
        '{"fragment_id":"FRAGMENT_SYNTH_001","message_id":"MESSAGE_SYNTH_001","span_start":0,"span_end":1,"fragment_type":"statement"}\n',
        encoding="utf-8",
    )
    # This report must not be opened or interpreted by the Stage 1 loader.
    development.joinpath("p02_error_report.aggregate.private.json").write_text(
        '{"message":"not part of projection"}', encoding="utf-8"
    )
    projection = load_development_gold_directory(development)
    assert projection.collection("messages")[0]["split"] == "development"
    assert "redacted_text" not in projection.collection("messages")[0]
    with pytest.raises(ValueError, match="frozen"):
        load_development_gold_directory(tmp_path / "frozen_test" / "development")


def test_full_synthetic_projection_scores_stage1_fields():
    gold = project_stage1_gold(_full_dataset())
    predictions = project_stage1_predictions(gold.to_dict()["records"])
    score = score_stage1(gold, predictions)
    assert score["scope"] == "development"
    assert score["metric_values"]["fragment_span_f1"] == 1.0
    assert score["metric_values"]["fragment_role_macro_f1"] == 1.0
    assert score["metric_values"]["speaker_accuracy"] == 1.0
    assert score["metric_values"]["mentioned_person_referent_f1"] == 1.0
    assert score["metric_values"]["object_resolution_macro_f1"] == 1.0
    assert score["metric_values"]["state_macro_f1"] == 1.0
    assert score["metric_values"]["modality_macro_f1"] == 1.0
    assert score["metric_values"]["context_relation_label_macro_f1"] == 1.0
    assert score["metric_values"]["context_evidence_strength_macro_f1"] == 1.0
    assert score["metric_values"]["no_reply_answers_recall"] == 1.0
    assert score["metric_values"]["time_only_relation_rate"] == 0.0


def test_missing_prediction_fields_are_na_not_zero():
    dataset = _full_dataset()
    gold = project_stage1_gold(dataset)
    # Keep the claim match key but remove Stage 1 fields from the prediction.
    predicted_claim = {
        "claim_id": "CLAIM_SYNTH_001",
        "message_id": "MESSAGE_SYNTH_001",
        "fragment_id": "FRAGMENT_SYNTH_001",
        "claim_type": "question",
        "target_entity_ids": ["OBJECT_SYNTH_A"],
        "evidence_spans": [{"start": 0, "end": 2}],
    }
    predictions = project_stage1_predictions({"claims": [predicted_claim], "fragments": [], "persons": []})
    score = score_stage1(gold, predictions)
    assert score["metric_values"]["modality_macro_f1"] == NA
    assert score["metric_values"]["claim_state_macro_f1"] == NA
    assert score["metric_values"]["attribution_accuracy"] == NA
    assert score["metric_values"]["fragment_role_macro_f1"] == NA
    assert score["metric_values"]["context_relation_label_macro_f1"] == NA
    assert score["metrics"]["modality_macro_f1"]["reason"] == "prediction_field_absent"


def test_claim_labels_are_scored_after_alignment_not_used_as_identity():
    gold = project_stage1_gold(_full_dataset())
    prediction_records = deepcopy(gold.to_dict()["records"])
    prediction_records["claims"][0]["claim_type"] = "fact"
    predictions = project_stage1_predictions(prediction_records)
    score = score_stage1(gold, predictions)
    assert score["counts"]["claims"]["aligned"] == 2
    assert score["metric_values"]["claim_coverage"] == 1.0
    assert score["metric_values"]["claim_type_macro_f1"] < 1.0


def test_context_strength_and_time_only_guard_are_observable():
    dataset = _full_dataset()
    relation = dataset["context_relations"][0]
    relation["supporting_slot_codes"] = ["time"]
    relation["evidence_strength"] = "weak"
    validation = validate_stage1_projection(dataset, require_development=True)
    assert validation.ok is False
    assert any("time proximity alone" in error for error in validation.errors)

    predictions = project_stage1_predictions({"context_relations": [relation]})
    gold = project_stage1_gold(_full_dataset())
    score = score_stage1(gold, predictions)
    assert score["metric_values"]["time_only_relation_rate"] == 1.0


def test_inherited_object_precision_requires_source_and_referent_agreement():
    gold = project_stage1_gold(_full_dataset())
    prediction_records = deepcopy(gold.to_dict()["records"])
    prediction_records["fragments"][1]["object_inherited_from_id"] = "FRAGMENT_SYNTH_WRONG"
    predictions = project_stage1_predictions(prediction_records)
    score = score_stage1(gold, predictions)
    assert score["metric_values"]["object_inheritance_precision"] == 0.0
