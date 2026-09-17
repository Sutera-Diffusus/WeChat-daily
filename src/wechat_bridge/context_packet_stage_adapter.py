"""Explicit K2 ``ContextPacket`` to staged-analyzer (K3) adapter.

The project currently has two deliberately different objects named
``ContextPacket``:

* :mod:`wechat_bridge.context_packets` owns the local, high-recall context
  projection (K2); and
* :mod:`wechat_bridge.staged_deepseek_analyzer` owns the small provider
  protocol (K3).

Keeping the boundary explicit is important.  The K2 packet contains the
authoritative registry projection and reversible candidate windows, while the
K3 packet is the narrow input accepted by the staged A/B/C analyzer.  This
module performs only a loss-aware projection between them.  It never accepts
model output as metadata, never decides a topic/event/claim, and never writes a
production result.

The adapter is intentionally provider-agnostic.  ``ContextPacketStageAdapter``
can be used by itself for inspection, or ``ContextPacketStageOrchestrator``
can pass the projected packet to an injected ``StagedDeepseekAnalyzer``.  The
orchestrator is suitable for synthetic/replay work; it does not create a
provider unless the caller explicitly supplied one to the analyzer.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .staged_deepseek_analyzer import (
    ANALYZER_SCHEMA_VERSION,
    CONTEXT_PACKET_SCHEMA_VERSION as K3_CONTEXT_PACKET_SCHEMA_VERSION,
    ContextPacket as K3ContextPacket,
    StageProtocolError,
    StagedAnalysisResult,
    StagedDeepseekAnalyzer,
    stable_hash,
    validate_context_packet as validate_k3_context_packet,
)
from .dialogue_segments import has_context_prefix, is_context_only_text


ADAPTER_SCHEMA_VERSION = "context_packet_stage_adapter_v1"
ADAPTER_PACKET_VERSION = "k2_to_k3_context_packet_v1"
UNKNOWN = "unknown"
FROZEN_SCOPES = frozenset({"frozen", "frozen_test", "frozen-test"})
_PROVIDER_CONTEXT_FRAGMENT_TYPES = frozenset(
    {"conversation_opener", "acknowledgement", "reaction", "context", "media"}
)
_PROVIDER_MEDIA_TYPES = frozenset(
    {"image", "video", "audio", "file", "sticker", "emoji", "system", "location"}
)
_PROVIDER_SOCIAL_ROLES = frozenset({"conversation_opener", "context_only"})
_PROVIDER_POSITIVE_ROLES = frozenset({"primary", "substantive", "mixed"})
_PROVIDER_BLOCKED_ROLES = frozenset(
    {
        "media",
        "media_placeholder",
        "placeholder",
        "system",
        "event",
        "event_placeholder",
        "event_place_holder",
        "empty",
        "empty_authority",
        "reaction",
        "greeting",
        "conversation_opener",
        "ack",
        "acknowledgement",
        "context_only",
        "authority_only",
    }
)
_PROVIDER_BLOCKED_MESSAGE_TYPES = frozenset(
    {
        "image",
        "photo",
        "picture",
        "video",
        "audio",
        "voice",
        "file",
        "document",
        "card",
        "sticker",
        "emoji",
        "system",
        "location",
        "media",
        "empty",
        "empty_authority",
        "event",
        "event_message",
        "event_placeholder",
    }
)
_PROVIDER_DIRECT_CUE_FIELDS = ("caption", "text", "text_redacted", "message_text")
_PROVIDER_MERGED_CUE_KEYS = frozenset(
    {
        "merged_cue",
        "merged_cues",
        "candidate_cue",
        "candidate_cues",
        "candidate_context",
        "activation_cue",
        "activation_cues",
        "cue_text",
    }
)

# K2 is a local projection and is allowed to retain these fields.  The K3
# provider packet gets an intentionally smaller, explicit projection.
_FRAGMENT_FIELDS = (
    "fragment_id",
    "message_id",
    "account_id",
    "chat_id",
    "segment_id",
    "text_redacted",
    "span",
    "role",
    "fragment_type",
    "speaker_id",
    "mentioned_person_ids",
    "subject_id",
    "subject_type",
    "object_id",
    "object_resolution",
    "object_inherited_from_id",
    "state_candidate",
    "state_evidence",
    "modality",
    "intent_candidate",
    "claim_role_candidate",
    "actions_candidate",
    "temporal_qualifier",
    "reply_to_message_id",
    "is_opener",
    "is_silent",
    "information_value",
    "candidate_only",
    "source",
)

_AUTHORITY_FIELDS = (
    "message_id",
    "registry_key",
    "account_id",
    "chat_id",
    "speaker_id",
    "direction",
    "message_type",
    "sequence_in_chat",
    "timestamp",
    "event_time",
    "time_offset_seconds",
    "reply_to_message_id",
    "quoted_message_id",
    "quote_edges",
    "reply_edges",
    "dialogue_segment_id",
    "dialogue_role",
    "source_mode",
    "source_snapshot_fingerprint",
    "metadata_revision",
    "metadata_authoritative",
    "content_hash",
    "metadata_hash",
    "record_hash",
    # Body-free role/presence markers let the adapter explain why a retained
    # K2 fragment was moved to context without carrying message content in
    # authority metadata.
    "role",
    "message_role",
    "provider_role",
    "semantic_role",
    "content_present",
    "direct_caption_present",
)

_SOURCE_FIELDS = (
    "type",
    "id",
    "message_id",
    "fragment_id",
    "claim_id",
    "registry_key",
    "content_hash",
    "metadata_hash",
    "record_hash",
)

_MESSAGE_ID_FIELDS = (
    "message_id",
    "left_message_id",
    "right_message_id",
    "question_id",
    "answer_id",
    "quoted_message_id",
    "reply_to_message_id",
)
_MESSAGE_LIST_FIELDS = (
    "message_ids",
    "source_message_ids",
)
_FRAGMENT_ID_FIELDS = (
    "fragment_id",
    "left_fragment_id",
    "right_fragment_id",
    "question_fragment_id",
    "answer_fragment_id",
    "fragment_ids",
)


class ContextPacketAdapterError(ValueError):
    """Body-free, stable K2/K3 mapping error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any, *, code: str) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): deepcopy(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return {str(key): deepcopy(item) for key, item in result.items()}
    raise ContextPacketAdapterError(code)


def _text(value: Any, *, code: str, allow_unknown: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextPacketAdapterError(code)
    # Preserve source IDs exactly.  Silently trimming a forged/ambiguous ID
    # would make the K2↔K3 mapping non-auditable and could bypass strict scope
    # checks at the provider boundary.
    if value != value.strip():
        raise ContextPacketAdapterError(code)
    result = value
    if not allow_unknown and result == UNKNOWN:
        raise ContextPacketAdapterError(code)
    if len(result) > 512 or any(ord(char) < 32 for char in result):
        raise ContextPacketAdapterError(code)
    return result


def _id_list(value: Any, *, code: str, allow_empty: bool = True) -> List[str]:
    if value is None:
        values: Sequence[Any] = ()
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ContextPacketAdapterError(code)
    result: List[str] = []
    seen = set()
    for item in values:
        item_text = _text(item, code=code, allow_unknown=False)
        if item_text not in seen:
            result.append(item_text)
            seen.add(item_text)
    if not allow_empty and not result:
        raise ContextPacketAdapterError(code)
    return result


def _mapping_list(value: Any, *, code: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ContextPacketAdapterError(code)
    result: List[Dict[str, Any]] = []
    for item in value:
        result.append(_mapping(item, code=code))
    return result


def _safe_copy_fields(value: Any, fields: Iterable[str]) -> Dict[str, Any]:
    data = _mapping(value, code="k2_field_not_mapping")
    result: Dict[str, Any] = {}
    for name in fields:
        if name in data:
            result[name] = deepcopy(data[name])
    return result


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in values if isinstance(item, str) and item))


def _fragment_text(row: Mapping[str, Any]) -> str:
    """Read only the source fragment text needed for provider role gating."""

    for key in ("text_redacted", "text", "message_text", "content", "body"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def _provider_labels(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    labels: set[str] = set()
    for key in (
        "semantic_role",
        "provider_role",
        "primary_context_role",
        "role",
        "dialogue_role",
        "message_role",
        "layer",
        "fragment_role",
    ):
        marker = value.get(key)
        if marker not in (None, ""):
            labels.add(str(marker).strip().casefold().replace("-", "_").replace(" ", "_"))
    roles = value.get("roles")
    if isinstance(roles, (list, tuple, set, frozenset)):
        labels.update(
            str(marker).strip().casefold().replace("-", "_").replace(" ", "_")
            for marker in roles
            if marker not in (None, "")
        )
    return labels


def _provider_message_type(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("message_type", "type", "media_type", "content_type"):
        marker = value.get(key)
        if marker not in (None, ""):
            return str(marker).strip().casefold().replace("-", "_").replace(" ", "_")
    return ""


def _provider_direct_caption_present(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in _PROVIDER_DIRECT_CUE_FIELDS:
        marker = value.get(key)
        if isinstance(marker, str) and marker.strip():
            return True
    return False


def _provider_has_merged_cue(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("merged_candidate_cue_present") is True:
        return True
    return any(
        str(key).casefold() in _PROVIDER_MERGED_CUE_KEYS and child not in (None, "", [], (), {})
        for key, child in value.items()
    )


def _provider_valid_span(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    start = value.get("start", value.get("span_start"))
    end = value.get("end", value.get("span_end", start))
    return isinstance(start, int) and isinstance(end, int) and start >= 0 and end >= start


def _provider_authority_blocks_cue(row: Mapping[str, Any], authority: Mapping[str, Any]) -> bool:
    authority_labels = _provider_labels(authority)
    authority_type = _provider_message_type(authority)
    if authority_labels & _PROVIDER_BLOCKED_ROLES:
        return True
    if authority_type in _PROVIDER_BLOCKED_MESSAGE_TYPES or authority_type in {"event", "event_message"}:
        return True
    row_labels = _provider_labels(row)
    row_type = _provider_message_type(row)
    if row_labels & _PROVIDER_BLOCKED_ROLES:
        return True
    if row_type in _PROVIDER_BLOCKED_MESSAGE_TYPES or row_type in {"event", "event_message"}:
        return True
    return any(row.get(key) is True or row.get(key) == 1 for key in ("is_placeholder", "placeholder", "media_placeholder", "event_placeholder"))


def _provider_has_span(row: Mapping[str, Any]) -> bool:
    if _provider_valid_span(row.get("span")):
        return True
    nested = row.get("evidence_refs")
    if isinstance(nested, (list, tuple)):
        return any(isinstance(item, Mapping) and _provider_valid_span(item.get("span")) for item in nested)
    return False


def _provider_context_only(
    row: Mapping[str, Any],
    authorities_by_message: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether a retained K2 fragment is context-only for K3.

    K2 ``primary_fragments`` is a lossless retention view.  This classifier is
    deliberately kept at the K3 boundary so an opener/ack/media row can be
    moved to the adjacent/context marker without changing the K2 source.  A
    recognised social prefix with a substantive suffix remains primary.
    """

    message_id = str(row.get("message_id") or "")
    if not message_id or message_id.casefold() == UNKNOWN:
        return True
    authority = authorities_by_message.get(message_id, {})
    # A fragment body/role is not an authoritative message binding.  The K3
    # projection therefore fails closed when the source fact is absent.
    if not isinstance(authority, Mapping) or not authority:
        return True
    message_type = authority.get("message_type", row.get("message_type", "text"))
    if _provider_authority_blocks_cue(row, authority):
        authority_labels = _provider_labels(authority)
        valid_role = bool(authority_labels & _PROVIDER_POSITIVE_ROLES) if authority_labels else bool(_provider_labels(row) & _PROVIDER_POSITIVE_ROLES)
        direct = _provider_direct_caption_present(row) or _provider_direct_caption_present(authority)
        if not (direct and valid_role and not _provider_has_merged_cue(row)):
            return True
    if not _provider_has_span(row):
        return True
    text = _fragment_text(row)
    if is_context_only_text(text, message_type=message_type):
        return True
    role = str(row.get("role") or row.get("dialogue_role") or row.get("message_role") or "").strip().casefold()
    fragment_type = str(row.get("fragment_type") or row.get("kind") or "").strip().casefold()
    if fragment_type in _PROVIDER_CONTEXT_FRAGMENT_TYPES:
        if fragment_type != "media" and text and has_context_prefix(text):
            return False
        if fragment_type == "media" and text and not is_context_only_text(text, message_type=message_type):
            return str(message_type or "").casefold() in _PROVIDER_MEDIA_TYPES
        return True
    if role in _PROVIDER_SOCIAL_ROLES or bool(row.get("is_opener")):
        if role in _PROVIDER_SOCIAL_ROLES and text and has_context_prefix(text):
            return False
        return True
    if bool(row.get("is_silent")) and str(message_type or "").casefold() in _PROVIDER_MEDIA_TYPES:
        return True
    return False


def _is_frozen_marker(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() in FROZEN_SCOPES
    return False


def _hash_or(value: Any, fallback: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return stable_hash(fallback)


def _safe_scope_key(account_id: str, chat_id: str) -> str:
    # Delimiters are structural; IDs themselves are not interpreted by the
    # provider.  Keeping both values prevents same-chat cross-account leakage.
    return "%s/%s" % (account_id, chat_id)


def _extract_scope_ids(item: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    account = item.get("account_id")
    chat = item.get("chat_id")
    if not isinstance(account, str) or not account:
        account = None
    if not isinstance(chat, str) or not chat:
        chat = None
    return account, chat


def _candidate_message_ids(item: Mapping[str, Any], fragment_to_message: Mapping[str, str]) -> Tuple[str, ...]:
    values: List[str] = []
    for key in _MESSAGE_ID_FIELDS:
        value = item.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    for key in _MESSAGE_LIST_FIELDS:
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(entry) for entry in value if isinstance(entry, str) and entry)
    for key in _FRAGMENT_ID_FIELDS:
        value = item.get(key)
        if isinstance(value, str) and value:
            message_id = fragment_to_message.get(value)
            if message_id:
                values.append(message_id)
        elif isinstance(value, (list, tuple)):
            values.extend(fragment_to_message.get(str(entry), "") for entry in value)
    return _dedupe(values)


def _check_nested_scope(item: Mapping[str, Any], account_id: str, chat_id: str) -> None:
    """Reject explicit foreign scope in a candidate/ref edge.

    Reply/quote/evidence refs sometimes carry scope only on the nested edge,
    not on the enclosing candidate.  Checking those structural fields keeps a
    foreign reference from hiding inside an otherwise local packet.  We do not
    infer scope from IDs when the edge does not declare it.
    """

    def check(value: Mapping[str, Any]) -> None:
        account = value.get("account_id")
        chat = value.get("chat_id")
        if account not in (None, UNKNOWN, account_id):
            raise ContextPacketAdapterError("cross_account_scope_forbidden")
        if chat not in (None, UNKNOWN, chat_id):
            raise ContextPacketAdapterError("cross_chat_scope_forbidden")

    check(item)
    for key in ("source_refs", "evidence_refs", "quote_edges", "reply_edges"):
        nested = item.get(key)
        if isinstance(nested, (list, tuple)):
            for value in nested:
                if isinstance(value, Mapping):
                    check(value)


@dataclass(frozen=True)
class ContextPacketMapping:
    """Auditable, body-free description of one K2→K3 projection."""

    adapter_schema_version: str
    k2_packet_id: str
    k2_packet_version: str
    k2_packet_hash: str
    k2_fixed_hash: str
    k2_dynamic_hash: str
    k2_cache_key: str
    k3_packet_id: str
    k3_packet_hash: str
    k3_packet_schema_version: str
    scope: str
    account_id: str
    chat_id: str
    source_message_ids: Tuple[str, ...]
    context_message_ids: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    entity_ids: Tuple[str, ...]
    authoritative_fact_count: int
    primary_fragment_count: int
    adjacent_context_count: int
    candidate_count: int
    activation_cue_count: int
    uncertainty_count: int

    @property
    def cache_namespace(self) -> str:
        return "%s:%s" % (self.adapter_schema_version, self.k2_packet_version)

    @property
    def cache_key(self) -> str:
        return stable_hash(
            {
                "adapter_schema_version": self.adapter_schema_version,
                "k2_packet_id": self.k2_packet_id,
                "k2_packet_version": self.k2_packet_version,
                "k2_packet_hash": self.k2_packet_hash,
                "k2_fixed_hash": self.k2_fixed_hash,
                "k2_dynamic_hash": self.k2_dynamic_hash,
                "k2_cache_key": self.k2_cache_key,
                "k3_packet_hash": self.k3_packet_hash,
                "k3_packet_schema_version": self.k3_packet_schema_version,
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_schema_version": self.adapter_schema_version,
            "k2_packet_id": self.k2_packet_id,
            "k2_packet_version": self.k2_packet_version,
            "k2_packet_hash": self.k2_packet_hash,
            "k2_fixed_hash": self.k2_fixed_hash,
            "k2_dynamic_hash": self.k2_dynamic_hash,
            "k2_cache_key": self.k2_cache_key,
            "k3_packet_id": self.k3_packet_id,
            "k3_packet_hash": self.k3_packet_hash,
            "k3_packet_schema_version": self.k3_packet_schema_version,
            "scope": self.scope,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "source_message_ids": list(self.source_message_ids),
            "context_message_ids": list(self.context_message_ids),
            "evidence_ids": list(self.evidence_ids),
            "entity_ids": list(self.entity_ids),
            "counts": {
                "authoritative_facts": self.authoritative_fact_count,
                "primary_fragments": self.primary_fragment_count,
                "adjacent_context": self.adjacent_context_count,
                "candidates": self.candidate_count,
                "activation_cues": self.activation_cue_count,
                "uncertainties": self.uncertainty_count,
            },
            "cache_namespace": self.cache_namespace,
            "cache_key": self.cache_key,
        }


@dataclass(frozen=True)
class AdaptedContextPacket:
    """K2 packet, K3 packet and their explicit mapping.

    ``to_dict`` is body-free by default.  Provider-facing content is available
    only through ``model_packet``/``to_model_packet`` and is never copied to a
    stage ledger by the orchestrator.
    """

    stage_packet: K3ContextPacket
    mapping: ContextPacketMapping
    _source_packet: Any = field(default=None, repr=False, compare=False)

    @property
    def k2_packet(self) -> Any:
        return self._source_packet

    @property
    def packet(self) -> K3ContextPacket:
        return self.stage_packet

    @property
    def model_packet(self) -> Dict[str, Any]:
        return self.stage_packet.to_model_packet()

    def to_model_packet(self) -> Dict[str, Any]:
        return self.model_packet

    def to_dict(self, *, include_model_packet: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
            "mapping": self.mapping.to_dict(),
            "stage_packet": {
                "packet_id": self.stage_packet.packet_id,
                "packet_sha256": self.stage_packet.packet_sha256,
                "schema_version": self.stage_packet.schema_version,
                "scope": self.stage_packet.scope,
                "message_count": len(self.stage_packet.all_message_ids),
                "evidence_count": len(self.stage_packet.evidence_ids),
                "entity_count": len(self.stage_packet.entity_ids),
            },
        }
        if include_model_packet:
            result["model_packet"] = self.model_packet
        return result


@dataclass(frozen=True)
class AdaptedContextPacketResult:
    """Batch projection retaining K2 result accounting without its body."""

    packets: Tuple[AdaptedContextPacket, ...]
    input_hash: str
    source_packet_version: str
    cache_hits: int = 0
    cache_misses: int = 0

    def __iter__(self):
        return iter(self.packets)

    def __len__(self) -> int:
        return len(self.packets)

    def __getitem__(self, index: int) -> AdaptedContextPacket:
        return self.packets[index]

    @property
    def stage_packets(self) -> Tuple[K3ContextPacket, ...]:
        return tuple(item.stage_packet for item in self.packets)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
            "input_hash": self.input_hash,
            "source_packet_version": self.source_packet_version,
            "packet_count": len(self.packets),
            "cache_hits": int(self.cache_hits),
            "cache_misses": int(self.cache_misses),
            "packets": [item.to_dict() for item in self.packets],
        }


@dataclass(frozen=True)
class StagedContextPacketResult:
    """Result of one K2 packet passed through the K3 staged analyzer."""

    adapted: AdaptedContextPacket
    analysis: StagedAnalysisResult

    @property
    def stage_packet(self) -> K3ContextPacket:
        return self.adapted.stage_packet

    @property
    def mapping(self) -> ContextPacketMapping:
        return self.adapted.mapping

    @property
    def status(self) -> str:
        return self.analysis.status

    @property
    def ledger(self):
        return self.analysis.ledger

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
            "mapping": self.mapping.to_dict(),
            "analysis": self.analysis.to_dict(),
        }


class ContextPacketStageAdapter:
    """Project one or many K2 packets into the K3 analyzer contract."""

    def __init__(self, *, adapter_schema_version: str = ADAPTER_SCHEMA_VERSION) -> None:
        self.adapter_schema_version = _text(adapter_schema_version, code="adapter_schema_version")

    def _source_data(self, packet: Any) -> Dict[str, Any]:
        data = _mapping(packet, code="k2_packet_type")
        # The K3 packet has no primary/adjacent fields; accepting it here
        # would silently bypass the authority boundary.
        if "primary_fragments" not in data or "authoritative_facts" not in data:
            raise ContextPacketAdapterError("k2_packet_shape")
        return data

    def _scope(self, data: Mapping[str, Any], facts: Sequence[Mapping[str, Any]], fragments: Sequence[Mapping[str, Any]]) -> Tuple[str, str, str]:
        fixed = data.get("fixed_part") if isinstance(data.get("fixed_part"), Mapping) else {}
        fixed_scope = fixed.get("scope") if isinstance(fixed, Mapping) else {}
        declared_account = data.get("account_id") or (fixed_scope.get("account_id") if isinstance(fixed_scope, Mapping) else None)
        declared_chat = data.get("chat_id") or (fixed_scope.get("chat_id") if isinstance(fixed_scope, Mapping) else None)
        account_id = str(declared_account or UNKNOWN)
        chat_id = str(declared_chat or UNKNOWN)

        observed: List[Tuple[str, str]] = []
        for item in tuple(facts) + tuple(fragments):
            account, chat = _extract_scope_ids(item)
            if account and chat and account != UNKNOWN and chat != UNKNOWN:
                observed.append((account, chat))
        observed_unique = list(dict.fromkeys(observed))
        if len(observed_unique) > 1:
            accounts = {item[0] for item in observed_unique}
            chats = {item[1] for item in observed_unique}
            if len(accounts) > 1:
                raise ContextPacketAdapterError("cross_account_scope_forbidden")
            raise ContextPacketAdapterError("cross_chat_scope_forbidden")
        if observed_unique:
            observed_account, observed_chat = observed_unique[0]
            if account_id != UNKNOWN and account_id != observed_account:
                raise ContextPacketAdapterError("account_scope_mismatch")
            if chat_id != UNKNOWN and chat_id != observed_chat:
                raise ContextPacketAdapterError("chat_scope_mismatch")
            account_id = observed_account if account_id == UNKNOWN else account_id
            chat_id = observed_chat if chat_id == UNKNOWN else chat_id

        if _is_frozen_marker(account_id) or _is_frozen_marker(chat_id):
            raise ContextPacketAdapterError("frozen_scope_forbidden")
        scope = _safe_scope_key(account_id, chat_id)
        if account_id == UNKNOWN or chat_id == UNKNOWN:
            # Without an authoritative pair, a multi-message packet could
            # bridge unrelated chats.  One-message packets remain recoverable;
            # the stage prompt receives the explicit unknown scope.
            source_ids = _id_list(data.get("source_message_ids"), code="k2_source_message_ids")
            if len(source_ids) > 1:
                raise ContextPacketAdapterError("scope_unknown_for_multi_message_packet")
        return account_id, chat_id, scope

    def _check_frozen(self, data: Mapping[str, Any], facts: Sequence[Mapping[str, Any]]) -> None:
        # Only inspect structural split/scope fields.  Do not scan message
        # content, hashes, or arbitrary metadata for the word "frozen".
        values: List[Any] = []
        for container in (data, data.get("fixed_part"), data.get("dynamic_part"), data.get("metadata")):
            if not isinstance(container, Mapping):
                continue
            for key in ("split", "dataset_split", "partition", "scope", "data_scope", "source_scope"):
                if key in container:
                    value = container[key]
                    values.append(value)
                    if isinstance(value, Mapping):
                        values.extend(
                            value.get(name)
                            for name in ("split", "dataset_split", "partition", "scope", "account_id", "chat_id")
                        )
        for item in facts:
            for key in ("split", "dataset_split", "partition", "scope"):
                if key in item:
                    values.append(item[key])
        for value in values:
            if _is_frozen_marker(value):
                raise ContextPacketAdapterError("frozen_scope_forbidden")
            if isinstance(value, Mapping) and any(_is_frozen_marker(item) for item in value.values()):
                raise ContextPacketAdapterError("frozen_scope_forbidden")

    def _authority_projection(self, facts: Sequence[Mapping[str, Any]], account_id: str, chat_id: str) -> Tuple[Dict[str, Any], ...]:
        result: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for fact in facts:
            _check_nested_scope(fact, account_id, chat_id)
            item = _safe_copy_fields(fact, _AUTHORITY_FIELDS)
            message_id = item.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                raise ContextPacketAdapterError("authoritative_message_id_missing")
            if message_id in seen:
                continue
            seen.add(message_id)
            fact_account = item.get("account_id")
            fact_chat = item.get("chat_id")
            if fact_account not in (None, UNKNOWN, account_id) or fact_chat not in (None, UNKNOWN, chat_id):
                if fact_account not in (None, UNKNOWN, account_id):
                    raise ContextPacketAdapterError("cross_account_scope_forbidden")
                raise ContextPacketAdapterError("cross_chat_scope_forbidden")
            # Keep only body-free authority metadata in the staged packet.  A
            # raw K2 fact may carry role/caption/content markers; preserve
            # the scalar marker (and aliases) while representing body
            # presence as booleans so it cannot be mistaken for evidence.
            for key in ("role", "message_role", "provider_role", "semantic_role"):
                if fact.get(key) not in (None, ""):
                    item[key] = str(fact[key])
            item["content_present"] = any(
                fact.get(key) not in (None, "")
                for key in ("content", "body", "text", "text_redacted", "message_text", "description")
            )
            item["direct_caption_present"] = _provider_direct_caption_present(fact)
            for key in ("message_alias", "source_message_alias", "message_handle", "source_message_handle"):
                if isinstance(fact.get(key), str) and fact.get(key):
                    item[key] = fact[key]
            item["metadata_authoritative"] = True
            result.append(item)
        return tuple(result)

    def _fragment_projection(self, fragments: Sequence[Mapping[str, Any]], account_id: str, chat_id: str) -> Tuple[Dict[str, Any], ...]:
        result: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for fragment in fragments:
            item = _safe_copy_fields(fragment, _FRAGMENT_FIELDS)
            fragment_id = item.get("fragment_id")
            message_id = item.get("message_id")
            if not isinstance(fragment_id, str) or not fragment_id:
                raise ContextPacketAdapterError("fragment_id_missing")
            if not isinstance(message_id, str) or not message_id:
                raise ContextPacketAdapterError("fragment_message_id_missing")
            fragment_account = item.get("account_id")
            fragment_chat = item.get("chat_id")
            if fragment_account not in (None, UNKNOWN, account_id):
                raise ContextPacketAdapterError("cross_account_scope_forbidden")
            if fragment_chat not in (None, UNKNOWN, chat_id):
                raise ContextPacketAdapterError("cross_chat_scope_forbidden")
            if fragment_id in seen:
                continue
            seen.add(fragment_id)
            if _provider_has_merged_cue(fragment):
                # Preserve only the fact that a merged/candidate cue was
                # present; never carry that cue body into the K3 packet.
                item["merged_candidate_cue_present"] = True
            item["candidate_only"] = True
            result.append(item)
        return tuple(result)

    def _message_projection(
        self,
        primary: Sequence[Mapping[str, Any]],
        adjacent: Sequence[Mapping[str, Any]],
        authorities: Sequence[Mapping[str, Any]],
        account_id: str,
        chat_id: str,
    ) -> Tuple[Dict[str, Any], ...]:
        authority_by_message = {str(item["message_id"]): item for item in authorities}
        fragments_by_message: Dict[str, List[Mapping[str, Any]]] = {}
        primary_ids = {str(item["message_id"]) for item in primary}
        for fragment in tuple(primary) + tuple(adjacent):
            fragments_by_message.setdefault(str(fragment["message_id"]), []).append(fragment)
        message_ids = list(fragments_by_message)
        for authority in authorities:
            message_id = str(authority["message_id"])
            if message_id not in fragments_by_message:
                message_ids.append(message_id)
        result: List[Dict[str, Any]] = []
        for message_id in message_ids:
            authority = authority_by_message.get(message_id, {})
            fragments = fragments_by_message.get(message_id, [])
            first = fragments[0] if fragments else {}
            content = first.get("text_redacted", "")
            if not isinstance(content, str):
                content = ""
            spans = [deepcopy(item.get("span", {})) for item in fragments if isinstance(item.get("span", {}), Mapping)]
            result.append(
                {
                    "message_id": message_id,
                    "content": content,
                    "fragment_ids": [str(item["fragment_id"]) for item in fragments],
                    "spans": spans,
                    "is_primary": message_id in primary_ids,
                    "metadata_only": not bool(fragments),
                    "authoritative": {
                        "account_id": authority.get("account_id", account_id),
                        "chat_id": authority.get("chat_id", chat_id),
                        "speaker_id": authority.get("speaker_id", UNKNOWN),
                        "direction": authority.get("direction", UNKNOWN),
                        "message_type": authority.get("message_type", UNKNOWN),
                        "sequence_in_chat": authority.get("sequence_in_chat"),
                        "timestamp": authority.get("timestamp"),
                        "event_time": authority.get("event_time"),
                        "time_offset_seconds": authority.get("time_offset_seconds"),
                        "reply_to_message_id": authority.get("reply_to_message_id"),
                        "quoted_message_id": authority.get("quoted_message_id"),
                        "quote_edges": deepcopy(authority.get("quote_edges", [])),
                        "dialogue_segment_id": authority.get("dialogue_segment_id"),
                        "dialogue_role": authority.get("dialogue_role", UNKNOWN),
                        "metadata_authoritative": True,
                    },
                    "candidate_signals": {
                        "fragments": [deepcopy(dict(item)) for item in fragments],
                        "source": "k2_candidate_projection",
                    },
                }
            )
        return tuple(result)

    def _validate_candidate_scopes(
        self,
        candidates: Sequence[Mapping[str, Any]],
        fragment_to_message: Mapping[str, str],
        authorities_by_message: Mapping[str, Mapping[str, Any]],
        account_id: str,
        chat_id: str,
    ) -> Tuple[str, ...]:
        message_ids: List[str] = []
        for candidate in candidates:
            _check_nested_scope(candidate, account_id, chat_id)
            ids = _candidate_message_ids(candidate, fragment_to_message)
            for message_id in ids:
                authority = authorities_by_message.get(message_id)
                if authority is not None:
                    observed_account = authority.get("account_id")
                    observed_chat = authority.get("chat_id")
                    if observed_account not in (None, UNKNOWN, account_id):
                        raise ContextPacketAdapterError("cross_account_scope_forbidden")
                    if observed_chat not in (None, UNKNOWN, chat_id):
                        raise ContextPacketAdapterError("cross_chat_scope_forbidden")
                message_ids.append(message_id)
        return _dedupe(message_ids)

    def _evidence_projection(
        self,
        data: Mapping[str, Any],
        primary: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
        allowed_messages: set[str],
        account_id: str,
        chat_id: str,
    ) -> Tuple[Tuple[str, ...], Tuple[Dict[str, Any], ...]]:
        refs: List[Dict[str, Any]] = []
        refs.extend(_mapping_list(data.get("evidence_refs"), code="k2_evidence_refs_shape"))
        # A valid K2 packet normally already contains these refs.  The local
        # fragment span is a safe recoverable fallback for hand-built packets.
        if not refs:
            for fragment in primary:
                refs.append(
                    {
                        "type": "fragment",
                        "id": fragment.get("fragment_id"),
                        "message_id": fragment.get("message_id"),
                        "span": deepcopy(fragment.get("span", {})),
                    }
                )
        for candidate in candidates:
            refs.extend(_mapping_list(candidate.get("evidence_refs"), code="candidate_evidence_shape"))

        result: List[Dict[str, Any]] = []
        handles: List[str] = []
        aliases: Dict[str, str] = {}
        for ref in refs:
            _check_nested_scope(ref, account_id, chat_id)
            source_id = ref.get("evidence_id") or ref.get("id")
            if not isinstance(source_id, str) or not source_id:
                raise ContextPacketAdapterError("evidence_id_missing")
            message_id = ref.get("message_id")
            if isinstance(message_id, str) and message_id and message_id not in allowed_messages:
                raise ContextPacketAdapterError("evidence_message_out_of_scope")
            ref_type = str(ref.get("type") or "evidence")
            handle = str(ref.get("evidence_id") or "e:%s:%s" % (ref_type, source_id))
            if handle in handles:
                continue
            span = ref.get("span") if isinstance(ref.get("span"), Mapping) else {}
            start = span.get("start", ref.get("span_start", 0))
            end = span.get("end", ref.get("span_end", start))
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                raise ContextPacketAdapterError("evidence_span_invalid")
            result.append(
                {
                    "evidence_id": handle,
                    "source_ref_id": source_id,
                    "type": ref_type,
                    "message_id": message_id if isinstance(message_id, str) else UNKNOWN,
                    "fragment_id": ref.get("fragment_id", UNKNOWN),
                    "claim_id": ref.get("claim_id", UNKNOWN),
                    "span": {"start": start, "end": end},
                    "candidate_only": True,
                    "source_authoritative": ref_type in {"fragment", "span", "message"},
                }
            )
            handles.append(handle)
            # Preserve the original source ID as an alias only when it cannot
            # collide with another typed reference.  Both forms are local
            # handles, never free-form provider-generated evidence.
            if source_id not in aliases:
                aliases[source_id] = handle
        # Add unambiguous aliases after the canonical handles are known.
        collisions = {source_id for source_id, handle in aliases.items() if sum(1 for item in result if item["source_ref_id"] == source_id) > 1}
        for source_id, handle in aliases.items():
            if source_id in collisions or source_id in handles:
                continue
            handles.append(source_id)
        return tuple(handles), tuple(result)

    def _entity_ids(
        self,
        primary: Sequence[Mapping[str, Any]],
        adjacent: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
        authorities: Sequence[Mapping[str, Any]],
    ) -> Tuple[str, ...]:
        values: List[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and value and value != UNKNOWN:
                values.append(value)

        for item in authorities:
            add(item.get("speaker_id"))
        for item in tuple(primary) + tuple(adjacent):
            add(item.get("speaker_id"))
            add(item.get("subject_id"))
            add(item.get("object_id"))
            add(item.get("object_inherited_from_id"))
            for person in item.get("mentioned_person_ids", ()) if isinstance(item.get("mentioned_person_ids"), (list, tuple)) else ():
                add(person)
        for item in candidates:
            for key in ("speaker_id", "subject_id", "object_id", "object_ref_id", "person_ref_id", "target_id"):
                add(item.get(key))
            for key in ("mentioned_person_ids", "person_ids"):
                values_from_item = item.get(key)
                if isinstance(values_from_item, (list, tuple)):
                    for value in values_from_item:
                        add(value)
        return _dedupe(values)

    def _candidate_context(
        self,
        data: Mapping[str, Any],
        primary: Sequence[Mapping[str, Any]],
        adjacent: Sequence[Mapping[str, Any]],
        candidates_by_kind: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> Dict[str, Any]:
        all_candidates: List[Dict[str, Any]] = []
        for values in candidates_by_kind.values():
            all_candidates.extend(deepcopy(dict(item)) for item in values)
        dynamic = data.get("dynamic_part") if isinstance(data.get("dynamic_part"), Mapping) else {}
        packet_reasons = data.get("candidate_reason")
        reasons: List[Dict[str, Any]] = []
        if isinstance(packet_reasons, (list, tuple)):
            packet_reason_codes = [reason for reason in packet_reasons if isinstance(reason, str) and reason]
            if packet_reason_codes:
                reasons.append(
                    {
                        "candidate_id": data.get("packet_id", UNKNOWN),
                        "reason_codes": list(dict.fromkeys(packet_reason_codes)),
                        "confidence": "low",
                        "evidence_refs": [],
                    }
                )
        for candidate in all_candidates:
            candidate_id = candidate.get("candidate_id", UNKNOWN)
            reason_codes = candidate.get("candidate_reason") or candidate.get("supporting_slot_codes") or ()
            if not isinstance(reason_codes, (list, tuple)):
                reason_codes = ()
            reasons.append(
                {
                    "candidate_id": candidate_id,
                    "reason_codes": [str(item) for item in reason_codes],
                    "confidence": str(candidate.get("confidence") or "low"),
                    "evidence_refs": deepcopy(candidate.get("evidence_refs") or []),
                }
            )
        # Keep both the K2 names and the K1 conceptual names so downstream
        # audit/replay code can verify that no context view disappeared.
        open_threads = _mapping_list(data.get("open_thread_candidates"), code="open_thread_shape")
        # ``unresolved`` is a local lifecycle label.  The provider-facing
        # packet uses an unambiguous non-terminal name so downstream checks do
        # not mistake it for the terminal state ``resolved``.
        normalized_open_threads: List[Dict[str, Any]] = []
        for thread in open_threads:
            if "unresolved_slot_codes" in thread and "open_slot_codes" not in thread:
                thread["open_slot_codes"] = thread.pop("unresolved_slot_codes")
            normalized_open_threads.append(thread)
        return {
            "primary_fragments": [deepcopy(dict(item)) for item in primary],
            "adjacent_context": [deepcopy(dict(item)) for item in adjacent],
            "continuity_candidates": deepcopy(all_candidates),
            "qa_candidates": deepcopy(list(candidates_by_kind.get("candidate_qa_links", ()))),
            "person_history": deepcopy(list(candidates_by_kind.get("candidate_person_history", ()))),
            "object_history": deepcopy(list(candidates_by_kind.get("candidate_object_history", ()))),
            "state_history": deepcopy(list(candidates_by_kind.get("candidate_state_history", ()))),
            "open_threads": deepcopy(normalized_open_threads),
            "activation_cues": deepcopy(_mapping_list(data.get("activation_cues"), code="activation_cue_shape")),
            "candidate_reasons": reasons,
            "uncertainties": [str(item) for item in (data.get("uncertainties") or ()) if isinstance(item, str)],
            "window_scale": dynamic.get("scale", data.get("scale", UNKNOWN)),
            "candidate_only": True,
        }

    def adapt_with_mapping(self, packet: Any) -> AdaptedContextPacket:
        data = self._source_data(packet)

        packet_id = _text(data.get("packet_id") or data.get("context_packet_id"), code="k2_packet_id", allow_unknown=False)
        packet_version = _text(data.get("packet_version"), code="k2_packet_version")
        # A hand-built K2 mapping may omit its precomputed hash.  Hash the
        # complete source mapping in that case so content changes cannot
        # accidentally reuse a staged-model cache entry.  This is a digest
        # operation only; source content is never copied into mapping/ledger
        # telemetry.
        packet_hash = _hash_or(
            data.get("packet_hash") or data.get("hash"),
            {"packet_id": packet_id, "packet_version": packet_version, "source_packet": data},
        )
        fixed = data.get("fixed_part") if isinstance(data.get("fixed_part"), Mapping) else {}
        dynamic = data.get("dynamic_part") if isinstance(data.get("dynamic_part"), Mapping) else {}
        fixed_hash = _hash_or(data.get("fixed_hash"), fixed)
        dynamic_hash = _hash_or(data.get("dynamic_hash"), dynamic)
        k2_cache_key = _hash_or(data.get("cache_key"), {"packet_hash": packet_hash, "fixed_hash": fixed_hash, "dynamic_hash": dynamic_hash})

        facts_raw = _mapping_list(data.get("authoritative_facts"), code="authoritative_facts_shape")
        primary_raw = _mapping_list(data.get("primary_fragments"), code="primary_fragments_shape")
        adjacent_raw = _mapping_list(data.get("adjacent_context"), code="adjacent_context_shape")
        if not primary_raw:
            raise ContextPacketAdapterError("primary_fragments_empty")
        self._check_frozen(data, facts_raw + primary_raw + adjacent_raw)
        account_id, chat_id, scope = self._scope(data, facts_raw, primary_raw + adjacent_raw)
        authorities = self._authority_projection(facts_raw, account_id, chat_id)
        authorities_by_message: Dict[str, Mapping[str, Any]] = {}
        for item in authorities:
            aliases = [item.get("message_id")]
            for key in ("message_alias", "source_message_alias", "message_handle", "source_message_handle"):
                aliases.append(item.get(key))
            for key in ("message_aliases", "aliases", "message_handles", "handles"):
                values = item.get(key)
                if isinstance(values, (list, tuple, set, frozenset)):
                    aliases.extend(values)
            for alias in aliases:
                if isinstance(alias, str) and alias:
                    authorities_by_message.setdefault(alias, item)
        retained_primary = self._fragment_projection(primary_raw, account_id, chat_id)
        source_adjacent = self._fragment_projection(adjacent_raw, account_id, chat_id)

        # K2 retains every source fragment in ``primary_fragments`` for local
        # recovery.  K3 has a stricter model-facing role split: pure social,
        # acknowledgement, silence and media rows become adjacent/context
        # markers, while a mixed substantive turn remains primary.
        context_primary = tuple(
            item for item in retained_primary if _provider_context_only(item, authorities_by_message)
        )
        semantic_primary = tuple(
            item for item in retained_primary if not _provider_context_only(item, authorities_by_message)
        )
        if not semantic_primary:
            raise ContextPacketAdapterError("primary_fragments_context_only")
        marker_ids = {str(item.get("fragment_id")) for item in source_adjacent}
        provider_adjacent_rows = list(source_adjacent)
        for item in context_primary:
            fragment_id = str(item.get("fragment_id"))
            if fragment_id not in marker_ids:
                provider_adjacent_rows.append(item)
                marker_ids.add(fragment_id)
        adjacent = tuple(provider_adjacent_rows)
        fragment_to_message = {
            str(item["fragment_id"]): str(item["message_id"])
            for item in tuple(retained_primary) + tuple(adjacent)
        }

        candidates_by_kind: Dict[str, Tuple[Dict[str, Any], ...]] = {}
        candidate_values: List[Mapping[str, Any]] = []
        for key in ("candidate_qa_links", "candidate_person_history", "candidate_object_history", "candidate_state_history"):
            values = tuple(_mapping_list(data.get(key), code="%s_shape" % key))
            # A candidate that references a pure context row would turn a
            # social/media marker into a semantic link.  K2 still keeps the
            # original candidate; the provider projection simply omits it.
            context_fragment_ids = {str(item.get("fragment_id")) for item in context_primary}
            semantic_message_ids = {str(item.get("message_id")) for item in semantic_primary}
            context_message_ids = {
                str(item.get("message_id"))
                for item in context_primary
                if str(item.get("message_id")) not in semantic_message_ids
            }
            filtered_values = tuple(
                item
                for item in values
                if not (
                    context_fragment_ids
                    & {
                        str(item.get(field))
                        for field in _FRAGMENT_ID_FIELDS
                        if isinstance(item.get(field), str) and item.get(field)
                    }
                )
                and not (set(_candidate_message_ids(item, fragment_to_message)) & context_message_ids)
            )
            candidates_by_kind[key] = filtered_values
            candidate_values.extend(filtered_values)
        # The names above are the K2 contract; K2 may additionally carry a
        # continuity list in a hand-built packet.  Retain it as candidates.
        extra_continuity = _mapping_list(data.get("continuity_candidates"), code="continuity_candidates_shape")
        if extra_continuity:
            candidates_by_kind["continuity_candidates"] = tuple(extra_continuity)
            candidate_values.extend(extra_continuity)
        candidate_message_ids = self._validate_candidate_scopes(candidate_values, fragment_to_message, authorities_by_message, account_id, chat_id)

        primary_message_ids = _dedupe(str(item["message_id"]) for item in semantic_primary)
        semantic_message_id_set = set(primary_message_ids)
        context_primary_message_ids = _dedupe(
            str(item["message_id"])
            for item in context_primary
            if str(item["message_id"]) not in semantic_message_id_set
        )
        context_ids = _dedupe(
            [str(item["message_id"]) for item in adjacent]
            + list(context_primary_message_ids)
            + list(candidate_message_ids)
            + [str(item["message_id"]) for item in authorities if str(item["message_id"]) not in primary_message_ids]
        )
        context_ids = tuple(item for item in context_ids if item not in primary_message_ids)
        all_message_ids = set(primary_message_ids) | set(context_ids)
        evidence_ids, evidence = self._evidence_projection(
            data,
            semantic_primary,
            candidate_values,
            all_message_ids,
            account_id,
            chat_id,
        )
        entities = self._entity_ids(semantic_primary, adjacent, candidate_values, authorities)
        messages = self._message_projection(semantic_primary, adjacent, authorities, account_id, chat_id)

        analysis_run_id = _get(data, "analysis_run_id") or _get(data.get("metadata"), "analysis_run_id")
        if not isinstance(analysis_run_id, str) or not analysis_run_id:
            analysis_run_id = "run-k2-%s" % packet_hash[:16]
        source_refs = []
        for source in _mapping_list(data.get("source_refs"), code="source_refs_shape"):
            _check_nested_scope(source, account_id, chat_id)
            source_refs.append(_safe_copy_fields(source, _SOURCE_FIELDS))
        uncertainties = [str(item) for item in (data.get("uncertainties") or ()) if isinstance(item, str)]
        candidate_context = self._candidate_context(data, semantic_primary, adjacent, candidates_by_kind)
        candidate_context["retained_context_fragments"] = [deepcopy(dict(item)) for item in context_primary]
        authority_payload = {
            "message_metadata": [deepcopy(dict(item)) for item in authorities],
            "source_refs": source_refs,
            "evidence_refs": [deepcopy(dict(item)) for item in evidence],
            "scope": {"account_id": account_id, "chat_id": chat_id},
            "authority_only": True,
        }
        metadata = {
            "adapter_schema_version": self.adapter_schema_version,
            "adapter_packet_version": ADAPTER_PACKET_VERSION,
            "analysis_run_id": analysis_run_id,
            # Flat aliases make the source identity obvious to request-level
            # audit code; the full versioned/hash set remains under k2_source.
            "source_packet_id": packet_id,
            "source_packet_version": packet_version,
            "source_packet_hash": packet_hash,
            "k2_source": {
                "packet_id": packet_id,
                "packet_version": packet_version,
                "packet_hash": packet_hash,
                "fixed_hash": fixed_hash,
                "dynamic_hash": dynamic_hash,
                "cache_key": k2_cache_key,
                "pipeline_version": data.get("pipeline_version", UNKNOWN),
                "ruleset_version": data.get("ruleset_version", UNKNOWN),
            },
            "authoritative_facts": authority_payload,
            "candidate_context": candidate_context,
            "boundary": deepcopy(dynamic.get("boundary") or data.get("boundary") or {
                "start": {"resolution": UNKNOWN, "message_id": UNKNOWN, "evidence_ref": UNKNOWN},
                "end": {"resolution": UNKNOWN, "message_id": UNKNOWN, "evidence_ref": UNKNOWN},
            }),
            "window": {
                "scale": candidate_context.get("window_scale", UNKNOWN),
                "message_ids": list(primary_message_ids + context_ids),
                "fragment_ids": [str(item["fragment_id"]) for item in tuple(retained_primary) + tuple(adjacent)],
                "claim_ids": list(_id_list(data.get("claim_ids"), code="k2_claim_ids")),
            },
            "overlap_group_id": data.get("overlap_group_id", UNKNOWN),
            "status": data.get("status", "pending"),
            "uncertainties": uncertainties,
            "source_refs": source_refs,
            "evidence_refs": [deepcopy(dict(item)) for item in evidence],
            "provenance": {
                "input_fingerprint": packet_hash,
                "pipeline_version": data.get("pipeline_version", UNKNOWN),
                "ruleset_version": data.get("ruleset_version", UNKNOWN),
                "k2_packet_version": packet_version,
                "k2_fixed_hash": fixed_hash,
                "k2_dynamic_hash": dynamic_hash,
                "k2_cache_key": k2_cache_key,
            },
        }
        k3 = K3ContextPacket(
            packet_id=packet_id,
            scope=scope,
            message_ids=primary_message_ids,
            context_message_ids=context_ids,
            evidence_ids=evidence_ids,
            entity_ids=entities,
            messages=messages,
            evidence=evidence,
            metadata=metadata,
            schema_version=K3_CONTEXT_PACKET_SCHEMA_VERSION,
        )
        validation = validate_k3_context_packet(k3)
        if not validation.ok:
            raise ContextPacketAdapterError(validation.errors[0] if validation.errors else "k3_packet_invalid")
        mapping = ContextPacketMapping(
            adapter_schema_version=self.adapter_schema_version,
            k2_packet_id=packet_id,
            k2_packet_version=packet_version,
            k2_packet_hash=packet_hash,
            k2_fixed_hash=fixed_hash,
            k2_dynamic_hash=dynamic_hash,
            k2_cache_key=k2_cache_key,
            k3_packet_id=k3.packet_id,
            k3_packet_hash=k3.packet_sha256,
            k3_packet_schema_version=k3.schema_version,
            scope=scope,
            account_id=account_id,
            chat_id=chat_id,
            source_message_ids=primary_message_ids,
            context_message_ids=context_ids,
            evidence_ids=evidence_ids,
            entity_ids=entities,
            authoritative_fact_count=len(authorities),
            primary_fragment_count=len(retained_primary),
            adjacent_context_count=len(adjacent),
            candidate_count=len(candidate_values),
            activation_cue_count=len(candidate_context.get("activation_cues", ())),
            uncertainty_count=len(uncertainties),
        )
        return AdaptedContextPacket(stage_packet=k3, mapping=mapping, _source_packet=packet)

    def adapt(self, packet: Any) -> K3ContextPacket:
        """Return only the K3 packet for callers that do not need the map."""

        return self.adapt_with_mapping(packet).stage_packet

    __call__ = adapt

    def adapt_result(self, result: Any) -> AdaptedContextPacketResult:
        data = _mapping(result, code="k2_packet_result_type") if isinstance(result, Mapping) else None
        packets_value = data.get("packets") if data is not None else _get(result, "packets")
        if packets_value is None:
            raise ContextPacketAdapterError("k2_packet_result_shape")
        if not isinstance(packets_value, (list, tuple)):
            raise ContextPacketAdapterError("k2_packet_result_packets_type")
        packets = tuple(self.adapt_with_mapping(item) for item in packets_value)
        input_hash = data.get("input_hash", "") if data is not None else _get(result, "input_hash", "")
        source_version = data.get("packet_version", "") if data is not None else _get(result, "packet_version", "")
        return AdaptedContextPacketResult(
            packets=packets,
            input_hash=str(input_hash or stable_hash([item.mapping.k2_packet_hash for item in packets])),
            source_packet_version=str(source_version or UNKNOWN),
            cache_hits=int(data.get("cache_hits", 0) if data is not None else (_get(result, "cache_hits", 0) or 0)),
            cache_misses=int(data.get("cache_misses", 0) if data is not None else (_get(result, "cache_misses", 0) or 0)),
        )

    def cache_key_for(self, packet: Any, *, stage: str = "", topic_id: str = "") -> str:
        """Return a stable adapter namespace key including every K2 hash."""

        adapted = self.adapt_with_mapping(packet)
        return stable_hash(
            {
                "adapter_schema_version": self.adapter_schema_version,
                "adapter_packet_version": ADAPTER_PACKET_VERSION,
                "k2_packet_hash": adapted.mapping.k2_packet_hash,
                "k2_packet_version": adapted.mapping.k2_packet_version,
                "k2_fixed_hash": adapted.mapping.k2_fixed_hash,
                "k2_dynamic_hash": adapted.mapping.k2_dynamic_hash,
                "k2_cache_key": adapted.mapping.k2_cache_key,
                "k3_packet_hash": adapted.mapping.k3_packet_hash,
                "analyzer_schema_version": ANALYZER_SCHEMA_VERSION,
                "stage": str(stage),
                "topic_id": str(topic_id),
            }
        )


class ContextPacketStageOrchestrator:
    """Run one or many adapted K2 packets through an injected analyzer."""

    def __init__(self, analyzer: StagedDeepseekAnalyzer, *, adapter: Optional[ContextPacketStageAdapter] = None) -> None:
        if not hasattr(analyzer, "analyze") or not callable(analyzer.analyze):
            raise TypeError("analyzer must provide analyze")
        self.analyzer = analyzer
        self.adapter = adapter or ContextPacketStageAdapter()

    def analyze(
        self,
        packet: Any,
        *,
        previous: Optional[Union[StagedAnalysisResult, StagedContextPacketResult]] = None,
        retry_stages: Optional[Iterable[str]] = None,
    ) -> StagedContextPacketResult:
        adapted = self.adapter.adapt_with_mapping(packet)
        previous_analysis: Optional[StagedAnalysisResult]
        if isinstance(previous, StagedContextPacketResult):
            if previous.mapping.k2_packet_hash != adapted.mapping.k2_packet_hash:
                raise ContextPacketAdapterError("previous_k2_packet_mismatch")
            previous_analysis = previous.analysis
        else:
            previous_analysis = previous
        analysis = self.analyzer.analyze(
            adapted.stage_packet,
            previous=previous_analysis,
            retry_stages=retry_stages,
        )
        return StagedContextPacketResult(adapted=adapted, analysis=analysis)

    def analyze_packets(
        self,
        packets: Iterable[Any],
        *,
        previous_by_packet_id: Optional[Mapping[str, Union[StagedAnalysisResult, StagedContextPacketResult]]] = None,
        retry_stages: Optional[Iterable[str]] = None,
    ) -> Tuple[StagedContextPacketResult, ...]:
        results: List[StagedContextPacketResult] = []
        previous = previous_by_packet_id or {}
        for packet in packets:
            packet_id = str(_get(packet, "packet_id") or _get(packet, "context_packet_id") or "")
            prior = previous.get(packet_id)
            results.append(self.analyze(packet, previous=prior, retry_stages=retry_stages))
        return tuple(results)


def adapt_context_packet(packet: Any) -> K3ContextPacket:
    """Functional shorthand for :meth:`ContextPacketStageAdapter.adapt`."""

    return ContextPacketStageAdapter().adapt(packet)


def adapt_context_packet_with_mapping(packet: Any) -> AdaptedContextPacket:
    return ContextPacketStageAdapter().adapt_with_mapping(packet)


def analyze_context_packet(
    packet: Any,
    analyzer: StagedDeepseekAnalyzer,
    *,
    previous: Optional[Union[StagedAnalysisResult, StagedContextPacketResult]] = None,
    retry_stages: Optional[Iterable[str]] = None,
    adapter: Optional[ContextPacketStageAdapter] = None,
) -> StagedContextPacketResult:
    return ContextPacketStageOrchestrator(analyzer, adapter=adapter).analyze(
        packet,
        previous=previous,
        retry_stages=retry_stages,
    )


__all__ = [
    "ADAPTER_PACKET_VERSION",
    "ADAPTER_SCHEMA_VERSION",
    "AdaptedContextPacket",
    "AdaptedContextPacketResult",
    "ContextPacketAdapterError",
    "ContextPacketMapping",
    "ContextPacketStageAdapter",
    "ContextPacketStageOrchestrator",
    "StagedContextPacketResult",
    "adapt_context_packet",
    "adapt_context_packet_with_mapping",
    "analyze_context_packet",
]
