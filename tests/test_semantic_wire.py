import json
from dataclasses import replace

import pytest

from wechat_bridge.bundle_semantics import BUNDLE_FIELDS, empty_bundle, validate_bundle
from wechat_bridge.contextual_bundle_pipeline import AIProviderConfig, ProviderHealthResult
from wechat_bridge.contextual_bundle_pipeline_runner import (
    SEMANTIC_WIRE_RUNNER_SCHEMA_VERSION,
    run_development_shadow_pilot_v28,
)
from wechat_bridge.semantic_wire import (
    CANONICAL_SCHEMA_VERSION,
    WIRE_EVIDENCE_FIELDS,
    WIRE_FIELDS,
    WIRE_PROMPT_VERSION,
    WIRE_SCHEMA_VERSION,
    RequestLocalSymbolTable,
    SemanticWireBundleModel,
    SemanticWireParseError,
    SymbolEvidence,
    assemble_wire_bundle,
    build_symbol_table,
    parse_wire_frame,
    validate_symbol_table,
    wire_frame_exemplar,
)


def _wire_frame(value):
    return "\n".join(
        "%s\t%s" % (field, json.dumps(value[field], ensure_ascii=False, separators=(",", ":")))
        for field in WIRE_FIELDS
    )


def _valid_wire():
    return {
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "claim_type": "unknown",
        "state": "unknown",
        "modality": "unknown",
        "subject": {
            "id": "object-local",
            "type": "object",
            "role": "subject",
            "resolution": "explicit",
            "evidence_handles": ["e0"],
        },
        "mentioned_person": [],
        "target": [],
        "object": [],
        "action": [],
        "coreference_candidates": [],
        "context_relations": [],
        "uncertainties": [],
        "evidence_handles": {field: [] for field in WIRE_EVIDENCE_FIELDS},
    }


def _request():
    return {
        "bundle_id": "bundle-with-a-long-authoritative-id",
        "chat_id": "chat-with-a-long-authoritative-id",
        "messages": [
            {
                "message_id": "message-with-a-long-authoritative-id",
                "chat_id": "chat-with-a-long-authoritative-id",
                "speaker_id": "speaker-authoritative-id",
                "message_type": "text",
                "content": "synthetic evidence",
            }
        ],
    }


def test_symbol_table_exposes_handles_without_long_authoritative_ids():
    table = build_symbol_table(_request())
    payload = json.dumps(table.to_model_payload(), ensure_ascii=False)

    assert table.messages[0].message_handle == "m0"
    assert table.messages[0].span_handle == "f0"
    assert table.messages[0].evidence_handle == "e0"
    assert "message-with-a-long-authoritative-id" not in payload
    assert "chat-with-a-long-authoritative-id" not in payload
    assert "speaker-authoritative-id" not in payload
    assert '"handle": "e0"' in payload
    assert table.cache_context(_request())["wire_schema_version"] == WIRE_SCHEMA_VERSION
    assert table.cache_context(_request())["wire_prompt_version"] == WIRE_PROMPT_VERSION


def test_symbol_table_binds_scope_and_account_locally_and_rejects_cross_scope_input():
    request = _request()
    request["scope"] = "scope-authoritative"
    request["messages"][0]["account_id"] = "account-a"
    table = build_symbol_table(request)

    assert table.scope == "scope-authoritative"
    assert table.account_id == "account-a"
    payload = json.dumps(table.to_model_payload(), ensure_ascii=False)
    assert "scope-authoritative" not in payload

    cross_scope = dict(request)
    cross_scope["messages"] = [
        dict(request["messages"][0]),
        {
            "message_id": "second-message",
            "chat_id": request["chat_id"],
            "account_id": "different-account",
            "content": "synthetic evidence 2",
        },
    ]
    with pytest.raises(SemanticWireParseError) as crossed:
        build_symbol_table(cross_scope)
    assert crossed.value.code == "wire_cross_scope_input"


