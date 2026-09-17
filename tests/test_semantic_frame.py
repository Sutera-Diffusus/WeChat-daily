import json

import pytest

from wechat_bridge.bundle_semantics import BUNDLE_FIELDS, empty_bundle
from wechat_bridge.contextual_bundle_pipeline import AIProviderConfig, SemanticFrameBundleModel
from wechat_bridge.semantic_frame import (
    SemanticFrameParseError,
    parse_bundle_frame,
    parse_health_frame,
)


def _frame(bundle):
    return "\n".join(
        "%s\t%s" % (field, json.dumps(bundle[field], ensure_ascii=False, separators=(",", ":")))
        for field in BUNDLE_FIELDS
    )


def _valid_bundle():
    return empty_bundle("bundle-synthetic", ["message-synthetic"], chat_id="chat-synthetic")


def _error_code(callable_, *args, **kwargs):
    with pytest.raises(SemanticFrameParseError) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


def test_health_frame_accepts_one_controlled_fenced_frame_and_rejects_multiple_or_extra():
    assert parse_health_frame("probe result:\n```semantic_frame_v1\nOK\ttrue\n```\n") is True
    assert _error_code(parse_health_frame, "OK\tfalse") == "semantic_frame_health_invalid"
    assert _error_code(parse_health_frame, "OK\ttrue\nOK\ttrue") == "semantic_frame_health_frame_count"
    assert _error_code(parse_health_frame, "OK\ttrue\nOTHER\tfalse") == "semantic_frame_health_frame_count"
    assert _error_code(parse_health_frame, "OK\t1") == "semantic_frame_health_invalid"


def test_bundle_frame_requires_exact_ordered_field_set_and_json_types():
    bundle = _valid_bundle()
    frame = _frame(bundle)
    assert parse_bundle_frame(
        frame,
        expected_message_ids=("message-synthetic",),
        expected_chat_id="chat-synthetic",
    ) == bundle

    lines = frame.splitlines()
    assert _error_code(parse_bundle_frame, "\n".join(lines[:-1]), expected_message_ids=("message-synthetic",)) == "semantic_frame_field_count"
    assert _error_code(parse_bundle_frame, frame + "\nEXTRA\tnull") == "semantic_frame_field_count"
    wrong_type = list(lines)
    wrong_type[2] = "message_ids\t{}"
    assert _error_code(parse_bundle_frame, "\n".join(wrong_type)) == "semantic_frame_message_ids_type"
    wrong_order = list(lines)
    wrong_order[0], wrong_order[1] = wrong_order[1], wrong_order[0]
    assert _error_code(parse_bundle_frame, "\n".join(wrong_order)) == "semantic_frame_field_order"


def test_bundle_frame_shape_diagnostics_expose_only_public_schema_names():
    bundle = _valid_bundle()
    frame = _frame(bundle)
    with pytest.raises(SemanticFrameParseError) as missing:
        parse_bundle_frame("\n".join(frame.splitlines()[:-1]))
    assert missing.value.code == "semantic_frame_field_count"
    assert missing.value.diagnostics["missing_field_names"] == ["metadata"]
    assert missing.value.diagnostics["extra_field_names"] == []
    assert missing.value.diagnostics["duplicate"] == []
    assert all(name in BUNDLE_FIELDS for values in missing.value.diagnostics.values() if isinstance(values, list) for name in values)

    with pytest.raises(SemanticFrameParseError) as duplicate:
        parse_bundle_frame(frame + "\nmetadata\t{}")
    assert duplicate.value.code == "semantic_frame_field_count"
    assert duplicate.value.diagnostics["duplicate"] == ["metadata"]
    assert duplicate.value.diagnostics["extra_field_names"] == ["metadata"]

    reordered = frame.splitlines()
    reordered[0], reordered[1] = reordered[1], reordered[0]
    with pytest.raises(SemanticFrameParseError) as order:
        parse_bundle_frame("\n".join(reordered))
    assert order.value.code == "semantic_frame_field_order"
    assert order.value.diagnostics["order_error"] == ["schema_version", "bundle_id"]

    private_extra = frame + "\nPRIVATE_PROVIDER_FIELD\t\"must-not-echo\""
    with pytest.raises(SemanticFrameParseError) as extra:
        parse_bundle_frame(private_extra)
    assert "PRIVATE_PROVIDER_FIELD" not in json.dumps(extra.value.diagnostics)
    assert "must-not-echo" not in json.dumps(extra.value.diagnostics)


