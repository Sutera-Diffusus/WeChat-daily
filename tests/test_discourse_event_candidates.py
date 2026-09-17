"""Public synthetic checks for Workstream D thread/event derivation.

The fixtures are invented mappings/DTOs only.  They do not load production,
private, frozen or frontend artifacts.
"""

from __future__ import annotations

from dataclasses import replace

from wechat_bridge.dialogue_bundle import (
    BundleClaim,
    BundleFragment,
    ContextRelationCandidate,
    DialogueBundleResult,
    REL_CONTINUES,
    build_dialogue_bundles,
)
from wechat_bridge.discourse_event_candidates import (
    EventDerivationConfig,
    ShadowSemanticStore,
    build_discourse_threads,
    derive_discourse_and_events,
    legacy_fallback_marker,
)


def _message(message_id: str, text: str, *, chat: str = "chat-synth", sequence: int = 1) -> dict[str, object]:
    return {
        "message_id": message_id,
        "account_id": "account-synth",
        "chat_id": chat,
        "speaker_id": "speaker-synth",
        "content": text,
        "message_type": "text",
        "sequence_in_chat": sequence,
        "time_offset_seconds": sequence,
        "split": "development",
    }


def _fragment(
    fragment_id: str,
    message_id: str,
    text: str,
    *,
    subject: str = "SUBJECT_SYNTH",
    object_id: str = "OBJECT_SYNTH",
    state: str = "ongoing",
    actions: tuple[str, ...] = ("check",),
    chat: str = "chat-synth",
    opener: bool = False,
    silent: bool = False,
    evidence: bool = True,
    uncertainties: tuple[str, ...] = (),
) -> BundleFragment:
    refs = (
        {"type": "fragment", "id": fragment_id, "span": {"start": 0, "end": len(text)}},
    ) if evidence and not silent else ()
    object_refs = refs if object_id != "unknown" and not silent else ()
    return BundleFragment(
        fragment_id=fragment_id,
        message_id=message_id,
        account_id="account-synth",
        chat_id=chat,
        segment_id="segment-synth",
        text=text,
        span_start=0,
        span_end=len(text),
        role="conversation_opener" if opener else "substantive",
        fragment_type="conversation_opener" if opener else "statement",
        speaker_id="speaker-synth",
        subject_id=subject,
        subject_type="person",
        object_id=object_id,
        object_resolution="explicit" if object_id != "unknown" else "unknown",
        object_evidence_refs=object_refs,
        state=state,
        state_evidence="explicit" if state != "unknown" else "unknown",
        closure_reason=state if state in {"resolved", "failed", "cancelled"} else "unknown",
        actions=actions,
        is_opener=opener,
        is_silent=silent,
        information_value="high" if not opener and not silent else "low",
        event_completeness="sufficient" if not opener and not silent else "not_applicable",
        evidence_refs=refs,
        uncertainties=uncertainties,
    )


def _result(*fragments: BundleFragment, messages: list[dict[str, object]] | None = None) -> DialogueBundleResult:
    messages = messages or [_message(fragment.message_id, fragment.text, chat=fragment.chat_id, sequence=index + 1) for index, fragment in enumerate(fragments)]
    return build_dialogue_bundles(messages, fragments=fragments)


def test_thread_stays_open_with_unknown_boundaries_and_no_event_for_opener_or_silence():
    opener = _fragment("f-opener", "m-opener", "hello", opener=True)
    silent = _fragment("f-silent", "m-silent", "", silent=True, subject="unknown", object_id="unknown", state="unknown", actions=())
    result = derive_discourse_and_events(_result(opener, silent), config=EventDerivationConfig(materialize_events=True))

    assert result.event_candidates == ()
    assert len(result.threads) == 2
    assert all(thread.open_boundary for thread in result.threads)
    assert all(thread.start_fragment_id == "unknown" for thread in result.threads)
    assert all(thread.end_fragment_id == "unknown" for thread in result.threads)
    assert all(thread.closure_reason == "unknown" for thread in result.threads)


