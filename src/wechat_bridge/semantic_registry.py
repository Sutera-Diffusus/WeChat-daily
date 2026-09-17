"""Low-cost, metadata-first message registration for the semantic shadow path.

This module is intentionally independent from the production bridge.  It
records a small public message envelope, freezes the public payload used by
the shadow path, and exposes deterministic hashes for replay/cache keys.  It
does not inspect ``raw_*``/private fields, infer missing metadata from display
names or content, and never creates event/title/frontend objects.

The registry is the first boundary in Workstream A:

``public message -> immutable registration -> semantic gate -> bundle``

Only explicitly supplied metadata is authoritative.  A missing speaker,
account, chat, sequence, timestamp or reply remains ``unknown``/``None``;
the content cannot fill that slot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, is_dataclass, asdict
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


SCHEMA_VERSION = "semantic_v2"
CONTEXT_SCHEMA_VERSION = "dialogue_context_v1"
PIPELINE_VERSION = "workstream_a_registry_v1"
RULESET_VERSION = "registry_rules_v1"
UNKNOWN = "unknown"

# These are the only input keys copied into a frozen public payload.  In
# particular, raw_text/private_* and arbitrary underscored fields never cross
# this boundary.  ``fragment_candidates``/``claims`` are public synthetic
# adapters for the later bundle builder and are not interpreted here.
PUBLIC_MESSAGE_FIELDS = frozenset(
    {
        "message_id",
        "source_message_id",
        "source_snapshot",
        "source_snapshot_fingerprint",
        "account_id",
        "chat_id",
        "chat_type",
        "speaker_id",
        "sender_id",
        "direction",
        "message_type",
        "content",
        "redacted_text",
        "text",
        "message_text",
        "timestamp",
        "time_offset_seconds",
        "time_offset",
        "sequence_in_chat",
        "sequence",
        "reply_to_message_id",
        "reply_to_id",
        "quoted_message_id",
        "quote_message_id",
        "referenced_message_id",
        "reference_message_id",
        "parent_message_id",
        "in_reply_to",
        "dialogue_segment_id",
        "segment_id",
        "dialogue_role",
        "message_role",
        "source_mode",
        "adapter_version",
        "event_time",
        "event_time_precision",
        "body_length_estimate",
        "language_hint",
        "metadata_revision",
        "registry_state",
        "gate_reason_codes",
        "activation_cues",
        "artifact_refs",
        "input_fingerprint",
        "created_at",
        "split",
        "semantic_channel",
        "gate_channel",
        "context_message_ids",
        "cold_recoverable",
        "message_type",
        "media_state",
        "fragment_candidates",
        "fragments",
        "claims",
        "mentioned_persons",
        "mentioned_people",
        "person_mentions",
        "mentions",
        "object",
        "object_ref",
        "objects",
        "object_refs",
        "target",
        "target_entity",
    }
)

_TEXT_FIELDS = ("content", "redacted_text", "text", "message_text")
_REPLY_FIELDS = (
    "reply_to_message_id",
    "reply_to_id",
    "quoted_message_id",
    "quote_message_id",
    "referenced_message_id",
    "reference_message_id",
    "parent_message_id",
    "in_reply_to",
)


def _jsonable(value: Any) -> Any:
    """Convert a public value into deterministic JSON-compatible data."""

    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    """Return the SHA-256 of a canonical public value."""

    return sha256(_canonical_bytes(value)).hexdigest()


def _normal_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _field(message: Any, name: str, default: Any = None) -> Any:
    if isinstance(message, Mapping):
        return message.get(name, default)
    if name.startswith("_"):
        return default
    return getattr(message, name, default)


def _public_mapping(message: Any) -> Dict[str, Any]:
    """Copy only explicitly whitelisted public fields from a message."""

    if isinstance(message, Mapping):
        return {
            str(key): value
            for key, value in message.items()
            if str(key) in PUBLIC_MESSAGE_FIELDS
        }
    return {
        name: _field(message, name)
        for name in PUBLIC_MESSAGE_FIELDS
        if _field(message, name) is not None
    }


def _freeze(value: Any) -> Any:
    """Recursively freeze a public payload without retaining mutable aliases."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if is_dataclass(value):
        return _freeze(asdict(value))
    return value


