import json
from types import SimpleNamespace

import pytest

from wechat_bridge.bundle_semantics import UNKNOWN
from wechat_bridge.staged_deepseek_analyzer import (
    ACTIONS,
    CLAIM_KEYS,
    ContextPacket,
    FakeStageModel,
    OpenAICompatibleStageModel,
    StageCache,
    StageModelResponse,
    StageProviderError,
    StagedDeepseekAnalyzer,
    summarize_ledger,
    validate_context_packet,
    validate_stage_a,
    validate_stage_b,
    validate_stage_c,
)


def _packet():
    return ContextPacket(
        packet_id="packet-synthetic",
        scope="synthetic",
        message_ids=("m0", "m1"),
        context_message_ids=("m2",),
        evidence_ids=("e0", "e1"),
        entity_ids=("person-a", "person-b", "person-c", "object-a"),
        messages=(
            {"message_id": "m0", "content": "synthetic primary one"},
            {"message_id": "m1", "content": "synthetic primary two"},
            {"message_id": "m2", "content": "synthetic context"},
        ),
        evidence=(
            {"evidence_id": "e0", "message_id": "m0", "start": 0, "end": 8},
            {"evidence_id": "e1", "message_id": "m1", "start": 0, "end": 8},
        ),
    )


def _topic(topic_id, primary, context, relation, evidence):
    return {
        "topic_id": topic_id,
        "primary_message_ids": list(primary),
        "context_message_ids": list(context),
        "relation": relation,
        "uncertainties": [],
        "evidence_ids": list(evidence),
    }


def _claim(*, speaker="person-a", subject="person-b", mentioned="person-c", evidence="e0"):
    return {
        "speaker": speaker,
        "subject": subject,
        "mentioned": mentioned,
        "target": UNKNOWN,
        "object": "object-a",
        "action": "inform",
        "claim_type": "fact",
        "state": "ongoing",
        "modality": "certain",
        "evidence_ids": [evidence],
        "uncertainties": [],
    }


def _responses():
    return {
        "A": {
            "topics": [
                _topic("t0", ("m0",), ("m2",), "continuation", ("e0",)),
                _topic("t1", ("m1",), (), "new_topic", ("e1",)),
            ]
        },
        "B:t0": {"topic_id": "t0", "claims": [_claim(evidence="e0")]},
        "B:t1": {
            "topic_id": "t1",
            "claims": [
                _claim(
                    speaker=UNKNOWN,
                    subject=UNKNOWN,
                    mentioned=UNKNOWN,
                    evidence="e1",
                )
                | {
                    "action": "ask",
                    "claim_type": "question",
                    "state": UNKNOWN,
                    "modality": UNKNOWN,
                    "object": UNKNOWN,
                }
            ],
        },
        "C": {
            "accepted_claim_ids": ["c0_0", "c1_0"],
            "conflicts": [],
            "missing_context": [],
            "overmerge": [],
            "undermerge": [],
            "needs_more_context": [],
        },
    }


def test_context_packet_v1_and_three_stage_contract_are_strict():
    packet = _packet()
    assert validate_context_packet(packet).ok
    fake = FakeStageModel(_responses())
    result = StagedDeepseekAnalyzer(fake).analyze(packet)

    assert result.status == "complete"
    assert result.pending_stages == ()
    assert tuple(result.stage_b) == ("t0", "t1")
    assert result.stage_a.payload["topics"][0]["primary_message_ids"] == ["m0"]
    assert result.stage_b["t0"].payload["claims"][0]["speaker"] == "person-a"
    assert result.stage_b["t0"].payload["claims"][0]["subject"] == "person-b"
    assert result.stage_b["t0"].payload["claims"][0]["mentioned"] == "person-c"
    assert result.stage_c.payload["accepted_claim_ids"] == ["c0_0", "c1_0"]
    assert set(result.claim_index) == {"c0_0", "c1_0"}
    assert [call["stage"] for call in fake.calls] == ["A", "B", "B", "C"]
    assert summarize_ledger(result.ledger)["provider_calls"] == 4


def test_stage_cache_is_separated_and_second_run_is_cache_only():
    packet = _packet()
    cache = StageCache()
    fake = FakeStageModel(_responses())
    analyzer = StagedDeepseekAnalyzer(fake, cache=cache)
    first = analyzer.analyze(packet)
    second = analyzer.analyze(packet)

    assert first.status == second.status == "complete"
    assert len(fake.calls) == 4
    assert cache.sizes() == {"A": 1, "B": 2, "C": 1}
    summary = summarize_ledger(second.ledger)
    assert summary["provider_calls"] == 0
    assert summary["cache_hits"] == 4
    assert summary["by_stage"]["A"]["cache_hits"] == 1
    assert summary["by_stage"]["B"]["cache_hits"] == 2
    assert summary["by_stage"]["C"]["cache_hits"] == 1
    assert len({row.system_prefix_sha256 for row in first.ledger if row.stage == "A"}) == 1


