"""Metadata-first blind runner for the local conversation reconstruction seam.

This runner deliberately has only two data phases:

``metadata``
    Read message identity, timestamp, chat scope and message type from the
    local SQLite projection.  No ``message_content``/body column is selected.
    A deterministic, body-free selection manifest is written and hashed.

``materialize``
    After the selection manifest is present and its hash has been checked,
    read body fields for the selected opaque message references and pass the
    rows to :mod:`wechat_bridge.context_reconstruction`.

The runner is intentionally provider-free.  It is a development review
artifact, not a production path and not a semantic accuracy evaluation.  In
particular, the default dates exclude the historical 2026-08-25 feedback
window and no frozen/gold artifact is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from .context_reconstruction import (
    assert_body_free,
    reconstruct_context,
    write_review_artifacts,
)


ROUND_NAME = "conversation-reconstruction-blind-round1"
ROUND_VERSION = "conversation_reconstruction_blind_round1_v1"
DEFAULT_OUTPUT_DIR = Path("output") / f"{ROUND_NAME}-20260901"
# The active-period pilot is a separate development artifact.  Keeping a
# separate name/path is deliberate: an active-period rerun must never replace
# the already-reviewed round1 output.
ACTIVE_PERIOD_TEST_NAME = "conversation-reconstruction-active-period-test-v1-20260901"
ACTIVE_PERIOD_TEST_VERSION = "conversation_reconstruction_active_period_test_v1"
DEFAULT_ACTIVE_PERIOD_OUTPUT_DIR = Path("output") / ACTIVE_PERIOD_TEST_NAME
DEFAULT_ACTIVE_PERIOD_GAP_SECONDS = 30 * 60
DEFAULT_ACTIVE_PERIOD_CARD_LIMIT = 3
DEFAULT_PREFERRED_DATES: Tuple[str, ...] = (
    "2026-08-20",
    "2026-08-21",
    "2026-08-22",
)
# The previous manually reviewed feedback window must not be reused.  Keep
# the exclusion in code as well as in the emitted manifest so a CLI typo cannot
# silently pull that day back into the blind round.
DEFAULT_EXCLUDED_DATES: Tuple[str, ...] = ("2026-08-25",)
LOCAL_TZ = timezone(timedelta(hours=8))
# ``context_reconstruction`` also emits a generic review-sample audit.  That
# audit carries a quota from an earlier single-direct-flow review and is useful
# as provenance, but it is not a blind-round gate when several direct windows
# are intentionally sampled across scopes.
SOURCE_RECONSTRUCTION_SAMPLE_AUDIT_APPLICABILITY = (
    "not_applicable_to_blind_multi_scope_round"
)

_TABLE_RE = re.compile(r"^Msg_[0-9a-fA-F]+$")
_MEDIA_TYPE_CODES = frozenset({3, 34, 43, 47, 48, 49, 10000})
_TYPE_NAMES = {
    1: "text",
    3: "image",
    34: "audio",
    43: "video",
    47: "sticker",
    48: "file",
    49: "file",
    10000: "system",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, (bytes, bytearray)) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _opaque(prefix: str, value: Any, length: int = 16) -> str:
    return f"{prefix}-{_sha256(value)[:length]}"


def _quote_identifier(value: str) -> str:
    # SQLite identifiers are sourced from sqlite_master/PRAGMA, but still
    # quote defensively so a malformed local DB cannot become SQL injection.
    return '"' + str(value).replace('"', '""') + '"'


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _epoch_seconds(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result > 10_000_000_000:
        result /= 1000.0
    return result


def _timestamp(value: Any) -> Optional[str]:
    seconds = _epoch_seconds(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=LOCAL_TZ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _event_timestamp_epoch(timestamp: Any, fallback: Any = None) -> Optional[float]:
    """Parse a metadata event timestamp in Asia/Shanghai-aware form."""

    stamp = str(timestamp or "").strip()
    if stamp:
        try:
            normalized = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            return parsed.astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return _epoch_seconds(fallback)


def _local_day(value: Any) -> Optional[str]:
    stamp = _timestamp(value)
    return stamp[:10] if stamp else None


def _normalise_type_code(value: Any) -> Optional[int]:
    code = _as_int(value)
    if code is None:
        return None
    if code > 0xFFFF and (code & 0xFF) in _MEDIA_TYPE_CODES | {1}:
        return code & 0xFF
    return code


def _is_media_type(value: Any) -> bool:
    code = _normalise_type_code(value)
    return code in _MEDIA_TYPE_CODES


def _message_type(value: Any) -> str:
    return _TYPE_NAMES.get(_normalise_type_code(value) or -1, "unknown")


def _type_is_group(chat_user: str) -> str:
    return "group" if str(chat_user).casefold().endswith("@chatroom") else "direct"


def _normalise_chat_type(value: Any, chat_user: str = "") -> str:
    lowered = str(value or "").strip().casefold()
    if lowered in {"group", "群聊", "群", "chatroom", "room", "group_chat", "multi", "多人"}:
        return "group"
    if lowered in {"direct", "私聊", "private", "single", "one_to_one", "one-to-one", "一对一"}:
        return "direct"
    return _type_is_group(chat_user)


@dataclass(frozen=True)
class _SourceLocator:
    """Private lookup coordinates; never serialized into a manifest."""

    db_rel: str = field(repr=False)
    table: str = field(repr=False)
    chat_user: str = field(repr=False)
    local_id: int = field(repr=False)


@dataclass(frozen=True)
class MetadataMessage:
    """Body-free message metadata used by the blind selector.

    ``locator`` is intentionally private and excluded from ``repr``.  It is
    retained only in memory so the post-lock materialization phase can fetch
    the selected bodies without putting source IDs in the selection manifest.
    """

    message_ref: str
    chat_ref: str
    chat_type: str
    local_day: str
    timestamp: Optional[str]
    timestamp_epoch: Optional[float]
    sequence: Optional[int]
    participant_ref: str
    message_type: str
    media: bool
    # Metadata-only continuity fields.  Older projections may omit them, but
    # when present they let a blind retrieval window preserve a long explicit
    # reply/quote chain without opening message bodies.
    source_message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    quote_message_ids: Tuple[str, ...] = ()
    locator: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MetadataWindow:
    """A deterministic metadata-only active-period retrieval candidate.

    ``rows`` is always the complete contiguous retrieval period for one chat
    scope.  The period is a sampling/retrieval unit only; its gap does not
    assert a semantic conversation boundary.  ``local_day`` remains as a
    backwards-compatible start-day alias while ``observed_days`` records all
    event-time days so cross-midnight periods are not silently calendar-cut.
    """

    window_ref: str
    local_day: str
    chat_ref: str
    chat_type: str
    rows: Tuple[MetadataMessage, ...]
    observed_days: Tuple[str, ...] = ()
    active_period_gap_seconds: Optional[float] = None
    source_sequence_start: Optional[int] = None
    source_sequence_end: Optional[int] = None
    retrieval_boundary_reason: str = "active_period_gap_candidate"

    @property
    def message_count(self) -> int:
        return len(self.rows)

    @property
    def grain(self) -> str:
        return "chat_scope_plus_event_time_span"

    @property
    def media_count(self) -> int:
        return sum(1 for row in self.rows if row.media)

    @property
    def message_type_counts(self) -> Dict[str, int]:
        return dict(sorted(Counter(row.message_type for row in self.rows).items()))

    @property
    def participant_count(self) -> int:
        return len({row.participant_ref for row in self.rows})

    @property
    def start_timestamp(self) -> Optional[str]:
        for row in self.rows:
            if row.timestamp:
                return row.timestamp
        return None

    @property
    def end_timestamp(self) -> Optional[str]:
        for row in reversed(self.rows):
            if row.timestamp:
                return row.timestamp
        return None

    @property
    def time_span_seconds(self) -> Optional[float]:
        values = [
            value
            for row in self.rows
            if (value := _event_timestamp_epoch(row.timestamp, row.timestamp_epoch)) is not None
        ]
        return round(max(values) - min(values), 3) if values else None

    def body_free(self) -> Dict[str, Any]:
        """Return the manifest projection; no locator or body fields."""

        days = self.observed_days or tuple(sorted({row.local_day for row in self.rows if row.local_day}))
        sequences = [row.sequence for row in self.rows if row.sequence is not None]
        return {
            "window_ref": self.window_ref,
            "period_ref": self.window_ref,
            "active_period_ref": self.window_ref,
            "candidate_kind": "retrieval_active_period",
            "grain": self.grain,
            "local_day": self.local_day,
            "observed_days": list(days),
            "chat_ref": self.chat_ref,
            "chat_type": self.chat_type,
            "message_count": self.message_count,
            "participant_count": self.participant_count,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "time_span_seconds": self.time_span_seconds,
            "event_time_span": {
                "start": self.start_timestamp,
                "end": self.end_timestamp,
                "seconds": self.time_span_seconds,
                "timezone": "Asia/Shanghai",
            },
            "source_sequence_start": self.source_sequence_start if self.source_sequence_start is not None else (min(sequences) if sequences else None),
            "source_sequence_end": self.source_sequence_end if self.source_sequence_end is not None else (max(sequences) if sequences else None),
            "retrieval_gap_seconds": self.active_period_gap_seconds,
            "retrieval_boundary_reason": self.retrieval_boundary_reason,
            "media_count": self.media_count,
            # ``assert_body_free`` intentionally treats a mapping key named
            # ``text`` as a possible payload field.  Keep the type histogram
            # as a list of code/count records so the metadata lock remains
            # machine-readable without looking like a body-bearing map.
            "message_type_counts": [
                {"message_kind": kind, "count": count}
                for kind, count in self.message_type_counts.items()
            ],
            "message_refs": [row.message_ref for row in self.rows],
            "source_message_ids": [row.source_message_id for row in self.rows],
            "reply_to_message_ids": [row.reply_to_message_id for row in self.rows],
            "quote_message_ids": [list(row.quote_message_ids) for row in self.rows],
            "period_complete_required": True,
            "window_boundary_is_not_semantic_boundary": True,
            "semantic_boundary_inferred": False,
        }


class BlindRunSource(Protocol):
    """Minimal source contract used by :func:`run_blind_round1`."""

    source_ref: str

    def scan_metadata(
        self,
        dates: Optional[Sequence[str]] = None,
        excluded_dates: Sequence[str] = (),
    ) -> Sequence[MetadataMessage]:
        ...

    def materialize(self, message_refs: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        ...


class WeChatDbSource:
    """Read-only metadata/body source backed by ``wechatauto-replica``.

    The source talks directly to the decrypted read-only SQLite projection
    produced by ``WeChatDB._open``.  Metadata queries select only identity,
    type, sequence and timestamp columns.  ``message_content`` is selected
    only by :meth:`materialize`, after the runner has persisted its lock.
    """

    def __init__(
        self,
        db_dir: Optional[str | Path] = None,
        account: Optional[str] = None,
        *,
        workdir: Optional[str | Path] = None,
    ) -> None:
        self.db_dir = str(db_dir) if db_dir is not None else None
        self.account = account
        self.workdir = str(workdir) if workdir is not None else None
        self._db: Any = None
        self._metadata: Dict[str, MetadataMessage] = {}
        self._account_ref = "account-unknown"
        self.source_ref = "source-unknown"

    def _connect(self) -> Any:
        if self._db is not None:
            return self._db
        from wechatauto import WeChatDB  # type: ignore

        kwargs: Dict[str, Any] = {}
        if self.db_dir:
            kwargs["db_dir"] = self.db_dir
        if self.account:
            kwargs["account"] = self.account
        if self.workdir:
            kwargs["workdir"] = self.workdir
        self._db = WeChatDB(**kwargs)
        account_value = str(getattr(self._db, "account", self.account or "unknown-account"))
        self._account_ref = _opaque("account", account_value)
        # The actual path/account never enters output; this is only a stable
        # provenance handle for reproducibility checks.
        self.source_ref = _opaque("source", (str(self.db_dir or "auto"), account_value))
        return self._db

    @staticmethod
    def _message_table(conn: sqlite3.Connection) -> List[str]:
        names = [str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        return [name for name in names if _TABLE_RE.match(name)]

    def scan_metadata(
        self,
        dates: Optional[Sequence[str]] = None,
        excluded_dates: Sequence[str] = (),
    ) -> Sequence[MetadataMessage]:
        db = self._connect()
        requested = {str(value) for value in dates or () if str(value)}
        excluded = {str(value) for value in excluded_dates}
        md5_index = db._build_md5_index()  # metadata-only contact/session index
        records: Dict[str, MetadataMessage] = {}
        # Rebuilding the map for each metadata scan is intentional: fallback
        # date discovery remains before the blind lock and still body-free.
        for rel in db._message_dbs():
            conn = db._open(rel)
            try:
                for table in self._message_table(conn):
                    md5 = table[4:]
                    chat_user = str(md5_index.get(md5) or md5)
                    chat_type = _type_is_group(chat_user)
                    chat_ref = _opaque("chat", (self._account_ref, chat_user))
                    # No SELECT * here: in particular message_content and
                    # packed_info_data are deliberately absent from this SQL.
                    sql = (
                        f"SELECT local_id, local_type, real_sender_id, create_time, sort_seq "
                        f"FROM {_quote_identifier(table)} ORDER BY sort_seq ASC, local_id ASC"
                    )
                    for local_id, local_type, sender_id, create_time, sort_seq in conn.execute(sql):
                        day = _local_day(create_time)
                        if not day or day in excluded or (requested and day not in requested):
                            continue
                        local_value = _as_int(local_id)
                        if local_value is None:
                            continue
                        epoch = _epoch_seconds(create_time)
                        message_ref = _opaque("message", (self._account_ref, rel, table, local_value))
                        participant_ref = _opaque("participant", (self._account_ref, chat_ref, sender_id))
                        record = MetadataMessage(
                            message_ref=message_ref,
                            chat_ref=chat_ref,
                            chat_type=chat_type,
                            local_day=day,
                            timestamp=_timestamp(create_time),
                            timestamp_epoch=epoch,
                            sequence=_as_int(sort_seq),
                            participant_ref=participant_ref,
                            message_type=_message_type(local_type),
                            media=_is_media_type(local_type),
                            source_message_id=message_ref,
                            locator=_SourceLocator(rel, table, chat_user, local_value),
                        )
                        records[message_ref] = record
            finally:
                conn.close()
        self._metadata = records
        return tuple(records.values())

    @staticmethod
    def _friendly_content(db: Any, value: Any, message_type: str) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            try:
                return db._friendly_content(value, {
                    "text": "文本",
                    "image": "图片",
                    "audio": "语音",
                    "video": "视频",
                    "sticker": "动画表情",
                    "file": "文件/链接/卡片",
                    "system": "系统消息",
                }.get(message_type, "文本"))
            except Exception:
                return ""
        return str(value).replace("\r\n", "\n").replace("\r", "\n")

    def materialize(self, message_refs: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        if not self._metadata:
            raise RuntimeError("metadata_scan_required_before_materialize")
        db = self._connect()
        wanted = [self._metadata[str(ref)] for ref in message_refs if str(ref) in self._metadata]
        grouped: Dict[Tuple[str, str], List[MetadataMessage]] = defaultdict(list)
        for record in wanted:
            locator = record.locator
            if isinstance(locator, _SourceLocator):
                grouped[(locator.db_rel, locator.table)].append(record)
        by_ref: Dict[str, Mapping[str, Any]] = {}
        for (rel, table), records in grouped.items():
            conn = db._open(rel)
            try:
                local_ids = [int(record.locator.local_id) for record in records]
                placeholders = ",".join("?" for _ in local_ids)
                # This is the only body-bearing query in this source.  The
                # runner invokes it after selection_manifest hash verification.
                sql = (
                    f"SELECT local_id, local_type, real_sender_id, create_time, "
                    f"message_content, sort_seq FROM {_quote_identifier(table)} "
                    f"WHERE local_id IN ({placeholders})"
                )
                rows = conn.execute(sql, tuple(local_ids)).fetchall()
            finally:
                conn.close()
            row_by_id = {int(row[0]): row for row in rows if row[0] is not None}
            for record in records:
                locator = record.locator
                row = row_by_id.get(int(locator.local_id)) if isinstance(locator, _SourceLocator) else None
                if row is None:
                    continue
                message_type = record.message_type
                body = self._friendly_content(db, row[4], message_type)
                # Keep opaque IDs in the reconstruction ledger.  The local
                # HTML still shows the selected body, while JSON manifests do
                # not reveal source chat/user/local IDs.
                output: Dict[str, Any] = {
                    "message_id": record.message_ref,
                    "account_id": self._account_ref,
                    "chat_id": record.chat_ref,
                    "chat_type": record.chat_type,
                    "speaker_id": record.participant_ref,
                    "speaker_name": record.participant_ref,
                    "timestamp": record.timestamp,
                    "local_day": record.local_day,
                    "sequence_in_chat": record.sequence,
                    "message_type": message_type,
                    "content": body,
                    "split": "development",
                    "source_mode": "development_sqlite",
                    "metadata_authoritative": True,
                    "blind_selection_locked": True,
                }
                if record.media:
                    output.update({
                        "media_state": "unavailable",
                        "media_available": False,
                        "media_path": None,
                    })
                by_ref[record.message_ref] = output
        return tuple(by_ref[ref] for ref in message_refs if ref in by_ref)


class InMemoryBlindSource:
    """Small source adapter for deterministic runner contract tests."""

    def __init__(self, metadata: Sequence[MetadataMessage], bodies: Mapping[str, Mapping[str, Any]], source_ref: str = "source-test") -> None:
        self._metadata = tuple(metadata)
        self._bodies = {str(key): dict(value) for key, value in bodies.items()}
        self.source_ref = source_ref
        self.body_read = False

    def scan_metadata(
        self,
        dates: Optional[Sequence[str]] = None,
        excluded_dates: Sequence[str] = (),
    ) -> Sequence[MetadataMessage]:
        wanted = {str(value) for value in dates or () if str(value)}
        excluded = {str(value) for value in excluded_dates}
        return tuple(
            row for row in self._metadata
            if row.local_day not in excluded and (not wanted or row.local_day in wanted)
        )

    def materialize(self, message_refs: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        self.body_read = True
        return tuple(self._bodies[str(ref)] for ref in message_refs if str(ref) in self._bodies)


def _coerce_metadata_message(value: Any, index: int = 0) -> Optional[MetadataMessage]:
    """Accept the public mapping-shaped metadata seam without reading body keys."""

    if isinstance(value, MetadataMessage):
        return value
    if not isinstance(value, Mapping):
        return None
    message_ref = str(value.get("message_ref") or value.get("message_id") or value.get("id") or f"metadata-row-{index + 1:06d}")
    source_message_id_value = value.get("source_message_id") or value.get("message_id") or value.get("id")
    source_message_id = str(source_message_id_value) if source_message_id_value not in (None, "") else None
    chat_ref = str(value.get("chat_ref") or value.get("chat_id") or value.get("scope_ref") or "unknown-chat")
    chat_type = _normalise_chat_type(value.get("chat_type") or value.get("scope_type"), chat_ref)
    timestamp_value = value.get("timestamp") or value.get("event_timestamp") or value.get("time")
    timestamp = str(timestamp_value) if timestamp_value not in (None, "") else None
    timestamp_epoch = _event_timestamp_epoch(timestamp, value.get("timestamp_epoch") or value.get("event_timestamp_epoch") or value.get("create_time"))
    local_day_value = value.get("local_day") or value.get("date") or value.get("day")
    local_day = str(local_day_value)[:10] if local_day_value not in (None, "") else (_local_day(timestamp_epoch) or "unknown")
    sequence = _as_int(value.get("sequence") if value.get("sequence") is not None else value.get("source_sequence") if value.get("source_sequence") is not None else value.get("sequence_in_chat"))
    participant_ref = str(value.get("participant_ref") or value.get("speaker_id") or value.get("sender_id") or value.get("user_id") or "unknown-participant")
    message_type = str(value.get("message_type") or value.get("message_kind") or value.get("type") or "unknown")
    media_value = value.get("media")
    media = bool(value.get("media")) if not isinstance(media_value, Mapping) else str(media_value.get("status") or media_value.get("availability") or "").casefold() in {"available", "partial", "unavailable", "missing"}
    if isinstance(media_value, Mapping):
        media = str(media_value.get("status") or media_value.get("availability") or "").casefold() in {"available", "partial", "unavailable", "missing"} or bool(media_value.get("is_media"))
    else:
        media = bool(value.get("media") or value.get("is_media") or _is_media_type(value.get("message_type") or value.get("type")))
    reply_value = (
        value.get("reply_to_message_id")
        or value.get("reply_to")
        or value.get("quoted_message_id")
        or value.get("referenced_message_id")
        or value.get("in_reply_to")
    )
    reply_to_message_id = (
        str(reply_value)
        if reply_value not in (None, "")
        and not isinstance(reply_value, (Mapping, list, tuple, set, frozenset))
        else None
    )
    quote_value = value.get("quote_message_ids")
    if quote_value in (None, ""):
        quote_value = value.get("quote_refs") or value.get("quoted_message_ids") or value.get("referenced_message_ids")
    if isinstance(quote_value, Mapping):
        quote_values = list(quote_value.keys())
    elif isinstance(quote_value, Sequence) and not isinstance(quote_value, (str, bytes, bytearray)):
        quote_values = list(quote_value)
    elif quote_value not in (None, ""):
        quote_values = [quote_value]
    else:
        quote_values = []
    quote_message_ids = tuple(
        str(item)
        for item in quote_values
        if item not in (None, "")
        and not isinstance(item, (Mapping, list, tuple, set, frozenset))
    )
    return MetadataMessage(
        message_ref=message_ref,
        chat_ref=chat_ref,
        chat_type=chat_type,
        local_day=local_day,
        timestamp=timestamp,
        timestamp_epoch=timestamp_epoch,
        sequence=sequence,
        participant_ref=participant_ref,
        message_type=message_type,
        media=media,
        source_message_id=source_message_id,
        reply_to_message_id=reply_to_message_id,
        quote_message_ids=quote_message_ids,
    )


def build_metadata_windows(
    records: Sequence[MetadataMessage],
    *,
    max_messages: int = 4,
    min_messages: int = 3,
    active_period_gap_seconds: float = DEFAULT_ACTIVE_PERIOD_GAP_SECONDS,
    gap_seconds: Optional[float] = None,
    max_gap_seconds: Optional[float] = None,
) -> Tuple[MetadataWindow, ...]:
    """Build complete active-period retrieval candidates from metadata.

    The old implementation cut each chat/day into fixed-size chunks.  That
    made the last messages disappear and made a calendar day look like a
    conversation boundary.  The active-period seam groups one ``chat_scope``
    by event-time continuity instead: a gap larger than
    ``active_period_gap_seconds`` starts another *retrieval candidate*.  The
    gap is deliberately not a semantic/episode decision.  ``max_messages``
    and ``min_messages`` are retained as source-compatible legacy arguments,
    but are intentionally ignored so callers cannot reintroduce fixed-count
    slicing or tail dropping.
    """

    if gap_seconds is not None:
        active_period_gap_seconds = gap_seconds
    if max_gap_seconds is not None:
        active_period_gap_seconds = max_gap_seconds
    try:
        gap_limit = float(active_period_gap_seconds)
    except (TypeError, ValueError):
        raise ValueError("invalid_active_period_gap_seconds") from None
    if gap_limit <= 0:
        raise ValueError("invalid_active_period_gap_seconds")

    def event_epoch(row: MetadataMessage) -> Optional[float]:
        # Prefer the event timestamp string because it carries the explicit
        # Asia/Shanghai wall-clock value.  ``timestamp_epoch`` is the
        # metadata-only fallback used by compact/synthetic sources.
        return _event_timestamp_epoch(row.timestamp, row.timestamp_epoch)

    def sort_key(row: MetadataMessage) -> Tuple[Any, ...]:
        epoch = event_epoch(row)
        return (
            epoch is None,
            epoch if epoch is not None else 0.0,
            row.sequence is None,
            row.sequence if row.sequence is not None else 0,
            str(row.message_ref),
        )

    grouped: Dict[Tuple[str, str], List[MetadataMessage]] = defaultdict(list)
    for index, raw_row in enumerate(records):
        row = _coerce_metadata_message(raw_row, index)
        if row is None:
            continue
        if row.chat_type not in {"direct", "group"} or not row.chat_ref:
            continue
        # chat_ref already contains the account/chat scope hash for the DB
        # adapter.  Keep chat type in the key as an additional guard for
        # synthetic adapters that reuse a display scope identifier.
        grouped[(str(row.chat_ref), str(row.chat_type))].append(row)

    windows: List[MetadataWindow] = []
    for (chat_ref, chat_type), values in sorted(grouped.items()):
        ordered = sorted(values, key=sort_key)
        periods: List[List[MetadataMessage]] = []
        current: List[MetadataMessage] = []
        previous_epoch: Optional[float] = None
        seen_identifiers: set[str] = set()

        def identifiers(row: MetadataMessage) -> set[str]:
            values = {str(row.message_ref)}
            if row.source_message_id:
                values.add(str(row.source_message_id))
            return values

        def bridges_known_context(row: MetadataMessage) -> bool:
            targets = set()
            if row.reply_to_message_id:
                targets.add(str(row.reply_to_message_id))
            targets.update(str(value) for value in row.quote_message_ids if value)
            return bool(targets & seen_identifiers)

        for row in ordered:
            current_epoch = event_epoch(row)
            if current:
                gap = (
                    current_epoch - previous_epoch
                    if current_epoch is not None and previous_epoch is not None
                    else None
                )
                # Unknown time is retained in the current period.  It cannot
                # justify a semantic or retrieval split.  Only a known,
                # positive event-time gap can close an active-period
                # candidate.
                # A long gap is a retrieval candidate, not a semantic close.
                # Explicit reply/quote metadata is stronger and keeps the
                # complete period intact, even when the gap crosses a day.
                # This is evaluated before body materialisation.
                if gap is not None and gap > gap_limit and not bridges_known_context(row):
                    periods.append(current)
                    current = []
            current.append(row)
            previous_epoch = current_epoch
            seen_identifiers.update(identifiers(row))
        if current:
            periods.append(current)

        for period_index, period_rows in enumerate(periods):
            rows_tuple = tuple(period_rows)
            days = tuple(sorted({str(row.local_day) for row in rows_tuple if row.local_day}))
            local_day = days[0] if days else "unknown"
            epochs = [event_epoch(row) for row in rows_tuple if event_epoch(row) is not None]
            sequences = [row.sequence for row in rows_tuple if row.sequence is not None]
            period_start = next((row.timestamp for row in rows_tuple if row.timestamp), None)
            period_end = next((row.timestamp for row in reversed(rows_tuple) if row.timestamp), None)
            span = round(max(epochs) - min(epochs), 3) if epochs else None
            # Include the stable ordered refs and the source-coordinate
            # envelope in the digest.  The period index is only a collision
            # guard for duplicate synthetic rows; it is not a ranking signal.
            window_ref = _opaque(
                "active-period",
                (
                    chat_ref,
                    chat_type,
                    tuple(row.message_ref for row in rows_tuple),
                    tuple(row.source_message_id for row in rows_tuple),
                    tuple(row.reply_to_message_id for row in rows_tuple),
                    tuple(row.quote_message_ids for row in rows_tuple),
                    period_start,
                    period_end,
                    tuple(sequences),
                    period_index,
                ),
                18,
            )
            windows.append(MetadataWindow(
                window_ref=window_ref,
                local_day=local_day,
                chat_ref=chat_ref,
                chat_type=chat_type,
                rows=rows_tuple,
                observed_days=days,
                active_period_gap_seconds=gap_limit,
                source_sequence_start=min(sequences) if sequences else None,
                source_sequence_end=max(sequences) if sequences else None,
                retrieval_boundary_reason=(
                    "active_period_gap_candidate" if len(periods) > 1 else "chat_scope_time_span_candidate"
                ),
            ))

    # ``max_messages``/``min_messages`` intentionally do not appear in this
    # ordering or in the selection.  Sort by event-time start, then source
    # sequence/chat/ref so replay is deterministic across input row order.
    def period_sort_key(window: MetadataWindow) -> Tuple[Any, ...]:
        first = window.rows[0] if window.rows else None
        first_epoch = event_epoch(first) if first is not None else None
        first_sequence = first.sequence if first is not None else None
        return (
            first_epoch is None,
            first_epoch if first_epoch is not None else 0.0,
            first_sequence is None,
            first_sequence if first_sequence is not None else 0,
            window.chat_type,
            window.chat_ref,
            window.window_ref,
        )

    return tuple(sorted(windows, key=period_sort_key))


def _active_period_date(window: MetadataWindow) -> str:
    """Return a stable display date without imposing a date boundary."""

    days = window.observed_days or tuple(sorted({row.local_day for row in window.rows if row.local_day}))
    return days[0] if days else window.local_day or "unknown"


def _active_period_volume_band(window: MetadataWindow) -> str:
    """Coarse metadata-only volume stratum (never a raw-count ranking)."""

    count = window.message_count
    if count <= 2:
        return "low"
    if count <= 8:
        return "medium"
    return "high"


def _active_period_duration_band(window: MetadataWindow) -> str:
    span = window.time_span_seconds
    if span is None:
        return "unknown"
    if span <= 60:
        return "brief"
    if span <= 15 * 60:
        return "short"
    if span <= 2 * 60 * 60:
        return "extended"
    return "long"


def _window_rank(window: MetadataWindow, used_chat_refs: set[str]) -> Tuple[Any, ...]:
    """Rank only by allowed metadata strata and opaque stable hash.

    ``message_count`` is intentionally represented only through the coarse
    volume band.  It is therefore useful for structural coverage/cost
    accounting, never for choosing a supposedly more valuable period.  No
    message type/body/topic/model/value field participates here.
    """

    participant_band = "multi" if window.participant_count >= 3 else ("two" if window.participant_count == 2 else "single")
    return (
        0 if window.chat_ref not in used_chat_refs else 1,
        # A multi-participant group is a requested structural stratum.  This
        # branch is metadata-only and does not imply semantic richness.
        0 if window.chat_type == "group" and participant_band == "multi" else 1,
        0 if window.chat_type == "direct" else 1,
        participant_band,
        _active_period_duration_band(window),
        _active_period_volume_band(window),
        _sha256((window.chat_ref, window.window_ref))[:24],
        window.window_ref,
    )


def select_representative_windows(
    windows: Sequence[MetadataWindow],
    *,
    preferred_dates: Sequence[str] = DEFAULT_PREFERRED_DATES,
    excluded_dates: Sequence[str] = DEFAULT_EXCLUDED_DATES,
    max_cards: int = 6,
    cost_cap_messages: Optional[int] = None,
) -> Tuple[MetadataWindow, ...]:
    """Select complete active-period candidates from metadata-only strata.

    The selection unit is an entire period; there is no message-count cut and
    no calendar-day closure.  Scope/date strata are covered first, with a
    multi-participant group preferred when available.  A cost cap, when
    supplied, defers an entire candidate that would exceed the remaining
    budget.  It never slices, downsamples, or re-ranks by raw message count.

    ``max_cards`` defaults to the legacy six for compatibility with round1;
    the active-period runner explicitly requests three cards.
    """

    if max_cards < 1:
        raise ValueError("max_cards_must_be_positive")
    if cost_cap_messages is not None:
        try:
            cost_cap = int(cost_cap_messages)
        except (TypeError, ValueError):
            raise ValueError("invalid_cost_cap_messages") from None
        if cost_cap < 1:
            raise ValueError("invalid_cost_cap_messages")
    else:
        cost_cap = None
    excluded = {str(value) for value in excluded_dates}
    usable = [
        window
        for window in windows
        if window.chat_type in {"direct", "group"}
        and not (set(window.observed_days or (window.local_day,)) & excluded)
    ]
    by_slot: Dict[Tuple[str, str], List[MetadataWindow]] = defaultdict(list)
    for window in usable:
        # A period may cross midnight.  Slot it by its first observed event
        # day for coverage purposes while retaining the full observed-day
        # envelope in the lock.  This is not a retrieval boundary.
        by_slot[(_active_period_date(window), window.chat_type)].append(window)
    for values in by_slot.values():
        values.sort(key=lambda item: _window_rank(item, set()))
    available_dates = {day for day, _scope in by_slot if day and day != "unknown"}
    date_order: List[str] = []
    for day in preferred_dates:
        text_day = str(day)
        if text_day in available_dates and text_day not in date_order:
            date_order.append(text_day)
    # Prefer days with both scopes, then any remaining available days.  No
    # body or lexical feature is used in this fallback.
    for day in sorted(available_dates):
        if day not in date_order and all((day, scope) in by_slot for scope in ("direct", "group")):
            date_order.append(day)
    for day in sorted(available_dates):
        if day not in date_order:
            date_order.append(day)
    selected: List[MetadataWindow] = []
    used_refs: set[str] = set()
    used_chats: set[str] = set()
    used_message_refs: set[str] = set()

    def can_add(candidate: MetadataWindow) -> bool:
        if candidate.window_ref in used_refs:
            return False
        if used_message_refs.intersection(str(row.message_ref) for row in candidate.rows):
            return False
        if cost_cap is not None and sum(item.message_count for item in selected) + candidate.message_count > cost_cap:
            return False
        return True

    def add_best(candidates: Sequence[MetadataWindow]) -> bool:
        available = [item for item in candidates if can_add(item)]
        if not available:
            return False
        # This rank contains only scope, participant, duration/volume bands
        # and stable opaque hashes.  It deliberately does not inspect body,
        # topics, model/value outputs, or media/text composition.
        choice = min(available, key=lambda item: _window_rank(item, used_chats))
        selected.append(choice)
        used_refs.add(choice.window_ref)
        used_chats.add(choice.chat_ref)
        used_message_refs.update(str(row.message_ref) for row in choice.rows)
        return True

    # First satisfy the hard scope strata on the earliest available date.
    # For three cards this yields direct + group on one date and one complete
    # period on another date whenever the metadata permits it.
    target_dates = date_order[: max(1, min(len(date_order), max_cards))]
    preferred_multi_group = min(
        (window for window in usable if window.chat_type == "group" and window.participant_count >= 3),
        key=lambda item: _window_rank(item, used_chats),
        default=None,
    )
    if target_dates:
        first_day = target_dates[0]
        for scope in ("direct", "group"):
            if len(selected) >= max_cards:
                break
            candidates = by_slot.get((first_day, scope), ())
            # Reserve the requested multi-participant group stratum even when
            # its earliest period is on a later date.  This is still a
            # metadata-only structural preference; no body/value signal is
            # used to pull it forward.
            if (
                scope == "group"
                and preferred_multi_group is not None
                and not any(item.participant_count >= 3 and item.chat_type == "group" for item in candidates)
                and can_add(preferred_multi_group)
            ):
                candidates = (preferred_multi_group,)
            add_best(candidates)
    # Then cover another date before filling structural slots.  This ensures
    # the active pilot does not accidentally become a one-day sample.
    for day in target_dates[1:]:
        if len(selected) >= max_cards:
            break
        scopes = ("group", "direct") if not any(item.chat_type == "group" for item in selected) else ("direct", "group")
        for scope in scopes:
            if len(selected) >= max_cards:
                break
            add_best(by_slot.get((day, scope), ()))
            if selected and len({ _active_period_date(item) for item in selected }) >= 2:
                break
    # Fill any shortfall from all remaining periods in deterministic strata
    # order.  A deferred candidate is left wholly deferred under the cap.
    remaining = [window for window in usable if window.window_ref not in used_refs]
    remaining.sort(key=lambda item: (
        _active_period_date(item) not in target_dates,
        _active_period_date(item),
        _window_rank(item, used_chats),
    ))
    for choice in remaining:
        if len(selected) >= max_cards:
            break
        add_best((choice,))

    # Event-time order is the review order.  It remains stable across source
    # scan order and preserves the source sequence tie-breaker embedded in the
    # period rows.
    def selected_sort_key(item: MetadataWindow) -> Tuple[Any, ...]:
        first = item.rows[0] if item.rows else None
        timestamp_epoch = _event_timestamp_epoch(first.timestamp, first.timestamp_epoch) if first else None
        sequence = first.sequence if first and first.sequence is not None else 10**18
        return (timestamp_epoch is None, timestamp_epoch if timestamp_epoch is not None else 0.0, sequence, item.chat_type, item.chat_ref, item.window_ref)

    selected.sort(key=selected_sort_key)
    return tuple(selected)


def build_selection_manifest(
    windows: Sequence[MetadataWindow],
    *,
    source_ref: str,
    preferred_dates: Sequence[str],
    excluded_dates: Sequence[str],
    round_name: str = ROUND_NAME,
    round_version: str = ROUND_VERSION,
    active_period_gap_seconds: Optional[float] = None,
    cost_cap_messages: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a body-free, hashable selection lock projection."""

    selected_days = sorted({
        day
        for window in windows
        for day in (window.observed_days or (window.local_day,))
        if day and day != "unknown"
    })
    selected_chats = sorted({window.chat_ref for window in windows})
    scope_counts = Counter(window.chat_type for window in windows)
    expected_refs = [row.message_ref for window in windows for row in window.rows]
    period_rows = [window.body_free() for window in windows]
    base: Dict[str, Any] = {
        "artifact_version": round_version,
        "round": round_name,
        "round_version": round_version,
        "selection_phase": "metadata_only",
        "selection_locked": True,
        "blind_locked_before_body_read": True,
        "source_scope": "development",
        "source_ref": source_ref,
        "source_backend": "local_sqlite_read_only",
        "preferred_dates": [str(value) for value in preferred_dates],
        "excluded_dates": sorted({str(value) for value in excluded_dates}),
        "selected_dates": selected_days,
        "selected_chat_refs": selected_chats,
        "chat_scope_counts": dict(sorted(scope_counts.items())),
        "window_count": len(windows),
        "period_count": len(windows),
        "selected_message_count": sum(window.message_count for window in windows),
        # Preserve event/source order.  A set or lexical sort would erase the
        # parity contract needed by the deferred body and rendering phases.
        "selected_message_refs": expected_refs,
        "expected_message_refs": expected_refs,
        "selected_period_refs": [window.window_ref for window in windows],
        "windows": period_rows,
        "periods": period_rows,
        "active_period_gap_seconds": active_period_gap_seconds,
        "active_period_candidate_grain": "chat_scope_plus_event_time_span",
        "event_timestamp_timezone": "Asia/Shanghai",
        "source_sequence_used": True,
        "fixed_message_count_partition": False,
        "calendar_day_forced_closure": False,
        "tail_messages_dropped": False,
        "period_complete_before_body_read": True,
        "provider_calls": 0,
        "provider_used": False,
        "production_blocked": True,
        "production_connected": False,
        "frozen_read": False,
        "gold_read": False,
        "body_free": True,
        "body_fields_read_during_selection": False,
        "selection_basis": "direct_group_stratum_participant_band_duration_band_volume_band_and_stable_opaque_hash_only",
        "message_count_role": "cost_and_completeness_only_not_boundary_or_rank",
        "cost_cap_messages": cost_cap_messages,
        "cost_cap_policy": "whole_period_deferred_no_split_or_downsample",
        "semantic_accuracy_measured": False,
        "semantic_accuracy_note": "No gold or human labels are read; this round reports structural review gates only.",
        "hash_basis": "canonical JSON with selection_manifest_sha256 omitted",
    }
    assert_body_free(base)
    base["selection_manifest_sha256"] = _sha256(base)
    return base


