from copy import deepcopy
import json
from pathlib import Path

import pytest

from wechat_bridge.annotation_tools import (
    ANNOTATOR_A,
    ANNOTATOR_B,
    AnnotationFormatError,
    FreezeGateError,
    UnresolvedDisagreementsError,
    AnnotationSet,
    build_adjudication,
    compare_annotation_sets,
    freeze_adjudicated_annotations,
    load_adjudications_jsonl,
    load_annotation_jsonl,
    merge_adjudicated_annotations,
    validate_freeze_readiness,
    write_adjudications_jsonl,
    write_annotation_jsonl,
    write_disagreements_jsonl,
)
from wechat_bridge.semantic_gold import ANNOTATION_GUIDE_VERSION, DATASET_VERSION, SCHEMA_VERSION


def _record(annotator, record_id, record_type, **fields):
    record = {
        "record_id": record_id,
        "record_type": record_type,
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "annotator_id": annotator,
        "annotation_status": "draft",
        "provenance": {
            "source_record_ids": [record_id],
            "created_by": annotator,
            "guide_version": ANNOTATION_GUIDE_VERSION,
            "revision": 1,
        },
    }
    record.update(fields)
    return record


def _annotation_sets():
    a_records = (
        _record(
            ANNOTATOR_A,
            "CLAIM_001",
            "claim",
            claim_id="CLAIM_001",
            message_id="MESSAGE_001",
            speaker_id="PERSON_001",
            claim_type="fact",
            claim_text_redacted="A 侧正文，不应出现在分歧报告",
            target_entity_ids=["ENTITY_001"],
            event_mention_ids=[],
            evidence_spans=[{"start": 0, "end": 1}],
            event_instance_id="INSTANCE_SYNTHETIC_001",
        ),
        _record(
            ANNOTATOR_A,
            "CLAIM_002",
            "claim",
            claim_id="CLAIM_002",
            message_id="MESSAGE_002",
            speaker_id="PERSON_001",
            claim_type="fact",
            claim_text_redacted="共同正文",
            target_entity_ids=["ENTITY_002"],
            event_mention_ids=[],
            evidence_spans=[{"start": 0, "end": 1}],
            event_instance_id="INSTANCE_SYNTHETIC_001",
        ),
        _record(
            ANNOTATOR_A,
            "RELATION_001_A",
            "relation",
            relation_id="RELATION_001_A",
            left_anchor_id="CLAIM_001",
            right_anchor_id="CLAIM_002",
            anchor_type="claim",
            label="same_topic_only",
            supporting_slot_codes=["explicit_shared_instance"],
            conflicting_slot_codes=[],
            evidence_message_ids=["MESSAGE_001", "MESSAGE_002"],
            observable_support_refs=[
                {"type": "claim", "id": "CLAIM_001", "support_code": "explicit_shared_instance", "side": "left"},
                {"type": "claim", "id": "CLAIM_002", "support_code": "explicit_shared_instance", "side": "right"},
            ],
            must_not_link=True,
            must_not_link_reason_codes=["TOPIC_ONLY_EVIDENCE"],
            confidence="medium",
        ),
    )
    b_records = (
        _record(
            ANNOTATOR_B,
            "CLAIM_001",
            "claim",
            claim_id="CLAIM_001",
            message_id="MESSAGE_001",
            speaker_id="PERSON_001",
            claim_type="opinion",
            claim_text_redacted="B 侧正文，也不应出现在分歧报告",
            target_entity_ids=["ENTITY_001"],
            event_mention_ids=[],
            evidence_spans=[{"start": 0, "end": 1}],
            event_instance_id="INSTANCE_SYNTHETIC_001",
        ),
        _record(
            ANNOTATOR_B,
            "CLAIM_002",
            "claim",
            claim_id="CLAIM_002",
            message_id="MESSAGE_002",
            speaker_id="PERSON_001",
            claim_type="fact",
            claim_text_redacted="共同正文",
            target_entity_ids=["ENTITY_002"],
            event_mention_ids=[],
            evidence_spans=[{"start": 0, "end": 1}],
            event_instance_id="INSTANCE_SYNTHETIC_001",
        ),
        # Relation IDs are intentionally different.  The relation must align
        # by canonical claim anchors, not by a locally generated relation ID.
        _record(
            ANNOTATOR_B,
            "RELATION_001_B",
            "relation",
            relation_id="RELATION_001_B",
            left_anchor_id="CLAIM_001",
            right_anchor_id="CLAIM_002",
            anchor_type="claim",
            label="related_event",
            supporting_slot_codes=["explicit_shared_instance"],
            conflicting_slot_codes=[],
            evidence_message_ids=["MESSAGE_001", "MESSAGE_002"],
            observable_support_refs=[
                {"type": "claim", "id": "CLAIM_001", "support_code": "explicit_shared_instance", "side": "left"},
                {"type": "claim", "id": "CLAIM_002", "support_code": "explicit_shared_instance", "side": "right"},
            ],
            must_not_link=True,
            must_not_link_reason_codes=["TOPIC_ONLY_EVIDENCE"],
            confidence="low",
        ),
        _record(
            ANNOTATOR_B,
            "CLAIM_003",
            "claim",
            claim_id="CLAIM_003",
            message_id="MESSAGE_003",
            speaker_id="PERSON_002",
            claim_type="question",
            claim_text_redacted="B 侧新增正文",
            target_entity_ids=["ENTITY_003"],
            event_mention_ids=[],
            evidence_spans=[{"start": 0, "end": 1}],
        ),
    )
    return (
        AnnotationSet(ANNOTATOR_A, ANNOTATION_GUIDE_VERSION, DATASET_VERSION, SCHEMA_VERSION, a_records),
        AnnotationSet(ANNOTATOR_B, ANNOTATION_GUIDE_VERSION, DATASET_VERSION, SCHEMA_VERSION, b_records),
    )