def test_empty_wire_exemplar_assembles_authoritative_canonical_bundle():
    request = _request()
    table = build_symbol_table(request)
    lines = wire_frame_exemplar().splitlines()[1:-1]
    wire = parse_wire_frame("\n".join(lines), symbol_table=table)
    bundle = assemble_wire_bundle(wire, request, symbol_table=table)

    assert bundle["schema_version"] == CANONICAL_SCHEMA_VERSION
    assert bundle["message_ids"] == [request["messages"][0]["message_id"]]
    assert bundle["metadata"]["chat_id"] == request["chat_id"]
    assert bundle["speaker"]["id"] == request["messages"][0]["speaker_id"]
    assert bundle["metadata"]["wire_schema_version"] == WIRE_SCHEMA_VERSION
    assert bundle["metadata"]["wire_prompt_version"] == WIRE_PROMPT_VERSION
    assert bundle["metadata"]["canonical_schema_version"] == CANONICAL_SCHEMA_VERSION
    assert bundle["metadata"]["symbol_table_sha256"] == table.symbol_table_sha256
    assert bundle["subject"]["id"] == "unknown"
    assert bundle["mentioned_person"] == []
    assert bundle["object"] == []
    assert validate_bundle(
        bundle,
        message_ids=table.message_ids,
        chat_id=table.chat_id,
        expected_schema_version=CANONICAL_SCHEMA_VERSION,
    ).ok


@pytest.mark.parametrize(
    ("wire_subject", "expected_subject"),
    (
        ("p0", "speaker-authoritative-id"),  # explicit first person
        ("unknown", "unknown"),  # second person is not inferred
        ("third-person-local", "third-person-local"),  # grounded third person
    ),
)
def test_subject_person_reference_does_not_overwrite_mentioned_or_authoritative_speaker(
    wire_subject, expected_subject
):
    request = _request()
    table = build_symbol_table(request)
    wire = _valid_wire()
    wire["subject"] = {
        "id": wire_subject,
        "type": "person",
        "role": "subject",
        "resolution": "explicit" if wire_subject != "unknown" else "unknown",
        "evidence_handles": ["e0"] if wire_subject != "unknown" else [],
    }
    wire["mentioned_person"] = [
        {
            "id": "mentioned-person-local",
            "type": "person",
            "role": "mentioned_person",
            "resolution": "explicit",
            "evidence_handles": ["e0"],
        }
    ]
    bundle = assemble_wire_bundle(
        parse_wire_frame(_wire_frame(wire), symbol_table=table),
        request,
        symbol_table=table,
    )

    assert bundle["speaker"]["id"] == request["messages"][0]["speaker_id"]
    assert bundle["subject"]["id"] == expected_subject
    assert bundle["mentioned_person"][0]["id"] == "mentioned-person-local"
    assert bundle["mentioned_person"][0]["id"] != bundle["speaker"]["id"]


@pytest.mark.parametrize("claim_type", ("question", "request", "opinion", "fact"))
def test_grounded_claim_type_intents_are_preserved_without_speaker_shortcut(claim_type):
    request = _request()
    table = build_symbol_table(request)
    wire = _valid_wire()
    wire["claim_type"] = claim_type
    wire["evidence_handles"] = {field: (["e0"] if field == "claim_type" else []) for field in WIRE_EVIDENCE_FIELDS}
    bundle = assemble_wire_bundle(
        parse_wire_frame(_wire_frame(wire), symbol_table=table),
        request,
        symbol_table=table,
    )

    assert bundle["claim_type"] == claim_type
    assert bundle["speaker"]["id"] == request["messages"][0]["speaker_id"]
    assert bundle["subject"]["id"] == "object-local"
    assert bundle["subject"]["id"] != bundle["speaker"]["id"]


def test_concrete_subject_and_claim_without_evidence_are_projected_to_unknown():
    request = _request()
    table = build_symbol_table(request)
    wire = _valid_wire()
    wire["subject"] = {
        "id": "ungrounded-subject",
        "type": "person",
        "role": "subject",
        "resolution": "explicit",
        "evidence_handles": [],
    }
    wire["claim_type"] = "fact"
    wire["evidence_handles"] = {field: [] for field in WIRE_EVIDENCE_FIELDS}
    bundle = assemble_wire_bundle(
        parse_wire_frame(_wire_frame(wire), symbol_table=table),
        request,
        symbol_table=table,
    )

    assert bundle["subject"]["id"] == "unknown"
    assert bundle["subject"]["resolution"] == "unknown"
    assert bundle["claim_type"] == "unknown"
    uncertainty_codes = {item["code"] for item in bundle["uncertainties"]}
    assert "wire_ungrounded_claim_type" in uncertainty_codes