def _id_value(value: Any) -> str:
    text = _normal_text(value)
    return text or UNKNOWN


def _optional_id(value: Any) -> Optional[str]:
    text = _normal_text(value)
    return text or None


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(mapping: Mapping[str, Any]) -> str:
    for field_name in _TEXT_FIELDS:
        if field_name in mapping and mapping[field_name] is not None:
            return str(mapping[field_name])
    return ""


def _reply_target(mapping: Mapping[str, Any]) -> Optional[str]:
    for field_name in _REPLY_FIELDS:
        value = _optional_id(mapping.get(field_name))
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class MessageMetadata:
    """Authoritative metadata extracted without semantic guessing."""

    message_id: str = UNKNOWN
    account_id: str = UNKNOWN
    chat_id: str = UNKNOWN
    chat_type: str = UNKNOWN
    speaker_id: str = UNKNOWN
    direction: str = UNKNOWN
    message_type: str = UNKNOWN
    timestamp: Optional[str] = None
    time_offset_seconds: Optional[float] = None
    sequence_in_chat: Optional[int] = None
    reply_to_message_id: Optional[str] = None
    dialogue_segment_id: Optional[str] = None
    dialogue_role: Optional[str] = None
    source_mode: str = UNKNOWN
    split: str = UNKNOWN
    adapter_version: str = UNKNOWN
    event_time_precision: str = UNKNOWN
    language_hint: str = UNKNOWN
    source_snapshot_fingerprint: str = UNKNOWN
    metadata_revision: int = 0
    metadata_complete: bool = False
    missing_fields: Tuple[str, ...] = ()

    @property
    def scope_key(self) -> Optional[Tuple[str, str, str]]:
        if UNKNOWN in {self.account_id, self.chat_id}:
            return None
        return (self.account_id, self.chat_id, self.message_id)

    @property
    def chat_scope(self) -> Optional[Tuple[str, str]]:
        if UNKNOWN in {self.account_id, self.chat_id}:
            return None
        return (self.account_id, self.chat_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "speaker_id": self.speaker_id,
            "direction": self.direction,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "time_offset_seconds": self.time_offset_seconds,
            "sequence_in_chat": self.sequence_in_chat,
            "reply_to_message_id": self.reply_to_message_id,
            "dialogue_segment_id": self.dialogue_segment_id,
            "dialogue_role": self.dialogue_role,
            "source_mode": self.source_mode,
            "split": self.split,
            "adapter_version": self.adapter_version,
            "event_time": self.timestamp,
            "event_time_precision": self.event_time_precision,
            "language_hint": self.language_hint,
            "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
            "metadata_revision": self.metadata_revision,
            "metadata_complete": self.metadata_complete,
            "missing_fields": list(self.missing_fields),
        }


def extract_metadata(message: Any) -> MessageMetadata:
    """Extract only explicit public metadata from *message*.

    ``sender_name`` is deliberately ignored as an identity fallback.  This
    function is public so tests and later adapters can audit the authority
    boundary independently from registration.
    """

    mapping = _public_mapping(message)
    # ``source_message_id`` is an explicit adapter alias, not an inferred
    # identity.  It lets offline adapters use the contract's source naming
    # while preserving the same authoritative message key.
    message_id = _id_value(mapping.get("message_id") or mapping.get("source_message_id"))
    account_id = _id_value(mapping.get("account_id"))
    chat_id = _id_value(mapping.get("chat_id"))
    # ``sender_id`` is still metadata, unlike the display-only sender_name.
    speaker_id = _id_value(mapping.get("speaker_id") or mapping.get("sender_id"))
    chat_type = _id_value(mapping.get("chat_type"))
    direction = _id_value(mapping.get("direction"))
    message_type = _id_value(mapping.get("message_type"))
    timestamp_value = mapping.get("timestamp")
    timestamp = None if timestamp_value is None or not str(timestamp_value) else str(timestamp_value)
    offset = mapping.get("time_offset_seconds")
    if offset is None:
        offset = mapping.get("time_offset")
    sequence = mapping.get("sequence_in_chat")
    if sequence is None:
        sequence = mapping.get("sequence")
    segment_id = _optional_id(mapping.get("dialogue_segment_id") or mapping.get("segment_id"))
    dialogue_role = _optional_id(mapping.get("dialogue_role") or mapping.get("message_role"))
    source_mode = _id_value(mapping.get("source_mode"))
    split = _id_value(mapping.get("split"))
    adapter_version = _id_value(mapping.get("adapter_version"))
    event_time_precision = _id_value(mapping.get("event_time_precision"))
    language_hint = _id_value(mapping.get("language_hint"))
    source_snapshot_fingerprint = _id_value(mapping.get("source_snapshot_fingerprint"))
    metadata_revision = _integer(mapping.get("metadata_revision")) or 0
    missing = tuple(
        field_name
        for field_name, value in (
            ("message_id", message_id),
            ("account_id", account_id),
            ("chat_id", chat_id),
            ("speaker_id", speaker_id),
        )
        if value == UNKNOWN
    )
    return MessageMetadata(
        message_id=message_id,
        account_id=account_id,
        chat_id=chat_id,
        chat_type=chat_type,
        speaker_id=speaker_id,
        direction=direction,
        message_type=message_type,
        timestamp=timestamp,
        time_offset_seconds=_number(offset),
        sequence_in_chat=_integer(sequence),
        reply_to_message_id=_reply_target(mapping),
        dialogue_segment_id=segment_id,
        dialogue_role=dialogue_role,
        source_mode=source_mode,
        split=split,
        adapter_version=adapter_version,
        event_time_precision=event_time_precision,
        language_hint=language_hint,
        source_snapshot_fingerprint=source_snapshot_fingerprint,
        metadata_revision=metadata_revision,
        metadata_complete=not missing,
        missing_fields=missing,
    )


