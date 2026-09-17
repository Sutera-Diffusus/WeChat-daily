"""Private gold-standard tooling for the authoritative 2026-08-25 contract.

The automatic exporter creates only a *private pre-redaction working seed*.
It cannot create a frozen release: free-form names, organizations, amounts and
secrets require human privacy review.  The module is disconnected from the
production API and opens only an explicitly supplied SQLite archive through a
WAL-aware read-only URI.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .semantic_pipeline import EVENT_RELATIONS, SemanticResultV2


SCHEMA_VERSION = "gold_semantic_v1"
DATASET_VERSION = "wechat-2026-08-25-v1"
ANNOTATION_GUIDE_VERSION = "gold-standard-2026-08-25-contract-v1"
REDACTION_POLICY_VERSION = "private-pre-redaction-v1"
TOOL_VERSION = "semantic_gold_tools_v2"
OBSERVABLE_SUPPORT_CONTRACT_VERSION = "same_event_observable_support_v1"
LOCAL_DAY = "2026-08-25"
BEIJING_TIMEZONE = "Asia/Shanghai"
WINDOW_START_LOCAL = "2026-08-25T00:00:00+08:00"
WINDOW_END_LOCAL = "2026-08-26T00:00:00+08:00"
WINDOW_START_UTC = "2026-08-24T16:00:00+00:00"
WINDOW_END_UTC = "2026-08-25T16:00:00+00:00"

PRIVATE_ROOT = Path("data") / "private" / "gold_standard" / LOCAL_DAY
DEFAULT_WORKING_DIR = PRIVATE_ROOT / "working"
DEFAULT_RELEASE_DIR = PRIVATE_ROOT / "releases" / "v1"
# Backward-compatible name, now aligned to the authoritative contract.
DEFAULT_PRIVATE_OUTPUT_DIR = DEFAULT_WORKING_DIR

PRIVATE_JSONL_FILES = (
    "messages.private.jsonl",
    "mentions.private.jsonl",
    "claims.private.jsonl",
    "relations.private.jsonl",
    "clusters.private.jsonl",
    "presentations.private.jsonl",
    "adjudications.private.jsonl",
)
COLLECTION_BY_FILE = {
    "messages.private.jsonl": "messages",
    "mentions.private.jsonl": "mentions",
    "claims.private.jsonl": "claims",
    "relations.private.jsonl": "relations",
    "clusters.private.jsonl": "clusters",
    "presentations.private.jsonl": "presentations",
    "adjudications.private.jsonl": "adjudications",
}
COLLECTIONS = tuple(COLLECTION_BY_FILE.values())

_REQUIRED_MANIFEST_FIELDS = {
    "dataset_id", "dataset_version", "status", "workflow_state", "scope_local_day",
    "schema_version", "annotation_guide_version", "redaction_policy_version",
    "pseudonym_key_id", "sampling_seed", "sampling_strata_and_targets",
    "coverage_counts", "coverage_shortfalls", "split_policy",
    "source_snapshot_fingerprint_hmac", "file_sha256", "record_counts_by_file",
    "label_counts", "must_not_link_count", "double_annotation_coverage",
    "adjudication_count", "privacy_scan_status", "created_at", "frozen_at",
    "supersedes", "known_limitations", "version_lineage", "release_eligible",
}
_COMMON_FIELDS = {"schema_version", "dataset_version", "record_id", "annotation_status", "provenance"}
_MESSAGE_FIELDS = {
    "message_id", "account_id", "chat_id", "chat_type", "speaker_id", "direction",
    "message_type", "local_day", "time_offset_seconds", "time_bucket",
    "sequence_in_chat", "reply_to_message_id", "redacted_text", "redaction_types",
    "media_state", "source_mode", "context_message_ids", "split",
}
# The contract uses a common ``record_id`` for audit/provenance, but every
# semantic object also has a domain-specific ID.  Foreign keys must resolve
# against the latter; falling back to ``record_id`` silently permits malformed
# rows and, more importantly, hides duplicate domain IDs when indexing.
_TYPED_ID_FIELDS = {
    "messages": "message_id",
    "mentions": "mention_id",
    "claims": "claim_id",
    "relations": "relation_id",
    "clusters": "cluster_id",
    "presentations": "presentation_id",
    "adjudications": "adjudication_id",
}
# ``event_seed`` is an anchor type in the contract, but v1 stores its seed on
# the event/cluster row rather than in a separate JSONL file.  Keep it as a
# first-class virtual typed index below so a seed ID cannot accidentally fall
# through to the generic record_id namespace.
_EVENT_SEED_FIELDS = ("event_seed_id", "event_id", "event_instance_id", "event_seed")
_MENTION_FIELDS = {
    "mention_id", "message_id", "mention_type", "span_start", "span_end",
    "surface_redacted", "normalized_id", "normalized_type", "attributes",
    "certainty", "annotator_notes",
}
_CLAIM_FIELDS = {
    "claim_id", "message_id", "speaker_id", "claim_type", "claim_text_redacted",
    "target_entity_ids", "event_mention_ids", "evidence_spans", "stance",
    "polarity", "modality", "status", "attribution", "timestamp_message_id",
    "context_message_ids",
}
_RELATION_FIELDS = {
    "relation_id", "left_anchor_id", "right_anchor_id", "anchor_type", "label",
    "supporting_slot_codes", "conflicting_slot_codes", "evidence_message_ids",
    "must_not_link", "must_not_link_reason_codes", "confidence",
    "adjudication_id",
}
# A merged relation may be one-sided when an annotator did not produce a
# source row.  The absence is valid only when it is explicit and auditable;
# a missing label without one of these markers remains a contract error.
_RELATION_MISSING_SIDE_FIELDS = {
    "missing_side", "missing_sides", "source_missing_side", "source_missing_sides",
    "missing_in_a", "missing_in_b", "annotator_a_missing", "annotator_b_missing",
    "source_a_missing", "source_b_missing", "missing_a", "missing_b",
    "a_missing", "b_missing", "annotator_a_status", "annotator_b_status",
    "source_a_status", "source_b_status", "annotator_a_state", "annotator_b_state",
}
_RELATION_MISSING_STATUS_VALUES = frozenset({"missing", "absent", "not_provided", "not-provided"})
_ADJUDICATION_EVIDENCE_REF_CONTRACT_VERSION = "typed_adjudication_evidence_v1"
# ``observable_support_refs`` is conditionally required for same_event rows;
# it remains optional on the other four relation labels so older related/topic
# edges keep their original wire shape.
_OBSERVABLE_SUPPORT_REF_FIELD = "observable_support_refs"
_OBSERVABLE_REF_TYPES = frozenset({"message", "mention", "claim"})
_OBSERVABLE_INSTANCE_FIELDS = (
    "event_instance_id", "instance_id", "shared_instance_id",
    "explicit_instance_id", "event_key", "event_id", "event_seed_id",
)
_OBSERVABLE_REF_SLOT_ALIASES = {
    "explicit_shared_instance": frozenset(
        {"explicit_shared_instance", "shared_instance", "same_instance", "instance"}
    ),
    "shared_action": frozenset({"shared_action", "action", "action_type"}),
    "shared_core_object": frozenset(
        {"shared_core_object", "core_object", "core_entity", "entity", "object"}
    ),
    "explicit_continuation_cue": frozenset(
        {"explicit_continuation_cue", "explicit_continuation", "continuation_cue"}
    ),
    "shared_event_or_state": frozenset(
        {"shared_event_or_state", "shared_event", "shared_state", "event_or_state"}
    ),
    "same_message": frozenset({"same_message"}),
    "explicit_reply": frozenset({"explicit_reply", "reply", "reply_to_message"}),
}
_OBSERVABLE_INSTANCE_SLOT_CODES = frozenset(
    {
        "explicit_shared_instance", "shared_instance", "same_instance", "instance",
        "same_message", "explicit_reply", "reply", "reply_to_message",
        "explicit_continuation", "explicit_continuation_cue", "continuation_cue",
    }
)
_CLUSTER_FIELDS = {
    "cluster_id", "cluster_type", "event_type", "core_entity_ids", "action_types",
    "intent_types", "state_sequence", "mention_ids", "claim_ids",
    "member_message_ids", "relation_ids", "must_not_link_checked",
    "start_message_id", "end_message_id", "topic_family_ids",
    "summary_of_boundary_redacted", "uncertainties",
}
_PRESENTATION_FIELDS = {
    "presentation_id", "presentation_type", "source_cluster_ids", "source_claim_ids",
    "title_redacted", "sentence_units", "participant_ids", "fact_claim_ids",
    "opinion_claim_ids", "question_claim_ids", "status", "uncertainties",
    "detail_policy", "expected_order_group", "must_remain_separate_from",
    "display_decision_reason_codes",
}
_ANCHOR_TYPES = frozenset({"mention", "claim", "event_seed"})
_ANCHOR_TARGET_COLLECTIONS = {
    "mention": "mentions",
    "claim": "claims",
    "event_seed": "event_seed",
}
_CLUSTER_TYPES = frozenset({"event", "non_event_context", "insufficient_context"})
_PRESENTATION_TYPES = frozenset({"event_card", "topic_observation", "trend_item", "do_not_display"})
_MENTION_TYPES = frozenset({"entity", "event_trigger", "action", "time", "state", "intent", "quantity", "other"})
_CLAIM_TYPES = frozenset({"fact", "opinion", "question", "suggestion", "hypothesis"})
_CERTAINTIES = frozenset({"asserted", "reported", "hypothetical", "negated", "unknown"})
_CONFIDENCES = frozenset({"high", "medium", "low"})
_LABEL_FIELDS = {
    "stance": frozenset({"support", "oppose", "neutral", "mixed", "unknown"}),
    "polarity": frozenset({"positive", "negative", "neutral", "mixed", "unknown"}),
    "modality": frozenset({"certain", "probable", "possible", "required", "desired", "unknown"}),
    "status": frozenset({"reported", "ongoing", "resolved", "failed", "planned", "historical", "unknown"}),
    "attribution": frozenset({"direct", "reply", "quote", "forwarded", "inferred_context"}),
}
_FORBIDDEN_FIELDS = {
    "timestamp", "raw_text", "raw_message", "sender_id", "sender_name", "chat_name",
    "media_path", "media_md5", "wxid", "wechat_id",
}

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_WECHAT_ID = re.compile(r"(?<![\w-])wxid_[A-Za-z0-9_-]+(?![\w-])", re.I)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\(?:[^\s<>:\"|?*]+\\)*[^\s<>:\"|?*]*")
_URL = re.compile(r"https?://([^/\s?#]+)[^\s]*", re.I)
_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|bearer\s+[A-Za-z0-9._-]{12,}|"
    r"(?:api[_ -]?key|cookie|password|密码|验证码)\s*[:=：]\s*\S+)"
)
_AMOUNT = re.compile(r"(?<!\d)(?:¥|￥|人民币)?\d+(?:\.\d+)?\s*(?:元|块|美元|usd)(?!\w)", re.I)
_AT_MENTION = re.compile(r"@[\w\-\u4e00-\u9fff]{1,40}")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: Tuple[str, ...]
    warnings: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass(frozen=True)
class WorkingSeedResult:
    dataset_id: str
    output_directory: str
    manifest_path: str
    file_paths: Tuple[str, ...]
    message_count: int
    workflow_state: str
    validation: ValidationResult

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "output_directory": self.output_directory,
            "manifest_path": self.manifest_path,
            "file_paths": list(self.file_paths),
            "message_count": self.message_count,
            "workflow_state": self.workflow_state,
            "validation": self.validation.to_dict(),
        }


# Compatibility aliases for callers of the first scaffold.
GoldValidationResult = ValidationResult
GoldExportResult = WorkingSeedResult


def contract_schema() -> Dict[str, Any]:
    return {
        "$id": SCHEMA_VERSION,
        "authority": "docs/gold-standard-2026-08-25-contract.md",
        "dataset_version": DATASET_VERSION,
        "private_root": PRIVATE_ROOT.as_posix(),
        "release_files": ["manifest.json"] + list(PRIVATE_JSONL_FILES),
        "record_common_fields": sorted(_COMMON_FIELDS),
        "typed_id_fields": {**_TYPED_ID_FIELDS, "event_seed": "virtual:event_seed"},
        "typed_reference_forms": [
            "{type: <anchor_type>, id: <typed_id>}",
            "<anchor_type>:<typed_id>",
            "<unique_plain_typed_id>",
        ],
        "adjudication_evidence_refs": {
            "contract_version": _ADJUDICATION_EVIDENCE_REF_CONTRACT_VERSION,
            "field": "evidence_refs",
            "item": {"type": "message|mention|claim|relation|cluster|presentation|adjudication", "id": "typed source ID"},
            "catalog_manifest_field": "adjudication_evidence_catalog",
        },
        "relation_missing_side": {
            "fields": ["missing_side", "missing_sides"],
            "statuses": ["annotator_a_status", "annotator_b_status"],
            "rule": "a missing source label must be null/omitted and explicitly marked; final label is never copied to the missing side",
        },
        "relation_observable_support_refs": {
            "contract_version": OBSERVABLE_SUPPORT_CONTRACT_VERSION,
            "field": _OBSERVABLE_SUPPORT_REF_FIELD,
            "required_when": "relation.label == 'same_event'",
            "item": {
                "type": "message|mention|claim",
                "id": "typed target ID",
                "support_code": "one supporting_slot_codes value",
                "side": "left|right|shared (optional)",
                "span": "optional {start,end} matching target evidence",
            },
            "span_required_for": ["mention", "claim"],
            "identity_rule": "same_event requires a message/claim instance signal, exact same-message edge, or explicit reply; block/segment/time alone do not qualify",
        },
        "message_fields": sorted(_MESSAGE_FIELDS),
        "relation_labels": sorted(EVENT_RELATIONS),
        "automatic_export_state": "privacy_review_required",
    }


gold_schema_v1 = contract_schema


def empty_contract_collections() -> Dict[str, List[Dict[str, Any]]]:
    return {name: [] for name in COLLECTIONS}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_token(key: str, namespace: str, value: Any) -> str:
    return hmac.new(
        key.encode("utf-8"),
        (namespace + "|" + str(value or "unknown")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_manifest() -> Dict[str, Any]:
    return {
        "timezone": BEIJING_TIMEZONE,
        "start_local": WINDOW_START_LOCAL,
        "end_local": WINDOW_END_LOCAL,
        "start_utc": WINDOW_START_UTC,
        "end_utc": WINDOW_END_UTC,
        "interval": "half_open",
    }


@contextmanager
def open_source_database_read_only(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a WAL-aware source without permitting SQL writes."""

    path = Path(database_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("source database must be a file")
    # immutable=1 is intentionally absent because it can ignore committed WAL.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("SQLite query_only could not be enabled")
        yield connection
    finally:
        connection.close()


def _columns(connection: sqlite3.Connection) -> Tuple[str, ...]:
    values = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(messages)"))
    required = {"message_id", "chat_id", "content", "timestamp"}
    missing = required - set(values)
    if missing:
        raise ValueError("messages table missing required columns: %s" % ", ".join(sorted(missing)))
    return values


def _coverage(connection: sqlite3.Connection, columns: Sequence[str]) -> Dict[str, Any]:
    where = "julianday(timestamp)>=julianday(?) AND julianday(timestamp)<julianday(?)"
    params = (WINDOW_START_UTC, WINDOW_END_UTC)
    total, chats = connection.execute(
        "SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM messages WHERE " + where, params
    ).fetchone()
    if "is_group" in columns:
        group, group_chats = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT chat_id) FROM messages WHERE " + where + " AND is_group=1", params
        ).fetchone()
        private, private_chats = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT chat_id) FROM messages WHERE " + where + " AND is_group=0", params
        ).fetchone()
    else:
        group = group_chats = private = private_chats = 0
    reply_columns = sorted(
        set(columns) & {"reply_to_message_id", "quoted_message_id", "reference_message_id"}
    )
    shortfalls = []
    if not reply_columns:
        shortfalls.append("REPLY_METADATA_ABSENT_REPLY_GOLD_PROHIBITED")
    if "is_group" not in columns:
        shortfalls.append("CHAT_TYPE_SCOPE_UNKNOWN")
    return {
        "window_message_count": int(total),
        "chat_count": int(chats),
        "group_message_count": int(group),
        "group_chat_count": int(group_chats),
        "private_message_count": int(private),
        "private_chat_count": int(private_chats),
        "unknown_scope_message_count": int(total) - int(group) - int(private),
        "reply_columns": reply_columns,
        "reply_metadata_state": "columns_present" if reply_columns else "absent",
        "raw_json_reply_audit": "confirmed_absent_by_data_audit_not_reinspected_by_tool",
        "coverage_shortfalls": shortfalls,
    }


