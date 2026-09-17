"""Synthetic/public contract for the active-period runner.

This module is intentionally independent from the existing topic/event and
private replay paths.  It describes the new boundary in terms of an ordered
``period -> message refs`` projection and calls one public, provider-free
runner.  The implementation is being finalized; the only compatibility seam
is :func:`_public_api`.  Once the public API is published, adapt that helper
once and keep the contract assertions strict.

Nothing in this file reads a database, a private artifact, or a frozen split.
Every body, timestamp, identity, topic, and evidence handle is synthetic.

The intended stable seam is ``run_active_periods(messages, *, max_periods,
whole_period_cap, source)`` returning structured ``selection``,
``materialized``, ``reconstructed`` and ``rendered`` period/card rows plus a
manifest.  ``validate_active_period_result(result)`` is the public ref-drift
gate.  If the final names differ, change only :func:`_public_api` and its
constants; do not relax the assertions below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import importlib
import inspect
import json
from typing import Any, Callable

import pytest


# This is the one-time public API adaptation boundary.  The names are
# deliberately singular and explicit: a draft or private implementation must
# not be made to pass the release contract through a growing alias list.
_PUBLIC_MODULE = "wechat_bridge.active_period_runner"
_RUN_ENTRYPOINT = "run_active_periods"
_VALIDATE_ENTRYPOINT = "validate_active_period_result"

_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "text",
        "message_text",
        "message_content",
        "raw_text",
        "raw_message",
        "prompt",
        "completion",
        "provider_response",
        "model_response",
        "reasoning",
    }
)
_REF_KEYS = (
    "message_refs",
    "source_message_refs",
    "ledger_message_refs",
    "included_message_refs",
    "message_ids",
    "source_message_ids",
    "ledger_message_ids",
    "timeline_refs",
    "refs",
)
_STAGE_KEYS: dict[str, tuple[str, ...]] = {
    "materialized": ("materialized", "materialized_periods", "active_periods"),
    "reconstructed": ("reconstructed", "reconstructed_periods", "periods"),
    "rendered": ("rendered", "rendered_periods", "rendered_cards", "cards"),
    "selection": ("selection", "selected", "selected_periods", "selected_active_periods"),
}


class _PublicActivePeriodAPI:
    """Call the stable public runner without importing implementation helpers."""

    def __init__(self, module: Any, run: Callable[..., Any], validate: Callable[..., Any] | None) -> None:
        self.module = module
        self.run_fn = run
        self.validate_fn = validate

    @staticmethod
    def _call(fn: Callable[..., Any], rows: Sequence[Mapping[str, Any]], kwargs: Mapping[str, Any]) -> Any:
        """Call a public function using its declared argument names only.

        The runner is expected to accept ``messages`` positionally.  Filtering
        optional keyword arguments here keeps this adapter a small one-time
        signature shim while ensuring the test never swallows a real runtime
        ``TypeError`` raised by the implementation.
        """

        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):  # pragma: no cover - unusual C callable
            return fn(tuple(rows), **dict(kwargs))
        params = signature.parameters
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values())
        named = dict(kwargs) if accepts_kwargs else {key: value for key, value in kwargs.items() if key in params}
        first = next(iter(params.values()), None)
        if (
            first is not None
            and first.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and first.name in {"messages", "rows", "source_messages", "input_rows"}
        ):
            return fn(**{first.name: tuple(rows), **named})
        return fn(tuple(rows), **named)

    def run(self, rows: Sequence[Mapping[str, Any]], **options: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "source": "synthetic",
            "include_bodies": True,
            "provider": None,
            "production_blocked": True,
        }
        kwargs.update(options)
        value = _jsonable(self._call(self.run_fn, rows, kwargs))
        if not isinstance(value, Mapping):
            pytest.fail("active-period runner must return a mapping or a to_dict DTO")
        return dict(value)

    def validate(self, result: Mapping[str, Any]) -> dict[str, Any]:
        if self.validate_fn is None:
            pytest.fail(
                f"{_PUBLIC_MODULE}.{_VALIDATE_ENTRYPOINT} is required for ref-drift blocking"
            )
        value = _jsonable(self._call(self.validate_fn, (result,), {}))
        if not isinstance(value, Mapping):
            pytest.fail("active-period result validator must return a mapping or a to_dict DTO")
        return dict(value)


def _public_api() -> _PublicActivePeriodAPI:
    """Resolve the one public module, skipping only while it is unpublished."""

    try:
        module = importlib.import_module(_PUBLIC_MODULE)
    except ModuleNotFoundError as exc:
        pytest.skip(f"active-period public API is not published yet: {exc}")
    run = getattr(module, _RUN_ENTRYPOINT, None)
    if not callable(run):
        pytest.skip(f"active-period public API is not stable yet: {_PUBLIC_MODULE}.{_RUN_ENTRYPOINT}")
    validate = getattr(module, _VALIDATE_ENTRYPOINT, None)
    return _PublicActivePeriodAPI(module, run, validate if callable(validate) else None)


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(child) for child in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    return value


def _timestamp(day: str, hour: int, minute: int) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+08:00"


def _synthetic_message(
    message_id: str,
    *,
    timestamp: str,
    account_id: str,
    chat_id: str,
    chat_type: str,
    sequence: int,
    message_type: str = "text",
    strand_id: str = "strand-main",
    episode_id: str = "episode-synthetic",
    reply_to_message_id: str | None = None,
    interaction_role: str = "continuation",
    media: Mapping[str, Any] | None = None,
    context_refs: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    body: str | None = None,
) -> dict[str, Any]:
    """Build one body-bearing synthetic row with authoritative metadata."""

    day = timestamp[:10]
    message_ref = f"synthetic-ref-{message_id}"
    if media is None:
        if message_type == "text":
            media = {
                "media_type": "text",
                "availability": "available",
                "semantic_evidence": True,
            }
        else:
            media = {
                "media_type": message_type,
                "availability": "not_present",
                "semantic_evidence": False,
            }
    row: dict[str, Any] = {
        "message_id": message_id,
        "message_ref": message_ref,
        "account_id": account_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "speaker_id": f"synthetic-speaker-{message_id}",
        "direction": "inbound",
        "timestamp": timestamp,
        "event_time": timestamp,
        "local_day": day,
        "sequence_in_chat": sequence,
        "message_type": message_type,
        "reply_to_message_id": reply_to_message_id,
        "quote_refs": [],
        "strand_id": strand_id,
        "internal_episode_id": episode_id,
        "interaction_role": interaction_role,
        "context_refs": list(context_refs),
        "evidence_refs": list(evidence_refs),
        # Deliberately adversarial ranking hints.  Selection must not use any
        # of these fields as a rank or period boundary.
        "topic": "SYNTHETIC-TOPIC-HINT-MUST-NOT-RANK",
        "topic_hint": "SYNTHETIC-TOPIC-HINT-MUST-NOT-RANK",
        "information_value": "high",
        "value": 999,
        "message_count": 999,
        "media": dict(media),
        "content": body or f"SYNTHETIC-BODY-{message_id}",
        "body": body or f"SYNTHETIC-BODY-{message_id}",
        "split": "development",
        "source_mode": "synthetic",
    }
    return row


def _build_fixture() -> tuple[list[dict[str, Any]], tuple[tuple[str, ...], ...]]:
    """Return 3 logical periods over 2 dates and both direct/group scopes.

    The first period has six messages and crosses midnight.  The second is a
    four-message group timeline with two internal episodes, a system row, an
    unavailable media row, greeting and acknowledgement.  The third contains
    an explicit-reply continuation after a long gap; the gap is a retrieval
    candidate, never a semantic resolution.
    """

    rows: list[dict[str, Any]] = []
    expected: list[tuple[str, ...]] = []

    # Period A: six messages, deliberately longer than the historical four
    # row window.  The reply chain and chronology continue through midnight.
    p1_ids = [f"p-cross-midnight-m{index}" for index in range(1, 7)]
    p1_times = (
        _timestamp("2026-08-30", 23, 58),
        _timestamp("2026-08-30", 23, 59),
        _timestamp("2026-08-31", 0, 1),
        _timestamp("2026-08-31", 0, 3),
        _timestamp("2026-08-31", 0, 5),
        _timestamp("2026-08-31", 0, 7),
    )
    p1_rows: list[dict[str, Any]] = []
    for index, (message_id, timestamp) in enumerate(zip(p1_ids, p1_times), start=1):
        p1_rows.append(
            _synthetic_message(
                message_id,
                timestamp=timestamp,
                account_id="synthetic-account-a",
                chat_id="synthetic-direct-cross-midnight",
                chat_type="direct",
                sequence=index,
                reply_to_message_id=p1_ids[index - 2] if index > 1 else None,
                strand_id="strand-main",
                episode_id="episode-cross-midnight",
                interaction_role="greeting" if index == 1 else "continuation",
                evidence_refs=(f"synthetic-evidence-{message_id}",) if index in {3, 4, 5, 6} else (),
                body=f"SYNTHETIC-PERIOD-A-BODY-{index}",
            )
        )
    rows.extend(p1_rows)
    expected.append(tuple(row["message_ref"] for row in p1_rows))

    # Period B: both parallel strands are interleaved in the authoritative
    # timeline.  System and media are retained but not evidence eligible.
    p2_specs = (
        ("p-group-parallel-m1", 9, 0, "text", "strand-a", "episode-group-a", "greeting", None, "SYNTHETIC-GREETING-BODY"),
        ("p-group-parallel-m2", 9, 1, "system", "strand-b", "episode-group-a", "system", None, "SYNTHETIC-SYSTEM-BODY"),
        ("p-group-parallel-m3", 9, 3, "image", "strand-a", "episode-group-b", "media", {
            "media_type": "image",
            "availability": "unavailable",
            "missing_reason": "synthetic-media-not-available",
            "semantic_evidence": False,
        }, "SYNTHETIC-MEDIA-BODY"),
        ("p-group-parallel-m4", 9, 4, "text", "strand-b", "episode-group-b", "ack", None, "SYNTHETIC-ACK-BODY"),
    )
    p2_ids = tuple(spec[0] for spec in p2_specs)
    p2_rows: list[dict[str, Any]] = []
    for index, (message_id, hour, minute, message_type, strand, episode, role, media, body) in enumerate(p2_specs, start=1):
        p2_rows.append(
            _synthetic_message(
                message_id,
                timestamp=_timestamp("2026-08-31", hour, minute),
                account_id="synthetic-account-a",
                chat_id="synthetic-group-parallel",
                chat_type="group",
                sequence=index,
                message_type=message_type,
                strand_id=strand,
                episode_id=episode,
                reply_to_message_id=p2_ids[index - 2] if index > 1 else None,
                interaction_role=role,
                media=media,
                # This whole period intentionally has zero context/evidence.
                context_refs=(),
                evidence_refs=(),
                body=body,
            )
        )
    rows.extend(p2_rows)
    expected.append(tuple(row["message_ref"] for row in p2_rows))

    # Period C: an explicit reply carries the period across a 149-minute gap.
    # The gap is observable retrieval metadata, not a semantic "resolved".
    p3_ids = [f"p-gap-continuation-m{index}" for index in range(1, 5)]
    p3_times = (
        _timestamp("2026-08-31", 15, 0),
        _timestamp("2026-08-31", 15, 1),
        _timestamp("2026-08-31", 17, 30),
        _timestamp("2026-08-31", 17, 31),
    )
    p3_rows: list[dict[str, Any]] = []
    for index, (message_id, timestamp) in enumerate(zip(p3_ids, p3_times), start=1):
        p3_rows.append(
            _synthetic_message(
                message_id,
                timestamp=timestamp,
                account_id="synthetic-account-b",
                chat_id="synthetic-direct-gap",
                chat_type="direct",
                sequence=index,
                reply_to_message_id=p3_ids[index - 2] if index > 1 else None,
                strand_id="strand-main",
                episode_id="episode-gap-continuation",
                interaction_role="continuation",
                context_refs=(),
                evidence_refs=(),
                body=f"SYNTHETIC-PERIOD-C-BODY-{index}",
            )
        )
    # Keep the observed gap explicit as a reversible retrieval cue.  The
    # runner may calculate the duration from timestamps, but must preserve
    # this candidate signal without promoting it to semantic resolution.
    p3_rows[2]["gap_after_seconds"] = 8940
    p3_rows[2]["retrieval_candidates"] = [
        {
            "kind": "gap",
            "reason_codes": ["synthetic_long_gap"],
            "from_message_ref": p3_rows[1]["message_ref"],
            "to_message_ref": p3_rows[2]["message_ref"],
            "candidate_only": True,
        }
    ]
    rows.extend(p3_rows)
    expected.append(tuple(row["message_ref"] for row in p3_rows))

    return rows, tuple(expected)


def _legacy_four_row_fixture() -> list[dict[str, Any]]:
    rows, _ = _build_fixture()
    # This is intentionally the old happy shape: one four-row slice.  It is
    # a negative release-gate input, not a gold fixture.
    return deepcopy(rows[:4])


FIXTURE_ROWS, EXPECTED_PERIOD_REFS = _build_fixture()
EXPECTED_ALL_REFS = tuple(ref for period in EXPECTED_PERIOD_REFS for ref in period)
REF_BY_TOKEN = {
    token: row["message_ref"]
    for row in FIXTURE_ROWS
    for token in (row["message_id"], row["message_ref"])
}
MEDIA_REF = REF_BY_TOKEN["p-group-parallel-m3"]
SYSTEM_REF = REF_BY_TOKEN["p-group-parallel-m2"]
GAP_PERIOD_REFS = EXPECTED_PERIOD_REFS[2]


def _canonical_ref(value: Any) -> str:
    token = str(value)
    return REF_BY_TOKEN.get(token, token)


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _extract_refs(row: Any) -> list[str]:
    """Extract ordered refs from one public period/card row."""

    if isinstance(row, str):
        return [_canonical_ref(row)]
    if not isinstance(row, Mapping):
        return []
    for key in _REF_KEYS:
        value = row.get(key)
        if isinstance(value, Mapping):
            value = list(value.keys())
        values = _sequence(value)
        if values:
            return [_canonical_ref(item) for item in values if item not in (None, "")]
    for key in ("timeline", "messages", "message_rows", "source_messages"):
        nested = _sequence(row.get(key))
        if nested:
            refs: list[str] = []
            for child in nested:
                if isinstance(child, Mapping):
                    token = child.get("message_ref") or child.get("source_message_ref") or child.get("message_id") or child.get("id")
                    if token not in (None, ""):
                        refs.append(_canonical_ref(token))
                elif child not in (None, ""):
                    refs.append(_canonical_ref(child))
            if refs:
                return refs
    return []


def _stage_rows(result: Mapping[str, Any], stage: str) -> list[Any]:
    """Locate structured rows for one stage without traversing arbitrary body text."""

    wanted = {key.casefold() for key in _STAGE_KEYS[stage]}

    def from_value(value: Any) -> list[Any]:
        if isinstance(value, Mapping):
            for key in (
                "rows",
                "periods",
                "cards",
                "items",
                "selected",
                "selected_periods",
                "active_periods",
                "materialized_periods",
                "rendered_cards",
            ):
                nested = value.get(key)
                if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                    return list(nested)
            if _extract_refs(value):
                return [value]
            return []
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return list(value)
        return []

    for key, value in result.items():
        if str(key).casefold() in wanted:
            found = from_value(value)
            if found:
                return found
    # Stable runners may wrap artifacts under a single ``artifacts`` mapping.
    for wrapper_key in ("artifacts", "output", "views", "stages"):
        wrapper = result.get(wrapper_key)
        if isinstance(wrapper, Mapping):
            for key, value in wrapper.items():
                if str(key).casefold() in wanted:
                    found = from_value(value)
                    if found:
                        return found
    return []


def _stage_groups(result: Mapping[str, Any], stage: str) -> tuple[tuple[str, ...], ...]:
    rows = _stage_rows(result, stage)
    groups: list[tuple[str, ...]] = []
    for row in rows:
        refs = tuple(_extract_refs(row))
        if refs:
            groups.append(refs)
    return tuple(groups)


def _flatten(groups: Sequence[Sequence[str]]) -> tuple[str, ...]:
    return tuple(ref for group in groups for ref in group)


def _walk(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _walk(child)


def _named_values(value: Any, names: Sequence[str]) -> list[Any]:
    wanted = {name.casefold() for name in names}
    return [child for key, child in _walk(value) if key.casefold() in wanted]


def _manifest(result: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("manifest", "run_manifest", "active_period_manifest"):
        value = result.get(key)
        if isinstance(value, Mapping):
            return value
    return result


def _status_values(value: Any) -> list[str]:
    values: list[str] = []
    for key, child in _walk(value):
        if (
            key.casefold()
            in {
                "status",
                "phase_status",
                "period_status",
                "resolution_status",
                "semantic_status",
                "semantic_resolution",
            }
            and isinstance(child, str)
        ):
            values.append(child.casefold())
    return values


def _all_strings_under_keys(value: Any, names: Sequence[str]) -> list[str]:
    wanted = {name.casefold() for name in names}
    found: list[str] = []

    def collect(child: Any) -> None:
        if isinstance(child, str):
            found.append(child)
        elif isinstance(child, Mapping):
            for nested in child.values():
                collect(nested)
        elif isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
            for nested in child:
                collect(nested)

    for key, child in _walk(value):
        if key.casefold() in wanted:
            collect(child)
    return found


def _contains_key(value: Any, names: Sequence[str]) -> bool:
    wanted = {name.casefold() for name in names}
    return any(key.casefold() in wanted for key, _ in _walk(value))


def _assert_body_free(value: Any) -> None:
    """Check manifest/phase metadata does not leak synthetic body text."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _BODY_KEYS:
                assert child in (None, "", [], {}, ()), f"body escaped metadata artifact at {key}"
            _assert_body_free(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            _assert_body_free(child)


def _period_row_for_refs(result: Mapping[str, Any], refs: Sequence[str], stage: str = "materialized") -> Mapping[str, Any]:
    wanted = tuple(refs)
    for row in _stage_rows(result, stage):
        if isinstance(row, Mapping) and tuple(_extract_refs(row)) == wanted:
            return row
    pytest.fail(f"{stage} stage has no period with exact refs {wanted}")


def _has_blocked_status(value: Mapping[str, Any]) -> bool:
    statuses = set(_status_values(value))
    if "blocked" in statuses:
        return True
    for key in ("release_gate", "ref_gate", "validation", "gate"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and str(nested.get("status") or "").casefold() == "blocked":
            return True
    return False


def _mutate_ref_stage(result: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """Drop or append one ref in one output stage for validator tests."""

    broken = deepcopy(dict(result))
    target_stage = "rendered"
    rows = _stage_rows(broken, target_stage)
    if not rows:
        pytest.fail("rendered structured refs are required for ref-drift validation")
    target = next((row for row in rows if isinstance(row, Mapping) and _extract_refs(row)), None)
    if not isinstance(target, Mapping):
        pytest.fail("rendered stage must expose ordered message refs")
    for key in _REF_KEYS:
        value = target.get(key)
        values = _sequence(value)
        if not values:
            continue
        replacement = list(values)
        if mode == "missing":
            replacement.pop()
        else:
            replacement.append("synthetic-extra-ref-not-in-ledger")
        target[key] = replacement  # type: ignore[index]
        return broken
    pytest.fail("rendered stage did not expose a mutable ref list")


def _metric_status(result: Mapping[str, Any], family: str) -> str:
    """Find the status for a zero-count context/evidence metric."""

    family = family.casefold()
    for key, value in _walk(result):
        if key.casefold() != family or not isinstance(value, Mapping):
            continue
        count = value.get("count", value.get("total", value.get("refs")))
        if count == 0 or count == [] or count == ():
            status = value.get("status", value.get("measurement_status", value.get("state")))
            if isinstance(status, str):
                return status.casefold()
    return ""


def _release_gate_state(result: Mapping[str, Any]) -> str:
    manifest = _manifest(result)
    for container in (manifest, result):
        for key in ("release_gate", "release_status", "release_ready", "release_allowed", "contract_gate"):
            value = container.get(key)
            if isinstance(value, Mapping):
                status = value.get("status") or value.get("state") or value.get("decision")
                if isinstance(status, str):
                    return status.casefold()
                for bool_key in ("ready", "allowed", "passed", "pass"):
                    if bool_key in value:
                        return "pass" if value[bool_key] is True else "blocked"
            elif isinstance(value, bool):
                return "pass" if value else "blocked"
            elif isinstance(value, str):
                return value.casefold()
    return ""


def test_full_periods_are_not_four_row_chunks_and_tail_is_retained() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)

    for stage in ("materialized", "reconstructed", "rendered"):
        groups = _stage_groups(result, stage)
        assert groups == EXPECTED_PERIOD_REFS, f"{stage} must preserve every period/ref in source order"
    assert len(_stage_rows(result, "rendered")) == 3
    assert len(_extract_refs(_period_row_for_refs(result, EXPECTED_PERIOD_REFS[0], "materialized"))) == 6


def test_cross_midnight_period_stays_one_period_and_gap_is_only_a_candidate() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)

    groups = _stage_groups(result, "reconstructed")
    assert groups == EXPECTED_PERIOD_REFS
    assert sum(ref in groups[0] for ref in EXPECTED_PERIOD_REFS[0]) == 6
    assert len(groups) == 3, "calendar day must not create an extra period"

    gap_row = _period_row_for_refs(result, GAP_PERIOD_REFS, "reconstructed")
    candidate_values = _named_values(
        gap_row,
        ("gap_candidate", "gap_candidates", "retrieval_candidate", "retrieval_candidates", "continuity_candidates"),
    )
    assert candidate_values, "the long gap must remain an explicit retrieval candidate"
    encoded_gap = json.dumps(candidate_values, ensure_ascii=False, sort_keys=True).casefold()
    assert "gap" in encoded_gap
    semantic_values = []
    for key, value in _walk(gap_row):
        lowered = key.casefold()
        if lowered in {"semantic_status", "semantic_resolution", "semantic_resolved", "resolution_status", "state"}:
            semantic_values.append(value)
    assert semantic_values, "gap period must expose semantic status separately from retrieval candidates"
    assert all(value is not True and value != "resolved" for value in semantic_values)


def test_selection_does_not_rank_by_body_topic_value_or_message_count() -> None:
    api = _public_api()
    baseline = api.run(FIXTURE_ROWS, max_periods=3)

    adversarial = deepcopy(FIXTURE_ROWS)
    for index, row in enumerate(adversarial, start=1):
        row["content"] = f"SYNTHETIC-ADVERSARIAL-BODY-{index}"
        row["body"] = f"SYNTHETIC-ADVERSARIAL-BODY-{index}"
        row["topic"] = f"SYNTHETIC-ADVERSARIAL-TOPIC-{index}"
        row["topic_hint"] = f"SYNTHETIC-ADVERSARIAL-TOPIC-{index}"
        row["information_value"] = "none" if index % 2 else "high"
        row["value"] = -index
        row["message_count"] = 1 if index % 2 else 9999
    changed = api.run(adversarial, max_periods=3)

    baseline_selection = _stage_groups(baseline, "selection") or _stage_groups(baseline, "materialized")
    changed_selection = _stage_groups(changed, "selection") or _stage_groups(changed, "materialized")
    assert baseline_selection == EXPECTED_PERIOD_REFS
    assert changed_selection == baseline_selection


def test_three_periods_cover_two_dates_and_direct_group_scopes_with_one_card_each() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)
    cards = _stage_rows(result, "rendered")
    assert len(cards) == 3

    scope_pairs = []
    for row in cards:
        assert isinstance(row, Mapping)
        refs = tuple(_extract_refs(row))
        assert refs in EXPECTED_PERIOD_REFS
        scope = row.get("scope")
        if isinstance(scope, Mapping):
            scope_pairs.append(
                (
                    scope.get("local_day") or scope.get("date") or row.get("local_day") or row.get("date"),
                    scope.get("chat_type") or row.get("chat_type"),
                )
            )
        else:
            scope_pairs.append((row.get("local_day") or row.get("date"), row.get("chat_type")))
        # Internal episodes are nested evidence, not additional cards.
        episodes = row.get("internal_episodes") or row.get("episodes")
        if refs == EXPECTED_PERIOD_REFS[1]:
            assert episodes is not None, "parallel period must retain its internal episode structure"
            assert len(_sequence(episodes)) == 2
    assert {pair[1] for pair in scope_pairs} >= {"direct", "group"}
    assert len({pair[0] for pair in scope_pairs if pair[0]}) >= 2