@dataclass(frozen=True)
class RawMessageRef:
    """Immutable reference to the whitelisted public message payload.

    The payload is a recursive ``MappingProxyType``/tuple structure.  Its
    ``to_dict`` intentionally returns only a reference summary; downstream
    audit artifacts therefore cannot accidentally serialize message content.
    """

    reference_id: str
    message_id: str
    payload: Mapping[str, Any] = field(repr=False, compare=False)
    content_hash: str

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    @property
    def raw_message(self) -> Mapping[str, Any]:
        """Read-only public payload alias for adapter code."""

        return self.payload

    def keys(self) -> Tuple[str, ...]:
        return tuple(self.payload.keys())

    def items(self) -> Tuple[Tuple[str, Any], ...]:
        return tuple(self.payload.items())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "message_id": self.message_id,
            "content_hash": self.content_hash,
            "immutable": True,
        }


@dataclass(frozen=True)
class RegisteredMessage:
    """A deterministic registration record and its immutable public payload."""

    registry_key: str
    metadata: MessageMetadata
    raw_message_ref: RawMessageRef
    content: str = field(repr=False, compare=False)
    content_hash: str = ""
    metadata_hash: str = ""
    record_hash: str = ""
    cache_key: str = ""
    registration_index: int = 0
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION

    @property
    def message_id(self) -> str:
        return self.metadata.message_id

    @property
    def account_id(self) -> str:
        return self.metadata.account_id

    @property
    def chat_id(self) -> str:
        return self.metadata.chat_id

    @property
    def scope_key(self) -> Optional[Tuple[str, str]]:
        return self.metadata.chat_scope

    @property
    def registry_id(self) -> str:
        return self.registry_key

    @property
    def source_message_id(self) -> str:
        return self.metadata.message_id

    @property
    def body_digest(self) -> str:
        return self.content_hash

    @property
    def body_length_estimate(self) -> int:
        return len(self.content)

    @property
    def language_hint(self) -> str:
        return self.metadata.language_hint

    @property
    def metadata_fingerprint(self) -> str:
        return self.metadata_hash

    @property
    def metadata_revision(self) -> int:
        return self.metadata.metadata_revision

    @property
    def registry_state(self) -> str:
        return "registered"

    @property
    def gate_channel(self) -> Optional[str]:
        return None

    @property
    def gate_reason_codes(self) -> Tuple[str, ...]:
        return ()

    @property
    def activation_cues(self) -> Tuple[str, ...]:
        return ()

    @property
    def artifact_refs(self) -> Dict[str, Any]:
        return {"fragment_ids": [], "bundle_ids": [], "snapshot_ids": []}

    @property
    def input_fingerprint(self) -> str:
        return self.record_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "registry_key": self.registry_key,
            "registry_id": self.registry_id,
            "message_id": self.metadata.message_id,
            "source_message_id": self.source_message_id,
            "account_id": self.metadata.account_id,
            "chat_id": self.metadata.chat_id,
            "speaker_id": self.metadata.speaker_id,
            "message_type": self.metadata.message_type,
            "sequence_in_chat": self.metadata.sequence_in_chat,
            "timestamp": self.metadata.timestamp,
            "time_offset_seconds": self.metadata.time_offset_seconds,
            "reply_to_message_id": self.metadata.reply_to_message_id,
            "dialogue_segment_id": self.metadata.dialogue_segment_id,
            "dialogue_role": self.metadata.dialogue_role,
            "source_mode": self.metadata.source_mode,
            "split": self.metadata.split,
            "adapter_version": self.metadata.adapter_version,
            "event_time": self.metadata.timestamp,
            "event_time_precision": self.metadata.event_time_precision,
            "metadata_complete": self.metadata.metadata_complete,
            "missing_metadata": list(self.metadata.missing_fields),
            "raw_message_ref": self.raw_message_ref.to_dict(),
            "content_hash": self.content_hash,
            "metadata_hash": self.metadata_hash,
            "record_hash": self.record_hash,
            "body_digest": self.body_digest,
            "body_length_estimate": self.body_length_estimate,
            "language_hint": self.language_hint,
            "metadata_fingerprint": self.metadata_fingerprint,
            "metadata_revision": self.metadata_revision,
            "registry_state": self.registry_state,
            "gate_channel": self.gate_channel,
            "gate_reason_codes": list(self.gate_reason_codes),
            "activation_cues": list(self.activation_cues),
            "artifact_refs": self.artifact_refs,
            "input_fingerprint": self.input_fingerprint,
            "cache_key": self.cache_key,
            "registration_index": self.registration_index,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