def test_bundle_frame_rejects_evidence_boundary_and_missing_evidence_without_repair():
    bundle = _valid_bundle()
    bundle["speaker"] = {
        "id": "speaker-synthetic",
        "type": "person",
        "role": "speaker",
        "resolution": "explicit",
        "evidence_ids": ["evidence-synthetic"],
    }
    frame = _frame(bundle)
    with pytest.raises(SemanticFrameParseError) as first_error:
        parse_bundle_frame(
            frame,
            expected_message_ids=("message-synthetic",),
            expected_chat_id="chat-synthetic",
        )
    assert first_error.value.code == "semantic_frame_bundle_validation_failed"
    assert first_error.value.validation_categories == ("evidence_reference",)

    bundle = _valid_bundle()
    bundle["evidence"] = [
        {
            "evidence_id": "evidence-synthetic",
            "message_id": "message-out-of-scope",
            "span": {"start": 0, "end": 1},
        }
    ]
    with pytest.raises(SemanticFrameParseError) as second_error:
        parse_bundle_frame(
            _frame(bundle),
            expected_message_ids=("message-synthetic",),
            expected_chat_id="chat-synthetic",
        )
    assert second_error.value.code == "semantic_frame_bundle_validation_failed"
    assert second_error.value.validation_categories == ("evidence_boundary",)


def test_bundle_frame_rejects_prose_and_semantic_coercion():
    bundle = _valid_bundle()
    lines = _frame(bundle).splitlines()
    assert _error_code(parse_bundle_frame, "explanation\n" + "\n".join(lines)) == "semantic_frame_prose_forbidden"
    invalid_enum = list(lines)
    invalid_enum[9] = 'claim_type\ttrue'
    assert _error_code(parse_bundle_frame, "\n".join(invalid_enum)) == "semantic_frame_claim_type_type"
    invalid_json = list(lines)
    invalid_json[2] = "message_ids\t[not-json]"
    assert _error_code(parse_bundle_frame, "\n".join(invalid_json)) == "semantic_frame_invalid_json"


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"output_text": self.responses.pop(0), "usage": {"prompt_tokens": 20, "completion_tokens": 20}}


class _FakeChat:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)


class _FakeClient:
    def __init__(self, responses):
        self.chat = _FakeChat(responses)