def test_independent_jsonl_round_trip_adds_provenance(tmp_path):
    a, _ = _annotation_sets()
    path = tmp_path / "ann_a.jsonl"
    write_annotation_jsonl(path, a.records, annotator_id=ANNOTATOR_A)
    loaded = load_annotation_jsonl(path, annotator_id=ANNOTATOR_A)
    assert loaded.annotator_id == ANNOTATOR_A
    assert loaded.guide_version == ANNOTATION_GUIDE_VERSION
    assert len(loaded.records) == len(a.records)
    assert loaded.records[0]["provenance"]["created_by"] == ANNOTATOR_A
    assert loaded.records[0]["claim_text_redacted"].startswith("A 侧")


def test_compare_aligns_claims_and_relations_and_report_contains_no正文(tmp_path):
    a, b = _annotation_sets()
    comparison = compare_annotation_sets(a, b)
    assert comparison.to_dict()["aligned_pair_count"] == 3
    assert comparison.to_dict()["b_only_count"] == 1
    assert {item.record_type for item in comparison.disagreements} == {"claim", "relation", "claim"}
    assert any(item.field == "claim_type" for item in comparison.disagreements)
    relation = next(item for item in comparison.disagreements if item.record_type == "relation" and item.field == "label")
    assert relation.a_record_id == "RELATION_001_A"
    assert relation.b_record_id == "RELATION_001_B"
    # Both sides asserted MNL, so a mandatory adjudication is emitted even
    # though their MNL boolean itself is equal.
    assert any(
        item.record_type == "relation"
        and item.field == "must_not_link"
        and item.kind == "mandatory_adjudication"
        for item in comparison.disagreements
    )
    path = tmp_path / "disagreements.jsonl"
    write_disagreements_jsonl(path, comparison)
    serialized = path.read_text(encoding="utf-8")
    assert "A 侧正文" not in serialized
    assert "B 侧正文" not in serialized
    assert "B 侧新增正文" not in serialized
    rows = [json.loads(line) for line in serialized.splitlines()]
    assert rows
    assert all(row["provenance"]["guide_version"] == ANNOTATION_GUIDE_VERSION for row in rows)
    assert all("value_omitted" in row["annotator_a"] for row in rows if row["field"] == "claim_text_redacted")


def test_alignment_crosswalk_handles_locally_different_claim_ids():
    a, b = _annotation_sets()
    a_claim = next(item for item in a.records if item.get("claim_id") == "CLAIM_001")
    b_claim = next(item for item in b.records if item.get("claim_id") == "CLAIM_001")
    a_claim["claim_id"] = "CLAIM_A_LOCAL"
    a_claim["record_id"] = "CLAIM_A_LOCAL"
    b_claim["claim_id"] = "CLAIM_B_LOCAL"
    b_claim["record_id"] = "CLAIM_B_LOCAL"
    a_relation = next(item for item in a.records if item.get("record_type") == "relation")
    b_relation = next(item for item in b.records if item.get("record_type") == "relation")
    a_relation["left_anchor_id"] = "CLAIM_A_LOCAL"
    b_relation["left_anchor_id"] = "CLAIM_B_LOCAL"
    comparison = compare_annotation_sets(a, b)
    assert not any(item.field == "claim_id" for item in comparison.disagreements)
    assert not any(item.field == "left_anchor_id" for item in comparison.disagreements)
    assert any(item.field == "claim_type" for item in comparison.disagreements)


def test_unresolved_disagreements_are_a_hard_merge_and_freeze_gate(tmp_path):
    a, b = _annotation_sets()
    comparison = compare_annotation_sets(a, b)
    assert not validate_freeze_readiness(comparison, []).ok
    with pytest.raises(UnresolvedDisagreementsError):
        merge_adjudicated_annotations(a, b, comparison, [])