@dataclass(frozen=True)
class RegistrySnapshot:
    """Immutable registry view suitable for a cache manifest."""

    version: int
    entries: Tuple[RegisteredMessage, ...]
    input_hash: str
    cache_key: str
    schema_version: str = SCHEMA_VERSION
    context_schema_version: str = CONTEXT_SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "entry_count": len(self.entries),
            "entry_hashes": [entry.record_hash for entry in self.entries],
            "input_hash": self.input_hash,
            "cache_key": self.cache_key,
            "schema_version": self.schema_version,
            "context_schema_version": self.context_schema_version,
            "pipeline_version": self.pipeline_version,
            "ruleset_version": self.ruleset_version,
        }


class MessageRegistry:
    """Register public messages without guessing metadata or mutating input."""

    def __init__(
        self,
        *,
        schema_version: str = SCHEMA_VERSION,
        pipeline_version: str = PIPELINE_VERSION,
        ruleset_version: str = RULESET_VERSION,
    ) -> None:
        self.schema_version = str(schema_version)
        self.context_schema_version = CONTEXT_SCHEMA_VERSION
        self.pipeline_version = str(pipeline_version)
        self.ruleset_version = str(ruleset_version)
        self._entries: Dict[str, RegisteredMessage] = {}
        self._message_keys: Dict[str, str] = {}
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[RegisteredMessage]:
        return iter(self._entries.values())

    def _make_entry(self, message: Any, index: int) -> RegisteredMessage:
        public = _public_mapping(message)
        metadata = extract_metadata(public)
        content = _text(public)
        content_hash = stable_hash({"content": content})
        metadata_hash = stable_hash(metadata.to_dict())
        # Unknown message IDs use an internal deterministic key but remain
        # ``message_id=unknown`` in the public metadata.
        registry_key = (
            metadata.message_id
            if metadata.message_id != UNKNOWN
            else "MESSAGE_UNKNOWN_" + stable_hash({"index": index, "public": public})[:16]
        )
        record_hash = stable_hash(
            {
                "schema_version": self.schema_version,
                "context_schema_version": self.context_schema_version,
                "pipeline_version": self.pipeline_version,
                "ruleset_version": self.ruleset_version,
                "metadata_hash": metadata_hash,
                "content_hash": content_hash,
            }
        )
        cache_key = "registry:%s:%s" % (self.ruleset_version, record_hash)
        frozen = _freeze(public)
        if not isinstance(frozen, Mapping):  # pragma: no cover - _freeze mapping invariant
            raise TypeError("public message payload must be a mapping")
        raw_ref = RawMessageRef(
            reference_id="RAW_REF_" + record_hash[:16],
            message_id=metadata.message_id,
            payload=frozen,
            content_hash=content_hash,
        )
        return RegisteredMessage(
            registry_key=registry_key,
            metadata=metadata,
            raw_message_ref=raw_ref,
            content=content,
            content_hash=content_hash,
            metadata_hash=metadata_hash,
            record_hash=record_hash,
            cache_key=cache_key,
            registration_index=index,
            schema_version=self.schema_version,
            context_schema_version=self.context_schema_version,
            pipeline_version=self.pipeline_version,
            ruleset_version=self.ruleset_version,
        )

    def register(self, message: Any) -> RegisteredMessage:
        """Register one message and return its immutable envelope."""

        if isinstance(message, RegisteredMessage):
            return message
        if not isinstance(message, Mapping) and not is_dataclass(message) and not hasattr(message, "__dict__"):
            raise TypeError("message must be a public mapping or object")
        explicit_metadata = extract_metadata(message)
        if explicit_metadata.message_id != UNKNOWN and explicit_metadata.message_id in self._message_keys:
            existing = self._entries[self._message_keys[explicit_metadata.message_id]]
            candidate = self._make_entry(message, existing.registration_index)
            if candidate.record_hash == existing.record_hash:
                # Replays/reimports of the same logical message are
                # idempotent.  Keep the first immutable public snapshot and
                # never replace authoritative metadata in place.
                return existing
            raise ValueError("conflicting duplicate message_id: %s" % explicit_metadata.message_id)
        entry = self._make_entry(message, len(self._entries))
        self._entries[entry.registry_key] = entry
        if entry.message_id != UNKNOWN:
            self._message_keys[entry.message_id] = entry.registry_key
        self._version += 1
        return entry

    def register_many(self, messages: Iterable[Any]) -> Tuple[RegisteredMessage, ...]:
        """Register a sequence in order, rejecting duplicate known IDs."""

        values = tuple(messages or ())
        # ``register`` performs the idempotence/conflict check per item.  Do
        # not pre-reject a replay batch: an exact duplicate must remain one
        # logical message, while a conflicting duplicate still fails closed.
        return tuple(self.register(message) for message in values)

    def get(self, key: str) -> RegisteredMessage:
        text = str(key)
        if text in self._entries:
            return self._entries[text]
        try:
            return self._entries[self._message_keys[text]]
        except KeyError as exc:
            raise KeyError(text) from exc

    def maybe_get(self, key: Any) -> Optional[RegisteredMessage]:
        try:
            return self.get(str(key))
        except KeyError:
            return None

    def snapshot(self) -> RegistrySnapshot:
        entries = tuple(self._entries.values())
        input_hash = stable_hash([entry.record_hash for entry in entries])
        cache_key = "registry-snapshot:%d:%s" % (self._version, input_hash)
        return RegistrySnapshot(
            version=self._version,
            entries=entries,
            input_hash=input_hash,
            cache_key=cache_key,
            schema_version=self.schema_version,
            context_schema_version=self.context_schema_version,
            pipeline_version=self.pipeline_version,
            ruleset_version=self.ruleset_version,
        )


# Explicit aliases keep the registry boundary discoverable to adapters without
# creating a second implementation or a production integration point.
SemanticRegistry = MessageRegistry
MessageRegistration = RegisteredMessage


def register_messages(messages: Iterable[Any], *, registry: Optional[MessageRegistry] = None) -> Tuple[RegisteredMessage, ...]:
    """One-shot registration helper for synthetic/offline callers."""

    target = registry if registry is not None else MessageRegistry()
    return target.register_many(messages)


__all__ = [
    "SCHEMA_VERSION",
    "CONTEXT_SCHEMA_VERSION",
    "PIPELINE_VERSION",
    "RULESET_VERSION",
    "UNKNOWN",
    "PUBLIC_MESSAGE_FIELDS",
    "MessageMetadata",
    "RawMessageRef",
    "RegisteredMessage",
    "RegistrySnapshot",
    "MessageRegistry",
    "SemanticRegistry",
    "MessageRegistration",
    "register_messages",
    "extract_metadata",
    "stable_hash",
]