def test_semantic_frame_provider_never_sends_response_format_and_checks_locally():
    bundle = _valid_bundle()
    client = _FakeClient(["OK\ttrue", _frame(bundle)])
    model = SemanticFrameBundleModel(
        AIProviderConfig(
            model="synthetic-frame-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        )
    )
    model._client = client

    health = model.health_check(max_input_tokens=500, max_output_tokens=100)
    encoded = model.encode_bundle(
        {
            "schema_version": "bundle_semantics_v1",
            "bundle_id": "bundle-synthetic",
            "chat_id": "chat-synthetic",
            "messages": [{"message_id": "message-synthetic", "chat_id": "chat-synthetic"}],
        }
    )

    assert health.ok is True
    assert encoded["bundle_id"] == "bundle-synthetic"
    assert all("response_format" not in call for call in client.chat.completions.calls)
    assert all(call["max_tokens"] in {100, 400} for call in client.chat.completions.calls)
    assert "synthetic-secret" not in json.dumps(client.chat.completions.calls)


def test_semantic_frame_provider_failure_ledger_keeps_safe_response_metadata_only():
    bundle = _valid_bundle()
    invalid_frame = "\n".join(_frame(bundle).splitlines()[:-1])
    client = _FakeClient([invalid_frame])
    model = SemanticFrameBundleModel(
        AIProviderConfig(
            model="synthetic-frame-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        )
    )
    model._client = client

    with pytest.raises(SemanticFrameParseError) as caught:
        model.encode_bundle(
            {
                "schema_version": "bundle_semantics_v1",
                "bundle_id": "bundle-synthetic",
                "chat_id": "chat-synthetic",
                "messages": [{"message_id": "message-synthetic", "chat_id": "chat-synthetic"}],
            }
        )
    metadata = caught.value.provider_metadata
    assert metadata["content_length"] > 0
    assert metadata["output_tokens"] == 20
    assert metadata["missing_field_names"] == ["metadata"]
    assert metadata["response_shape_diagnosis"] == "nonempty_output"
    payload = json.dumps(metadata, ensure_ascii=False)
    assert "synthetic-secret" not in payload
    assert invalid_frame not in payload


def test_semantic_frame_thinking_switch_and_compact_payload_are_explicit_and_bounded():
    bundle = _valid_bundle()
    client = _FakeClient(["OK\ttrue", _frame(bundle)])
    model = SemanticFrameBundleModel(
        AIProviderConfig(
            model="synthetic-frame-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        ),
        thinking_disabled=True,
    )
    model._client = client
    model.health_check(max_input_tokens=500, max_output_tokens=100)
    model.encode_bundle(
        {
            "schema_version": "bundle_semantics_v1",
            "bundle_id": "bundle-synthetic",
            "chat_id": "chat-synthetic",
            "messages": [
                {
                    "message_id": "message-synthetic",
                    "chat_id": "chat-synthetic",
                    "speaker_id": "speaker-synthetic",
                    "message_type": "text",
                    "content": "x" * 10000,
                }
            ],
        }
    )

    assert client.chat.completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert client.chat.completions.calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}
    user_content = client.chat.completions.calls[1]["messages"][1]["content"]
    assert len(user_content) <= 1800


def test_bundle_health_keeps_response_metadata_only_and_never_body_or_reasoning():
    bundle = empty_bundle(
        "semantic-frame-health-bundle",
        ["semantic-frame-health-message"],
        chat_id="semantic-frame-health-chat",
        status="complete",
        source="health",
    )
    frame = _frame(bundle)
    client = _FakeClient([frame])
    model = SemanticFrameBundleModel(
        AIProviderConfig(
            model="synthetic-frame-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        )
    )
    model._client = client

    health = model.bundle_health_check(max_input_tokens=500, max_output_tokens=100)
    payload = json.dumps(health.to_dict(), ensure_ascii=False)

    assert health.ok is True
    assert health.diagnostics["probe_kind"] == "synthetic_bundle"
    assert health.diagnostics["response_format_sent"] is False
    assert health.diagnostics["raw_content_saved"] is False
    assert health.diagnostics["raw_reasoning_saved"] is False
    assert "semantic-frame-health-message" not in payload
    assert "synthetic-secret" not in payload


def test_bundle_health_records_empty_response_metadata_without_reading_body():
    class _EmptyCompletions:
        def create(self, **kwargs):
            return {
                "output_text": "",
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "private-reasoning"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 22, "completion_tokens": 100},
                "status_code": 200,
            }

    class _EmptyClient:
        class _Chat:
            completions = _EmptyCompletions()

        chat = _Chat()

    model = SemanticFrameBundleModel(
        AIProviderConfig(
            model="synthetic-frame-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        )
    )
    model._client = _EmptyClient()

    health = model.bundle_health_check(max_input_tokens=500, max_output_tokens=100)
    payload = json.dumps(health.to_dict(), ensure_ascii=False)

    assert health.ok is False
    assert health.error_code == "semantic_frame_empty"
    assert health.diagnostics["content_length"] == 0
    assert health.diagnostics["reasoning_content_length"] == len("private-reasoning")
    assert health.diagnostics["finish_reasons"] == ["length"]
    assert health.diagnostics["response_shape_diagnosis"] == "reasoning_or_output_length_exhaustion_suspected"
    assert health.diagnostics["http_status"] == 200
    assert health.diagnostics["raw_reasoning_saved"] is False
    assert "private-reasoning" not in payload