class _FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"output_text": self.response, "usage": {"prompt_tokens": 30, "completion_tokens": 40}}


class _FakeClient:
    def __init__(self, response):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(response)})()


def test_wire_model_returns_canonical_bundle_and_never_sends_authoritative_ids():
    wire = _valid_wire()
    frame = _wire_frame(wire)
    client = _FakeClient(frame)
    model = SemanticWireBundleModel(
        AIProviderConfig(
            model="synthetic-wire-model",
            api_key="synthetic-secret",
            base_url="https://synthetic.invalid/v1",
        ),
        thinking_disabled=True,
    )
    model._client = client
    result = model.encode_bundle(_request())
    call = client.chat.completions.calls[0]
    sent = json.dumps(call, ensure_ascii=False)

    assert result["schema_version"] == CANONICAL_SCHEMA_VERSION
    assert result["subject"]["id"] == "object-local"
    assert result["evidence"][0]["message_id"] == "message-with-a-long-authoritative-id"
    assert result["metadata"]["wire_schema_version"] == WIRE_SCHEMA_VERSION
    assert result["metadata"]["wire_prompt_version"] == WIRE_PROMPT_VERSION
    assert result["metadata"]["canonical_schema_version"] == CANONICAL_SCHEMA_VERSION
    assert result["metadata"]["symbol_table_sha256"]
    assert "message-with-a-long-authoritative-id" not in sent
    assert "chat-with-a-long-authoritative-id" not in sent
    assert "speaker-authoritative-id" not in sent
    assert "response_format" not in call
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "synthetic-secret" not in sent
    instructions = call["messages"][0]["content"]
    assert "subject is what the utterance predicates" in instructions
    assert "mentioned_person is a referred person, not a speaker shortcut" in instructions
    assert "information-seeking=question" in instructions
    assert "without evidence emit unknown/empty" in instructions


def test_wire_parser_rejects_forged_duplicate_and_cross_chat_handles():
    table = build_symbol_table(_request())
    wire = _valid_wire()

    forged = dict(wire)
    forged["subject"] = dict(wire["subject"])
    forged["subject"]["evidence_handles"] = ["e99"]
    with pytest.raises(SemanticWireParseError) as unknown:
        parse_wire_frame(_wire_frame(forged), symbol_table=table)
    assert unknown.value.code == "wire_evidence_handle_unknown"

    duplicate = dict(wire)
    duplicate["subject"] = dict(wire["subject"])
    duplicate["subject"]["evidence_handles"] = ["e0", "e0"]
    with pytest.raises(SemanticWireParseError) as repeated:
        parse_wire_frame(_wire_frame(duplicate), symbol_table=table)
    assert repeated.value.code == "wire_evidence_handle_duplicate"

    with pytest.raises(SemanticWireParseError) as cross_chat:
        build_symbol_table(
            {
                "bundle_id": "bundle",
                "chat_id": "chat-a",
                "messages": [
                    {"message_id": "m0", "chat_id": "chat-a", "content": "a"},
                    {"message_id": "m1", "chat_id": "chat-b", "content": "b"},
                ],
            }
        )
    assert cross_chat.value.code == "wire_cross_chat_input"


def test_wire_parser_rejects_duplicate_json_object_keys_and_direct_forged_mapping():
    table = build_symbol_table(_request())
    wire = _valid_wire()
    lines = []
    for field in WIRE_FIELDS:
        if field == "subject":
            encoded = '{"id":"first","id":"second","type":"object","role":"subject","resolution":"explicit","evidence_handles":["e0"]}'
        else:
            encoded = json.dumps(wire[field], ensure_ascii=False, separators=(",", ":"))
        lines.append("%s\t%s" % (field, encoded))
    with pytest.raises(SemanticWireParseError) as duplicate_key:
        parse_wire_frame("\n".join(lines), symbol_table=table)
    assert duplicate_key.value.code == "wire_duplicate_object_key"

    forged = dict(wire)
    forged["subject"] = dict(wire["subject"])
    forged["subject"]["evidence_handles"] = ["e99"]
    with pytest.raises(SemanticWireParseError) as forged_handle:
        assemble_wire_bundle(forged, _request(), symbol_table=table)
    assert forged_handle.value.code == "wire_evidence_handle_unknown"