def test_sufficient_subject_object_action_state_and_evidence_can_materialize_event():
    fragment = _fragment("f-sufficient", "m-sufficient", "synthetic issue ongoing")
    result = derive_discourse_and_events(
        _result(fragment),
        config=EventDerivationConfig(materialize_events=True),
    )

    assert len(result.event_candidates) == 1
    event = result.event_candidates[0]
    assert event.thread_id in {thread.discourse_thread_id for thread in result.threads}
    assert event.subject_id == "SUBJECT_SYNTH"
    assert event.object_id == "OBJECT_SYNTH"
    assert event.actions == ("check",)
    assert event.state == "ongoing"
    assert event.evidence_refs
    assert event.bundle_ids
    assert event.fragment_ids == ("f-sufficient",)
    assert event.source_message_ids == ("m-sufficient",)
    assert "event_id" not in result.to_dict()


def test_unknown_slot_or_missing_evidence_abstains_without_guessing():
    unknown_subject = _fragment("f-unknown-subject", "m-unknown-subject", "object only", subject="unknown")
    missing_evidence = _fragment("f-missing-evidence", "m-missing-evidence", "no evidence", evidence=False)
    # Keep this regression at the D boundary: the A builder is allowed to
    # synthesize a message-level fallback evidence ref, so remove those
    # derived records and pass only the intentionally incomplete fragments.
    base = _result(unknown_subject, missing_evidence)
    incomplete = replace(base, claims=(), bundles=(), relations=(), fragments=(unknown_subject, missing_evidence))
    result = derive_discourse_and_events(
        incomplete,
        config=EventDerivationConfig(materialize_events=True),
    )

    assert result.event_candidates == ()
    reasons = {code for thread in result.threads for code in thread.uncertainties}
    assert "subject_unknown" in reasons
    assert "evidence_insufficient" in reasons


def test_unknown_object_is_retained_on_open_thread_but_never_guessed_into_event():
    fragment = _fragment(
        "f-unknown-object",
        "m-unknown-object",
        "pronoun only",
        object_id="unknown",
    )
    derived = derive_discourse_and_events(
        _result(fragment),
        config=EventDerivationConfig(materialize_events=True),
    )

    assert derived.event_candidates == ()
    assert derived.threads[0].object_refs
    assert derived.threads[0].object_refs[0]["resolution"] == "unknown"
    assert derived.threads[0].object_refs[0]["id"] == "unknown"


def test_context_only_fragment_remains_thread_context_and_cannot_be_event():
    fragment = _fragment("f-context", "m-context", "ack", subject="SUBJECT_SYNTH")
    fragment = replace(fragment, role="context_only", fragment_type="acknowledgement")
    derived = derive_discourse_and_events(
        _result(fragment),
        config=EventDerivationConfig(materialize_events=True),
    )

    assert derived.event_candidates == ()
    assert "context_only" in derived.threads[0].uncertainties
    assert "context_only_fragment" in derived.threads[0].uncertainties


def test_explicit_terminal_state_and_typed_evidence_close_thread_without_inference():
    fragment = _fragment(
        "f-resolved",
        "m-resolved",
        "synthetic issue resolved",
        state="resolved",
    )
    derived = derive_discourse_and_events(
        _result(fragment),
        config=EventDerivationConfig(materialize_events=True),
    )

    thread = derived.threads[0]
    assert thread.closure_reason == "resolved"
    assert thread.status == "closed"
    assert thread.open_boundary is False
    assert derived.event_candidates[0].state == "resolved"


def test_time_same_segment_embedding_and_greeting_cannot_overmerge_or_create_event():
    first = _fragment("f-first", "m-first", "same theme", object_id="OBJECT_A", actions=("check",))
    second = _fragment("f-second", "m-second", "same theme", object_id="OBJECT_B", actions=("check",))
    # A bundle-shaped mapping may carry recall hints, but those hints are not
    # semantic evidence for thread/event identity.
    first = replace(first, uncertainties=("embedding_match", "same_segment", "time_close"))
    second = replace(second, uncertainties=("embedding_match", "same_segment", "time_close"))
    result = derive_discourse_and_events(
        _result(first, second),
        config=EventDerivationConfig(materialize_events=True),
    )

    assert len(result.threads) == 2
    assert len(result.event_candidates) == 2
    assert all(event.fragment_ids in {("f-first",), ("f-second",)} for event in result.event_candidates)
    assert all(len(event.object_id) > 0 for event in result.event_candidates)


