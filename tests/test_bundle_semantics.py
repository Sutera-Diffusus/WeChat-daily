import json

import pytest

from wechat_bridge.bundle_semantics import (
    BUNDLE_FIELDS,
    BUNDLE_SCHEMA_VERSION,
    BundleSemanticEncoder,
    BundleSemanticPipeline,
    BundleSchemaError,
    SparseStructuralIndex,
    VersionedBundleCache,
    bundle_schema,
    empty_bundle,
    stable_hash,
    validate_bundle,
)


def _evidence(evidence_id, message_id="m1", field="object"):
    return {
        "evidence_id": evidence_id,
        "message_id": message_id,
        "span": {"start": 0, "end": 3},
        "field": field,
        "kind": "span",
    }


def _bundle(bundle_id, *, chat_id="chat-1", object_id="object-1", state="ongoing"):
    bundle = empty_bundle(bundle_id, [bundle_id + "-message"], chat_id=chat_id, status="complete", source="synthetic")
    evidence = [
        _evidence(bundle_id + "-speaker", bundle_id + "-message", "speaker"),
        _evidence(bundle_id + "-object", bundle_id + "-message", "object"),
        _evidence(bundle_id + "-claim-type", bundle_id + "-message", "claim_type"),
        _evidence(bundle_id + "-state", bundle_id + "-message", "state"),
        _evidence(bundle_id + "-modality", bundle_id + "-message", "modality"),
    ]
    bundle["speaker"] = {
        "id": "person-1",
        "type": "person",
        "role": "speaker",
        "resolution": "explicit",
        "evidence_ids": [bundle_id + "-speaker"],
    }
    bundle["object"] = [
        {
            "id": object_id,
            "type": "object",
            "role": "object",
            "resolution": "explicit",
            "evidence_ids": [bundle_id + "-object"],
        }
    ]
    bundle["claim_type"] = "fact"
    bundle["state"] = state
    bundle["modality"] = "certain"
    bundle["evidence"] = evidence
    bundle["metadata"]["keywords"] = [object_id, "service"]
    return bundle


class FakeModel:
    def __init__(self, response=None, failures=0):
        self.response = response
        self.failures = failures
        self.encode_requests = []
        self.judge_requests = []

    def encode_bundle(self, request):
        self.encode_requests.append(request)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic provider failure")
        return self.response

    def judge_pair(self, request):
        self.judge_requests.append(request)
        return {
            "label": "answers",
            "strength": "medium",
            "evidence_ids": ["left-object"],
            "status": "complete",
            "uncertainties": [],
        }


def test_fixed_schema_has_all_required_bundle_slots_and_no_network_default():
    schema = bundle_schema()
    assert schema["required"] == list(BUNDLE_FIELDS)
    assert set(BUNDLE_FIELDS) >= {
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
    }
    outcome = BundleSemanticEncoder().encode(
        [{"message_id": "m1", "chat_id": "chat-1", "speaker_id": "person-1", "content": "service failed"}],
        bundle_id="fallback-bundle",
    )
    assert outcome.status == "fallback"
    assert outcome.validation.ok is True
    assert outcome.bundle["metadata"]["source"] == "contextual_fragments_fallback"
    assert outcome.stats["model_calls"] == 0


def test_model_output_is_normalized_cached_and_version_hashed():
    expected = _bundle("model-bundle")
    expected["message_ids"] = ["m1"]
    for item in expected["evidence"]:
        item["message_id"] = "m1"
    model = FakeModel(expected)
    cache = VersionedBundleCache()
    encoder = BundleSemanticEncoder(model, cache=cache, model_version="fake-1")
    messages = [{"message_id": "m1", "chat_id": "chat-1", "content": "service"}]
    first = encoder.encode(messages, bundle_id="model-bundle")
    second = encoder.encode(messages, bundle_id="model-bundle")
    assert first.status == second.status == "complete"
    assert first.validation.ok is True
    assert second.validation.ok is True
    assert len(model.encode_requests) == 1
    assert encoder.stats.cache_hits == 1
    assert first.input_sha256 == second.input_sha256
    assert first.cache_key != stable_hash(messages)
    assert model.encode_requests[0]["response_schema"]["required"] == list(BUNDLE_FIELDS)
    assert second.bundle["schema_version"] == BUNDLE_SCHEMA_VERSION


