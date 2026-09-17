"""Independent synthetic contract tests for conversation reconstruction.

These tests intentionally describe the new product boundary in terms of
messages, open conversation threads, and reviewable views.  They do not use
the legacy topic/event pipeline and do not read a private or frozen artifact.
All bodies, identities, timestamps, and media markers below are synthetic.

The implementation is deliberately loaded through one small compatibility
helper.  The helper is the only place that should need an edit once the new
public API is finalized; the contract assertions must remain unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any

import pytest


# Keep the adaptation boundary in one place.  The expected public API is a
# pure, offline entry point that returns a mapping or a DTO with ``to_dict``.
# No provider is passed and no source/database path is read by this module.
_PUBLIC_MODULE = "wechat_bridge.context_reconstruction"
_PUBLIC_ENTRYPOINT = "reconstruct_context"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(child) for child in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    return value


def _reconstruct(messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Call the single public reconstruction entry point.

    A missing API is a hard test failure, not a skip: these tests are intended
    to become a release gate as soon as the implementation is published.
    """

    try:
        module = import_module(_PUBLIC_MODULE)
        entrypoint = getattr(module, _PUBLIC_ENTRYPOINT)
    except (ImportError, AttributeError) as exc:  # pragma: no cover - red gate
        pytest.fail(
            "public conversation reconstruction API is not available yet: "
            f"{_PUBLIC_MODULE}.{_PUBLIC_ENTRYPOINT} ({exc})"
        )
    # The stable API is provider-free and names the review date explicitly.
    # These two translations are the one-time compatibility seam for the
    # original contract fixture vocabulary.
    call_kwargs = dict(kwargs)
    call_kwargs.pop("mode", None)
    if "as_of" in call_kwargs:
        call_kwargs["reference_date"] = call_kwargs.pop("as_of")
    # Original text is required only for this local synthetic review.  The
    # production-facing/default projection remains body-free by default.
    call_kwargs["include_bodies"] = True
    result = entrypoint(tuple(messages), **call_kwargs)
    value = _jsonable(result)
    if not isinstance(value, Mapping):
        pytest.fail("reconstruction result must be a mapping or a to_dict DTO")
    return _adapt_result(dict(value), tuple(messages), module)