def test_failed_stage_is_pending_not_cached_and_retry_preserves_stage_a():
    packet = _packet()
    responses = _responses()
    responses["B:t0"] = [
        {"topic_id": "t0", "claims": [{"bad": True}]},
        responses["B:t0"],
    ]
    fake = FakeStageModel(responses)
    analyzer = StagedDeepseekAnalyzer(fake)
    first = analyzer.analyze(packet)

    assert first.stage_a.status == "complete"
    assert first.stage_b["t0"].status == "pending"
    assert first.stage_b["t1"].status == "complete"
    assert first.stage_c.status == "pending"
    assert "A" not in first.pending_stages
    assert analyzer.cache.sizes() == {"A": 1, "B": 1, "C": 0}

    second = analyzer.analyze(packet, previous=first)
    assert second.status == "complete"
    assert second.stage_a.payload == first.stage_a.payload
    assert second.stage_b["t0"].status == "complete"
    assert second.stage_b["t1"].status == "complete"
    assert second.stage_c.status == "complete"
    # A is retained, the failed B topic is retried, and C is then evaluated.
    assert [call["stage"] for call in fake.calls] == ["A", "B", "B", "B", "C"]
    assert analyzer.cache.sizes() == {"A": 1, "B": 2, "C": 1}


def test_explicit_retry_of_stage_c_does_not_spend_pending_stage_b_call():
    packet = _packet()
    responses = _responses()
    responses["B:t0"] = {"topic_id": "t0", "claims": [{"bad": True}]}
    fake = FakeStageModel(responses)
    analyzer = StagedDeepseekAnalyzer(fake)
    first = analyzer.analyze(packet)
    second = analyzer.analyze(packet, previous=first, retry_stages={"C"})

    assert first.stage_b["t0"].status == "pending"
    assert second.stage_b["t0"].status == "pending"
    assert second.stage_c.status == "pending"
    assert [call["stage"] for call in fake.calls] == ["A", "B", "B"]
    assert summarize_ledger(second.ledger)["provider_calls"] == 0


def test_stage_a_failure_does_not_erase_or_fake_downstream_results():
    packet = _packet()
    responses = _responses()
    responses["A"] = [
        {"topics": [{"topic_id": "t0", "primary_message_ids": ["m9"], "context_message_ids": [], "relation": "new_topic", "uncertainties": [], "evidence_ids": []}]},
        responses["A"],
    ]
    fake = FakeStageModel(responses)
    analyzer = StagedDeepseekAnalyzer(fake)
    first = analyzer.analyze(packet)
    assert first.status == "pending"
    assert first.stage_a.status == "pending"
    assert first.stage_b == {}
    assert first.stage_c.status == "pending"
    assert analyzer.cache.sizes()["A"] == 0

    second = analyzer.analyze(packet, previous=first)
    assert second.status == "complete"
    assert [call["stage"] for call in fake.calls] == ["A", "A", "B", "B", "C"]


def test_adversarial_stage_a_scope_and_shape_rejections():
    packet = _packet()
    valid = _responses()["A"]
    bad_scope = json.loads(json.dumps(valid))
    bad_scope["topics"][0]["primary_message_ids"] = ["m9"]
    assert not validate_stage_a(bad_scope, packet).ok
    assert "stage_a_primary_out_of_scope" in validate_stage_a(bad_scope, packet).errors
    extra = json.loads(json.dumps(valid))
    extra["topics"][0]["extra"] = 1
    assert "stage_a_topic_shape" in validate_stage_a(extra, packet).errors


def test_adversarial_stage_b_evidence_entity_and_extra_field_rejections():
    packet = _packet()
    valid = _responses()["B:t0"]
    forged_entity = json.loads(json.dumps(valid))
    forged_entity["claims"][0]["subject"] = "person-forged"
    assert "stage_b_subject_out_of_scope" in validate_stage_b(forged_entity, packet, expected_topic_id="t0").errors
    forged_evidence = json.loads(json.dumps(valid))
    forged_evidence["claims"][0]["evidence_ids"] = ["e9"]
    assert "stage_b_evidence_out_of_scope" in validate_stage_b(forged_evidence, packet, expected_topic_id="t0").errors
    missing_evidence = json.loads(json.dumps(valid))
    missing_evidence["claims"][0]["evidence_ids"] = []
    assert "stage_b_known_claim_missing_evidence" in validate_stage_b(
        missing_evidence, packet, expected_topic_id="t0"
    ).errors
    extra = json.loads(json.dumps(valid))
    extra["claims"][0]["free_text"] = "must not pass"
    assert "stage_b_claim_shape" in validate_stage_b(extra, packet, expected_topic_id="t0").errors
    assert CLAIM_KEYS == set(_responses()["B:t0"]["claims"][0])
    assert UNKNOWN in ACTIONS


