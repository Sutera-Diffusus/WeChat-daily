"""Independent synthetic K15 contract for the compact Stage-A protocol.

This file deliberately exercises only the in-memory protocol boundary.  It
does not read a private/frozen split, invoke a provider, or call a runner.  A
missing ``wechat_bridge.compact_stage_a_protocol`` is an intentional red gate
until the protocol implementation lands.

The implementation may expose equivalent public names (for example
``build_stage_a_request`` instead of ``build_request``); the resolver below
accepts those names while keeping the wire invariants strict.  The canonical
wire used by the tests is deliberately small: message and candidate aliases
are opaque, all semantic Stage-B slots are absent, and assignments contain
only topic membership plus bounded uncertainty metadata.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

import pytest

from wechat_bridge import compact_stage_a_protocol as protocol


ACCOUNT = "account-k15-synthetic"
CHAT = "chat-k15-synthetic"
OTHER_ACCOUNT = "account-k15-other"
OTHER_CHAT = "chat-k15-other"
SCOPE = {"account_id": ACCOUNT, "chat_id": CHAT}

PRIMARY_ALIASES = tuple("m%02d" % index for index in range(12))
CONTEXT_ALIASES = ("m12", "m13")
CANDIDATE_ALIASES = tuple("c%02d" % index for index in range(20))
REQUEST_MESSAGE_ALIASES = tuple("m%d" % index for index in range(1, 15))
REQUEST_CANDIDATE_ALIASES = tuple("c%d" % index for index in range(1, 21))
BODY_MARKER = "K15_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"

# Stage A may retain uncertainty labels, but it must not produce any of the
# semantic slots that belong to Stage B (or presentation/event layers).
STAGE_B_FORBIDDEN_KEYS = frozenset(
    {
        "speaker",
        "speaker_id",
        "person",
        "person_id",
        "mentioned",
        "mentioned_person",
        "mentioned_person_id",
        "mentioned_person_ids",
        "subject",
        "subject_id",
        "object",
        "object_id",
        "action",
        "actions",
        "state",
        "claim",
        "claims",
        "claim_id",
        "claim_ids",
        "claim_type",
        "modality",
        "event",
        "event_id",
        "title",
        "summary",
        "narrative",
        "rationale",
        "explanation",
    }
)

BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_body",
        "transcript",
    }
)


def _jsonable(value: Any) -> Any:
    """Convert a public result/dataclass into deterministic JSON data."""

    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=str)
    return value


def _mapping(value: Any) -> dict[str, Any]:
    data = _jsonable(value)
    if not isinstance(data, Mapping):
        raise AssertionError("K15 public result must serialize to a mapping")
    return dict(data)


def _canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _token_proxy(value: Any, system: str = "") -> int:
    """Use the same conservative 4-character proxy as the local contracts."""

    return (len(system) + len(_canonical_json(value)) + 3) // 4


def _first_callable(*names: str, announced_tokens: Iterable[str] = ()) -> Callable[..., Any]:
    """Resolve the implementation's announced API without weakening checks."""

    announced: list[str] = []
    for key in ("PUBLIC_API", "PUBLIC_APIS", "API", "API_NAMES"):
        value = getattr(protocol, key, None)
        if isinstance(value, Mapping):
            announced.extend(str(item) for item in value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            announced.extend(str(item) for item in value)
    tokens = tuple(str(item).casefold() for item in announced_tokens)
    announced.extend(
        str(item)
        for item in getattr(protocol, "__all__", ()) or ()
        if not tokens or any(token in str(item).casefold() for token in tokens)
    )
    ordered = list(dict.fromkeys(tuple(names) + tuple(announced)))
    for name in ordered:
        candidate = getattr(protocol, name, None)
        # ``__all__`` also announces error/data classes.  A class is not a
        # protocol operation; in particular never mistake the protocol's
        # exception class for the response validator.
        if callable(candidate) and not isinstance(candidate, type):
            return candidate
    raise AssertionError("K15 protocol does not expose one of %s" % (", ".join(names),))


def _builder() -> Callable[..., Any]:
    return _first_callable(
        "build_request",
        "build_stage_a_request",
        "build_compact_stage_a_request",
        "build_canonical_request",
        "make_request",
        announced_tokens=("build", "request", "make"),
    )


def _validator() -> Callable[..., Any]:
    return _first_callable(
        "validate_response",
        "validate_stage_a_response",
        "validate_stage_a_output",
        "validate_topic_assignments",
        "validate_compact_stage_a_output",
        "validate_output",
        announced_tokens=("response", "output", "assignment"),
    )


def _sizer() -> Callable[..., Any]:
    return _first_callable(
        "size_report",
        "stage_a_size_report",
        "request_size_report",
        "report_size",
        "measure_size",
        "measure_wire_size",
        "request_size",
        "output_size",
        announced_tokens=("size", "report", "proxy"),
    )


def _ledger_projector() -> Callable[..., Any] | None:
    for name in (
        "project_ledger",
        "ledger_projection",
        "project_body_free_ledger",
        "to_ledger_projection",
        "body_free_ledger_projection",
    ):
        candidate = getattr(protocol, name, None)
        if callable(candidate):
            return candidate
    return None


def _messages() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, alias in enumerate(PRIMARY_ALIASES + CONTEXT_ALIASES):
        is_context = alias in CONTEXT_ALIASES
        rows.append(
            {
                "message_alias": alias,
                "handle": "%s/%s|message|%s" % (ACCOUNT, CHAT, alias),
                "scope": dict(SCOPE),
                "sequence": index,
                "role": "context" if is_context else "primary",
                "kind": "greeting" if alias == "m12" else ("unknown_context" if alias == "m13" else "statement"),
                # Bodies are provider input material only.  The ledger
                # projection must retain handles/counts and omit this value.
                "text": BODY_MARKER + "_%02d" % index,
            }
        )
    return rows


def _candidates() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, alias in enumerate(CANDIDATE_ALIASES):
        left = PRIMARY_ALIASES[index % len(PRIMARY_ALIASES)]
        right = PRIMARY_ALIASES[(index + 1) % len(PRIMARY_ALIASES)]
        rows.append(
            {
                "candidate_alias": alias,
                "handle": "%s/%s|candidate|%s" % (ACCOUNT, CHAT, alias),
                "scope": dict(SCOPE),
                "left_message_alias": left,
                "right_message_alias": right,
                "relation": "no_reply" if index == 19 else ("reply" if index % 3 == 0 else "continuity"),
                "reply_to": None if index == 19 else right,
                "uncertainty": "no_explicit_reply" if index == 19 else None,
            }
        )
    return rows


def _packet() -> dict[str, Any]:
    return {
        "schema_version": "compact_stage_a_v1",
        "packet_id": "packet-k15-synthetic",
        "scope": dict(SCOPE),
        "messages": _messages(),
        "candidates": _candidates(),
        "primary_message_aliases": list(PRIMARY_ALIASES),
        "context_message_aliases": list(CONTEXT_ALIASES),
        "unknown_context_aliases": ["m13"],
    }


def _call_with_variants(function: Callable[..., Any], variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
    """Call a pure protocol API despite one-time positional/keyword naming."""

    errors: list[TypeError] = []
    for args, kwargs in variants:
        try:
            return function(*args, **kwargs)
        except TypeError as exc:
            errors.append(exc)
    if errors:
        raise errors[-1]
    raise AssertionError("no API invocation variant supplied")


def _build(packet: Mapping[str, Any]) -> Any:
    function = _builder()
    return _call_with_variants(
        function,
        (
            ((packet,), {}),
            ((), {"packet": packet}),
            ((), {"context_packet": packet}),
            (
                (),
                {
                    "messages": packet["messages"],
                    "candidates": packet["candidates"],
                    "scope": packet["scope"],
                },
            ),
        ),
    )


def _validate(response: Mapping[str, Any], request: Any) -> Any:
    function = _validator()
    return _call_with_variants(
        function,
        (
            ((response, request), {}),
            ((request, response), {}),
            ((), {"response": response, "request": request}),
            ((), {"value": response, "request": request}),
            ((), {"response": response, "packet": request}),
        ),
    )


def _size(request: Any, response: Any | None = None) -> Any:
    function = _sizer()
    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((request,), {})]
    if response is not None:
        variants.extend(
            [
                ((request, response), {}),
                ((), {"request": request, "response": response}),
                ((), {"user": request, "response": response}),
            ]
        )
    return _call_with_variants(function, variants)


def _ledger(request: Any, response: Any, report: Any) -> Any:
    function = _ledger_projector()
    if function is None:
        data = _mapping(request)
        for key in ("ledger", "ledger_projection", "persisted_ledger"):
            if key in data:
                return data[key]
        # The projection is a frozen K15 requirement, so absence is a clear
        # contract failure rather than a reason to silently skip the check.
        raise AssertionError("K15 protocol must expose a body-free ledger projection")
    return _call_with_variants(
        function,
        (
            ((request, response, report), {}),
            ((request, response), {}),
            ((request,), {}),
            ((), {"request": request, "response": response, "size_report": report}),
            ((), {"request": request, "response": response}),
        ),
    )


def _request_sections(request: Any) -> tuple[str, Any]:
    """Extract the actual system text and canonical user value from a request."""

    if isinstance(request, (tuple, list)) and len(request) == 2:
        return str(request[0]), request[1]
    data = _mapping(request)

    # OpenAI-style envelopes are accepted only as a representation detail;
    # all size checks still use the exact canonical user projection below.
    messages = data.get("messages")
    if isinstance(messages, list) and messages and all(isinstance(item, Mapping) for item in messages):
        system_parts: list[str] = []
        user_parts: list[Any] = []
        for item in messages:
            role = str(item.get("role", "")).casefold()
            if role == "system":
                system_parts.append(str(item.get("content", "")))
            elif role == "user":
                user_parts.append(item.get("content", ""))
        if system_parts or user_parts:
            user: Any = user_parts[0] if len(user_parts) == 1 else user_parts
            if isinstance(user, str):
                try:
                    user = json.loads(user)
                except (TypeError, ValueError):
                    pass
            return "".join(system_parts), user

    system = ""
    for key in ("system", "system_prompt", "canonical_system", "stage_a_system_prompt"):
        if key in data:
            system = str(data[key])
            break
    user: Any = None
    for key in ("user", "user_payload", "user_packet", "canonical_user", "canonical_user_payload", "payload"):
        if key in data:
            user = data[key]
            break
    if user is None:
        user = {
            key: value
            for key, value in data.items()
            if key not in {"system", "system_prompt", "canonical_system", "stage_a_system_prompt"}
        }
    if not system and set(data) >= {"v", "s", "h"}:
        system = str(getattr(protocol, "SYSTEM_PROMPT", getattr(protocol, "STAGE_A_SYSTEM_PROMPT", "")))
    if isinstance(user, str):
        try:
            user = json.loads(user)
        except (TypeError, ValueError):
            pass
    return system, user


def _named_lists(value: Any, names: Iterable[str]) -> list[list[Any]]:
    wanted = {str(name).casefold() for name in names}
    found: list[list[Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in wanted and isinstance(child, list):
                    found.append(child)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _row_alias(row: Any, singular_names: Iterable[str]) -> str | None:
    if isinstance(row, str):
        return row
    if not isinstance(row, Mapping):
        return None
    wanted = {str(name).casefold() for name in singular_names}
    for key, value in row.items():
        if str(key).casefold() in wanted and isinstance(value, str):
            return value
    # A compact implementation may use ``alias`` for both tables.  Do not
    # treat endpoint references as record identity unless this row is itself
    # a message/candidate row.
    if isinstance(row.get("alias"), str):
        return str(row["alias"])
    return None


def _collection_aliases(value: Any, *, candidate: bool) -> list[str]:
    if candidate:
        names = ("candidates", "candidate_rows", "candidate_units", "candidate_aliases", "candidate_handles")
        singular = ("candidate_alias", "candidate_id", "candidate_handle", "alias")
    else:
        names = ("messages", "message_rows", "message_units", "message_aliases", "message_handles")
        singular = ("message_alias", "message_id", "message_handle", "alias")
    # The K15 compact wire has one handle table ``h``.  Its short aliases are
    # intentionally distinct from the long authoritative handles in source
    # records and are the only aliases visible to the provider.
    data = _mapping(value)
    compact_rows = data.get("h")
    if isinstance(compact_rows, list) and all(isinstance(row, Mapping) for row in compact_rows):
        kind = "c" if candidate else "m"
        return [str(row["i"]) for row in compact_rows if row.get("k") == kind and isinstance(row.get("i"), str)]

    collections = _named_lists(value, names)
    if not collections:
        return []
    # Prefer the collection whose cardinality matches the frozen worst-case
    # shape; wrappers may also expose a short metadata list.
    target = 20 if candidate else 14
    collection = next((items for items in collections if len(items) == target), max(collections, key=len))
    aliases = [_row_alias(item, singular) for item in collection]
    return [item for item in aliases if item is not None]


def _topic_rows(response: Any) -> list[dict[str, Any]]:
    data = _mapping(response)
    for key in ("topics", "topic_assignments", "assignments", "topic_groups", "t"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [_mapping(row) for row in rows]
    raise AssertionError("K15 response has no topic assignment list")


def _field(row: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    wanted = {str(name).casefold() for name in names}
    for key, value in row.items():
        if str(key).casefold() in wanted:
            return value
    return default


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return [str(value)]


def _assignment_sets(response: Any) -> tuple[set[str], set[str], set[str]]:
    primary: list[str] = []
    context: list[str] = []
    candidates: list[str] = []
    for row in _topic_rows(response):
        primary.extend(
            _string_list(
                _field(
                    row,
                    (
                        "p",
                        "primary_message_aliases",
                        "primary_message_ids",
                        "primary_messages",
                        "primary",
                        "message_aliases",
                        "message_handles",
                    ),
                )
            )
        )
        context.extend(
            _string_list(
                _field(
                    row,
                    (
                        "c",
                        "context_message_aliases",
                        "context_message_ids",
                        "context_messages",
                        "context",
                        "context_handles",
                    ),
                )
            )
        )
        candidates.extend(
            _string_list(
                _field(row, ("candidate_aliases", "candidate_ids", "candidates", "candidate_handles"))
            )
        )
    return set(primary), set(context), set(candidates)


def _assert_body_free(value: Any) -> None:
    bad_keys: list[str] = []

    def visit(item: Any, parent_key: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key)
                # ``content_ref``/``body_ref`` are opaque handles, not body.
                is_ref = key_text.casefold().endswith(("_ref", "_handle", "_handles"))
                if key_text.casefold() in BODY_KEYS and child not in (None, "", [], (), {}) and not is_ref:
                    bad_keys.append(key_text)
                visit(child, key_text)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child, parent_key)

    visit(value)
    assert not bad_keys, "K15 ledger projection contains body fields: %s" % bad_keys[:8]
    assert BODY_MARKER not in _canonical_json(value)


def _assert_accepted(result: Any) -> None:
    """Accept None/normalized values, but reject explicit invalid results."""

    if result is None:
        return
    data = _mapping(result)
    for key in ("ok", "valid", "is_valid", "accepted"):
        if key in data:
            assert data[key] is True, "K15 validator rejected valid response: %r" % data
            return
    status = str(data.get("status", "")).casefold()
    if status:
        assert status in {"valid", "accepted", "complete", "ok"}, "K15 validator status=%s" % status


def _assert_rejected(response: Mapping[str, Any], request: Any) -> None:
    try:
        result = _validate(response, request)
    except Exception:
        return
    data = _mapping(result) if result is not None else {}
    if any(data.get(key) is False for key in ("ok", "valid", "is_valid", "accepted")):
        return
    if str(data.get("status", "")).casefold() in {"invalid", "rejected", "fail", "failed"}:
        return
    errors = data.get("errors") or data.get("error_codes")
    if isinstance(errors, (list, tuple, set, frozenset)) and errors:
        return
    raise AssertionError("K15 validator accepted a malformed response: %s" % data)


def _valid_response(*, unknown: bool = False) -> dict[str, Any]:
    return {
        # Compact K15 uses one-letter keys on the provider wire.  They expand
        # locally to topic id / primary aliases / context aliases / uncertainty.
        "t": [
            {
                "i": "topic-alpha",
                "p": ["m%d" % index for index in range(1, 7)],
                # m13 is the greeting retained as context, not silently
                # dropped when the next topic starts.
                "c": ["m13"],
                "u": "uncertain" if unknown else "certain",
            },
            {
                "i": "topic-beta",
                "p": ["m%d" % index for index in range(7, 13)],
                "c": ["m14"],
                "u": "unknown",
            },
        ]
    }


def _max_response(request: Any) -> Any:
    """Use the implementation's deterministic max-shape witness when exposed."""

    function = getattr(protocol, "build_max_size_response", None)
    if callable(function) and not isinstance(function, type):
        return function(request)
    return _valid_response()


def _forbidden_keys(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in STAGE_B_FORBIDDEN_KEYS:
                    found.append(str(key))
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return found


def test_public_api_builds_a_real_bounded_canonical_request() -> None:
    packet = _packet()
    request = _build(packet)
    system, user = _request_sections(request)
    actual_proxy = _token_proxy(user, system)

    assert system.strip(), "K15 must send a non-empty Stage-A system contract"
    assert actual_proxy <= 1600, "actual canonical system+user proxy exceeded 1600: %d" % actual_proxy

    message_aliases = _collection_aliases(request, candidate=False)
    candidate_aliases = _collection_aliases(request, candidate=True)
    assert len(message_aliases) == 14, "provider input must contain all 14 messages once"
    assert len(candidate_aliases) == 20, "provider input must retain all 20 candidate rows"
    assert len(message_aliases) == len(set(message_aliases)), "message aliases are duplicated in provider input"
    assert len(candidate_aliases) == len(set(candidate_aliases)), "candidate aliases are duplicated in provider input"
    assert set(message_aliases) == set(REQUEST_MESSAGE_ALIASES)
    assert set(candidate_aliases) == set(REQUEST_CANDIDATE_ALIASES)
    assert "c20" in candidate_aliases, "no-reply candidate was dropped"

    report = _size(request)
    report_data = _mapping(report)
    proxy_values = [
        value
        for key, value in report_data.items()
        if str(key).casefold() in {
            "canonical_proxy",
            "canonical_token_proxy",
            "input_token_proxy",
            "request_token_proxy",
            "system_user_token_proxy",
            "total_token_proxy",
        }
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ]
    assert proxy_values, "size_report must expose a numeric canonical/input proxy"
    assert min(proxy_values) <= 1600
    for key in ("input_token_proxy", "http_token_proxy"):
        if key in report_data:
            assert report_data[key] <= 1600


def test_worst_case_response_is_stage_a_only_and_within_400_tokens() -> None:
    request = _build(_packet())
    response = _max_response(request)
    _assert_accepted(_validate(response, request))

    primary, context, candidates = _assignment_sets(response)
    assert primary == set(REQUEST_MESSAGE_ALIASES[:12]), "every primary must be assigned exactly once"
    assert context == set(REQUEST_MESSAGE_ALIASES[12:]), "context aliases must remain in the legal context channel"
    # Candidates are input-only Stage-A context.  They must be retained in
    # the request but must not inflate or leak into the assignment response.
    assert candidates == set(), "Stage A response must contain topic assignments only"

    # Verify exact response cardinality and reject duplicate primary records,
    # rather than merely checking set coverage.
    primary_occurrences: list[str] = []
    for row in _topic_rows(response):
        primary_occurrences.extend(
            _string_list(
                _field(
                    row,
                    ("p", "primary_message_aliases", "primary_message_ids", "primary_messages", "primary"),
                )
            )
        )
    assert len(primary_occurrences) == len(REQUEST_MESSAGE_ALIASES[:12])
    assert len(primary_occurrences) == len(set(primary_occurrences))
    assert not _forbidden_keys(response), "Stage A response leaked Stage-B fields"

    response_proxy = _token_proxy(response)
    assert response_proxy <= 400, "worst-case legal response proxy exceeded 400: %d" % response_proxy
    report = _size(request, response)
    report_data = _mapping(report)
    output_values = [
        value
        for key, value in report_data.items()
        if any(token in str(key).casefold() for token in ("output", "response", "completion"))
        and "proxy" in str(key).casefold()
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ]
    if output_values:
        assert min(output_values) <= 400


def test_unknown_and_uncertainty_are_legal_without_stage_b_slots() -> None:
    request = _build(_packet())
    response = _valid_response(unknown=True)
    _assert_accepted(_validate(response, request))
    assert any(_field(row, ("u", "uncertainties", "uncertainty", "unknown")) for row in _topic_rows(response))
    assert not _forbidden_keys(response)


def test_greeting_context_and_topic_shift_can_coexist_in_two_assignments() -> None:
    request = _build(_packet())
    response = _valid_response()
    _assert_accepted(_validate(response, request))
    rows = _topic_rows(response)
    assert len(rows) == 2, "a topic shift must be representable as two topics"
    assert any("m13" in _string_list(_field(row, ("c", "context_message_aliases", "context_message_ids", "context"))) for row in rows)
    assert any("m1" in _string_list(_field(row, ("p", "primary_message_aliases", "primary_message_ids", "primary"))) for row in rows)
    assert any("m7" in _string_list(_field(row, ("p", "primary_message_aliases", "primary_message_ids", "primary"))) for row in rows)
    assert set(REQUEST_MESSAGE_ALIASES[:6]).isdisjoint(set(REQUEST_MESSAGE_ALIASES[6:]))


@pytest.mark.parametrize("mutation", ("forged", "duplicate", "missing", "crossscope", "stage_b_field"))
def test_response_validator_rejects_forged_duplicate_missing_crossscope_and_stage_b(
    mutation: str,
) -> None:
    request = _build(_packet())
    malformed = deepcopy(_valid_response())
    first = malformed["t"][0]
    if mutation == "forged":
        first["p"].append("m-forged")
    elif mutation == "duplicate":
        first["p"].append(first["p"][0])
    elif mutation == "missing":
        first["p"].pop()
    elif mutation == "crossscope":
        first["c"].append("%s/%s|message|m00" % (OTHER_ACCOUNT, OTHER_CHAT))
    else:
        first["state"] = "unknown"
    _assert_rejected(malformed, request)


def test_body_free_ledger_projection_does_not_persist_provider_material() -> None:
    packet = _packet()
    request = _build(packet)
    response = _valid_response()
    _assert_accepted(_validate(response, request))
    report = _size(request, response)
    projection = _ledger(request, response, report)
    _assert_body_free(projection)


def test_request_builder_rejects_an_authoritative_handle_from_another_scope() -> None:
    packet = _packet()
    packet["messages"][0]["handle"] = "%s/%s|message|m00" % (OTHER_ACCOUNT, OTHER_CHAT)
    with pytest.raises(ValueError):
        _build(packet)


def test_public_api_is_purely_protocol_local_and_does_not_need_private_inputs() -> None:
    """The contract fixture is synthetic and should be sufficient by itself."""

    packet = _packet()
    request = _build(packet)
    assert packet["scope"] == SCOPE
    assert OTHER_ACCOUNT not in _canonical_json(request)
    assert OTHER_CHAT not in _canonical_json(request)
    # Keep this assertion about the public wire, not module internals: no
    # filesystem path, frozen split, or provider object is part of the packet.
    assert not any(token in _canonical_json(request).casefold() for token in ("data/private", "frozen_test", "provider_call"))