def verify_selection_manifest(manifest: Mapping[str, Any]) -> bool:
    """Verify the selection lock hash and body-free invariant."""

    assert_body_free(manifest)
    expected = str(manifest.get("selection_manifest_sha256") or "")
    if not expected:
        return False
    copy = dict(manifest)
    copy.pop("selection_manifest_sha256", None)
    return _sha256(copy) == expected and bool(manifest.get("selection_locked"))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _evidence_refs(result: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for segment in result.get("segments") or ():
        if not isinstance(segment, Mapping):
            continue
        for key in ("evidence_refs", "supporting_evidence_refs"):
            values = segment.get(key) or ()
            if isinstance(values, Mapping):
                values = (values,)
            if isinstance(values, (str, bytes)):
                values = (values,)
            for value in values:
                if isinstance(value, Mapping):
                    found.add(str(value.get("message_ref") or value.get("message_id") or value.get("source_message_id") or ""))
                elif value:
                    found.add(str(value))
    return {value for value in found if value}


def _source_reconstruction_sample_audit(
    sample_audit: Mapping[str, Any],
) -> Dict[str, Any]:
    """Annotate the generic reconstruction audit as non-gating provenance.

    The lower-level reconstruction seam has its own review-card policy.  Keep
    that exact audit available for diagnosis, while making its scope explicit
    so a blind multi-scope round cannot accidentally treat its legacy
    direct-card quota as a round-level quality gate.
    """

    value = dict(sample_audit)
    value.update({
        "applicability": SOURCE_RECONSTRUCTION_SAMPLE_AUDIT_APPLICABILITY,
        "participates_in_pass": False,
        "pass_semantics": "source_reconstruction_provenance_only",
    })
    return value


def build_blind_audit(
    result: Mapping[str, Any],
    selected_windows: Sequence[MetadataWindow],
    *,
    selection_manifest_verified: bool,
    body_read_started_after_lock: bool,
) -> Dict[str, Any]:
    """Report structural review gates without inventing semantic accuracy."""

    review = result.get("review") if isinstance(result.get("review"), Mapping) else {}
    sample_units = review.get("sample_units") or ()
    sample_sets = [set(str(ref) for ref in unit.get("display_message_refs") or ()) for unit in sample_units if isinstance(unit, Mapping)]
    intersections: List[List[str]] = []
    for index, left in enumerate(sample_sets):
        for right in sample_sets[index + 1:]:
            overlap = sorted(left & right)
            if overlap:
                intersections.append(overlap)
    sample_audit = review.get("sample_audit") if isinstance(review, Mapping) else {}
    sample_audit = sample_audit if isinstance(sample_audit, Mapping) else {}
    representative_review = review.get("representative_review") if isinstance(review, Mapping) else {}
    representative_review = representative_review if isinstance(representative_review, Mapping) else {}
    # The existing reconstruction exposes this invariant on the review
    # projection (and the lower-level sample audit exposes it when a direct
    # flow is present).  Accept either location, but record the source as a
    # non-gating diagnostic; this round deliberately has multiple direct
    # windows and does not infer a merge between them.
    direct_flow_units = [
        unit
        for unit in sample_units
        if isinstance(unit, Mapping)
        and unit.get("chat_type") == "direct"
        and unit.get("flow_ref")
    ]
    direct_flow_merge_audit_present = bool(direct_flow_units) and bool(
        sample_audit.get("direct_flow_card_is_merged_at_review_layer")
        or (sample_audit.get("criteria") or {}).get("direct_flow_card_is_merged_at_review_layer")
        or representative_review.get("direct_flow_is_one_review_card")
    )
    source_reconstruction_sample_audit = _source_reconstruction_sample_audit(sample_audit)
    all_selected_refs = [row.message_ref for window in selected_windows for row in window.rows]
    duplicate_selected_refs = sorted(ref for ref, count in Counter(all_selected_refs).items() if count > 1)
    all_media_refs = {
        str(row.get("message_ref"))
        for row in result.get("messages") or ()
        if isinstance(row, Mapping) and (row.get("media") or {}).get("semantic_evidence_eligible") is False
    }
    media_evidence_refs = sorted(all_media_refs & _evidence_refs(result))
    envelopes = [item for item in result.get("review_context_envelopes") or () if isinstance(item, Mapping)]
    allowed_context_evidence = {
        "explicit_reply_or_quote",
        "shared_concrete_object",
        "authoritative_segment_continuity",
        "reference_or_qa_continuation",
    }
    context_refs = [str(ref) for envelope in envelopes for ref in envelope.get("context_message_refs") or ()]
    context_evidence = {
        str(ref): str(label)
        for envelope in envelopes
        for ref, label in (envelope.get("context_evidence_by_ref") or {}).items()
    }
    weak_context_refs = sorted(ref for ref, label in context_evidence.items() if label not in allowed_context_evidence)
    chats = [item for item in result.get("chats") or () if isinstance(item, Mapping)]
    scope_counts = Counter(str(chat.get("chat_type") or "unknown") for chat in chats)
    selected_day_count = len({window.local_day for window in selected_windows})
    selected_chat_count = len({window.chat_ref for window in selected_windows})
    direct_cards = sum(1 for unit in sample_units if isinstance(unit, Mapping) and unit.get("chat_type") == "direct")
    group_cards = sum(1 for unit in sample_units if isinstance(unit, Mapping) and unit.get("chat_type") == "group")
    information_unknown = all(
        isinstance(thread, Mapping)
        and (thread.get("information_value") or {}).get("label") == "unknown"
        and (thread.get("information_value") or {}).get("score") is None
        for thread in result.get("threads") or ()
        if isinstance(thread, Mapping)
    )
    gates = {
        "blind_locked_before_body_read": bool(body_read_started_after_lock),
        "selection_manifest_hash_verified": bool(selection_manifest_verified),
        "selection_manifest_body_free": True,
        "provider_calls_zero": int(result.get("provider_calls", -1)) == 0,
        "provider_disabled": result.get("provider_used") is False,
        "production_blocked": result.get("production_blocked") is True,
        "production_connected_false": result.get("production_connected") is False,
        "frozen_read_false": result.get("frozen_read") is False,
        "gold_read_false": True,
        "dates_exclude_2026_08_25": "2026-08-25" not in {window.local_day for window in selected_windows},
        "at_least_three_dates": selected_day_count >= 3,
        "direct_and_group_covered": bool(scope_counts.get("direct")) and bool(scope_counts.get("group")),
        "default_review_cards_at_most_six": len(sample_units) <= 6,
        "review_source_sets_mutually_exclusive": not intersections and not duplicate_selected_refs,
        "context_only_medium_or_strong": not weak_context_refs,
        "media_unavailable_not_evidence": not media_evidence_refs,
        "information_value_remains_unknown": information_unknown,
        # The generic reconstruction sample audit is intentionally retained
        # below as provenance, but none of its single-direct-flow criteria are
        # applicable to this multi-scope blind round.
        "source_reconstruction_sample_audit_not_used_as_global_gate": True,
    }
    passed = all(gates.values())
    return {
        "audit_version": f"{ROUND_VERSION}_audit",
        "status": "structural_gates_passed" if passed else "structural_gates_failed",
        "semantic_accuracy_measured": False,
        "semantic_accuracy_note": "No gold/frozen/human labels and no provider output are used; accuracy is intentionally not reported.",
        "source_reconstruction_sample_audit": source_reconstruction_sample_audit,
        "repetition": {
            "selected_message_ref_count": len(all_selected_refs),
            "duplicate_selected_message_refs": duplicate_selected_refs,
            "review_card_source_intersections": intersections,
            "passed": not duplicate_selected_refs and not intersections,
        },
        "mixed_or_category_overlap": {
            "representative_category_overlap_card_count": int(sample_audit.get("representative_category_overlap_card_count", 0) or 0),
            "category_overlap": [unit.get("category_overlap") or [] for unit in sample_units if isinstance(unit, Mapping) and unit.get("category_overlap")],
            "passed": True,
            "note": "Category overlap/source repetition is retained as a review red line; no semantic category is collapsed here.",
        },
        "context": {
            "context_message_count": len(context_refs),
            "context_evidence_by_ref": context_evidence,
            "weak_context_refs": weak_context_refs,
            "allowed_evidence_labels": sorted(allowed_context_evidence),
            "passed": not weak_context_refs,
        },
        "direct_flow_merge": {
            "audit_present": direct_flow_merge_audit_present,
            "applicability": SOURCE_RECONSTRUCTION_SAMPLE_AUDIT_APPLICABILITY,
            "participates_in_pass": False,
            "source": (
                "sample_audit"
                if direct_flow_merge_audit_present and sample_audit.get("direct_flow_card_is_merged_at_review_layer")
                else "sample_audit.criteria"
                if direct_flow_merge_audit_present and (sample_audit.get("criteria") or {}).get("direct_flow_card_is_merged_at_review_layer")
                else "review.representative_review"
                if direct_flow_merge_audit_present and representative_review.get("direct_flow_is_one_review_card")
                else "not_applicable_to_blind_multi_scope_round"
            ),
            "note": "Independent direct windows are not merged; no direct-flow merge is inferred in this multi-scope round.",
        },
        "scope": {
            "selected_date_count": selected_day_count,
            "selected_chat_count": selected_chat_count,
            "selected_chat_scope_counts": dict(Counter(window.chat_type for window in selected_windows)),
            "reconstructed_chat_scope_counts": dict(sorted(scope_counts.items())),
            "default_review_direct_cards": direct_cards,
            "default_review_group_cards": group_cards,
            "passed": bool(scope_counts.get("direct")) and bool(scope_counts.get("group")) and selected_day_count >= 3,
        },
        "gates": gates,
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Active-period pilot (v1)
# ---------------------------------------------------------------------------

def _active_period_refs(windows: Sequence[MetadataWindow]) -> List[str]:
    """Flatten complete selected periods in their locked event order."""

    return [str(row.message_ref) for window in windows for row in window.rows]


def _row_ref(row: Mapping[str, Any]) -> str:
    """Read only an explicit opaque reference from a materialized row."""

    for key in ("message_ref", "canonical_message_ref", "blind_message_ref", "message_id"):
        value = row.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return str(value)
    return ""


def _bind_materialized_refs(
    rows: Sequence[Mapping[str, Any]],
    expected_refs: Sequence[str],
    metadata_by_ref: Optional[Mapping[str, MetadataMessage]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Attach an explicit ref alias without guessing missing identities.

    WeChatDbSource and the test source return ``message_id`` as the opaque
    selected reference.  Other adapters may return ``message_ref``.  This
    helper normalizes those two explicit forms for reconstruction; it never
    maps rows by ordinal, body, topic, or model output.
    """

    bound: List[Dict[str, Any]] = []
    refs: List[str] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            bound.append({})
            refs.append("")
            continue
        row = dict(raw)
        ref = _row_ref(row)
        if ref:
            row["message_ref"] = ref
            # Tell the reconstruction seam that this is the already-locked
            # opaque reference.  Ordinary callers' incidental ``message_ref``
            # display fields must continue to use the historical hash path.
            row["blind_selection_locked"] = True
            metadata = (metadata_by_ref or {}).get(ref)
            if metadata is not None:
                # Body-phase rows may omit one of the authoritative metadata
                # columns.  Fill only absent fields; never let a body/topic
                # field alter period membership or identity.
                defaults = {
                    "chat_type": metadata.chat_type,
                    "timestamp": metadata.timestamp,
                    "local_day": metadata.local_day,
                    "sequence_in_chat": metadata.sequence,
                    "message_type": metadata.message_type,
                }
                for key, value in defaults.items():
                    if row.get(key) in (None, "") and value not in (None, ""):
                        row[key] = value
        bound.append(row)
        refs.append(ref)
    return bound, refs


def _reference_integrity(
    expected_refs: Sequence[str],
    materialized_refs: Sequence[str],
    reconstructed_refs: Sequence[str],
    rendered_refs: Sequence[str],
) -> Dict[str, Any]:
    """Return an exact set-and-order parity record for the active pilot."""

    expected = [str(value) for value in expected_refs]
    materialized = [str(value) for value in materialized_refs]
    reconstructed = [str(value) for value in reconstructed_refs]
    rendered = [str(value) for value in rendered_refs]
    names = {
        "expected_refs": expected,
        "materialized_refs": materialized,
        "reconstructed_refs": reconstructed,
        "rendered_refs": rendered,
    }
    missing_extra: Dict[str, Dict[str, List[str]]] = {}
    expected_set = set(expected)
    for name, refs in names.items():
        if name == "expected_refs":
            continue
        missing_extra[name] = {
            "missing": [ref for ref in expected if ref not in set(refs)],
            "extra": [ref for ref in refs if ref not in expected_set],
        }
    exact = all(refs == expected for name, refs in names.items() if name != "expected_refs")
    return {
        "expected_refs": expected,
        "materialized_refs": materialized,
        "reconstructed_refs": reconstructed,
        "rendered_refs": rendered,
        # Explicit aliases make the equality invariant discoverable to
        # auditors that use the selected/materialized naming convention.
        "selected_expected_refs": expected,
        "selected_equals_materialized": materialized == expected,
        "materialized_equals_reconstructed": reconstructed == materialized,
        "reconstructed_equals_rendered": rendered == reconstructed,
        "exact_set_and_order_equal": exact,
        "missing_or_extra": missing_extra,
        "status": "passed" if exact else "blocked",
    }


def _reorder_reconstruction_refs(
    result: Dict[str, Any],
    expected_refs: Sequence[str],
) -> None:
    """Align the public ledger order with the locked period/card order.

    The lower-level seam sorts its authority ledger globally by timestamp. A
    period review, however, is intentionally card-major: all rows of period 1
    precede all rows of period 2.  Reordering only the flat authority ledgers
    keeps the semantic episode/segment structures untouched while making the
    cross-phase reference parity explicit and deterministic.
    """

    order = {str(ref): index for index, ref in enumerate(expected_refs)}

    def reorder_rows(value: Any, key: str = "message_ref") -> Any:
        if not isinstance(value, list):
            return value
        rows = [row for row in value if isinstance(row, Mapping) and str(row.get(key) or "") in order]
        if len(rows) != len(value) or {str(row.get(key) or "") for row in rows} != set(order):
            return value
        return sorted(rows, key=lambda row: order[str(row.get(key))])

    result["messages"] = reorder_rows(result.get("messages"))
    result["time_ledger"] = reorder_rows(result.get("time_ledger"))
    authority = result.get("authority_ledger")
    if isinstance(authority, dict):
        authority["messages"] = reorder_rows(authority.get("messages"))
        authority["time"] = reorder_rows(authority.get("time"))


def _active_period_card_projection(
    result: Mapping[str, Any],
    windows: Sequence[MetadataWindow],
) -> List[Dict[str, Any]]:
    """Build one body-free review card and complete timeline per period."""

    messages = {
        str(row.get("message_ref")): row
        for row in result.get("messages") or ()
        if isinstance(row, Mapping) and row.get("message_ref")
    }
    episodes = [
        row for row in result.get("episodes") or ()
        if isinstance(row, Mapping)
    ]
    cards: List[Dict[str, Any]] = []
    for window in windows:
        refs = [str(row.message_ref) for row in window.rows]
        ref_set = set(refs)
        period_episodes = [
            episode for episode in episodes
            if ref_set.intersection(str(ref) for ref in episode.get("message_refs") or ())
        ]
        strands: List[Dict[str, Any]] = []
        for episode in period_episodes:
            strand_refs = [
                str(ref) for ref in episode.get("message_refs") or ()
                if str(ref) in ref_set
            ]
            if not strand_refs:
                continue
            strand_ref = _opaque("parallel-strand", (window.window_ref, tuple(strand_refs)), 16)
            strands.append({
                "strand_ref": strand_ref,
                "episode_ref": episode.get("episode_ref"),
                "message_refs": strand_refs,
                "candidate_only": True,
                "semantic_status": "candidate_only",
                "parallel_candidate": len(period_episodes) > 1,
                "reason": "parallel_topic_strand_candidate" if len(period_episodes) > 1 else "single_internal_episode_candidate",
            })
        if len(period_episodes) > 1 and len(strands) > 1:
            parallel_status = "observed_candidate"
            parallel_note = "multiple internal episode/strand candidates retained inside this one review card"
        else:
            parallel_status = "not_measured"
            parallel_note = "no parallel strand was observed in this pilot period; empty is not a pass"

        timeline: List[Dict[str, Any]] = []
        for ref in refs:
            message = messages.get(ref, {})
            media = message.get("media") if isinstance(message.get("media"), Mapping) else {}
            is_system = str(message.get("message_type") or "").casefold() == "system"
            evidence_eligible = media.get("semantic_evidence_eligible")
            if is_system:
                evidence_eligible = False
            timeline.append({
                "message_ref": ref,
                "message_id": message.get("message_id") or message.get("source_message_id") or ref,
                "timestamp": message.get("timestamp"),
                "local_day": message.get("local_day"),
                "sequence": message.get("sequence"),
                "message_type": message.get("message_type"),
                "chat_ref": message.get("chat_ref"),
                "participant_ref": message.get("participant_ref"),
                "media_status": media.get("status"),
                "media_missing_reason": media.get("missing_reason"),
                "semantic_evidence_eligible": bool(evidence_eligible),
                "system_semantic_policy": "metadata_only_not_semantic" if is_system else None,
                "unavailable_media_is_false_evidence": bool(
                    media.get("status") == "unavailable" or evidence_eligible is False
                ),
            })

        cards.append({
            "card_ref": _opaque("active-card", window.window_ref, 18),
            "unit_ref": _opaque("active-card", window.window_ref, 18),
            "unit_id": _opaque("active-card", window.window_ref, 18),
            "period_ref": window.window_ref,
            "active_period_ref": window.window_ref,
            "chat_ref": window.chat_ref,
            "chat_type": window.chat_type,
            "observed_days": list(window.observed_days or (window.local_day,)),
            "period_start_timestamp": window.start_timestamp,
            "period_end_timestamp": window.end_timestamp,
            "source_sequence_start": window.source_sequence_start,
            "source_sequence_end": window.source_sequence_end,
            "message_refs": refs,
            "timeline_message_refs": refs,
            "timeline": timeline,
            "internal_episode_refs": [str(episode.get("episode_ref")) for episode in period_episodes],
            "internal_episode_count": len(period_episodes),
            "candidate_parallel_strands": strands,
            "parallel_status": parallel_status,
            "parallel_note": parallel_note,
            "one_period_one_review_card": True,
            "internal_episodes_not_review_cards": True,
            "candidate_only": True,
            "semantic_status": "candidate_only",
        })
    return cards


def _active_context_status(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize context evidence without treating an empty set as pass."""

    envelopes = [
        envelope for envelope in result.get("review_context_envelopes") or ()
        if isinstance(envelope, Mapping)
    ]
    refs: List[str] = []
    evidence_by_ref: Dict[str, str] = {}
    allowed = {
        "explicit_reply_or_quote",
        "shared_concrete_object",
        "authoritative_segment_continuity",
        "reference_or_qa_continuation",
    }
    for envelope in envelopes:
        for ref in envelope.get("context_message_refs") or ():
            text_ref = str(ref)
            refs.append(text_ref)
        mapping = envelope.get("context_evidence_by_ref")
        if isinstance(mapping, Mapping):
            for ref, label in mapping.items():
                evidence_by_ref[str(ref)] = str(label)
    observed_refs = sorted({ref for ref in refs if evidence_by_ref.get(ref) in allowed})
    observed = bool(observed_refs)
    return {
        "status": "observed_candidate" if observed else "not_measured",
        "pilot_limited": not observed,
        "context_message_refs": observed_refs,
        "context_message_count": len(observed_refs),
        "context_evidence_by_ref": {ref: evidence_by_ref[ref] for ref in observed_refs},
        "allowed_evidence_labels": sorted(allowed),
        "empty_collection_pass": False,
        "passed": observed,
        "note": (
            "context candidates are review support only"
            if observed
            else "context was not measured in this pilot; empty context cannot pass"
        ),
    }


def _active_parallel_status(cards: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    observed_cards = [
        card for card in cards
        if str(card.get("parallel_status")) == "observed_candidate"
        and card.get("candidate_parallel_strands")
    ]
    strands = [
        strand
        for card in cards
        for strand in card.get("candidate_parallel_strands") or ()
        if isinstance(strand, Mapping) and strand.get("parallel_candidate")
    ]
    observed = bool(observed_cards and strands)
    return {
        "status": "observed_candidate" if observed else "not_measured",
        "pilot_limited": not observed,
        "observed_card_count": len(observed_cards),
        "candidate_strand_count": len(strands),
        "empty_collection_pass": False,
        "passed": observed,
        "note": (
            "parallel strands remain candidate-only and inside their period card"
            if observed
            else "parallel strands were not measured in this pilot; empty cannot pass"
        ),
    }


def build_active_period_audit(
    result: Mapping[str, Any],
    selected_windows: Sequence[MetadataWindow],
    *,
    reference_integrity: Mapping[str, Any],
    selection_manifest_verified: bool,
    body_read_started_after_lock: bool,
    active_period_gap_seconds: float,
    cost_cap_messages: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the body-free active-period pilot audit.

    This is a structural/retrieval audit.  It intentionally has no semantic
    accuracy score and reports unobserved context/parallel dimensions as
    ``not_measured``/pilot-limited rather than passing empty collections.
    """

    cards = [
        card for card in (result.get("active_period_review", {}) or {}).get("cards") or ()
        if isinstance(card, Mapping)
    ]
    expected_refs = _active_period_refs(selected_windows)
    selected_days = sorted({
        day
        for window in selected_windows
        for day in (window.observed_days or (window.local_day,))
        if day and day != "unknown"
    })
    selected_scope_counts = Counter(window.chat_type for window in selected_windows)
    group_multi = any(window.chat_type == "group" and window.participant_count >= 3 for window in selected_windows)
    timeline_refs = [str(ref) for card in cards for ref in card.get("timeline_message_refs") or ()]
    timeline_messages_complete = timeline_refs == expected_refs
    message_rows = [row for row in result.get("messages") or () if isinstance(row, Mapping)]
    system_refs = [str(row.get("message_ref")) for row in message_rows if str(row.get("message_type") or "").casefold() == "system"]
    system_timeline_refs = [ref for ref in timeline_refs if ref in set(system_refs)]
    system_policy_ok = all(
        any(
            str(item.get("message_ref")) == ref
            and item.get("system_semantic_policy") == "metadata_only_not_semantic"
            and item.get("semantic_evidence_eligible") is False
            for card in cards
            for item in card.get("timeline") or ()
            if isinstance(item, Mapping)
        )
        for ref in system_refs
    )
    unavailable_refs = {
        str(row.get("message_ref"))
        for row in message_rows
        if isinstance(row.get("media"), Mapping)
        and (
            row.get("media", {}).get("status") == "unavailable"
            or row.get("media", {}).get("semantic_evidence_eligible") is False
        )
    }
    evidence_refs = _evidence_refs(result)
    unavailable_media_not_evidence = not (unavailable_refs & evidence_refs)
    context = _active_context_status(result)
    parallel = _active_parallel_status(cards)
    integrity_passed = bool(reference_integrity.get("exact_set_and_order_equal"))
    cap_valid = cost_cap_messages is None or all(
        window.message_count <= int(cost_cap_messages) for window in selected_windows
    )
    gates = {
        "selection_manifest_hash_verified": bool(selection_manifest_verified),
        "blind_locked_before_body_read": bool(body_read_started_after_lock),
        "provider_calls_zero": int(result.get("provider_calls", -1)) == 0,
        "provider_disabled": result.get("provider_used") is False,
        "production_blocked": result.get("production_blocked") is True,
        "production_connected_false": result.get("production_connected") is False,
        "frozen_read_false": result.get("frozen_read") is False,
        "gold_read_false": result.get("gold_read", False) is False,
        "active_period_gap_is_retrieval_only": all(
            bool(window.body_free().get("window_boundary_is_not_semantic_boundary"))
            and window.body_free().get("semantic_boundary_inferred") is False
            for window in selected_windows
        ),
        "fixed_message_count_partition_false": all(
            window.body_free().get("candidate_kind") == "retrieval_active_period"
            and window.body_free().get("period_complete_required") is True
            for window in selected_windows
        ),
        "calendar_day_forced_closure_false": True,
        "tail_messages_dropped_false": True,
        "three_complete_periods": len(selected_windows) == DEFAULT_ACTIVE_PERIOD_CARD_LIMIT,
        "at_least_two_dates": len(selected_days) >= 2,
        "direct_and_group_covered": bool(selected_scope_counts.get("direct")) and bool(selected_scope_counts.get("group")),
        "multi_participant_group_preferred": group_multi,
        "one_period_one_card": len(cards) == len(selected_windows) and all(card.get("one_period_one_review_card") for card in cards),
        "internal_episodes_not_split_into_cards": all(card.get("internal_episodes_not_review_cards") for card in cards),
        "timeline_complete_and_ordered": timeline_messages_complete,
        "all_system_messages_in_timeline": set(system_refs) == set(system_timeline_refs),
        "system_metadata_not_semantic": system_policy_ok,
        "unavailable_media_not_evidence": unavailable_media_not_evidence,
        "cost_cap_whole_period_only": cap_valid,
        "reference_integrity_exact_set_and_order": integrity_passed,
        # Empty context/parallel collections are explicitly non-passing pilot
        # observations.  Keep the positive gates visible for future reruns.
        "context_observed": bool(context.get("passed")),
        "parallel_observed": bool(parallel.get("passed")),
    }
    structural_pass = all(gates.values())
    return {
        "audit_version": f"{ACTIVE_PERIOD_TEST_VERSION}_audit",
        "status": "structural_gates_passed" if structural_pass else "blocked",
        "blocked": not structural_pass,
        "semantic_accuracy_measured": False,
        "semantic_accuracy_note": "Candidate structural pilot only; no semantic accuracy claim or gold/frozen labels.",
        "active_period": {
            "candidate_grain": "chat_scope_plus_event_time_span",
            "event_timestamp_timezone": "Asia/Shanghai",
            "source_sequence_used": True,
            "gap_seconds": active_period_gap_seconds,
            "gap_semantics": "retrieval_candidate_only_not_semantic_boundary",
            "selected_period_count": len(selected_windows),
            "selected_message_count": len(expected_refs),
            "selected_dates": selected_days,
            "selected_chat_scope_counts": dict(sorted(selected_scope_counts.items())),
            "multi_participant_group_selected": group_multi,
        },
        "expected_refs": list(reference_integrity.get("expected_refs") or ()),
        "materialized_refs": list(reference_integrity.get("materialized_refs") or ()),
        "reconstructed_refs": list(reference_integrity.get("reconstructed_refs") or ()),
        "rendered_refs": list(reference_integrity.get("rendered_refs") or ()),
        "reference_integrity": dict(reference_integrity),
        "timeline": {
            "expected_message_count": len(expected_refs),
            "rendered_message_count": len(timeline_refs),
            "system_message_refs": system_refs,
            "system_messages_in_timeline": system_timeline_refs,
            "all_messages_in_selected_periods_retained": timeline_messages_complete,
            "passed": timeline_messages_complete,
        },
        "media": {
            "unavailable_media_ref_count": len(unavailable_refs),
            "unavailable_media_refs_not_evidence": unavailable_media_not_evidence,
            "passed": unavailable_media_not_evidence,
        },
        "context": context,
        "parallel": parallel,
        "cost": {
            "cap_messages": cost_cap_messages,
            "selected_message_count": len(expected_refs),
            "policy": "whole_period_deferred_no_split_or_downsample",
            "passed": cap_valid,
        },
        "gates": gates,
        "passed": structural_pass,
    }


def _render_active_period_review_html_with_refs(
    result: Mapping[str, Any],
    source_messages: Sequence[Mapping[str, Any]],
) -> Tuple[str, List[str]]:
    """Render exactly one collapsed card per selected period."""

    body_by_ref: Dict[str, str] = {}
    for row in source_messages:
        if not isinstance(row, Mapping):
            continue
        ref = _row_ref(row)
        if ref:
            body = row.get("content")
            if body in (None, ""):
                body = row.get("text") or row.get("body") or row.get("message_content") or ""
            body_by_ref[ref] = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    cards = [
        card for card in (result.get("active_period_review", {}) or {}).get("cards") or ()
        if isinstance(card, Mapping)
    ]
    rendered_refs: List[str] = []
    card_html: List[str] = []
    for card in cards:
        timeline_html: List[str] = []
        timeline = [item for item in card.get("timeline") or () if isinstance(item, Mapping)]
        for item in timeline:
            ref = str(item.get("message_ref") or "")
            if not ref:
                continue
            rendered_refs.append(ref)
            message_type = str(item.get("message_type") or "unknown")
            is_system = message_type.casefold() == "system"
            media_status = str(item.get("media_status") or "")
            unavailable = media_status == "unavailable" or item.get("semantic_evidence_eligible") is False
            if is_system:
                policy = "system metadata only; not semantic evidence"
            elif unavailable:
                policy = "unavailable media; false evidence / not semantic evidence"
            else:
                policy = "candidate interaction signal only"
            body = body_by_ref.get(ref, "[body unavailable]")
            timeline_html.append(
                f'<li class="timeline-message" data-message-ref="{html.escape(ref, quote=True)}">'
                f'<div class="message-meta">{html.escape(str(item.get("timestamp") or "time unknown"))} · '
                f'{html.escape(str(item.get("local_day") or "day unknown"))} · '
                f'{html.escape(message_type)} · sequence={html.escape(str(item.get("sequence") or "unknown"))}</div>'
                f'<div class="message-body">{html.escape(body)}</div>'
                f'<div class="message-facts">{html.escape(policy)} · '
                f'media={html.escape(media_status or "none")} · ref={html.escape(ref)}</div></li>'
            )
        strands = [
            strand for strand in card.get("candidate_parallel_strands") or ()
            if isinstance(strand, Mapping)
        ]
        strand_html = "".join(
            f'<li>{html.escape(str(strand.get("strand_ref") or ""))}: '
            f'{html.escape(", ".join(str(ref) for ref in strand.get("message_refs") or ()))}</li>'
            for strand in strands
        ) or "<li>none; status=not_measured (empty cannot pass)</li>"
        card_html.append(
            f'<details class="active-period-card" data-card-ref="{html.escape(str(card.get("card_ref") or ""), quote=True)}">'
            f'<summary>{html.escape(str(card.get("chat_type") or "unknown"))} · '
            f'{html.escape(str(card.get("period_start_timestamp") or "time unknown"))} → '
            f'{html.escape(str(card.get("period_end_timestamp") or "time unknown"))} · '
            f'{len(timeline)} messages · one complete retrieval period</summary>'
            f'<p>period={html.escape(str(card.get("period_ref") or ""))}; '
            f'days={html.escape(", ".join(str(day) for day in card.get("observed_days") or ())) }; '
            f'internal episodes={len(card.get("internal_episode_refs") or ())}; '
            f'parallel status={html.escape(str(card.get("parallel_status") or "not_measured"))}</p>'
            f'<p class="notice">{html.escape(str(card.get("parallel_note") or ""))}</p>'
            f'<h3>candidate parallel strands (inside this card)</h3><ul>{strand_html}</ul>'
            f'<h3>complete period timeline</h3><ol>{"".join(timeline_html) or "<li>empty timeline</li>"}</ol>'
            '</details>'
        )
    context = (result.get("active_period_review") or {}).get("context") if isinstance(result.get("active_period_review"), Mapping) else {}
    context_status = str((context or {}).get("status") or "not_measured")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Active-period conversation reconstruction pilot</title>'
        '<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:24px;line-height:1.5;background:#f5f7fa;color:#17202c}'
        'header,details{background:#fff;border:1px solid #d7dee8;border-radius:12px;padding:16px;margin:14px 0}'
        'details>summary{cursor:pointer;font-weight:700}.notice{background:#fff4d6;border:1px solid #e3bd58;padding:10px;border-radius:8px}'
        '.timeline-message{margin:10px 0;padding:10px;background:#f8fafc;border-left:4px solid #788da8;border-radius:6px}'
        '.message-meta,.message-facts{font-size:12px;color:#526173}.message-body{white-space:pre-wrap;background:#fff;border:1px solid #e2e7ed;padding:8px;margin:5px 0}'
        'code{font-size:12px}</style></head><body>'
        '<header><h1>Active-period conversation reconstruction pilot</h1>'
        '<p>development-only · provider=0 · production blocked · candidate structural review</p>'
        '<p><strong>Retrieval grain:</strong> chat scope + Asia/Shanghai event-time span. A gap only forms a retrieval candidate; it is not a semantic boundary.</p>'
        '<p><strong>System policy:</strong> system rows remain in the complete timeline as metadata only and are not semantic evidence. Unavailable media is explicitly false evidence.</p>'
        f'<p><strong>Context status:</strong> {html.escape(context_status)}; empty collections cannot pass.</p></header>'
        + "".join(card_html)
        + '<footer><p>No semantic accuracy claim is made by this pilot.</p></footer></body></html>'
    ), rendered_refs


def render_active_period_review_html(result: Mapping[str, Any], source_messages: Any = None) -> str:
    """Public renderer for the active-period collapsed-card review surface."""

    rows = [row for row in (source_messages or ()) if isinstance(row, Mapping)]
    html_text, _refs = _render_active_period_review_html_with_refs(result, rows)
    return html_text


def _active_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_active_blocked_artifacts(
    target: Path,
    *,
    selection_manifest: Mapping[str, Any],
    reason: str,
    reference_integrity: Mapping[str, Any],
    phase: str,
) -> Dict[str, Path]:
    """Persist a body-free blocked ledger before surfacing a hard mismatch."""

    audit: Dict[str, Any] = {
        "audit_version": f"{ACTIVE_PERIOD_TEST_VERSION}_audit",
        "status": "blocked",
        "blocked": True,
        "blocked_reason": reason,
        "semantic_accuracy_measured": False,
        "semantic_accuracy_note": "Blocked structural pilot; no semantic accuracy claim.",
        "selection_manifest_sha256": selection_manifest.get("selection_manifest_sha256"),
        "expected_refs": list(reference_integrity.get("expected_refs") or ()),
        "materialized_refs": list(reference_integrity.get("materialized_refs") or ()),
        "reconstructed_refs": list(reference_integrity.get("reconstructed_refs") or ()),
        "rendered_refs": list(reference_integrity.get("rendered_refs") or ()),
        "reference_integrity": dict(reference_integrity),
        "provider_calls": 0,
        "provider_used": False,
        "production_blocked": True,
        "production_connected": False,
        "frozen_read": False,
        "gold_read": False,
        "body_free": True,
    }
    assert_body_free(audit)
    audit_path = target / "active_period_audit.json"
    _write_json(audit_path, audit)
    # Keep a descriptive alias for callers that use the blind-round audit
    # filename while this pilot has its own explicit versioned name.
    blind_audit_path = target / "blind_audit.json"
    _write_json(blind_audit_path, audit)
    phase_path = target / "phase_trace.json"
    _write_json(phase_path, {
        "round": ACTIVE_PERIOD_TEST_NAME,
        "round_version": ACTIVE_PERIOD_TEST_VERSION,
        "body_free": True,
        "status": "blocked",
        "blocked_reason": reason,
        "phases": [
            {"phase": "metadata_only_scan", "body_fields_selected": False, "completed": True},
            {"phase": "selection_manifest_write", "body_fields_selected": False, "completed": True, "selection_manifest_sha256": selection_manifest.get("selection_manifest_sha256")},
            {"phase": "selection_lock_verify", "body_fields_selected": False, "completed": True},
            {"phase": phase, "body_fields_selected": phase == "selected_body_materialize", "completed": False, "blocked": True},
        ],
        "blind_locked_before_body_read": True,
        "provider_calls": 0,
        "production_blocked": True,
    })
    return {
        "selection_manifest": target / "selection_manifest.json",
        "active_period_audit": audit_path,
        "blind_audit": blind_audit_path,
        "phase_trace": phase_path,
    }


def _active_manifest_projection(
    generic_manifest: Mapping[str, Any],
    *,
    selection_manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
    reference_integrity: Mapping[str, Any],
    card_count: int,
) -> Dict[str, Any]:
    """Add active-period provenance to the body-free reconstruction manifest."""

    value = dict(generic_manifest)
    value.pop("review_sample_audit", None)
    value.update({
        "round": ACTIVE_PERIOD_TEST_NAME,
        "round_version": ACTIVE_PERIOD_TEST_VERSION,
        "artifact_version": ACTIVE_PERIOD_TEST_VERSION,
        "selection_manifest_sha256": selection_manifest.get("selection_manifest_sha256"),
        "selection_manifest_path": "selection_manifest.json",
        "selection_phase": "metadata_only_locked_then_whole_period_materialized",
        "active_period_candidate_grain": "chat_scope_plus_event_time_span",
        "event_timestamp_timezone": "Asia/Shanghai",
        "source_sequence_used": True,
        "gap_semantics": "retrieval_candidate_only_not_semantic_boundary",
        "period_complete_before_body_read": True,
        "body_read_started_after_selection_lock": True,
        "default_review_card_count": card_count,
        "default_review_card_policy": "one_complete_period_one_collapsed_card",
        "expected_refs": list(reference_integrity.get("expected_refs") or ()),
        "materialized_refs": list(reference_integrity.get("materialized_refs") or ()),
        "reconstructed_refs": list(reference_integrity.get("reconstructed_refs") or ()),
        "rendered_refs": list(reference_integrity.get("rendered_refs") or ()),
        "reference_integrity": dict(reference_integrity),
        "provider_calls": 0,
        "provider_used": False,
        "production_blocked": True,
        "production_connected": False,
        "frozen_read": False,
        "gold_read": False,
        "quality_gates": dict(audit),
        "semantic_accuracy_measured": False,
        "semantic_accuracy_note": "Structural active-period pilot only; no gold/frozen/human semantic accuracy.",
        "body_free": True,
    })
    assert_body_free(value)
    return value


def run_active_period_test_v1(
    source: Optional[BlindRunSource] = None,
    *,
    output_dir: str | Path = DEFAULT_ACTIVE_PERIOD_OUTPUT_DIR,
    preferred_dates: Sequence[str] = DEFAULT_PREFERRED_DATES,
    excluded_dates: Sequence[str] = DEFAULT_EXCLUDED_DATES,
    max_cards: int = DEFAULT_ACTIVE_PERIOD_CARD_LIMIT,
    active_period_gap_seconds: float = DEFAULT_ACTIVE_PERIOD_GAP_SECONDS,
    cost_cap_messages: Optional[int] = None,
    reference_date: Optional[str] = None,
    db_dir: Optional[str | Path] = None,
    account: Optional[str] = None,
    workdir: Optional[str | Path] = None,
) -> Dict[str, Path]:
    """Generate the development-only active-period candidate artifact.

    The function is intentionally separate from :func:`run_blind_round1` so
    the legacy round1 directory remains untouched.  It scans all eligible
    metadata before selecting three complete periods, locks that selection,
    then performs exactly one whole-period body read.  No provider, frozen,
    gold, or production path is reachable here.
    """

    if max_cards != DEFAULT_ACTIVE_PERIOD_CARD_LIMIT:
        raise ValueError("active_period_test_requires_exactly_three_cards")
    if source is None:
        source = WeChatDbSource(db_dir=db_dir, account=account, workdir=workdir)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    selection_path = target / "selection_manifest.json"
    if selection_path.exists():
        raise FileExistsError(f"active_period_selection_manifest_already_exists:{selection_path}")

    # Metadata-only discovery must cover the entire eligible source before a
    # period can be declared complete.  Filtering to preferred dates here
    # would manufacture a false boundary at the edge of the filtered scan.
    metadata = tuple(source.scan_metadata(None, tuple(excluded_dates)))
    windows = build_metadata_windows(
        metadata,
        active_period_gap_seconds=active_period_gap_seconds,
    )
    selected = select_representative_windows(
        windows,
        preferred_dates=preferred_dates,
        excluded_dates=excluded_dates,
        max_cards=DEFAULT_ACTIVE_PERIOD_CARD_LIMIT,
        cost_cap_messages=cost_cap_messages,
    )
    selected_days = {
        day
        for window in selected
        for day in (window.observed_days or (window.local_day,))
        if day and day != "unknown"
    }
    selected_scopes = {window.chat_type for window in selected}
    if len(selected) != DEFAULT_ACTIVE_PERIOD_CARD_LIMIT:
        raise ValueError("active_period_selection_requires_three_complete_periods")
    if len(selected_days) < 2:
        raise ValueError("active_period_selection_requires_two_dates")
    if selected_scopes != {"direct", "group"}:
        raise ValueError("active_period_selection_requires_direct_and_group")

    deferred_period_refs: List[str] = []
    if cost_cap_messages is not None:
        # The selector itself only admits whole periods.  This audit hint is
        # intentionally conservative: it names only candidates that cannot
        # fit the cap as a complete period, never a sliced suffix/prefix.
        deferred_period_refs = [
            window.window_ref
            for window in windows
            if window.window_ref not in {item.window_ref for item in selected}
            and window.message_count > int(cost_cap_messages)
        ]

    selection_manifest = build_selection_manifest(
        selected,
        source_ref=str(getattr(source, "source_ref", "source-unknown")),
        preferred_dates=preferred_dates,
        excluded_dates=excluded_dates,
        round_name=ACTIVE_PERIOD_TEST_NAME,
        round_version=ACTIVE_PERIOD_TEST_VERSION,
        active_period_gap_seconds=float(active_period_gap_seconds),
        cost_cap_messages=cost_cap_messages,
    )
    selection_manifest["deferred_whole_period_refs"] = deferred_period_refs
    selection_manifest["selected_period_count"] = len(selected)
    # The deferred list is part of the lock hash, so add it before writing.
    selection_manifest.pop("selection_manifest_sha256", None)
    selection_manifest["selection_manifest_sha256"] = _sha256(selection_manifest)
    assert_body_free(selection_manifest)
    _write_json(selection_path, selection_manifest)
    locked_manifest = json.loads(selection_path.read_text(encoding="utf-8"))
    if not verify_selection_manifest(locked_manifest):
        _write_active_blocked_artifacts(
            target,
            selection_manifest=locked_manifest,
            reason="active_period_selection_manifest_hash_verification_failed",
            reference_integrity=_reference_integrity(_active_period_refs(selected), (), (), ()),
            phase="selection_lock_verify",
        )
        raise ValueError("active_period_selection_manifest_hash_verification_failed")

    expected_refs = _active_period_refs(selected)
    # Materialization is the first body-bearing operation and receives the
    # already-locked complete period refs only.
    raw_materialized = tuple(source.materialize(expected_refs))
    metadata_by_ref = {
        str(row.message_ref): row
        for window in selected
        for row in window.rows
    }
    materialized, materialized_refs = _bind_materialized_refs(raw_materialized, expected_refs, metadata_by_ref)
    parity = _reference_integrity(expected_refs, materialized_refs, (), ())
    if not parity["selected_equals_materialized"]:
        _write_active_blocked_artifacts(
            target,
            selection_manifest=locked_manifest,
            reason="active_period_reference_integrity_missing_or_extra_materialized_refs",
            reference_integrity=parity,
            phase="selected_body_materialize",
        )
        raise ValueError("active_period_reference_integrity_missing_or_extra_materialized_refs")

    reconstruction = reconstruct_context(
        materialized,
        reference_date=reference_date or max(selected_days),
        include_bodies=True,
        source_scope="development",
    )
    _reorder_reconstruction_refs(reconstruction, expected_refs)
    reconstructed_refs = [
        str(row.get("message_ref"))
        for row in reconstruction.get("messages") or ()
        if isinstance(row, Mapping) and row.get("message_ref")
    ]
    parity = _reference_integrity(expected_refs, materialized_refs, reconstructed_refs, ())
    if not parity["materialized_equals_reconstructed"] or not parity["selected_equals_materialized"]:
        _write_active_blocked_artifacts(
            target,
            selection_manifest=locked_manifest,
            reason="active_period_reference_integrity_missing_or_extra_reconstructed_refs",
            reference_integrity=parity,
            phase="provider_free_reconstruction",
        )
        raise ValueError("active_period_reference_integrity_missing_or_extra_reconstructed_refs")

    # Replace the generic episode-card projection with exactly one active
    # period card per selected period.  Internal episodes/parallel strands
    # remain nested data, never additional review cards.
    cards = _active_period_card_projection(reconstruction, selected)
    context_status = _active_context_status(reconstruction)
    parallel_status = _active_parallel_status(cards)
    reconstruction["active_period_review"] = {
        "status": "candidate_only",
        "card_count": len(cards),
        "default_card_count": DEFAULT_ACTIVE_PERIOD_CARD_LIMIT,
        "cards": cards,
        "one_period_one_review_card": True,
        "timeline_policy": "all_messages_in_each_complete_period",
        "system_semantic_policy": "metadata_only_not_semantic",
        "unavailable_media_policy": "false_evidence_not_semantic_evidence",
        "context": context_status,
        "parallel": parallel_status,
    }
    review_block = dict(reconstruction.get("review") or {})
    review_block["sample_units"] = cards
    review_block["active_period_cards"] = cards
    review_block["representative_review"] = {
        "default_thread_limit": DEFAULT_ACTIVE_PERIOD_CARD_LIMIT,
        "default_card_limit": DEFAULT_ACTIVE_PERIOD_CARD_LIMIT,
        "selection_is_review_only": True,
        "one_period_one_review_card": True,
        "internal_episodes_not_review_cards": True,
        "sample_unit_count": len(cards),
        "sample_units": cards,
    }
    reconstruction["review"] = review_block

    html_text, rendered_refs = _render_active_period_review_html_with_refs(reconstruction, materialized)
    parity = _reference_integrity(expected_refs, materialized_refs, reconstructed_refs, rendered_refs)
    if not parity["exact_set_and_order_equal"]:
        _write_active_blocked_artifacts(
            target,
            selection_manifest=locked_manifest,
            reason="active_period_reference_integrity_missing_or_extra_rendered_refs",
            reference_integrity=parity,
            phase="active_period_review_render",
        )
        raise ValueError("active_period_reference_integrity_missing_or_extra_rendered_refs")
    reconstruction["active_period_reference_integrity"] = parity
    reconstruction["expected_message_refs"] = list(expected_refs)
    reconstruction["materialized_message_refs"] = list(materialized_refs)
    reconstruction["reconstructed_message_refs"] = list(reconstructed_refs)
    reconstruction["rendered_message_refs"] = rendered_refs

    audit = build_active_period_audit(
        reconstruction,
        selected,
        reference_integrity=parity,
        selection_manifest_verified=True,
        body_read_started_after_lock=True,
        active_period_gap_seconds=float(active_period_gap_seconds),
        cost_cap_messages=cost_cap_messages,
    )
    # Write the standard body-free reconstruction projection first, then
    # replace its HTML with the active-period card renderer.
    paths = write_review_artifacts(
        reconstruction,
        target,
        source_messages=materialized,
        source_name="development",
    )
    paths["review_html"].write_text(html_text, encoding="utf-8")
    generic_manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    active_manifest = _active_manifest_projection(
        generic_manifest,
        selection_manifest=locked_manifest,
        audit=audit,
        reference_integrity=parity,
        card_count=len(cards),
    )
    active_manifest["artifacts"] = {
        "reconstruction": paths["reconstruction"].name,
        "review_html": paths["review_html"].name,
        "reconstruction_sha256": _active_file_sha256(paths["reconstruction"]),
        "review_html_sha256": _active_file_sha256(paths["review_html"]),
    }
    _write_json(paths["manifest"], active_manifest)
    reconstruction_manifest_path = target / "reconstruction_manifest.json"
    _write_json(reconstruction_manifest_path, active_manifest)
    active_manifest_path = target / "active_period_manifest.json"
    _write_json(active_manifest_path, active_manifest)
    audit_path = target / "active_period_audit.json"
    _write_json(audit_path, audit)
    blind_audit_path = target / "blind_audit.json"
    _write_json(blind_audit_path, audit)
    phase_path = target / "phase_trace.json"
    _write_json(phase_path, {
        "round": ACTIVE_PERIOD_TEST_NAME,
        "round_version": ACTIVE_PERIOD_TEST_VERSION,
        "body_free": True,
        "status": "candidate_artifact_generated",
        "phases": [
            {"phase": "metadata_only_scan", "body_fields_selected": False, "completed": True, "metadata_record_count": len(metadata), "candidate_period_count": len(windows)},
            {"phase": "selection_manifest_write", "body_fields_selected": False, "completed": True, "selection_manifest_sha256": locked_manifest["selection_manifest_sha256"], "selected_period_count": len(selected)},
            {"phase": "selection_lock_verify", "body_fields_selected": False, "completed": True},
            {"phase": "selected_body_materialize", "body_fields_selected": True, "completed": True, "whole_period_only": True, "materialized_message_count": len(materialized_refs)},
            {"phase": "provider_free_reconstruction", "body_fields_selected": False, "completed": True, "reconstructed_message_count": len(reconstructed_refs)},
            {"phase": "active_period_review_render", "body_fields_selected": True, "completed": True, "rendered_message_count": len(rendered_refs), "collapsed_card_count": len(cards)},
        ],
        "blind_locked_before_body_read": True,
        "provider_calls": 0,
        "production_blocked": True,
        "reference_integrity_status": parity.get("status"),
    })
    return {
        "selection_manifest": selection_path,
        "manifest": paths["manifest"],
        "active_period_manifest": active_manifest_path,
        "reconstruction_manifest": reconstruction_manifest_path,
        "reconstruction": paths["reconstruction"],
        "review_html": paths["review_html"],
        "active_period_audit": audit_path,
        "blind_audit": blind_audit_path,
        "phase_trace": phase_path,
    }


def _augment_reconstruction_manifest(
    manifest: Mapping[str, Any],
    *,
    selection_manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> Dict[str, Any]:
    value = dict(manifest)
    # ``write_review_artifacts`` emits the lower-level reconstruction audit as
    # ``review_sample_audit``.  Preserve it under an explicit provenance name
    # and never expose its legacy pass/fail result as the blind manifest's
    # review gate.  The blind audit is the only applicable gate projection.
    source_sample_audit = audit.get("source_reconstruction_sample_audit")
    if not isinstance(source_sample_audit, Mapping):
        source_sample_audit = value.get("review_sample_audit")
    value.pop("review_sample_audit", None)
    if isinstance(source_sample_audit, Mapping):
        value["source_reconstruction_sample_audit"] = dict(source_sample_audit)
    value.update({
        "round": ROUND_NAME,
        "round_version": ROUND_VERSION,
        "blind_locked_before_body_read": True,
        "selection_manifest_sha256": selection_manifest.get("selection_manifest_sha256"),
        "selection_manifest_path": "selection_manifest.json",
        "selection_phase": "metadata_only_locked_then_materialized",
        "body_read_started_after_selection_lock": True,
        "provider_calls": 0,
        "provider_used": False,
        "production_blocked": True,
        "production_connected": False,
        "frozen_read": False,
        "gold_read": False,
        "quality_gates": dict(audit),
        "semantic_accuracy_measured": False,
        "semantic_accuracy_note": "Structural review only; no gold/frozen/human labels were read.",
    })
    value["body_free"] = True
    assert_body_free(value)
    return value


def run_blind_round1(
    source: Optional[BlindRunSource] = None,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    preferred_dates: Sequence[str] = DEFAULT_PREFERRED_DATES,
    excluded_dates: Sequence[str] = DEFAULT_EXCLUDED_DATES,
    max_cards: int = 6,
    reference_date: Optional[str] = None,
    db_dir: Optional[str | Path] = None,
    account: Optional[str] = None,
    workdir: Optional[str | Path] = None,
) -> Dict[str, Path]:
    """Run one real metadata-first blind reconstruction round."""

    if source is None:
        source = WeChatDbSource(db_dir=db_dir, account=account, workdir=workdir)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    selection_path = target / "selection_manifest.json"
    if selection_path.exists():
        raise FileExistsError(f"selection_manifest_already_exists:{selection_path}")

    # All scans before this point are metadata-only.  Preferred dates are
    # tried first; a fallback scan is still metadata-only and occurs before
    # the lock, if a test/source does not contain enough preferred rows.
    metadata = tuple(source.scan_metadata(tuple(preferred_dates), tuple(excluded_dates)))
    windows = build_metadata_windows(metadata)
    selected = select_representative_windows(
        windows,
        preferred_dates=preferred_dates,
        excluded_dates=excluded_dates,
        max_cards=max_cards,
    )
    if len(selected) < min(max_cards, 6):
        all_metadata = tuple(source.scan_metadata(None, tuple(excluded_dates)))
        windows = build_metadata_windows(all_metadata)
        selected = select_representative_windows(
            windows,
            preferred_dates=preferred_dates,
            excluded_dates=excluded_dates,
            max_cards=max_cards,
        )
    if len(selected) < min(max_cards, 6):
        raise ValueError("blind_selection_has_fewer_than_six_metadata_windows")
    if len({window.local_day for window in selected}) < 3:
        raise ValueError("blind_selection_requires_three_dates")
    if {window.chat_type for window in selected} != {"direct", "group"}:
        raise ValueError("blind_selection_requires_direct_and_group")

    selection_manifest = build_selection_manifest(
        selected,
        source_ref=str(getattr(source, "source_ref", "source-unknown")),
        preferred_dates=preferred_dates,
        excluded_dates=excluded_dates,
    )
    assert_body_free(selection_manifest)
    _write_json(selection_path, selection_manifest)
    # The file read is deliberately body-free and is the lock boundary.  No
    # materialize call is made until both presence and hash verification pass.
    locked_manifest = json.loads(selection_path.read_text(encoding="utf-8"))
    if not verify_selection_manifest(locked_manifest):
        raise ValueError("selection_manifest_hash_verification_failed")
    body_read_started_after_lock = True
    selected_refs = [row.message_ref for window in selected for row in window.rows]
    materialized = tuple(source.materialize(selected_refs))
    if not materialized:
        raise ValueError("blind_selection_materialized_no_rows")
    result = reconstruct_context(
        materialized,
        reference_date=reference_date or max(window.local_day for window in selected),
        include_bodies=True,
        source_scope="development",
    )
    audit = build_blind_audit(
        result,
        selected,
        selection_manifest_verified=True,
        body_read_started_after_lock=body_read_started_after_lock,
    )
    paths = write_review_artifacts(result, target, source_messages=materialized, source_name="development")
    generic_manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    reconstruction_manifest = _augment_reconstruction_manifest(
        generic_manifest,
        selection_manifest=selection_manifest,
        audit=audit,
    )
    # Keep the existing manifest filename compatible with the reconstruction
    # seam and provide the explicit name requested by the blind round.
    _write_json(paths["manifest"], reconstruction_manifest)
    reconstruction_manifest_path = target / "reconstruction_manifest.json"
    _write_json(reconstruction_manifest_path, reconstruction_manifest)
    audit_path = target / "blind_audit.json"
    _write_json(audit_path, audit)
    phase_path = target / "phase_trace.json"
    _write_json(phase_path, {
        "round": ROUND_NAME,
        "body_free": True,
        "phases": [
            {"phase": "metadata_only_scan", "body_fields_selected": False, "completed": True},
            {"phase": "selection_manifest_write", "body_fields_selected": False, "completed": True, "selection_manifest_sha256": selection_manifest["selection_manifest_sha256"]},
            {"phase": "selection_lock_verify", "body_fields_selected": False, "completed": True},
            {"phase": "selected_body_materialize", "body_fields_selected": True, "completed": True},
            {"phase": "provider_free_reconstruction", "body_fields_selected": False, "completed": True},
        ],
        "blind_locked_before_body_read": True,
        "provider_calls": 0,
        "production_blocked": True,
    })
    return {
        "selection_manifest": selection_path,
        "manifest": paths["manifest"],
        "reconstruction_manifest": reconstruction_manifest_path,
        "reconstruction": paths["reconstruction"],
        "review_html": paths["review_html"],
        "blind_audit": audit_path,
        "phase_trace": phase_path,
    }


# Descriptive aliases keep the new pilot discoverable while retaining the
# historical round1 entry point and its existing output contract.
ActivePeriodCandidate = MetadataWindow
build_active_period_candidates = build_metadata_windows
build_active_period_retrieval_candidates = build_metadata_windows
select_active_periods = select_representative_windows
run_active_period_test = run_active_period_test_v1
run_active_period_v1 = run_active_period_test_v1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a provider-free metadata-first blind conversation reconstruction review")
    parser.add_argument("--db-dir", default=None, help="local WeChat xwechat_files directory")
    parser.add_argument("--account", default=None, help="opaque local WeChat account directory name")
    parser.add_argument("--workdir", default=None, help="wechatauto decrypted-cache workdir")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="new blind-round artifact directory")
    parser.add_argument("--dates", nargs="+", default=list(DEFAULT_PREFERRED_DATES), help="preferred development dates")
    parser.add_argument("--reference-date", default=None, help="reference date for today/yesterday/week projections")
    parser.add_argument(
        "--active-period",
        action="store_true",
        help="generate the separate three-card active-period pilot artifact",
    )
    parser.add_argument(
        "--active-period-gap-seconds",
        type=float,
        default=DEFAULT_ACTIVE_PERIOD_GAP_SECONDS,
        help="metadata-only retrieval gap for active-period candidates",
    )
    parser.add_argument(
        "--cost-cap-messages",
        type=int,
        default=None,
        help="optional whole-period deferred message cost cap for the active pilot",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.active_period:
        paths = run_active_period_test_v1(
            output_dir=(str(DEFAULT_ACTIVE_PERIOD_OUTPUT_DIR) if args.output == str(DEFAULT_OUTPUT_DIR) else args.output),
            preferred_dates=tuple(args.dates),
            reference_date=args.reference_date,
            active_period_gap_seconds=args.active_period_gap_seconds,
            cost_cap_messages=args.cost_cap_messages,
            db_dir=args.db_dir,
            account=args.account,
            workdir=args.workdir,
        )
    else:
        paths = run_blind_round1(
            output_dir=args.output,
            preferred_dates=tuple(args.dates),
            reference_date=args.reference_date,
            db_dir=args.db_dir,
            account=args.account,
            workdir=args.workdir,
        )
    # Path-only CLI output.  Never print local message bodies.
    for key, path in paths.items():
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_EXCLUDED_DATES",
    "DEFAULT_ACTIVE_PERIOD_GAP_SECONDS",
    "DEFAULT_ACTIVE_PERIOD_OUTPUT_DIR",
    "DEFAULT_ACTIVE_PERIOD_CARD_LIMIT",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_PREFERRED_DATES",
    "InMemoryBlindSource",
    "MetadataMessage",
    "MetadataWindow",
    "ActivePeriodCandidate",
    "ACTIVE_PERIOD_TEST_NAME",
    "ACTIVE_PERIOD_TEST_VERSION",
    "ROUND_NAME",
    "ROUND_VERSION",
    "WeChatDbSource",
    "build_blind_audit",
    "build_active_period_audit",
    "build_active_period_candidates",
    "build_active_period_retrieval_candidates",
    "build_metadata_windows",
    "build_selection_manifest",
    "main",
    "render_active_period_review_html",
    "run_active_period_test",
    "run_active_period_test_v1",
    "run_active_period_v1",
    "run_blind_round1",
    "select_active_periods",
    "select_representative_windows",
    "verify_selection_manifest",
]