def test_greeting_ack_media_and_system_are_retained_but_media_system_are_not_evidence() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)
    all_refs = _flatten(_stage_groups(result, "rendered"))
    assert MEDIA_REF in all_refs and SYSTEM_REF in all_refs
    assert "SYNTHETIC-GREETING-BODY" in json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC-ACK-BODY" in json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC-MEDIA-BODY" in json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC-SYSTEM-BODY" in json.dumps(result, ensure_ascii=False)

    evidence_tokens = {
        _canonical_ref(token)
        for token in _all_strings_under_keys(
            result,
            ("evidence", "evidence_refs", "evidence_ids", "supporting_evidence_refs"),
        )
    }
    assert MEDIA_REF not in evidence_tokens
    assert SYSTEM_REF not in evidence_tokens
    media_values = _named_values(result, ("media",))
    media_encoded = json.dumps(media_values, ensure_ascii=False, sort_keys=True).casefold()
    assert "unavailable" in media_encoded
    assert any(value is False for value in _named_values(result, ("semantic_evidence",)))


def test_parallel_strands_keep_one_authoritative_timeline() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)
    group_row = _period_row_for_refs(result, EXPECTED_PERIOD_REFS[1], "reconstructed")
    parallel_values = _named_values(group_row, ("parallel_strands", "strands", "parallel_topic_strands", "topic_strands"))
    assert parallel_values, "parallel group must retain explicit strand structure"
    encoded = json.dumps(parallel_values, ensure_ascii=False, sort_keys=True)
    assert "strand-a" in encoded and "strand-b" in encoded
    timeline_refs = _extract_refs(group_row)
    assert timeline_refs == list(EXPECTED_PERIOD_REFS[1])