def inspect_source_coverage_read_only(database_path: Path) -> Dict[str, Any]:
    """Run aggregate SQL only; message content is not selected."""

    with open_source_database_read_only(database_path) as connection:
        connection.execute("BEGIN")
        result = _coverage(connection, _columns(connection))
        connection.execute("COMMIT")
        return result


def _read_snapshot(database_path: Path) -> Tuple[Tuple[Dict[str, Any], ...], Dict[str, Any]]:
    required = {"message_id", "chat_id", "content", "timestamp"}
    optional = {
        "account_id", "chat_name", "sender_id", "sender_name", "is_self", "is_group",
        "message_type", "source_mode",
    }
    with open_source_database_read_only(database_path) as connection:
        connection.execute("BEGIN")
        columns = _columns(connection)
        selected = sorted(required | (set(columns) & optional))
        sql = (
            "SELECT %s FROM messages WHERE julianday(timestamp)>=julianday(?) "
            "AND julianday(timestamp)<julianday(?) ORDER BY julianday(timestamp),message_id"
        ) % ",".join('"%s"' % value for value in selected)
        rows = tuple(dict(row) for row in connection.execute(sql, (WINDOW_START_UTC, WINDOW_END_UTC)))
        coverage = _coverage(connection, columns)
        connection.execute("COMMIT")
        return rows, coverage


def read_source_messages_read_only(database_path: Path) -> Tuple[Dict[str, Any], ...]:
    rows, _ = _read_snapshot(database_path)
    return rows


def build_export_manifest_dry_run(
    database_path: Path, *, created_at: Optional[str] = None
) -> Dict[str, Any]:
    coverage = inspect_source_coverage_read_only(database_path)
    return {
        "state": "dry_run",
        "content_read": False,
        "dataset_version": DATASET_VERSION,
        "schema_version": SCHEMA_VERSION,
        "scope_local_day": LOCAL_DAY,
        "source": {"mode": "sqlite_ro", "query_only": True, "wal_aware": True, "path_included": False},
        "window": _window_manifest(),
        "coverage_counts": coverage,
        "coverage_shortfalls": list(coverage["coverage_shortfalls"]),
        "workflow_state": "privacy_review_required_after_export",
        "ready_for_frozen_release": False,
        "ready_for_reply_gold": coverage["reply_metadata_state"] != "absent",
        "created_at": str(created_at or datetime.now(timezone.utc).isoformat()),
    }


class _Redactor:
    """Mechanical pre-redaction; never a privacy approval."""

    def __init__(self, key: str, known_people: Mapping[str, str], known_chats: Mapping[str, str]):
        self.key = key
        self.known_people = dict(known_people)
        self.known_chats = dict(known_chats)
        self.tokens: Dict[Tuple[str, str], str] = {}
        self.counts: Dict[str, int] = {}

    def token(self, kind: str, raw: str, suffix: str = "") -> str:
        key = (kind, _hmac_token(self.key, kind, raw))
        if key not in self.tokens:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.tokens[key] = "[%s_%03d%s]" % (kind, self.counts[kind], suffix)
        return self.tokens[key]

    @staticmethod
    def domain_class(host: str) -> str:
        host = host.casefold()
        if host.endswith("github.com"):
            return "CODE_HOST"
        if any(value in host for value in ("linux.do", "v2ex")):
            return "FORUM"
        return "OTHER"

    def redact(self, value: Any) -> Tuple[str, Tuple[str, ...]]:
        text = str(value or "")
        kinds: List[str] = []

        def pattern(pattern: re.Pattern, kind: str, suffix: str = "") -> None:
            nonlocal text
            def replace(match: re.Match) -> str:
                kinds.append(kind)
                return self.token(kind, match.group(0), suffix)
            text = pattern.sub(replace, text)

        text = _SECRET.sub(lambda _match: kinds.append("SECRET") or "[SECRET_REDACTED]", text)
        text = _URL.sub(
            lambda match: kinds.append("URL") or self.token(
                "URL", match.group(0), ":" + self.domain_class(match.group(1))
            ), text,
        )
        pattern(_EMAIL, "EMAIL")
        pattern(_PHONE, "PHONE")
        pattern(_WECHAT_ID, "ACCOUNT")
        pattern(_WINDOWS_PATH, "FILE", ":LOCAL_PATH")
        text = _AMOUNT.sub(lambda _match: kinds.append("AMOUNT") or "[AMOUNT:BUCKET_PENDING]", text)
        text = _AT_MENTION.sub(lambda match: kinds.append("PERSON") or self.token("PERSON", match.group(0)), text)
        # Known source identities are replaced longest-first. Unknown names,
        # organizations and references remain a mandatory manual-review risk.
        replacements = [(raw, token) for raw, token in {**self.known_people, **self.known_chats}.items() if raw]
        for raw, token in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
            if raw in text:
                kinds.append("PERSON" if token.startswith("[PERSON") else "CHAT")
                text = text.replace(raw, token)
        return text, tuple(sorted(set(kinds)))


def _alias_map(values: Iterable[str], prefix: str, key: str) -> Dict[str, str]:
    unique = {str(value) for value in values if str(value)}
    ordered = sorted(unique, key=lambda value: _hmac_token(key, prefix, value))
    return {value: "%s_%03d" % (prefix, index) for index, value in enumerate(ordered, 1)}


def _time_bucket(local_time: datetime) -> str:
    hour = local_time.hour
    if hour < 6:
        return "early"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "late"


def _common(record_id: str, source_ids: Sequence[str], created_by: str = "pipeline") -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "record_id": record_id,
        "annotation_status": "draft",
        "provenance": {
            "source_record_ids": list(source_ids),
            "created_by": created_by,
            "guide_version": ANNOTATION_GUIDE_VERSION,
            "revision": 1,
        },
    }


def _message_type(value: Any) -> str:
    raw = str(value or "other").casefold()
    return {
        "text": "text", "link": "link", "image": "image", "voice": "audio",
        "audio": "audio", "file": "file", "sticker": "sticker", "system": "system",
    }.get(raw, "other")