def test_symbol_table_rejects_out_of_bounds_span_and_duplicate_message_ids():
    table = build_symbol_table(_request())
    message = table.messages[0]
    bad_evidence = replace(table.evidence_by_handle["e0"], end=len(message.content) + 1)
    bad_table = replace(table, evidence_by_handle={"e0": bad_evidence})
    with pytest.raises(SemanticWireParseError) as bounds:
        validate_symbol_table(bad_table)
    assert bounds.value.code == "wire_span_out_of_bounds"

    with pytest.raises(SemanticWireParseError) as duplicate:
        build_symbol_table(
            {
                "bundle_id": "bundle",
                "chat_id": "chat",
                "messages": [
                    {"message_id": "same", "chat_id": "chat", "content": "a"},
                    {"message_id": "same", "chat_id": "chat", "content": "b"},
                ],
            }
        )
    assert duplicate.value.code == "wire_message_id_duplicate_or_missing"


class _FakeCanonicalModel:
    model_version = "synthetic-wire-model"

    def encode_bundle(self, request):
        return empty_bundle(
            request["bundle_id"],
            [item["message_id"] for item in request["messages"]],
            chat_id=request["chat_id"],
            status="complete",
            source="wire-test",
        )


def test_v28_runner_records_wire_contract_and_reuses_health_without_new_probe(tmp_path):
    development = tmp_path / "development"
    development.mkdir()
    (development / "messages.private.jsonl").write_text(
        json.dumps(
            {
                "split": "development",
                "local_day": "2026-08-25",
                "message_id": "message-synthetic",
                "chat_id": "chat-synthetic",
                "speaker_id": "speaker-synthetic",
                "redacted_text": "synthetic",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = AIProviderConfig(model="synthetic-wire-model", api_key="synthetic-secret", base_url="https://synthetic.invalid/v1")
    health = ProviderHealthResult(
        ok=True,
        status="available",
        source="synthetic-provider",
        model="synthetic-wire-model",
        request_sha256="synthetic-health-hash",
        input_tokens=41,
        output_tokens=23,
        max_input_tokens=500,
        max_output_tokens=400,
        latency_ms=1.0,
        error_code=None,
        config={"api_key_configured": True},
        diagnostics={"probe_kind": "synthetic_bundle"},
    )
    result = run_development_shadow_pilot_v28(
        development,
        tmp_path / "v2_8",
        provider_config=config,
        provider_health=health,
        model=_FakeCanonicalModel(),
        capability_artifact=tmp_path / "v2_6_capability",
    )

    assert result.ok is True
    manifest = json.loads((tmp_path / "v2_8" / "manifest.private.json").read_text(encoding="utf-8"))
    assert manifest["runner_schema_version"] == SEMANTIC_WIRE_RUNNER_SCHEMA_VERSION
    assert manifest["wire_schema_version"] == WIRE_SCHEMA_VERSION
    assert manifest["wire_prompt_version"] == WIRE_PROMPT_VERSION
    assert manifest["canonical_schema_version"] == CANONICAL_SCHEMA_VERSION
    assert manifest["wire_symbol_table_enforced"] is True
    assert manifest["model_authoritative_id_write"] is False
    assert manifest["evidence_handle_validation"] == "strict_local"
    assert manifest["cache_includes_symbol_table"] is True
    assert manifest["provider_health_reused"] is True
    assert manifest["split"] == "development"
    assert manifest["local_day"] == "2026-08-25"
    assert manifest["development_input_read"] is True
    assert manifest["frozen_read"] is False
    assert "synthetic-secret" not in json.dumps(manifest)