def test_zero_context_and_evidence_are_not_measured_not_passed() -> None:
    api = _public_api()
    _, expected = _build_fixture()
    group_rows = [row for row in FIXTURE_ROWS if row["message_ref"] in expected[1]]
    result = api.run(group_rows, max_periods=1)
    assert _metric_status(result, "context") == "not_measured"
    assert _metric_status(result, "evidence") == "not_measured"
    # A broad pass marker is not an acceptable substitute for the two explicit
    # N/A metrics when their denominator is zero.
    assert _metric_status(result, "context") != "pass"
    assert _metric_status(result, "evidence") != "pass"


def test_whole_period_cap_defers_without_truncating_the_period() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3, whole_period_cap=4)
    p1_row = _period_row_for_refs(result, EXPECTED_PERIOD_REFS[0], "materialized")
    assert tuple(_extract_refs(p1_row)) == EXPECTED_PERIOD_REFS[0]
    statuses = _status_values(p1_row)
    assert any(status in {"deferred", "pending", "over_capacity", "blocked"} for status in statuses), (
        "a whole period over cap must remain complete in refs and be deferred as a unit"
    )
    assert len(_stage_rows(result, "materialized")) == 3


def test_manifest_phase_hash_provider_zero_and_production_blocked() -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)
    manifest = _manifest(result)
    provider_key = next((key for key in ("provider_calls", "provider_call_count") if key in manifest), None)
    assert provider_key is not None, "manifest must expose the provider call counter"
    assert manifest[provider_key] == 0
    assert manifest.get("production_blocked") is True
    phase = manifest.get("phase") or manifest.get("run_phase") or manifest.get("pipeline_phase")
    assert isinstance(phase, str) and phase
    hash_value = next(
        (
            manifest.get(key)
            for key in ("hash", "run_hash", "input_hash", "input_fingerprint", "selection_hash", "selection_manifest_sha256")
            if manifest.get(key)
        ),
        None,
    )
    assert isinstance(hash_value, str) and len(hash_value) >= 8
    _assert_body_free(manifest)
    assert _contains_key(result, ("phase", "manifest"))


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_missing_or_extra_rendered_ref_blocks_result(mode: str) -> None:
    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)
    broken = _mutate_ref_stage(result, mode)
    validation = api.validate(broken)
    assert _has_blocked_status(validation), f"{mode} ref drift must be blocked, got {validation}"


def test_old_four_row_happy_fixture_cannot_pass_new_release_gate() -> None:
    api = _public_api()
    result = api.run(_legacy_four_row_fixture(), max_periods=3)
    gate_state = _release_gate_state(result)
    assert gate_state, "runner must expose a release/contract gate state"
    assert gate_state not in {"pass", "passed", "complete", "accepted", "ready", "allowed"}
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).casefold()
    assert any(marker in encoded for marker in ("coverage", "period_count", "direct", "group", "three_period"))


def test_full_stage_ref_alignment_is_exact_and_ordered() -> None:
    """A compact single gate for expected=materialized=reconstructed=rendered."""

    api = _public_api()
    result = api.run(FIXTURE_ROWS, max_periods=3)
    expected = EXPECTED_PERIOD_REFS
    materialized = _stage_groups(result, "materialized")
    reconstructed = _stage_groups(result, "reconstructed")
    rendered = _stage_groups(result, "rendered")
    assert materialized == expected
    assert reconstructed == expected
    assert rendered == expected
    assert _flatten(materialized) == EXPECTED_ALL_REFS
    assert _flatten(reconstructed) == EXPECTED_ALL_REFS
    assert _flatten(rendered) == EXPECTED_ALL_REFS
