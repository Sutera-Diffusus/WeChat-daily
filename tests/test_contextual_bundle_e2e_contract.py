"""Independent synthetic E2E contract checks for the contextual bundle path.

This module deliberately exercises the public registry -> gate -> dialogue
bundle -> bundle-semantics interfaces together.  Every message, person,
object, and evidence identifier is invented; no private or frozen artifact is
read.  The assertions are contract gates, so an unimplemented contract
surface is expected to fail loudly rather than being hidden behind a mock.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from wechat_bridge.bundle_semantics import (
    BUNDLE_FIELDS,
    BundleSemanticPipeline,
    BundleSemanticEncoder,
    SparseStructuralIndex,
    VersionedBundleCache,
    empty_bundle,
    validate_bundle,
)
from wechat_bridge.contextual_bundle_pipeline import ContextualBundlePipeline
from wechat_bridge.dialogue_bundle import (
    BundleClaim,
    BundleFragment,
    DialogueBundleBuilder,
    build_dialogue_bundles,
)
from wechat_bridge.semantic_gate import (
    CHANNEL_BACKGROUND,
    CHANNEL_COLD_RECOVERABLE,
    CHANNEL_IMMEDIATE,
    CHANNEL_PENDING_CONTEXT,
    SemanticGate,
)
from wechat_bridge.semantic_registry import UNKNOWN, MessageRegistry


def _message(
    message_id: str,
    text: str,
    *,
    account: str = "account-synthetic",
    chat: str = "chat-synthetic",
    speaker: str = "speaker-synthetic",
    sequence: int = 1,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "message_id": message_id,
        "account_id": account,
        "chat_id": chat,
        "chat_type": "direct",
        "speaker_id": speaker,
        "sender_name": "display-name-is-not-authority",
        "content": text,
        "message_type": "text",
        "sequence_in_chat": sequence,
        "time_offset_seconds": sequence,
        "split": "development",
        "source_mode": "live",
    }
    value.update(extra)
    return value


def _fragment(
    fragment_id: str,
    message_id: str,
    text: str,
    *,
    account: str = "account-synthetic",
    chat: str = "chat-synthetic",
    speaker: str = "speaker-synthetic",
    mentioned: tuple[str, ...] = (),
    subject: str = UNKNOWN,
    object_id: str = UNKNOWN,
    object_resolution: str = UNKNOWN,
    object_inherited_from_id: str | None = None,
    state: str = UNKNOWN,
    state_evidence: str = UNKNOWN,
    intent: str = "statement",
    fragment_type: str = "statement",
    actions: tuple[str, ...] = (),
    segment_id: str = "segment-synthetic",
    role: str = "substantive",
    is_opener: bool = False,
    is_silent: bool = False,
    reply_to_message_id: str | None = None,
    topic_shift: bool = False,
    information_value: str = "unknown",
    event_completeness: str = "unknown",
) -> BundleFragment:
    return BundleFragment(
        fragment_id=fragment_id,
        message_id=message_id,
        account_id=account,
        chat_id=chat,
        segment_id=segment_id,
        text=text,
        span_start=0,
        span_end=len(text),
        role=role,
        fragment_type=fragment_type,
        speaker_id=speaker,
        mentioned_person_ids=mentioned,
        subject_id=subject,
        subject_type="person" if subject != UNKNOWN else "unknown",
        object_id=object_id,
        object_resolution=object_resolution,
        object_inherited_from_id=object_inherited_from_id,
        state=state,
        state_evidence=state_evidence,
        closure_reason=state if state in {"resolved", "failed", "cancelled"} else UNKNOWN,
        intent=intent,
        claim_role="question" if intent == "question" else "fact",
        modality="unknown",
        actions=actions,
        topic_shift=topic_shift,
        is_opener=is_opener,
        is_silent=is_silent,
        reply_to_message_id=reply_to_message_id,
        information_value=information_value,
        event_completeness=event_completeness,
    )


def _semantic_bundle(
    bundle_id: str,
    *,
    message_id: str | None = None,
    chat_id: str = "chat-synthetic",
    object_id: str = "object-synthetic",
) -> dict[str, object]:
    message_id = message_id or f"{bundle_id}-message"
    bundle = empty_bundle(
        bundle_id,
        [message_id],
        chat_id=chat_id,
        status="complete",
        source="synthetic",
    )
    bundle["speaker"] = {
        "id": "person-speaker",
        "type": "person",
        "role": "speaker",
        "resolution": "explicit",
        "evidence_ids": [f"{bundle_id}-speaker"],
    }
    bundle["object"] = [
        {
            "id": object_id,
            "type": "object",
            "role": "object",
            "resolution": "explicit",
            "evidence_ids": [f"{bundle_id}-object"],
        }
    ]
    bundle["claim_type"] = "fact"
    bundle["state"] = "ongoing"
    bundle["modality"] = "certain"
    bundle["metadata"]["keywords"] = [object_id, "synthetic-service"]
    bundle["evidence"] = [
        {
            "evidence_id": f"{bundle_id}-speaker",
            "message_id": message_id,
            "span": {"start": 0, "end": 3},
            "field": "speaker",
            "kind": "span",
        },
        {
            "evidence_id": f"{bundle_id}-object",
            "message_id": message_id,
            "span": {"start": 0, "end": 3},
            "field": "object",
            "kind": "span",
        },
        {
            "evidence_id": f"{bundle_id}-claim-type",
            "message_id": message_id,
            "span": {"start": 0, "end": 3},
            "field": "claim_type",
            "kind": "span",
        },
        {
            "evidence_id": f"{bundle_id}-state",
            "message_id": message_id,
            "span": {"start": 0, "end": 3},
            "field": "state",
            "kind": "span",
        },
        {
            "evidence_id": f"{bundle_id}-modality",
            "message_id": message_id,
            "span": {"start": 0, "end": 3},
            "field": "modality",
            "kind": "span",
        },
    ]
    return bundle


class _SyntheticBundleModel:
    def __init__(self, response: dict[str, object] | None = None, *, failures: int = 0) -> None:
        self.response = response
        self.failures = failures
        self.encode_requests: list[dict[str, object]] = []
        self.judge_requests: list[dict[str, object]] = []

    def encode_bundle(self, request: dict[str, object]) -> dict[str, object]:
        self.encode_requests.append(request)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic provider failure")
        return deepcopy(self.response or {})

    def judge_pair(self, request: dict[str, object]) -> dict[str, object]:
        self.judge_requests.append(request)
        left = request["left_bundle"]
        evidence_id = left["evidence"][1]["evidence_id"]
        return {
            "label": "answers",
            "strength": "medium",
            "evidence_ids": [evidence_id],
            "status": "complete",
            "uncertainties": [],
        }


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result: set[str] = set(value)
        for child in value.values():
            result.update(_all_keys(child))
        return result
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for child in value:
            result.update(_all_keys(child))
        return result
    return set()


def _four_channel_inputs() -> tuple[list[dict[str, object]], list[BundleFragment], tuple[str, ...]]:
    channels = (
        CHANNEL_IMMEDIATE,
        CHANNEL_PENDING_CONTEXT,
        CHANNEL_BACKGROUND,
        CHANNEL_COLD_RECOVERABLE,
    )
    messages: list[dict[str, object]] = []
    fragments: list[BundleFragment] = []
    for sequence, channel in enumerate(channels, start=1):
        message_id = f"m-channel-{sequence}"
        text = f"synthetic {channel}"
        messages.append(_message(message_id, text, sequence=sequence, semantic_channel=channel))
        fragments.append(
            _fragment(
                f"f-channel-{sequence}",
                message_id,
                text,
                object_id=f"object-channel-{sequence}",
                object_resolution="explicit",
            )
        )
    return messages, fragments, channels


def test_e2e_registry_metadata_authority_freeze_and_idempotent_replay():
    source = _message(
        "m-registry-authority",
        "synthetic immutable body",
        speaker="speaker-authoritative",
        raw_text="must never cross the public boundary",
        private_sender_id="private-not-read",
    )
    registry = MessageRegistry()
    entry = registry.register(source)

    assert entry.metadata.speaker_id == "speaker-authoritative"
    assert entry.metadata.account_id == "account-synthetic"
    assert entry.metadata.chat_id == "chat-synthetic"
    assert entry.raw_message_ref.get("content") == "synthetic immutable body"
    with pytest.raises(TypeError):
        entry.raw_message_ref["content"] = "mutated"  # type: ignore[index]

    source["content"] = "changed after registration"
    source["speaker_id"] = "spoofed-after-registration"
    assert entry.content == "synthetic immutable body"
    assert entry.metadata.speaker_id == "speaker-authoritative"
    public = entry.to_dict()
    assert "content" not in public
    assert "raw_message" not in public
    assert "private_sender_id" not in public
    assert entry.content_hash == entry.body_digest
    assert entry.record_hash == entry.input_fingerprint
    assert registry.register(dict(source, content="synthetic immutable body", speaker_id="speaker-authoritative")) is entry
    with pytest.raises(ValueError, match="conflicting duplicate"):
        registry.register(dict(source, content="different body", speaker_id="speaker-authoritative"))


def test_e2e_gate_has_four_reversible_channels_and_reactivation_history():
    registry = MessageRegistry()
    gate = SemanticGate(registry)
    requested = (
        ("m-immediate", CHANNEL_IMMEDIATE),
        ("m-pending", CHANNEL_PENDING_CONTEXT),
        ("m-background", CHANNEL_BACKGROUND),
        ("m-cold", CHANNEL_COLD_RECOVERABLE),
    )
    decisions = []
    for sequence, (message_id, channel) in enumerate(requested, start=1):
        entry = registry.register(_message(message_id, f"synthetic-{message_id}", sequence=sequence, semantic_channel=channel))
        decisions.append(gate.route(entry))

    assert tuple(item.channel for item in decisions) == tuple(channel for _, channel in requested)
    assert all(item.reversible for item in decisions)
    assert gate.active_ids() == tuple(message_id for message_id, _ in requested)

    reactivated = gate.reactivate(
        "m-pending",
        channel=CHANNEL_IMMEDIATE,
        reason="synthetic-new-context",
        semantic_signals=("explicit_reply",),
    )
    assert reactivated.previous_channel == CHANNEL_PENDING_CONTEXT
    assert reactivated.from_channel == CHANNEL_PENDING_CONTEXT
    assert reactivated.to_channel == CHANNEL_IMMEDIATE
    assert gate.history("m-pending")[-1] == reactivated
    snapshot = gate.snapshot(force=True)
    assert snapshot.forced_snapshot is True
    assert snapshot.history_count == len(requested) + 1
    assert all(item.reversible for item in snapshot.decisions)


def test_e2e_bundle_replay_hash_is_stable_and_open_snapshot_is_first_class():
    message = _message("m-open", "synthetic open thread")
    fragment = _fragment("f-open", "m-open", "synthetic open thread", object_id="object-open", object_resolution="explicit")
    first = build_dialogue_bundles([message], fragments=[fragment])
    second = build_dialogue_bundles([dict(message)], fragments=[fragment])
    assert first.input_hash == second.input_hash
    assert first.cache_key == second.cache_key
    assert first.to_dict()["schema_version"] == second.to_dict()["schema_version"]

    builder = DialogueBundleBuilder()
    builder.add_fragment(fragment)
    forced = builder.force_snapshot("synthetic-window-limit")
    payload = forced.to_dict()
    assert payload["forced_snapshot"] is True
    assert payload["snapshot_reason"] == "synthetic-window-limit"
    assert "open_context_snapshots" in payload
    assert any(item.open_context_snapshot_id for item in forced.bundles)


def test_e2e_multiscale_windows_cover_w0_to_w4_and_skip_nonsemantic_insertions():
    question = _fragment(
        "f-question",
        "m-question",
        "synthetic question?",
        object_id="object-answer",
        object_resolution="explicit",
        intent="question",
        fragment_type="question",
        segment_id="same-segment",
    )
    opener = _fragment(
        "f-opener",
        "m-opener",
        "你好",
        role="conversation_opener",
        fragment_type="conversation_opener",
        is_opener=True,
        segment_id="same-segment",
    )
    acknowledgement = _fragment(
        "f-ack",
        "m-ack",
        "收到",
        role="context_only",
        fragment_type="acknowledgement",
        segment_id="same-segment",
    )
    answer = _fragment(
        "f-answer",
        "m-answer",
        "synthetic answer",
        object_id="object-answer",
        object_resolution="inherited",
        object_inherited_from_id="f-question",
        intent="answer",
        fragment_type="answer",
        state="resolved",
        state_evidence="explicit",
        segment_id="same-segment",
    )
    cold = _fragment(
        "f-cold",
        "m-cold",
        "synthetic recoverable history",
        object_id="object-cold",
        object_resolution="explicit",
        segment_id="long-gap",
    )
    messages = [
        _message("m-question", "synthetic question?", sequence=1),
        _message("m-opener", "你好", sequence=2),
        _message("m-ack", "收到", sequence=3),
        _message("m-answer", "synthetic answer", sequence=4),
        _message("m-cold", "synthetic recoverable history", sequence=5, semantic_channel=CHANNEL_COLD_RECOVERABLE),
    ]
    result = build_dialogue_bundles(messages, fragments=[question, opener, acknowledgement, answer, cold])
    direct = [item for item in result.relations if item.source_message_ids == ("m-question", "m-answer")]
    assert any(item.label == "answers" for item in direct)
    assert not any(item.label == "answers" and item.source_message_ids in {
        ("m-question", "m-opener"),
        ("m-question", "m-ack"),
    } for item in result.relations)
    window_scales = {item.window_scale for item in result.bundles}
    assert {"W0", "W1", "W2", "W3", "W4"}.issubset(window_scales)


def test_e2e_fragment_and_claim_can_have_multiple_bundle_memberships_with_one_evidence_span():
    first = _fragment(
        "f-a",
        "m-a",
        "synthetic A",
        subject="person-shared",
        object_id="object-a",
        object_resolution="explicit",
        actions=("check",),
    )
    second = _fragment(
        "f-b",
        "m-b",
        "synthetic B",
        subject="person-shared",
        object_id="object-b",
        object_resolution="explicit",
        actions=("check",),
    )
    follow_up = _fragment(
        "f-follow-up",
        "m-follow-up",
        "synthetic follow-up",
        subject="person-shared",
        object_id=UNKNOWN,
        actions=("check",),
    )
    claim = BundleClaim(
        claim_id="claim-follow-up",
        fragment_id="f-follow-up",
        message_id="m-follow-up",
        evidence_span=(0, len("synthetic follow-up")),
        claim_type="fact",
    )
    result = build_dialogue_bundles(
        [
            _message("m-a", "synthetic A", sequence=1),
            _message("m-b", "synthetic B", sequence=2),
            _message("m-follow-up", "synthetic follow-up", sequence=3),
        ],
        fragments=[first, second, follow_up],
        claims=[claim],
    )
    candidates = [item for item in result.bundles if item.scale == "local" and "f-follow-up" in item.fragment_ids]
    assert len(candidates) >= 2
    assert all("claim-follow-up" in item.claim_ids for item in candidates)
    assert len([item for item in result.claims if item.claim_id == "claim-follow-up"]) == 1
    assert result.claims[-1].evidence_refs == (
        {"type": "fragment", "id": "f-follow-up", "span": {"start": 0, "end": len("synthetic follow-up")}},
    )


def test_e2e_person_roles_and_object_resolution_never_collapse_or_guess():
    explicit = _fragment(
        "f-explicit-object",
        "m-explicit-object",
        "synthetic explicit object",
        speaker="person-speaker",
        mentioned=("person-mentioned",),
        subject="person-subject",
        object_id="object-explicit",
        object_resolution="explicit",
    )
    inherited = _fragment(
        "f-inherited-object",
        "m-inherited-object",
        "synthetic inherited object",
        speaker="person-speaker",
        mentioned=("person-mentioned",),
        subject="person-subject",
        object_id="object-explicit",
        object_resolution="inherited",
        object_inherited_from_id="f-explicit-object",
    )
    unknown = _fragment(
        "f-unknown-object",
        "m-unknown-object",
        "synthetic pronoun only",
        speaker="person-speaker",
        mentioned=("person-mentioned",),
        subject="person-subject",
    )
    result = build_dialogue_bundles(
        [
            _message("m-explicit-object", "synthetic explicit object", sequence=1),
            _message("m-inherited-object", "synthetic inherited object", sequence=2),
            _message("m-unknown-object", "synthetic pronoun only", sequence=3),
        ],
        fragments=[explicit, inherited, unknown],
    )
    by_id = {item.fragment_id: item for item in result.fragments}
    assert by_id["f-explicit-object"].speaker_id == "person-speaker"
    assert by_id["f-explicit-object"].mentioned_person_ids == ("person-mentioned",)
    assert by_id["f-explicit-object"].subject_id == "person-subject"
    assert by_id["f-explicit-object"].object_resolution == "explicit"
    assert by_id["f-inherited-object"].object_resolution == "inherited"
    assert by_id["f-inherited-object"].object_inherited_from_id == "f-explicit-object"
    assert by_id["f-unknown-object"].object_id == UNKNOWN
    assert by_id["f-unknown-object"].object_resolution == UNKNOWN

    missing_source = BundleFragment.from_mapping(
        {
            "fragment_id": "f-missing-inherited-source",
            "message_id": "m-missing-inherited-source",
            "account_id": "account-synthetic",
            "chat_id": "chat-synthetic",
            "text": "synthetic omitted object source",
            "object_id": "object-not-proven",
            "object_resolution": "inherited",
        }
    )
    assert missing_source.object_id == UNKNOWN
    assert missing_source.object_resolution == UNKNOWN


def test_e2e_six_state_values_and_open_boundaries_do_not_use_silence_as_resolution():
    states = (UNKNOWN, "planned", "ongoing", "resolved", "failed", "cancelled")
    fragments = [
        _fragment(
            f"f-state-{state}",
            f"m-state-{state}",
            f"synthetic {state}",
            object_id="object-state",
            object_resolution="explicit",
            state=state,
            state_evidence="unknown" if state == UNKNOWN else "explicit",
        )
        for state in states
    ]
    silent = _fragment(
        "f-silent",
        "m-silent",
        "",
        object_id="object-state",
        object_resolution="explicit",
        state="resolved",
        state_evidence="explicit",
        is_silent=True,
        fragment_type="unknown",
    )
    opener = _fragment(
        "f-opener-state",
        "m-opener-state",
        "你好",
        object_id="object-state",
        object_resolution="explicit",
        state="failed",
        state_evidence="explicit",
        role="conversation_opener",
        fragment_type="conversation_opener",
        is_opener=True,
    )
    fragments.extend((silent, opener))
    messages = [
        _message(fragment.message_id, fragment.text, sequence=index + 1, message_type="image" if fragment.is_silent else "text")
        for index, fragment in enumerate(fragments)
    ]
    result = build_dialogue_bundles(messages, fragments=fragments)
    by_id = {item.fragment_id: item for item in result.fragments}
    assert {by_id[item.fragment_id].state for item in fragments[:6]} == set(states)
    assert by_id["f-silent"].state == UNKNOWN
    assert by_id["f-silent"].closure_reason == UNKNOWN
    assert by_id["f-opener-state"].state == UNKNOWN
    # A previously closed session bundle may remain closed while carrying a
    # later silent/opener fragment.  The contract is about the silent/opener
    # evidence itself: their own bundle snapshots must stay open/unknown.
    own_silent_or_opener = [
        item
        for item in result.bundles
        if item.fragment_ids in {("f-silent",), ("f-opener-state",)}
    ]
    assert own_silent_or_opener
    assert all(item.open_boundary for item in own_silent_or_opener)
    assert all(item.start_time_source == UNKNOWN and item.end_time_source == UNKNOWN for item in result.bundles)


def test_e2e_llm_bundle_schema_is_fixed_evidence_checked_and_unknown_is_valid():
    response = _semantic_bundle("llm-bundle", message_id="m-llm")
    response["narrative"] = "synthetic free text must be stripped"
    model = _SyntheticBundleModel(response)
    cache = VersionedBundleCache()
    pipeline = BundleSemanticPipeline(model=model, cache=cache, model_version="synthetic-model", max_retries=0)
    messages = [{"message_id": "m-llm", "chat_id": "chat-synthetic", "content": "synthetic body"}]

    first = pipeline.encode(messages, bundle_id="llm-bundle")
    second = pipeline.encode(messages, bundle_id="llm-bundle")
    assert first.status == second.status == "complete"
    assert first.validation.ok is True
    assert second.validation.ok is True
    assert len(model.encode_requests) == 1
    assert model.encode_requests[0]["response_schema"]["required"] == list(BUNDLE_FIELDS)
    assert first.bundle["subject"]["id"] == UNKNOWN
    assert first.bundle["subject"]["resolution"] == UNKNOWN
    assert not _all_keys(first.bundle) & {"body", "text", "raw_text", "narrative", "prompt", "response"}
    assert first.input_sha256 == second.input_sha256
    assert first.cache_key == second.cache_key


def test_e2e_embedding_is_recall_only_and_cross_chat_is_blocked():
    left = _semantic_bundle("bundle-left", message_id="m-left", chat_id="chat-one", object_id="object-left")
    other = _semantic_bundle("bundle-other", message_id="m-other", chat_id="chat-two", object_id="object-dense")
    other["state"] = UNKNOWN
    other["claim_type"] = UNKNOWN
    other["modality"] = UNKNOWN
    other["speaker"] = {
        "id": "person-other",
        "type": "person",
        "role": "speaker",
        "resolution": "unknown",
        "evidence_ids": [],
    }

    class _SyntheticEmbedder:
        def __init__(self) -> None:
            self.calls = 0

        def embed(self, value: dict[str, object]) -> tuple[float, float]:
            self.calls += 1
            return (1.0, 0.0) if value.get("bundle_id") in {"bundle-other", "bundle-query"} else (0.0, 1.0)

    embedder = _SyntheticEmbedder()
    index = SparseStructuralIndex([left, other], embedder=embedder)
    query = _semantic_bundle("bundle-query", message_id="m-query", chat_id="chat-query", object_id="object-query")
    other["metadata"]["keywords"] = ["denseonly"]
    query["metadata"]["keywords"] = ["queryonly"]
    # The index stores a defensive copy on add; replace the source bundle in
    # the fixture before querying only to keep the intent explicit.
    index = SparseStructuralIndex([left, other], embedder=embedder)
    results = index.retrieve(query, top_k=3, allow_cross_chat=True)
    dense_only = next(item for item in results if item.bundle_id == "bundle-other")
    assert dense_only.dense_only is True
    assert dense_only.sources == ("dense_recall",)
    assert dense_only.score == 0.0
    assert dense_only.dense_score > 0.0

    model = _SyntheticBundleModel()
    pipeline = BundleSemanticPipeline(model=model, max_retries=0)
    judgement, report = pipeline.judge_pair(left, other)
    assert judgement.label == "insufficient"
    assert judgement.status == "pending"
    assert report.ok is False
    assert "cross_chat_link_forbidden" in report.errors


def test_e2e_time_only_same_segment_and_silence_do_not_create_strong_links():
    time_left = _fragment(
        "f-time-left",
        "m-time-left",
        "synthetic time left",
        segment_id="same-segment",
        object_id=UNKNOWN,
    )
    time_right = _fragment(
        "f-time-right",
        "m-time-right",
        "synthetic time right",
        segment_id="same-segment",
        object_id=UNKNOWN,
    )
    cross_chat = replace(
        time_right,
        fragment_id="f-cross-chat",
        message_id="m-cross-chat",
        chat_id="chat-other",
        object_id="object-shared",
        object_resolution="explicit",
    )
    result = build_dialogue_bundles(
        [
            _message("m-time-left", "synthetic time left", sequence=10),
            _message("m-time-right", "synthetic time right", sequence=11),
            _message("m-cross-chat", "synthetic cross chat", chat="chat-other", sequence=12),
        ],
        fragments=[time_left, time_right, cross_chat],
    )
    assert not any(item.source_message_ids == ("m-time-left", "m-time-right") for item in result.relations)
    assert not any("m-cross-chat" in item.source_message_ids for item in result.relations)
    assert not any(item.evidence_strength == "strong" and item.time_evidence == "weak" and not item.supporting_slot_codes for item in result.relations)

    bundle = _semantic_bundle("bundle-time-validation", message_id="m-time-validation")
    bundle["context_relations"] = [
        {
            "relation_id": "relation-time-only",
            "source_bundle_id": "bundle-time-validation",
            "target_bundle_id": "bundle-other",
            "label": "continues",
            "strength": "strong",
            "evidence_ids": [],
            "supporting_signals": ["time"],
            "left_chat_id": "chat-synthetic",
            "right_chat_id": "chat-other",
        }
    ]
    report = validate_bundle(bundle)
    assert report.ok is False
    assert "context_relation_0_time_only_strong_forbidden" in report.errors
    assert "context_relation_0_cross_chat" in report.errors


def test_e2e_model_failure_returns_pending_unknown_without_losing_hash():
    model = _SyntheticBundleModel(failures=4)
    pipeline = BundleSemanticPipeline(model=model, max_retries=1)
    messages = [{"message_id": "m-pending", "chat_id": "chat-synthetic", "content": "synthetic provider input"}]
    first = pipeline.encode(messages, bundle_id="pending-bundle")
    second = pipeline.encode(messages, bundle_id="pending-bundle")
    assert first.status == second.status == "pending"
    assert first.validation.ok is True
    assert first.bundle["claim_type"] == UNKNOWN
    assert first.bundle["state"] == UNKNOWN
    assert first.bundle["metadata"]["status"] == "pending"
    assert first.input_sha256 == second.input_sha256
    assert first.cache_key == second.cache_key
    assert first.stats["model_calls"] == 2
    assert first.stats["model_retries"] == 1
    assert first.stats["fallback_calls"] == 0


def test_e2e_budget_14_280_is_explicit_and_window_does_not_promote_old_context():
    builder = DialogueBundleBuilder(window_size=14, max_candidates=280)
    assert builder.window_size == 14
    assert builder.max_candidates == 280

    anchor = _fragment(
        "f-budget-anchor",
        "m-budget-anchor",
        "synthetic anchor",
        object_id="object-budget",
        object_resolution="explicit",
    )
    fillers = [
        _fragment(
            f"f-budget-filler-{index}",
            f"m-budget-filler-{index}",
            f"synthetic filler {index}",
            object_id=f"object-filler-{index}",
            object_resolution="explicit",
        )
        for index in range(14)
    ]
    answer = _fragment(
        "f-budget-answer",
        "m-budget-answer",
        "synthetic late answer",
        object_id="object-budget",
        object_resolution="inherited",
        object_inherited_from_id="f-budget-anchor",
        intent="answer",
        fragment_type="answer",
    )
    all_fragments = [anchor, *fillers, answer]
    result = builder.ingest(
        [
            _message(fragment.message_id, fragment.text, sequence=index + 1)
            for index, fragment in enumerate(all_fragments)
        ],
        fragments=all_fragments,
    )
    assert not any(
        item.label == "answers" and item.source_message_ids == ("m-budget-anchor", "m-budget-answer")
        for item in result.relations
    )


def test_e2e_integration_pipeline_replay_is_body_free_and_metadata_authoritative():
    messages, fragments, channels = _four_channel_inputs()
    first = ContextualBundlePipeline(
        mode="disabled",
        window_size=14,
        max_candidates=280,
    ).run(messages, fragments=fragments, split="development")
    replay_messages = [dict(message, sender_name="synthetic-spoofed-display-name") for message in messages]
    replay = ContextualBundlePipeline(
        mode="disabled",
        window_size=14,
        max_candidates=280,
    ).run(replay_messages, fragments=fragments, split="development")

    assert first.input_sha256 == replay.input_sha256
    assert first.manifest["replay_key"] == replay.manifest["replay_key"]
    assert first.registry_snapshot["input_hash"] == replay.registry_snapshot["input_hash"]
    assert first.gate_snapshot["history_count"] == len(messages)
    assert {item["channel"] for item in first.gate_snapshot["decisions"]} == set(channels)
    assert {item["channel"] for item in first.decisions} == set(channels)
    assert first.dialogue_snapshot["bundle_count"] == len(first.bundles) == len(first.decisions)
    assert all("open_context_snapshot_id" in item for item in first.bundles)
    assert all(item["validation"]["ok"] for item in first.decisions)
    assert first.manifest["body_free_outputs"] is True
    assert not _all_keys(first.artifacts()) & {
        "content",
        "text",
        "body",
        "raw",
        "raw_text",
        "raw_message",
        "private_sender_id",
        "sender_name",
        "prompt",
        "response",
    }
    assert first.artifacts() == replay.artifacts()


def test_e2e_integration_pipeline_cache_replay_skips_model_and_keeps_hashes():
    class _CacheReplayModel:
        model_version = "synthetic-cache-model"

        def __init__(self) -> None:
            self.calls = 0

        def encode_bundle(self, request: dict[str, object]) -> dict[str, object]:
            self.calls += 1
            message_ids = [item["message_id"] for item in request["messages"]]
            return empty_bundle(
                request["bundle_id"],
                message_ids,
                chat_id=request["chat_id"],
                status="complete",
                source="synthetic-cache-model",
            )

    message = _message("m-cache-replay", "synthetic cache replay")
    fragment = _fragment(
        "f-cache-replay",
        "m-cache-replay",
        "synthetic cache replay",
        object_id="object-cache-replay",
        object_resolution="explicit",
    )
    model = _CacheReplayModel()
    pipeline = ContextualBundlePipeline(
        mode="fake",
        model=model,
        cache=VersionedBundleCache(),
        max_input_tokens=100000,
        max_output_tokens=100000,
    )
    first = pipeline.run([message], fragments=[fragment], split="development")
    replay = pipeline.run([dict(message)], fragments=[fragment], split="development")

    assert first.input_sha256 == replay.input_sha256
    assert first.manifest["replay_key"] == replay.manifest["replay_key"]
    assert first.cost["semantic_stats"]["model_calls"] == len(first.decisions)
    assert model.calls == len(first.decisions)
    assert replay.cost["semantic_stats"]["model_calls"] == 0
    assert replay.cost["semantic_stats"]["cache_hits"] == len(replay.decisions)
    assert replay.cost["budget"]["cache_hits"] == len(replay.decisions)
    assert all(item["status"] == "complete" for item in replay.decisions)


def test_e2e_integration_pipeline_never_bridges_chat_scopes():
    messages = [
        _message("m-chat-one", "synthetic chat one", chat="chat-one", sequence=1),
        _message("m-chat-two", "synthetic chat two", chat="chat-two", sequence=2),
    ]
    fragments = [
        _fragment(
            "f-chat-one",
            "m-chat-one",
            "synthetic chat one",
            chat="chat-one",
            object_id="object-one",
            object_resolution="explicit",
        ),
        _fragment(
            "f-chat-two",
            "m-chat-two",
            "synthetic chat two",
            chat="chat-two",
            object_id="object-two",
            object_resolution="explicit",
        ),
    ]
    result = ContextualBundlePipeline(mode="disabled").run(messages, fragments=fragments, split="development")
    message_chats = {item["message_id"]: item["chat_id"] for item in messages}

    assert result.relations == ()
    assert result.errors == ()
    for bundle in result.bundles:
        source_chats = {message_chats[message_id] for message_id in bundle["source_message_ids"]}
        assert len(source_chats) <= 1
        assert bundle["chat_scope"]["chat_ids"] == sorted(source_chats)
        assert bundle["cross_chat_bridge_refs"] == []
    for decision in result.decisions:
        assert {message_chats[message_id] for message_id in decision["message_ids"]} == {decision["chat_id"]}


def test_e2e_integration_pipeline_budget_cap_and_failure_return_pending_unknown():
    messages, fragments, _ = _four_channel_inputs()
    model = _SyntheticBundleModel(failures=100)
    pipeline = ContextualBundlePipeline(
        mode="fake",
        model=model,
        max_bundle_calls=14,
        max_input_tokens=2000,
        max_output_tokens=400,
        max_retries=1,
        window_size=14,
        max_candidates=280,
    )
    result = pipeline.run(messages, fragments=fragments, split="development")
    budget = result.cost["budget"]

    assert pipeline.window_size == 14
    assert pipeline.max_candidates == 280
    assert budget["max_bundle_calls"] == 14
    assert budget["max_input_tokens"] == 2000
    assert budget["max_output_tokens"] == 400
    assert budget["calls_used"] == 14
    assert len(model.encode_requests) == 14
    assert budget["rejection_count"] > 0
    assert result.cost["pending_count"] == len(result.decisions)
    assert all(item["status"] == "pending" for item in result.decisions)
    assert all(item["semantic_bundle"]["claim_type"] == UNKNOWN for item in result.decisions)
    assert all(item["semantic_bundle"]["state"] == UNKNOWN for item in result.decisions)
    assert all(item["semantic_bundle"]["metadata"]["status"] == "pending" for item in result.decisions)
    assert any(item["source"] == "budget" for item in result.requests)
    assert any(item["code"] == "bundle_call_budget_exhausted" for item in result.errors)