def test_canonical_relation_without_semantic_support_cannot_join_thread():
    first = _fragment("f-weak-first", "m-weak-first", "first", object_id="OBJECT_A")
    second = _fragment("f-weak-second", "m-weak-second", "second", object_id="OBJECT_A")
    weak_only = ContextRelationCandidate(
        context_relation_id="relation-weak-only",
        left_anchor_id=first.fragment_id,
        right_anchor_id=second.fragment_id,
        label=REL_CONTINUES,
        supporting_slot_codes=("same_segment", "time_proximity_weak"),
        evidence_refs=(
            {"type": "fragment", "id": first.fragment_id, "span": {"start": 0, "end": 5}},
            {"type": "fragment", "id": second.fragment_id, "span": {"start": 0, "end": 6}},
        ),
        provenance={"same_segment": True, "time_is_weak_only": True},
    )
    result = replace(_result(first, second), relations=(weak_only,))
    derived = derive_discourse_and_events(
        result,
        config=EventDerivationConfig(materialize_events=True),
    )

    assert len(derived.threads) == 2
    assert all(thread.fragment_ids in {("f-weak-first",), ("f-weak-second",)} for thread in derived.threads)
    assert all(event.fragment_ids in {("f-weak-first",), ("f-weak-second",)} for event in derived.event_candidates)


def test_legacy_relation_alias_is_normalized_only_with_typed_semantic_support():
    first = _fragment("f-alias-first", "m-alias-first", "first", object_id="OBJECT_A")
    second = _fragment("f-alias-second", "m-alias-second", "second", object_id="OBJECT_A")
    alias = {
        "relation_id": "relation-alias",
        "left_anchor_id": first.fragment_id,
        "right_anchor_id": second.fragment_id,
        "relation": "continuation",
        "supporting_slot_codes": ["shared_object"],
        "evidence_refs": [
            {"type": "fragment", "id": first.fragment_id, "span": {"start": 0, "end": 5}},
            {"type": "fragment", "id": second.fragment_id, "span": {"start": 0, "end": 6}},
        ],
    }
    result = replace(_result(first, second), relations=(alias,))
    derived = derive_discourse_and_events(
        result,
        config=EventDerivationConfig(materialize_events=True),
    )

    assert len(derived.threads) == 1
    assert derived.threads[0].fragment_ids == ("f-alias-first", "f-alias-second")
    assert len(derived.event_candidates) == 1


def test_mnl_and_cross_chat_conflicts_block_event_derivation():
    left = _fragment("f-left", "m-left", "left", object_id="OBJECT_A")
    right = _fragment("f-right", "m-right", "right", object_id="OBJECT_A", uncertainties=("must_not_link",))
    relation = ContextRelationCandidate(
        context_relation_id="relation-cross-chat",
        left_anchor_id="f-left",
        right_anchor_id="f-cross",
        label="continues",
        evidence_refs=(),
        evidence_message_ids=("m-left", "m-cross"),
        provenance={"same_segment": True},
    )
    cross = _fragment("f-cross", "m-cross", "cross", object_id="OBJECT_A", chat="other-chat")
    result = _result(left, right, cross)
    result = replace(result, relations=(relation,))
    derived = derive_discourse_and_events(result, config=EventDerivationConfig(materialize_events=True))

    assert len(derived.threads) >= 2
    assert derived.event_candidates == ()
    assert any("cross_chat_conflict" in thread.uncertainties or "must_not_link" in thread.uncertainties for thread in derived.threads)


def test_model_pending_or_fallback_never_materializes_event_by_default():
    fragment = _fragment("f-model", "m-model", "model pending")
    result = derive_discourse_and_events(
        _result(fragment),
        config=EventDerivationConfig(materialize_events=True, model_status="pending"),
    )
    fallback = derive_discourse_and_events(
        _result(fragment),
        config=EventDerivationConfig(materialize_events=True, model_status="fallback"),
    )

    assert result.event_candidates == ()
    assert fallback.event_candidates == ()
    assert all("model_pending" in code or "model_fallback" in code for code in {item for thread in result.threads for item in thread.uncertainties} | {item for thread in fallback.threads for item in thread.uncertainties})


def test_shadow_store_is_idempotent_and_legacy_marker_is_explicit():
    fragment = _fragment("f-store", "m-store", "stored")
    result = derive_discourse_and_events(_result(fragment), config=EventDerivationConfig(materialize_events=True))
    store = ShadowSemanticStore()
    store.append(result)
    store.append(result)

    assert len(store.threads()) == len(result.threads)
    assert len(store.events()) == len(result.event_candidates)
    marker = legacy_fallback_marker()
    assert marker["source"] == "semantic_v2_shadow"
    assert marker["fallback_source"] == "legacy_fallback"
    assert "events" not in store.to_dict()