def test_adversarial_stage_c_claim_message_and_topic_scope_rejections():
    packet = _packet()
    valid = _responses()["C"]
    forged_claim = json.loads(json.dumps(valid))
    forged_claim["accepted_claim_ids"] = ["c9"]
    assert "stage_c_accepted_claim_out_of_scope" in validate_stage_c(
        forged_claim, packet, known_claim_ids=("c0_0", "c1_0"), known_topic_ids=("t0", "t1")
    ).errors
    forged_message = json.loads(json.dumps(valid))
    forged_message["missing_context"] = ["m9"]
    assert "stage_c_message_scope" in validate_stage_c(
        forged_message, packet, known_claim_ids=("c0_0", "c1_0"), known_topic_ids=("t0", "t1")
    ).errors
    extra = json.loads(json.dumps(valid))
    extra["unexpected"] = []
    assert "stage_c_shape" in validate_stage_c(
        extra, packet, known_claim_ids=("c0_0", "c1_0"), known_topic_ids=("t0", "t1")
    ).errors


def test_frozen_scope_is_rejected_without_reading_any_directory():
    packet = _packet()
    frozen = ContextPacket(
        packet_id=packet.packet_id,
        scope="frozen",
        message_ids=packet.message_ids,
        evidence_ids=packet.evidence_ids,
    )
    validation = validate_context_packet(frozen)
    assert not validation.ok
    assert "frozen_scope_forbidden" in validation.errors


def test_openai_compatible_adapter_is_lazy_and_fake_response_is_structured():
    adapter = OpenAICompatibleStageModel()
    assert not adapter.configured
    with pytest.raises(StageProviderError, match="provider_unconfigured"):
        adapter.complete("A", "system", {"packet": "synthetic"}, max_output_tokens=20)

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["model"] == "fake-openai"
            assert kwargs["temperature"] == 0
            return SimpleNamespace(
                id="request-synthetic",
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"topics":[]}'))],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    adapter = OpenAICompatibleStageModel(model="fake-openai", client=client)
    response = adapter.complete("A", "system", {"packet": "synthetic"}, max_output_tokens=20)
    assert isinstance(response, StageModelResponse)
    assert response.payload == {"topics": []}
    assert response.input_tokens == 3
    assert response.output_tokens == 2


def test_openai_compatible_adapter_projects_stage_a_contract_and_safe_telemetry():
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                id="request-synthetic-contract",
                model="deepseek-v4-flash",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"topics":[]}',
                            reasoning_content="hidden-synthetic-reasoning",
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=5),
            )

    adapter = OpenAICompatibleStageModel(
        model="deepseek-v4-flash",
        client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )
    result = adapter.complete(
        "A",
        "synthetic-system",
        {"packet": "synthetic"},
        max_output_tokens=400,
        extra_body={"thinking": {"type": "disabled"}},
    )

    assert len(calls) == 1
    request = calls[0]
    assert request["model"] == "deepseek-v4-flash"
    assert request["messages"] == [
        {"role": "system", "content": "synthetic-system"},
        {"role": "user", "content": '{"packet":"synthetic"}'},
    ]
    assert request["max_tokens"] == 400
    assert request["temperature"] == 0
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "response_format" not in request
    assert result.payload == {"topics": []}
    assert result.input_tokens == 7
    assert result.output_tokens == 5
    assert result.finish_reason == "stop"
    assert result.reasoning_length == len("hidden-synthetic-reasoning")


def test_openai_compatible_adapter_distinguishes_shape_failure_from_json_failure():
    class EmptyChoices:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    shape_adapter = OpenAICompatibleStageModel(
        model="fake-openai",
        client=SimpleNamespace(chat=SimpleNamespace(completions=EmptyChoices())),
    )
    with pytest.raises(StageProviderError, match="provider_response_shape"):
        shape_adapter.complete("A", "system", {"packet": "synthetic"}, max_output_tokens=20)

    class InvalidJson:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    invalid_adapter = OpenAICompatibleStageModel(
        model="fake-openai",
        client=SimpleNamespace(chat=SimpleNamespace(completions=InvalidJson())),
    )
    with pytest.raises(StageProviderError, match="provider_invalid_json") as invalid_info:
        invalid_adapter.complete("A", "system", {"packet": "synthetic"}, max_output_tokens=20)
    assert invalid_info.value.content_length == len("not-json")
    assert invalid_info.value.output_tokens == 1

    class EmptyText:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
            )

    empty_adapter = OpenAICompatibleStageModel(
        model="fake-openai",
        client=SimpleNamespace(chat=SimpleNamespace(completions=EmptyText())),
    )
    with pytest.raises(StageProviderError, match="provider_invalid_json") as empty_info:
        empty_adapter.complete("A", "system", {"packet": "synthetic"}, max_output_tokens=20)
    assert empty_info.value.content_length == 0
    assert empty_info.value.output_tokens == 3


def test_openai_compatible_adapter_wraps_sdk_request_errors_without_json_reclassification():
    class RequestFailure:
        def create(self, **kwargs):
            raise json.JSONDecodeError("synthetic", "{}", 0)

    adapter = OpenAICompatibleStageModel(
        model="fake-openai",
        client=SimpleNamespace(chat=SimpleNamespace(completions=RequestFailure())),
    )
    with pytest.raises(StageProviderError, match="provider_request_failed"):
        adapter.complete("A", "system", {"packet": "synthetic"}, max_output_tokens=20)
