"""Synthetic Workstream E checks for the shadow semantic orchestrator.

All IDs and message text in this file are invented.  The tests exercise the
public in-memory pipeline only and never load private or frozen artifacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wechat_bridge.bundle_semantics import empty_bundle
from wechat_bridge.dialogue_bundle import BundleFragment
from wechat_bridge.shadow_semantic import (
    SOURCE_CONSERVATIVE_FALLBACK,
    SOURCE_LLM_ACCEPTED,
    SOURCE_LLM_PENDING,
    SOURCE_PROVIDER_BLOCKED,
    ShadowRunConfig,
    ShadowSemanticCache,
    ShadowSemanticRunStore,
    replay_shadow_semantic,
    run_shadow_semantic,
)


def _message(message_id: str = "m-e-synth", *, chat_id: str = "chat-e-synth") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "account_id": "account-e-synth",
        "chat_id": chat_id,
        "speaker_id": "speaker-e-synth",
        "message_type": "text",
        "content": "synthetic body retained only in memory",
        "sequence_in_chat": 1,
        "time_offset_seconds": 1,
        "split": "development",
    }


def _fragment(message_id: str = "m-e-synth", *, chat_id: str = "chat-e-synth") -> BundleFragment:
    text = "synthetic issue ongoing"
    evidence = ({"type": "fragment", "id": "f-e-synth", "span": {"start": 0, "end": len(text)}},)
    return BundleFragment(
        fragment_id="f-e-synth",
        message_id=message_id,
        account_id="account-e-synth",
        chat_id=chat_id,
        segment_id="segment-e-synth",
        text=text,
        span_start=0,
        span_end=len(text),
        role="substantive",
        fragment_type="statement",
        speaker_id="speaker-e-synth",
        subject_id="subject-e-synth",
        subject_type="person",
        object_id="object-e-synth",
        object_resolution="explicit",
        object_evidence_refs=evidence,
        state="ongoing",
        state_evidence="explicit",
        closure_reason="unknown",
        actions=("check",),
        information_value="high",
        event_completeness="sufficient",
        evidence_refs=evidence,
    )


class _AcceptedModel:
    model_version = "synthetic-e-accepted-v1"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def encode_bundle(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        message_id = request["messages"][0]["message_id"]
        evidence = [
            {
                "evidence_id": "ev-e-subject",
                "message_id": message_id,
                "span": {"start": 0, "end": 23},
                "field": "subject",
            },
            {
                "evidence_id": "ev-e-object",
                "message_id": message_id,
                "span": {"start": 0, "end": 23},
                "field": "object",
            },
            {
                "evidence_id": "ev-e-action",
                "message_id": message_id,
                "span": {"start": 0, "end": 23},
                "field": "action",
            },
            {
                "evidence_id": "ev-e-state",
                "message_id": message_id,
                "span": {"start": 0, "end": 23},
                "field": "state",
            },
            {
                "evidence_id": "ev-e-claim",
                "message_id": message_id,
                "span": {"start": 0, "end": 23},
                "field": "claim_type",
            },
            {
                "evidence_id": "ev-e-modality",
                "message_id": message_id,
                "span": {"start": 0, "end": 23},
                "field": "modality",
            },
        ]
        result = empty_bundle(
            request["bundle_id"],
            [item["message_id"] for item in request["messages"]],
            chat_id=request["chat_id"],
            status="complete",
            source="synthetic-accepted",
        )
        result.update(
            {
                "speaker": {"id": "speaker-e-synth", "type": "person", "resolution": "explicit", "evidence_ids": ["ev-e-subject"]},
                "subject": {"id": "subject-from-model", "type": "person", "resolution": "explicit", "evidence_ids": ["ev-e-subject"]},
                "object": [{"id": "object-from-model", "type": "object", "resolution": "explicit", "evidence_ids": ["ev-e-object"]}],
                "action": [{"id": "action-e-synth", "label": "check", "resolution": "explicit", "evidence_ids": ["ev-e-action"]}],
                "claim_type": "fact",
                "state": "ongoing",
                "modality": "certain",
                "evidence": evidence,
            }
        )
        return result


class _FailedModel:
    model_version = "synthetic-e-failed-v1"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def encode_bundle(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        raise RuntimeError("synthetic provider failure")


def _body_keys(value: Any) -> list[str]:
    body_names = {
        "text",
        "content",
        "body",
        "raw",
        "raw_text",
        "redacted_text",
        "message_text",
        "fragment_text_redacted",
        "evidence_text",
        "claim_text_redacted",
        "summary",
        "narrative",
        "prompt",
        "response",
    }
    output: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if name.casefold() in body_names or name.casefold().endswith(("_text", "_content", "_surface")):
                output.append(name)
            output.extend(_body_keys(item))
    elif isinstance(value, list):
        for item in value:
            output.extend(_body_keys(item))
    return output


def _run_kwargs(model: Any, *, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "model": model,
        "messages": (_message(),),
        "fragments": (_fragment(),),
        "config": ShadowRunConfig(
            mode=mode,
            event_materialization_enabled=True,
            pipeline_kwargs={"max_input_tokens": 10000, "max_output_tokens": 10000},
        ),
    }


def test_fake_accepted_model_flows_to_thread_and_event_candidate():
    model = _AcceptedModel()
    result = run_shadow_semantic(**_run_kwargs(model, mode="fake"))

    assert result.source_marker == SOURCE_LLM_ACCEPTED
    assert result.model_status == "accepted"
    assert result.api_envelope()["provider_status"] == "succeeded"
    assert result.api_envelope()["llm_accepted"] is True
    assert result.api_envelope()["fallback_reason"] is None
    assert result.threads
    assert len(result.event_candidates) == 1
    assert result.event_candidates[0].source == SOURCE_LLM_ACCEPTED
    assert result.event_candidates[0].bundle_ids
    assert result.event_candidates[0].fragment_ids == ("f-e-synth",)
    assert model.requests
    assert not _body_keys(result.to_dict())


def test_fake_accepted_model_can_fill_unknown_slots_without_input_fragments():
    model = _AcceptedModel()
    kwargs = _run_kwargs(model, mode="fake")
    kwargs["fragments"] = ()
    result = run_shadow_semantic(**kwargs)

    assert result.source_marker == SOURCE_LLM_ACCEPTED
    assert len(result.event_candidates) == 1
    assert result.threads[0].subject_ids == ("subject-from-model",)
    assert result.threads[0].object_refs[0]["id"] == "object-from-model"


def test_disabled_mode_is_conservative_fallback_and_never_materializes_event():
    model = _AcceptedModel()
    result = run_shadow_semantic(**_run_kwargs(model, mode="disabled"))

    assert result.source_marker == SOURCE_CONSERVATIVE_FALLBACK
    assert result.model_status == "fallback"
    assert result.api_envelope()["provider_status"] == "disabled"
    assert result.api_envelope()["llm_accepted"] is False
    assert result.api_envelope()["fallback_reason"] == "disabled_mode"
    assert result.event_candidates == ()
    assert model.requests == []
    assert result.manifest["source_marker"] == SOURCE_CONSERVATIVE_FALLBACK


def test_failed_model_is_llm_pending_and_never_materializes_event():
    model = _FailedModel()
    result = run_shadow_semantic(**_run_kwargs(model, mode="fake"))

    assert result.source_marker == SOURCE_LLM_PENDING
    assert result.model_status == "pending"
    assert result.api_envelope()["provider_status"] == "failed"
    assert result.api_envelope()["llm_accepted"] is False
    assert result.event_candidates == ()
    assert result.threads
    assert model.requests


def test_missing_provider_is_distinguished_as_provider_blocked():
    result = run_shadow_semantic(**_run_kwargs(None, mode="fake"))

    assert result.source_marker == SOURCE_PROVIDER_BLOCKED
    assert result.model_status == "unavailable"
    assert result.api_envelope()["provider_status"] == "blocked"
    assert result.api_envelope()["llm_accepted"] is False
    assert result.event_candidates == ()
    assert result.manifest["provider_blocked"] is True


def test_shadow_feature_flag_short_circuits_provider_and_keeps_body_free_manifest():
    model = _AcceptedModel()
    kwargs = _run_kwargs(model, mode="fake")
    kwargs["config"] = ShadowRunConfig(mode="fake", shadow_enabled=False)
    result = run_shadow_semantic(**kwargs)

    assert result.source_marker == SOURCE_CONSERVATIVE_FALLBACK
    assert result.threads == ()
    assert result.event_candidates == ()
    assert model.requests == []
    assert result.manifest["feature_flags"]["shadow_enabled"] is False
    assert not _body_keys(result.to_dict())


def test_shadow_cache_store_and_replay_are_idempotent_and_hash_stable():
    model = _AcceptedModel()
    cache = ShadowSemanticCache()
    store = ShadowSemanticRunStore()
    kwargs = _run_kwargs(model, mode="fake")
    first = run_shadow_semantic(**kwargs, cache=cache, store=store)
    request_count = len(model.requests)
    replay = replay_shadow_semantic(
        kwargs["messages"],
        model=model,
        fragments=kwargs["fragments"],
        config=kwargs["config"],
        cache=cache,
        store=store,
    )
    assert replay.cache_hit is True
    assert replay.input_sha256 == first.input_sha256
    assert replay.replay_key == first.replay_key
    assert len(model.requests) == request_count
    assert len(store.runs()) == 1
    assert len(store.threads()) == len(first.threads)
    assert len(store.events()) == len(first.event_candidates)
    assert not _body_keys(store.to_dict())
