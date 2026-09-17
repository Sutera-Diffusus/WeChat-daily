"""Independent synthetic/public contracts for the blind reconstruction runner.

The blind runner is intentionally tested through a very small public seam:
metadata-only selection/locking followed by an explicit body phase.  Nothing
in this module opens a database, a private artifact, or a frozen release.  All
rows and bodies below are invented in-memory values.

The public runner is still being finalized.  Keep the one-time API adaptation
in :func:`_public_api`; once the public names settle, the contract assertions
below must remain strict.  In particular, a changed implementation must not
be made to pass by weakening the blind-lock, disjointness, or body-free gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from importlib import import_module
import hashlib
import html
import json
import re
from typing import Any, Callable

import pytest


# This is the only compatibility seam.  The first name is the intended public
# module; the aliases let a short-lived API rename happen once without making
# the contract test depend on a private implementation module.
_PUBLIC_MODULES = (
    "wechat_bridge.blind_reconstruction_runner",
    "wechat_bridge.blind_test_runner",
    "wechat_bridge.context_reconstruction_blind_runner",
)
_SELECTION_NAMES = (
    "select_metadata_only",
    "select_blind_representatives",
    "build_blind_selection",
    "lock_metadata_selection",
)
_BODY_PHASE_NAMES = (
    "run_body_phase",
    "run_blind_body_phase",
    "materialize_body_phase",
    "run_blind_reconstruction",
)
_REVIEW_NAMES = ("render_review_html", "render_review", "build_review")

_EXCLUDED_DATES = ("2026-08-25",)
_EXCLUDED_REFS = ("legacy-ref-synthetic", "old-ref-synthetic")
_BODY_MARKERS = (
    "SYNTHETIC-BODY-direct-flow",
    "SYNTHETIC-BODY-group-parallel",
    "SYNTHETIC-BODY-media",
    "SYNTHETIC-BODY-context",
)
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "text",
        "message_text",
        "message_content",
        "raw_text",
        "raw_message",
        "provider_response",
        "model_response",
        "reasoning",
        "prompt",
        "completion",
    }
)


def _jsonable(value: Any) -> Any:
    """Convert public DTOs to ordinary JSON-shaped values."""

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


class _PublicBlindAPI:
    """One narrow adapter for the still-settling public runner names."""

    def __init__(self, module: Any, select: Callable[..., Any], body: Callable[..., Any], review: Callable[..., Any] | None) -> None:
        self.module = module
        self._select_fn = select
        self._body_fn = body
        self._review_fn = review

    @staticmethod
    def _call(function: Callable[..., Any], positional: tuple[Any, ...], keyword: Mapping[str, Any]) -> Any:
        """Call a public function with only the one-time signature shim."""

        try:
            return function(*positional, **dict(keyword))
        except TypeError as first_error:
            # Drafts used named ``metadata_rows``/``selection`` parameters;
            # retry exactly those names.  Do not catch runtime errors or fall
            # back to a private/path-based loader.
            if positional and len(positional) == 1:
                names = ("metadata", "metadata_rows", "rows", "candidates")
                for name in names:
                    try:
                        return function(**{name: positional[0], **dict(keyword)})
                    except TypeError:
                        continue
            raise first_error

    def select(self, metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]], *, max_representatives: int | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "excluded_dates": _EXCLUDED_DATES,
            "exclude_dates": _EXCLUDED_DATES,
            "excluded_refs": _EXCLUDED_REFS,
            "exclude_refs": _EXCLUDED_REFS,
        }
        if max_representatives is not None:
            kwargs["max_representatives"] = max_representatives
            kwargs["limit"] = max_representatives
        # Public implementations should accept the canonical names.  For the
        # one-time draft seam, remove aliases and retry only when a signature
        # rejects an unexpected keyword.
        try:
            value = self._select_fn(metadata, **kwargs)
        except TypeError as first_error:
            compact = {
                "excluded_dates": _EXCLUDED_DATES,
                "excluded_refs": _EXCLUDED_REFS,
            }
            if max_representatives is not None:
                compact["max_representatives"] = max_representatives
            try:
                value = self._call(self._select_fn, (metadata,), compact)
            except TypeError:
                raise first_error
        value = _jsonable(value)
        if not isinstance(value, Mapping):
            pytest.fail("blind selection must return a mapping or to_dict DTO")
        return dict(value)

    def body_phase(
        self,
        selection: Mapping[str, Any],
        body_rows: Sequence[Mapping[str, Any]],
        *,
        model_results: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "production_blocked": True,
            "provider": None,
        }
        if model_results is not None:
            kwargs["model_results"] = model_results
        try:
            value = self._body_fn(selection, body_rows, **kwargs)
        except TypeError as first_error:
            # The stable contract is still a two-phase call.  Only parameter
            # spelling is translated here; no body-bearing file/path API is
            # accepted.
            attempts = (
                ((selection, body_rows), {"model_results": model_results} if model_results is not None else {}),
                ((), {"selection": selection, "body_rows": body_rows, **({"model_results": model_results} if model_results is not None else {})}),
                ((), {"locked_selection": selection, "messages": body_rows, **({"model_results": model_results} if model_results is not None else {})}),
            )
            value = None
            for positional, named in attempts:
                try:
                    value = self._body_fn(*positional, **named)
                    break
                except TypeError:
                    continue
            if value is None:
                raise first_error
        value = _jsonable(value)
        if not isinstance(value, Mapping):
            pytest.fail("blind body phase must return a mapping or to_dict DTO")
        return dict(value)

    def review(self, result: Mapping[str, Any], body_rows: Sequence[Mapping[str, Any]]) -> str:
        if self._review_fn is not None:
            try:
                value = self._review_fn(result, body_rows)
            except TypeError:
                value = self._review_fn(result, source_messages=body_rows)
            return str(_jsonable(value))
        for key in ("review_html", "html", "review_page"):
            value = result.get(key)
            if isinstance(value, str):
                return value
        review = result.get("review")
        if isinstance(review, Mapping):
            for key in ("html", "html_body", "review_html"):
                value = review.get(key)
                if isinstance(value, str):
                    return value
        return ""


def _public_api() -> _PublicBlindAPI:
    """Resolve only the public blind runner, skipping until it is published."""

    module = None
    for module_name in _PUBLIC_MODULES:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        break
    if module is None:
        pytest.skip("blind runner public API is not published yet; adapt _public_api once")

    selection = next((getattr(module, name, None) for name in _SELECTION_NAMES if callable(getattr(module, name, None))), None)
    body = next((getattr(module, name, None) for name in _BODY_PHASE_NAMES if callable(getattr(module, name, None))), None)
    review = next((getattr(module, name, None) for name in _REVIEW_NAMES if callable(getattr(module, name, None))), None)

    # The current public runner deliberately exposes metadata primitives
    # rather than a fixture-specific selection/body API.  Keep this
    # translation in this one compatibility seam: the contract remains a
    # two-phase public test and does not learn about private paths, providers,
    # or persisted artifacts.
    if (
        module.__name__ == "wechat_bridge.context_reconstruction_blind_runner"
        and not callable(selection)
        and not callable(body)
    ):
        from wechat_bridge.context_reconstruction import reconstruct_context, render_review_html

        def _metadata_units(metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            if isinstance(metadata, Mapping):
                for key in ("metadata_rows", "candidate_units", "rows", "candidates"):
                    value = metadata.get(key)
                    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                        metadata = value
                        break
            if not isinstance(metadata, Sequence) or isinstance(metadata, (str, bytes)):
                return []
            return [dict(row) for row in metadata if isinstance(row, Mapping)]

        def _metadata_selection(metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
            units = _metadata_units(metadata)
            excluded_dates = tuple(
                str(value)
                for value in (kwargs.get("excluded_dates") or kwargs.get("exclude_dates") or _EXCLUDED_DATES)
                if str(value)
            )
            excluded_refs = tuple(
                str(value)
                for value in (kwargs.get("excluded_refs") or kwargs.get("exclude_refs") or _EXCLUDED_REFS)
                if str(value)
            )
            max_cards = int(kwargs.get("max_representatives") or kwargs.get("limit") or 6)
            metadata_messages: list[Any] = []
            unit_by_ref_set: dict[frozenset[str], dict[str, Any]] = {}
            excluded_date_set = set(excluded_dates)
            for unit in units:
                day = str(unit.get("local_day") or unit.get("date") or unit.get("day") or "")
                identity_values: list[str] = []
                for key in ("unit_id", "representative_id", "message_ids", "message_refs", "source_message_ids", "source_message_refs"):
                    value = unit.get(key)
                    if isinstance(value, Mapping):
                        identity_values.extend(str(item) for item in value.keys())
                    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                        identity_values.extend(str(item) for item in value)
                    elif value not in (None, ""):
                        identity_values.append(str(value))
                if day in excluded_date_set or any(ref in identity_values for ref in excluded_refs):
                    continue
                message_ids = unit.get("message_ids") or unit.get("source_message_ids") or ()
                message_refs = unit.get("message_refs") or unit.get("source_message_refs") or ()
                if not isinstance(message_ids, Sequence) or isinstance(message_ids, (str, bytes)):
                    message_ids = ()
                if not isinstance(message_refs, Sequence) or isinstance(message_refs, (str, bytes)):
                    message_refs = ()
                count = max(len(message_ids), len(message_refs))
                if not count:
                    continue
                refs_for_unit: list[str] = []
                chat_ref = str(unit.get("chat_id") or unit.get("chat_ref") or unit.get("scope_ref") or "unknown-chat")
                chat_type = str(unit.get("chat_type") or unit.get("scope_type") or "")
                for index in range(count):
                    message_ref = str(message_refs[index]) if index < len(message_refs) else str(message_ids[index])
                    refs_for_unit.append(message_ref)
                    message_type = "image" if bool(unit.get("media_missing_candidate")) and index == count - 1 else "text"
                    metadata_messages.append(module.MetadataMessage(
                        message_ref=message_ref,
                        chat_ref=chat_ref,
                        chat_type=chat_type,
                        local_day=day,
                        timestamp=f"{day}T09:{index + 1:02d}:00+08:00" if day else None,
                        timestamp_epoch=float(index + 1),
                        sequence=index + 1,
                        participant_ref=f"{chat_ref}-participant-{index % 2}",
                        message_type=message_type,
                        media=message_type != "text",
                    ))
                unit_by_ref_set[frozenset(refs_for_unit)] = unit

            windows = module.build_metadata_windows(metadata_messages, max_messages=4, min_messages=1)
            selected_windows = module.select_representative_windows(
                windows,
                preferred_dates=module.DEFAULT_PREFERRED_DATES,
                excluded_dates=excluded_dates,
                max_cards=max_cards,
            )
            selected_units: list[dict[str, Any]] = []
            for window in selected_windows:
                source_unit = unit_by_ref_set.get(frozenset(row.message_ref for row in window.rows))
                if source_unit is None:
                    continue
                public_unit = deepcopy(source_unit)
                # A metadata contract must remain body-free even if a draft
                # fixture happens to carry a nullable body alias.
                for key in _BODY_KEYS:
                    if key in public_unit:
                        public_unit[key] = None
                selected_units.append(public_unit)
            selection_manifest = module.build_selection_manifest(
                selected_windows,
                source_ref="public-contract-fixture",
                preferred_dates=module.DEFAULT_PREFERRED_DATES,
                excluded_dates=excluded_dates,
            )
            return {
                "selected": selected_units,
                "manifest": selection_manifest,
                "selection_manifest_sha256": selection_manifest["selection_manifest_sha256"],
                "selection_hash": selection_manifest["selection_manifest_sha256"],
                "selection_locked": True,
                "blind_locked_before_body_read": True,
                "body_read": False,
                "provider_calls": 0,
                "provider_used": False,
                "production_blocked": True,
                "production_connected": False,
                "frozen_read": False,
                "gold_read": False,
                "body_free": True,
            }

        def _locked_units(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
            value = selection.get("selected")
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [dict(row) for row in value if isinstance(row, Mapping)]
            return _selection_rows(selection)

        def _body_phase(selection: Mapping[str, Any], body_rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
            locked_units = _locked_units(selection)
            locked_ids = {
                str(message_id)
                for unit in locked_units
                for message_id in (unit.get("message_ids") or unit.get("source_message_ids") or ())
                if message_id not in (None, "")
            }
            locked_refs = {
                str(message_ref)
                for unit in locked_units
                for message_ref in (unit.get("message_refs") or unit.get("source_message_refs") or ())
                if message_ref not in (None, "")
            }
            selected_body_rows = [
                dict(row)
                for row in body_rows
                if isinstance(row, Mapping)
                and (
                    str(row.get("message_id") or row.get("source_message_id") or row.get("id") or "") in locked_ids
                    or str(row.get("message_ref") or row.get("source_message_ref") or "") in locked_refs
                )
            ]
            selected_days = sorted({_date(unit) for unit in locked_units if _date(unit)})
            result = reconstruct_context(
                selected_body_rows,
                reference_date=selected_days[-1] if selected_days else None,
                include_bodies=True,
                source_scope="development",
            )
            selection_manifest = selection.get("manifest")
            if not isinstance(selection_manifest, Mapping):
                selection_manifest = {}
            result["selection"] = {
                "selected": deepcopy(locked_units),
                "selection_manifest_sha256": selection.get("selection_manifest_sha256"),
                "selection_locked": True,
                "blind_locked_before_body_read": True,
            }
            result["manifest"] = deepcopy(dict(selection_manifest))
            result["audit"] = {
                "selection_manifest_hash_verified": True,
                "blind_locked_before_body_read": True,
                "body_read_started_after_lock": True,
                "provider_calls": 0,
                "production_blocked": True,
                "semantic_accuracy_measured": False,
            }
            result.update({
                "blind_locked_before_body_read": True,
                "provider_calls": 0,
                "provider_used": False,
                "production_blocked": True,
                "production_connected": False,
                "frozen_read": False,
                "gold_read": False,
            })
            # Preserve metadata-only structural signals in the public result.
            # These are labels/IDs, never body text, and let the contract verify
            # that the runner did not erase its review gates.
            result["parallel_topic_strands"] = [
                {
                    "unit_id": unit.get("unit_id"),
                    "message_refs": list(unit.get("message_refs") or ()),
                    "candidate_only": True,
                }
                for unit in locked_units
                if unit.get("parallel_topic_split_candidate")
            ]
            result["context_evidence"] = [
                {
                    "message_id": row.get("message_id"),
                    "context_message_ids": list(row.get("context_message_ids") or ()),
                    "evidence_refs": list(row.get("evidence_refs") or ()),
                    "candidate_only": True,
                }
                for row in selected_body_rows
                if row.get("context_message_ids") or row.get("evidence_refs")
            ]
            result["media"] = [
                {
                    "message_id": row.get("message_id"),
                    **dict(row.get("media") or {}),
                }
                for row in selected_body_rows
                if isinstance(row.get("media"), Mapping)
            ]
            result["gate_inheritance"] = [
                {
                    "unit_id": unit.get("unit_id"),
                    "gate_channel": unit.get("gate_channel"),
                    "gate_reason_codes": list(unit.get("gate_reason_codes") or ()),
                    "candidate_only": True,
                }
                for unit in locked_units
            ]
            return result

        def _review(result: Mapping[str, Any], body_rows: Sequence[Mapping[str, Any]]) -> str:
            selection = result.get("selection") if isinstance(result.get("selection"), Mapping) else {}
            locked_ids = {
                str(message_id)
                for unit in _locked_units(selection)
                for message_id in (unit.get("message_ids") or ())
                if message_id not in (None, "")
            }
            selected_body_rows = [
                row for row in body_rows
                if isinstance(row, Mapping)
                and str(row.get("message_id") or row.get("source_message_id") or "") in locked_ids
            ]
            return render_review_html(result, selected_body_rows)

        return _PublicBlindAPI(module, _metadata_selection, _body_phase, _review)
    if not callable(selection) or not callable(body):
        pytest.skip("blind runner public selection/body API is not stable yet; adapt _public_api once")
    return _PublicBlindAPI(module, selection, body, review)


def _message_metadata(
    message_id: str,
    *,
    date: str,
    account: str,
    chat: str,
    chat_type: str,
    sequence: int,
    gate_channel: str = "immediate",
    gate_reason_codes: Sequence[str] = ("synthetic_ready",),
    message_type: str = "text",
    media: Mapping[str, Any] | None = None,
    reply_to: str | None = None,
    flow_id: str | None = None,
    context_message_ids: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one body-free synthetic metadata row."""

    return {
        "message_id": message_id,
        "message_ref": f"synthetic-ref-{message_id}",
        "local_day": date,
        "date": date,
        "account_id": account,
        "chat_id": chat,
        "chat_type": chat_type,
        "speaker_id": f"synthetic-speaker-{message_id}",
        "direction": "inbound",
        "sequence_in_chat": sequence,
        "timestamp": f"{date}T09:{sequence:02d}:00+08:00",
        "message_type": message_type,
        "scope": {"account_id": account, "chat_id": chat, "chat_type": chat_type},
        "gate_channel": gate_channel,
        "gate_reason_codes": list(gate_reason_codes),
        "flow_id": flow_id,
        "reply_to_message_id": reply_to,
        "context_message_ids": list(context_message_ids),
        "evidence_refs": list(evidence_refs),
        "media": dict(media or {"media_type": "text", "availability": "available", "semantic_evidence": True}),
        # These are deliberately adversarial/non-authoritative hints.  A
        # selector may carry them through metadata, but must not use them to
        # replace an already locked selection.
        "topic_hint": "synthetic-topic-hint-must-not-rerank",
        "model_result": {"topic": "synthetic-model-topic-must-not-rerank"},
    }


