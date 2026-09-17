from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from wechat_bridge.semantic_gold import (
    ANNOTATION_GUIDE_VERSION, DATASET_VERSION, DEFAULT_PRIVATE_OUTPUT_DIR,
    DEFAULT_RELEASE_DIR, DEFAULT_WORKING_DIR, LOCAL_DAY, PRIVATE_JSONL_FILES,
    PRIVATE_ROOT, SCHEMA_VERSION, WINDOW_END_LOCAL, WINDOW_END_UTC,
    WINDOW_START_LOCAL, WINDOW_START_UTC, build_export_manifest_dry_run,
    contract_schema, export_private_pre_redaction_seed,
    inspect_source_coverage_read_only, open_source_database_read_only,
    read_source_messages_read_only, score_gold_predictions,
    semantic_result_to_evaluation_payload, validate_contract_dataset,
    validate_contract_directory,
)
from wechat_bridge.semantic_pipeline import run_semantic_pipeline


def _create_synthetic_archive(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE messages (message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, "
            "chat_name TEXT, sender_id TEXT, sender_name TEXT, content TEXT NOT NULL, "
            "timestamp TEXT NOT NULL, message_type TEXT, is_self INTEGER, is_group INTEGER)"
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("before", "chat-a", "合成私聊", "a", "合成人甲", "窗口前", "2026-08-24T15:59:59+00:00", "text", 0, 0),
                ("start", "chat-a", "合成私聊", "a", "合成人甲",
                 "合成人甲说 GPT 重置；邮箱 test@example.com，电话 13800138000，文件 C:\\Users\\Fake\\a.txt",
                 WINDOW_START_UTC, "text", 0, 0),
                ("inside", "chat-b", "合成群", "wxid_synthetic", "合成人乙",
                 "GitHub 注册邮件失败 https://github.com/private?token=synthetic @合成人丙，费用100元",
                 "2026-08-25T12:00:00+08:00", "text", 0, 1),
                ("end", "chat-a", "合成私聊", "a", "合成人甲", "窗口后", WINDOW_END_UTC, "text", 0, 0),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _common(record_id, source_ids=()):
    return {
        "schema_version": SCHEMA_VERSION, "dataset_version": DATASET_VERSION,
        "record_id": record_id, "annotation_status": "adjudicated",
        "provenance": {"source_record_ids": list(source_ids), "created_by": "ADJ_1",
                       "guide_version": ANNOTATION_GUIDE_VERSION, "revision": 1},
    }


def _synthetic_contract_and_predictions():
    texts = [
        ("MESSAGE_000001", "CHAT_001", "PERSON_001", "GPT 最近又在不停重置。", "group"),
        ("MESSAGE_000002", "CHAT_002", "PERSON_002", "中转站没有性价比，价格太贵。", "group"),
        ("MESSAGE_000003", "CHAT_003", "PERSON_003", "Codex 最近又在不停重置。", "direct"),
    ]
    messages = []
    for index, (message_id, chat_id, speaker_id, text, chat_type) in enumerate(texts):
        messages.append(
            {
                **_common(message_id, ("SYNTHETIC_%d" % (index + 1),)),
                "message_id": message_id, "account_id": "ACCOUNT_001", "chat_id": chat_id,
                "chat_type": chat_type, "speaker_id": speaker_id, "direction": "inbound",
                "message_type": "text", "local_day": LOCAL_DAY,
                "time_offset_seconds": index * 3600, "time_bucket": "early",
                "sequence_in_chat": 0, "reply_to_message_id": None, "redacted_text": text,
                "redaction_types": [], "media_state": "none", "source_mode": "history",
                "context_message_ids": [], "split": "development",
            }
        )
    result = run_semantic_pipeline(
        [
            {"message_id": item["message_id"], "chat_id": item["chat_id"],
             "sender_id": item["speaker_id"], "sender_name": item["speaker_id"],
             "content": item["redacted_text"],
             "timestamp": "2026-08-24T%02d:00:00+00:00" % (16 + index),
             "is_group": item["chat_type"] == "group", "is_self": False}
            for index, item in enumerate(messages)
        ]
    )
    predictions = semantic_result_to_evaluation_payload(result)

    mentions, mention_ids = [], {}
    for index, item in enumerate(result.mentions, 1):
        mention_id = "MENTION_%06d" % index
        mention_ids[item.mention_id] = mention_id
        mentions.append(
            {**_common(mention_id, (item.message_id,)), "mention_id": mention_id,
             "message_id": item.message_id, "mention_type": item.mention_type,
             "span_start": item.span_start, "span_end": item.span_end,
             "surface_redacted": item.evidence_text, "normalized_id": item.normalized_id,
             "normalized_type": item.mention_type, "attributes": {}, "certainty": "reported",
             "annotator_notes": None}
        )
    claims, claim_ids = [], {}
    for index, item in enumerate(result.claims, 1):
        claim_id = "CLAIM_%06d" % index
        claim_ids[item.claim_id] = claim_id
        claims.append(
            {**_common(claim_id, (item.message_id,)), "claim_id": claim_id,
             "message_id": item.message_id, "speaker_id": item.speaker_id,
             "claim_type": item.claim_type, "claim_text_redacted": item.claim_text,
             "target_entity_ids": list(item.target_entity_ids),
             "event_mention_ids": [mention_ids[value] for value in item.event_mention_ids],
             "evidence_spans": [{"start": item.evidence_span.span_start, "end": item.evidence_span.span_end}],
             "stance": "neutral", "polarity": "negative" if item.stance_or_polarity == "negative" else "neutral",
             "modality": "unknown", "status": "ongoing" if item.status_or_modality == "recurring" else "reported",
             "attribution": "direct", "timestamp_message_id": item.message_id,
             "context_message_ids": []}
        )
    relations = []
    for index, item in enumerate(result.pair_decisions, 1):
        relation_id = "RELATION_%06d" % index
        left, right = sorted((claim_ids[item.left_claim_id], claim_ids[item.right_claim_id]))
        mnl = item.relation in {"same_topic_only", "unrelated"}
        observable_support_refs = []
        if item.relation == "same_event":
            left_claim = next(claim for claim in claims if claim["claim_id"] == left)
            right_claim = next(claim for claim in claims if claim["claim_id"] == right)
            # The synthetic same-event pair is intentionally same-message.
            # Bind every declared support slot to a typed claim/mention/message
            # ref so the public graph gate exercises the observable contract.
            observable_support_refs = [
                {"type": "mention", "id": left_claim["event_mention_ids"][0],
                 "support_code": "core_entity", "side": "left"},
                {"type": "mention", "id": right_claim["event_mention_ids"][0],
                 "support_code": "core_entity", "side": "right"},
                {"type": "mention", "id": left_claim["event_mention_ids"][-1],
                 "support_code": "action", "side": "left"},
                {"type": "mention", "id": right_claim["event_mention_ids"][-1],
                 "support_code": "action", "side": "right"},
                {"type": "claim", "id": left, "support_code": "request", "side": "left"},
                {"type": "claim", "id": right, "support_code": "request", "side": "right"},
                {"type": "message", "id": item.source_message_ids[0], "support_code": "same_message"},
                {"type": "claim", "id": left, "support_code": "time_window", "side": "left"},
            ]
        relations.append(
            {**_common(relation_id, (left, right)), "relation_id": relation_id,
             "left_anchor_id": left, "right_anchor_id": right, "anchor_type": "claim",
             "label": item.relation, "supporting_slot_codes": list(item.supporting_slots),
             "conflicting_slot_codes": list(item.conflicting_slots),
             "evidence_message_ids": list(item.source_message_ids), "must_not_link": mnl,
             "must_not_link_reason_codes": list(item.hard_conflict_reasons) if mnl else [],
             "confidence": "high", "annotator_a_label": item.relation,
             "annotator_b_label": item.relation, "adjudication_id": None,
             "observable_support_refs": observable_support_refs}
        )
    clusters, cluster_ids = [], {}
    for index, item in enumerate(result.events, 1):
        cluster_id = "GOLD_CLUSTER_%06d" % index
        cluster_ids[item.event_id] = cluster_id
        event_claims = [claim_ids[value] for value in item.claim_ids]
        clusters.append(
            {**_common(cluster_id, tuple(event_claims)), "cluster_id": cluster_id,
             "cluster_type": "event", "event_type": item.event_type,
             "core_entity_ids": list(item.core_entity_ids), "action_types": list(item.actions),
             "intent_types": list(item.requests), "state_sequence": list(item.statuses),
             "mention_ids": [mention_ids[value] for value in item.mention_ids],
             "claim_ids": event_claims, "member_message_ids": list(item.source_message_ids),
             "relation_ids": [], "must_not_link_checked": True,
             "start_message_id": item.source_message_ids[0], "end_message_id": item.source_message_ids[-1],
             "topic_family_ids": ["TOPIC_AI"], "summary_of_boundary_redacted": "合成事件边界",
             "uncertainties": list(item.uncertainties)}
        )
    presentations = []
    for index, item in enumerate(result.presentations, 1):
        presentation_id = "PRESENTATION_%06d" % index
        source_claims = [claim_ids[value] for value in item.supported_claim_ids]
        presentations.append(
            {**_common(presentation_id, tuple(source_claims)), "presentation_id": presentation_id,
             "presentation_type": "event_card", "source_cluster_ids": [cluster_ids[item.event_id]],
             "source_claim_ids": source_claims, "title_redacted": item.title,
             "sentence_units": [
                 {"text_redacted": sentence.text,
                  "claim_ids": [claim_ids[value] for value in sentence.claim_ids],
                  "message_ids": list(sentence.message_ids)} for sentence in item.sentences],
             "participant_ids": [], "fact_claim_ids": [], "opinion_claim_ids": [],
             "question_claim_ids": [], "status": "unknown", "uncertainties": [],
             "detail_policy": "summary_and_evidence", "expected_order_group": None,
             "must_remain_separate_from": [], "display_decision_reason_codes": ["SYNTHETIC"]}
        )
    label_counts = {}
    for item in relations:
        label_counts[item["label"]] = label_counts.get(item["label"], 0) + 1
    manifest = {
        "dataset_id": "synthetic-contract-v1", "dataset_version": DATASET_VERSION,
        "status": "adjudicated", "workflow_state": "adjudicated", "scope_local_day": LOCAL_DAY,
        "schema_version": SCHEMA_VERSION, "annotation_guide_version": ANNOTATION_GUIDE_VERSION,
        "redaction_policy_version": "synthetic-v1", "pseudonym_key_id": "SYNTHETIC_KEY",
        "sampling_seed": 42, "sampling_strata_and_targets": {"data_origin": "synthetic"},
        "coverage_counts": {"window_message_count": 3, "reply_metadata_state": "absent"},
        "coverage_shortfalls": ["REPLY_METADATA_ABSENT_REPLY_GOLD_PROHIBITED"],
        "split_policy": {"type": "cluster"}, "source_snapshot_fingerprint_hmac": "SYNTHETIC",
        "file_sha256": {}, "record_counts_by_file": {}, "label_counts": {"relations": label_counts},
        "must_not_link_count": sum(item["must_not_link"] for item in relations),
        "double_annotation_coverage": {"mentions": 1.0, "claims": 1.0, "relations": 1.0, "presentations": 1.0},
        "adjudication_count": 0, "privacy_scan_status": "synthetic_no_private_data",
        "created_at": "2026-08-26T00:00:00+00:00", "frozen_at": None, "supersedes": None,
        "known_limitations": ["synthetic only"],
        "version_lineage": {"parent_dataset_version": None, "change_type": "synthetic"},
        "release_eligible": False, "external_sharing_allowed": True, "data_origin": "synthetic",
        "window": {"timezone": "Asia/Shanghai", "start_local": WINDOW_START_LOCAL,
                   "end_local": WINDOW_END_LOCAL, "start_utc": WINDOW_START_UTC,
                   "end_utc": WINDOW_END_UTC, "interval": "half_open"},
    }
    return ({"manifest": manifest, "messages": messages, "mentions": mentions,
             "claims": claims, "relations": relations, "clusters": clusters,
             "presentations": presentations, "adjudications": []}, predictions)


def test_contract_paths_schema_and_defense_in_depth_gitignore():
    assert PRIVATE_ROOT.as_posix() == "data/private/gold_standard/2026-08-25"
    assert DEFAULT_WORKING_DIR == PRIVATE_ROOT / "working"
    assert DEFAULT_PRIVATE_OUTPUT_DIR == DEFAULT_WORKING_DIR
    assert DEFAULT_RELEASE_DIR == PRIVATE_ROOT / "releases" / "v1"
    assert contract_schema()["release_files"] == ["manifest.json"] + list(PRIVATE_JSONL_FILES)
    ignored = Path(".gitignore").read_text(encoding="utf-8")
    for pattern in ("data/", "private_fixtures/", "gold-standard-private/", "*.private.jsonl"):
        assert pattern in ignored
    assert contract_schema()["relation_observable_support_refs"]["contract_version"] == "same_event_observable_support_v1"


def test_frozen_beijing_window_is_exact_half_open_interval():
    assert WINDOW_START_LOCAL == "2026-08-25T00:00:00+08:00"
    assert WINDOW_END_LOCAL == "2026-08-26T00:00:00+08:00"
    assert datetime.fromisoformat(WINDOW_START_LOCAL).astimezone(timezone.utc).isoformat() == WINDOW_START_UTC
    assert datetime.fromisoformat(WINDOW_END_LOCAL).astimezone(timezone.utc).isoformat() == WINDOW_END_UTC


def test_read_only_wal_aware_reader_and_content_free_dry_run(tmp_path):
    database = tmp_path / "synthetic.sqlite"
    _create_synthetic_archive(database)
    assert [item["message_id"] for item in read_source_messages_read_only(database)] == ["start", "inside"]
    with open_source_database_read_only(database) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE messages SET content='forbidden'")
    coverage = inspect_source_coverage_read_only(database)
    assert coverage["window_message_count"] == 2
    assert coverage["group_message_count"] == coverage["private_message_count"] == 1
    assert coverage["reply_metadata_state"] == "absent"
    dry_run = build_export_manifest_dry_run(database, created_at="2026-08-26T00:00:00+00:00")
    assert dry_run["content_read"] is False
    assert dry_run["ready_for_frozen_release"] is False
    assert dry_run["ready_for_reply_gold"] is False


def test_working_seed_uses_contract_jsonl_relative_time_and_privacy_gate(tmp_path):
    database = tmp_path / "synthetic.sqlite"
    _create_synthetic_archive(database)
    output = tmp_path / "working"
    result = export_private_pre_redaction_seed(
        database, pseudonym_key="synthetic-private-key-material",
        pseudonym_key_id="SYNTHETIC_KEY_V1", output_directory=output,
        created_at="2026-08-26T00:00:00+00:00",
    )
    assert result.workflow_state == "privacy_review_required"
    assert result.validation.ok is True
    assert {Path(value).name for value in result.file_paths} == set(PRIVATE_JSONL_FILES)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "draft"
    assert manifest["release_eligible"] is False
    assert manifest["external_sharing_allowed"] is False
    assert manifest["privacy_scan_status"] == "pending_manual_review"
    assert manifest["pseudonym_key_id"] == "SYNTHETIC_KEY_V1"
    assert "synthetic-private-key-material" not in json.dumps(manifest)
    assert "REPLY_METADATA_ABSENT_REPLY_GOLD_PROHIBITED" in manifest["coverage_shortfalls"]
    assert set(manifest["file_sha256"]) == set(PRIVATE_JSONL_FILES)
    assert validate_contract_directory(output).ok is True
    records = [json.loads(line) for line in (output / "messages.private.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all("timestamp" not in item for item in records)
    assert all(item["local_day"] == LOCAL_DAY and item["reply_to_message_id"] is None for item in records)
    assert [item["time_offset_seconds"] for item in records] == [0, 43200]
    serialized = json.dumps(records, ensure_ascii=False)
    for private_value in ("start", "inside", "chat-a", "chat-b", "合成人甲", "合成人乙",
                          "test@example.com", "13800138000", "wxid_synthetic", "C:\\Users\\Fake",
                          "github.com/private", "@合成人丙", "100元"):
        assert private_value not in serialized
    assert all(value in serialized for value in ("[EMAIL_", "[PHONE_", "[FILE_", "[URL_", "[AMOUNT:BUCKET_PENDING]"))
    with pytest.raises(FileExistsError):
        export_private_pre_redaction_seed(
            database, pseudonym_key="synthetic-private-key-material",
            pseudonym_key_id="SYNTHETIC_KEY_V1", output_directory=output)


def test_seed_cannot_be_frozen_and_absolute_timestamp_is_forbidden():
    dataset, _ = _synthetic_contract_and_predictions()
    assert validate_contract_dataset(dataset).ok is True
    seed = deepcopy(dataset)
    seed["manifest"].update({"status": "frozen", "workflow_state": "privacy_review_required",
                             "release_eligible": True, "frozen_at": "2026-08-26T00:00:00+00:00"})
    validation = validate_contract_dataset(seed)
    assert any("privacy_review_required seed cannot be frozen" in value for value in validation.errors)
    absolute = deepcopy(dataset)
    absolute["messages"][0]["timestamp"] = "2026-08-25T01:00:00+08:00"
    assert any("forbidden field" in value and "timestamp" in value
               for value in validate_contract_dataset(absolute).errors)


def test_contract_validator_checks_mention_evidence_and_mnl_clusters():
    dataset, _ = _synthetic_contract_and_predictions()
    invalid = deepcopy(dataset)
    invalid["mentions"][0]["surface_redacted"] = "不匹配"
    assert any("span does not match" in value for value in validate_contract_dataset(invalid).errors)
    relation = next(item for item in dataset["relations"] if item["must_not_link"])
    invalid = deepcopy(dataset)
    invalid["clusters"][0]["claim_ids"] = [relation["left_anchor_id"], relation["right_anchor_id"]]
    assert any("unoverridden MNL" in value for value in validate_contract_dataset(invalid).errors)


@pytest.mark.parametrize(
    ("collection", "typed_field"),
    [
        ("messages", "message_id"),
        ("mentions", "mention_id"),
        ("claims", "claim_id"),
        ("relations", "relation_id"),
        ("clusters", "cluster_id"),
        ("presentations", "presentation_id"),
    ],
)
def test_contract_validator_rejects_duplicate_typed_ids(collection, typed_field):
    dataset, _ = _synthetic_contract_and_predictions()
    duplicate = deepcopy(dataset[collection][0])
    duplicate["record_id"] = "%s_DUPLICATE_RECORD" % typed_field
    dataset[collection].append(duplicate)
    result = validate_contract_dataset(dataset)
    assert not result.ok
    assert any("duplicate %s ID" % typed_field[:-3] in value for value in result.errors)


def test_contract_validator_rejects_duplicate_adjudication_ids():
    dataset, _ = _synthetic_contract_and_predictions()
    first = {**_common("ADJ_SYNTHETIC_1"), "adjudication_id": "ADJ_SYNTHETIC_1"}
    second = {**_common("ADJ_SYNTHETIC_2"), "adjudication_id": "ADJ_SYNTHETIC_1"}
    dataset["adjudications"] = [first, second]
    result = validate_contract_dataset(dataset)
    assert not result.ok
    assert any("duplicate adjudication ID" in value for value in result.errors)


def test_contract_validator_rejects_typed_anchor_and_evidence_foreign_keys():
    dataset, _ = _synthetic_contract_and_predictions()
    relation = dataset["relations"][0]
    relation["left_anchor_id"] = dataset["mentions"][0]["mention_id"]
    result = validate_contract_dataset(dataset)
    assert not result.ok
    assert any("left_anchor_id references unknown claims" in value for value in result.errors)

    dataset, _ = _synthetic_contract_and_predictions()
    relation = dataset["relations"][0]
    relation["evidence_message_ids"] = ["MESSAGE_UNKNOWN"]
    result = validate_contract_dataset(dataset)
    assert not result.ok
    assert any("evidence_message_ids references unknown messages" in value for value in result.errors)


def test_contract_validator_requires_typed_observable_support_for_same_event():
    dataset, _ = _synthetic_contract_and_predictions()
    relation = next(item for item in dataset["relations"] if item["label"] == "same_event")
    relation.pop("observable_support_refs")
    result = validate_contract_dataset(dataset)
    assert not result.ok
    assert any("same_event requires observable_support_refs" in value for value in result.errors)

    dataset, _ = _synthetic_contract_and_predictions()
    relation = next(item for item in dataset["relations"] if item["label"] == "same_event")
    relation["observable_support_refs"][0] = "MENTION_000001"
    result = validate_contract_dataset(dataset)
    assert not result.ok
    assert any("must be a typed object" in value for value in result.errors)


def test_public_split_coverage_omits_relation_labels_and_label_derived_counts():
    from tests.build_p01_split import _public_coverage

    rows = {name: [] for name in ("messages", "mentions", "claims", "relations", "clusters", "presentations", "adjudications")}
    rows["messages"] = [{"message_id": "MESSAGE_SYNTHETIC", "chat_id": "CHAT_SYNTHETIC", "chat_type": "group", "time_bucket": "early"}]
    rows["relations"] = [{"label": "same_event", "must_not_link": True}]
    public = _public_coverage(rows)
    assert "relation_labels" not in public
    assert "must_not_link_count" not in public
    assert "same_event" not in json.dumps(public, ensure_ascii=False)


def test_cluster_and_presentation_scoring_aligns_claim_sets_not_cluster_ids():
    dataset, predictions = _synthetic_contract_and_predictions()
    assert all(item["cluster_id"].startswith("GOLD_CLUSTER_") for item in dataset["clusters"])
    assert all(item["prediction_cluster_id"].startswith("event:") for item in predictions["clusters"])
    score = score_gold_predictions(dataset, predictions)
    assert score["candidate_coverage"]["gold_coverage"] == 1.0
    assert score["candidate_coverage"]["predicted_in_gold_rate"] == 1.0
    assert score["cluster"]["pairwise_f1"] == 1.0
    assert score["cluster"]["b_cubed_f1"] == 1.0
    assert score["presentation"]["exact_evidence_rate"] == 1.0
    assert all(gold != predicted for gold, predicted in score["presentation"]["cluster_alignment"].items()
               if predicted is not None)


def test_scorer_reports_missing_extra_candidates_and_catastrophic_merge():
    dataset, predictions = _synthetic_contract_and_predictions()
    broken = deepcopy(predictions)
    broken["relations"] = broken["relations"][:-1]
    broken["claims"].append(
        {"prediction_claim_id": "prediction:extra", "message_id": "MESSAGE_EXTRA",
         "speaker_id": "PERSON_EXTRA", "claim_type": "fact",
         "target_entity_ids": ["service:extra"], "evidence_spans": [{"start": 0, "end": 1}]}
    )
    broken["relations"].append(
        {"left_anchor_id": broken["claims"][0]["prediction_claim_id"],
         "right_anchor_id": "prediction:extra", "label": "unrelated"}
    )
    score = score_gold_predictions(dataset, broken)
    assert score["candidate_coverage"]["missing_predicted_count"] == 1
    assert score["candidate_coverage"]["extra_predicted_count"] == 1
    merged = deepcopy(predictions)
    merged["clusters"] = [
        {"prediction_cluster_id": "event:wrong_merge",
         "claim_ids": [item["prediction_claim_id"] for item in merged["claims"]]}
    ]
    merged_score = score_gold_predictions(dataset, merged)
    assert merged_score["cluster"]["overmerge_count"] > 0
    assert merged_score["must_not_link"]["violation_count"] > 0


def test_b_cubed_penalizes_missing_gold_cluster_member_and_reports_claim_coverage():
    dataset, predictions = _synthetic_contract_and_predictions()
    gold_claim_ids = [item["claim_id"] for item in dataset["claims"][:2]]
    remaining_clusters = [
        item for item in dataset["clusters"]
        if not set(item["claim_ids"]) & set(gold_claim_ids)
    ]
    dataset["clusters"] = [
        {
            **deepcopy(dataset["clusters"][0]),
            "cluster_id": "GOLD_CLUSTER_COMBINED",
            "record_id": "GOLD_CLUSTER_COMBINED",
            "claim_ids": gold_claim_ids,
            "member_message_ids": [item["message_id"] for item in dataset["claims"][:2]],
        },
        *remaining_clusters,
    ]
    # This fixture is about metric behavior; remove pair constraints that would
    # independently reject the deliberately combined synthetic cluster.
    dataset["relations"] = []
    dataset["presentations"] = []
    dataset["manifest"]["label_counts"]["relations"] = {}
    dataset["manifest"]["must_not_link_count"] = 0
    predicted_claim_ids = [item["prediction_claim_id"] for item in predictions["claims"][:2]]
    remaining_predicted_clusters = [
        item for item in predictions["clusters"]
        if not set(item["claim_ids"]) & set(predicted_claim_ids)
    ]
    predictions["clusters"] = [
        {"prediction_cluster_id": "event:combined", "claim_ids": predicted_claim_ids},
        *remaining_predicted_clusters,
    ]
    complete = score_gold_predictions(dataset, predictions)
    missing = deepcopy(predictions)
    missing_id = missing["claims"][1]["prediction_claim_id"]
    missing["claims"] = [item for item in missing["claims"] if item["prediction_claim_id"] != missing_id]
    missing["relations"] = [
        item for item in missing["relations"]
        if missing_id not in {item["left_anchor_id"], item["right_anchor_id"]}
    ]
    missing["clusters"] = [
        {**item, "claim_ids": [value for value in item["claim_ids"] if value != missing_id]}
        for item in missing["clusters"]
    ]
    missing["presentations"] = [
        {**item, "source_claim_ids": [value for value in item["source_claim_ids"] if value != missing_id]}
        for item in missing["presentations"]
    ]
    score = score_gold_predictions(dataset, missing)
    assert score["claim"]["gold_coverage"] < 1.0
    assert score["cluster"]["b_cubed_recall"] < complete["cluster"]["b_cubed_recall"]
    assert score["cluster"]["b_cubed_evaluation_universe"] == \
        "gold_claims_missing_predictions_as_singletons"


@pytest.mark.parametrize("side", ["gold", "predicted"])
def test_scorer_rejects_ambiguous_duplicate_claim_match_keys(side):
    dataset, predictions = _synthetic_contract_and_predictions()
    if side == "gold":
        duplicate = deepcopy(dataset["claims"][0])
        duplicate["claim_id"] = "CLAIM_DUPLICATE_MATCH_KEY"
        duplicate["record_id"] = "CLAIM_DUPLICATE_MATCH_KEY"
        dataset["claims"].append(duplicate)
    else:
        duplicate = deepcopy(predictions["claims"][0])
        duplicate["prediction_claim_id"] = "prediction:duplicate-match-key"
        predictions["claims"].append(duplicate)
    with pytest.raises(ValueError, match="ambiguous %s claim match keys" % side):
        score_gold_predictions(dataset, predictions)