def _seed_records(
    rows: Sequence[Mapping[str, Any]], key: str
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    timestamps = [_parse_timestamp(row.get("timestamp")) for row in rows]
    if any(value is None for value in timestamps):
        raise ValueError("source contains an invalid timestamp in the selected window")
    parsed_times = [value for value in timestamps if value is not None]
    baseline = min(parsed_times) if parsed_times else _parse_timestamp(WINDOW_START_UTC)
    chat_values = [str(row.get("chat_id") or "unknown") for row in rows]
    person_values = [
        str(row.get("sender_id") or row.get("sender_name") or "unknown") for row in rows
    ]
    account_values = [str(row.get("account_id") or "default") for row in rows]
    chat_aliases = _alias_map(chat_values, "CHAT", key)
    person_aliases = _alias_map(person_values, "PERSON", key)
    account_aliases = _alias_map(account_values, "ACCOUNT", key)
    known_people = {
        str(row.get("sender_name") or ""): "[" + person_aliases[str(row.get("sender_id") or row.get("sender_name") or "unknown")] + "]"
        for row in rows if str(row.get("sender_name") or "")
    }
    known_chats = {
        str(row.get("chat_name") or ""): "[" + chat_aliases[str(row.get("chat_id") or "unknown")] + "]"
        for row in rows if str(row.get("chat_name") or "")
    }
    redactor = _Redactor(key, known_people, known_chats)
    sequence: Dict[str, int] = {}
    output: List[Dict[str, Any]] = []
    beijing = ZoneInfo(BEIJING_TIMEZONE)
    for index, (row, timestamp) in enumerate(zip(rows, parsed_times), 1):
        raw_chat = str(row.get("chat_id") or "unknown")
        raw_person = str(row.get("sender_id") or row.get("sender_name") or "unknown")
        raw_account = str(row.get("account_id") or "default")
        sequence[raw_chat] = sequence.get(raw_chat, 0) + 1
        message_id = "MESSAGE_%06d" % index
        redacted_text, redaction_types = redactor.redact(row.get("content"))
        local_time = timestamp.astimezone(beijing)
        message_type = _message_type(row.get("message_type"))
        media_state = "none" if message_type in {"text", "link", "system"} else "placeholder"
        source_hmac = "SOURCE_" + _hmac_token(key, "message", row.get("message_id"))[:20]
        record = _common(message_id, (source_hmac,))
        record.update(
            {
                "message_id": message_id,
                "account_id": account_aliases[raw_account],
                "chat_id": chat_aliases[raw_chat],
                "chat_type": "group" if row.get("is_group") in {1, True} else "direct",
                "speaker_id": person_aliases[raw_person] if raw_person != "unknown" else "unknown",
                "direction": "outbound" if row.get("is_self") in {1, True} else "inbound",
                "message_type": message_type,
                "local_day": LOCAL_DAY,
                "time_offset_seconds": max(0, int((timestamp - baseline).total_seconds())),
                "time_bucket": _time_bucket(local_time),
                "sequence_in_chat": sequence[raw_chat] - 1,
                # Source audit confirmed no usable reply/quote metadata. Null
                # is explicit; reply gold is prohibited by the manifest.
                "reply_to_message_id": None,
                "redacted_text": redacted_text,
                "redaction_types": list(redaction_types),
                "media_state": media_state,
                "source_mode": str(row.get("source_mode") or "unknown")
                if str(row.get("source_mode") or "unknown") in {"live", "history", "recovered", "unknown"}
                else "unknown",
                "context_message_ids": [],
                "split": "development",
            }
        )
        output.append(record)
    return output, dict(sorted(redactor.counts.items()))


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(_canonical_json(record) + "\n")


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def export_private_pre_redaction_seed(
    database_path: Path,
    *,
    pseudonym_key: str,
    pseudonym_key_id: str,
    output_directory: Path = DEFAULT_WORKING_DIR,
    created_at: Optional[str] = None,
) -> WorkingSeedResult:
    """Create a contract-shaped working seed that still requires privacy review."""

    if len(str(pseudonym_key or "")) < 16:
        raise ValueError("pseudonym_key must contain at least 16 characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", str(pseudonym_key_id or "")):
        raise ValueError("pseudonym_key_id is required and must not contain key material")
    source = Path(database_path).expanduser().resolve(strict=True)
    rows, coverage = _read_snapshot(source)
    messages, redaction_counts = _seed_records(rows, pseudonym_key)
    collections = empty_contract_collections()
    collections["messages"] = messages
    destination = Path(output_directory).expanduser().resolve()
    targets = [destination / name for name in PRIVATE_JSONL_FILES] + [destination / "manifest.json"]
    if any(path.exists() for path in targets):
        raise FileExistsError("refusing to overwrite an existing private working seed")
    destination.mkdir(parents=True, exist_ok=True)
    for filename, collection in COLLECTION_BY_FILE.items():
        _write_jsonl(destination / filename, collections[collection])
    hashes = {filename: _file_sha256(destination / filename) for filename in PRIVATE_JSONL_FILES}
    counts = {filename: len(collections[collection]) for filename, collection in COLLECTION_BY_FILE.items()}
    # Fingerprint the selected logical snapshot, not the main DB file's size
    # or mtime. The latter can remain unchanged while committed rows live in
    # WAL. Only the HMAC is retained in the manifest.
    source_fingerprint = _hmac_token(
        pseudonym_key,
        "source_snapshot",
        _canonical_json(
            [
                {key: row.get(key) for key in sorted(row)}
                for row in rows
            ]
        ),
    )
    dataset_id = "wechat-2026-08-25-working-" + source_fingerprint[:12]
    manifest = {
        "dataset_id": dataset_id,
        "dataset_version": DATASET_VERSION,
        "status": "draft",
        "workflow_state": "privacy_review_required",
        "scope_local_day": LOCAL_DAY,
        "schema_version": SCHEMA_VERSION,
        "annotation_guide_version": ANNOTATION_GUIDE_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "pseudonym_key_id": pseudonym_key_id,
        "sampling_seed": None,
        "sampling_strata_and_targets": {"state": "pending_after_privacy_review"},
        "coverage_counts": coverage,
        "coverage_shortfalls": list(coverage["coverage_shortfalls"]),
        "split_policy": {"state": "pending_cluster_level_split", "default_working_split": "development"},
        "source_snapshot_fingerprint_hmac": source_fingerprint,
        "file_sha256": hashes,
        "record_counts_by_file": counts,
        "label_counts": {"relations": {}, "clusters": {}, "presentations": {}},
        "must_not_link_count": 0,
        "double_annotation_coverage": {
            "mentions": 0.0, "claims": 0.0, "relations": 0.0, "presentations": 0.0
        },
        "adjudication_count": 0,
        "privacy_scan_status": "pending_manual_review",
        "automatic_redaction_counts": redaction_counts,
        "created_at": str(created_at or datetime.now(timezone.utc).isoformat()),
        "frozen_at": None,
        "supersedes": None,
        "known_limitations": [
            "Automatic pattern redaction does not establish privacy safety.",
            "Free-form names, organizations, references, amounts and secrets require human review.",
            "Reply and quote metadata are absent; reply gold must not be created.",
        ],
        "version_lineage": {"parent_dataset_version": None, "change_type": "initial_working_seed"},
        "release_eligible": False,
        "external_sharing_allowed": False,
        "data_origin": "private_local_source",
        "window": _window_manifest(),
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validation = validate_contract_dataset({"manifest": manifest, **collections})
    if not validation.ok:
        raise ValueError("working seed failed validation: %s" % "; ".join(validation.errors))
    return WorkingSeedResult(
        dataset_id=dataset_id,
        output_directory=str(destination),
        manifest_path=str(manifest_path),
        file_paths=tuple(str(destination / name) for name in PRIVATE_JSONL_FILES),
        message_count=len(messages),
        workflow_state="privacy_review_required",
        validation=validation,
    )


def export_redacted_gold_seed(
    database_path: Path,
    *,
    redaction_salt: str,
    pseudonym_key_id: str = "LOCAL_WORKING_KEY",
    output_root: Path = DEFAULT_WORKING_DIR,
    created_at: Optional[str] = None,
) -> WorkingSeedResult:
    """Compatibility wrapper; output is explicitly not a privacy-approved release."""

    return export_private_pre_redaction_seed(
        database_path,
        pseudonym_key=redaction_salt,
        pseudonym_key_id=pseudonym_key_id,
        output_directory=output_root,
        created_at=created_at,
    )


def _walk_forbidden(value: Any, path: str = "$") -> List[str]:
    errors: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = path + "." + str(key)
            if key in _FORBIDDEN_FIELDS:
                errors.append("forbidden field %s" % child_path)
            errors.extend(_walk_forbidden(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_walk_forbidden(child, "%s[%d]" % (path, index)))
    return errors


def _privacy_findings(value: str) -> List[str]:
    return [
        label for label, pattern in (
            ("EMAIL", _EMAIL), ("PHONE", _PHONE), ("WECHAT_ID", _WECHAT_ID),
            ("WINDOWS_PATH", _WINDOWS_PATH), ("FULL_URL", _URL), ("SECRET", _SECRET),
        ) if pattern.search(value)
    ]


def _is_id(value: Any) -> bool:
    """Return whether ``value`` is a valid contract ID.

    IDs are deliberately not coerced from numbers or arbitrary objects.  A
    coercion here would make a malformed foreign key look valid and would make
    duplicate detection depend on Python's string representation.
    """

    return isinstance(value, str) and bool(value.strip())


def _required_id(
    record: Mapping[str, Any], field: str, owner: str, errors: List[str]
) -> str:
    value = record.get(field)
    if not _is_id(value):
        errors.append("%s.%s must be a non-empty string ID" % (owner, field))
        return ""
    return str(value)


def _optional_id(
    record: Mapping[str, Any], field: str, owner: str, errors: List[str]
) -> Optional[str]:
    if field not in record or record.get(field) is None:
        return None
    value = record.get(field)
    if not _is_id(value):
        errors.append("%s.%s must be null or a non-empty string ID" % (owner, field))
        return None
    return str(value)


def _required_id_list(
    record: Mapping[str, Any], field: str, owner: str, errors: List[str]
) -> List[str]:
    value = record.get(field)
    if not isinstance(value, list):
        errors.append("%s.%s must be a list of string IDs" % (owner, field))
        return []
    output: List[str] = []
    for index, item in enumerate(value):
        if not _is_id(item):
            errors.append("%s.%s[%d] must be a non-empty string ID" % (owner, field, index))
            continue
        output.append(str(item))
    if len(output) != len(set(output)):
        errors.append("%s.%s contains duplicate IDs" % (owner, field))
    return output


def _optional_id_list(
    record: Mapping[str, Any], field: str, owner: str, errors: List[str]
) -> List[str]:
    if field not in record or record.get(field) is None:
        return []
    return _required_id_list(record, field, owner, errors)


def _require_fields(
    record: Mapping[str, Any], fields: Iterable[str], owner: str, errors: List[str]
) -> None:
    missing = sorted(set(fields) - set(record))
    if missing:
        errors.append("%s missing fields: %s" % (owner, ",".join(missing)))


def _check_enum(
    record: Mapping[str, Any], field: str, allowed: Iterable[str], owner: str, errors: List[str]
) -> None:
    if record.get(field) not in set(allowed):
        errors.append("%s has invalid %s" % (owner, field))


def _check_fk(
    owner: str,
    field: str,
    values: Iterable[str],
    target_name: str,
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    errors: List[str],
) -> None:
    target = indexes.get(target_name, {})
    for value in values:
        if value not in target:
            errors.append("%s.%s references unknown %s %s" % (owner, field, target_name, value))


_REFERENCE_TYPE_ALIASES = {
    "message": "messages", "messages": "messages", "message_id": "messages",
    "mention": "mentions", "mentions": "mentions", "mention_id": "mentions",
    "claim": "claims", "claims": "claims", "claim_id": "claims",
    "relation": "relations", "relations": "relations", "relation_id": "relations",
    "cluster": "clusters", "clusters": "clusters", "cluster_id": "clusters",
    "event_seed": "event_seed", "event-seed": "event_seed", "event": "event_seed",
    "presentation": "presentations", "presentations": "presentations", "presentation_id": "presentations",
    "adjudication": "adjudications", "adjudications": "adjudications", "adjudication_id": "adjudications",
}


def _typed_reference(value: Any) -> Optional[Tuple[str, str, bool]]:
    """Parse a typed reference as ``(collection, id, was_explicit)``.

    The JSON contract historically used plain strings.  A mapping such as
    ``{"type": "claim", "id": "CLAIM_1"}`` and compact strings such as
    ``claim:CLAIM_1`` are accepted for new adjudication rows.  Unknown forms
    return ``None`` and are reported by the caller rather than guessed.
    """

    if isinstance(value, Mapping):
        type_value = (
            value.get("type")
            or value.get("record_type")
            or value.get("collection")
            or value.get("kind")
        )
        identifier = (
            value.get("id")
            or value.get("typed_id")
            or value.get("value")
        )
        if not _is_id(type_value) or not _is_id(identifier):
            return None
        target = _REFERENCE_TYPE_ALIASES.get(str(type_value).strip().casefold())
        if not target:
            return None
        return target, str(identifier), True
    if not _is_id(value):
        return None
    text = str(value).strip()
    for separator in (":", "/", "#"):
        if separator not in text:
            continue
        prefix, identifier = text.split(separator, 1)
        target = _REFERENCE_TYPE_ALIASES.get(prefix.strip().casefold())
        if target and _is_id(identifier):
            return target, str(identifier), True
    return None


def _relation_missing_sides_contract(
    record: Mapping[str, Any], owner: str, errors: List[str]
) -> set[str]:
    """Parse explicit missing-side metadata without inferring labels.

    ``missing_side``/``missing_sides`` are the canonical scalar/list forms;
    boolean and status aliases are accepted only as explicit declarations.
    Invalid marker values are errors rather than being ignored (which would
    turn an incomplete A/B pair into a silently accepted relation).
    """

    sides: set[str] = set()

    def add_value(value: Any, field: str) -> None:
        values = value if isinstance(value, list) else (value,)
        if value is None:
            return
        if not isinstance(value, (str, list)):
            errors.append("%s.%s must be a side string or list" % (owner, field))
            return
        for item in values:
            if not isinstance(item, str):
                errors.append("%s.%s contains a non-string side" % (owner, field))
                continue
            normalized = item.strip().casefold().replace("_", "-")
            if normalized in {"a", "annotator-a", "annotator-a-side", "left"}:
                sides.add("a")
            elif normalized in {"b", "annotator-b", "annotator-b-side", "right"}:
                sides.add("b")
            elif normalized not in {"", "none", "null"}:
                errors.append("%s.%s has invalid side" % (owner, field))

    for field in ("missing_side", "missing_sides", "source_missing_side", "source_missing_sides"):
        if field in record:
            add_value(record.get(field), field)
    for side, fields in {
        "a": ("missing_in_a", "annotator_a_missing", "source_a_missing", "missing_a", "a_missing"),
        "b": ("missing_in_b", "annotator_b_missing", "source_b_missing", "missing_b", "b_missing"),
    }.items():
        for field in fields:
            if field not in record:
                continue
            value = record.get(field)
            if value is True:
                sides.add(side)
            elif value not in (False, None):
                errors.append("%s.%s must be boolean" % (owner, field))
    for side in ("a", "b"):
        for field in ("annotator_%s_status" % side, "source_%s_status" % side, "annotator_%s_state" % side):
            if field not in record or record.get(field) is None:
                continue
            value = record.get(field)
            if not isinstance(value, str):
                errors.append("%s.%s must be a status string" % (owner, field))
                continue
            normalized = value.strip().casefold().replace("_", "-")
            if normalized in {"missing", "absent", "not-provided"}:
                sides.add(side)
            elif normalized not in {"present", "provided", "available"}:
                errors.append("%s.%s has invalid status" % (owner, field))
    return sides


def _resolve_reference(
    value: Any,
    *,
    generic_index: Mapping[str, Sequence[Tuple[str, Mapping[str, Any]]]],
    typed_indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve a typed or uniquely plain ID without cross-type guessing."""

    parsed = _typed_reference(value)
    if parsed is not None:
        target, identifier, _explicit = parsed
        if identifier not in typed_indexes.get(target, {}):
            return target, identifier, "unknown"
        return target, identifier, None
    if not _is_id(value):
        return None, None, "malformed"
    identifier = str(value)
    owners = list(generic_index.get(identifier, ()))
    if not owners:
        return None, identifier, "unknown"
    collections = {str(item[0]) for item in owners}
    if len(collections) != 1:
        return None, identifier, "ambiguous"
    target = next(iter(collections))
    return target, identifier, None


def _safe_int(value: Any) -> Optional[int]:
    # bool is an int subclass, but it is not a valid offset/index in the
    # contract.  Do not accept numeric strings either.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _observable_slot_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _observable_ref_type(value: Any) -> str:
    aliases = {
        "message": "message", "messages": "message", "message_id": "message",
        "mention": "mention", "mentions": "mention", "mention_id": "mention",
        "claim": "claim", "claims": "claim", "claim_id": "claim",
    }
    return aliases.get(_observable_slot_key(value), "")


def _observable_ref_slot_matches(item_code: Any, declared_code: Any) -> bool:
    item_key = _observable_slot_key(item_code)
    declared_key = _observable_slot_key(declared_code)
    if not item_key or not declared_key:
        return False
    aliases = _OBSERVABLE_REF_SLOT_ALIASES.get(declared_key)
    return item_key in aliases if aliases is not None else item_key == declared_key


def _observable_span_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    start, end = value.get("start"), value.get("end")
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
    )


def _observable_record_signal(record: Optional[Mapping[str, Any]]) -> set[str]:
    if not isinstance(record, Mapping):
        return set()
    return {
        str(record[field])
        for field in _OBSERVABLE_INSTANCE_FIELDS
        if _is_id(record.get(field))
    }


def _validate_observable_same_event(
    record: Mapping[str, Any],
    *,
    owner: str,
    claims: Mapping[str, Mapping[str, Any]],
    mentions: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    errors: List[str],
) -> None:
    """Validate typed, span-bound support and an algorithm-readable identity.

    This is deliberately data-quality validation only.  It never infers an
    event ID from text, time, block, segment or speaker similarity.
    """

    if record.get("label") != "same_event":
        return
    raw_refs = record.get(_OBSERVABLE_SUPPORT_REF_FIELD)
    if not isinstance(raw_refs, list) or not raw_refs:
        errors.append("%s same_event requires observable_support_refs" % owner)
        return
    anchor_type = str(record.get("anchor_type") or "")
    left_id, right_id = record.get("left_anchor_id"), record.get("right_anchor_id")
    anchors = claims if anchor_type == "claim" else mentions if anchor_type == "mention" else {}
    left_anchor, right_anchor = anchors.get(left_id), anchors.get(right_id)
    left_message_id = left_anchor.get("message_id") if isinstance(left_anchor, Mapping) else None
    right_message_id = right_anchor.get("message_id") if isinstance(right_anchor, Mapping) else None
    anchor_message_ids = {
        value for value in (left_message_id, right_message_id) if _is_id(value)
    }
    anchor_mention_ids: set[str] = set()
    for anchor in (left_anchor, right_anchor):
        if isinstance(anchor, Mapping):
            anchor_mention_ids.update(
                str(value)
                for value in anchor.get("event_mention_ids") or []
                if _is_id(value)
            )
    evidence_message_ids = {
        str(value)
        for value in record.get("evidence_message_ids") or []
        if _is_id(value)
    }
    parsed_refs: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_refs):
        if not isinstance(raw, Mapping):
            errors.append("%s.observable_support_refs[%d] must be a typed object" % (owner, index))
            continue
        ref_type = _observable_ref_type(
            raw.get("type") or raw.get("ref_type") or raw.get("record_type")
            or raw.get("collection") or raw.get("kind")
        )
        ref_id = raw.get("id") or raw.get("ref_id") or raw.get("typed_id") or raw.get("value")
        support_code = raw.get("support_code") or raw.get("slot_code") or raw.get("reason_code") or raw.get("reason") or raw.get("for")
        side = _observable_slot_key(raw.get("side"))
        signal = _observable_slot_key(raw.get("signal"))
        if ref_type not in _OBSERVABLE_REF_TYPES:
            errors.append("%s.observable_support_refs[%d] must use type message|mention|claim" % (owner, index))
            continue
        if not _is_id(ref_id):
            errors.append("%s.observable_support_refs[%d] must have a non-empty typed ID" % (owner, index))
            continue
        if not _is_id(support_code):
            errors.append("%s.observable_support_refs[%d] must bind a support_code" % (owner, index))
            continue
        target_map = {"message": messages, "mention": mentions, "claim": claims}[ref_type]
        target = target_map.get(str(ref_id))
        if target is None:
            errors.append("%s.observable_support_refs[%d] references unknown %s %s" % (owner, index, ref_type, ref_id))
            continue
        explicit_span = raw.get("span")
        if explicit_span is not None and not _observable_span_valid(explicit_span):
            errors.append("%s.observable_support_refs[%d] has an invalid span" % (owner, index))
            continue
        if ref_type == "mention":
            target_span = {"start": target.get("span_start"), "end": target.get("span_end")}
            if not _observable_span_valid(target_span):
                errors.append("%s.observable_support_refs[%d] mention lacks a concrete span" % (owner, index))
                continue
            if explicit_span is not None and dict(explicit_span) != target_span:
                errors.append("%s.observable_support_refs[%d] mention span does not match target" % (owner, index))
                continue
        elif ref_type == "claim":
            target_spans = target.get("evidence_spans")
            if not isinstance(target_spans, list) or not any(_observable_span_valid(item) for item in target_spans):
                errors.append("%s.observable_support_refs[%d] claim lacks a concrete evidence span" % (owner, index))
                continue
            if explicit_span is not None and not any(dict(item) == dict(explicit_span) for item in target_spans if _observable_span_valid(item)):
                errors.append("%s.observable_support_refs[%d] claim span does not match evidence" % (owner, index))
                continue
        elif explicit_span is not None:
            text = target.get("redacted_text")
            if not isinstance(text, str) or explicit_span.get("end") > len(text):
                errors.append("%s.observable_support_refs[%d] message span is outside message" % (owner, index))
                continue
        parsed_refs.append({
            "type": ref_type, "id": str(ref_id), "support_code": str(support_code),
            "side": side, "signal": signal, "target": target,
        })

    if not parsed_refs:
        return
    support_codes = record.get("supporting_slot_codes")
    if not isinstance(support_codes, list) or not support_codes:
        errors.append("%s same_event requires supporting_slot_codes" % owner)
    else:
        for support_code in support_codes:
            if not any(_observable_ref_slot_matches(item["support_code"], support_code) for item in parsed_refs):
                errors.append("%s support code %s is not bound to observable_support_refs" % (owner, support_code))

    for item in parsed_refs:
        ref_type, ref_id, side = item["type"], item["id"], item["side"]
        if side not in {"", "left", "right", "shared"}:
            errors.append("%s observable_support_refs has invalid side" % owner)
            continue
        if ref_type == "claim":
            allowed = {str(left_id), str(right_id)}
            if side == "left":
                allowed = {str(left_id)}
            elif side == "right":
                allowed = {str(right_id)}
            if ref_id not in allowed:
                errors.append("%s observable claim ref is outside relation anchors" % owner)
        elif ref_type == "mention":
            allowed = anchor_mention_ids
            if side == "left" and isinstance(left_anchor, Mapping):
                allowed = {str(value) for value in left_anchor.get("event_mention_ids") or []}
            elif side == "right" and isinstance(right_anchor, Mapping):
                allowed = {str(value) for value in right_anchor.get("event_mention_ids") or []}
            if ref_id not in allowed:
                errors.append("%s observable mention ref is outside relation anchors" % owner)
        elif ref_id not in evidence_message_ids | anchor_message_ids:
            errors.append("%s observable message ref is outside relation evidence" % owner)

    # Exact same-message/reply links and an explicitly repeated instance key
    # are stable input signals.  Shared action/core-object mentions alone are
    # deliberately insufficient.
    stable_instance = bool(left_message_id and left_message_id == right_message_id)
    stable_instance = stable_instance or bool(
        _observable_record_signal(left_anchor) & _observable_record_signal(right_anchor)
    )
    left_message = messages.get(str(left_message_id)) if _is_id(left_message_id) else None
    right_message = messages.get(str(right_message_id)) if _is_id(right_message_id) else None
    if isinstance(left_message, Mapping) and isinstance(right_message, Mapping):
        stable_instance = stable_instance or left_message.get("reply_to_message_id") == right_message_id
        stable_instance = stable_instance or right_message.get("reply_to_message_id") == left_message_id
        stable_instance = stable_instance or bool(
            _observable_record_signal(left_message) & _observable_record_signal(right_message)
        )
    for item in parsed_refs:
        code = _observable_slot_key(item["support_code"])
        signal = item["signal"]
        if code not in _OBSERVABLE_INSTANCE_SLOT_CODES and signal not in _OBSERVABLE_INSTANCE_SLOT_CODES:
            continue
        target = item["target"]
        stable_instance = stable_instance or bool(_observable_record_signal(target))
        if item["type"] == "mention":
            attrs = target.get("attributes") or {}
            stable_instance = stable_instance or any(
                _is_id(attrs.get(field)) for field in _OBSERVABLE_INSTANCE_FIELDS
            )
    if not stable_instance:
        errors.append("%s same_event lacks algorithm-readable observable instance signal" % owner)


def validate_contract_dataset(dataset: Mapping[str, Any]) -> ValidationResult:
    """Validate a contract dataset with a fail-closed typed graph gate.

    This validator is intentionally independent of the production semantic
    path.  It indexes every domain ID without coercion, then checks every
    contract foreign key against the matching typed index.  In particular, a
    duplicate ID is never hidden by a dictionary overwrite.
    """

    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(dataset, Mapping):
        return ValidationResult(False, ("dataset must be an object",), ())
    manifest = dataset.get("manifest")
    if not isinstance(manifest, Mapping):
        return ValidationResult(False, ("manifest must be an object",), ())

    missing_manifest = sorted(_REQUIRED_MANIFEST_FIELDS - set(manifest))
    if missing_manifest:
        errors.append("manifest missing fields: %s" % ",".join(missing_manifest))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("manifest schema_version must equal %s" % SCHEMA_VERSION)
    if manifest.get("dataset_version") != DATASET_VERSION:
        errors.append("unexpected dataset_version")
    if manifest.get("scope_local_day") != LOCAL_DAY:
        errors.append("scope_local_day must equal %s" % LOCAL_DAY)
    if not _is_id(manifest.get("pseudonym_key_id")):
        errors.append("pseudonym_key_id is required")
    if manifest.get("workflow_state") == "privacy_review_required":
        if manifest.get("status") == "frozen" or manifest.get("release_eligible") is True:
            errors.append("privacy_review_required seed cannot be frozen or release eligible")
        if manifest.get("external_sharing_allowed") is not False:
            errors.append("privacy_review_required seed must prohibit external sharing")
    if manifest.get("status") == "frozen":
        if manifest.get("privacy_scan_status") != "passed_zero_unresolved_high_risk":
            errors.append("frozen release requires a passed privacy scan")
        if manifest.get("release_eligible") is not True or not _is_id(manifest.get("frozen_at")):
            errors.append("frozen release requires release_eligible=true and frozen_at")
        coverage = manifest.get("double_annotation_coverage")
        if not isinstance(coverage, Mapping):
            errors.append("frozen release requires double_annotation_coverage object")
        else:
            for key in ("mentions", "claims", "relations", "presentations"):
                try:
                    complete = float(coverage.get(key, 0)) >= 1.0
                except (TypeError, ValueError):
                    complete = False
                if not complete:
                    errors.append("frozen release requires 100% double annotation coverage")
                    break

    # Scan the complete JSON-like payload for fields that must never be part
    # of the release.  The scan is structural and does not print field values.
    errors.extend(_walk_forbidden({key: value for key, value in dataset.items() if key != "manifest"}))

    collections: Dict[str, List[Mapping[str, Any]]] = {}
    indexes: Dict[str, Dict[str, Mapping[str, Any]]] = {
        collection: {} for collection in COLLECTIONS
    }
    all_typed_ids: Dict[str, List[Tuple[str, str]]] = {}
    for name in COLLECTIONS:
        value = dataset.get(name)
        if not isinstance(value, list):
            errors.append("%s must be an array" % name)
            collections[name] = []
            continue
        # Keep malformed entries out of indexes, but report them below rather
        # than throwing while validating the remaining rows.
        collections[name] = [item for item in value if isinstance(item, Mapping)]
        if len(collections[name]) != len(value):
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    errors.append("%s[%d] must be an object" % (name, index))
        typed_field = _TYPED_ID_FIELDS[name]
        seen_typed: Dict[str, int] = {}
        seen_record: Dict[str, int] = {}
        all_typed_ids[name] = []
        for index, record in enumerate(value):
            if not isinstance(record, Mapping):
                continue
            owner = "%s[%d]" % (name, index)
            missing = _COMMON_FIELDS - set(record)
            if missing:
                errors.append("%s missing common fields: %s" % (owner, ",".join(sorted(missing))))
            typed_value = record.get(typed_field)
            typed_id = str(typed_value) if _is_id(typed_value) else ""
            if not typed_id:
                errors.append("%s missing a non-empty %s" % (owner, typed_field))
            elif typed_id in seen_typed:
                errors.append("duplicate %s ID %s" % (typed_field[:-3] if typed_field.endswith("_id") else name, typed_id))
            else:
                seen_typed[typed_id] = index
                indexes[name][typed_id] = record
                all_typed_ids[name].append((typed_id, owner))
            record_value = record.get("record_id")
            record_id = str(record_value) if _is_id(record_value) else ""
            if not record_id:
                errors.append("%s record_id is missing or invalid" % owner)
            elif record_id in seen_record:
                errors.append("%s record_id duplicated: %s" % (name, record_id))
            else:
                seen_record[record_id] = index
            if record.get("schema_version") != SCHEMA_VERSION or record.get("dataset_version") != DATASET_VERSION:
                errors.append("%s %s has wrong schema/dataset version" % (name, typed_id or record_id or index))
            if not isinstance(record.get("provenance"), Mapping):
                errors.append("%s provenance must be an object" % owner)

    # A bare ID is never allowed to identify two semantic domains.  Typed
    # foreign keys know their target collection, but adjudication evidence and
    # legacy audit payloads often carry plain strings; accepting a collision
    # would make those rows impossible to resolve deterministically.
    typed_id_owners: Dict[str, List[str]] = {}
    for collection_name, values in all_typed_ids.items():
        for typed_id, _owner in values:
            typed_id_owners.setdefault(typed_id, []).append(collection_name)
    for typed_id, owners in sorted(typed_id_owners.items()):
        unique_owners = sorted(set(owners))
        if len(unique_owners) > 1:
            errors.append(
                "cross-type duplicate typed ID %s: %s"
                % (typed_id, ",".join(unique_owners))
            )

    # Event-seed is a contract anchor kind but has no standalone JSONL file.
    # Accept explicit event seed IDs and event identity fields emitted by
    # cluster builders; an arbitrary unknown string is still rejected.  The
    # cluster ID alias is retained for v1 compatibility, but it is indexed as
    # a separate type so a plain adjudication evidence ID becomes ambiguous
    # instead of silently resolving to whichever domain was indexed last.
    event_seed_ids: Dict[str, Mapping[str, Any]] = {}
    event_seed_owners: Dict[str, List[str]] = {}
    for collection_name in COLLECTIONS:
        for candidate_index, candidate in enumerate(collections[collection_name]):
            candidate_owner = "%s[%d]" % (collection_name, candidate_index)
            for field in _EVENT_SEED_FIELDS:
                value = candidate.get(field)
                if _is_id(value):
                    typed_id = str(value)
                    if typed_id in event_seed_ids and event_seed_ids[typed_id] is not candidate:
                        errors.append("duplicate event_seed ID %s" % typed_id)
                    else:
                        event_seed_ids[typed_id] = candidate
                    event_seed_owners.setdefault(typed_id, []).append(candidate_owner)
            if (
                collection_name == "clusters"
                and candidate.get("cluster_type") == "event"
                and _is_id(candidate.get("cluster_id"))
            ):
                typed_id = str(candidate["cluster_id"])
                event_seed_ids.setdefault(typed_id, candidate)
                event_seed_owners.setdefault(typed_id, []).append(candidate_owner + ":cluster_alias")
    # Explicit event-seed fields participate in the same global typed-ID
    # namespace as the persisted collections.  A cluster's compatibility
    # ``cluster_id`` alias is intentionally exempt when it points at that same
    # row; otherwise every legacy event cluster would look like a collision
    # with itself.  A seed reused by another domain (or another cluster) is a
    # real cross-type ambiguity and must fail closed.
    for event_seed_id, event_seed_record in event_seed_ids.items():
        for collection_name, lookup in indexes.items():
            other_record = lookup.get(event_seed_id)
            if other_record is None:
                continue
            if collection_name == "clusters" and other_record is event_seed_record:
                continue
            errors.append(
                "cross-type duplicate typed ID %s: event_seed,%s"
                % (event_seed_id, collection_name)
            )
    typed_indexes: Dict[str, Dict[str, Mapping[str, Any]]] = dict(indexes)
    typed_indexes["event_seed"] = event_seed_ids
    generic_index: Dict[str, List[Tuple[str, Mapping[str, Any]]]] = {}
    for collection, lookup in indexes.items():
        for identifier, record in lookup.items():
            generic_index.setdefault(identifier, []).append((collection, record))
    for identifier, record in event_seed_ids.items():
        generic_index.setdefault(identifier, []).append(("event_seed", record))

    messages = indexes["messages"]
    mentions = indexes["mentions"]
    claims = indexes["claims"]
    relations = indexes["relations"]
    clusters = indexes["clusters"]
    presentations = indexes["presentations"]
    adjudications = indexes["adjudications"]

    # Messages are the root evidence records.  Their self-contained metadata
    # is checked here before any child record dereferences it.
    for index, record in enumerate(collections["messages"]):
        message_id = str(record.get("message_id")) if _is_id(record.get("message_id")) else ("%d" % index)
        owner = "message %s" % message_id
        _require_fields(record, _MESSAGE_FIELDS, owner, errors)
        if record.get("local_day") != LOCAL_DAY:
            errors.append("%s has wrong local_day" % owner)
        offset = _safe_int(record.get("time_offset_seconds"))
        sequence = _safe_int(record.get("sequence_in_chat"))
        if offset is None or offset < 0:
            errors.append("%s has invalid time_offset_seconds" % owner)
        if sequence is None or sequence < 0:
            errors.append("%s has invalid sequence_in_chat" % owner)
        if record.get("time_bucket") not in {"early", "morning", "afternoon", "evening", "late"}:
            errors.append("%s has invalid time_bucket" % owner)
        if record.get("split") not in {"development", "frozen_test"}:
            errors.append("%s has invalid split" % owner)
        if not isinstance(record.get("redacted_text"), str):
            errors.append("%s redacted_text must be a string" % owner)
        reply_id = _optional_id(record, "reply_to_message_id", owner, errors)
        if reply_id:
            _check_fk(owner, "reply_to_message_id", (reply_id,), "messages", typed_indexes, errors)
        context_ids = _required_id_list(record, "context_message_ids", owner, errors)
        _check_fk(owner, "context_message_ids", context_ids, "messages", typed_indexes, errors)
        if isinstance(record.get("redacted_text"), str):
            findings = _privacy_findings(record["redacted_text"])
            if findings:
                errors.append("%s has unresolved high-risk patterns: %s" % (owner, ",".join(findings)))

    # Mentions and claims reference typed messages/mentions.  Numeric IDs and
    # malformed lists are rejected rather than converted to strings.
    for index, record in enumerate(collections["mentions"]):
        mention_id = str(record.get("mention_id")) if _is_id(record.get("mention_id")) else ("%d" % index)
        owner = "mention %s" % mention_id
        _require_fields(record, _MENTION_FIELDS, owner, errors)
        message_id = _required_id(record, "message_id", owner, errors)
        _check_fk(owner, "message_id", (message_id,) if message_id else (), "messages", typed_indexes, errors)
        _check_enum(record, "mention_type", _MENTION_TYPES, owner, errors)
        start, end = _safe_int(record.get("span_start")), _safe_int(record.get("span_end"))
        if start is None or end is None or start < 0 or end <= start:
            errors.append("%s has invalid span" % owner)
        if not isinstance(record.get("surface_redacted"), str):
            errors.append("%s surface_redacted must be a string" % owner)
        if record.get("normalized_id") is not None and not _is_id(record.get("normalized_id")):
            errors.append("%s normalized_id must be null or a non-empty string ID" % owner)
        if record.get("normalized_type") is not None and not _is_id(record.get("normalized_type")):
            errors.append("%s normalized_type must be null or a non-empty string" % owner)
        if not isinstance(record.get("attributes"), Mapping):
            errors.append("%s attributes must be an object" % owner)
        _check_enum(record, "certainty", _CERTAINTIES, owner, errors)
        message = messages.get(message_id)
        if message is not None and start is not None and end is not None and isinstance(record.get("surface_redacted"), str):
            text = message.get("redacted_text")
            if not isinstance(text, str) or end > len(text) or text[start:end] != record.get("surface_redacted"):
                errors.append("%s span does not match redacted_text" % owner)

    for index, record in enumerate(collections["claims"]):
        claim_id = str(record.get("claim_id")) if _is_id(record.get("claim_id")) else ("%d" % index)
        owner = "claim %s" % claim_id
        _require_fields(record, _CLAIM_FIELDS, owner, errors)
        message_id = _required_id(record, "message_id", owner, errors)
        _check_fk(owner, "message_id", (message_id,) if message_id else (), "messages", typed_indexes, errors)
        timestamp_id = _required_id(record, "timestamp_message_id", owner, errors)
        _check_fk(owner, "timestamp_message_id", (timestamp_id,) if timestamp_id else (), "messages", typed_indexes, errors)
        if message_id and timestamp_id and timestamp_id != message_id:
            errors.append("%s timestamp_message_id must reference its source message" % owner)
        context_ids = _required_id_list(record, "context_message_ids", owner, errors)
        _check_fk(owner, "context_message_ids", context_ids, "messages", typed_indexes, errors)
        mention_ids = _required_id_list(record, "event_mention_ids", owner, errors)
        _check_fk(owner, "event_mention_ids", mention_ids, "mentions", typed_indexes, errors)
        for mention_id in mention_ids:
            mention = mentions.get(mention_id)
            if mention is not None and message_id and mention.get("message_id") != message_id:
                errors.append("%s event_mention_ids references mention from another message" % owner)
        target_ids = _required_id_list(record, "target_entity_ids", owner, errors)
        del target_ids  # entity IDs are an external vocabulary, not a collection FK
        spans = record.get("evidence_spans")
        if not isinstance(spans, list) or not spans:
            errors.append("%s has no evidence_spans" % owner)
            spans = []
        source_message = messages.get(message_id)
        source_text = source_message.get("redacted_text") if source_message else None
        for span_index, span in enumerate(spans):
            if not isinstance(span, Mapping):
                errors.append("%s evidence_spans[%d] must be an object" % (owner, span_index))
                continue
            start, end = _safe_int(span.get("start")), _safe_int(span.get("end"))
            if start is None or end is None or start < 0 or end <= start:
                errors.append("%s evidence_spans[%d] has invalid span" % (owner, span_index))
            elif isinstance(source_text, str) and end > len(source_text):
                errors.append("%s evidence_spans[%d] is outside message" % (owner, span_index))
        if not _is_id(record.get("speaker_id")) and record.get("speaker_id") != "unknown":
            errors.append("%s has no speaker_id/unknown" % owner)
        if not isinstance(record.get("claim_text_redacted"), str):
            errors.append("%s claim_text_redacted must be a string" % owner)
        for field, allowed in _LABEL_FIELDS.items():
            _check_enum(record, field, allowed, owner, errors)

    seen_pairs: set = set()
    mnl_pairs: set = set()
    actual_label_counts: Dict[str, int] = {}
    for index, record in enumerate(collections["relations"]):
        relation_id = str(record.get("relation_id")) if _is_id(record.get("relation_id")) else ("%d" % index)
        owner = "relation %s" % relation_id
        _require_fields(record, _RELATION_FIELDS, owner, errors)
        anchor_type = record.get("anchor_type")
        if anchor_type not in _ANCHOR_TYPES:
            errors.append("%s has invalid anchor_type" % owner)
            anchor_type = ""
        left = _required_id(record, "left_anchor_id", owner, errors)
        right = _required_id(record, "right_anchor_id", owner, errors)
        if left and right and left >= right:
            errors.append("%s anchors are not in canonical order" % owner)
        target_name = _ANCHOR_TARGET_COLLECTIONS.get(str(anchor_type), "") if anchor_type else ""
        if target_name:
            _check_fk(owner, "left_anchor_id", (left,) if left else (), target_name, typed_indexes, errors)
            _check_fk(owner, "right_anchor_id", (right,) if right else (), target_name, typed_indexes, errors)
        if left and right:
            pair = (str(anchor_type) if anchor_type else "", left, right)
            if pair in seen_pairs:
                errors.append("duplicate relation pair %s" % (pair,))
            seen_pairs.add(pair)
        label = record.get("label")
        if label not in EVENT_RELATIONS:
            errors.append("%s has invalid label" % owner)
        else:
            actual_label_counts[str(label)] = actual_label_counts.get(str(label), 0) + 1
        for field in ("supporting_slot_codes", "conflicting_slot_codes", "evidence_message_ids", "must_not_link_reason_codes"):
            values = _required_id_list(record, field, owner, errors)
            if field == "evidence_message_ids":
                _check_fk(owner, field, values, "messages", typed_indexes, errors)
        if not isinstance(record.get("must_not_link"), bool):
            errors.append("%s must_not_link must be boolean" % owner)
        _check_enum(record, "confidence", _CONFIDENCES, owner, errors)
        missing_sides = _relation_missing_sides_contract(record, owner, errors)
        for side in ("a", "b"):
            field = "annotator_%s_label" % side
            value = record.get(field)
            if side in missing_sides:
                # Explicitly missing source labels remain null/omitted.  Do
                # not substitute the final label or the other annotator's
                # value: that would manufacture evidence for a side that did
                # not produce a record.
                if value is not None:
                    errors.append("%s %s must be null/omitted when side is missing" % (owner, field))
            elif not _is_id(value) or value not in EVENT_RELATIONS:
                errors.append("%s %s is required and must be a relation label" % (owner, field))
        adjudication_id = _optional_id(record, "adjudication_id", owner, errors)
        if adjudication_id:
            _check_fk(owner, "adjudication_id", (adjudication_id,), "adjudications", typed_indexes, errors)
        if missing_sides and not adjudication_id:
            errors.append("%s explicit missing-side relation requires adjudication_id" % owner)
        if record.get("must_not_link") is True:
            if target_name and left and right:
                mnl_pairs.add((str(anchor_type), left, right))
            if label == "same_event":
                if not _is_id(record.get("override_reason")):
                    errors.append("%s is same_event and unoverridden MNL" % owner)
                if not adjudication_id:
                    errors.append("%s same_event MNL override requires adjudication_id" % owner)
                evidence = record.get("evidence_message_ids")
                if not isinstance(evidence, list) or not evidence:
                    errors.append("%s same_event MNL override requires evidence_message_ids" % owner)
        _validate_observable_same_event(
            record,
            owner=owner,
            claims=claims,
            mentions=mentions,
            messages=messages,
            errors=errors,
        )

    for index, record in enumerate(collections["clusters"]):
        cluster_id = str(record.get("cluster_id")) if _is_id(record.get("cluster_id")) else ("%d" % index)
        owner = "cluster %s" % cluster_id
        _require_fields(record, _CLUSTER_FIELDS, owner, errors)
        _check_enum(record, "cluster_type", _CLUSTER_TYPES, owner, errors)
        claim_ids = _required_id_list(record, "claim_ids", owner, errors)
        mention_ids = _required_id_list(record, "mention_ids", owner, errors)
        message_ids = _required_id_list(record, "member_message_ids", owner, errors)
        relation_ids = _required_id_list(record, "relation_ids", owner, errors)
        _check_fk(owner, "claim_ids", claim_ids, "claims", typed_indexes, errors)
        _check_fk(owner, "mention_ids", mention_ids, "mentions", typed_indexes, errors)
        _check_fk(owner, "member_message_ids", message_ids, "messages", typed_indexes, errors)
        _check_fk(owner, "relation_ids", relation_ids, "relations", typed_indexes, errors)
        for field in ("start_message_id", "end_message_id"):
            value = _required_id(record, field, owner, errors)
            _check_fk(owner, field, (value,) if value else (), "messages", typed_indexes, errors)
        for field in ("core_entity_ids", "action_types", "intent_types", "state_sequence", "topic_family_ids", "uncertainties"):
            _required_id_list(record, field, owner, errors)
        if not isinstance(record.get("must_not_link_checked"), bool):
            errors.append("%s must_not_link_checked must be boolean" % owner)
        if not isinstance(record.get("summary_of_boundary_redacted"), str):
            errors.append("%s summary_of_boundary_redacted must be a string" % owner)
        if not record.get("must_not_link_checked"):
            if manifest.get("status") == "frozen":
                errors.append("frozen %s has not completed MNL review" % owner)
            else:
                warnings.append("%s has not completed MNL review" % owner)
        for left_index, left in enumerate(claim_ids):
            for right in claim_ids[left_index + 1 :]:
                canonical = ("claim", min(left, right), max(left, right))
                if canonical in mnl_pairs:
                    errors.append("%s contains an unoverridden MNL pair" % owner)

    for index, record in enumerate(collections["presentations"]):
        presentation_id = str(record.get("presentation_id")) if _is_id(record.get("presentation_id")) else ("%d" % index)
        owner = "presentation %s" % presentation_id
        _require_fields(record, _PRESENTATION_FIELDS, owner, errors)
        _check_enum(record, "presentation_type", _PRESENTATION_TYPES, owner, errors)
        source_clusters = _required_id_list(record, "source_cluster_ids", owner, errors)
        source_claims = _required_id_list(record, "source_claim_ids", owner, errors)
        _check_fk(owner, "source_cluster_ids", source_clusters, "clusters", typed_indexes, errors)
        _check_fk(owner, "source_claim_ids", source_claims, "claims", typed_indexes, errors)
        for field in ("fact_claim_ids", "opinion_claim_ids", "question_claim_ids"):
            values = _required_id_list(record, field, owner, errors)
            _check_fk(owner, field, values, "claims", typed_indexes, errors)
        separate_ids = _required_id_list(record, "must_remain_separate_from", owner, errors)
        _check_fk(owner, "must_remain_separate_from", separate_ids, "presentations", typed_indexes, errors)
        title = record.get("title_redacted")
        if title is not None and not isinstance(title, str):
            errors.append("%s title_redacted must be null or a string" % owner)
        sentences = record.get("sentence_units")
        if not isinstance(sentences, list):
            errors.append("%s sentence_units must be a list" % owner)
            sentences = []
        visible = record.get("presentation_type") != "do_not_display"
        for sentence_index, sentence in enumerate(sentences):
            sentence_owner = "%s sentence[%d]" % (owner, sentence_index)
            if not isinstance(sentence, Mapping):
                errors.append("%s must be an object" % sentence_owner)
                continue
            if not isinstance(sentence.get("text_redacted"), str):
                errors.append("%s text_redacted must be a string" % sentence_owner)
            sentence_claims = _required_id_list(sentence, "claim_ids", sentence_owner, errors)
            sentence_messages = _required_id_list(sentence, "message_ids", sentence_owner, errors)
            _check_fk(sentence_owner, "claim_ids", sentence_claims, "claims", typed_indexes, errors)
            _check_fk(sentence_owner, "message_ids", sentence_messages, "messages", typed_indexes, errors)
            if visible and (not sentence_claims or not sentence_messages):
                errors.append("%s has an unsupported sentence" % owner)
            if not set(sentence_claims).issubset(set(source_claims)):
                errors.append("%s sentence claim evidence is outside its sources" % owner)
            if source_claims and not set(sentence_claims).issubset(set(source_claims)):
                errors.append("%s sentence evidence is outside its sources" % owner)

    # Adjudication evidence is versioned separately from graph foreign keys.
    # v4 carries explicit typed refs and a manifest catalog of the A/B source
    # IDs, because alternate annotation rows are intentionally not materialized
    # in the merged graph.  The catalog is bounded and validated; an arbitrary
    # ``{"type": ..., "id": ...}`` is never accepted as an escape hatch.
    evidence_catalog_raw = manifest.get("adjudication_evidence_catalog")
    evidence_catalog: Dict[str, set[str]] = {}
    is_v4_artifact = (
        manifest.get("artifact_version") == "pre_release_v4"
        or str(manifest.get("dataset_id") or "").endswith("-v4")
    )
    if is_v4_artifact and evidence_catalog_raw is None:
        errors.append("pre_release_v4 requires adjudication_evidence_catalog")
    if evidence_catalog_raw is not None:
        if not isinstance(evidence_catalog_raw, Mapping):
            errors.append("manifest adjudication_evidence_catalog must be an object")
        else:
            allowed_catalog_types = set(_REFERENCE_TYPE_ALIASES.values()) | {"event_seed"}
            for catalog_type, catalog_values in evidence_catalog_raw.items():
                catalog_name = _REFERENCE_TYPE_ALIASES.get(str(catalog_type).casefold(), str(catalog_type))
                if catalog_name not in allowed_catalog_types:
                    errors.append("manifest adjudication_evidence_catalog has unknown type %s" % catalog_type)
                    continue
                if not isinstance(catalog_values, list) or any(not _is_id(value) for value in catalog_values):
                    errors.append("manifest adjudication_evidence_catalog.%s must be a list of string IDs" % catalog_type)
                    continue
                if len(catalog_values) != len(set(catalog_values)):
                    errors.append("manifest adjudication_evidence_catalog.%s contains duplicate IDs" % catalog_type)
                evidence_catalog[catalog_name] = set(catalog_values)
            catalog_hash = manifest.get("adjudication_evidence_catalog_sha256")
            if catalog_hash is not None:
                if not _is_id(catalog_hash):
                    errors.append("manifest adjudication_evidence_catalog_sha256 must be a string")
                elif _sha256_bytes(_canonical_json(evidence_catalog_raw).encode("utf-8")) != catalog_hash:
                    errors.append("manifest adjudication_evidence_catalog_sha256 does not match catalog")

    # Adjudication rows may retain the legacy ``evidence_ids`` representation
    # in older artifacts.  New v4 rows must use explicit typed mappings and,
    # when they point outside the merged graph, resolve through the catalog.
    seen_disagreements: set = set()
    for index, record in enumerate(collections["adjudications"]):
        adjudication_id = str(record.get("adjudication_id")) if _is_id(record.get("adjudication_id")) else ("%d" % index)
        owner = "adjudication %s" % adjudication_id
        disagreement = record.get("disagreement_id")
        if disagreement is not None:
            if not _is_id(disagreement):
                errors.append("%s disagreement_id must be a non-empty string ID" % owner)
            elif disagreement in seen_disagreements:
                errors.append("duplicate disagreement_id %s" % disagreement)
            else:
                seen_disagreements.add(str(disagreement))
        record_type = record.get("record_type")
        anchor = record.get("anchor_id")
        if anchor is not None:
            parsed_anchor = _typed_reference(anchor)
            if parsed_anchor is not None:
                anchor_collection, anchor_id, anchor_explicit = parsed_anchor
            elif _is_id(anchor):
                anchor_explicit = False
                anchor_collection, anchor_id, anchor_error = _resolve_reference(
                    anchor, generic_index=generic_index, typed_indexes=typed_indexes
                )
                if anchor_error == "ambiguous":
                    errors.append("%s.anchor_id is ambiguous across typed IDs %s" % (owner, anchor))
                elif anchor_error == "unknown":
                    errors.append("%s.anchor_id references unknown typed ID %s" % (owner, anchor))
                elif anchor_error == "malformed":
                    errors.append("%s.anchor_id must be a typed or plain string ID" % owner)
            else:
                anchor_collection, anchor_id = None, None
                errors.append("%s.anchor_id must be a typed or plain string ID" % owner)
            expected_collection = _REFERENCE_TYPE_ALIASES.get(str(record_type or "").casefold())
            if expected_collection and anchor_collection and expected_collection != anchor_collection:
                errors.append("%s.anchor_id type does not match record_type" % owner)
            if anchor_collection and anchor_id:
                # A/B annotation rows may remain valid adjudication anchors
                # even though only the adjudicated row is materialized in the
                # merged graph.  v4 permits that case only for an explicit
                # typed anchor whose ID is in the bounded evidence catalog;
                # bare unknown IDs remain a hard error.
                external_typed_anchor = (
                    is_v4_artifact
                    and anchor_explicit
                    and anchor_id in evidence_catalog.get(anchor_collection, set())
                )
                if not external_typed_anchor:
                    _check_fk(owner, "anchor_id", (anchor_id,), anchor_collection, typed_indexes, errors)
            elif anchor_id and anchor_collection is None:
                errors.append("%s.anchor_id references unknown typed ID %s" % (owner, anchor_id))
        if "evidence_refs" in record:
            raw_evidence_refs = record.get("evidence_refs")
            if not isinstance(raw_evidence_refs, list):
                errors.append("%s.evidence_refs must be a list of explicit typed refs" % owner)
                raw_evidence_refs = []
            seen_evidence_refs: set[Tuple[str, str]] = set()
            for evidence_index, evidence_value in enumerate(raw_evidence_refs):
                if not isinstance(evidence_value, Mapping):
                    errors.append("%s.evidence_refs[%d] must be an explicit typed object" % (owner, evidence_index))
                    continue
                parsed = _typed_reference(evidence_value)
                if parsed is None:
                    errors.append("%s.evidence_refs[%d] must contain a known type and string id" % (owner, evidence_index))
                    continue
                target_collection, evidence_id, _explicit = parsed
                key = (target_collection, evidence_id)
                if key in seen_evidence_refs:
                    errors.append("%s.evidence_refs contains duplicate typed refs" % owner)
                seen_evidence_refs.add(key)
                if evidence_id not in typed_indexes.get(target_collection, {}):
                    if evidence_id not in evidence_catalog.get(target_collection, set()):
                        errors.append(
                            "%s.evidence_refs[%d] references unknown %s %s"
                            % (owner, evidence_index, target_collection, evidence_id)
                        )
        if "evidence_refs" in record and "evidence_ids" in record:
            errors.append("%s must not mix evidence_refs and evidence_ids" % owner)
        if is_v4_artifact and "evidence_refs" not in record:
            errors.append("%s pre_release_v4 requires evidence_refs" % owner)
        if "evidence_ids" in record:
            raw_evidence_ids = record.get("evidence_ids")
            if not isinstance(raw_evidence_ids, list):
                errors.append("%s.evidence_ids must be a list of typed or plain IDs" % owner)
                raw_evidence_ids = []
            seen_evidence: set = set()
            for evidence_index, evidence_value in enumerate(raw_evidence_ids):
                target_collection, evidence_id, evidence_error = _resolve_reference(
                    evidence_value,
                    generic_index=generic_index,
                    typed_indexes=typed_indexes,
                )
                if evidence_id and evidence_id in seen_evidence:
                    errors.append("%s.evidence_ids contains duplicate IDs" % owner)
                if evidence_id:
                    seen_evidence.add(evidence_id)
                if evidence_error == "ambiguous":
                    errors.append(
                        "%s.evidence_ids[%d] is ambiguous across typed IDs %s"
                        % (owner, evidence_index, evidence_id or evidence_value)
                    )
                elif evidence_error == "unknown":
                    errors.append(
                        "%s.evidence_ids[%d] references unknown typed ID %s"
                        % (owner, evidence_index, evidence_id or evidence_value)
                    )
                elif evidence_error == "malformed":
                    errors.append(
                        "%s.evidence_ids[%d] must be a typed or plain string ID"
                        % (owner, evidence_index)
                    )

    coverage_counts = manifest.get("coverage_counts")
    if not isinstance(coverage_counts, Mapping):
        errors.append("manifest coverage_counts must be an object")
        coverage_counts = {}
    coverage_shortfalls = manifest.get("coverage_shortfalls")
    if not isinstance(coverage_shortfalls, list):
        errors.append("manifest coverage_shortfalls must be an array")
        coverage_shortfalls = []
    if coverage_counts.get("reply_metadata_state") == "absent" and not any(
        "REPLY_METADATA_ABSENT" in str(value) for value in coverage_shortfalls
    ):
        errors.append("missing reply metadata must be recorded in coverage_shortfalls")
    declared_mnl = manifest.get("must_not_link_count")
    if isinstance(declared_mnl, bool) or not isinstance(declared_mnl, int) or declared_mnl < 0:
        errors.append("manifest must_not_link_count must be a non-negative integer")
    elif declared_mnl != len(mnl_pairs):
        errors.append("manifest must_not_link_count does not match relations")
    label_counts = manifest.get("label_counts")
    if not isinstance(label_counts, Mapping):
        errors.append("manifest label_counts must be an object")
        label_counts = {}
    manifest_relation_counts = label_counts.get("relations")
    if not isinstance(manifest_relation_counts, Mapping) or dict(manifest_relation_counts) != actual_label_counts:
        errors.append("manifest relation label_counts do not match relations")
    if not isinstance(manifest.get("file_sha256"), Mapping):
        errors.append("manifest file_sha256 must be an object")
    if not isinstance(manifest.get("record_counts_by_file"), Mapping):
        errors.append("manifest record_counts_by_file must be an object")
    return ValidationResult(not errors, tuple(sorted(set(errors))), tuple(sorted(set(warnings))))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("%s:%d is invalid JSON: %s" % (path.name, line_number, exc)) from exc
        if not isinstance(value, dict):
            raise ValueError("%s:%d must contain a JSON object" % (path.name, line_number))
        records.append(value)
    return records


def validate_contract_directory(directory: Path) -> ValidationResult:
    root = Path(directory).resolve(strict=True)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return ValidationResult(False, ("manifest.json is missing",), ())
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return ValidationResult(False, ("manifest.json is invalid: %s" % exc.__class__.__name__,), ())
    if not isinstance(manifest, Mapping):
        return ValidationResult(False, ("manifest.json must contain an object",), ())
    dataset: Dict[str, Any] = {"manifest": manifest}
    errors: List[str] = []
    for filename, collection in COLLECTION_BY_FILE.items():
        path = root / filename
        if not path.is_file():
            errors.append("%s is missing" % filename)
            dataset[collection] = []
            continue
        try:
            dataset[collection] = _read_jsonl(path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append("%s is invalid: %s" % (filename, exc))
            dataset[collection] = []
            continue
        file_hashes = manifest.get("file_sha256")
        expected_hash = file_hashes.get(filename) if isinstance(file_hashes, Mapping) else None
        try:
            actual_hash = _file_sha256(path)
        except (OSError, UnicodeError) as exc:
            errors.append("%s cannot be hashed: %s" % (filename, exc.__class__.__name__))
            actual_hash = None
        if expected_hash != actual_hash:
            errors.append("%s SHA-256 does not match manifest" % filename)
        record_counts = manifest.get("record_counts_by_file")
        expected_count = record_counts.get(filename) if isinstance(record_counts, Mapping) else None
        if expected_count != len(dataset[collection]):
            errors.append("%s record count does not match manifest" % filename)
    result = validate_contract_dataset(dataset)
    return ValidationResult(
        not errors and result.ok,
        tuple(sorted(set(errors) | set(result.errors))),
        result.warnings,
    )


def _claim_match_key(record: Mapping[str, Any]) -> Tuple[Any, ...]:
    spans = tuple(
        sorted((int(item.get("start", 0)), int(item.get("end", 0))) for item in record.get("evidence_spans") or [])
    )
    return (
        str(record.get("message_id") or ""), spans, str(record.get("claim_type") or ""),
        tuple(sorted(str(value) for value in record.get("target_entity_ids") or [])),
    )


def _unique_claim_index(
    records: Sequence[Mapping[str, Any]], *, id_field: str, side: str,
) -> Tuple[Dict[str, Mapping[str, Any]], Dict[str, Tuple[Any, ...]], Dict[Tuple[Any, ...], str]]:
    """Build an unambiguous evaluation index; never choose one duplicate silently."""

    claims: Dict[str, Mapping[str, Any]] = {}
    for item in records:
        claim_id = str(item.get(id_field) or "")
        if not claim_id:
            raise ValueError("%s claim is missing %s" % (side, id_field))
        if claim_id in claims:
            raise ValueError("%s claims contain duplicate %s %r" % (side, id_field, claim_id))
        claims[claim_id] = item
    key_by_id = {claim_id: _claim_match_key(item) for claim_id, item in claims.items()}
    ids_by_key: Dict[Tuple[Any, ...], List[str]] = {}
    for claim_id, match_key in key_by_id.items():
        ids_by_key.setdefault(match_key, []).append(claim_id)
    ambiguous = {key: ids for key, ids in ids_by_key.items() if len(ids) > 1}
    if ambiguous:
        details = "; ".join(
            "%r -> %s" % (key, ",".join(sorted(ids)))
            for key, ids in sorted(ambiguous.items(), key=lambda item: repr(item[0]))
        )
        raise ValueError("ambiguous %s claim match keys: %s" % (side, details))
    return claims, key_by_id, {key: ids[0] for key, ids in ids_by_key.items()}


def semantic_result_to_evaluation_payload(result: SemanticResultV2) -> Dict[str, Any]:
    claims = []
    for item in result.claims:
        claims.append(
            {
                "prediction_claim_id": item.claim_id,
                "message_id": item.message_id,
                "speaker_id": item.speaker_id,
                "claim_type": item.claim_type,
                "target_entity_ids": list(item.target_entity_ids),
                "evidence_spans": [{"start": item.evidence_span.span_start, "end": item.evidence_span.span_end}],
            }
        )
    payload = {
        "mentions": [
            {
                "message_id": item.message_id, "span_start": item.span_start, "span_end": item.span_end,
                "mention_type": item.mention_type, "normalized_id": item.normalized_id,
            }
            for item in result.mentions
        ],
        "claims": claims,
        "relations": [
            {
                "left_anchor_id": item.left_claim_id, "right_anchor_id": item.right_claim_id,
                "label": item.relation,
            }
            for item in result.pair_decisions
        ],
        "clusters": [
            {"prediction_cluster_id": item.event_id, "claim_ids": list(item.claim_ids)}
            for item in result.events
        ],
        "presentations": [
            {
                "prediction_presentation_id": item.presentation_id,
                "source_cluster_ids": [item.event_id],
                "source_claim_ids": list(item.supported_claim_ids),
            }
            for item in result.presentations
        ],
    }
    # P0.1 exposes candidate blocking separately from the scored relation
    # edges.  Keep the field absent for the original P0 path so existing
    # evaluation payloads remain byte-compatible.
    if getattr(result, "candidate_pairs", ()):
        payload["candidate_pairs"] = [
            {
                "candidate_id": item.candidate_id,
                "left_anchor_id": item.left_claim_id,
                "right_anchor_id": item.right_claim_id,
                "blocking_reasons": list(item.blocking_reasons),
            }
            for item in result.candidate_pairs
        ]
    return payload


def _pairs(groups: Iterable[Iterable[Any]]) -> set:
    result: set = set()
    for group in groups:
        values = sorted(set(group), key=repr)
        for index, left in enumerate(values):
            for right in values[index + 1 :]:
                result.add((left, right))
    return result


def _ratio(value: int, total: int, empty: float = 1.0) -> float:
    return round(value / total, 6) if total else empty


def _f1(precision: float, recall: float) -> float:
    return round(2 * precision * recall / (precision + recall), 6) if precision + recall else 0.0


def _best_cluster_alignment(gold_sets: Mapping[str, set], predicted_sets: Mapping[str, set]) -> Dict[str, Optional[str]]:
    alignment: Dict[str, Optional[str]] = {}
    for gold_id, gold_set in gold_sets.items():
        candidates = []
        for predicted_id, predicted_set in predicted_sets.items():
            union = gold_set | predicted_set
            score = len(gold_set & predicted_set) / len(union) if union else 1.0
            candidates.append((score, len(gold_set & predicted_set), predicted_id))
        best = max(candidates, default=(0.0, 0, None))
        alignment[gold_id] = best[2] if best[0] > 0 else None
    return alignment


def score_gold_predictions(dataset: Mapping[str, Any], predictions: Mapping[str, Any]) -> Dict[str, Any]:
    """Score candidates, clusters and presentations without comparing cluster IDs."""

    validation = validate_contract_dataset(dataset)
    if not validation.ok:
        raise ValueError("gold dataset is invalid: %s" % "; ".join(validation.errors))
    gold_claims, gold_key_by_id, gold_id_by_key = _unique_claim_index(
        dataset["claims"], id_field="claim_id", side="gold",
    )
    pred_claims, pred_key_by_id, pred_id_by_key = _unique_claim_index(
        predictions.get("claims") or [], id_field="prediction_claim_id", side="predicted",
    )
    common_keys = set(gold_id_by_key) & set(pred_id_by_key)

    gold_mentions = {
        (str(item.get("message_id")), int(item.get("span_start")), int(item.get("span_end")),
         str(item.get("mention_type")), str(item.get("normalized_id")))
        for item in dataset["mentions"]
    }
    pred_mentions = {
        (str(item.get("message_id")), int(item.get("span_start")), int(item.get("span_end")),
         str(item.get("mention_type")), str(item.get("normalized_id")))
        for item in predictions.get("mentions") or []
    }
    mention_tp = len(gold_mentions & pred_mentions)

    def relation_map(items: Sequence[Mapping[str, Any]], id_to_key: Mapping[str, Any]) -> Dict[Tuple[Any, Any], str]:
        output = {}
        for item in items:
            left = id_to_key.get(str(item.get("left_anchor_id")))
            right = id_to_key.get(str(item.get("right_anchor_id")))
            if left is not None and right is not None and left != right:
                output[tuple(sorted((left, right), key=repr))] = str(item.get("label"))
        return output

    gold_relations = relation_map(dataset["relations"], gold_key_by_id)
    pred_relations = relation_map(predictions.get("relations") or [], pred_key_by_id)
    gold_candidate_pairs = set(gold_relations)
    pred_candidate_pairs = set(pred_relations)
    candidate_overlap = gold_candidate_pairs & pred_candidate_pairs
    relation_rows = []
    typed_correct = same_tp = overmerge_rel = oversplit_rel = 0
    for pair in sorted(gold_candidate_pairs | pred_candidate_pairs, key=repr):
        gold_label = gold_relations.get(pair)
        pred_label = pred_relations.get(pair)
        typed_correct += int(gold_label is not None and gold_label == pred_label)
        same_tp += int(gold_label == pred_label == "same_event")
        overmerge_rel += int(gold_label not in {None, "same_event"} and pred_label == "same_event")
        oversplit_rel += int(gold_label == "same_event" and pred_label != "same_event")
        relation_rows.append({"pair": [repr(pair[0]), repr(pair[1])], "gold": gold_label, "predicted": pred_label})

    def cluster_sets(records: Sequence[Mapping[str, Any]], claim_map: Mapping[str, Any]) -> Dict[str, set]:
        output = {}
        for record in records:
            cluster_id = str(record.get("cluster_id") or record.get("prediction_cluster_id") or "")
            output[cluster_id] = {
                claim_map[value] for value in map(str, record.get("claim_ids") or []) if value in claim_map
            }
        return output

    gold_clusters = cluster_sets(dataset["clusters"], gold_key_by_id)
    pred_clusters = cluster_sets(predictions.get("clusters") or [], pred_key_by_id)
    gold_cluster_pairs = _pairs(gold_clusters.values())
    pred_cluster_pairs = _pairs(pred_clusters.values())
    cluster_tp = len(gold_cluster_pairs & pred_cluster_pairs)
    cluster_precision = _ratio(cluster_tp, len(pred_cluster_pairs))
    cluster_recall = _ratio(cluster_tp, len(gold_cluster_pairs))
    overmerge = pred_cluster_pairs - gold_cluster_pairs
    oversplit = gold_cluster_pairs - pred_cluster_pairs

    gold_membership = {member: members for members in gold_clusters.values() for member in members}
    pred_membership = {member: members for members in pred_clusters.values() for member in members}
    bc_precision_values = []
    bc_recall_values = []
    # B-Cubed uses the complete gold claim universe. A missed prediction is an
    # unassigned singleton: it cannot inherit the cluster of another predicted
    # member and therefore lowers recall for a multi-claim gold cluster. Extra
    # predictions are reported by claim coverage rather than added to this
    # gold-universe average.
    for member in sorted(gold_id_by_key, key=repr):
        gold_set = gold_membership.get(member, {member})
        pred_set = pred_membership.get(member, {member}) if member in common_keys else {member}
        intersection = len(gold_set & pred_set)
        bc_precision_values.append(intersection / len(pred_set))
        bc_recall_values.append(intersection / len(gold_set))
    bc_precision = sum(bc_precision_values) / len(bc_precision_values) if bc_precision_values else 1.0
    bc_recall = sum(bc_recall_values) / len(bc_recall_values) if bc_recall_values else 1.0

    mnl_pairs = {
        tuple(sorted((gold_key_by_id[str(item["left_anchor_id"])], gold_key_by_id[str(item["right_anchor_id"])]), key=repr))
        for item in dataset["relations"]
        if item.get("must_not_link") is True
        and str(item.get("left_anchor_id")) in gold_key_by_id
        and str(item.get("right_anchor_id")) in gold_key_by_id
    }
    mnl_violations = mnl_pairs & pred_cluster_pairs

    alignment = _best_cluster_alignment(gold_clusters, pred_clusters)
    pred_presentations_by_cluster: Dict[str, List[Mapping[str, Any]]] = {}
    for item in predictions.get("presentations") or []:
        for cluster_id in item.get("source_cluster_ids") or []:
            pred_presentations_by_cluster.setdefault(str(cluster_id), []).append(item)
    presentation_exact = 0
    presentation_total = len(dataset["presentations"])
    presentation_expansion = 0
    for gold_presentation in dataset["presentations"]:
        gold_source_clusters = [str(value) for value in gold_presentation.get("source_cluster_ids") or []]
        gold_source_keys = {
            gold_key_by_id[value]
            for value in map(str, gold_presentation.get("source_claim_ids") or [])
            if value in gold_key_by_id
        }
        aligned_ids = [alignment.get(value) for value in gold_source_clusters]
        candidates = [
            item for cluster_id in aligned_ids if cluster_id is not None
            for item in pred_presentations_by_cluster.get(cluster_id, [])
        ]
        candidate_sets = [
            {pred_key_by_id[value] for value in map(str, item.get("source_claim_ids") or []) if value in pred_key_by_id}
            for item in candidates
        ]
        presentation_exact += int(any(value == gold_source_keys for value in candidate_sets))
        presentation_expansion += int(any(bool(value - gold_source_keys) for value in candidate_sets))

    attribution_correct = sum(
        str(gold_claims[gold_id_by_key[key]].get("speaker_id"))
        == str(pred_claims[pred_id_by_key[key]].get("speaker_id"))
        for key in common_keys
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mention": {
            "precision": _ratio(mention_tp, len(pred_mentions)),
            "recall": _ratio(mention_tp, len(gold_mentions)),
            "f1": _f1(_ratio(mention_tp, len(pred_mentions)), _ratio(mention_tp, len(gold_mentions))),
        },
        "claim": {
            "gold_count": len(gold_claims), "predicted_count": len(pred_claims),
            "aligned_count": len(common_keys),
            "gold_coverage": _ratio(len(common_keys), len(gold_claims)),
            "predicted_in_gold_rate": _ratio(len(common_keys), len(pred_claims)),
            "speaker_accuracy": _ratio(attribution_correct, len(gold_claims)),
        },
        "candidate_coverage": {
            "gold_candidate_count": len(gold_candidate_pairs),
            "predicted_candidate_count": len(pred_candidate_pairs),
            "overlap_count": len(candidate_overlap),
            "missing_predicted_count": len(gold_candidate_pairs - pred_candidate_pairs),
            "extra_predicted_count": len(pred_candidate_pairs - gold_candidate_pairs),
            "gold_coverage": _ratio(len(candidate_overlap), len(gold_candidate_pairs)),
            "predicted_in_gold_rate": _ratio(len(candidate_overlap), len(pred_candidate_pairs)),
        },
        "relation": {
            "typed_accuracy_on_gold": _ratio(typed_correct, len(gold_candidate_pairs)),
            "same_event_true_positive": same_tp,
            "overmerge_count": overmerge_rel,
            "oversplit_count": oversplit_rel,
            "rows": relation_rows,
        },
        "cluster": {
            "b_cubed_evaluation_universe": "gold_claims_missing_predictions_as_singletons",
            "pairwise_precision": cluster_precision,
            "pairwise_recall": cluster_recall,
            "pairwise_f1": _f1(cluster_precision, cluster_recall),
            "b_cubed_precision": round(bc_precision, 6),
            "b_cubed_recall": round(bc_recall, 6),
            "b_cubed_f1": _f1(bc_precision, bc_recall),
            "overmerge_count": len(overmerge),
            "oversplit_count": len(oversplit),
        },
        "must_not_link": {
            "count": len(mnl_pairs), "violation_count": len(mnl_violations),
            "violation_rate": _ratio(len(mnl_violations), len(mnl_pairs), 0.0),
        },
        "presentation": {
            "gold_count": presentation_total,
            "exact_evidence_count": presentation_exact,
            "exact_evidence_rate": _ratio(presentation_exact, presentation_total),
            "evidence_expansion_count": presentation_expansion,
            "cluster_alignment": alignment,
        },
    }


validate_gold_dataset = validate_contract_dataset


__all__ = [
    "SCHEMA_VERSION", "DATASET_VERSION", "ANNOTATION_GUIDE_VERSION", "REDACTION_POLICY_VERSION",
    "LOCAL_DAY", "WINDOW_START_LOCAL", "WINDOW_END_LOCAL", "WINDOW_START_UTC", "WINDOW_END_UTC",
    "PRIVATE_ROOT", "DEFAULT_WORKING_DIR", "DEFAULT_RELEASE_DIR", "DEFAULT_PRIVATE_OUTPUT_DIR",
    "PRIVATE_JSONL_FILES", "ValidationResult", "WorkingSeedResult", "GoldValidationResult",
    "GoldExportResult", "contract_schema", "gold_schema_v1", "empty_contract_collections",
    "open_source_database_read_only", "inspect_source_coverage_read_only",
    "read_source_messages_read_only", "build_export_manifest_dry_run",
    "export_private_pre_redaction_seed", "export_redacted_gold_seed",
    "validate_contract_dataset", "validate_contract_directory", "validate_gold_dataset",
    "semantic_result_to_evaluation_payload", "score_gold_predictions",
]