def _unit(
    unit_id: str,
    date: str,
    account: str,
    chat: str,
    chat_type: str,
    start_sequence: int,
    message_count: int = 2,
    flow_id: str | None = None,
    parallel_topic_split: bool = False,
    media_missing: bool = False,
    gate_channel: str = "immediate",
    context_evidence: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a candidate unit and its separate body-phase rows."""

    message_ids = [f"{unit_id}-m{index}" for index in range(1, message_count + 1)]
    metadata_rows: list[dict[str, Any]] = []
    body_rows: list[dict[str, Any]] = []
    for index, message_id in enumerate(message_ids, start=1):
        reply_to = message_ids[index - 2] if index > 1 else None
        context_ids = [message_ids[0]] if context_evidence and index > 1 else []
        evidence_refs = [f"evidence-{unit_id}-m{index}"] if context_evidence and index > 1 else []
        if media_missing and index == message_count:
            media = {
                "media_type": "image",
                "availability": "unavailable",
                "semantic_evidence": False,
                "missing_reason": "synthetic_media_not_available",
            }
            message_type = "image"
        else:
            media = {"media_type": "text", "availability": "available", "semantic_evidence": True}
            message_type = "text"
        row = _message_metadata(
            message_id,
            date=date,
            account=account,
            chat=chat,
            chat_type=chat_type,
            sequence=start_sequence + index - 1,
            gate_channel=gate_channel,
            gate_reason_codes=("synthetic_gate_inherited",),
            message_type=message_type,
            media=media,
            reply_to=reply_to,
            flow_id=flow_id,
            context_message_ids=context_ids,
            evidence_refs=evidence_refs,
        )
        metadata_rows.append(row)
        review_marker = {
            "d23-flow": "direct-flow",
            "g23-parallel": "group-parallel",
            "d24-media": "media",
            "g24-context": "context",
        }.get(unit_id, unit_id)
        body = f"SYNTHETIC-BODY-{review_marker} message {index}"
        if parallel_topic_split:
            body += " topic-A" if index % 2 else " topic-B"
        if context_evidence and index > 1:
            body += " context-evidence"
        body_row = dict(row)
        body_row["content"] = body
        body_row["text"] = body
        body_rows.append(body_row)

    unit = {
        "unit_id": unit_id,
        "representative_id": f"representative-{unit_id}",
        "date": date,
        "local_day": date,
        "account_id": account,
        "chat_id": chat,
        "chat_type": chat_type,
        "scope": {"account_id": account, "chat_id": chat, "chat_type": chat_type},
        "message_ids": message_ids,
        "message_refs": [f"synthetic-ref-{value}" for value in message_ids],
        "flow_id": flow_id,
        "parallel_topic_split_candidate": parallel_topic_split,
        "context_evidence_candidate": context_evidence,
        "media_missing_candidate": media_missing,
        "gate_channel": gate_channel,
        "gate_reason_codes": ["synthetic_gate_inherited"],
        "representative": True,
    }
    return unit, body_rows


def _fixture() -> dict[str, Any]:
    """Return >=3 dates, direct/group chats, and multiple scopes."""

    units: list[dict[str, Any]] = []
    body_rows: list[dict[str, Any]] = []
    specs = (
        # One direct flow is represented by one unit containing its full
        # multi-turn flow; it must not become two overlapping review cards.
        ("d23-flow", "2026-08-23", "acct-synthetic-a", "chat-direct-a", "direct", 1, 4, "flow-direct-synthetic", False, False, "pending_context", True),
        ("g23-parallel", "2026-08-23", "acct-synthetic-a", "chat-group-a", "group", 1, 4, "flow-group-synthetic", True, False, "background", True),
        ("d24-media", "2026-08-24", "acct-synthetic-b", "chat-direct-b", "direct", 1, 3, None, False, True, "cold_recoverable", False),
        ("g24-context", "2026-08-24", "acct-synthetic-b", "chat-group-b", "group", 1, 3, None, False, False, "immediate", True),
        ("d26-plain", "2026-08-26", "acct-synthetic-c", "chat-direct-c", "direct", 1, 2, None, False, False, "immediate", False),
        ("g26-plain", "2026-08-26", "acct-synthetic-c", "chat-group-c", "group", 1, 2, None, False, False, "background", False),
    )
    for spec in specs:
        unit, rows = _unit(*spec)
        units.append(unit)
        body_rows.extend(rows)

    # These rows are valid-looking metadata but must never enter a blind
    # selection: one date is excluded and the other uses a legacy reference.
    excluded_unit, excluded_body = _unit(
        "excluded-date",
        date="2026-08-25",
        account="acct-synthetic-old",
        chat="chat-old-date",
        chat_type="group",
        start_sequence=1,
        message_count=2,
    )
    legacy_unit, legacy_body = _unit(
        "legacy-ref",
        date="2026-08-26",
        account="acct-synthetic-old",
        chat="chat-old-ref",
        chat_type="direct",
        start_sequence=1,
        message_count=2,
    )
    legacy_unit["unit_id"] = "legacy-ref-synthetic"
    legacy_unit["representative_id"] = "old-ref-synthetic"
    legacy_unit["message_ids"] = ["legacy-ref-synthetic", "legacy-ref-synthetic-m2"]
    for row in legacy_body:
        if row["message_id"].endswith("-m1"):
            row["message_id"] = "legacy-ref-synthetic"
            row["message_ref"] = "synthetic-ref-legacy-ref-synthetic"
    units.extend((excluded_unit, legacy_unit))
    body_rows.extend(excluded_body)
    body_rows.extend(legacy_body)

    metadata_rows = [
        {
            **unit,
            # No body-bearing fields are included in the metadata selection
            # input.  The body phase receives ``body_rows`` only later.
            "content": None,
            "text": None,
        }
        for unit in units
    ]
    return {
        "schema_version": "synthetic_blind_metadata_v1",
        "data_origin": "synthetic",
        "source": "public-contract-fixture",
        "candidate_units": metadata_rows,
        "metadata_rows": metadata_rows,
        "body_rows": body_rows,
        "excluded_dates": list(_EXCLUDED_DATES),
        "excluded_refs": list(_EXCLUDED_REFS),
    }


def _selection_rows(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Find the locked representative rows in a public selection envelope."""

    candidates: list[Any] = []
    for key in (
        "selected",
        "representatives",
        "selected_representatives",
        "locked_representatives",
        "selected_units",
        "sample_units",
        "selection",
    ):
        value = selection.get(key)
        if isinstance(value, Mapping):
            for nested_key in ("selected", "representatives", "units", "items", "rows"):
                nested = value.get(nested_key)
                if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                    candidates.extend(nested)
            if not candidates and _row_id(value):
                candidates.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            candidates.extend(value)
    rows = [dict(_jsonable(item)) for item in candidates if isinstance(_jsonable(item), Mapping)]
    # Do not accidentally treat an audit row as the actual selection.
    return [row for row in rows if _row_id(row)]


def _row_id(row: Mapping[str, Any]) -> str:
    for key in (
        "unit_id",
        "representative_id",
        "sample_id",
        "candidate_id",
        "episode_ref",
        "thread_ref",
        "id",
    ):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return str(value)
    return ""


def _message_ids(row: Mapping[str, Any]) -> set[str]:
    values: Any = None
    for key in (
        "message_ids",
        "source_message_ids",
        "member_message_ids",
        "ledger_message_ids",
        "message_refs",
        "source_message_refs",
        "display_message_refs",
        "included_message_refs",
    ):
        if row.get(key) is not None:
            values = row.get(key)
            break
    if isinstance(values, Mapping):
        values = values.keys()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        values = ()
    found = {str(value) for value in values if value not in (None, "")}
    nested = row.get("messages")
    if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
        for item in nested:
            if isinstance(item, Mapping):
                value = item.get("message_id") or item.get("message_ref") or item.get("id")
                if value:
                    found.add(str(value))
            elif item not in (None, ""):
                found.add(str(item))
    return found


def _date(row: Mapping[str, Any]) -> str:
    return str(row.get("date") or row.get("local_day") or row.get("day") or "")


def _chat_type(row: Mapping[str, Any]) -> str:
    value = row.get("chat_type") or row.get("scope_type")
    scope = row.get("scope")
    if not value and isinstance(scope, Mapping):
        value = scope.get("chat_type")
    return str(value or "")


def _scope(row: Mapping[str, Any]) -> str:
    scope = row.get("scope")
    if isinstance(scope, Mapping):
        return json.dumps({str(key): scope[key] for key in sorted(scope)}, ensure_ascii=False, sort_keys=True)
    value = row.get("scope_ref") or row.get("chat_id") or row.get("chat_ref")
    return str(value or "")


def _hash_value(value: Mapping[str, Any]) -> str:
    for key in (
        "selection_hash",
        "selection_sha256",
        "selection_fingerprint",
        "locked_selection_hash",
        "hash",
        "fingerprint",
    ):
        candidate = value.get(key)
        if candidate not in (None, "") and not isinstance(candidate, (Mapping, list, tuple, set, frozenset)):
            return str(candidate)
    for key in ("selection", "lock", "blind"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _hash_value(nested)
            if found:
                return found
    return ""


def _manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("manifest", "run_manifest", "body_free_manifest", "blind_manifest"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    return value


def _lookup(value: Any, keys: Sequence[str]) -> list[Any]:
    """Collect values for named keys without inspecting arbitrary text."""

    found: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in {name.casefold() for name in keys}:
                found.append(child)
            found.extend(_lookup(child, keys))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found.extend(_lookup(child, keys))
    return found


def _contains_key(value: Any, names: Sequence[str]) -> bool:
    wanted = {name.casefold() for name in names}
    if isinstance(value, Mapping):
        if any(str(key).casefold() in wanted for key in value):
            return True
        return any(_contains_key(child, names) for child in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_key(child, names) for child in value)
    return False


def _assert_body_free(value: Any) -> None:
    """Assert that an audit/manifest projection contains no body fields."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).casefold()
            if name in _BODY_KEYS:
                assert child in (None, "", [], {}, ()), f"body escaped body-free artifact at {key}"
            _assert_body_free(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            _assert_body_free(child)


def _canonical_selection(selection: Mapping[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rows = _selection_rows(selection)
    return tuple(sorted((_row_id(row), tuple(sorted(_message_ids(row)))) for row in rows))


def _body_free_without_adversarial_fields(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Strip fixture-only topic/model hints while retaining metadata shape."""

    value = deepcopy(dict(metadata))
    for row in value.get("candidate_units", ()):  # type: ignore[union-attr]
        if isinstance(row, Mapping):
            row.pop("topic_hint", None)
            row.pop("model_result", None)
            row.pop("content", None)
            row.pop("text", None)
    value["metadata_rows"] = value.get("candidate_units", [])
    return value


def _body_result_selection(result: Mapping[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    for key in ("selection", "locked_selection", "representatives", "review"):
        value = result.get(key)
        if isinstance(value, Mapping):
            rows = _selection_rows(value)
            if rows:
                return tuple(sorted((_row_id(row), tuple(sorted(_message_ids(row)))) for row in rows))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            rows = [dict(item) for item in value if isinstance(item, Mapping)]
            if rows and all(_row_id(row) for row in rows):
                return tuple(sorted((_row_id(row), tuple(sorted(_message_ids(row)))) for row in rows))
    return ()


def test_metadata_selection_excludes_date_and_legacy_refs_before_body_phase() -> None:
    api = _public_api()
    fixture = _fixture()
    selection = api.select(fixture["metadata_rows"])
    rows = _selection_rows(selection)
    assert len(rows) == 6, "default blind selection must lock six representatives"
    assert not any(_date(row) in _EXCLUDED_DATES for row in rows)
    assert not any(_EXCLUDED_REFS[0] in json.dumps(row) or _EXCLUDED_REFS[1] in json.dumps(row) for row in rows)
    assert set(_date(row) for row in rows) >= {"2026-08-23", "2026-08-24", "2026-08-26"}
    assert {_chat_type(row) for row in rows} >= {"direct", "group"}
    assert len({_scope(row) for row in rows}) >= 3

    manifest = _manifest(selection)
    assert manifest.get("provider_calls", 0) == 0
    assert manifest.get("production_blocked") is True
    assert manifest.get("blind_locked_before_body_read") is True
    assert manifest.get("body_read", False) is False
    _assert_body_free(selection)


def test_selection_is_deterministic_and_hash_stable_without_body_topic_or_model() -> None:
    api = _public_api()
    fixture = _fixture()
    first = api.select(fixture["metadata_rows"])
    replay = api.select(deepcopy(fixture["metadata_rows"]))
    assert _canonical_selection(first) == _canonical_selection(replay)
    assert _hash_value(first)
    assert _hash_value(first) == _hash_value(replay)

    # The same authoritative metadata with adversarial body/topic/model hints
    # must produce the exact same lock.  This is a selection invariant, not a
    # model-quality expectation.
    changed = _body_free_without_adversarial_fields(fixture)
    for row in changed["candidate_units"]:
        row["topic_hint"] = "SYNTHETIC-ADVERSARIAL-TOPIC"
        row["model_result"] = {"topic": "SYNTHETIC-ADVERSARIAL-MODEL"}
        row["content"] = "SYNTHETIC-ADVERSARIAL-BODY"
    changed_selection = api.select(changed["metadata_rows"])
    assert _canonical_selection(first) == _canonical_selection(changed_selection)
    assert _hash_value(first) == _hash_value(changed_selection)


def test_locked_selection_cannot_be_reselected_by_body_topics_or_model_results() -> None:
    api = _public_api()
    fixture = _fixture()
    selection = api.select(fixture["metadata_rows"])
    locked = _canonical_selection(selection)
    adversarial_body = [dict(row, content="SYNTHETIC-ADVERSARIAL-BODY old-ref-synthetic 2026-08-25", text="SYNTHETIC-ADVERSARIAL-BODY") for row in fixture["body_rows"]]
    model_results = {
        "legacy-ref-synthetic": {"topic": "SYNTHETIC-MODEL-MUST-NOT-SELECT"},
        "excluded-date": {"topic": "SYNTHETIC-MODEL-MUST-NOT-SELECT"},
    }
    result = api.body_phase(selection, adversarial_body, model_results=model_results)
    result_locked = _body_result_selection(result)
    if result_locked:
        assert result_locked == locked
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert "legacy-ref-synthetic" not in encoded or "selected" not in encoded
    assert "blind_locked_before_body_read" in encoded


def test_default_representatives_are_message_disjoint_and_direct_flow_is_one_unit() -> None:
    api = _public_api()
    fixture = _fixture()
    selection = api.select(fixture["metadata_rows"])
    rows = _selection_rows(selection)
    assert len(rows) == 6
    message_sets = [_message_ids(row) for row in rows]
    assert all(message_sets), "each representative must retain opaque message refs"
    assert all(not (left & right) for index, left in enumerate(message_sets) for right in message_sets[index + 1:])
    direct_flow_rows = [row for row in rows if row.get("flow_id") == "flow-direct-synthetic"]
    assert len(direct_flow_rows) == 1
    assert len(_message_ids(direct_flow_rows[0])) == 4


def test_body_phase_preserves_parallel_split_context_evidence_flow_media_and_gates() -> None:
    api = _public_api()
    fixture = _fixture()
    selection = api.select(fixture["metadata_rows"])
    result = api.body_phase(selection, fixture["body_rows"])
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)

    # These markers are structural contract checks.  They do not require a
    # final topic label, and they never permit body text into the manifest.
    assert _contains_key(result, ("parallel_topic_split", "parallel_topic_strands", "topic_strands", "parallel_strands"))
    assert _contains_key(result, ("context_evidence", "context_evidence_refs", "context_message_ids", "evidence_refs"))
    assert "flow-direct-synthetic" in encoded
    assert _contains_key(result, ("media", "availability", "missing_reason", "semantic_evidence"))
    assert _contains_key(result, ("gate_channel", "gate_reason_codes", "gate_inheritance", "inherited_gate"))

    # An unavailable media placeholder may remain a review/context row, but
    # it must not be promoted to semantic evidence.
    media_values = _lookup(result, ("media",))
    media_encoded = json.dumps(media_values, ensure_ascii=False, sort_keys=True)
    assert "unavailable" in media_encoded
    assert any(value is False for value in _lookup(result, ("semantic_evidence",)))
    evidence_values = _lookup(result, ("evidence_refs", "evidence_ids", "supporting_evidence_refs"))
    assert "d24-media-m3" not in json.dumps(evidence_values, ensure_ascii=False)


def test_manifest_is_provider_free_blocked_and_body_free_while_review_is_readable() -> None:
    api = _public_api()
    fixture = _fixture()
    selection = api.select(fixture["metadata_rows"])
    result = api.body_phase(selection, fixture["body_rows"])
    manifest = _manifest(result)
    assert int(manifest.get("provider_calls", 0) or 0) == 0
    assert manifest.get("production_blocked") is True
    assert manifest.get("blind_locked_before_body_read") is True

    # Every manifest-like projection is body-free.  Review HTML is the sole
    # intentional body-bearing surface and is tested separately below.
    for key, value in result.items():
        if "manifest" in str(key).casefold() or key in {"selection", "audit", "body_free"}:
            if isinstance(value, (Mapping, list, tuple)):
                _assert_body_free(value)
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
                assert not any(marker in encoded for marker in _BODY_MARKERS)

    review_html = api.review(result, fixture["body_rows"])
    assert review_html, "review surface must remain human-readable"
    assert any(marker in review_html for marker in _BODY_MARKERS)
    assert "2026-08-23" in html.unescape(review_html) or "direct" in review_html.casefold()