def _adapt_result(
    result: dict[str, Any],
    source_messages: Sequence[Mapping[str, Any]],
    module: Any,
) -> dict[str, Any]:
    """Normalize stable reconstruction IDs into source-message test IDs.

    The reconstruction API intentionally uses opaque ``message_ref`` values
    in candidate artifacts.  Contract assertions speak in authoritative
    source IDs, so this helper joins only through the API's public ledger.
    """

    message_rows = [dict(row) for row in _rows(result, "messages")]
    ref_to_source = {
        str(row.get("message_ref")): str(row.get("source_message_id"))
        for row in message_rows
        if row.get("message_ref") and row.get("source_message_id")
    }
    body_by_ref: dict[str, str] = {}
    review = result.get("review") if isinstance(result.get("review"), Mapping) else {}
    for row in review.get("source_messages") or ():
        if isinstance(row, Mapping) and row.get("message_ref"):
            body_by_ref[str(row["message_ref"])] = _message_text(dict(row))
    chat_type_by_ref = {
        str(row.get("chat_ref")): str(row.get("chat_type"))
        for row in _rows(result, "chats")
        if row.get("chat_ref")
    }
    normalized_messages: list[dict[str, Any]] = []
    for row in message_rows:
        value = dict(row)
        source_id = str(row.get("source_message_id") or "")
        message_ref = str(row.get("message_ref") or "")
        value["message_id"] = source_id
        value["content"] = body_by_ref.get(message_ref, "")
        value["chat_type"] = chat_type_by_ref.get(str(row.get("chat_ref")), row.get("explicit_chat_type"))
        value["sequence_in_chat"] = row.get("sequence")
        media = row.get("media") if isinstance(row.get("media"), Mapping) else {}
        value["availability"] = media.get("status")
        value["evidence_eligible"] = media.get("semantic_evidence_eligible")
        normalized_messages.append(value)
    result["messages"] = normalized_messages

    normalized_threads: list[dict[str, Any]] = []
    for raw_thread in _thread_rows(result):
        thread = dict(raw_thread)
        refs = [str(value) for value in thread.get("message_refs") or ()]
        thread["thread_id"] = str(thread.get("thread_ref") or thread.get("thread_id") or "")
        thread["message_ids"] = [ref_to_source.get(ref, ref) for ref in refs]
        normalized_threads.append(thread)
    result["threads"] = normalized_threads

    views = result.get("views")
    if isinstance(views, Mapping):
        normalized_views: dict[str, Any] = {}
        for name, raw_view in views.items():
            view = dict(raw_view) if isinstance(raw_view, Mapping) else {}
            refs: list[str] = []
            for candidate in view.get("thread_candidates") or ():
                if not isinstance(candidate, Mapping):
                    continue
                refs.extend(str(value) for value in candidate.get("included_message_refs") or ())
                refs.extend(str(value) for value in candidate.get("context_message_refs") or ())
            view["message_ids"] = [ref_to_source.get(ref, ref) for ref in dict.fromkeys(refs)]
            view["thread_ids"] = list(view.get("thread_refs") or ())
            normalized_views[str(name)] = view
        result["views"] = normalized_views

    # Segments are the public fragment-level candidates in this prototype.
    # Add source IDs and exact spans for the test seam without changing the
    # production implementation or claiming model-level text semantics.
    normalized_fragments: list[dict[str, Any]] = []
    for raw_segment in _rows(result, "segments"):
        fragment = dict(raw_segment)
        message_ref = str(fragment.get("message_ref") or "")
        fragment["message_id"] = ref_to_source.get(message_ref, message_ref)
        span = fragment.get("span") if isinstance(fragment.get("span"), Mapping) else {}
        start, end = int(span.get("start") or 0), int(span.get("end") or 0)
        body = body_by_ref.get(message_ref, "")
        fragment["text"] = body[start:end]
        normalized_fragments.append(fragment)
    result["fragments"] = normalized_fragments

    # Evidence in the prototype is intentionally an opaque message_ref.  A
    # private helper key makes the test's negative media assertion compare
    # against authoritative source IDs without treating every opaque token as
    # an ID.  It is never emitted by the implementation or written to an
    # artifact.
    evidence_refs: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).casefold() in {
                    "evidence_refs",
                    "evidence_ids",
                    "supporting_evidence_refs",
                    "conflicting_evidence_refs",
                }:
                    if isinstance(child, Mapping):
                        child = (child,)
                    if isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                        for ref in child:
                            if isinstance(ref, Mapping):
                                for ref_key in ("message_ref", "message_id", "source_message_id", "id"):
                                    if ref.get(ref_key):
                                        evidence_refs.add(str(ref[ref_key]))
                            elif ref not in (None, ""):
                                evidence_refs.add(str(ref))
                collect(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                collect(child)

    collect(result)
    result["_evidence_source_ids"] = sorted({ref_to_source.get(ref, ref) for ref in evidence_refs})
    renderer = getattr(module, "render_review_html", None)
    if callable(renderer):
        result["_review_html"] = renderer(result, source_messages)
    return result


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _rows(result: Mapping[str, Any], name: str) -> tuple[dict[str, Any], ...]:
    values = result.get(name, ())
    if isinstance(values, Mapping):
        # A keyed registry is accepted as a public projection, but each row
        # still has to carry its own stable identifier in the assertions.
        values = tuple(values.values())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(dict(_jsonable(item)) for item in values if isinstance(_jsonable(item), Mapping))


def _message_rows(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    return _rows(result, "messages")


def _thread_rows(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    # ``threads`` is the contract name.  ``conversation_threads`` is accepted
    # only as a DTO serialization alias so a final API rename is one helper
    # edit, not a rewrite of the contract tests.
    values = result.get("threads")
    if values is None:
        values = result.get("conversation_threads", ())
    if isinstance(values, Mapping):
        values = tuple(values.values())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(dict(_jsonable(item)) for item in values if isinstance(_jsonable(item), Mapping))


def _thread_message_ids(thread: Mapping[str, Any]) -> set[str]:
    values = (
        thread.get("message_ids")
        or thread.get("member_message_ids")
        or thread.get("source_message_ids")
        or ()
    )
    return {str(value) for value in values if value not in (None, "")}


def _message_id(row: Mapping[str, Any]) -> str:
    return str(row.get("message_id") or row.get("id") or "")


def _message_text(row: Mapping[str, Any]) -> str:
    return str(row.get("content") or row.get("text") or row.get("message_text") or "")


def _message_by_id(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {_message_id(row): row for row in _message_rows(result) if _message_id(row)}


def _view_mapping(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("views")
    if isinstance(value, Mapping):
        return value
    pytest.fail("reconstruction result must expose views.today/yesterday/week")


def _view_ids(view: Any, key: str = "message_ids") -> set[str]:
    if isinstance(view, Mapping):
        values = view.get(key)
        if values is None and key == "message_ids":
            values = view.get("members") or view.get("source_message_ids")
        if values is None and key == "thread_ids":
            values = view.get("threads")
    else:
        values = ()
    if isinstance(values, Mapping):
        values = values.keys()
    return {str(value) for value in (values or ()) if value not in (None, "")}


def _run_meta(result: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("run", "metadata", "manifest"):
        value = result.get(key)
        if isinstance(value, Mapping) and any(
            name in value
            for name in ("provider_calls", "production_blocked", "stage_b", "stage_c")
        ):
            return value
    return result


def _evidence_message_ids(value: Any) -> set[str]:
    """Collect message IDs only from explicitly typed evidence fields."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        source_ids = value.get("_evidence_source_ids")
        if isinstance(source_ids, Sequence) and not isinstance(source_ids, (str, bytes)):
            found.update(str(item) for item in source_ids if item not in (None, ""))
        for key, child in value.items():
            name = str(key).casefold()
            if name in {
                "evidence_refs",
                "evidence_ids",
                "supporting_evidence_refs",
                "conflicting_evidence_refs",
                "supporting_evidence",
            }:
                if isinstance(child, Mapping):
                    child = (child,)
                if isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
                    for ref in child:
                        if isinstance(ref, Mapping):
                            message_id = ref.get("message_id") or ref.get("source_message_id")
                            if message_id:
                                found.add(str(message_id))
                            elif ref.get("type") in {"message", "span", "reply", "quote"} and ref.get("id"):
                                found.add(str(ref["id"]))
                        elif isinstance(ref, str) and ref.startswith("message:"):
                            found.add(ref.split(":", 1)[1])
                        elif isinstance(ref, str):
                            found.add(ref)
            found.update(_evidence_message_ids(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found.update(_evidence_message_ids(child))
    return found


def _review_rows(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    review = result.get("review")
    if isinstance(review, Mapping):
        values = review.get("rows") or review.get("items") or review.get("messages")
    else:
        values = result.get("review_rows")
    if isinstance(values, Mapping):
        values = tuple(values.values())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(dict(_jsonable(item)) for item in values if isinstance(_jsonable(item), Mapping))


def _review_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _message(
    message_id: str,
    content: str,
    *,
    chat_id: str = "chat-direct-synthetic",
    chat_type: str = "direct",
    speaker_id: str = "speaker-a-synthetic",
    timestamp: str = "2026-08-31T09:00:00+08:00",
    sequence: int = 1,
    message_type: str = "text",
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "message_id": message_id,
        "account_id": "account-synthetic",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "speaker_id": speaker_id,
        "direction": "incoming",
        "timestamp": timestamp,
        "event_time": timestamp,
        "time_offset_seconds": sequence * 60,
        "sequence_in_chat": sequence,
        "message_type": message_type,
        "content": content,
        "source_mode": "synthetic",
        "metadata_authoritative": True,
    }
    value.update(extra)
    return value


def _social_messages() -> list[dict[str, Any]]:
    return [
        _message("social-hello", "你好", speaker_id="speaker-a-synthetic", sequence=1),
        _message("social-ack", "收到，确认一下。", speaker_id="speaker-b-synthetic", sequence=2),
        _message("social-ok", "好的，谢谢。", speaker_id="speaker-a-synthetic", sequence=3),
    ]


def _cross_day_messages() -> list[dict[str, Any]]:
    return [
        _message(
            "cross-day-open",
            "接口部署还在排查。",
            timestamp="2026-08-30T23:58:00+08:00",
            sequence=10,
            speaker_id="speaker-a-synthetic",
        ),
        _message(
            "cross-day-reply",
            "我继续看上面这个部署问题，日志明早给你。",
            timestamp="2026-08-31T00:04:00+08:00",
            sequence=11,
            speaker_id="speaker-b-synthetic",
            reply_to_message_id="cross-day-open",
        ),
    ]


def test_authoritative_message_speaker_chat_time_and_original_text_are_recoverable() -> None:
    messages = [
        _message(
            "authority-m1",
            "SYNTHETIC original line one",
            speaker_id="speaker-authoritative-a",
            chat_id="chat-authority",
            timestamp="2026-08-31T10:01:02+08:00",
            sequence=41,
            sender_name="display-name-must-not-win",
        ),
        _message(
            "authority-m2",
            "SYNTHETIC original line two",
            speaker_id="speaker-authoritative-b",
            chat_id="chat-authority",
            timestamp="2026-08-31T10:02:03+08:00",
            sequence=42,
            # Text may claim another identity; source metadata stays in charge.
            claimed_speaker="speaker-forged-in-text",
        ),
    ]
    result = _reconstruct(messages, mode="offline")
    rows = _message_by_id(result)
    assert set(rows) >= {"authority-m1", "authority-m2"}
    for source in messages:
        row = rows[source["message_id"]]
        assert _message_text(row) == source["content"]
        assert row.get("speaker_id") == source["speaker_id"]
        assert row.get("chat_id") == source["chat_id"]
        assert row.get("timestamp") == source["timestamp"]
        assert row.get("sequence_in_chat") == source["sequence_in_chat"]
    assert rows["authority-m2"].get("speaker_id") != "speaker-forged-in-text"


def test_direct_and_group_scope_and_chat_type_are_preserved() -> None:
    messages = [
        _message(
            "scope-direct",
            "私聊里的合成消息。",
            chat_id="chat-direct-synthetic",
            chat_type="direct",
            speaker_id="speaker-direct-peer",
        ),
        _message(
            "scope-group",
            "群聊里的合成消息。",
            chat_id="chat-group-synthetic",
            chat_type="group",
            speaker_id="speaker-group-member",
        ),
    ]
    result = _reconstruct(messages, mode="offline")
    rows = _message_by_id(result)
    assert rows["scope-direct"]["chat_type"] == "direct"
    assert rows["scope-group"]["chat_type"] == "group"
    assert rows["scope-direct"]["chat_id"] != rows["scope-group"]["chat_id"]
    threads = _thread_rows(result)
    assert threads
    assert any("scope-direct" in _thread_message_ids(thread) for thread in threads)
    assert any("scope-group" in _thread_message_ids(thread) for thread in threads)
    assert all(
        not ({"scope-direct", "scope-group"} <= _thread_message_ids(thread))
        for thread in threads
    )


def test_greeting_and_confirmation_are_retained_without_forced_topic_or_value() -> None:
    messages = _social_messages()
    result = _reconstruct(messages, mode="offline")
    rows = _message_by_id(result)
    assert set(rows) >= {item["message_id"] for item in messages}
    labels = {
        str(signal.get("label"))
        for segment in _rows(result, "fragments")
        for signal in segment.get("interaction_signals") or ()
        if isinstance(signal, Mapping)
    }
    assert "greeting" in labels
    assert "acknowledgement" in labels
    for message_id in ("social-hello", "social-ack", "social-ok"):
        row = rows[message_id]
        assert row.get("role") in {None, "greeting", "conversation_opener", "confirmation", "context_only", "social"}
        assert row.get("topic_id") in {None, "", "unknown"}
        assert row.get("topic") in {None, "", "unknown"}
        assert row.get("information_value") in {None, "", "unknown", "none", "low"}
    assert set().union(*(_thread_message_ids(thread) for thread in _thread_rows(result))) >= {
        "social-hello",
        "social-ack",
        "social-ok",
    }


def test_mixed_greeting_and_substantive_content_stays_recoverable_and_primary() -> None:
    mixed = _message(
        "mixed-greeting-substance",
        "你好，接口部署今天失败了，请保留这条问题。",
        sequence=7,
    )
    result = _reconstruct([mixed], mode="offline")
    rows = _message_by_id(result)
    assert _message_text(rows[mixed["message_id"]]) == mixed["content"]
    fragments = _rows(result, "fragments")
    mixed_fragments = [row for row in fragments if row.get("message_id") == mixed["message_id"]]
    assert mixed_fragments, "mixed turn must not disappear behind its greeting prefix"
    assert any(
        "接口部署" in str(row.get("text") or row.get("fragment_text") or row.get("content") or "")
        for row in mixed_fragments
    )
    assert any(
        row.get("role") in {"substantive", "primary", "claim"}
        or row.get("fragment_type") in {"statement", "question", "request", "answer"}
        or any(
            isinstance(signal, Mapping)
            and signal.get("label") in {"discussion", "question", "request", "sharing"}
            for signal in row.get("interaction_signals") or ()
        )
        or row.get("explicit_matters")
        for row in mixed_fragments
    )


def test_one_chitchat_sequence_can_remain_open_without_an_explicit_topic() -> None:
    messages = [
        _message("chitchat-1", "今天过得还行吗？", sequence=1),
        _message("chitchat-2", "还行，刚喝了杯茶。", speaker_id="speaker-b-synthetic", sequence=2),
    ]
    result = _reconstruct(messages, mode="offline")
    assert set(_message_by_id(result)) >= {"chitchat-1", "chitchat-2"}
    threads = _thread_rows(result)
    assert threads
    assert any({"chitchat-1", "chitchat-2"} <= _thread_message_ids(thread) for thread in threads)
    for thread in threads:
        if _thread_message_ids(thread) & {"chitchat-1", "chitchat-2"}:
            assert thread.get("open_boundary", True) is not False
            assert thread.get("topic_id") in {None, "", "unknown"}
            assert thread.get("topic") in {None, "", "unknown"}
            assert thread.get("event_id") in {None, "", "unknown"}


def test_multiple_topic_streams_can_coexist_without_forced_merge() -> None:
    messages = [
        _message("stream-project-1", "项目部署的接口还在排查。", sequence=1),
        _message(
            "stream-project-2",
            "我继续看这个部署问题，日志随后补上。",
            speaker_id="speaker-b-synthetic",
            sequence=2,
            reply_to_message_id="stream-project-1",
        ),
        _message(
            "stream-movie-1",
            "换个话题，周末电影我想看科幻片。",
            speaker_id="speaker-a-synthetic",
            sequence=3,
            topic_shift=True,
        ),
        _message(
            "stream-movie-2",
            "那就选一部两小时以内的。",
            speaker_id="speaker-b-synthetic",
            sequence=4,
            reply_to_message_id="stream-movie-1",
        ),
    ]
    result = _reconstruct(messages, mode="offline")
    thread_sets = [_thread_message_ids(thread) for thread in _thread_rows(result)]
    project = {"stream-project-1", "stream-project-2"}
    movie = {"stream-movie-1", "stream-movie-2"}
    assert any(project <= values for values in thread_sets)
    assert any(movie <= values for values in thread_sets)
    assert not any(project | movie <= values for values in thread_sets), (
        "topic streams must remain independently reviewable"
    )


def test_cross_day_thread_candidate_survives_today_yesterday_week_views() -> None:
    result = _reconstruct(
        _cross_day_messages(),
        mode="offline",
        as_of="2026-08-31",
    )
    threads = _thread_rows(result)
    assert any({"cross-day-open", "cross-day-reply"} <= _thread_message_ids(thread) for thread in threads)
    views = _view_mapping(result)
    assert {"today", "yesterday", "week"} <= set(views)
    today_ids = _view_ids(views["today"])
    yesterday_ids = _view_ids(views["yesterday"])
    week_ids = _view_ids(views["week"])
    assert "cross-day-reply" in today_ids
    assert "cross-day-open" in yesterday_ids
    assert {"cross-day-open", "cross-day-reply"} <= week_ids

    # The date labels are projections over one canonical thread, not three
    # independent semantic boundaries.
    thread_ids = {
        str(thread.get("thread_id") or thread.get("id")):
        thread
        for thread in threads
        if thread.get("thread_id") or thread.get("id")
    }
    assert thread_ids
    cross_thread_id = next(
        thread_id
        for thread_id, thread in thread_ids.items()
        if {"cross-day-open", "cross-day-reply"} <= _thread_message_ids(thread)
    )
    view_thread_ids = set()
    for view in views.values():
        view_thread_ids.update(_view_ids(view, "thread_ids"))
    if view_thread_ids:
        assert cross_thread_id in view_thread_ids


def test_fixed_message_count_or_day_sampling_is_not_a_hard_thread_boundary() -> None:
    messages = [
        _message(
            "long-context-open",
            "需要继续跟进这个合成部署问题。",
            timestamp="2026-08-31T08:00:00+08:00",
            sequence=1,
        )
    ]
    for sequence in range(2, 27):
        messages.append(
            _message(
                f"long-context-noise-{sequence}",
                f"合成闲聊 {sequence}。",
                speaker_id="speaker-b-synthetic" if sequence % 2 else "speaker-a-synthetic",
                timestamp=f"2026-08-31T08:{sequence:02d}:00+08:00",
                sequence=sequence,
            )
        )
    messages.append(
        _message(
            "long-context-reply",
            "我还在继续上面那个部署问题，已经找到线索。",
            speaker_id="speaker-b-synthetic",
            timestamp="2026-08-31T09:30:00+08:00",
            sequence=100,
            reply_to_message_id="long-context-open",
        )
    )
    result = _reconstruct(messages, mode="offline")
    threads = _thread_rows(result)
    assert any({"long-context-open", "long-context-reply"} <= _thread_message_ids(thread) for thread in threads)
    assert len(_message_by_id(result)) >= len(messages)
    assert not any(
        str(thread.get("boundary_reason") or "").casefold() in {"message_count", "day_count", "fixed_limit"}
        for thread in threads
    )


@pytest.mark.parametrize(
    ("message_type", "marker"),
    (("link", "[SYNTHETIC LINK UNAVAILABLE]"),
     ("image", "[SYNTHETIC IMAGE UNAVAILABLE]"),
     ("video", "[SYNTHETIC VIDEO UNAVAILABLE]"),
     ("audio", "[SYNTHETIC AUDIO UNAVAILABLE]")),
)
def test_unresolved_media_is_explicitly_unavailable_and_never_evidence(
    message_type: str,
    marker: str,
) -> None:
    message_id = f"media-{message_type}"
    message = _message(
        message_id,
        marker,
        message_type=message_type,
        media_state="unavailable",
        media_available=False,
        media_path=None,
        sequence=9,
    )
    result = _reconstruct([message], mode="offline")
    row = _message_by_id(result)[message_id]
    availability = row.get("availability") or row.get("media_state") or row.get("parse_status")
    assert str(availability).casefold() in {"unavailable", "not_available"}
    assert row.get("evidence_eligible") is False
    evidence_ids = _evidence_message_ids(result)
    assert message_id not in evidence_ids
    # Retaining the media message as context is allowed; promoting its
    # unresolved bytes/placeholder to semantic evidence is not.
    assert message_id in _message_by_id(result)


def test_discussion_weight_and_information_value_are_independent_axes() -> None:
    messages = [
        _message(
            "axis-social-1",
            "哈哈，收到。",
            chat_id="axis-social-chat",
            sequence=1,
        ),
        _message(
            "axis-social-2",
            "好呀，继续聊。",
            chat_id="axis-social-chat",
            speaker_id="speaker-b-synthetic",
            sequence=2,
        ),
        _message(
            "axis-social-3",
            "嗯嗯。",
            chat_id="axis-social-chat",
            sequence=3,
        ),
        _message(
            "axis-fact",
            "版本部署在合成环境已经失败，需记录具体错误。",
            chat_id="axis-fact-chat",
            sequence=4,
        ),
    ]
    result = _reconstruct(messages, mode="offline")
    threads = _thread_rows(result)
    assert threads
    for thread in threads:
        discussion = thread.get("discussion_weight")
        information = thread.get("information_value")
        assert isinstance(discussion, Mapping)
        assert isinstance(information, Mapping)
        assert discussion.get("candidate_only") is True
        assert information.get("candidate_only") is True
    assert any(
        (thread.get("information_value") or {}).get("label") in {"none", "low", "unknown"}
        and (thread.get("discussion_weight") or {}).get("score")
        != (thread.get("information_value") or {}).get("score")
        for thread in threads
    )
    # A value axis must not be used as a filter that deletes the social run.
    assert {"axis-social-1", "axis-social-2", "axis-social-3"} <= set(_message_by_id(result))


def test_offline_contract_has_zero_provider_calls_and_blocks_production_stages() -> None:
    result = _reconstruct(_social_messages(), mode="offline")
    meta = _run_meta(result)
    assert int(meta.get("provider_calls", result.get("provider_calls", -1))) == 0
    assert meta.get("production_blocked", result.get("production_blocked")) is True
    assert meta.get("stage_b", result.get("stage_b")) is False
    assert meta.get("stage_c", result.get("stage_c")) is False


def test_direct_and_group_review_page_is_understandable_and_explains_missing_context_and_join() -> None:
    messages = [
        _message(
            "review-direct",
            "私聊中讨论合成接口。",
            chat_id="review-chat-direct",
            chat_type="direct",
            speaker_id="review-speaker-direct",
            timestamp="2026-08-31T11:00:00+08:00",
            speaker_name="合成私聊成员",
        ),
        _message(
            "review-group",
            "群聊中讨论合成部署。",
            chat_id="review-chat-group",
            chat_type="group",
            speaker_id="review-speaker-group",
            timestamp="2026-08-31T11:01:00+08:00",
            speaker_name="合成群成员",
        ),
    ]
    result = _reconstruct(messages, mode="offline")
    page = str(result.get("_review_html", ""))
    assert page
    assert "交流过程重建候选" in page
    assert "合成私聊成员" in page
    assert "合成群成员" in page
    assert "私聊中讨论合成接口。" in page
    assert "群聊中讨论合成部署。" in page
    assert "参与者" in page
    assert "时间" in page
    assert "逐条消息：谁、何时、说了什么、媒体缺什么" in page
    assert "为什么暂时拼在一起" in page
    assert "仍不确定什么" in page
    assert "缺失" in page