def test_provider_failure_retries_then_returns_pending_unknown_without_fallback():
    model = FakeModel(failures=3)
    encoder = BundleSemanticEncoder(model, max_retries=2)
    outcome = encoder.encode([{"message_id": "m1", "chat_id": "chat-1", "content": "x"}], bundle_id="failed-bundle")
    assert outcome.status == "pending"
    assert outcome.validation.ok is True
    assert outcome.bundle["claim_type"] == "unknown"
    assert outcome.bundle["state"] == "unknown"
    assert encoder.stats.model_calls == 3
    assert encoder.stats.model_retries == 2
    assert encoder.stats.fallback_calls == 0


def test_validation_checks_body_evidence_conflict_and_cross_chat_boundaries():
    bundle = _bundle("validation-bundle")
    assert validate_bundle(bundle).ok is True
    bad_body = dict(bundle)
    bad_body["body"] = "must not cross bundle boundary"
    assert "body_field_present" in validate_bundle(bad_body).errors

    bad = _bundle("bad-bundle")
    bad["context_relations"] = [
        {
            "relation_id": "relation-1",
            "source_bundle_id": "bad-bundle",
            "target_bundle_id": "other-bundle",
            "label": "continues",
            "strength": "strong",
            "evidence_ids": [],
            "supporting_signals": ["time"],
            "left_chat_id": "chat-1",
            "right_chat_id": "chat-2",
        }
    ]
    report = validate_bundle(bad)
    assert report.ok is False
    assert "context_relation_0_missing_evidence" in report.errors
    assert "context_relation_0_time_only_strong_forbidden" in report.errors
    assert "context_relation_0_cross_chat" in report.errors
    bad["coreference_candidates"] = [
        {"source_id": "person-1", "target_id": "object-1", "relation": "corefers", "score": 0.9, "evidence_ids": []},
        {"source_id": "person-1", "target_id": "object-1", "relation": "not_corefer", "score": 0.1, "evidence_ids": []},
    ]
    assert "coreference_conflict" in validate_bundle(bad).errors


def test_sparse_structural_index_precedes_dense_recall_and_respects_chat_boundary():
    left = _bundle("left", chat_id="chat-1", object_id="object-shared")
    same_chat = _bundle("same-chat", chat_id="chat-1", object_id="object-other")
    other_chat = _bundle("other-chat", chat_id="chat-2", object_id="object-dense")
    other_chat["state"] = "unknown"
    other_chat["claim_type"] = "unknown"
    other_chat["speaker"]["id"] = "person-other"
    other_chat["metadata"]["keywords"] = ["unique-dense-term"]

    class FakeEmbedder:
        def embed(self, value):
            bundle_id = value.get("bundle_id")
            if bundle_id == "other-chat":
                return (1.0, 0.0)
            return (0.0, 1.0)

    index = SparseStructuralIndex([left, same_chat, other_chat], embedder=FakeEmbedder())
    query = _bundle("query", chat_id="chat-1", object_id="object-shared")
    results = index.retrieve(query, top_k=3)
    assert results[0].bundle_id == "left"
    assert "structural" in results[0].sources
    assert all(item.bundle_id != "other-chat" for item in results)
    cross_chat_results = index.retrieve(query, top_k=3, allow_cross_chat=True)
    dense_only = next(item for item in cross_chat_results if item.bundle_id == "other-chat")
    assert dense_only.dense_only is True
    assert dense_only.sources == ("dense_recall",)
    assert dense_only.score == 0.0
    assert index.embedder_calls >= 4


def test_pairwise_judgement_is_separate_and_evidence_checked():
    left = _bundle("left", object_id="object-left")
    left["evidence"][1]["evidence_id"] = "left-object"
    right = _bundle("right", object_id="object-right")
    model = FakeModel()
    pipeline = BundleSemanticPipeline(model=model, max_retries=0)
    judgement, report = pipeline.judge_pair(left, right)
    assert judgement.label == "answers"
    assert judgement.status == "complete"
    assert report.ok is True
    assert len(model.judge_requests) == 1
    cached_judgement, cached_report = pipeline.judge_pair(left, right)
    assert cached_judgement == judgement
    assert cached_report.ok is True
    assert len(model.judge_requests) == 1

    no_model = BundleSemanticPipeline()
    pending, pending_report = no_model.judge_pair(left, right)
    assert pending.status == "pending"
    assert pending.label == "insufficient"
    assert pending_report.ok is True


def test_invalid_model_schema_is_retried_and_schema_error_is_not_indexed():
    model = FakeModel({"schema_version": "wrong", "bundle_id": "x"})
    encoder = BundleSemanticEncoder(model, max_retries=1)
    outcome = encoder.encode([{"message_id": "m1", "chat_id": "chat-1", "content": "x"}], bundle_id="x")
    assert outcome.status == "pending"
    assert outcome.bundle["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert encoder.stats.model_validation_failures == 2
    assert encoder.stats.model_calls == 2