def test_adjudications_round_trip_and_merge_sets_final_values(tmp_path):
    a, b = _annotation_sets()
    comparison = compare_annotation_sets(a, b)
    adjudications = []
    for disagreement in comparison.disagreements:
        decision = "accept_b" if disagreement.kind == "missing_in_a" else "accept_a"
        adjudications.append(
            build_adjudication(
                disagreement,
                decision=decision,
                adjudicator_id="ADJ_1",
                reason_codes=["SYNTHETIC_TEST"],
                evidence_ids=[disagreement.anchor_id],
            )
        )
    adjudication_path = tmp_path / "adjudications.jsonl"
    write_adjudications_jsonl(adjudication_path, adjudications, adjudicator_id="ADJ_1")
    loaded_adjudications = load_adjudications_jsonl(adjudication_path, adjudicator_id="ADJ_1")
    result = merge_adjudicated_annotations(a, b, comparison, loaded_adjudications, adjudicator_id="ADJ_1")
    assert result.unresolved_disagreement_ids == ()
    assert result.validation.ok
    claim = next(item for item in result.records if item.get("claim_id") == "CLAIM_001")
    assert claim["claim_type"] == "fact"
    assert claim["annotation_status"] == "adjudicated"
    assert claim["provenance"]["created_by"] == "ADJ_1"
    assert claim["provenance"]["guide_version"] == ANNOTATION_GUIDE_VERSION
    relation = next(item for item in result.records if item.get("relation_id") == "RELATION_001_A")
    assert relation["label"] == "same_topic_only"
    assert relation["adjudication_id"]
    assert any(item.get("claim_id") == "CLAIM_003" for item in result.records)


def test_freeze_marks_only_a_fully_adjudicated_merge(tmp_path):
    a, b = _annotation_sets()
    comparison = compare_annotation_sets(a, b)
    adjudications = [
        build_adjudication(
            item,
            decision="accept_b" if item.kind == "missing_in_a" else "accept_a",
        )
        for item in comparison.disagreements
    ]
    merged = merge_adjudicated_annotations(a, b, comparison, adjudications)
    frozen = freeze_adjudicated_annotations(merged, comparison)
    assert frozen
    assert all(item["annotation_status"] == "frozen" for item in frozen)
    assert all(item["provenance"]["revision"] >= 3 for item in frozen)


def test_same_event_mnl_requires_explicit_override_reason():
    a, b = _annotation_sets()
    for records in (a.records, b.records):
        relation = next(item for item in records if item.get("record_type") == "relation")
        relation["label"] = "same_event"
    comparison = compare_annotation_sets(a, b)
    adjudications = [
        build_adjudication(
            item,
            decision="accept_b" if item.kind == "missing_in_a" else "accept_a",
        )
        for item in comparison.disagreements
    ]
    merged = merge_adjudicated_annotations(a, b, comparison, adjudications)
    assert not merged.validation.ok
    with pytest.raises(FreezeGateError):
        freeze_adjudicated_annotations(merged, comparison)
    adjudications = [
        build_adjudication(
            item,
            decision="accept_b" if item.kind == "missing_in_a" else "accept_a",
            override_reason="synthetic evidence confirms one event instance",
        )
        for item in comparison.disagreements
    ]
    merged = merge_adjudicated_annotations(a, b, comparison, adjudications)
    assert merged.validation.ok
    assert freeze_adjudicated_annotations(merged, comparison)


def test_incompatible_guides_and_duplicate_ids_fail_closed(tmp_path):
    a, b = _annotation_sets()
    bad_b = deepcopy(b.records[0])
    bad_b["provenance"]["guide_version"] = "guide-v-next"
    bad_b["record_id"] = "CLAIM_BAD"
    with pytest.raises(AnnotationFormatError):
        compare_annotation_sets(
            a,
            AnnotationSet(
                ANNOTATOR_B,
                "guide-v-next",
                DATASET_VERSION,
                SCHEMA_VERSION,
                (bad_b,),
            ),
        )
    duplicate = deepcopy(a.records[0])
    with pytest.raises(AnnotationFormatError):
        write_annotation_jsonl(tmp_path / "duplicate.jsonl", [a.records[0], duplicate], annotator_id=ANNOTATOR_A)
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text(json.dumps({"record_id": "C1"}) + "\n", encoding="utf-8")
    with pytest.raises(AnnotationFormatError):
        load_annotation_jsonl(malformed, annotator_id=ANNOTATOR_A)


def test_report_rejects_forbidden_fields_when_reloaded(tmp_path):
    a, b = _annotation_sets()
    comparison = compare_annotation_sets(a, b)
    path = tmp_path / "disagreements.jsonl"
    write_disagreements_jsonl(path, comparison)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["raw_text"] = "private"
    path.write_text("\n".join(json.dumps(item) for item in rows) + "\n", encoding="utf-8")
    from wechat_bridge.annotation_tools import read_disagreements_jsonl

    with pytest.raises(AnnotationFormatError):
        read_disagreements_jsonl(path)
