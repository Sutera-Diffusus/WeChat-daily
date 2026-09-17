"""Pure, replayable semantic-v2 shadow pipeline.

This module is deliberately isolated from the production analysis path.  It
does not read configuration, databases, files or the network.  Callers pass a
finite collection of legacy-shaped message mappings and receive immutable,
JSON-serializable semantic objects suitable for offline evaluation.

The baseline is precision-oriented: shared parent topics are candidate recall
signals only.  Events are built exclusively from claims, and a presentation is
always a projection of exactly one event.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

from .dialogue_segments import (
    ROLE_CONTEXT_ONLY,
    ROLE_CONVERSATION_OPENER,
    ROLE_SUBSTANTIVE,
    segment_dialogues,
)


SCHEMA_VERSION = "semantic_v2"
PIPELINE_VERSION = "semantic_pipeline_p0_v1"
RULESET_VERSION = "structured_rules_a_v1"
# P0.1 is an offline-only refinement of the pure shadow pipeline.  Keep the
# original constants stable so existing P0 replay artifacts remain comparable;
# new callers opt into the versioned P0.1 entry point below.
P01_PIPELINE_VERSION = "semantic_pipeline_p0_1"
P01_RULESET_VERSION = "structured_rules_a_p0_1"
P01_VERSION = "p0.1"
# P0.2 is an offline development-only refinement.  It keeps the P0/P0.1
# namespaces intact so historical replays remain byte-comparable.
P02_PIPELINE_VERSION = "semantic_pipeline_p0_2"
P02_RULESET_VERSION = "structured_rules_a_p0_2"
P02_VERSION = "p0.2"
SHADOW_SOURCE = "semantic_v2_shadow"

RELATION_SAME_EVENT = "same_event"
RELATION_RELATED_EVENT = "related_event"
RELATION_SAME_TOPIC_ONLY = "same_topic_only"
RELATION_UNRELATED = "unrelated"
RELATION_INSUFFICIENT_CONTEXT = "insufficient_context"
EVENT_RELATIONS = frozenset(
    {
        RELATION_SAME_EVENT,
        RELATION_RELATED_EVENT,
        RELATION_SAME_TOPIC_ONLY,
        RELATION_UNRELATED,
        RELATION_INSUFFICIENT_CONTEXT,
    }
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


class SerializableV2:
    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ProvenanceV2(SerializableV2):
    input_ids: Tuple[str, ...]
    stage: str
    parameters_version: str = RULESET_VERSION
    human_revision_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRefV2(SerializableV2):
    message_id: str
    span_start: int
    span_end: int
    evidence_text: str


@dataclass(frozen=True)
class MessageV2(SerializableV2):
    message_id: str
    chat_id: str
    speaker_id: str
    speaker_name: str
    content: str
    timestamp: str
    reply_to_message_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    source: str = SHADOW_SOURCE
    compatibility_warnings: Tuple[str, ...] = ()
    # Optional structured linkage supplied by an offline development fixture.
    # It is never inferred from timestamp proximity.
    explicit_instance_id: Optional[str] = None
    # Optional upstream conversation/block metadata.  These values are only
    # honored when supplied by the caller; P0.1 never invents a block key.
    dialogue_segment_id: Optional[str] = None
    block_id: Optional[str] = None
    # Account is part of the isolation scope even when the legacy input only
    # supplied the default account.  Keeping it on the DTO lets the P0.1
    # candidate/classification layers reject cross-account links without
    # reaching back into raw input mappings.
    account_id: str = "default"
    # Optional source attribution used by the offline development relation
    # gate.  It is deliberately metadata-only; ordinary legacy callers keep
    # the historical ``direct`` default.
    attribution: str = "direct"
    # Optional ordering metadata supplied by an offline fixture.  P0.2 uses
    # it only to keep continuation blocking local to a few turns.
    position_in_block: Optional[int] = None


@dataclass(frozen=True)
class MentionV2(SerializableV2):
    mention_id: str
    message_id: str
    span_start: int
    span_end: int
    evidence_text: str
    mention_type: str
    normalized_id: str
    surface_text: str
    action: Optional[str]
    time_value: Optional[str]
    status: Optional[str]
    request: Optional[str]
    confidence: float
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimV2(SerializableV2):
    claim_id: str
    speaker_id: str
    speaker_name: str
    claim_text: str
    claim_type: str
    target_entity_ids: Tuple[str, ...]
    event_mention_ids: Tuple[str, ...]
    action: str
    request: str
    stance_or_polarity: str
    status_or_modality: str
    timestamp: str
    message_id: str
    reply_to_message_id: Optional[str]
    evidence_span: EvidenceRefV2
    confidence: float
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()
    # P0.1 keeps the optional context and attribution slots on the DTO rather
    # than encoding them in presentation text.  Defaults preserve wire
    # compatibility with P0 callers that construct ClaimV2 directly.
    dialogue_segment_id: Optional[str] = None
    attribution: str = "direct"
    context_message_ids: Tuple[str, ...] = ()
    explicit_instance_id: Optional[str] = None
    block_id: Optional[str] = None
    # All exact-span entity evidence in the clause, including generic object
    # mentions that are intentionally not promoted to ``target_entity_ids``.
    # Relation/MNL logic can use this evidence without weakening claim-match
    # target precision.
    entity_ids: Tuple[str, ...] = ()
    # Explicit scope carried forward from the source message.  These fields
    # are appended for wire/source compatibility with existing direct DTO
    # callers while making cross-chat/account relation gates auditable.
    account_id: str = "default"
    chat_id: str = "unknown"
    # P0.2 keeps the canonical primary action in ``action`` for wire
    # compatibility, while retaining all recognized actions for blocking and
    # instance-aware relation classification.
    action_types: Tuple[str, ...] = ()
    # Optional source ordering metadata.  It is only a locality guard for
    # continuation evidence and is absent from historical P0/P0.1 claims.
    position_in_block: Optional[int] = None


@dataclass(frozen=True)
class PairDecisionV2(SerializableV2):
    decision_id: str
    left_claim_id: str
    right_claim_id: str
    relation: str
    supporting_slots: Tuple[str, ...]
    conflicting_slots: Tuple[str, ...]
    hard_conflict_reasons: Tuple[str, ...]
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    confidence: float
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()
    must_not_link: bool = False
    must_not_link_reason_codes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidatePairV2(SerializableV2):
    """A high-recall, pre-classification claim pair.

    Candidate generation is intentionally a separate layer from the
    five-way relation decision.  A blocking reason is a recall signal only;
    it never authorizes an event merge.
    """

    candidate_id: str
    left_claim_id: str
    right_claim_id: str
    blocking_reasons: Tuple[str, ...]
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    score: float
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = P01_PIPELINE_VERSION
    ruleset_version: str = P01_RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EventV2(SerializableV2):
    event_id: str
    event_type: str
    core_entity_ids: Tuple[str, ...]
    actions: Tuple[str, ...]
    start_at: str
    end_at: str
    statuses: Tuple[str, ...]
    requests: Tuple[str, ...]
    participant_ids: Tuple[str, ...]
    claim_ids: Tuple[str, ...]
    mention_ids: Tuple[str, ...]
    supporting_evidence_refs: Tuple[EvidenceRefV2, ...]
    conflicting_evidence_refs: Tuple[EvidenceRefV2, ...]
    relation_decision_ids: Tuple[str, ...]
    confidence: float
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TopicFamilyV2(SerializableV2):
    topic_family_id: str
    family_key: str
    label: str
    event_ids: Tuple[str, ...]
    confidence: float
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TrendV2(SerializableV2):
    trend_id: str
    topic_family_id: str
    signal_key: str
    event_ids: Tuple[str, ...]
    claim_ids: Tuple[str, ...]
    confidence: float
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PresentationSentenceV2(SerializableV2):
    text: str
    claim_ids: Tuple[str, ...]
    message_ids: Tuple[str, ...]


@dataclass(frozen=True)
class PresentationV2(SerializableV2):
    presentation_id: str
    event_id: str
    presentation_role: str
    title: str
    summary: str
    title_support_claim_ids: Tuple[str, ...]
    sentences: Tuple[PresentationSentenceV2, ...]
    supported_claim_ids: Tuple[str, ...]
    source_message_ids: Tuple[str, ...]
    evidence_refs: Tuple[EvidenceRefV2, ...]
    confidence: float
    source: str
    provenance: ProvenanceV2
    analysis_run_id: str
    created_at: str
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    uncertainties: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticResultV2(SerializableV2):
    analysis_run_id: str
    created_at: str
    messages: Tuple[MessageV2, ...]
    mentions: Tuple[MentionV2, ...]
    claims: Tuple[ClaimV2, ...]
    pair_decisions: Tuple[PairDecisionV2, ...]
    events: Tuple[EventV2, ...]
    topic_families: Tuple[TopicFamilyV2, ...]
    trends: Tuple[TrendV2, ...]
    presentations: Tuple[PresentationV2, ...]
    warnings: Tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    ruleset_version: str = RULESET_VERSION
    source: str = SHADOW_SOURCE
    # Optional P0.1 layers.  They are empty on the original P0 path so the
    # existing result shape remains usable by the baseline runner.
    candidate_pairs: Tuple[Any, ...] = ()
    message_roles: Dict[str, str] = field(default_factory=dict)
    dialogue_segments: Tuple[Dict[str, Any], ...] = ()
    # Aggregate-only diagnostics for development replay.  It intentionally
    # contains counts and signal names, never message text or identity-bearing
    # IDs.
    candidate_diagnostics: Dict[str, Any] = field(default_factory=dict)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def stable_id(
    kind: str,
    identity: Mapping[str, Any],
    *,
    pipeline_version: str = PIPELINE_VERSION,
    ruleset_version: str = RULESET_VERSION,
) -> str:
    """Return an order-independent, version-scoped stable identifier.

    ``pipeline_version`` and ``ruleset_version`` are keyword-only so the P0
    API remains source compatible while P0.1 can mint IDs in its own namespace.
    """

    canonical = json.dumps(
        _jsonable(identity), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(
        (
            SCHEMA_VERSION
            + "|"
            + pipeline_version
            + "|"
            + ruleset_version
            + "|"
            + kind
            + "|"
            + canonical
        ).encode("utf-8")
    ).hexdigest()[:20]
    return "%s:%s" % (kind, digest)


def _timestamp_text(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return "unknown"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _raw_reply_id(raw: Mapping[str, Any]) -> Optional[str]:
    for key in ("reply_to_message_id", "quoted_message_id", "reference_message_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    nested = raw.get("raw_message")
    if isinstance(nested, Mapping):
        return _raw_reply_id(nested)
    return None


def _p01_scoped_boundary_id(
    kind: str,
    account_id: Any,
    chat_id: Any,
    boundary_id: Any,
) -> str:
    """Return an auditable boundary key isolated by account and chat.

    Upstream fixtures are allowed to provide short segment/block labels, but
    those labels are not globally unique.  Percent-encoding each component
    keeps the key readable while preventing delimiter collisions and, more
    importantly, makes ``segment-a`` in two chats two different scopes.
    """

    def _component(value: Any, fallback: str) -> str:
        text = str(value or "").strip() or fallback
        return quote(text, safe="")

    return "%s:account=%s|chat=%s|id=%s" % (
        str(kind or "boundary"),
        _component(account_id, "default"),
        _component(chat_id, "unknown"),
        _component(boundary_id, "unknown"),
    )


def legacy_messages_to_v2(
    messages: Iterable[Mapping[str, Any]],
    *,
    scope_boundaries: bool = False,
    allow_legacy_instance_aliases: bool = True,
) -> Tuple[MessageV2, ...]:
    """Map legacy message dictionaries without invoking legacy analysis.

    ``scope_boundaries`` is used only by the offline P0.1 path.  The original
    P0 mapper keeps caller-provided metadata byte-for-byte compatible, while
    P0.1 scopes supplied segment/block labels by account and chat before they
    can participate in relation blocking.
    """

    output: List[MessageV2] = []
    seen: set = set()
    for index, raw in enumerate(messages):
        message_id = str(raw.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("semantic_v2 requires every message to have message_id")
        if message_id in seen:
            raise ValueError("duplicate message_id in semantic_v2 input: %s" % message_id)
        seen.add(message_id)
        content = str(raw.get("content") or "")
        chat_id = str(raw.get("chat_id") or raw.get("chat_name") or "unknown-chat").strip()
        account_id = str(raw.get("account_id") or "default").strip() or "default"
        warnings: List[str] = []
        sender_id = str(raw.get("sender_id") or "").strip()
        sender_name = str(raw.get("sender_name") or "").strip()
        if raw.get("is_self") is True:
            sender_id, sender_name = "self", sender_name or "我"
        if not sender_id:
            sender_id = stable_id(
                "speaker",
                {"chat_id": chat_id, "sender_name": sender_name or "unknown", "scope": "legacy"},
            )
            warnings.append("speaker_id_missing:scoped_fallback")
        if not sender_name:
            sender_name = "待识别成员"
            warnings.append("speaker_name_missing")
        if not str(raw.get("timestamp") or "").strip():
            warnings.append("timestamp_missing")
        explicit_instance_id = None
        instance_keys = ("explicit_instance_id",)
        if allow_legacy_instance_aliases:
            # P0/P0.1 compatibility for offline fixtures.  P0.2 evaluation
            # callers disable these aliases so labels such as shared/event
            # instance keys cannot silently authorize a relation.
            instance_keys += ("shared_instance_id", "event_instance_id")
        for key in instance_keys:
            candidate = str(raw.get(key) or "").strip()
            if candidate:
                explicit_instance_id = candidate
                break
        dialogue_segment_id = None
        for key in ("dialogue_segment_id", "segment_id"):
            candidate = str(raw.get(key) or "").strip()
            if candidate:
                dialogue_segment_id = candidate
                break
        block_id = None
        for key in ("block_id", "conversation_block_id"):
            candidate = str(raw.get(key) or "").strip()
            if candidate:
                block_id = candidate
                break
        if scope_boundaries:
            if dialogue_segment_id:
                dialogue_segment_id = _p01_scoped_boundary_id(
                    "segment", account_id, chat_id, dialogue_segment_id
                )
            if block_id:
                block_id = _p01_scoped_boundary_id(
                    "block", account_id, chat_id, block_id
                )
        output.append(
            MessageV2(
                message_id=message_id,
                chat_id=chat_id,
                speaker_id=sender_id,
                speaker_name=sender_name,
                content=content,
                timestamp=_timestamp_text(raw.get("timestamp")),
                reply_to_message_id=_raw_reply_id(raw),
                compatibility_warnings=tuple(warnings),
                explicit_instance_id=explicit_instance_id,
                dialogue_segment_id=dialogue_segment_id,
                block_id=block_id,
                account_id=account_id,
                attribution=(
                    str(
                        raw.get("attribution")
                        or raw.get("claim_attribution")
                        or raw.get("source_attribution")
                        or "direct"
                    ).strip()
                    or "direct"
                ),
                position_in_block=(
                    int(raw["position_in_block"])
                    if raw.get("position_in_block") is not None
                    and str(raw.get("position_in_block")).strip() != ""
                    and str(raw.get("position_in_block")).strip().lstrip("-").isdigit()
                    else None
                ),
            )
        )
    return tuple(sorted(output, key=lambda item: item.message_id))


@dataclass(frozen=True)
class _EntityRule:
    normalized_id: str
    family_key: str
    label: str
    pattern: re.Pattern


_ENTITY_RULES = (
    _EntityRule("service:gpt", "ai_services", "GPT", re.compile(r"(?<![a-z0-9])(?:chatgpt|gpt)(?![a-z0-9])", re.I)),
    _EntityRule("service:codex", "ai_services", "Codex", re.compile(r"(?<![a-z0-9])codex(?![a-z0-9])|code\s*x", re.I)),
    _EntityRule("service:multica", "ai_services", "multica", re.compile(r"(?<![a-z0-9])multica(?![a-z0-9])", re.I)),
    _EntityRule("service:relay", "ai_services", "中转站", re.compile(r"中转站|中转服务|中转商", re.I)),
    _EntityRule("platform:wechat", "messaging_platforms", "微信", re.compile(r"企业微信|微信|wechat", re.I)),
    _EntityRule("platform:github", "developer_accounts", "GitHub", re.compile(r"(?<![a-z0-9])github(?![a-z0-9])", re.I)),
    _EntityRule("site:linux_do", "developer_accounts", "Linux.do", re.compile(r"linux\s*\.\s*do|linuxdo", re.I)),
    _EntityRule("site:v2ex", "developer_accounts", "V2EX", re.compile(r"(?<![a-z0-9])v2ex(?![a-z0-9])", re.I)),
    _EntityRule("site:linux_related", "developer_accounts", "Linux相关网站", re.compile(r"linux\s*相关网站|linuxsb", re.I)),
    _EntityRule("channel:email", "developer_accounts", "邮箱", re.compile(r"邮箱|邮件|验证码邮件|qq\s*邮箱", re.I)),
)

_FAMILY_LABELS = {
    "ai_services": "AI 服务",
    "messaging_platforms": "消息平台",
    "developer_accounts": "开发者账号与网站",
    "unknown": "待分类",
}
_ENTITY_BY_ID = {rule.normalized_id: rule for rule in _ENTITY_RULES}


@dataclass(frozen=True)
class _ActionRule:
    action: str
    label: str
    pattern: re.Pattern


_ACTION_RULES = (
    _ActionRule("reset", "重置", re.compile(r"重置|reset", re.I)),
    _ActionRule("cost", "成本或价格比较", re.compile(r"性价比|划算|价格|收费|太贵|成本", re.I)),
    _ActionRule("usage", "消耗或用量", re.compile(r"消耗|用量|耗得|额度(?:掉|降|少)|token.{0,6}(?:多|快)", re.I)),
    _ActionRule("risk", "风控或合规", re.compile(r"风控|合规|封号|封禁|安全风险", re.I)),
    _ActionRule("register", "注册", re.compile(r"注册|创建账号|申请账号", re.I)),
    _ActionRule("email_delivery", "邮件接收失败", re.compile(r"收不到.{0,8}(?:邮件|邮箱|验证码)|(?:邮件|验证码).{0,8}(?:没到|不来|失败)", re.I)),
    _ActionRule("outage", "故障", re.compile(r"故障|崩了|宕机|不可用|用不了|报错", re.I)),
)
_ACTION_LABELS = {rule.action: rule.label for rule in _ACTION_RULES}

_CLAUSE_PATTERN = re.compile(r"[^，,。；;！？!?\n]+[，,。；;！？!?\n]*")
_QUESTION = re.compile(r"[？?]|(?:吗|么|如何|怎么|为什么|是否|能否|有没有)\s*$")
_SUGGESTION = re.compile(r"建议|应该|最好|不如|需要|可以先|尽量")
_HYPOTHESIS = re.compile(r"可能|也许|或许|大概|估计|听说|据说|似乎")
_OPINION = re.compile(r"我觉得|我认为|感觉|没有性价比|没性价比|不划算|担心|讨厌")
_NEGATIVE = re.compile(r"没有|没法|不能|不行|收不到|用不了|失败|不划算|没性价比")
_RECOVERED = re.compile(r"恢复了|已恢复|恢复正常|现在好了")
_ONGOING = re.compile(r"仍然|还在|不停|反复|一直|又")
_TIME_PATTERN = re.compile(
    r"今天|明天|昨天|本周|下周|上周|"
    r"\d{4}[年/-]\d{1,2}(?:[月/-]\d{1,2}日?)?|"
    r"\d{1,2}月\d{1,2}日|\d{1,2}[点时](?:\d{1,2}分)?"
)


def _clauses(content: str) -> List[Tuple[int, int, str]]:
    values = []
    for match in _CLAUSE_PATTERN.finditer(content):
        start, end = match.span()
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        stripped = re.sub(r"^(?:而且|并且|同时|然后|另外)\s*", "", stripped)
        if not stripped:
            continue
        adjusted = content.find(stripped, start, end)
        if adjusted < 0:
            adjusted = start + leading
        values.append((adjusted, adjusted + len(stripped), stripped))
    return values


def _mention(
    message: MessageV2,
    start: int,
    end: int,
    mention_type: str,
    normalized_id: str,
    action: Optional[str],
    time_value: Optional[str],
    status: Optional[str],
    request: Optional[str],
    analysis_run_id: str,
    created_at: str,
    confidence: float = 0.96,
    *,
    pipeline_version: str = PIPELINE_VERSION,
    ruleset_version: str = RULESET_VERSION,
    provenance_stage: str = "mention_extraction",
) -> MentionV2:
    if start < 0 or end <= start or message.content[start:end] == "":
        raise ValueError("mention evidence span must locate non-empty source text")
    evidence_text = message.content[start:end]
    evidence = EvidenceRefV2(message.message_id, start, end, evidence_text)
    mention_id = stable_id(
        "mention",
        {
            "message_id": message.message_id,
            "span": [start, end],
            "type": mention_type,
            "normalized_id": normalized_id,
            "action": action,
            "time_value": time_value,
            "status": status,
            "request": request,
        },
        pipeline_version=pipeline_version,
        ruleset_version=ruleset_version,
    )
    provenance_input_ids: Tuple[str, ...] = (message.message_id,)
    # P0.1 and P0.2 both carry scoped boundaries in provenance.  Keep the
    # source message as the first input and append only the boundaries owned
    # by that message; otherwise a direct P0.2 stage call could emit an object
    # whose provenance is technically valid-looking but cannot be replayed.
    if pipeline_version in {P01_PIPELINE_VERSION, P02_PIPELINE_VERSION}:
        boundary_ids = tuple(
            value
            for value in (message.dialogue_segment_id, message.block_id)
            if value
        )
        provenance_input_ids = tuple(dict.fromkeys(provenance_input_ids + boundary_ids))
    return MentionV2(
        mention_id=mention_id,
        message_id=message.message_id,
        span_start=start,
        span_end=end,
        evidence_text=evidence_text,
        mention_type=mention_type,
        normalized_id=normalized_id,
        surface_text=evidence_text,
        action=action,
        time_value=time_value,
        status=status,
        request=request,
        confidence=confidence,
        source_message_ids=(message.message_id,),
        evidence_refs=(evidence,),
        provenance=ProvenanceV2(
            provenance_input_ids, provenance_stage, ruleset_version
        ),
        analysis_run_id=analysis_run_id,
        created_at=created_at,
        pipeline_version=pipeline_version,
        ruleset_version=ruleset_version,
    )


def _claim_type(text: str) -> str:
    if _QUESTION.search(text):
        return "question"
    if _SUGGESTION.search(text):
        return "suggestion"
    if _HYPOTHESIS.search(text):
        return "hypothesis"
    if _OPINION.search(text):
        return "opinion"
    return "fact"


def _status(text: str) -> str:
    if _RECOVERED.search(text):
        return "recovered"
    if _ONGOING.search(text):
        return "recurring"
    if _QUESTION.search(text):
        return "unknown"
    if _NEGATIVE.search(text):
        return "failed_or_negative"
    return "reported"


def _request(action: str, claim_type: str, text: str) -> str:
    if claim_type == "question":
        return "seek_information"
    if action == "cost":
        return "compare_cost"
    if action in {"register", "email_delivery"} and re.search(r"求助|帮忙|怎么办|如何|怎么", text):
        return "seek_help"
    if claim_type == "suggestion":
        return "recommend_action"
    return "report_state"


def extract_mentions_and_claims(
    messages: Sequence[MessageV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[Tuple[MentionV2, ...], Tuple[ClaimV2, ...]]:
    mentions: List[MentionV2] = []
    claims: List[ClaimV2] = []
    for message in sorted(messages, key=lambda item: item.message_id):
        entity_mentions: List[MentionV2] = []
        for rule in _ENTITY_RULES:
            for match in rule.pattern.finditer(message.content):
                entity_mentions.append(
                    _mention(
                        message, match.start(), match.end(), "entity", rule.normalized_id,
                        None, None, None, None, analysis_run_id, created_at,
                    )
                )
        entity_mentions.sort(key=lambda item: (item.span_start, item.span_end, item.normalized_id))
        mentions.extend(entity_mentions)
        for match in _TIME_PATTERN.finditer(message.content):
            mentions.append(
                _mention(
                    message, match.start(), match.end(), "time",
                    "time:" + _normalized_text(match.group(0)), None,
                    match.group(0), None, None, analysis_run_id, created_at, 0.92,
                )
            )

        previous_entity_mentions: Tuple[MentionV2, ...] = ()
        for start, end, clause in _clauses(message.content):
            local_entity_mentions = tuple(
                item
                for item in entity_mentions
                if item.span_start < end and item.span_end > start
            )
            inherited_target = not local_entity_mentions and bool(previous_entity_mentions)
            if local_entity_mentions:
                previous_entity_mentions = local_entity_mentions
            active_entity_mentions = local_entity_mentions or previous_entity_mentions
            targets = tuple(sorted({item.normalized_id for item in active_entity_mentions}))
            for action_rule in _ACTION_RULES:
                for action_match in action_rule.pattern.finditer(clause):
                    absolute_start = start + action_match.start()
                    absolute_end = start + action_match.end()
                    status = _status(clause)
                    claim_type = _claim_type(clause)
                    request = _request(action_rule.action, claim_type, clause)
                    action_mention = _mention(
                        message, absolute_start, absolute_end, "event_trigger",
                        "action:" + action_rule.action, action_rule.action, None, status,
                        request, analysis_run_id, created_at, 0.94,
                    )
                    mentions.append(action_mention)
                    # Mention provenance is clause-local. If the clause omits
                    # its object, inherit only the immediately preceding
                    # clause's concrete mentions, never every same-named
                    # entity occurrence elsewhere in the message.
                    target_mentions = tuple(item.mention_id for item in active_entity_mentions)
                    evidence = EvidenceRefV2(message.message_id, start, end, message.content[start:end])
                    antecedent_evidence = tuple(
                        evidence_ref
                        for item in active_entity_mentions
                        for evidence_ref in item.evidence_refs
                    ) if inherited_target else ()
                    claim_evidence_refs = tuple(
                        sorted(
                            set((evidence,) + antecedent_evidence),
                            key=lambda item: (item.message_id, item.span_start, item.span_end),
                        )
                    )
                    uncertainties = (
                        ("target_inherited_from_previous_clause",)
                        if inherited_target
                        else (() if targets else ("core_entity_unknown",))
                    )
                    claim_id = stable_id(
                        "claim",
                        {
                            "message_id": message.message_id,
                            "span": [start, end],
                            "speaker_id": message.speaker_id,
                            "targets": sorted(targets),
                            "target_mentions": sorted(target_mentions),
                            "action": action_rule.action,
                            "request": request,
                            "claim_type": claim_type,
                            "status": status,
                            "claim_text": _normalized_text(clause),
                        },
                    )
                    claims.append(
                        ClaimV2(
                            claim_id=claim_id,
                            speaker_id=message.speaker_id,
                            speaker_name=message.speaker_name,
                            claim_text=message.content[start:end],
                            claim_type=claim_type,
                            target_entity_ids=targets,
                            event_mention_ids=tuple(sorted(set(target_mentions + (action_mention.mention_id,)))),
                            action=action_rule.action,
                            request=request,
                            stance_or_polarity="negative" if _NEGATIVE.search(clause) else "neutral_or_positive",
                            status_or_modality=status,
                            timestamp=message.timestamp,
                            message_id=message.message_id,
                            reply_to_message_id=message.reply_to_message_id,
                            evidence_span=evidence,
                            confidence=0.84 if inherited_target else (0.9 if targets else 0.62),
                            source_message_ids=(message.message_id,),
                            evidence_refs=claim_evidence_refs,
                            provenance=ProvenanceV2(
                                tuple(sorted(set(target_mentions + (action_mention.mention_id,)))),
                                "claim_extraction",
                            ),
                            analysis_run_id=analysis_run_id,
                            created_at=created_at,
                            uncertainties=uncertainties,
                        )
                    )
    unique_mentions = {item.mention_id: item for item in mentions}
    unique_claims = {item.claim_id: item for item in claims}
    return (
        tuple(sorted(unique_mentions.values(), key=lambda item: item.mention_id)),
        tuple(sorted(unique_claims.values(), key=lambda item: item.claim_id)),
    )


def _families_for_entities(entity_ids: Sequence[str]) -> Tuple[str, ...]:
    return tuple(
        sorted(
            {
                _ENTITY_BY_ID[entity_id].family_key
                for entity_id in entity_ids
                if entity_id in _ENTITY_BY_ID
            }
        )
    )


def _parsed_timestamp(value: str) -> Optional[datetime]:
    if not value or value == "unknown":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_claim_pair(
    left: ClaimV2,
    right: ClaimV2,
    analysis_run_id: str,
    created_at: str,
) -> PairDecisionV2:
    """Classify a candidate pair using explicit slots and hard conflicts."""

    if left.claim_id == right.claim_id:
        raise ValueError("candidate pair requires two distinct claims")
    left_targets, right_targets = set(left.target_entity_ids), set(right.target_entity_ids)
    shared_targets = left_targets.intersection(right_targets)
    left_families = set(_families_for_entities(left.target_entity_ids))
    right_families = set(_families_for_entities(right.target_entity_ids))
    shared_families = left_families.intersection(right_families)
    support: List[str] = []
    conflicts: List[str] = []
    hard: List[str] = []
    uncertainties: List[str] = []

    if shared_targets:
        support.append("core_entity")
    elif left_targets and right_targets:
        conflicts.append("core_entity")
        hard.append("different_core_entity")
    else:
        uncertainties.append("core_entity_missing")

    if left.action == right.action:
        support.append("action")
    else:
        conflicts.append("action")
        hard.append("different_action")
    if left.request == right.request:
        support.append("request")
    else:
        conflicts.append("request")
        hard.append("different_request")

    incompatible_status = {
        left.status_or_modality,
        right.status_or_modality,
    } == {"recovered", "recurring"} or {
        left.status_or_modality,
        right.status_or_modality,
    } == {"recovered", "failed_or_negative"}
    if incompatible_status:
        conflicts.append("status")
        hard.append("incompatible_status")
    elif left.status_or_modality == right.status_or_modality:
        support.append("status")

    same_message = left.message_id == right.message_id
    explicit_reply = left.reply_to_message_id == right.message_id or right.reply_to_message_id == left.message_id
    if same_message:
        support.append("same_message")
    if explicit_reply:
        support.append("explicit_reply")
    left_time = _parsed_timestamp(left.timestamp)
    right_time = _parsed_timestamp(right.timestamp)
    if left_time is None or right_time is None:
        uncertainties.append("timestamp_missing_or_unparseable")
    else:
        time_gap = abs((left_time - right_time).total_seconds())
        if time_gap <= 24 * 60 * 60:
            support.append("time_window")
        elif explicit_reply:
            support.append("long_range_explicit_reply")
        else:
            conflicts.append("time")
            hard.append("time_window_conflict")

    if (
        not left_targets
        or not right_targets
        or ("timestamp_missing_or_unparseable" in uncertainties and not (same_message or explicit_reply))
    ):
        relation = RELATION_INSUFFICIENT_CONTEXT
        confidence = 0.64
    elif hard:
        if shared_targets or explicit_reply:
            relation = RELATION_RELATED_EVENT
            confidence = 0.93
        elif shared_families:
            relation = RELATION_SAME_TOPIC_ONLY
            confidence = 0.96
        else:
            relation = RELATION_UNRELATED
            confidence = 0.96
    elif shared_targets and left.action == right.action and left.request == right.request:
        if same_message or explicit_reply:
            relation = RELATION_SAME_EVENT
            confidence = 0.94 if explicit_reply else 0.92
        else:
            # Matching slots are not an event identity. Two people can report
            # separate resets of the same service within minutes of each
            # other. P0 therefore requires an explicit message-level link;
            # without one, the safest result is a related event edge.
            relation = RELATION_RELATED_EVENT
            confidence = 0.9
            uncertainties.append("missing_explicit_event_linkage")
    elif shared_targets:
        relation = RELATION_RELATED_EVENT
        confidence = 0.9
    elif shared_families:
        relation = RELATION_SAME_TOPIC_ONLY
        confidence = 0.9
    else:
        relation = RELATION_UNRELATED
        confidence = 0.94

    left_id, right_id = sorted((left.claim_id, right.claim_id))
    evidence = tuple(
        sorted(
            {item for item in left.evidence_refs + right.evidence_refs},
            key=lambda item: (item.message_id, item.span_start, item.span_end),
        )
    )
    decision_id = stable_id(
        "pair",
        {"left": left_id, "right": right_id, "relation": relation, "hard": sorted(set(hard))},
    )
    return PairDecisionV2(
        decision_id=decision_id,
        left_claim_id=left_id,
        right_claim_id=right_id,
        relation=relation,
        supporting_slots=tuple(sorted(set(support))),
        conflicting_slots=tuple(sorted(set(conflicts))),
        hard_conflict_reasons=tuple(sorted(set(hard))),
        source_message_ids=tuple(sorted({left.message_id, right.message_id})),
        evidence_refs=evidence,
        confidence=confidence,
        provenance=ProvenanceV2((left_id, right_id), "pair_classification"),
        analysis_run_id=analysis_run_id,
        created_at=created_at,
        uncertainties=tuple(sorted(set(uncertainties))),
    )


def classify_candidate_pairs(
    claims: Sequence[ClaimV2], analysis_run_id: str, created_at: str
) -> Tuple[PairDecisionV2, ...]:
    values: List[PairDecisionV2] = []
    ordered = sorted(claims, key=lambda item: item.claim_id)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            values.append(classify_claim_pair(left, right, analysis_run_id, created_at))
    return tuple(sorted(values, key=lambda item: item.decision_id))


def _evidence_union(claims: Sequence[ClaimV2]) -> Tuple[EvidenceRefV2, ...]:
    return tuple(
        sorted(
            {evidence for claim in claims for evidence in claim.evidence_refs},
            key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
        )
    )


def build_events(
    claims: Sequence[ClaimV2],
    pair_decisions: Sequence[PairDecisionV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[EventV2, ...]:
    """Build conservative all-pairs-compatible event groups from claims."""

    if any(not claim.evidence_refs or not claim.source_message_ids for claim in claims):
        raise ValueError("event claims must carry source evidence")
    decisions = {
        frozenset((item.left_claim_id, item.right_claim_id)): item
        for item in pair_decisions
    }
    groups: List[List[ClaimV2]] = []
    for claim in sorted(claims, key=lambda item: item.claim_id):
        placed = False
        for group in groups:
            relations = [
                decisions.get(frozenset((claim.claim_id, member.claim_id)))
                for member in group
            ]
            if relations and all(item is not None and item.relation == RELATION_SAME_EVENT for item in relations):
                group.append(claim)
                placed = True
                break
        if not placed:
            groups.append([claim])

    events: List[EventV2] = []
    for group in groups:
        group = sorted(group, key=lambda item: item.claim_id)
        claim_ids = tuple(item.claim_id for item in group)
        entity_ids = tuple(sorted({value for item in group for value in item.target_entity_ids}))
        actions = tuple(sorted({item.action for item in group}))
        evidence = _evidence_union(group)
        message_ids = tuple(sorted({item.message_id for item in group}))
        mention_ids = tuple(sorted({value for item in group for value in item.event_mention_ids}))
        timestamps = sorted(item.timestamp for item in group)
        related_decisions = tuple(
            sorted(
                item.decision_id
                for item in pair_decisions
                if item.left_claim_id in claim_ids and item.right_claim_id in claim_ids
            )
        )
        event_id = stable_id(
            "event",
            {
                "claim_ids": sorted(claim_ids),
                "core_entities": sorted(entity_ids),
                "actions": sorted(actions),
            },
        )
        events.append(
            EventV2(
                event_id=event_id,
                event_type=actions[0] if len(actions) == 1 else "compound_unknown",
                core_entity_ids=entity_ids,
                actions=actions,
                start_at=timestamps[0] if timestamps else "unknown",
                end_at=timestamps[-1] if timestamps else "unknown",
                statuses=tuple(sorted({item.status_or_modality for item in group})),
                requests=tuple(sorted({item.request for item in group})),
                participant_ids=tuple(sorted({item.speaker_id for item in group})),
                claim_ids=claim_ids,
                mention_ids=mention_ids,
                supporting_evidence_refs=evidence,
                conflicting_evidence_refs=(),
                relation_decision_ids=related_decisions,
                confidence=min(item.confidence for item in group),
                source_message_ids=message_ids,
                evidence_refs=evidence,
                provenance=ProvenanceV2(claim_ids, "event_construction"),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                uncertainties=tuple(sorted({value for item in group for value in item.uncertainties})),
            )
        )
    return tuple(sorted(events, key=lambda item: item.event_id))


def derive_topic_families(
    events: Sequence[EventV2], claims: Sequence[ClaimV2], analysis_run_id: str, created_at: str
) -> Tuple[TopicFamilyV2, ...]:
    claim_by_id = {item.claim_id: item for item in claims}
    buckets: Dict[str, List[EventV2]] = {}
    for event in events:
        families = _families_for_entities(event.core_entity_ids) or ("unknown",)
        for family in families:
            buckets.setdefault(family, []).append(event)
    output: List[TopicFamilyV2] = []
    for family_key, family_events in sorted(buckets.items()):
        event_ids = tuple(sorted(item.event_id for item in family_events))
        family_claims = [
            claim_by_id[claim_id]
            for event in family_events
            for claim_id in event.claim_ids
            if claim_id in claim_by_id
        ]
        evidence = _evidence_union(family_claims)
        output.append(
            TopicFamilyV2(
                topic_family_id=stable_id("topic_family", {"family_key": family_key}),
                family_key=family_key,
                label=_FAMILY_LABELS.get(family_key, family_key),
                event_ids=event_ids,
                confidence=0.95 if family_key != "unknown" else 0.4,
                source_message_ids=tuple(sorted({item.message_id for item in family_claims})),
                evidence_refs=evidence,
                provenance=ProvenanceV2(event_ids, "topic_family_derivation"),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                uncertainties=("family_unknown",) if family_key == "unknown" else (),
            )
        )
    return tuple(output)


def derive_trends(
    events: Sequence[EventV2], topic_families: Sequence[TopicFamilyV2], claims: Sequence[ClaimV2],
    analysis_run_id: str, created_at: str,
) -> Tuple[TrendV2, ...]:
    """Emit only repeated signals; a trend never replaces constituent events."""

    event_by_id = {item.event_id: item for item in events}
    claim_by_id = {item.claim_id: item for item in claims}
    output: List[TrendV2] = []
    for family in topic_families:
        by_action: Dict[str, List[EventV2]] = {}
        for event_id in family.event_ids:
            event = event_by_id[event_id]
            for action in event.actions:
                by_action.setdefault(action, []).append(event)
        for action, action_events in sorted(by_action.items()):
            if len(action_events) < 2:
                continue
            event_ids = tuple(sorted(item.event_id for item in action_events))
            claim_ids = tuple(sorted({value for item in action_events for value in item.claim_ids}))
            trend_claims = [claim_by_id[value] for value in claim_ids]
            evidence = _evidence_union(trend_claims)
            output.append(
                TrendV2(
                    trend_id=stable_id("trend", {"family": family.topic_family_id, "action": action, "events": event_ids}),
                    topic_family_id=family.topic_family_id,
                    signal_key=action,
                    event_ids=event_ids,
                    claim_ids=claim_ids,
                    confidence=0.7,
                    source_message_ids=tuple(sorted({item.message_id for item in trend_claims})),
                    evidence_refs=evidence,
                    provenance=ProvenanceV2(event_ids, "trend_derivation"),
                    analysis_run_id=analysis_run_id,
                    created_at=created_at,
                    uncertainties=("trend_is_candidate_not_fact",),
                )
            )
    return tuple(sorted(output, key=lambda item: item.trend_id))


def _entity_label(entity_id: str) -> str:
    rule = _ENTITY_BY_ID.get(entity_id)
    return rule.label if rule else "对象待确认"


def derive_presentations(
    events: Sequence[EventV2], claims: Sequence[ClaimV2], analysis_run_id: str, created_at: str
) -> Tuple[PresentationV2, ...]:
    claim_by_id = {item.claim_id: item for item in claims}
    output: List[PresentationV2] = []
    for event in events:
        event_claims = [claim_by_id[value] for value in event.claim_ids]
        if not event_claims:
            raise ValueError("presentation cannot be created for an event without claims")
        evidence = _evidence_union(event_claims)
        message_ids = tuple(sorted({item.message_id for item in event_claims}))
        if evidence != event.evidence_refs or message_ids != event.source_message_ids:
            raise ValueError("presentation evidence must equal, not expand, event evidence")
        entity_text = "、".join(_entity_label(value) for value in event.core_entity_ids[:2])
        action_text = "、".join(_ACTION_LABELS.get(value, value) for value in event.actions)
        title = "%s：%s" % (entity_text or "对象待确认", action_text or "事项待确认")
        sentences = tuple(
            PresentationSentenceV2(
                text="%s：%s" % (item.speaker_name, item.claim_text),
                claim_ids=(item.claim_id,),
                message_ids=(item.message_id,),
            )
            for item in event_claims
        )
        summary = "；".join(item.text for item in sentences)
        presentation_id = stable_id("presentation", {"event_id": event.event_id, "role": "brief"})
        output.append(
            PresentationV2(
                presentation_id=presentation_id,
                event_id=event.event_id,
                presentation_role="brief",
                title=title,
                summary=summary,
                title_support_claim_ids=event.claim_ids,
                sentences=sentences,
                supported_claim_ids=event.claim_ids,
                source_message_ids=message_ids,
                evidence_refs=evidence,
                confidence=event.confidence,
                source=SHADOW_SOURCE,
                provenance=ProvenanceV2((event.event_id,) + event.claim_ids, "presentation_derivation"),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                uncertainties=event.uncertainties,
            )
        )
    return tuple(sorted(output, key=lambda item: item.presentation_id))


def _default_created_at(messages: Sequence[MessageV2]) -> str:
    timestamps = sorted(item.timestamp for item in messages if item.timestamp != "unknown")
    return timestamps[-1] if timestamps else "1970-01-01T00:00:00+00:00"


def run_semantic_pipeline(
    legacy_messages: Iterable[Mapping[str, Any]],
    *,
    analysis_run_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> SemanticResultV2:
    """Run the deterministic P0 rule baseline without side effects."""

    messages = legacy_messages_to_v2(legacy_messages)
    message_identity = tuple(
        sorted(
            stable_id(
                "message_input",
                {
                    "message_id": item.message_id,
                    "chat_id": item.chat_id,
                    "speaker_id": item.speaker_id,
                    "content": _normalized_text(item.content),
                    "timestamp": item.timestamp,
                    "reply_to": item.reply_to_message_id,
                    "explicit_instance_id": item.explicit_instance_id,
                },
            )
            for item in messages
        )
    )
    resolved_run_id = analysis_run_id or stable_id("analysis_run", {"messages": message_identity})
    resolved_created_at = _timestamp_text(created_at) if created_at else _default_created_at(messages)
    mentions, claims = extract_mentions_and_claims(messages, resolved_run_id, resolved_created_at)
    pair_decisions = classify_candidate_pairs(claims, resolved_run_id, resolved_created_at)
    events = build_events(claims, pair_decisions, resolved_run_id, resolved_created_at)
    families = derive_topic_families(events, claims, resolved_run_id, resolved_created_at)
    trends = derive_trends(events, families, claims, resolved_run_id, resolved_created_at)
    presentations = derive_presentations(events, claims, resolved_run_id, resolved_created_at)
    warnings = tuple(
        sorted(
            "%s:%s" % (message.message_id, warning)
            for message in messages
            for warning in message.compatibility_warnings
        )
    )
    return SemanticResultV2(
        analysis_run_id=resolved_run_id,
        created_at=resolved_created_at,
        messages=messages,
        mentions=mentions,
        claims=claims,
        pair_decisions=pair_decisions,
        events=events,
        topic_families=families,
        trends=trends,
        presentations=presentations,
        warnings=warnings,
    )


def v2_result_to_legacy_preview(result: SemanticResultV2) -> Dict[str, Any]:
    """Return an explicit shadow preview; never masquerade as production AI."""

    return {
        "ok": True,
        "source": SHADOW_SOURCE,
        "schema_version": result.schema_version,
        "pipeline_version": result.pipeline_version,
        "ruleset_version": result.ruleset_version,
        "analysis_run_id": result.analysis_run_id,
        "created_at": result.created_at,
        "cards": [item.to_dict() for item in result.presentations],
        "semantic_graph": {
            "mentions": [item.to_dict() for item in result.mentions],
            "claims": [item.to_dict() for item in result.claims],
            "pair_decisions": [item.to_dict() for item in result.pair_decisions],
            "events": [item.to_dict() for item in result.events],
            "topic_families": [item.to_dict() for item in result.topic_families],
            "trends": [item.to_dict() for item in result.trends],
        },
        "warnings": list(result.warnings),
        "read_only": True,
        "production_connected": False,
    }


def score_pairwise_relations(
    predicted: Sequence[PairDecisionV2],
    gold_relations: Mapping[Tuple[str, str], str],
) -> Dict[str, Any]:
    """Score same-event pairwise precision/recall and typed relation accuracy."""

    predicted_by_pair = {
        tuple(sorted((item.left_claim_id, item.right_claim_id))): item.relation
        for item in predicted
    }
    normalized_gold = {
        tuple(sorted((str(pair[0]), str(pair[1])))): relation
        for pair, relation in gold_relations.items()
    }
    for relation in normalized_gold.values():
        if relation not in EVENT_RELATIONS:
            raise ValueError("unknown gold relation: %s" % relation)
    tp = fp = fn = correct = 0
    rows = []
    for pair, gold in sorted(normalized_gold.items()):
        predicted_relation = predicted_by_pair.get(pair, RELATION_INSUFFICIENT_CONTEXT)
        if gold == RELATION_SAME_EVENT and predicted_relation == RELATION_SAME_EVENT:
            tp += 1
        elif gold != RELATION_SAME_EVENT and predicted_relation == RELATION_SAME_EVENT:
            fp += 1
        elif gold == RELATION_SAME_EVENT and predicted_relation != RELATION_SAME_EVENT:
            fn += 1
        correct += int(gold == predicted_relation)
        rows.append({"pair": list(pair), "gold": gold, "predicted": predicted_relation})
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "pair_count": len(normalized_gold),
        "pairwise_precision": round(precision, 6),
        "pairwise_recall": round(recall, 6),
        "pairwise_f1": round(f1, 6),
        "typed_relation_accuracy": round(correct / len(normalized_gold), 6) if normalized_gold else 1.0,
        "over_merge_count": fp,
        "over_split_count": fn,
        "over_merge_rate": round(fp / len(normalized_gold), 6) if normalized_gold else 0.0,
        "over_split_rate": round(fn / len(normalized_gold), 6) if normalized_gold else 0.0,
        "rows": rows,
    }


def score_must_not_link(
    events: Sequence[EventV2], must_not_link: Iterable[Tuple[str, str]]
) -> Dict[str, Any]:
    """Report catastrophic merges of claim pairs that must remain separate."""

    event_by_claim = {
        claim_id: event.event_id for event in events for claim_id in event.claim_ids
    }
    pairs = sorted({tuple(sorted((str(left), str(right)))) for left, right in must_not_link})
    violations = [
        pair
        for pair in pairs
        if event_by_claim.get(pair[0]) is not None
        and event_by_claim.get(pair[0]) == event_by_claim.get(pair[1])
    ]
    return {
        "must_not_link_count": len(pairs),
        "violation_count": len(violations),
        "violation_rate": round(len(violations) / len(pairs), 6) if pairs else 0.0,
        "violations": [list(pair) for pair in violations],
    }


# ---------------------------------------------------------------------------
# P0.1: high-recall, dialogue-aware shadow refinement
# ---------------------------------------------------------------------------

# These rules deliberately live beside, rather than mutate, the P0 rules.  A
# development experiment can therefore compare P0 and P0.1 replays without
# changing the original IDs or production analysis path.
@dataclass(frozen=True)
class _P01EntityRule:
    normalized_id: str
    family_key: str
    label: str
    pattern: re.Pattern


@dataclass(frozen=True)
class _P01ActionRule:
    action: str
    pattern: re.Pattern


_P01_ENTITY_RULES = tuple(
    _P01EntityRule(
        rule.normalized_id, rule.family_key, rule.label, rule.pattern
    )
    for rule in _ENTITY_RULES
) + (
    # Variant-specific aliases are canonicalized to the same service IDs.
    # The P0 boundary intentionally remains unchanged for replay comparability.
    _P01EntityRule(
        "service:gpt", "ai_services", "GPT",
        re.compile(r"(?<![a-z0-9])(?:chat\s*gpt|gpt(?:[-_ ]?\d+)?)(?![a-z0-9])", re.I),
    ),
    _P01EntityRule(
        "service:claude", "ai_services", "Claude",
        re.compile(r"(?<![a-z0-9])claude(?:[-_ ]?\d+)?(?![a-z0-9])", re.I),
    ),
    _P01EntityRule(
        "service:deepseek", "ai_services", "DeepSeek",
        re.compile(r"(?<![a-z0-9])deepseek(?![a-z0-9])|深度求索", re.I),
    ),
    _P01EntityRule(
        "service:openai", "ai_services", "OpenAI",
        re.compile(r"(?<![a-z0-9])open\s*ai(?![a-z0-9])", re.I),
    ),
    _P01EntityRule(
        "object:model", "ai_services", "模型",
        re.compile(r"大模型|模型|人工智能|(?<![a-z0-9])ai(?![a-z0-9])", re.I),
    ),
    _P01EntityRule(
        "object:interface", "development_tooling", "接口",
        re.compile(r"接口|(?<![a-z0-9])api(?![a-z0-9])", re.I),
    ),
    _P01EntityRule(
        "object:database", "development_tooling", "数据库",
        re.compile(r"数据库|数据表|数据仓库", re.I),
    ),
    _P01EntityRule(
        "object:server", "development_tooling", "服务器",
        re.compile(r"服务器|主机|服务端", re.I),
    ),
    _P01EntityRule(
        "object:project_board", "project_work", "项目看板",
        re.compile(r"项目看板|工作看板", re.I),
    ),
    _P01EntityRule(
        "object:project", "project_work", "项目",
        re.compile(r"项目|需求|方案|交付|上线", re.I),
    ),
    _P01EntityRule(
        "object:course", "project_work", "课程安排",
        re.compile(r"课程|选课|课表|教务系统", re.I),
    ),
    _P01EntityRule(
        "object:backup", "project_work", "备份",
        re.compile(r"数据库备份|备份", re.I),
    ),
    _P01EntityRule(
        "object:quote", "project_work", "报价",
        re.compile(r"报价|报价单|费用单|订单", re.I),
    ),
    _P01EntityRule(
        "object:account", "developer_accounts", "账号",
        re.compile(r"账号|账户|登录账号", re.I),
    ),
    _P01EntityRule(
        "object:password", "developer_accounts", "密码",
        re.compile(r"密码", re.I),
    ),
    _P01EntityRule(
        "object:link", "developer_accounts", "链接",
        re.compile(r"链接|网址|外链", re.I),
    ),
    _P01EntityRule(
        "object:log", "development_tooling", "日志",
        re.compile(r"日志|错误日志|报错日志", re.I),
    ),
)
# Development gold uses a stable, source-independent namespace for common
# aliases.  Put these rules first so a canonical alias wins over the legacy
# P0 spelling when both cover the same surface.  Less common objects retain a
# hashed unknown namespace rather than copying arbitrary text into an ID.
_P01_ENTITY_RULES = (
    _P01EntityRule("ENTITY_GPT", "ai_services", "GPT", re.compile(r"(?<![a-z0-9])(?:chat\s*gpt|gpt(?:[-_ ]?\d+)?)(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_CODEX", "ai_services", "Codex", re.compile(r"(?<![a-z0-9])codex(?![a-z0-9])|code\s*x", re.I)),
    _P01EntityRule("ENTITY_CLAUDE", "ai_services", "Claude", re.compile(r"(?<![a-z0-9])claude(?:[-_ ]?\d+)?(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_MULTICA", "ai_services", "multica", re.compile(r"(?<![a-z0-9])multica(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_RELAY_SERVICE", "ai_services", "中转站", re.compile(r"中转站|中转服务|中转商", re.I)),
    _P01EntityRule("ENTITY_GITHUB", "developer_accounts", "GitHub", re.compile(r"(?<![a-z0-9])github(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_LINUXDO", "developer_accounts", "Linux.do", re.compile(r"linux\s*\.\s*do|linuxdo", re.I)),
    _P01EntityRule("ENTITY_KIMI", "ai_services", "Kimi", re.compile(r"(?<![a-z0-9])kimi(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_GROK_46", "ai_services", "Grok 4.6", re.compile(r"(?<![a-z0-9])grok\s*4\.6(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_GROK", "ai_services", "Grok", re.compile(r"(?<![a-z0-9])grok(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_CURSOR", "development_tooling", "Cursor", re.compile(r"(?<![a-z0-9])cursor(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_AGENT", "development_tooling", "agent", re.compile(r"(?<![a-z0-9])agent(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_PPT", "development_tooling", "PPT", re.compile(r"(?<![a-z0-9])ppt(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_SOL", "ai_services", "Sol", re.compile(r"(?<![a-z0-9])sol(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_HUAWEI", "developer_accounts", "Huawei", re.compile(r"(?<![a-z0-9])huawei(?![a-z0-9])|华为", re.I)),
    _P01EntityRule("ENTITY_SUPERPOWERS", "development_tooling", "Superpowers", re.compile(r"(?<![a-z0-9])superpowers?(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_CONNECT", "development_tooling", "Connect", re.compile(r"(?<![a-z0-9])connect(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_HERMES", "ai_services", "Hermes", re.compile(r"(?<![a-z0-9])hermes(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_OPUS_5", "ai_services", "Opus 5", re.compile(r"(?<![a-z0-9])opus\s*5(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_DS4F", "ai_services", "ds4f", re.compile(r"(?<![a-z0-9])ds4f(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_EXO", "ai_services", "exo", re.compile(r"(?<![a-z0-9])exo(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_QWEN", "ai_services", "Qwen", re.compile(r"(?<![a-z0-9])qwen(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_QWEN_120B", "ai_services", "Qwen 120B", re.compile(r"(?<![a-z0-9])qwen3\.8-120b-a6b(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_ALIBABA", "ai_services", "Alibaba", re.compile(r"(?<![a-z0-9])alibaba(?![a-z0-9])|阿里巴巴", re.I)),
    _P01EntityRule("ENTITY_RUST", "development_tooling", "Rust", re.compile(r"(?<![a-z0-9])rust(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_SAAS", "development_tooling", "SaaS", re.compile(r"(?<![a-z0-9])saas(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_SUBAGENT", "development_tooling", "subagent", re.compile(r"(?<![a-z0-9])subagent(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_TIBO", "ai_services", "Tibo", re.compile(r"(?<![a-z0-9])tibo(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_L_FORUM", "developer_accounts", "论坛", re.compile(r"(?<![a-z0-9])l\s*forum(?![a-z0-9])|论坛", re.I)),
    _P01EntityRule("ENTITY_CODEX_WORKBUDDY", "ai_services", "Codex Workbuddy", re.compile(r"codex\s*workbuddy", re.I)),
    _P01EntityRule("ENTITY_DOC", "development_tooling", "Doc", re.compile(r"(?<![a-z0-9])doc(?![a-z0-9])", re.I)),
    _P01EntityRule("ENTITY_CLAUDE_CODE", "development_tooling", "Claude Code", re.compile(r"claude\s*code", re.I)),
    _P01EntityRule("object:issue", "project_work", "Issue", re.compile(r"(?<![a-z0-9])issue(?![a-z0-9])", re.I)),
    _P01EntityRule("tool:board", "project_work", "看板", re.compile(r"项目看板|工作看板|看板|(?<![a-z0-9])board(?![a-z0-9])", re.I)),
    _P01EntityRule("activity:game", "project_work", "活动", re.compile(r"游戏|活动", re.I)),
    # Redaction placeholders are entity evidence in the development contract.
    # Match both bracketed and bare forms, while canonicalizing the opaque
    # token (and URL class suffix) without recovering any underlying value.
    _P01EntityRule("[PERSON_PLACEHOLDER]", "unknown", "人物", re.compile(r"\[?(?:PERSON|USER)_\d+\]?", re.I)),
    _P01EntityRule("[PHONE_PLACEHOLDER]", "unknown", "电话", re.compile(r"\[?PHONE_\d+\]?", re.I)),
    _P01EntityRule("[URL_PLACEHOLDER]", "unknown", "链接", re.compile(r"\[?URL_\d+(?::[^\]\s]+)?\]?", re.I)),
) + _P01_ENTITY_RULES
_P01_ENTITY_BY_ID = {
    rule.normalized_id: rule for rule in _P01_ENTITY_RULES
}


def _p01_placeholder_normalized_id(surface: str) -> str:
    """Canonicalize a redaction placeholder without exposing its value."""

    value = str(surface or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.upper().startswith("URL_") and ":" in value:
        value = value.split(":", 1)[0]
    return value


def _p01_claim_target_allowed(
    mention: MentionV2,
    *,
    explicit_continuation: bool = False,
) -> bool:
    """Keep claim targets conservative while retaining broad mention recall.

    Generic nouns (``model``, ``email``, ``issue`` and similar) are useful
    mentions but are not automatically the subject of a claim.  Named/opaque
    entities have a stable namespace and can be attached when their exact span
    is present.  A small object exception supports explicit offline examples
    and continuation references without turning every noun into a target.
    """

    normalized = str(mention.normalized_id or "")
    if normalized.startswith("entity:unknown:"):
        return False
    if normalized.startswith(("ENTITY_", "PERSON_", "PHONE_", "URL_", "service:", "platform:", "site:")):
        return True
    if normalized == "object:backup":
        return True
    if normalized == "object:interface" and explicit_continuation:
        return True
    return False
_P01_FAMILY_LABELS = {
    **_FAMILY_LABELS,
    "development_tooling": "开发工具与系统",
    "project_work": "项目与事务",
}

_P01_ACTION_RULES = (
    _P01ActionRule("reset", re.compile(r"重置|reset", re.I)),
    _P01ActionRule("cost", re.compile(r"性价比|划算|价格|收费|太贵|成本|多少钱|报价|费用", re.I)),
    _P01ActionRule("usage", re.compile(r"消耗|用量|耗得|额度(?:掉|降|少)|token.{0,8}(?:多|快)|掉得快", re.I)),
    _P01ActionRule("risk", re.compile(r"风控|合规|封号|封禁|安全风险|风险|漏洞|泄露", re.I)),
    _P01ActionRule("register", re.compile(r"注册|创建账号|申请账号|开户", re.I)),
    _P01ActionRule("email_delivery", re.compile(r"收不到.{0,10}(?:邮件|邮箱|验证码)|(?:邮件|验证码).{0,10}(?:没到|不来|失败)", re.I)),
    _P01ActionRule("outage", re.compile(r"故障|崩了|宕机|不可用|用不了|报错|打不开|挂了|超时|异常|失效|连不上|卡住|不行|(?:错误码|状态码|http\s*)?[45]\d{2}\b", re.I)),
    _P01ActionRule("login", re.compile(r"登录|登陆|登不上|登录不了|密码错误", re.I)),
    _P01ActionRule("schedule", re.compile(r"几点|何时|什么时候|截止|开始|安排|时间|日期", re.I)),
    _P01ActionRule("handle", re.compile(r"怎么处理|如何处理|怎么办|咋办|排查|解决|修复|确认|检查|查看|看看|处理", re.I)),
    _P01ActionRule("state_update", re.compile(r"恢复|好了|完成|上线|取消|进展|继续|还是|仍然|又", re.I)),
)
_P01_ACTION_BY_ID = {rule.action: rule for rule in _P01_ACTION_RULES}
_P01_ACTION_PRIORITY = {
    "reset": 100, "outage": 95, "email_delivery": 92, "register": 90,
    "login": 88, "cost": 85, "usage": 84, "risk": 82, "schedule": 78,
    "handle": 74, "state_update": 70,
}
# Operational actions are emitted as a separate ``action`` mention layer.
# They do not alter the smaller claim-action vocabulary above, which keeps
# relation blocking stable while recovering short Chinese action spans.
_P01_ACTION_MENTION_RULES = (
    ("ACTION_RESET", re.compile(r"重置|reset", re.I)),
    ("ACTION_REGISTER", re.compile(r"注册|创建账号|申请账号|开户", re.I)),
    ("ACTION_LOGIN", re.compile(r"登录|登陆", re.I)),
    ("ACTION_COURSE_SELECT", re.compile(r"选课|选课程", re.I)),
    ("ACTION_COURSE_TAKE", re.compile(r"上课|修课|报课", re.I)),
    ("ACTION_WITHDRAW", re.compile(r"退课|退订|退出|撤销", re.I)),
    ("ACTION_RESEARCH", re.compile(r"调研|研究", re.I)),
    ("ACTION_DOMAIN_PURCHASE", re.compile(r"购买域名|买域名", re.I)),
    ("ACTION_UNINSTALL", re.compile(r"卸载", re.I)),
    ("ACTION_RENEW", re.compile(r"续费", re.I)),
    ("ACTION_CREATE_DECK", re.compile(r"(?:做|制作|创建|生成).{0,4}(?:ppt|幻灯片|演示文稿)", re.I)),
    ("ACTION_CONFIGURE", re.compile(r"配置|设置", re.I)),
    ("ACTION_TEST", re.compile(r"测试|试用", re.I)),
    ("ACTION_RELEASE", re.compile(r"发布|上线", re.I)),
    ("ACTION_TOP_UP", re.compile(r"充值|充钱", re.I)),
    ("ACTION_DELETE_ACCOUNT", re.compile(r"注销账号|删除账号", re.I)),
    ("ACTION_SUBSCRIBE", re.compile(r"订阅", re.I)),
    ("ACTION_CREATE_WEB", re.compile(r"建站|创建网站", re.I)),
    ("ACTION_UPGRADE", re.compile(r"升级", re.I)),
    ("ACTION_OPTIMIZE", re.compile(r"优化", re.I)),
    ("ACTION_BETA", re.compile(r"内测|公测", re.I)),
    ("ACTION_SHARE", re.compile(r"分享", re.I)),
    ("ACTION_INVITE", re.compile(r"邀请", re.I)),
    ("ACTION_SPLIT_ORDER", re.compile(r"拆单|拆分订单", re.I)),
)
_P01_QUANTITY_PATTERN = re.compile(
    r"(?<![A-Za-z_])\d+(?:[-_]\d+)?(?:\.\d+)?(?:\s*(?:万|千|百|亿|秒|个|条|项|次|分钟?|小时?|天|周|月|年|元|块|刀|[kKmMgGtT]|token(?:s)?|GB|MB|%|[xX]))?(?![A-Za-z_])",
    re.I,
)
_P01_STATE_MENTION_RULES = (
    ("STATE_PROBLEM", re.compile(r"问题|故障|异常|报错", re.I)),
    ("STATE_SLOW", re.compile(r"慢|卡顿|卡住", re.I)),
    ("STATE_NOT_ALLOWED", re.compile(r"禁止|不允许|不能|无法", re.I)),
    ("STATE_DURABLE", re.compile(r"一直|持续|反复|总是|还在", re.I)),
    ("STATE_MISSING", re.compile(r"没有|没收到|收不到|缺失", re.I)),
    ("STATE_OK", re.compile(r"好了|恢复|正常|已修复|修好了", re.I)),
    ("STATE_USABLE", re.compile(r"可用|能用|可正常使用", re.I)),
    ("STATE_FAILED", re.compile(r"失败|失效|错误", re.I)),
    ("STATE_LIMITED", re.compile(r"限制|限额", re.I)),
    ("STATE_EXPENSIVE", re.compile(r"太贵|昂贵", re.I)),
    ("STATE_STABLE", re.compile(r"稳定|不稳定", re.I)),
    ("STATE_UNCERTAIN", re.compile(r"不确定|未知", re.I)),
    ("STATE_DIFFICULT", re.compile(r"困难|难以|棘手", re.I)),
    ("STATE_DEGRADED", re.compile(r"降级|变慢|变差", re.I)),
)
_P01_INTENT_MENTION_RULES = (
    ("INTENT_ASK_HOW", re.compile(r"怎么|如何", re.I)),
    ("INTENT_ASK_WHY", re.compile(r"为什么|为何", re.I)),
    ("INTENT_ASK_WHEN", re.compile(r"几点|何时|什么时候", re.I)),
    ("INTENT_ASK_ABILITY", re.compile(r"能否|能不能|是否|可以吗", re.I)),
    ("INTENT_ASK_EXISTENCE", re.compile(r"有没有|有吗", re.I)),
    ("INTENT_ASK_WHICH", re.compile(r"哪个|哪种|哪一个", re.I)),
    ("INTENT_QUESTION_PARTICLE", re.compile(r"吗|呢|[？?]", re.I)),
)
_P01_GENERIC_ENTITY_STOPWORDS = frozenset(
    {
        "这个", "那个", "一下", "怎么", "如何", "为什么", "是否", "能否",
        "有没有", "需要", "应该", "建议", "最好", "感觉", "觉得", "还是",
        "仍然", "继续", "处理", "确认", "排查", "解决", "修复", "查看",
        "看看", "已经", "现在", "可能", "比较", "一个", "事情", "问题",
    }
)
_P01_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,14}")
_P01_LATIN_RUN = re.compile(r"(?<![a-z0-9])[a-z][a-z0-9_.-]{1,30}(?![a-z0-9])", re.I)
_P01_PERSON_MENTION = re.compile(r"(?<![\w])@[\w\-\u3400-\u4dbf\u4e00-\u9fff]{1,40}")
_P01_TIME_PATTERN = re.compile(
    r"今天|明天|昨天|刚才|今晚|今早|本周|下周|上周|稍后|过会儿|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\d{4}[年/-]\d{1,2}(?:[月/-]\d{1,2}日?)?|"
    r"\d{1,2}月\d{1,2}日|\d{1,2}[点时](?:\d{1,2}分)?",
    re.I,
)
_P01_STATE_RULES = (
    ("recovered", re.compile(r"恢复了|已恢复|恢复正常|现在好了|好了|已修复|修好了", re.I)),
    ("recurring", re.compile(r"仍然|还是|还在|不停|反复|一直|又", re.I)),
    ("failed", re.compile(r"失败|不行|不可用|用不了|打不开|报错|超时|登不上|收不到|(?:错误码|状态码|http\s*)?[45]\d{2}\b", re.I)),
    ("planned", re.compile(r"计划|准备|安排|需要|应该|建议|将要", re.I)),
)
_P01_INTENT_RULES = (
    ("seek_information", re.compile(r"怎么|如何|为什么|是否|能否|有没有|几点|何时|什么时候|吗[？?]?$|呢[？?]?$", re.I)),
    ("seek_help", re.compile(r"求助|帮忙|怎么办|咋办|怎么处理|如何处理", re.I)),
    ("recommend_action", re.compile(r"建议|应该|最好|可以先|尽量|记得", re.I)),
    ("compare_cost", re.compile(r"性价比|划算|价格|收费|成本|多少钱|报价|费用", re.I)),
    ("confirm_state", re.compile(r"确认|检查|查看|看看|排查|处理|解决|修复", re.I)),
)
_P01_CONTINUATION_CUE = re.compile(
    r"这个|那个|它|这件事|这块|那块|上述|前面|刚才|继续|还是|仍然|同样|回复|引用|又|好了|不行|怎么办|咋办",
    re.I,
)
_P01_QUESTION_CUE = re.compile(
    r"[？?]|吗$|么$|呢$|怎么|如何|为什么|是否|能否|有没有|几点|何时|什么时候",
    re.I,
)
_P01_SUGGESTION_CUE = re.compile(r"建议|应该|最好|不如|需要|可以先|尽量|记得", re.I)
_P01_HYPOTHESIS_CUE = re.compile(r"可能|也许|或许|大概|估计|听说|据说|似乎", re.I)
_P01_OPINION_CUE = re.compile(r"我觉得|我认为|感觉|不划算|没性价比|担心|讨厌", re.I)
_P01_NEGATIVE_CUE = re.compile(r"没有|没法|不能|不行|收不到|用不了|失败|不划算|没性价比|打不开|超时", re.I)
_P01_CLAUSE_BOUNDARY = re.compile(
    r"[，,。；;！？!?\n]+|而且|并且|同时|另外|然后|但是|不过|以及|对了|顺便|但",
    re.I,
)


def _p01_normalized_entity_id(surface: str) -> str:
    """Make an opaque ID for an unseen noun without putting its text in IDs."""

    digest = hashlib.sha256(_normalized_text(surface).encode("utf-8")).hexdigest()[:12]
    return "entity:unknown:%s" % digest


def _p01_families_for_entities(entity_ids: Sequence[str]) -> Tuple[str, ...]:
    return tuple(
        sorted(
            {
                _P01_ENTITY_BY_ID[entity_id].family_key
                for entity_id in entity_ids
                if entity_id in _P01_ENTITY_BY_ID
            }
        )
    )


def _p01_clauses(content: str) -> List[Tuple[int, int, str]]:
    """Split Chinese short turns while retaining exact half-open spans."""

    values: List[Tuple[int, int, str]] = []
    cursor = 0
    for match in _P01_CLAUSE_BOUNDARY.finditer(content):
        start, end = match.span()
        raw = content[cursor:start]
        stripped = raw.strip()
        if stripped:
            adjusted = cursor + len(raw) - len(raw.lstrip())
            values.append((adjusted, adjusted + len(stripped), stripped))
        cursor = end
    raw = content[cursor:]
    stripped = raw.strip()
    if stripped:
        adjusted = cursor + len(raw) - len(raw.lstrip())
        values.append((adjusted, adjusted + len(stripped), stripped))
    return values


def _p01_non_overlapping_mentions(
    message: MessageV2,
    rules: Sequence[_P01EntityRule],
    analysis_run_id: str,
    created_at: str,
) -> List[MentionV2]:
    matches: List[Tuple[int, int, int, _P01EntityRule]] = []
    for priority, rule in enumerate(rules):
        for match in rule.pattern.finditer(message.content):
            matches.append((match.start(), match.end(), priority, rule))
    # Prefer the longest surface, then the earlier rule.  This removes
    # ``项目`` when ``项目看板`` is present and prevents duplicate target IDs
    # from overlapping spans.
    selected: List[Tuple[int, int, _P01EntityRule]] = []
    for start, end, priority, rule in sorted(
        matches, key=lambda item: (-(item[1] - item[0]), item[0], item[2], item[3].normalized_id)
    ):
        if any(start < other_end and end > other_start for other_start, other_end, _ in selected):
            continue
        selected.append((start, end, rule))
    output = [
        _mention(
            message,
            start,
            end,
            "entity",
            (
                _p01_placeholder_normalized_id(message.content[start:end])
                if rule.normalized_id.endswith("_PLACEHOLDER]")
                else rule.normalized_id
            ),
            None,
            None,
            None,
            None,
            analysis_run_id,
            created_at,
            0.94,
            pipeline_version=P01_PIPELINE_VERSION,
            ruleset_version=P01_RULESET_VERSION,
        )
        for start, end, rule in selected
    ]
    return sorted(output, key=lambda item: (item.span_start, item.span_end, item.normalized_id))


def _p01_entity_fallback(
    message: MessageV2,
    clauses: Sequence[Tuple[int, int, str]],
    existing: Sequence[MentionV2],
    analysis_run_id: str,
    created_at: str,
) -> List[MentionV2]:
    """Recover a concrete noun span for an unseen short Chinese object.

    The fallback is intentionally narrow: it runs only in a clause that has a
    recognized action and only keeps a noun run after removing the action and
    common glue words.  A generic ``entity:unknown`` object is never emitted
    for a social acknowledgement or an empty fragment.
    """

    output: List[MentionV2] = []
    existing_ranges = [(item.span_start, item.span_end) for item in existing]
    for start, end, clause in clauses:
        if any(item.span_start < end and item.span_end > start for item in existing):
            continue
        action_ranges = []
        for rule in _P01_ACTION_RULES:
            action_ranges.extend((start + m.start(), start + m.end()) for m in rule.pattern.finditer(clause))
        if not action_ranges:
            continue
        masked = list(message.content[start:end])
        for action_start, action_end in action_ranges:
            for index in range(max(start, action_start), min(end, action_end)):
                masked[index - start] = " "
        residual = "".join(masked)
        noun_matches: List[Tuple[int, int, str]] = []
        for match in _P01_CJK_RUN.finditer(residual):
            surface = match.group(0)
            if surface in _P01_GENERIC_ENTITY_STOPWORDS:
                continue
            words = [
                value for value in re.split(r"[，,。；;！？!?\s]+", surface)
                if value and value not in _P01_GENERIC_ENTITY_STOPWORDS
            ]
            if not words:
                continue
            noun_matches.append((start + match.start(), start + match.end(), "".join(words)))
        for match in _P01_LATIN_RUN.finditer(residual):
            noun_matches.append((start + match.start(), start + match.end(), match.group(0)))
        if not noun_matches:
            continue
        noun_start, noun_end, surface = max(noun_matches, key=lambda item: (item[1] - item[0], -item[0]))
        if any(noun_start < other_end and noun_end > other_start for other_start, other_end in existing_ranges):
            continue
        output.append(
            _mention(
                message,
                noun_start,
                noun_end,
                "entity",
                _p01_normalized_entity_id(surface),
                None,
                None,
                None,
                None,
                analysis_run_id,
                created_at,
                0.72,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
            )
        )
        existing_ranges.append((noun_start, noun_end))
    return output


def _p01_quantity_id(surface: str) -> str:
    value = re.sub(r"\s+", "", _normalized_text(surface))
    number = re.match(r"\d+(?:[-_]\d+)?(?:\.\d+)?", value)
    if number is None:
        return "QUANTITY_UNKNOWN"
    suffix = value[number.end() :]
    # Keep compact Latin magnitude units, but avoid putting arbitrary prose in
    # a normalized identifier.
    suffix = suffix.upper() if re.fullmatch(r"[KMG T X]", suffix or "", re.I) else ""
    return "QUANTITY_" + number.group(0).replace("-", "_") + suffix


def _p01_state_mention_id(normalized: str) -> str:
    aliases = {
        "recovered": "STATE_OK",
        "recurring": "STATE_DURABLE",
        "failed": "STATE_FAILED",
        "planned": "STATE_PLANNED",
    }
    return aliases.get(normalized, "state:" + normalized)


def _p01_intent_mention_id(normalized: str, surface: str) -> str:
    compact = _normalized_text(surface)
    if re.search(r"怎么|如何", compact):
        return "INTENT_ASK_HOW"
    if re.search(r"为什么|为何", compact):
        return "INTENT_ASK_WHY"
    if re.search(r"几点|何时|什么时候", compact):
        return "INTENT_ASK_WHEN"
    if re.search(r"能否|能不能|是否|可以吗", compact):
        return "INTENT_ASK_ABILITY"
    if re.search(r"有没有|有吗", compact):
        return "INTENT_ASK_EXISTENCE"
    if re.search(r"哪个|哪种|哪一个", compact):
        return "INTENT_ASK_WHICH"
    if re.search(r"吗|呢|[？?]", compact):
        return "INTENT_QUESTION_PARTICLE"
    return "intent:" + normalized


def _p01_time_mention_id(surface: str) -> str:
    compact = _normalized_text(surface)
    aliases = {
        "今天": "TIME_TODAY",
        "明天": "TIME_TOMORROW",
        "昨天": "TIME_YESTERDAY",
        "刚才": "TIME_JUST_NOW",
        "本周": "TIME_THIS_WEEK",
        "下周": "TIME_WEEK_PLUS",
        "上周": "TIME_PRIOR_YEARS",
        "今晚": "TIME_EVENING",
        "今早": "TIME_MORNING",
    }
    return aliases.get(compact, "time:" + compact)


def _p01_auxiliary_mentions(
    message: MessageV2, analysis_run_id: str, created_at: str
) -> List[MentionV2]:
    output: List[MentionV2] = []
    for match in _P01_TIME_PATTERN.finditer(message.content):
        output.append(
            _mention(
                message, match.start(), match.end(), "time",
                _p01_time_mention_id(match.group(0)), None, match.group(0),
                None, None, analysis_run_id, created_at, 0.9,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
            )
        )
    for match in _P01_PERSON_MENTION.finditer(message.content):
        surface = match.group(0)
        digest = hashlib.sha256(_normalized_text(surface).encode("utf-8")).hexdigest()[:12]
        output.append(
            _mention(
                message, match.start(), match.end(), "person", "person:%s" % digest,
                None, None, None, None, analysis_run_id, created_at, 0.9,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
            )
        )
    for normalized, pattern in _P01_STATE_RULES:
        for match in pattern.finditer(message.content):
            output.append(
                _mention(
                    message, match.start(), match.end(), "state", _p01_state_mention_id(normalized),
                    None, None, normalized, None, analysis_run_id, created_at, 0.86,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
            )
    for normalized, pattern in _P01_INTENT_RULES:
        for match in pattern.finditer(message.content):
            output.append(
                _mention(
                    message, match.start(), match.end(), "intent", _p01_intent_mention_id(normalized, match.group(0)),
                    None, None, None, normalized, analysis_run_id, created_at, 0.84,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
            )
    for normalized, pattern in _P01_STATE_MENTION_RULES:
        for match in pattern.finditer(message.content):
            output.append(
                _mention(
                    message, match.start(), match.end(), "state", normalized,
                    None, None, normalized, None, analysis_run_id, created_at, 0.82,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
            )
    for normalized, pattern in _P01_INTENT_MENTION_RULES:
        for match in pattern.finditer(message.content):
            output.append(
                _mention(
                    message, match.start(), match.end(), "intent", normalized,
                    None, None, None, normalized, analysis_run_id, created_at, 0.82,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
            )
    for normalized, pattern in _P01_ACTION_MENTION_RULES:
        for match in pattern.finditer(message.content):
            output.append(
                _mention(
                    message, match.start(), match.end(), "action", normalized,
                    normalized, None, None, None, analysis_run_id, created_at, 0.88,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
            )
    for match in _P01_QUANTITY_PATTERN.finditer(message.content):
        output.append(
            _mention(
                message, match.start(), match.end(), "quantity",
                _p01_quantity_id(match.group(0)), None, None, None, None,
                analysis_run_id, created_at, 0.8,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
            )
        )
    return output


def _p01_action_matches(clause: str) -> List[Tuple[_P01ActionRule, int, int]]:
    matches: List[Tuple[_P01ActionRule, int, int]] = []
    for rule in _P01_ACTION_RULES:
        for match in rule.pattern.finditer(clause):
            matches.append((rule, match.start(), match.end()))
    # One action can have overlapping aliases (e.g. ``登录不了``).  Keep the
    # longest occurrence for that action, while retaining distinct actions in
    # a compound clause for recall.
    selected: List[Tuple[_P01ActionRule, int, int]] = []
    for item in sorted(
        matches,
        key=lambda value: (
            -_P01_ACTION_PRIORITY.get(value[0].action, 0),
            -(value[2] - value[1]),
            value[1],
            value[0].action,
        ),
    ):
        rule, start, end = item
        if any(
            rule.action == other_rule.action
            and start < other_end
            and end > other_start
            for other_rule, other_start, other_end in selected
        ):
            continue
        selected.append(item)
    if any(item[0].action != "state_update" for item in selected):
        selected = [item for item in selected if item[0].action != "state_update"]
    return sorted(selected, key=lambda value: (value[1], value[2], value[0].action))


def _p01_claim_type(text: str) -> str:
    if _P01_QUESTION_CUE.search(text):
        return "question"
    if _P01_SUGGESTION_CUE.search(text):
        return "suggestion"
    if _P01_HYPOTHESIS_CUE.search(text):
        return "hypothesis"
    if _P01_OPINION_CUE.search(text):
        return "opinion"
    return "fact"


def _p01_status(text: str, claim_type: str) -> str:
    for normalized, pattern in _P01_STATE_RULES:
        if pattern.search(text):
            return normalized
    if claim_type in {"question", "hypothesis"}:
        return "unknown"
    if _P01_NEGATIVE_CUE.search(text):
        return "failed"
    return "reported"


def _p01_request(action: str, claim_type: str, text: str) -> str:
    if claim_type == "question":
        if re.search(r"怎么办|咋办|怎么处理|如何处理|求助|帮忙", text):
            return "seek_help"
        return "seek_information"
    if claim_type == "suggestion":
        return "recommend_action"
    if action == "cost":
        return "compare_cost"
    if action == "schedule":
        return "confirm_schedule"
    if action == "handle":
        return "confirm_state"
    return "report_state"


def _p01_is_reference_like(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(
        _P01_CONTINUATION_CUE.search(compact)
        or len(compact) <= 16
        and re.search(r"(?:呢|吗|么|不行|好了|恢复|异常|失败|咋办)$", compact)
    )


def _p01_message_boundary_ids(message: MessageV2) -> Tuple[str, ...]:
    """Return the scoped boundary identities carried by a P0.1 message."""

    return tuple(
        dict.fromkeys(
            value
            for value in (message.dialogue_segment_id, message.block_id)
            if value
        )
    )


def _p01_claim_boundary_ids(claim: ClaimV2) -> Tuple[str, ...]:
    """Return boundary identities for provenance on derived P0.1 objects."""

    return tuple(
        dict.fromkeys(
            value for value in (claim.dialogue_segment_id, claim.block_id) if value
        )
    )


# ---------------------------------------------------------------------------
# P0.1.1 governance gates
# ---------------------------------------------------------------------------
#
# P0.1 objects are deliberately plain immutable DTOs.  That makes them useful
# in notebooks and replay scripts, but it also means a caller can accidentally
# splice an object from another run (or from the original P0 pipeline) into a
# later stage.  The helpers below are the single fail-closed boundary for all
# P0.1 stage inputs and for the final result validator.  They only inspect
# metadata and graph links; they do not change extraction, blocking, or
# relation semantics.

_P01_BOUNDARY_PATTERN = re.compile(
    r"^(?P<kind>segment|block):account=(?P<account>[^|]+)\|"
    r"chat=(?P<chat>[^|]+)\|id=(?P<identifier>.+)$"
)
_P01_STAGE_NAMES = {
    "mention_extraction",
    "mention_extraction_p01",
    "claim_extraction_p01",
    "candidate_generation_p01",
    "pair_classification_p01",
    "event_construction_p01",
    "topic_family_derivation_p01",
    "trend_derivation_p01",
    "presentation_derivation_p01",
}


def _p01_run_id(value: Any) -> str:
    run_id = str(value or "").strip()
    if not run_id:
        raise ValueError("P0.1 analysis_run_id must be a non-empty string")
    return run_id


def _p01_boundary_parts(value: Any) -> Optional[Tuple[str, str, str, str]]:
    """Parse a scoped boundary ID without accepting caller-global labels."""

    text = str(value or "").strip()
    match = _P01_BOUNDARY_PATTERN.fullmatch(text)
    if match is None:
        return None
    return (
        match.group("kind"),
        unquote(match.group("account")),
        unquote(match.group("chat")),
        unquote(match.group("identifier")),
    )


def _p01_boundary_scope(value: Any) -> Optional[Tuple[str, str]]:
    parts = _p01_boundary_parts(value)
    return (parts[1], parts[2]) if parts else None


def _p01_is_boundary_id(value: Any) -> bool:
    return _p01_boundary_parts(value) is not None


def _p01_scope_of_message(message: MessageV2) -> Tuple[str, str]:
    return (str(message.account_id or "default"), str(message.chat_id or "unknown"))


def _p01_scope_of_claim(claim: ClaimV2) -> Tuple[str, str]:
    return (str(claim.account_id or "default"), str(claim.chat_id or "unknown"))


def _p01_unique_ids(values: Iterable[Any], owner: str) -> Tuple[str, ...]:
    output = tuple(str(value or "").strip() for value in values)
    if any(not value for value in output):
        raise ValueError("%s contains an empty ID" % owner)
    if len(set(output)) != len(output):
        raise ValueError("%s contains duplicate IDs" % owner)
    return output


def _p01_metadata_errors(
    item: Any,
    run_id: str,
    owner: str,
    *,
    expected_stages: Optional[Iterable[str]] = None,
) -> List[str]:
    """Return metadata/provenance errors for one typed P0.1 object."""

    errors: List[str] = []
    expected_run = str(run_id)
    if not isinstance(item, SerializableV2):
        return ["%s is not a semantic DTO" % owner]
    if getattr(item, "analysis_run_id", None) != expected_run:
        errors.append("%s analysis_run_id mismatch" % owner)
    if getattr(item, "schema_version", None) != SCHEMA_VERSION:
        errors.append("%s schema_version is not semantic_v2" % owner)
    if getattr(item, "pipeline_version", None) != P01_PIPELINE_VERSION:
        errors.append("%s is not a P0.1 pipeline object" % owner)
    if getattr(item, "ruleset_version", None) != P01_RULESET_VERSION:
        errors.append("%s ruleset_version mismatch" % owner)
    provenance = getattr(item, "provenance", None)
    if not isinstance(provenance, ProvenanceV2):
        errors.append("%s provenance is missing or malformed" % owner)
        return errors
    if provenance.parameters_version != P01_RULESET_VERSION:
        errors.append("%s provenance parameters_version mismatch" % owner)
    stage = str(provenance.stage or "")
    allowed_stages = set(expected_stages or _P01_STAGE_NAMES)
    if stage not in allowed_stages:
        errors.append("%s provenance stage is not allowed: %s" % (owner, stage or "<empty>"))
    try:
        input_ids = tuple(provenance.input_ids)
    except TypeError:
        input_ids = ()
        errors.append("%s provenance input_ids is not iterable" % owner)
    if not input_ids:
        errors.append("%s provenance input_ids is empty" % owner)
    elif any(not str(value or "").strip() for value in input_ids):
        errors.append("%s provenance input_ids contains an empty ID" % owner)
    elif len(set(str(value) for value in input_ids)) != len(input_ids):
        errors.append("%s provenance input_ids contains duplicates" % owner)
    return errors


def _p01_check_provenance_inputs(
    item: Any,
    *,
    owner: str,
    allowed_ids: Iterable[str],
    required_boundary_ids: Iterable[str] = (),
    required_ids: Iterable[str] = (),
) -> List[str]:
    """Reject stale/external provenance IDs and missing boundary provenance."""

    errors: List[str] = []
    provenance = getattr(item, "provenance", None)
    if not isinstance(provenance, ProvenanceV2):
        return errors
    input_ids = tuple(str(value) for value in provenance.input_ids)
    allowed = {str(value) for value in allowed_ids if str(value)}
    boundaries = {str(value) for value in required_boundary_ids if str(value)}
    allowed.update(boundaries)
    unknown = sorted(set(input_ids) - allowed)
    if unknown:
        errors.append("%s provenance has external input_id(s): %s" % (owner, ",".join(unknown)))
    missing_boundaries = sorted(boundaries - set(input_ids))
    if missing_boundaries:
        errors.append(
            "%s provenance is missing boundary ID(s): %s"
            % (owner, ",".join(missing_boundaries))
        )
    missing_ids = sorted({str(value) for value in required_ids if str(value)} - set(input_ids))
    if missing_ids:
        errors.append("%s provenance is missing input ID(s): %s" % (owner, ",".join(missing_ids)))
    return errors


def _p01_boundary_errors(
    boundary_ids: Iterable[Any],
    *,
    owner: str,
    scope: Optional[Tuple[str, str]] = None,
) -> List[str]:
    errors: List[str] = []
    seen: set[str] = set()
    for raw in boundary_ids:
        value = str(raw or "").strip()
        if not value:
            errors.append("%s has an empty boundary ID" % owner)
            continue
        parts = _p01_boundary_parts(value)
        if parts is None:
            errors.append("%s has unscoped boundary ID: %s" % (owner, value))
            continue
        if value in seen:
            errors.append("%s repeats boundary ID: %s" % (owner, value))
        seen.add(value)
        if scope is not None and (parts[1], parts[2]) != scope:
            errors.append("%s boundary scope mismatch: %s" % (owner, value))
    return errors


def _p01_raise(errors: Iterable[str]) -> None:
    values = tuple(sorted(set(str(value) for value in errors if str(value))))
    if values:
        raise ValueError("P0.1.1 governance failure: " + "; ".join(values))


def _p01_validate_messages(
    messages: Sequence[MessageV2],
    *,
    segment_by_message: Optional[Mapping[str, str]] = None,
    owner: str = "messages",
) -> Dict[str, MessageV2]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ValueError("%s must be a sequence of MessageV2 objects" % owner)
    output: Dict[str, MessageV2] = {}
    errors: List[str] = []
    for index, message in enumerate(messages):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(message, MessageV2):
            errors.append("%s is not MessageV2" % row_owner)
            continue
        message_id = str(message.message_id or "").strip()
        if not message_id:
            errors.append("%s message_id is empty" % row_owner)
            continue
        if message_id in output:
            errors.append("duplicate message_id: %s" % message_id)
            continue
        output[message_id] = message
        if message.schema_version != SCHEMA_VERSION:
            errors.append("%s schema_version is not semantic_v2" % row_owner)
        if message.source != SHADOW_SOURCE:
            errors.append("%s is not a semantic shadow message" % row_owner)
        scope = _p01_scope_of_message(message)
        boundaries = _p01_message_boundary_ids(message)
        # A P0.1 stage may only consume a message that has been assigned to a
        # scoped segment.  ``run_semantic_pipeline_p01`` supplies this from the
        # public segmenter before entering any typed stage.
        if not message.dialogue_segment_id:
            errors.append("%s is missing dialogue_segment_id" % row_owner)
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner, scope=scope))
        if message.block_id and not _p01_is_boundary_id(message.block_id):
            errors.append("%s block_id is unscoped" % row_owner)
        if message.reply_to_message_id and str(message.reply_to_message_id) not in {
            str(item.message_id) for item in messages if isinstance(item, MessageV2)
        }:
            errors.append("%s reply_to_message_id is external" % row_owner)
        if segment_by_message is not None:
            if message_id not in segment_by_message:
                errors.append("segment_by_message missing %s" % message_id)
            else:
                supplied = str(segment_by_message.get(message_id) or "")
                if not supplied or not _p01_is_boundary_id(supplied):
                    errors.append("segment_by_message has unscoped boundary for %s" % message_id)
                elif supplied != str(message.dialogue_segment_id or ""):
                    errors.append("segment_by_message disagrees with message boundary: %s" % message_id)
    if segment_by_message is not None:
        external_keys = sorted(set(str(key) for key in segment_by_message) - set(output))
        if external_keys:
            errors.append("segment_by_message has external input_id(s): %s" % ",".join(external_keys))
    _p01_raise(errors)
    return output


def _p01_validate_mentions(
    mentions: Sequence[MentionV2],
    run_id: str,
    *,
    messages: Optional[Mapping[str, MessageV2]] = None,
    owner: str = "mentions",
) -> Dict[str, MentionV2]:
    output: Dict[str, MentionV2] = {}
    errors: List[str] = []
    for index, mention in enumerate(mentions):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(mention, MentionV2):
            errors.append("%s is not MentionV2" % row_owner)
            continue
        mention_id = str(mention.mention_id or "").strip()
        if not mention_id:
            errors.append("%s mention_id is empty" % row_owner)
            continue
        if mention_id in output:
            errors.append("duplicate mention_id: %s" % mention_id)
            continue
        output[mention_id] = mention
        errors.extend(_p01_metadata_errors(mention, run_id, row_owner, expected_stages={"mention_extraction", "mention_extraction_p01"}))
        message = messages.get(str(mention.message_id)) if messages is not None else None
        if messages is not None and message is None:
            errors.append("%s message_id is external" % row_owner)
        source_ids = tuple(str(value) for value in mention.source_message_ids)
        if not source_ids or str(mention.message_id) not in source_ids:
            errors.append("%s source_message_ids do not include message_id" % row_owner)
        if messages is not None and set(source_ids) - set(messages):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        evidence = tuple(mention.evidence_refs)
        if not evidence:
            errors.append("%s evidence_refs is empty" % row_owner)
        if any(not isinstance(item, EvidenceRefV2) for item in evidence):
            errors.append("%s evidence_refs is malformed" % row_owner)
        evidence_message_ids = {str(item.message_id) for item in evidence if isinstance(item, EvidenceRefV2)}
        if str(mention.message_id) not in evidence_message_ids:
            errors.append("%s evidence_refs do not include message_id" % row_owner)
        if messages is not None and evidence_message_ids - set(messages):
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        boundaries = _p01_message_boundary_ids(message) if message is not None else tuple(
            value for value in mention.provenance.input_ids if _p01_is_boundary_id(value)
        )
        scope = _p01_scope_of_message(message) if message is not None else None
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner, scope=scope))
        allowed = set(source_ids) | set(boundaries)
        errors.extend(_p01_check_provenance_inputs(mention, owner=row_owner, allowed_ids=allowed, required_boundary_ids=boundaries, required_ids=(mention.message_id,)))
        if message is not None:
            if mention.span_start < 0 or mention.span_end <= mention.span_start or mention.span_end > len(message.content):
                errors.append("%s has invalid evidence span" % row_owner)
            elif message.content[mention.span_start:mention.span_end] != mention.evidence_text:
                errors.append("%s evidence span does not match message" % row_owner)
    _p01_raise(errors)
    return output


def _p01_validate_claims(
    claims: Sequence[ClaimV2],
    run_id: str,
    *,
    messages: Optional[Mapping[str, MessageV2]] = None,
    mentions: Optional[Mapping[str, MentionV2]] = None,
    owner: str = "claims",
) -> Dict[str, ClaimV2]:
    output: Dict[str, ClaimV2] = {}
    errors: List[str] = []
    for index, claim in enumerate(claims):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(claim, ClaimV2):
            errors.append("%s is not ClaimV2" % row_owner)
            continue
        claim_id = str(claim.claim_id or "").strip()
        if not claim_id:
            errors.append("%s claim_id is empty" % row_owner)
            continue
        if claim_id in output:
            errors.append("duplicate claim_id: %s" % claim_id)
            continue
        output[claim_id] = claim
        errors.extend(_p01_metadata_errors(claim, run_id, row_owner, expected_stages={"claim_extraction_p01"}))
        message = messages.get(str(claim.message_id)) if messages is not None else None
        if messages is not None and message is None:
            errors.append("%s message_id is external" % row_owner)
        source_ids = tuple(str(value) for value in claim.source_message_ids)
        if not source_ids or str(claim.message_id) not in source_ids:
            errors.append("%s source_message_ids do not include message_id" % row_owner)
        if messages is not None and set(source_ids) - set(messages):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        context_ids = tuple(str(value) for value in claim.context_message_ids)
        if not set(context_ids).issubset(set(source_ids)):
            errors.append("%s context_message_ids are outside source_message_ids" % row_owner)
        mention_ids = tuple(str(value) for value in claim.event_mention_ids)
        if mentions is not None:
            unknown_mentions = sorted(set(mention_ids) - set(mentions))
            if unknown_mentions:
                errors.append("%s event_mention_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_mentions)))
            for mention_id in mention_ids:
                mention = mentions.get(mention_id)
                if mention is not None and str(mention.message_id) not in set(source_ids):
                    errors.append("%s event mention is outside source messages" % row_owner)
        evidence = tuple(claim.evidence_refs)
        if not evidence:
            errors.append("%s evidence_refs is empty" % row_owner)
        if any(not isinstance(item, EvidenceRefV2) for item in evidence):
            errors.append("%s evidence_refs is malformed" % row_owner)
        evidence_message_ids = {str(item.message_id) for item in evidence if isinstance(item, EvidenceRefV2)}
        if not evidence_message_ids.issubset(set(source_ids)):
            errors.append("%s evidence_refs are outside source_message_ids" % row_owner)
        if messages is not None and evidence_message_ids - set(messages):
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        own_boundaries = _p01_claim_boundary_ids(claim)
        scope = _p01_scope_of_claim(claim)
        errors.extend(_p01_boundary_errors(own_boundaries, owner=row_owner, scope=scope))
        allowed = set(mention_ids) | set(source_ids) | set(own_boundaries)
        errors.extend(_p01_check_provenance_inputs(claim, owner=row_owner, allowed_ids=allowed, required_boundary_ids=own_boundaries, required_ids=mention_ids))
    _p01_raise(errors)
    return output


def _p01_validate_candidates(
    candidates: Sequence[CandidatePairV2],
    run_id: str,
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "candidate_pairs",
) -> Dict[str, CandidatePairV2]:
    output: Dict[str, CandidatePairV2] = {}
    errors: List[str] = []
    for index, candidate in enumerate(candidates):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(candidate, CandidatePairV2):
            errors.append("%s is not CandidatePairV2" % row_owner)
            continue
        candidate_id = str(candidate.candidate_id or "").strip()
        if not candidate_id:
            errors.append("%s candidate_id is empty" % row_owner)
            continue
        if candidate_id in output:
            errors.append("duplicate candidate_id: %s" % candidate_id)
            continue
        output[candidate_id] = candidate
        errors.extend(_p01_metadata_errors(candidate, run_id, row_owner, expected_stages={"candidate_generation_p01"}))
        left_id, right_id = str(candidate.left_claim_id), str(candidate.right_claim_id)
        if not left_id or not right_id or left_id == right_id:
            errors.append("%s has invalid claim pair" % row_owner)
        if left_id not in claims or right_id not in claims:
            errors.append("%s claim pair contains external input_id(s)" % row_owner)
        left, right = claims.get(left_id), claims.get(right_id)
        boundary_ids = tuple(dict.fromkeys(
            (_p01_claim_boundary_ids(left) if left else ())
            + (_p01_claim_boundary_ids(right) if right else ())
        ))
        errors.extend(_p01_boundary_errors(boundary_ids, owner=row_owner))
        source_ids = tuple(str(value) for value in candidate.source_message_ids)
        expected_sources = set()
        if left and right:
            expected_sources.update(left.source_message_ids)
            expected_sources.update(right.source_message_ids)
        if not set(source_ids).issubset(expected_sources):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        evidence_ids = {str(item.message_id) for item in candidate.evidence_refs if isinstance(item, EvidenceRefV2)}
        expected_evidence_ids = {
            str(item.message_id)
            for claim in (left, right)
            if claim is not None
            for item in claim.evidence_refs
        }
        if not evidence_ids.issubset(expected_evidence_ids):
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        errors.extend(_p01_check_provenance_inputs(
            candidate,
            owner=row_owner,
            allowed_ids={left_id, right_id} | set(source_ids) | set(boundary_ids),
            required_boundary_ids=boundary_ids,
            required_ids=(left_id, right_id),
        ))
    _p01_raise(errors)
    return output


def _p01_validate_decisions(
    decisions: Sequence[PairDecisionV2],
    run_id: str,
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "pair_decisions",
) -> Dict[str, PairDecisionV2]:
    output: Dict[str, PairDecisionV2] = {}
    errors: List[str] = []
    for index, decision in enumerate(decisions):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(decision, PairDecisionV2):
            errors.append("%s is not PairDecisionV2" % row_owner)
            continue
        decision_id = str(decision.decision_id or "").strip()
        if not decision_id:
            errors.append("%s decision_id is empty" % row_owner)
            continue
        if decision_id in output:
            errors.append("duplicate decision_id: %s" % decision_id)
            continue
        output[decision_id] = decision
        errors.extend(_p01_metadata_errors(decision, run_id, row_owner, expected_stages={"pair_classification_p01"}))
        left_id, right_id = str(decision.left_claim_id), str(decision.right_claim_id)
        if not left_id or not right_id or left_id == right_id:
            errors.append("%s has invalid claim pair" % row_owner)
        if left_id not in claims or right_id not in claims:
            errors.append("%s claim pair contains external input_id(s)" % row_owner)
        left, right = claims.get(left_id), claims.get(right_id)
        boundary_ids = tuple(dict.fromkeys(
            (_p01_claim_boundary_ids(left) if left else ())
            + (_p01_claim_boundary_ids(right) if right else ())
        ))
        errors.extend(_p01_boundary_errors(boundary_ids, owner=row_owner))
        expected_sources = set()
        if left and right:
            expected_sources.update(left.source_message_ids)
            expected_sources.update(right.source_message_ids)
        if not set(str(value) for value in decision.source_message_ids).issubset(expected_sources):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        evidence_ids = {str(item.message_id) for item in decision.evidence_refs if isinstance(item, EvidenceRefV2)}
        expected_evidence_ids = {
            str(item.message_id)
            for claim in (left, right)
            if claim is not None
            for item in claim.evidence_refs
        }
        if not evidence_ids.issubset(expected_evidence_ids):
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        errors.extend(_p01_check_provenance_inputs(
            decision,
            owner=row_owner,
            allowed_ids={left_id, right_id} | set(decision.source_message_ids) | set(boundary_ids),
            required_boundary_ids=boundary_ids,
            required_ids=(left_id, right_id),
        ))
    _p01_raise(errors)
    return output


def _p01_event_boundaries(
    event: EventV2,
    claims: Mapping[str, ClaimV2],
) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(
        boundary
        for claim_id in event.claim_ids
        if claim_id in claims
        for boundary in _p01_claim_boundary_ids(claims[claim_id])
    ))


def _p01_validate_events(
    events: Sequence[EventV2],
    run_id: str,
    claims: Mapping[str, ClaimV2],
    decisions: Optional[Mapping[str, PairDecisionV2]] = None,
    *,
    owner: str = "events",
) -> Dict[str, EventV2]:
    output: Dict[str, EventV2] = {}
    errors: List[str] = []
    decisions = decisions or {}
    for index, event in enumerate(events):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(event, EventV2):
            errors.append("%s is not EventV2" % row_owner)
            continue
        event_id = str(event.event_id or "").strip()
        if not event_id:
            errors.append("%s event_id is empty" % row_owner)
            continue
        if event_id in output:
            errors.append("duplicate event_id: %s" % event_id)
            continue
        output[event_id] = event
        errors.extend(_p01_metadata_errors(event, run_id, row_owner, expected_stages={"event_construction_p01"}))
        claim_ids = tuple(str(value) for value in event.claim_ids)
        if not claim_ids:
            errors.append("%s has no claims" % row_owner)
        unknown_claims = sorted(set(claim_ids) - set(claims))
        if unknown_claims:
            errors.append("%s claim_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_claims)))
        event_claims = [claims[value] for value in claim_ids if value in claims]
        scopes = {_p01_scope_of_claim(item) for item in event_claims}
        if len(scopes) > 1:
            errors.append("%s merges claims across account/chat boundaries" % row_owner)
        boundaries = _p01_event_boundaries(event, claims)
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        source_ids = set(str(value) for value in event.source_message_ids)
        expected_sources = {str(value) for claim in event_claims for value in claim.source_message_ids}
        if not source_ids.issubset(expected_sources):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        evidence_ids = {str(item.message_id) for item in event.evidence_refs if isinstance(item, EvidenceRefV2)}
        expected_evidence = {str(item.message_id) for claim in event_claims for item in claim.evidence_refs}
        if not evidence_ids.issubset(expected_evidence):
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        relation_ids = tuple(str(value) for value in event.relation_decision_ids)
        if decisions and not set(relation_ids).issubset(set(decisions)):
            errors.append("%s relation_decision_ids contain external input_id(s)" % row_owner)
        for left_index, left_id in enumerate(claim_ids):
            for right_id in claim_ids[left_index + 1:]:
                decision = next((value for value in decisions.values() if {value.left_claim_id, value.right_claim_id} == {left_id, right_id}), None)
                if decision is not None and (decision.must_not_link or decision.relation != RELATION_SAME_EVENT):
                    errors.append("%s contains a non-merge decision" % row_owner)
        errors.extend(_p01_check_provenance_inputs(
            event,
            owner=row_owner,
            allowed_ids=set(claim_ids) | set(relation_ids) | set(source_ids) | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=claim_ids,
        ))
    _p01_raise(errors)
    return output


def _p01_validate_families(
    families: Sequence[TopicFamilyV2],
    run_id: str,
    events: Mapping[str, EventV2],
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "topic_families",
) -> Dict[str, TopicFamilyV2]:
    output: Dict[str, TopicFamilyV2] = {}
    errors: List[str] = []
    for index, family in enumerate(families):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(family, TopicFamilyV2):
            errors.append("%s is not TopicFamilyV2" % row_owner)
            continue
        family_id = str(family.topic_family_id or "").strip()
        if not family_id:
            errors.append("%s topic_family_id is empty" % row_owner)
            continue
        if family_id in output:
            errors.append("duplicate topic_family_id: %s" % family_id)
            continue
        output[family_id] = family
        errors.extend(_p01_metadata_errors(family, run_id, row_owner, expected_stages={"topic_family_derivation_p01"}))
        event_ids = tuple(str(value) for value in family.event_ids)
        unknown_events = sorted(set(event_ids) - set(events))
        if unknown_events:
            errors.append("%s event_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_events)))
        family_events = [events[value] for value in event_ids if value in events]
        claim_ids = tuple(dict.fromkeys(claim_id for event in family_events for claim_id in event.claim_ids))
        boundaries = tuple(dict.fromkeys(boundary for event in family_events for boundary in _p01_event_boundaries(event, claims)))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        if not set(str(value) for value in family.source_message_ids).issubset({str(value) for event in family_events for value in event.source_message_ids}):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        errors.extend(_p01_check_provenance_inputs(
            family,
            owner=row_owner,
            allowed_ids=set(event_ids) | set(claim_ids) | set(family.source_message_ids) | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=event_ids,
        ))
    _p01_raise(errors)
    return output


def _p01_validate_trends(
    trends: Sequence[TrendV2],
    run_id: str,
    families: Mapping[str, TopicFamilyV2],
    events: Mapping[str, EventV2],
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "trends",
) -> Dict[str, TrendV2]:
    output: Dict[str, TrendV2] = {}
    errors: List[str] = []
    for index, trend in enumerate(trends):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(trend, TrendV2):
            errors.append("%s is not TrendV2" % row_owner)
            continue
        trend_id = str(trend.trend_id or "").strip()
        if not trend_id:
            errors.append("%s trend_id is empty" % row_owner)
            continue
        if trend_id in output:
            errors.append("duplicate trend_id: %s" % trend_id)
            continue
        output[trend_id] = trend
        errors.extend(_p01_metadata_errors(trend, run_id, row_owner, expected_stages={"trend_derivation_p01"}))
        if trend.topic_family_id not in families:
            errors.append("%s topic_family_id is external" % row_owner)
        event_ids = tuple(str(value) for value in trend.event_ids)
        unknown_events = sorted(set(event_ids) - set(events))
        if unknown_events:
            errors.append("%s event_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_events)))
        claim_ids = tuple(str(value) for value in trend.claim_ids)
        unknown_claims = sorted(set(claim_ids) - set(claims))
        if unknown_claims:
            errors.append("%s claim_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_claims)))
        trend_events = [events[value] for value in event_ids if value in events]
        boundaries = tuple(dict.fromkeys(boundary for event in trend_events for boundary in _p01_event_boundaries(event, claims)))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        errors.extend(_p01_check_provenance_inputs(
            trend,
            owner=row_owner,
            allowed_ids={str(trend.topic_family_id)} | set(event_ids) | set(claim_ids) | set(trend.source_message_ids) | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=event_ids,
        ))
    _p01_raise(errors)
    return output


def _p01_validate_presentations(
    presentations: Sequence[PresentationV2],
    run_id: str,
    events: Mapping[str, EventV2],
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "presentations",
) -> Dict[str, PresentationV2]:
    output: Dict[str, PresentationV2] = {}
    errors: List[str] = []
    for index, presentation in enumerate(presentations):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(presentation, PresentationV2):
            errors.append("%s is not PresentationV2" % row_owner)
            continue
        presentation_id = str(presentation.presentation_id or "").strip()
        if not presentation_id:
            errors.append("%s presentation_id is empty" % row_owner)
            continue
        if presentation_id in output:
            errors.append("duplicate presentation_id: %s" % presentation_id)
            continue
        output[presentation_id] = presentation
        errors.extend(_p01_metadata_errors(presentation, run_id, row_owner, expected_stages={"presentation_derivation_p01"}))
        event = events.get(str(presentation.event_id))
        if event is None:
            errors.append("%s event_id is external" % row_owner)
            event_claim_ids: Tuple[str, ...] = ()
            boundaries: Tuple[str, ...] = ()
        else:
            event_claim_ids = tuple(str(value) for value in event.claim_ids)
            boundaries = _p01_event_boundaries(event, claims)
        supported = tuple(str(value) for value in presentation.supported_claim_ids)
        if not set(supported).issubset(set(event_claim_ids)):
            errors.append("%s supported_claim_ids are outside event" % row_owner)
        if not set(str(value) for value in presentation.title_support_claim_ids).issubset(set(event_claim_ids)):
            errors.append("%s title_support_claim_ids are outside event" % row_owner)
        if not set(str(value) for value in presentation.source_message_ids).issubset(set(event.source_message_ids) if event else set()):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        errors.extend(_p01_check_provenance_inputs(
            presentation,
            owner=row_owner,
            allowed_ids={str(presentation.event_id)} | set(event_claim_ids) | set(presentation.source_message_ids) | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=(presentation.event_id,),
        ))
    _p01_raise(errors)
    return output


_P01_ERROR_INSTANCE = re.compile(
    r"(?i)(?:错误码|状态码|http\s*)?[45]\d{2}\b|(?:err|error)[-_ ]?[a-z0-9]+"
)
_P01_LINK_INSTANCE = re.compile(r"https?://[^\s，,。；;！？!?]+", re.I)


def _p01_instance_key(message: MessageV2, clause: str) -> Optional[str]:
    """Return an opaque explicit-instance key, never a time-derived key."""

    raw_key = str(message.explicit_instance_id or "").strip()
    if raw_key:
        value = "explicit:" + raw_key
    else:
        match = _P01_ERROR_INSTANCE.search(clause)
        if match is None:
            match = _P01_LINK_INSTANCE.search(clause)
        if match is None:
            return None
        value = "surface:" + _normalized_text(match.group(0))
    return "instance:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _p01_role_value(raw: Mapping[str, Any], default: Optional[str] = None) -> Optional[str]:
    pilot = raw.get("pilot")
    if isinstance(pilot, Mapping):
        for key in ("dialogue_role", "dialogue_event_role", "message_role"):
            value = str(pilot.get(key) or "").strip()
            if value:
                return value
    for key in ("dialogue_role", "dialogue_event_role", "message_role"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return default


def _p01_evidence_flag(raw: Mapping[str, Any]) -> Optional[bool]:
    pilot = raw.get("pilot")
    if isinstance(pilot, Mapping):
        if "dialogue_evidence_eligible" in pilot:
            return pilot.get("dialogue_evidence_eligible") is True
        if "event_evidence_eligible" in pilot:
            return pilot.get("event_evidence_eligible") is True
    for key in ("dialogue_evidence_eligible", "event_evidence_eligible", "evidence_eligible"):
        if key in raw:
            return raw.get(key) is True
    return None


def _p01_greeting_only(raw: Mapping[str, Any]) -> bool:
    pilot = raw.get("pilot")
    if isinstance(pilot, Mapping) and (
        pilot.get("dialogue_greeting_only") is True or pilot.get("greeting_only") is True
    ):
        return True
    return raw.get("dialogue_greeting_only") is True or raw.get("greeting_only") is True


def _p01_is_event_eligible(
    raw: Mapping[str, Any],
    role: Optional[str],
    segment_annotation: Optional[Mapping[str, Any]],
) -> bool:
    """Deny event evidence for social/context/media rows by default."""

    if str(raw.get("message_type") or "text").casefold() not in {"text", "link"}:
        return False
    if _p01_greeting_only(raw):
        return False
    normalized_role = str(role or "").casefold()
    if normalized_role in {
        ROLE_CONTEXT_ONLY,
        ROLE_CONVERSATION_OPENER,
        "context",
        "opener",
        "conversation_opener",
    }:
        return False
    explicit_flag = _p01_evidence_flag(raw)
    if explicit_flag is False:
        return False
    if segment_annotation is not None:
        if segment_annotation.get("role") in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER}:
            return False
        if segment_annotation.get("evidence_eligible") is False:
            return False
    # If no upstream flag is present, substantive text is eligible; the
    # segmenter has already provided the conservative social classification.
    return True


def _p01_segment_context(
    raw_messages: Sequence[Mapping[str, Any]],
    *,
    max_gap_seconds: float = 15 * 60,
) -> Tuple[Dict[str, str], Dict[str, Mapping[str, Any]], Dict[str, Dict[str, Any]], Tuple[Dict[str, Any], ...], Tuple[str, ...]]:
    """Return roles, segment annotations, raw lookup, and stable message order."""

    segmentation = segment_dialogues(raw_messages, max_gap_seconds=max_gap_seconds)
    roles = dict(segmentation.message_roles)
    annotations = dict(segmentation.message_annotations)
    raw_by_id = {
        str(message.get("message_id")): message
        for message in raw_messages
        if str(message.get("message_id") or "")
    }
    ordered_ids: List[str] = []
    for segment in segmentation.segments:
        ordered_ids.extend(str(value) for value in segment.message_ids)
    # A segmenter always accounts for valid message IDs, but keep this guard so
    # direct library callers cannot lose a row if their mapping is unusual.
    ordered_ids.extend(
        message_id for message_id in sorted(raw_by_id)
        if message_id not in set(ordered_ids)
    )
    # Development fixtures may carry an adjudicated segment/block assignment.
    # Use it as an upstream boundary when present.  This is important for an
    # evaluation replay: the public heuristic segmenter remains the fallback
    # for ordinary callers, while a supplied segment is never silently
    # replaced by a timestamp-derived guess.
    supplied_segment_values = {
        message_id: str(raw.get("dialogue_segment_id") or raw.get("segment_id") or "").strip()
        for message_id, raw in raw_by_id.items()
    }
    supplied_block_values = {
        message_id: str(raw.get("block_id") or raw.get("conversation_block_id") or "").strip()
        for message_id, raw in raw_by_id.items()
    }
    if any(supplied_segment_values.values()) or any(supplied_block_values.values()):
        # A caller-provided label is local metadata, not a global identifier.
        # Scope both explicit labels and the segmenter's fallback labels so a
        # mixed fixture cannot accidentally join two chats that reused a key.
        supplied_segment_ids = {}
        for message_id, raw in raw_by_id.items():
            account_id = str(raw.get("account_id") or "default").strip() or "default"
            chat_id = str(raw.get("chat_id") or raw.get("chat_name") or "unknown-chat").strip()
            base_segment = supplied_segment_values.get(message_id) or str(
                annotations.get(message_id, {}).get("segment_id") or ""
            ).strip()
            supplied_segment_ids[message_id] = (
                _p01_scoped_boundary_id("segment", account_id, chat_id, base_segment)
                if base_segment
                else ""
            )
        for message_id, raw in raw_by_id.items():
            annotation = dict(annotations.get(message_id) or {})
            supplied_segment = supplied_segment_ids.get(message_id) or str(
                annotation.get("segment_id") or ""
            )
            if supplied_segment:
                annotation["segment_id"] = supplied_segment
            supplied_role = _p01_role_value(raw, None)
            if supplied_role:
                role_key = supplied_role.casefold()
                if role_key in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER, "context", "opener", "conversation_opener"}:
                    normalized_role = (
                        ROLE_CONVERSATION_OPENER
                        if role_key in {ROLE_CONVERSATION_OPENER, "opener"}
                        else ROLE_CONTEXT_ONLY
                    )
                else:
                    normalized_role = ROLE_SUBSTANTIVE
                roles[message_id] = normalized_role
                annotation["role"] = normalized_role
                annotation["event_role"] = (
                    ROLE_CONTEXT_ONLY
                    if normalized_role != ROLE_SUBSTANTIVE
                    else ROLE_SUBSTANTIVE
                )
            else:
                # Keep the heuristic role but normalize its annotation key so
                # extractor eligibility has one vocabulary.
                roles[message_id] = str(roles.get(message_id) or ROLE_SUBSTANTIVE)
                annotation["role"] = roles[message_id]
            explicit_evidence = _p01_evidence_flag(raw)
            if explicit_evidence is not None:
                annotation["evidence_eligible"] = explicit_evidence
            account_id = str(raw.get("account_id") or "default").strip() or "default"
            chat_id = str(raw.get("chat_id") or raw.get("chat_name") or "unknown-chat").strip()
            raw_block = str(
                raw.get("block_id") or raw.get("conversation_block_id") or ""
            ).strip()
            annotation["block_id"] = (
                _p01_scoped_boundary_id("block", account_id, chat_id, raw_block)
                if raw_block
                else None
            )
            annotation["account_id"] = account_id
            annotation["chat_id"] = chat_id
            annotations[message_id] = annotation

        # Rebuild the lightweight segment projection from the supplied IDs.
        # Only aggregate identifiers/roles are retained; source text is never
        # copied into the projection.
        def _raw_order(item: Mapping[str, Any]) -> Tuple[Any, ...]:
            def _number(key: str) -> Tuple[int, Any]:
                value = item.get(key)
                try:
                    return (0, int(value))
                except (TypeError, ValueError):
                    return (1, str(value or ""))
            return (
                _number("block_order"),
                _number("position_in_block"),
                _number("sequence_in_chat"),
                str(item.get("message_id") or ""),
            )

        grouped_segments: Dict[str, List[Mapping[str, Any]]] = {}
        for message_id, raw in raw_by_id.items():
            segment_id = supplied_segment_ids.get(message_id) or str(
                annotations[message_id].get("segment_id") or ""
            )
            grouped_segments.setdefault(segment_id, []).append(raw)
        segment_values: List[Dict[str, Any]] = []
        for segment_id, entries in sorted(grouped_segments.items()):
            entries = sorted(entries, key=_raw_order)
            message_ids = [str(item.get("message_id")) for item in entries]
            for position, message_id in enumerate(message_ids):
                annotations[message_id]["position_in_segment"] = position
            context_ids = [
                message_id for message_id in message_ids
                if roles.get(message_id) in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER}
            ]
            substantive_ids = [
                message_id for message_id in message_ids
                if roles.get(message_id) == ROLE_SUBSTANTIVE
            ]
            topic_ids = [
                message_id for message_id in message_ids
                if annotations[message_id].get("topic_bearing")
            ]
            evidence_ids = [
                message_id for message_id in message_ids
                if annotations[message_id].get("evidence_eligible") is True
            ]
            speakers = list(dict.fromkeys(
                str(raw_by_id[message_id].get("speaker_id") or raw_by_id[message_id].get("sender_id") or "unknown")
                for message_id in message_ids
            ))
            first = raw_by_id[message_ids[0]]
            last = raw_by_id[message_ids[-1]]
            segment_values.append(
                {
                    "segment_id": segment_id,
                    "account_id": str(first.get("account_id") or "default"),
                    "chat_id": str(first.get("chat_id") or "unknown"),
                    "message_ids": message_ids,
                    "opener_message_ids": [
                        message_id for message_id in message_ids
                        if roles.get(message_id) == ROLE_CONVERSATION_OPENER
                    ],
                    "context_message_ids": context_ids,
                    "substantive_message_ids": substantive_ids,
                    "topic_bearing_message_ids": topic_ids,
                    "evidence_eligible_message_ids": evidence_ids,
                    "topic_keys": sorted({
                        key for message_id in message_ids
                        for key in annotations[message_id].get("topic_keys", ())
                    }),
                    "speaker_ids": speakers,
                    "start_message_id": message_ids[0],
                    "end_message_id": message_ids[-1],
                    "boundary_before": "provided_segment_metadata",
                    "block_ids": sorted({
                        str(annotations[message_id].get("block_id"))
                        for message_id in message_ids
                        if annotations[message_id].get("block_id")
                    }),
                    "start_time": first.get("time_offset_seconds"),
                    "end_time": last.get("time_offset_seconds"),
                }
            )
        segment_dicts = tuple(segment_values)
        ordered_ids = [
            message_id
            for segment in segment_values
            for message_id in segment["message_ids"]
        ]
    else:
        # Even heuristic segment IDs are scoped before entering P0.1 object
        # provenance.  The public segmenter keeps its historical IDs; this
        # normalization is local to the shadow pipeline.
        normalized_segments: List[Dict[str, Any]] = []
        for segment in segmentation.segments:
            scoped_segment_id = _p01_scoped_boundary_id(
                "segment", segment.account_id, segment.chat_id, segment.segment_id
            )
            for position, message_id in enumerate(segment.message_ids):
                annotation = dict(annotations.get(message_id) or {})
                annotation["segment_id"] = scoped_segment_id
                annotation["position_in_segment"] = position
                annotation["account_id"] = segment.account_id
                annotation["chat_id"] = segment.chat_id
                annotations[message_id] = annotation
            value = segment.to_dict()
            value["segment_id"] = scoped_segment_id
            value["boundary_before"] = segment.boundary_before
            value["block_ids"] = []
            normalized_segments.append(value)
        segment_dicts = tuple(normalized_segments)
    return roles, annotations, raw_by_id, segment_dicts, tuple(ordered_ids)


def extract_mentions_and_claims_p01(
    messages: Sequence[MessageV2],
    analysis_run_id: str,
    created_at: str,
    *,
    segment_by_message: Optional[Mapping[str, str]] = None,
    message_roles: Optional[Mapping[str, str]] = None,
    eligible_message_ids: Optional[Iterable[str]] = None,
) -> Tuple[Tuple[MentionV2, ...], Tuple[ClaimV2, ...]]:
    """Extract P0.1 mentions and claims from an already mapped message set.

    The function is pure and accepts the dialogue context explicitly.  It can
    be used by development replays without loading a database or invoking a
    model.  Mentions are retained for every input row; claims are restricted
    to substantive/event-eligible rows, so context turns cannot become event
    evidence by accident.
    """

    analysis_run_id = _p01_run_id(analysis_run_id)
    message_by_id = _p01_validate_messages(
        messages,
        segment_by_message=segment_by_message,
        owner="extract_mentions_and_claims_p01.messages",
    )
    if message_roles is not None:
        external_roles = sorted(
            {str(value) for value in message_roles} - set(message_by_id)
        )
        if external_roles:
            raise ValueError(
                "P0.1.1 governance failure: message_roles has external input_id(s): %s"
                % ",".join(external_roles)
            )
    if eligible_message_ids is not None:
        eligible_values = tuple(str(value) for value in eligible_message_ids)
        external_eligible = sorted(set(eligible_values) - set(message_by_id))
        if external_eligible:
            raise ValueError(
                "P0.1.1 governance failure: eligible_message_ids has external input_id(s): %s"
                % ",".join(external_eligible)
            )

    ordered = sorted(
        messages,
        key=lambda item: (
            str(segment_by_message.get(item.message_id, "") if segment_by_message else ""),
            item.chat_id,
            _parsed_timestamp(item.timestamp) or datetime.max.replace(tzinfo=timezone.utc),
            item.message_id,
        ),
    )
    roles = dict(message_roles or {})
    eligible = set(str(value) for value in eligible_message_ids) if eligible_message_ids is not None else {
        item.message_id for item in ordered
        if roles.get(item.message_id, ROLE_SUBSTANTIVE) == ROLE_SUBSTANTIVE
    }
    all_mentions: List[MentionV2] = []
    claims: List[ClaimV2] = []
    entity_by_message: Dict[str, Tuple[MentionV2, ...]] = {}
    # The last concrete target is scoped by chat and dialogue segment.  It is
    # updated only by an eligible substantive message, never by a social row.
    previous_targets: Dict[Tuple[str, str, str], Tuple[MentionV2, ...]] = {}
    previous_message_ids: Dict[Tuple[str, str, str], Tuple[str, ...]] = {}

    for message in ordered:
        segment_id = str(
            (segment_by_message.get(message.message_id, "") if segment_by_message else "")
            or message.dialogue_segment_id
            or ""
        )
        if segment_id and not segment_id.startswith("segment:account="):
            segment_id = _p01_scoped_boundary_id(
                "segment", message.account_id, message.chat_id, segment_id
            )
        block_id = message.block_id
        if block_id and not str(block_id).startswith("block:account="):
            block_id = _p01_scoped_boundary_id(
                "block", message.account_id, message.chat_id, block_id
            )
        if segment_id != message.dialogue_segment_id or block_id != message.block_id:
            # Ensure every mention generated below carries the same normalized
            # boundary identity as its claim, including direct library calls
            # that provide ``segment_by_message`` separately from MessageV2.
            message = replace(
                message,
                dialogue_segment_id=segment_id or None,
                block_id=block_id,
            )
        clauses = _p01_clauses(message.content)
        entities = _p01_non_overlapping_mentions(
            message, _P01_ENTITY_RULES, analysis_run_id, created_at
        )
        entities.extend(
            _p01_entity_fallback(
                message, clauses, entities, analysis_run_id, created_at
            )
        )
        entities = sorted(
            {
                item.mention_id: item for item in entities
            }.values(),
            key=lambda item: (item.span_start, item.span_end, item.normalized_id),
        )
        auxiliary = _p01_auxiliary_mentions(message, analysis_run_id, created_at)
        all_mentions.extend(entities)
        all_mentions.extend(auxiliary)
        entity_by_message[message.message_id] = tuple(entities)

        if message.message_id not in eligible:
            continue
        if roles and roles.get(message.message_id) in {
            ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER,
        }:
            continue
        previous_key = (message.account_id, message.chat_id, segment_id)
        for start, end, clause in clauses:
            local_entities = tuple(
                item for item in entities
                if item.span_start < end and item.span_end > start
            )
            action_matches = _p01_action_matches(clause)
            if action_matches:
                # The evaluation claim identity is clause/type/object based;
                # emitting two different action rows for one identical span
                # creates an ambiguous match key.  Keep the strongest action
                # and retain all distinct actions through later clauses.
                action_matches = [
                    max(
                        action_matches,
                        key=lambda item: (
                            _P01_ACTION_PRIORITY.get(item[0].action, 0),
                            item[2] - item[1],
                            item[0].action,
                        ),
                    )
                ]
            claim_type = _p01_claim_type(clause)
            has_prior_target = bool(previous_targets.get(previous_key))
            continuation_cue = has_prior_target and _p01_is_reference_like(clause)
            # An object is inherited only by an explicit continuation/reference
            # cue.  A nearby action alone is not evidence of shared identity.
            can_inherit = not local_entities and has_prior_target and continuation_cue
            active_entities = local_entities or (
                previous_targets.get(previous_key) if can_inherit else ()
            )
            target_entities = tuple(
                item for item in active_entities
                if _p01_claim_target_allowed(
                    item, explicit_continuation=bool(continuation_cue)
                )
            )
            targets = tuple(sorted({item.normalized_id for item in target_entities}))
            if not action_matches and active_entities:
                # A target-bearing short question/state turn still carries a
                # recoverable claim even when it has no canonical verb.
                if _P01_QUESTION_CUE.search(clause):
                    action_matches = [(_P01ActionRule("ask", re.compile(r"$")), len(clause), len(clause))]
                elif _P01_STATE_RULES and any(pattern.search(clause) for _, pattern in _P01_STATE_RULES):
                    action_matches = [(_P01ActionRule("state_update", re.compile(r"$")), len(clause), len(clause))]
            if not action_matches:
                # P0.1's recall contract emits one auditable claim for every
                # eligible substantive clause, even when the action/object is
                # unknown.  Such claims remain insufficient-context and can
                # never create a same-event edge without later evidence.
                action_matches = [(_P01ActionRule("unknown", re.compile(r"$")), 0, len(clause))]
            for action_rule, action_start, action_end in action_matches:
                action = action_rule.action
                status = _p01_status(clause, claim_type)
                request = _p01_request(action, claim_type, clause)
                if action_start == action_end == len(clause):
                    action_start = max(0, len(clause) - 1)
                    action_end = len(clause)
                action_mention = _mention(
                    message,
                    start + action_start,
                    start + action_end,
                    "event_trigger",
                    "action:" + action,
                    action,
                    None,
                    status,
                    request,
                    analysis_run_id,
                    created_at,
                    0.9,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
                all_mentions.append(action_mention)
                target_ids = tuple(item.mention_id for item in target_entities)
                antecedent_refs = tuple(
                    evidence_ref
                    for item in active_entities
                    if item.message_id != message.message_id
                    for evidence_ref in item.evidence_refs
                )
                evidence = EvidenceRefV2(
                    message.message_id, start, end, message.content[start:end]
                )
                evidence_refs = tuple(
                    sorted(
                        set((evidence,) + antecedent_refs),
                        key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
                    )
                )
                uncertainties: List[str] = []
                context_ids = tuple(sorted({item.message_id for item in active_entities if item.message_id != message.message_id}))
                if context_ids:
                    uncertainties.append("object_inherited_from_dialogue_segment")
                    if _p01_is_reference_like(clause):
                        uncertainties.append("explicit_continuation_cue")
                elif continuation_cue:
                    # The current clause may name a narrower object (for
                    # example ``这个接口``) while still explicitly continuing
                    # the previous target.  Keep the linkage marker without
                    # smuggling the prior object into current evidence.
                    uncertainties.append("explicit_continuation_cue")
                if not targets:
                    uncertainties.append("core_entity_unknown")
                instance_key = _p01_instance_key(message, clause)
                if instance_key:
                    uncertainties.append("explicit_instance_evidence")
                claim_id = stable_id(
                    "claim",
                    {
                        "message_id": message.message_id,
                        "span": [start, end],
                        "action": action,
                        "targets": sorted(targets),
                        "claim_type": claim_type,
                        "request": request,
                        "status": status,
                        "segment_id": segment_id,
                        "instance_key": instance_key,
                    },
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                )
                source_message_ids = tuple(sorted({message.message_id, *context_ids}))
                claim_mentions = tuple(sorted(set(target_ids + (action_mention.mention_id,))))
                claims.append(
                    ClaimV2(
                        claim_id=claim_id,
                        speaker_id=message.speaker_id,
                        speaker_name=message.speaker_name,
                        claim_text=message.content[start:end],
                        claim_type=claim_type,
                        target_entity_ids=targets,
                        event_mention_ids=claim_mentions,
                        action=action,
                        request=request,
                        stance_or_polarity="negative" if _P01_NEGATIVE_CUE.search(clause) else "neutral_or_positive",
                        status_or_modality=status,
                        timestamp=message.timestamp,
                        message_id=message.message_id,
                        reply_to_message_id=message.reply_to_message_id,
                        evidence_span=evidence,
                        confidence=0.82 if context_ids else (0.9 if targets else 0.62),
                        source_message_ids=source_message_ids,
                        evidence_refs=evidence_refs,
                        provenance=ProvenanceV2(
                            tuple(
                                dict.fromkeys(
                                    claim_mentions + _p01_message_boundary_ids(message)
                                )
                            ),
                            "claim_extraction_p01",
                            P01_RULESET_VERSION,
                        ),
                        analysis_run_id=analysis_run_id,
                        created_at=created_at,
                        pipeline_version=P01_PIPELINE_VERSION,
                        ruleset_version=P01_RULESET_VERSION,
                        uncertainties=tuple(sorted(set(uncertainties))),
                        dialogue_segment_id=segment_id or None,
                        attribution="direct",
                        context_message_ids=context_ids,
                        explicit_instance_id=instance_key,
                        block_id=message.block_id,
                        entity_ids=tuple(sorted({item.normalized_id for item in active_entities})),
                        account_id=message.account_id,
                        chat_id=message.chat_id,
                    )
                )
        if entities and message.message_id in eligible and roles.get(message.message_id) not in {
            ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER, "context", "opener",
        }:
            previous_targets[previous_key] = tuple(entities)
            previous_message_ids[previous_key] = (message.message_id,)

    unique_mentions = {item.mention_id: item for item in all_mentions}
    unique_claims = {item.claim_id: item for item in claims}
    mention_output = tuple(sorted(unique_mentions.values(), key=lambda item: item.mention_id))
    claim_output = tuple(sorted(unique_claims.values(), key=lambda item: item.claim_id))
    mention_index = _p01_validate_mentions(
        mention_output,
        analysis_run_id,
        messages=message_by_id,
        owner="extract_mentions_and_claims_p01.mentions",
    )
    _p01_validate_claims(
        claim_output,
        analysis_run_id,
        messages=message_by_id,
        mentions=mention_index,
        owner="extract_mentions_and_claims_p01.claims",
    )
    return mention_output, claim_output


def _p01_explicit_reply(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(
        left.reply_to_message_id == right.message_id
        or right.reply_to_message_id == left.message_id
    )


def _p01_same_segment(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(
        left.dialogue_segment_id
        and right.dialogue_segment_id
        and left.dialogue_segment_id == right.dialogue_segment_id
    )


def _p01_same_block(left: ClaimV2, right: ClaimV2) -> bool:
    """Return true only for a caller-supplied block key.

    A block is an upstream conversation unit, not a value inferred from time
    or lexical similarity.  Missing keys intentionally do not match.
    """

    return bool(left.block_id and right.block_id and left.block_id == right.block_id)


def _p01_has_continuation_evidence(left: ClaimV2, right: ClaimV2) -> bool:
    if not _p01_same_segment(left, right):
        return False
    for claim in (left, right):
        if "explicit_continuation_cue" in claim.uncertainties:
            return True
    return False


def _p01_has_shared_instance(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(
        left.explicit_instance_id
        and right.explicit_instance_id
        and left.explicit_instance_id == right.explicit_instance_id
    )


def _p01_unknown_entity_id(entity_id: Any) -> bool:
    value = str(entity_id or "").strip()
    return not value or value.startswith("entity:unknown:") or value in {
        "ENTITY_UNKNOWN",
        "UNKNOWN_ENTITY",
    }


def _p01_unknown_action_id(action: Any) -> bool:
    """Return whether an action is outside the P0.1 canonical vocabulary."""

    value = str(action or "").strip()
    # ``ask`` is synthesized for a target-bearing short question with no
    # canonical verb; it is still a known, non-unknown action class.
    known_actions = {rule.action for rule in _P01_ACTION_RULES} | {"ask"}
    return not value or value == "unknown" or value not in known_actions


def _p01_has_core_identity(claim: ClaimV2) -> bool:
    """Whether a claim has a named, target-level identity for merging."""

    targets = tuple(str(value or "").strip() for value in claim.target_entity_ids)
    # Generic but recognized object evidence (for example ``tool:board``) is
    # sufficient to classify an MNL/topic conflict, even when the extractor
    # deliberately did not promote it to a merge target.  It is *not* enough
    # for same-event support; that gate continues to require shared targets.
    identity_values = targets or tuple(
        str(value or "").strip() for value in (claim.entity_ids or ())
    )
    return bool(identity_values) and not any(
        _p01_unknown_entity_id(value) for value in identity_values
    )


def _p01_cross_block(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(left.block_id and right.block_id and left.block_id != right.block_id)


def _p01_same_scope(left: ClaimV2, right: ClaimV2) -> bool:
    return (
        str(left.account_id or "default") == str(right.account_id or "default")
        and str(left.chat_id or "unknown") == str(right.chat_id or "unknown")
    )


def _p01_time_gap(left: ClaimV2, right: ClaimV2) -> Optional[float]:
    left_time = _parsed_timestamp(left.timestamp)
    right_time = _parsed_timestamp(right.timestamp)
    if left_time is None or right_time is None:
        return None
    return abs((left_time - right_time).total_seconds())


def generate_candidate_pairs_p01(
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[CandidatePairV2, ...]:
    """Generate high-recall claim pairs using explainable blocking signals.

    Blocking is intentionally separate from event identity: a supplied block,
    a derived dialogue segment, or an explicit reply is enough to retain a
    pair for five-way classification.  No block can create a same-event edge.
    Cross-block lexical/family similarity is never a candidate by itself.

    Claims are indexed by account/chat/segment and account/chat/block before
    pairing.  This preserves every claim combination inside one structural
    scope without doing a global all-pairs expansion.  Cross-block replies
    remain an explicit exception for classification (where they are forced to
    ``related_event``), while cross-chat/account links are always rejected.
    """

    analysis_run_id = _p01_run_id(analysis_run_id)
    claim_index = _p01_validate_claims(
        claims,
        analysis_run_id,
        owner="generate_candidate_pairs_p01.claims",
    )

    from collections import defaultdict

    ordered = sorted(claims, key=lambda item: item.claim_id)
    by_id = claim_index
    candidate_keys: set[Tuple[str, str]] = set()

    def _add_group(group: Sequence[ClaimV2]) -> None:
        values = sorted(group, key=lambda item: item.claim_id)
        for index, left in enumerate(values):
            for right in values[index + 1 :]:
                candidate_keys.add((left.claim_id, right.claim_id))

    segment_groups: Dict[Tuple[str, str, str], List[ClaimV2]] = defaultdict(list)
    block_groups: Dict[Tuple[str, str, str], List[ClaimV2]] = defaultdict(list)
    message_groups: Dict[Tuple[str, str, str], List[ClaimV2]] = defaultdict(list)
    instance_groups: Dict[Tuple[str, str, str], List[ClaimV2]] = defaultdict(list)
    claims_by_message: Dict[str, List[ClaimV2]] = defaultdict(list)
    for claim in ordered:
        scope = (str(claim.account_id or "default"), str(claim.chat_id or "unknown"))
        if claim.dialogue_segment_id:
            segment_groups[scope + (str(claim.dialogue_segment_id),)].append(claim)
        if claim.block_id:
            block_groups[scope + (str(claim.block_id),)].append(claim)
        message_groups[scope + (str(claim.message_id),)].append(claim)
        claims_by_message[str(claim.message_id)].append(claim)
        if claim.explicit_instance_id:
            instance_groups[scope + (str(claim.explicit_instance_id),)].append(claim)

    for group in segment_groups.values():
        _add_group(group)
    for group in block_groups.values():
        _add_group(group)
    for group in message_groups.values():
        _add_group(group)
    for group in instance_groups.values():
        _add_group(group)
    # A reply target is indexed by message ID, then checked for account/chat
    # equality below.  This permits a valid cross-block reply without allowing
    # a reused message ID in another chat to become a link.
    for claim in ordered:
        target_id = str(claim.reply_to_message_id or "").strip()
        if not target_id:
            continue
        for other in claims_by_message.get(target_id, ()):
            if claim.claim_id == other.claim_id:
                continue
            candidate_keys.add(tuple(sorted((claim.claim_id, other.claim_id))))

    values: List[CandidatePairV2] = []
    for left_id, right_id in sorted(candidate_keys):
        left = by_id[left_id]
        right = by_id[right_id]
        same_scope = (
            str(left.account_id or "default") == str(right.account_id or "default")
            and str(left.chat_id or "unknown") == str(right.chat_id or "unknown")
        )
        # Scope is a hard safety boundary, including for explicit replies and
        # shared incident keys.
        if not same_scope:
            continue
        left_targets = set(left.target_entity_ids)
        right_targets = set(right.target_entity_ids)
        left_families = set(_p01_families_for_entities(left.target_entity_ids))
        right_families = set(_p01_families_for_entities(right.target_entity_ids))
        explicit_reply = _p01_explicit_reply(left, right)
        same_block = _p01_same_block(left, right)
        same_segment = _p01_same_segment(left, right)
        shared_instance = _p01_has_shared_instance(left, right)
        same_message = left.message_id == right.message_id
        cross_block = bool(left.block_id and right.block_id and left.block_id != right.block_id)
        # A known cross-block pair is only retained for an explicit linkage
        # signal.  When one side has no block, the segment gate remains the
        # conservative fallback because the boundary is not known to differ.
        if cross_block and not (explicit_reply or shared_instance or same_message):
            continue
        if not (
            same_message
            or explicit_reply
            or shared_instance
            or same_segment
            or same_block
        ):
            continue
        reasons: List[str] = []
        if explicit_reply:
            reasons.append("explicit_reply")
        if same_block:
            reasons.append("same_block")
        if same_segment:
            reasons.append("same_dialogue_segment")
        if shared_instance:
            reasons.append("explicit_shared_instance")
        if cross_block:
            reasons.append("cross_block")
        score = 0.35
        score += 0.45 if explicit_reply else 0.0
        score += 0.25 if left_targets.intersection(right_targets) else 0.0
        score += 0.15 if same_block else 0.0
        score += 0.12 if same_segment else 0.0
        score += 0.2 if shared_instance else 0.0
        score += 0.08 if left_families.intersection(right_families) else 0.0
        score = min(1.0, score)
        evidence = tuple(
            sorted(
                set(left.evidence_refs + right.evidence_refs),
                key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
            )
        )
        candidate_id = stable_id(
            "candidate",
            {"left": left_id, "right": right_id, "blocking_reasons": sorted(set(reasons))},
            pipeline_version=P01_PIPELINE_VERSION,
            ruleset_version=P01_RULESET_VERSION,
        )
        provenance_ids = tuple(
            dict.fromkeys(
                (left_id, right_id)
                + _p01_claim_boundary_ids(left)
                + _p01_claim_boundary_ids(right)
            )
        )
        values.append(
            CandidatePairV2(
                candidate_id=candidate_id,
                left_claim_id=left_id,
                right_claim_id=right_id,
                blocking_reasons=tuple(sorted(set(reasons))),
                source_message_ids=tuple(sorted({left.message_id, right.message_id})),
                evidence_refs=evidence,
                score=round(score, 6),
                provenance=ProvenanceV2(
                    provenance_ids, "candidate_generation_p01", P01_RULESET_VERSION
                ),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
            )
        )
    output = tuple(sorted(values, key=lambda item: item.candidate_id))
    _p01_validate_candidates(
        output,
        analysis_run_id,
        claim_index,
        owner="generate_candidate_pairs_p01.candidates",
    )
    return output


def classify_claim_pair_p01(
    left: ClaimV2,
    right: ClaimV2,
    analysis_run_id: str,
    created_at: str,
) -> PairDecisionV2:
    """Apply P0.1 hard gates before assigning one of five relation labels."""

    analysis_run_id = _p01_run_id(analysis_run_id)
    claim_index = _p01_validate_claims(
        (left, right),
        analysis_run_id,
        owner="classify_claim_pair_p01.claims",
    )
    if left.claim_id == right.claim_id:
        raise ValueError("candidate pair requires two distinct claims")
    left_targets, right_targets = set(left.target_entity_ids), set(right.target_entity_ids)
    shared_targets = left_targets.intersection(right_targets)
    # ``entity_ids`` contains all exact-span entity evidence, including a
    # generic object that was deliberately not promoted to a claim target.
    # Use it for MNL/topic conflict detection so conservative target matching
    # does not erase an evidenced object distinction.
    left_entities = set(left.entity_ids or left.target_entity_ids)
    right_entities = set(right.entity_ids or right.target_entity_ids)
    shared_entity_evidence = left_entities.intersection(right_entities)
    left_families = set(_p01_families_for_entities(tuple(left_entities)))
    right_families = set(_p01_families_for_entities(tuple(right_entities)))
    shared_families = left_families.intersection(right_families)
    explicit_reply = _p01_explicit_reply(left, right)
    continuation = _p01_has_continuation_evidence(left, right)
    same_message = left.message_id == right.message_id
    shared_instance = _p01_has_shared_instance(left, right)
    same_scope = _p01_same_scope(left, right)
    cross_block = _p01_cross_block(left, right)
    left_core_known = _p01_has_core_identity(left)
    right_core_known = _p01_has_core_identity(right)
    unknown_action = _p01_unknown_action_id(left.action) or _p01_unknown_action_id(
        right.action
    )
    strong_link = explicit_reply or same_message or shared_instance or continuation
    support: List[str] = []
    conflicts: List[str] = []
    hard: List[str] = []
    uncertainties: List[str] = []

    shared_targets_known = bool(shared_targets) and not any(
        _p01_unknown_entity_id(value) for value in shared_targets
    )
    if shared_targets_known:
        support.append("core_entity")
    elif shared_targets:
        uncertainties.append("core_entity_unknown")
    elif shared_entity_evidence:
        support.append("entity_evidence")
    elif left_entities and right_entities:
        conflicts.append("core_entity")
        hard.append("CORE_OBJECT_CONFLICT")
    else:
        uncertainties.append("core_entity_missing")
    left_action_known = not _p01_unknown_action_id(left.action)
    right_action_known = not _p01_unknown_action_id(right.action)
    if left_action_known and right_action_known and left.action == right.action:
        support.append("action")
    elif left_action_known and right_action_known and left.action and right.action:
        conflicts.append("action")
        hard.append("ACTION_TYPE_CONFLICT")
    else:
        uncertainties.append("action_unknown" if not (left_action_known and right_action_known) else "action_missing")
    if left.request == right.request and left.request:
        support.append("intent")
    elif left.request and right.request:
        conflicts.append("intent")
        hard.append("INTENT_CONFLICT")
    else:
        uncertainties.append("intent_missing")
    if left.attribution == right.attribution:
        support.append("attribution")
    else:
        conflicts.append("attribution")
        hard.append("ATTRIBUTION_CONFLICT")

    status_pair = {left.status_or_modality, right.status_or_modality}
    status_conflict = status_pair.intersection({"recovered", "failed", "failed_or_negative"}) and (
        "recurring" in status_pair or "reported" in status_pair or "failed" in status_pair or "failed_or_negative" in status_pair
    ) and left.status_or_modality != right.status_or_modality
    if status_conflict:
        conflicts.append("status")
        if not strong_link:
            hard.append("STATUS_CONTRADICTION_WITHOUT_SHARED_INSTANCE")
        else:
            support.append("status_progression_with_linkage")
    elif left.status_or_modality == right.status_or_modality:
        support.append("status")

    if explicit_reply:
        support.append("explicit_reply")
    elif same_message:
        support.append("same_message")
    elif shared_instance:
        support.append("shared_instance")
    elif continuation:
        support.append("dialogue_continuation")
    else:
        uncertainties.append("missing_strong_event_linkage")
    time_gap = _p01_time_gap(left, right)
    if time_gap is None:
        uncertainties.append("timestamp_missing_or_unparseable")
    elif time_gap <= 24 * 60 * 60:
        support.append("time_window")
    elif not explicit_reply:
        conflicts.append("time")
        hard.append("TEMPORAL_INSTANCE_CONFLICT")
    else:
        support.append("long_range_explicit_reply")

    # Identity and scope gates are evaluated before any positive slot score.
    # In particular, an equal opaque ``entity:unknown`` value or equal
    # ``unknown`` action is not evidence that two turns describe one event.
    if not same_scope:
        relation = RELATION_UNRELATED
        confidence = 0.99
        hard.append("CROSS_ACCOUNT_OR_CHAT_SCOPE")
    elif not left_core_known or not right_core_known:
        relation = RELATION_INSUFFICIENT_CONTEXT
        confidence = 0.62
        hard.append("CORE_ENTITY_UNKNOWN")
    elif unknown_action:
        relation = RELATION_INSUFFICIENT_CONTEXT
        confidence = 0.62
        hard.append("ACTION_UNKNOWN")
    elif cross_block and explicit_reply:
        # A reply crossing an adjudicated block is useful as a related edge,
        # but it is not sufficient authority to merge event cards.  This also
        # handles a same target/action pair and long-range replies.
        relation = RELATION_RELATED_EVENT
        confidence = 0.82
        hard.append("CROSS_BLOCK_REPLY_ONLY_RELATED")
    elif hard and not (
        set(hard).issubset({"STATUS_CONTRADICTION_WITHOUT_SHARED_INSTANCE"})
        and strong_link
    ):
        if shared_targets:
            relation = RELATION_RELATED_EVENT
            confidence = 0.88
        elif shared_families:
            relation = RELATION_SAME_TOPIC_ONLY
            confidence = 0.9
        else:
            relation = RELATION_UNRELATED
            confidence = 0.92
    elif (
        shared_targets
        and left.action == right.action
        and left.request == right.request
        and strong_link
    ):
        relation = RELATION_SAME_EVENT
        confidence = 0.96 if explicit_reply else 0.91
    elif shared_targets:
        relation = RELATION_RELATED_EVENT
        confidence = 0.86
        if not strong_link:
            uncertainties.append("same_slots_without_strong_linkage")
    elif shared_families:
        relation = RELATION_SAME_TOPIC_ONLY
        confidence = 0.88
        hard.append("TOPIC_ONLY_EVIDENCE")
    else:
        relation = RELATION_UNRELATED
        confidence = 0.9

    # Topic-only evidence is a hard non-link even when a shared family was the
    # only block.  A same-event decision is never allowed to carry MNL.
    mnl_reasons = tuple(sorted(set(hard)))
    must_not_link = bool(mnl_reasons) and relation != RELATION_SAME_EVENT
    if relation == RELATION_SAME_TOPIC_ONLY and "TOPIC_ONLY_EVIDENCE" not in mnl_reasons:
        mnl_reasons = tuple(sorted(set(mnl_reasons + ("TOPIC_ONLY_EVIDENCE",))))
        must_not_link = True
    left_id, right_id = sorted((left.claim_id, right.claim_id))
    evidence = tuple(
        sorted(
            set(left.evidence_refs + right.evidence_refs),
            key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
        )
    )
    decision_id = stable_id(
        "pair",
        {
            "left": left_id,
            "right": right_id,
            "relation": relation,
            "hard": sorted(set(mnl_reasons)),
        },
        pipeline_version=P01_PIPELINE_VERSION,
        ruleset_version=P01_RULESET_VERSION,
    )
    provenance_ids = tuple(
        dict.fromkeys(
            (left_id, right_id)
            + _p01_claim_boundary_ids(left)
            + _p01_claim_boundary_ids(right)
        )
    )
    decision = PairDecisionV2(
        decision_id=decision_id,
        left_claim_id=left_id,
        right_claim_id=right_id,
        relation=relation,
        supporting_slots=tuple(sorted(set(support))),
        conflicting_slots=tuple(sorted(set(conflicts))),
        hard_conflict_reasons=mnl_reasons,
        source_message_ids=tuple(sorted({left.message_id, right.message_id})),
        evidence_refs=evidence,
        confidence=confidence,
        provenance=ProvenanceV2(
            provenance_ids, "pair_classification_p01", P01_RULESET_VERSION
        ),
        analysis_run_id=analysis_run_id,
        created_at=created_at,
        pipeline_version=P01_PIPELINE_VERSION,
        ruleset_version=P01_RULESET_VERSION,
        uncertainties=tuple(sorted(set(uncertainties))),
        must_not_link=must_not_link,
        must_not_link_reason_codes=mnl_reasons if must_not_link else (),
    )
    _p01_validate_decisions(
        (decision,),
        analysis_run_id,
        claim_index,
        owner="classify_claim_pair_p01.decisions",
    )
    return decision


def classify_candidate_pairs_p01(
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[PairDecisionV2, ...]:
    analysis_run_id = _p01_run_id(analysis_run_id)
    candidates = generate_candidate_pairs_p01(claims, analysis_run_id, created_at)
    by_id = {item.claim_id: item for item in claims}
    decisions = [
        classify_claim_pair_p01(
            by_id[candidate.left_claim_id],
            by_id[candidate.right_claim_id],
            analysis_run_id,
            created_at,
        )
        for candidate in candidates
    ]
    output = tuple(sorted(decisions, key=lambda item: item.decision_id))
    _p01_validate_decisions(
        output,
        analysis_run_id,
        by_id,
        owner="classify_candidate_pairs_p01.decisions",
    )
    return output


def _p01_evidence_union(claims: Sequence[ClaimV2]) -> Tuple[EvidenceRefV2, ...]:
    return tuple(
        sorted(
            {evidence for claim in claims for evidence in claim.evidence_refs},
            key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
        )
    )


def build_events_p01(
    claims: Sequence[ClaimV2],
    pair_decisions: Sequence[PairDecisionV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[EventV2, ...]:
    """Construct events from only strong same-event edges.

    ``related_event`` and ``same_topic_only`` edges are deliberately ignored
    for grouping.  This is the core over-merge guard for P0.1.
    """

    analysis_run_id = _p01_run_id(analysis_run_id)
    claim_index = _p01_validate_claims(
        claims,
        analysis_run_id,
        owner="build_events_p01.claims",
    )
    decision_index = _p01_validate_decisions(
        pair_decisions,
        analysis_run_id,
        claim_index,
        owner="build_events_p01.decisions",
    )

    if any(not claim.evidence_refs or not claim.source_message_ids for claim in claims):
        raise ValueError("event claims must carry source evidence")
    claim_by_id = claim_index
    decisions = {
        frozenset((item.left_claim_id, item.right_claim_id)): item
        for item in decision_index.values()
    }
    groups: List[List[ClaimV2]] = []
    for claim in sorted(claims, key=lambda item: item.claim_id):
        placed = False
        for group in groups:
            relations = [
                decisions.get(frozenset((claim.claim_id, member.claim_id)))
                for member in group
            ]
            if relations and all(
                item is not None
                and item.relation == RELATION_SAME_EVENT
                and not item.must_not_link
                for item in relations
            ):
                group.append(claim)
                placed = True
                break
        if not placed:
            groups.append([claim])

    events: List[EventV2] = []
    for raw_group in groups:
        group = sorted(raw_group, key=lambda item: item.claim_id)
        claim_ids = tuple(item.claim_id for item in group)
        entity_ids = tuple(sorted({value for item in group for value in item.target_entity_ids}))
        actions = tuple(sorted({item.action for item in group if item.action}))
        evidence = _p01_evidence_union(group)
        source_message_ids = tuple(sorted({value for item in group for value in item.source_message_ids}))
        mention_ids = tuple(sorted({value for item in group for value in item.event_mention_ids}))
        timestamps = sorted(item.timestamp for item in group if item.timestamp != "unknown")
        related_decisions = tuple(
            sorted(
                item.decision_id
                for item in pair_decisions
                if item.left_claim_id in claim_ids
                and item.right_claim_id in claim_ids
                and item.relation == RELATION_SAME_EVENT
            )
        )
        event_id = stable_id(
            "event",
            {"claim_ids": sorted(claim_ids), "core_entities": sorted(entity_ids), "actions": sorted(actions)},
            pipeline_version=P01_PIPELINE_VERSION,
            ruleset_version=P01_RULESET_VERSION,
        )
        provenance_ids = tuple(
            dict.fromkeys(
                claim_ids
                + tuple(
                    boundary_id
                    for item in group
                    for boundary_id in _p01_claim_boundary_ids(item)
                )
            )
        )
        events.append(
            EventV2(
                event_id=event_id,
                event_type=actions[0] if len(actions) == 1 else "compound_unknown",
                core_entity_ids=entity_ids,
                actions=actions,
                start_at=timestamps[0] if timestamps else "unknown",
                end_at=timestamps[-1] if timestamps else "unknown",
                statuses=tuple(sorted({item.status_or_modality for item in group})),
                requests=tuple(sorted({item.request for item in group})),
                participant_ids=tuple(sorted({item.speaker_id for item in group})),
                claim_ids=claim_ids,
                mention_ids=mention_ids,
                supporting_evidence_refs=evidence,
                conflicting_evidence_refs=(),
                relation_decision_ids=related_decisions,
                confidence=min(item.confidence for item in group),
                source_message_ids=source_message_ids,
                evidence_refs=evidence,
                provenance=ProvenanceV2(
                    provenance_ids, "event_construction_p01", P01_RULESET_VERSION
                ),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
                uncertainties=tuple(sorted({value for item in group for value in item.uncertainties})),
            )
        )
    output = tuple(sorted(events, key=lambda item: item.event_id))
    _p01_validate_events(
        output,
        analysis_run_id,
        claim_index,
        decision_index,
        owner="build_events_p01.events",
    )
    return output


def derive_topic_families_p01(
    events: Sequence[EventV2],
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[TopicFamilyV2, ...]:
    analysis_run_id = _p01_run_id(analysis_run_id)
    claim_by_id = _p01_validate_claims(
        claims,
        analysis_run_id,
        owner="derive_topic_families_p01.claims",
    )
    event_by_id = _p01_validate_events(
        events,
        analysis_run_id,
        claim_by_id,
        owner="derive_topic_families_p01.events",
    )
    buckets: Dict[str, List[EventV2]] = {}
    for event in event_by_id.values():
        families = _p01_families_for_entities(event.core_entity_ids) or ("unknown",)
        for family in families:
            buckets.setdefault(family, []).append(event)
    output: List[TopicFamilyV2] = []
    for family_key, family_events in sorted(buckets.items()):
        event_ids = tuple(sorted(item.event_id for item in family_events))
        family_claims = [
            claim_by_id[claim_id]
            for event in family_events
            for claim_id in event.claim_ids
            if claim_id in claim_by_id
        ]
        evidence = _p01_evidence_union(family_claims)
        output.append(
            TopicFamilyV2(
                topic_family_id=stable_id(
                    "topic_family", {"family_key": family_key},
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                ),
                family_key=family_key,
                label=_P01_FAMILY_LABELS.get(family_key, family_key),
                event_ids=event_ids,
                confidence=0.94 if family_key != "unknown" else 0.4,
                source_message_ids=tuple(sorted({item.message_id for item in family_claims})),
                evidence_refs=evidence,
                provenance=ProvenanceV2(
                    tuple(
                        dict.fromkeys(
                            event_ids
                            + tuple(
                                boundary_id
                                for item in family_claims
                                for boundary_id in _p01_claim_boundary_ids(item)
                            )
                        )
                    ),
                    "topic_family_derivation_p01",
                    P01_RULESET_VERSION,
                ),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
                uncertainties=("family_unknown",) if family_key == "unknown" else (),
            )
        )
    result = tuple(output)
    _p01_validate_families(
        result,
        analysis_run_id,
        event_by_id,
        claim_by_id,
        owner="derive_topic_families_p01.families",
    )
    return result


def derive_trends_p01(
    events: Sequence[EventV2],
    topic_families: Sequence[TopicFamilyV2],
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[TrendV2, ...]:
    analysis_run_id = _p01_run_id(analysis_run_id)
    claim_by_id = _p01_validate_claims(
        claims,
        analysis_run_id,
        owner="derive_trends_p01.claims",
    )
    event_by_id = _p01_validate_events(
        events,
        analysis_run_id,
        claim_by_id,
        owner="derive_trends_p01.events",
    )
    family_by_id = _p01_validate_families(
        topic_families,
        analysis_run_id,
        event_by_id,
        claim_by_id,
        owner="derive_trends_p01.families",
    )
    output: List[TrendV2] = []
    for family in family_by_id.values():
        by_action: Dict[str, List[EventV2]] = {}
        for event_id in family.event_ids:
            event = event_by_id[event_id]
            for action in event.actions:
                by_action.setdefault(action, []).append(event)
        for action, action_events in sorted(by_action.items()):
            if len(action_events) < 2:
                continue
            event_ids = tuple(sorted(item.event_id for item in action_events))
            claim_ids = tuple(sorted({value for item in action_events for value in item.claim_ids}))
            trend_claims = [claim_by_id[value] for value in claim_ids]
            output.append(
                TrendV2(
                    trend_id=stable_id(
                        "trend", {"family": family.topic_family_id, "action": action, "events": event_ids},
                        pipeline_version=P01_PIPELINE_VERSION,
                        ruleset_version=P01_RULESET_VERSION,
                    ),
                    topic_family_id=family.topic_family_id,
                    signal_key=action,
                    event_ids=event_ids,
                    claim_ids=claim_ids,
                    confidence=0.68,
                    source_message_ids=tuple(sorted({item.message_id for item in trend_claims})),
                    evidence_refs=_p01_evidence_union(trend_claims),
                    provenance=ProvenanceV2(
                        tuple(
                            dict.fromkeys(
                                event_ids
                                + tuple(
                                    boundary_id
                                    for item in trend_claims
                                    for boundary_id in _p01_claim_boundary_ids(item)
                                )
                            )
                        ),
                        "trend_derivation_p01",
                        P01_RULESET_VERSION,
                    ),
                    analysis_run_id=analysis_run_id,
                    created_at=created_at,
                    pipeline_version=P01_PIPELINE_VERSION,
                    ruleset_version=P01_RULESET_VERSION,
                    uncertainties=("trend_is_candidate_not_fact",),
                )
            )
    result = tuple(sorted(output, key=lambda item: item.trend_id))
    _p01_validate_trends(
        result,
        analysis_run_id,
        family_by_id,
        event_by_id,
        claim_by_id,
        owner="derive_trends_p01.trends",
    )
    return result


def _p01_entity_label(entity_id: str) -> str:
    rule = _P01_ENTITY_BY_ID.get(entity_id)
    if rule is not None:
        return rule.label
    if entity_id.startswith("entity:unknown:"):
        return "对象待确认"
    return "对象待确认"


def derive_presentations_p01(
    events: Sequence[EventV2],
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[PresentationV2, ...]:
    """Project one event at a time with exact claim/message evidence.

    Unknown-object/action events are retained as ``do_not_display`` records;
    they are useful for offline error analysis but cannot become a polished
    user-facing fact.  Visible sentences are one-to-one with source claims.
    """

    analysis_run_id = _p01_run_id(analysis_run_id)
    claim_by_id = _p01_validate_claims(
        claims,
        analysis_run_id,
        owner="derive_presentations_p01.claims",
    )
    event_by_id = _p01_validate_events(
        events,
        analysis_run_id,
        claim_by_id,
        owner="derive_presentations_p01.events",
    )
    output: List[PresentationV2] = []
    for event in event_by_id.values():
        event_claims = [claim_by_id[value] for value in event.claim_ids if value in claim_by_id]
        if not event_claims:
            raise ValueError("presentation cannot be created for an event without claims")
        evidence = _p01_evidence_union(event_claims)
        source_message_ids = tuple(sorted({value for item in event_claims for value in item.source_message_ids}))
        if evidence != event.evidence_refs or source_message_ids != event.source_message_ids:
            raise ValueError("presentation evidence must equal, not expand, event evidence")
        visible = bool(event.core_entity_ids and event.actions and event.event_type != "unknown")
        entity_text = "、".join(_p01_entity_label(value) for value in event.core_entity_ids[:2])
        action_text = "、".join(_ACTION_LABELS.get(value, value) for value in event.actions[:2])
        title = "%s：%s" % (entity_text or "对象待确认", action_text or "事项待确认")
        sentences = tuple(
            PresentationSentenceV2(
                text="%s：%s" % (item.speaker_name, item.claim_text),
                claim_ids=(item.claim_id,),
                message_ids=tuple(sorted(item.source_message_ids)),
            )
            for item in event_claims
        ) if visible else ()
        supported = event.claim_ids if visible else ()
        presentation_id = stable_id(
            "presentation",
            {"event_id": event.event_id, "role": "brief" if visible else "do_not_display"},
            pipeline_version=P01_PIPELINE_VERSION,
            ruleset_version=P01_RULESET_VERSION,
        )
        provenance_ids = tuple(
            dict.fromkeys(
                (event.event_id,)
                + event.claim_ids
                + tuple(
                    boundary_id
                    for item in event_claims
                    for boundary_id in _p01_claim_boundary_ids(item)
                )
            )
        )
        output.append(
            PresentationV2(
                presentation_id=presentation_id,
                event_id=event.event_id,
                presentation_role="brief" if visible else "do_not_display",
                title=title,
                summary="；".join(item.text for item in sentences),
                title_support_claim_ids=supported,
                sentences=sentences,
                supported_claim_ids=supported,
                source_message_ids=source_message_ids,
                evidence_refs=evidence,
                confidence=event.confidence,
                source=SHADOW_SOURCE,
                provenance=ProvenanceV2(
                    provenance_ids,
                    "presentation_derivation_p01",
                    P01_RULESET_VERSION,
                ),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
                uncertainties=event.uncertainties,
            )
        )
    result = tuple(sorted(output, key=lambda item: item.presentation_id))
    _p01_validate_presentations(
        result,
        analysis_run_id,
        event_by_id,
        claim_by_id,
        owner="derive_presentations_p01.presentations",
    )
    return result


def validate_p01_invariants(
    result: SemanticResultV2,
    *,
    event_eligible_message_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return zero-tolerance checks for a P0.1.1 semantic result.

    This function is intentionally total: malformed objects, mixed-run
    objects, and even a non-``SemanticResultV2`` value produce a failed report
    instead of an exception.  The stage functions use the same gates as
    raising preconditions; keeping the report path fail-closed prevents a
    caller from treating a partially inspected graph as valid.
    """

    errors: List[str] = []

    def collect(label: str, callback: Any) -> Any:
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001 - validator must be total
            errors.append("%s:%s" % (label, str(exc) or exc.__class__.__name__))
            return {}

    if not isinstance(result, SemanticResultV2):
        return {
            "passed": False,
            "error_count": 1,
            "errors": ("result is not SemanticResultV2",),
            "context_only_event_evidence_count": 0,
            "presentation_evidence_expansion_count": 0,
        }

    try:
        run_id = _p01_run_id(result.analysis_run_id)
    except Exception as exc:  # noqa: BLE001 - validator must be total
        run_id = ""
        errors.append("analysis_run_id:%s" % (str(exc) or exc.__class__.__name__))
    if result.schema_version != SCHEMA_VERSION:
        errors.append("schema_version_not_semantic_v2")
    if result.pipeline_version != P01_PIPELINE_VERSION:
        errors.append("pipeline_version_not_p01")
    if result.ruleset_version != P01_RULESET_VERSION:
        errors.append("ruleset_version_not_p01")
    if result.source != SHADOW_SOURCE:
        errors.append("non_shadow_source")
    if not isinstance(result.created_at, str) or not result.created_at.strip():
        errors.append("created_at_missing_or_invalid")

    # Do not silently coerce malformed result fields to empty tuples.  An
    # empty fallback would allow a partially inspected graph to pass when all
    # downstream collections happen to be empty.
    collection_values = {
        "messages": result.messages,
        "mentions": result.mentions,
        "claims": result.claims,
        "candidate_pairs": result.candidate_pairs,
        "pair_decisions": result.pair_decisions,
        "events": result.events,
        "topic_families": result.topic_families,
        "trends": result.trends,
        "presentations": result.presentations,
    }
    for collection_name, collection_value in collection_values.items():
        if not isinstance(collection_value, Sequence) or isinstance(collection_value, (str, bytes)):
            errors.append("%s must be a sequence" % collection_name)
    if not isinstance(result.message_roles, Mapping):
        errors.append("message_roles must be a mapping")
    if not isinstance(result.dialogue_segments, Sequence) or isinstance(result.dialogue_segments, (str, bytes)):
        errors.append("dialogue_segments must be a sequence")

    messages = result.messages if isinstance(result.messages, Sequence) else ()
    message_by_id = collect(
        "messages",
        lambda: _p01_validate_messages(messages, owner="validate_p01_invariants.messages"),
    )
    mentions = result.mentions if isinstance(result.mentions, Sequence) else ()
    mention_by_id = collect(
        "mentions",
        lambda: _p01_validate_mentions(
            mentions,
            run_id,
            messages=message_by_id,
            owner="validate_p01_invariants.mentions",
        ),
    )
    claims = result.claims if isinstance(result.claims, Sequence) else ()
    claim_by_id = collect(
        "claims",
        lambda: _p01_validate_claims(
            claims,
            run_id,
            messages=message_by_id,
            mentions=mention_by_id,
            owner="validate_p01_invariants.claims",
        ),
    )
    candidates = result.candidate_pairs if isinstance(result.candidate_pairs, Sequence) else ()
    candidate_by_id = collect(
        "candidate_pairs",
        lambda: _p01_validate_candidates(
            candidates,
            run_id,
            claim_by_id,
            owner="validate_p01_invariants.candidates",
        ),
    )
    decisions = result.pair_decisions if isinstance(result.pair_decisions, Sequence) else ()
    decision_by_id = collect(
        "pair_decisions",
        lambda: _p01_validate_decisions(
            decisions,
            run_id,
            claim_by_id,
            owner="validate_p01_invariants.decisions",
        ),
    )
    events = result.events if isinstance(result.events, Sequence) else ()
    event_by_id = collect(
        "events",
        lambda: _p01_validate_events(
            events,
            run_id,
            claim_by_id,
            decision_by_id,
            owner="validate_p01_invariants.events",
        ),
    )
    families = result.topic_families if isinstance(result.topic_families, Sequence) else ()
    family_by_id = collect(
        "topic_families",
        lambda: _p01_validate_families(
            families,
            run_id,
            event_by_id,
            claim_by_id,
            owner="validate_p01_invariants.families",
        ),
    )
    trends = result.trends if isinstance(result.trends, Sequence) else ()
    collect(
        "trends",
        lambda: _p01_validate_trends(
            trends,
            run_id,
            family_by_id,
            event_by_id,
            claim_by_id,
            owner="validate_p01_invariants.trends",
        ),
    )
    presentations = result.presentations if isinstance(result.presentations, Sequence) else ()
    collect(
        "presentations",
        lambda: _p01_validate_presentations(
            presentations,
            run_id,
            event_by_id,
            claim_by_id,
            owner="validate_p01_invariants.presentations",
        ),
    )

    eligible_supplied = event_eligible_message_ids is not None
    try:
        eligible = {
            str(value) for value in (event_eligible_message_ids or ())
        }
    except Exception as exc:  # noqa: BLE001 - validator must be total
        eligible = set()
        errors.append("event_eligible_message_ids:%s" % (str(exc) or exc.__class__.__name__))
    if eligible - set(message_by_id):
        errors.append(
            "event_eligible_message_ids has external input_id(s): %s"
            % ",".join(sorted(eligible - set(message_by_id)))
        )

    for claim in claims:
        if not isinstance(claim, ClaimV2):
            continue
        if not claim.evidence_refs or not claim.source_message_ids:
            errors.append("claim_missing_evidence:%s" % claim.claim_id)
        if eligible_supplied and claim.message_id not in eligible:
            errors.append("claim_context_leak:%s" % claim.claim_id)
        for target in claim.target_entity_ids:
            target_mentions = [
                mention_by_id[value]
                for value in claim.event_mention_ids
                if value in mention_by_id
                and mention_by_id[value].normalized_id == target
            ]
            if not target_mentions:
                errors.append("claim_target_without_span:%s" % claim.claim_id)

    for decision in decisions:
        if not isinstance(decision, PairDecisionV2):
            continue
        if decision.relation == RELATION_SAME_EVENT:
            left = claim_by_id.get(decision.left_claim_id)
            right = claim_by_id.get(decision.right_claim_id)
            if left is None or right is None:
                errors.append("same_event_unknown_claim:%s" % decision.decision_id)
            elif decision.must_not_link:
                errors.append("same_event_mnl:%s" % decision.decision_id)
            elif not _p01_same_scope(left, right):
                errors.append("same_event_cross_scope:%s" % decision.decision_id)
            elif not _p01_has_core_identity(left) or not _p01_has_core_identity(right):
                errors.append("same_event_unknown_entity:%s" % decision.decision_id)
            elif _p01_unknown_action_id(left.action) or _p01_unknown_action_id(right.action):
                errors.append("same_event_unknown_action:%s" % decision.decision_id)
            elif _p01_cross_block(left, right) and _p01_explicit_reply(left, right):
                errors.append("same_event_cross_block_reply:%s" % decision.decision_id)
            elif not (
                _p01_explicit_reply(left, right)
                or left.message_id == right.message_id
                or _p01_has_shared_instance(left, right)
                or _p01_has_continuation_evidence(left, right)
            ):
                errors.append("same_event_without_strong_link:%s" % decision.decision_id)

    for event in events:
        if not isinstance(event, EventV2):
            continue
        current_claim_ids = set(event.claim_ids)
        for decision in decisions:
            if (
                isinstance(decision, PairDecisionV2)
                and decision.left_claim_id in current_claim_ids
                and decision.right_claim_id in current_claim_ids
                and decision.must_not_link
            ):
                errors.append("event_mnl:%s" % event.event_id)
        if current_claim_ids.issubset(set(claim_by_id)):
            expected_evidence = _p01_evidence_union(
                [claim_by_id[value] for value in event.claim_ids]
            )
            if expected_evidence != event.evidence_refs:
                errors.append("event_evidence_expansion:%s" % event.event_id)
        if eligible_supplied and set(event.source_message_ids) - eligible:
            errors.append("event_context_leak:%s" % event.event_id)

    for presentation in presentations:
        if not isinstance(presentation, PresentationV2):
            continue
        event = event_by_id.get(presentation.event_id)
        if event is None:
            errors.append("presentation_unknown_event:%s" % presentation.presentation_id)
            continue
        if presentation.evidence_refs != event.evidence_refs:
            errors.append("presentation_evidence_expansion:%s" % presentation.presentation_id)
        if presentation.presentation_role != "do_not_display":
            sentence_claims = {
                value for sentence in presentation.sentences for value in sentence.claim_ids
            }
            if sentence_claims != set(presentation.supported_claim_ids):
                errors.append("presentation_claim_coverage:%s" % presentation.presentation_id)
            if any(
                not sentence.claim_ids or not sentence.message_ids
                for sentence in presentation.sentences
            ):
                errors.append("presentation_sentence_without_evidence:%s" % presentation.presentation_id)

    unique_errors = tuple(sorted(set(errors)))
    return {
        "passed": not unique_errors,
        "error_count": len(unique_errors),
        "errors": unique_errors,
        "context_only_event_evidence_count": 0,
        "presentation_evidence_expansion_count": sum(
            value.startswith("presentation_evidence_expansion:") for value in unique_errors
        ),
    }


def run_semantic_pipeline_p01(
    legacy_messages: Iterable[Mapping[str, Any]],
    *,
    analysis_run_id: Optional[str] = None,
    created_at: Optional[str] = None,
    max_gap_seconds: float = 15 * 60,
) -> SemanticResultV2:
    """Run the offline P0.1 refinement on development/synthetic messages.

    This entry point deliberately accepts only caller-provided mappings.  It
    does not read files, databases, configuration, network services, or
    production analysis results.  Upstream dialogue metadata is honored when
    present, while absent metadata is conservatively derived by the public
    segmenter.
    """

    raw_messages = list(legacy_messages)
    if any(not isinstance(message, Mapping) for message in raw_messages):
        raise TypeError("legacy_messages must contain mapping objects")
    raw_ids = [str(message.get("message_id") or "").strip() for message in raw_messages]
    if any(not value for value in raw_ids):
        raise ValueError("semantic_p0_1 requires every message to have message_id")
    if len(set(raw_ids)) != len(raw_ids):
        raise ValueError("duplicate message_id in semantic_p0_1 input")
    roles, annotations, raw_by_id, segment_dicts, ordered_ids = _p01_segment_context(
        raw_messages, max_gap_seconds=max_gap_seconds
    )
    segment_by_message = {
        message_id: str(annotation.get("segment_id") or "")
        for message_id, annotation in annotations.items()
    }
    eligible_ids: set[str] = set()
    for message_id in ordered_ids:
        raw = raw_by_id[message_id]
        role = _p01_role_value(raw, roles.get(message_id))
        if _p01_is_event_eligible(raw, role, annotations.get(message_id)):
            eligible_ids.add(message_id)
    messages = legacy_messages_to_v2(raw_messages, scope_boundaries=True)
    # Carry the normalized heuristic segment boundary onto every P0.1
    # message as well as every claim.  This makes mention provenance complete
    # even when the caller did not provide upstream segment metadata.
    messages = tuple(
        replace(
            item,
            dialogue_segment_id=segment_by_message.get(item.message_id) or item.dialogue_segment_id,
            block_id=str(annotations.get(item.message_id, {}).get("block_id") or item.block_id or "") or None,
            account_id=str(
                annotations.get(item.message_id, {}).get("account_id")
                or item.account_id
                or "default"
            ),
        )
        for item in messages
    )
    message_identity = tuple(
        sorted(
            stable_id(
                "message_input",
                {
                    "message_id": item.message_id,
                    "account_id": item.account_id,
                    "chat_id": item.chat_id,
                    "speaker_id": item.speaker_id,
                    "content": _normalized_text(item.content),
                    "timestamp": item.timestamp,
                    "reply_to": item.reply_to_message_id,
                    "explicit_instance_id": item.explicit_instance_id,
                    "dialogue_segment_id": item.dialogue_segment_id,
                    "block_id": item.block_id,
                },
                pipeline_version=P01_PIPELINE_VERSION,
                ruleset_version=P01_RULESET_VERSION,
            )
            for item in messages
        )
    )
    boundary_identity = tuple(
        sorted(
            (
                message_id,
                str(annotation.get("account_id") or raw_by_id[message_id].get("account_id") or "default"),
                str(annotation.get("chat_id") or raw_by_id[message_id].get("chat_id") or "unknown-chat"),
                str(annotation.get("segment_id") or ""),
                str(annotation.get("block_id") or ""),
            )
            for message_id, annotation in annotations.items()
        )
    )
    if analysis_run_id is not None:
        analysis_run_id = _p01_run_id(analysis_run_id)
    resolved_run_id = analysis_run_id or stable_id(
        "analysis_run",
        {"messages": message_identity, "boundaries": boundary_identity},
        pipeline_version=P01_PIPELINE_VERSION,
        ruleset_version=P01_RULESET_VERSION,
    )
    resolved_created_at = _timestamp_text(created_at) if created_at else _default_created_at(messages)
    mentions, claims = extract_mentions_and_claims_p01(
        messages,
        resolved_run_id,
        resolved_created_at,
        segment_by_message=segment_by_message,
        message_roles=roles,
        eligible_message_ids=eligible_ids,
    )
    candidate_pairs = generate_candidate_pairs_p01(claims, resolved_run_id, resolved_created_at)
    pair_decisions = tuple(
        classify_claim_pair_p01(
            next(item for item in claims if item.claim_id == candidate.left_claim_id),
            next(item for item in claims if item.claim_id == candidate.right_claim_id),
            resolved_run_id,
            resolved_created_at,
        )
        for candidate in candidate_pairs
    )
    pair_decisions = tuple(sorted(pair_decisions, key=lambda item: item.decision_id))
    events = build_events_p01(claims, pair_decisions, resolved_run_id, resolved_created_at)
    families = derive_topic_families_p01(events, claims, resolved_run_id, resolved_created_at)
    trends = derive_trends_p01(events, families, claims, resolved_run_id, resolved_created_at)
    presentations = derive_presentations_p01(events, claims, resolved_run_id, resolved_created_at)
    warnings = tuple(
        sorted(
            "%s:%s" % (message.message_id, warning)
            for message in messages
            for warning in message.compatibility_warnings
        )
    )
    result = SemanticResultV2(
        analysis_run_id=resolved_run_id,
        created_at=resolved_created_at,
        messages=messages,
        mentions=mentions,
        claims=claims,
        pair_decisions=pair_decisions,
        events=events,
        topic_families=families,
        trends=trends,
        presentations=presentations,
        warnings=warnings,
        pipeline_version=P01_PIPELINE_VERSION,
        ruleset_version=P01_RULESET_VERSION,
        candidate_pairs=tuple(candidate_pairs),
        message_roles={key: roles[key] for key in sorted(roles)},
        dialogue_segments=tuple(segment_dicts),
    )
    invariant = validate_p01_invariants(result, event_eligible_message_ids=eligible_ids)
    if not invariant["passed"]:
        raise ValueError("semantic_p0_1 invariant failure: %s" % "; ".join(invariant["errors"]))
    return result


def p01_result_to_legacy_preview(result: SemanticResultV2) -> Dict[str, Any]:
    """Return an explicit read-only P0.1 preview with candidate metadata."""

    preview = v2_result_to_legacy_preview(result)
    preview["candidate_pairs"] = [item.to_dict() for item in result.candidate_pairs]
    preview["message_roles"] = dict(result.message_roles)
    preview["dialogue_segments"] = [dict(item) for item in result.dialogue_segments]
    return preview


# ---------------------------------------------------------------------------
# P0.2: canonical message claims and bounded candidate blocking
# ---------------------------------------------------------------------------

# P0.1 was intentionally clause-oriented.  The development contract used for
# the P0.2 iteration has one canonical claim per eligible message, with one
# exact span covering the complete trimmed message.  Keeping this refinement
# in its own namespace makes the experiment replayable without changing P0.1.


def _p02_version_mention(mention: MentionV2) -> MentionV2:
    stage = str(mention.provenance.stage).replace("_p01", "_p02")
    if stage == "mention_extraction":
        stage = "mention_extraction_p02"
    provenance = replace(
        mention.provenance,
        parameters_version=P02_RULESET_VERSION,
        stage=stage,
    )
    return replace(
        mention,
        provenance=provenance,
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
    )


def _p02_version_provenance(provenance: ProvenanceV2) -> ProvenanceV2:
    stage = str(provenance.stage).replace("_p01", "_p02")
    if stage == "mention_extraction":
        stage = "mention_extraction_p02"
    return replace(
        provenance,
        parameters_version=P02_RULESET_VERSION,
        stage=stage,
    )


def _p02_prepare_message(message: MessageV2) -> MessageV2:
    """Return a validated message without repairing caller metadata.

    Earlier development versions scoped bare segment/block labels here.  That
    made a direct stage call silently change its input boundary, which is
    unsafe when an object from another run is accidentally supplied.  P0.2
    boundaries are now assigned by the runner and checked at every stage;
    direct callers must provide the same scoped IDs.
    """

    if not isinstance(message, MessageV2):
        raise ValueError("P0.2 message must be MessageV2")
    return message


# ---------------------------------------------------------------------------
# P0.2 governance gates
# ---------------------------------------------------------------------------
#
# P0.2 deliberately has its own metadata gate instead of routing objects
# through the P0.1 validator.  A stage must consume one run, one schema,
# one pipeline/ruleset pair, and provenance that is closed over the supplied
# objects and their scoped boundaries.  These helpers are metadata/graph
# checks only: they do not alter extraction or relation semantics.

_P02_STAGE_NAMES = {
    "mention_extraction_p02",
    "event_trigger_extraction_p02",
    "claim_extraction_p02",
    "candidate_generation_p02",
    "pair_classification_p02",
    "event_construction_p02",
    "topic_family_derivation_p02",
    "trend_derivation_p02",
    "presentation_derivation_p02",
}


def _p02_run_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("P0.2 analysis_run_id must be a non-empty string")
    return value.strip()


def _p02_raise(errors: Iterable[Any]) -> None:
    values = tuple(sorted({str(value) for value in errors if str(value)}))
    if values:
        raise ValueError("P0.2 governance failure: " + "; ".join(values))


def _p02_metadata_errors(
    item: Any,
    run_id: str,
    owner: str,
    *,
    expected_stages: Optional[Iterable[str]] = None,
) -> List[str]:
    """Validate run/schema/pipeline/ruleset/provenance metadata on one DTO."""

    errors: List[str] = []
    if not isinstance(item, SerializableV2):
        return ["%s is not a semantic DTO" % owner]
    expected_run = str(run_id)
    if getattr(item, "analysis_run_id", None) != expected_run:
        errors.append("%s analysis_run_id mismatch" % owner)
    if getattr(item, "schema_version", None) != SCHEMA_VERSION:
        errors.append("%s schema_version mismatch" % owner)
    if getattr(item, "pipeline_version", None) != P02_PIPELINE_VERSION:
        errors.append("%s pipeline_version mismatch" % owner)
    if getattr(item, "ruleset_version", None) != P02_RULESET_VERSION:
        errors.append("%s ruleset_version mismatch" % owner)
    provenance = getattr(item, "provenance", None)
    if not isinstance(provenance, ProvenanceV2):
        errors.append("%s provenance is missing or malformed" % owner)
        return errors
    if provenance.parameters_version != P02_RULESET_VERSION:
        errors.append("%s provenance parameters_version mismatch" % owner)
    stage = provenance.stage if isinstance(provenance.stage, str) else ""
    allowed_stages = set(expected_stages or _P02_STAGE_NAMES)
    if stage not in allowed_stages:
        errors.append("%s provenance stage is not allowed: %s" % (owner, stage or "<empty>"))
    input_ids = provenance.input_ids
    if not isinstance(input_ids, (tuple, list)):
        errors.append("%s provenance input_ids is not a sequence" % owner)
        return errors
    if not input_ids:
        errors.append("%s provenance input_ids is empty" % owner)
    elif any(not isinstance(value, str) or not value.strip() for value in input_ids):
        errors.append("%s provenance input_ids contains an empty/non-string ID" % owner)
    elif len(set(input_ids)) != len(input_ids):
        errors.append("%s provenance input_ids contains duplicates" % owner)
    return errors


def _p02_check_provenance_inputs(
    item: Any,
    *,
    owner: str,
    allowed_ids: Iterable[str],
    required_boundary_ids: Iterable[str] = (),
    required_ids: Iterable[str] = (),
) -> List[str]:
    """Ensure provenance is closed over stage inputs and required boundaries."""

    provenance = getattr(item, "provenance", None)
    if not isinstance(provenance, ProvenanceV2):
        return []
    try:
        input_ids = tuple(provenance.input_ids)
    except TypeError:
        return ["%s provenance input_ids is not iterable" % owner]
    allowed = {value for value in allowed_ids if isinstance(value, str) and value}
    boundaries = {value for value in required_boundary_ids if isinstance(value, str) and value}
    allowed.update(boundaries)
    string_ids = tuple(value for value in input_ids if isinstance(value, str))
    errors: List[str] = []
    if len(string_ids) != len(input_ids):
        errors.append("%s provenance has non-string input_id(s)" % owner)
    unknown = sorted(set(string_ids) - allowed)
    if unknown:
        errors.append(
            "%s provenance has external input_id(s): %s"
            % (owner, ",".join(unknown))
        )
    missing_boundaries = sorted(boundaries - set(string_ids))
    if missing_boundaries:
        errors.append(
            "%s provenance is missing boundary ID(s): %s"
            % (owner, ",".join(missing_boundaries))
        )
    missing_ids = sorted(
        {value for value in required_ids if isinstance(value, str) and value}
        - set(string_ids)
    )
    if missing_ids:
        errors.append("%s provenance is missing input ID(s): %s" % (owner, ",".join(missing_ids)))
    return errors


def _p02_scope_boundaries(
    *,
    segment_id: Any,
    block_id: Any,
    scope: Tuple[str, str],
    owner: str,
) -> Tuple[Tuple[str, ...], List[str]]:
    """Validate a DTO's segment/block labels and return canonical IDs."""

    errors: List[str] = []
    segment = segment_id
    block = block_id
    if not isinstance(segment, str) or not segment.strip():
        errors.append("%s is missing dialogue segment boundary" % owner)
        segment = ""
    values = tuple(value for value in (segment, block) if isinstance(value, str) and value)
    errors.extend(_p01_boundary_errors(values, owner=owner, scope=scope))
    if isinstance(segment, str) and segment and _p01_boundary_parts(segment):
        if _p01_boundary_parts(segment)[0] != "segment":
            errors.append("%s dialogue segment boundary has wrong kind" % owner)
    if isinstance(block, str) and block and _p01_boundary_parts(block):
        if _p01_boundary_parts(block)[0] != "block":
            errors.append("%s block boundary has wrong kind" % owner)
    return tuple(values), errors


def _p02_validate_messages(
    messages: Sequence[MessageV2],
    *,
    segment_by_message: Optional[Mapping[str, str]] = None,
    owner: str = "messages",
) -> Dict[str, MessageV2]:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ValueError("%s must be a sequence of MessageV2 objects" % owner)
    output: Dict[str, MessageV2] = {}
    errors: List[str] = []
    for index, message in enumerate(messages):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(message, MessageV2):
            errors.append("%s is not MessageV2" % row_owner)
            continue
        message_id = message.message_id
        if not isinstance(message_id, str) or not message_id.strip():
            errors.append("%s message_id is empty/non-string" % row_owner)
            continue
        if message_id in output:
            errors.append("duplicate message_id: %s" % message_id)
            continue
        output[message_id] = message
        if message.schema_version != SCHEMA_VERSION:
            errors.append("%s schema_version mismatch" % row_owner)
        if message.source != SHADOW_SOURCE:
            errors.append("%s source mismatch" % row_owner)
        scope = _p01_scope_of_message(message)
        _, boundary_errors = _p02_scope_boundaries(
            segment_id=message.dialogue_segment_id,
            block_id=message.block_id,
            scope=scope,
            owner=row_owner,
        )
        errors.extend(boundary_errors)
        if message.reply_to_message_id is not None and not isinstance(message.reply_to_message_id, str):
            errors.append("%s reply_to_message_id is non-string" % row_owner)
    message_ids = set(output)
    for index, message in enumerate(messages):
        if not isinstance(message, MessageV2):
            continue
        if message.reply_to_message_id and message.reply_to_message_id not in message_ids:
            errors.append("%s[%d] reply_to_message_id is external" % (owner, index))
    if segment_by_message is not None:
        if not isinstance(segment_by_message, Mapping):
            errors.append("segment_by_message is not a mapping")
        else:
            for message_id, message in output.items():
                if message_id not in segment_by_message:
                    errors.append("segment_by_message missing %s" % message_id)
                    continue
                supplied = segment_by_message.get(message_id)
                if not isinstance(supplied, str) or not _p01_is_boundary_id(supplied):
                    errors.append("segment_by_message has unscoped boundary for %s" % message_id)
                elif supplied != message.dialogue_segment_id:
                    errors.append("segment_by_message disagrees with message boundary: %s" % message_id)
            external = sorted(set(segment_by_message) - message_ids)
            if external:
                errors.append("segment_by_message has external input_id(s): %s" % ",".join(map(str, external)))
    _p02_raise(errors)
    return output


def _p02_validate_mentions(
    mentions: Sequence[MentionV2],
    run_id: str,
    *,
    messages: Optional[Mapping[str, MessageV2]] = None,
    owner: str = "mentions",
) -> Dict[str, MentionV2]:
    if not isinstance(mentions, Sequence) or isinstance(mentions, (str, bytes)):
        raise ValueError("%s must be a sequence of MentionV2 objects" % owner)
    output: Dict[str, MentionV2] = {}
    errors: List[str] = []
    for index, mention in enumerate(mentions):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(mention, MentionV2):
            errors.append("%s is not MentionV2" % row_owner)
            continue
        mention_id = mention.mention_id
        if not isinstance(mention_id, str) or not mention_id.strip():
            errors.append("%s mention_id is empty/non-string" % row_owner)
            continue
        if mention_id in output:
            errors.append("duplicate mention_id: %s" % mention_id)
            continue
        output[mention_id] = mention
        errors.extend(_p02_metadata_errors(mention, run_id, row_owner, expected_stages={"mention_extraction_p02", "event_trigger_extraction_p02"}))
        message = messages.get(mention.message_id) if messages is not None and isinstance(mention.message_id, str) else None
        if messages is not None and message is None:
            errors.append("%s message_id is external" % row_owner)
        source_ids = mention.source_message_ids
        if not isinstance(source_ids, (tuple, list)) or not source_ids or mention.message_id not in source_ids:
            errors.append("%s source_message_ids do not include message_id" % row_owner)
        elif set(source_ids) != {mention.message_id}:
            # P0.2 mentions are clause/message-local.  Do not let a caller
            # widen the declared source set and then use that widened set to
            # smuggle an otherwise external provenance input through the
            # stage gate.
            errors.append("%s source_message_ids are not message-local" % row_owner)
        if messages is not None and set(source_ids or ()) - set(messages):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        evidence = mention.evidence_refs
        if not isinstance(evidence, (tuple, list)) or not evidence:
            errors.append("%s evidence_refs is empty" % row_owner)
            evidence = ()
        if any(not isinstance(item, EvidenceRefV2) for item in evidence):
            errors.append("%s evidence_refs is malformed" % row_owner)
        evidence_ids = {item.message_id for item in evidence if isinstance(item, EvidenceRefV2)}
        if mention.message_id not in evidence_ids:
            errors.append("%s evidence_refs do not include message_id" % row_owner)
        if messages is not None and evidence_ids - set(messages):
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        if message is not None:
            boundaries, boundary_errors = _p02_scope_boundaries(
                segment_id=message.dialogue_segment_id,
                block_id=message.block_id,
                scope=_p01_scope_of_message(message),
                owner=row_owner,
            )
        else:
            boundaries = tuple(value for value in (mention.provenance.input_ids if isinstance(mention.provenance, ProvenanceV2) else ()) if _p01_is_boundary_id(value))
            boundary_errors = _p01_boundary_errors(boundaries, owner=row_owner)
            if not any(_p01_boundary_parts(value) and _p01_boundary_parts(value)[0] == "segment" for value in boundaries):
                boundary_errors.append("%s is missing dialogue segment boundary" % row_owner)
        errors.extend(boundary_errors)
        # ``source_message_ids`` is checked above and is intentionally not an
        # authority for provenance closure.  The only valid mention lineage is
        # its own source message plus that message's scoped boundaries.
        allowed = {mention.message_id} | set(boundaries)
        errors.extend(_p02_check_provenance_inputs(
            mention,
            owner=row_owner,
            allowed_ids=allowed,
            required_boundary_ids=boundaries,
            required_ids=(mention.message_id,),
        ))
        if message is not None:
            if not isinstance(mention.span_start, int) or not isinstance(mention.span_end, int) or mention.span_start < 0 or mention.span_end <= mention.span_start or mention.span_end > len(message.content):
                errors.append("%s has invalid evidence span" % row_owner)
            elif message.content[mention.span_start:mention.span_end] != mention.evidence_text:
                errors.append("%s evidence span does not match message" % row_owner)
    _p02_raise(errors)
    return output


def _p02_validate_claims(
    claims: Sequence[ClaimV2],
    run_id: str,
    *,
    messages: Optional[Mapping[str, MessageV2]] = None,
    mentions: Optional[Mapping[str, MentionV2]] = None,
    owner: str = "claims",
) -> Dict[str, ClaimV2]:
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
        raise ValueError("%s must be a sequence of ClaimV2 objects" % owner)
    output: Dict[str, ClaimV2] = {}
    errors: List[str] = []
    for index, claim in enumerate(claims):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(claim, ClaimV2):
            errors.append("%s is not ClaimV2" % row_owner)
            continue
        claim_id = claim.claim_id
        if not isinstance(claim_id, str) or not claim_id.strip():
            errors.append("%s claim_id is empty/non-string" % row_owner)
            continue
        if claim_id in output:
            errors.append("duplicate claim_id: %s" % claim_id)
            continue
        output[claim_id] = claim
        errors.extend(_p02_metadata_errors(claim, run_id, row_owner, expected_stages={"claim_extraction_p02"}))
        message = messages.get(claim.message_id) if messages is not None and isinstance(claim.message_id, str) else None
        if messages is not None and message is None:
            errors.append("%s message_id is external" % row_owner)
        source_ids = claim.source_message_ids
        if not isinstance(source_ids, (tuple, list)) or not source_ids or claim.message_id not in source_ids:
            errors.append("%s source_message_ids do not include message_id" % row_owner)
        elif set(source_ids) != {claim.message_id}:
            # P0.2 emits one canonical claim for one eligible message.  Keep
            # this exact so source metadata cannot be expanded in tandem with
            # provenance to make unrelated message evidence look local.
            errors.append("%s source_message_ids are not message-local" % row_owner)
        if messages is not None and set(source_ids or ()) - set(messages):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        evidence = claim.evidence_refs
        if not isinstance(evidence, (tuple, list)) or len(evidence) != 1 or not isinstance(evidence[0], EvidenceRefV2):
            errors.append("%s evidence_refs is not one canonical reference" % row_owner)
            evidence = ()
        if evidence and evidence[0] != claim.evidence_span:
            errors.append("%s evidence_refs do not equal evidence_span" % row_owner)
        if evidence and evidence[0].message_id != claim.message_id:
            errors.append("%s evidence_span is not source message" % row_owner)
        if messages is not None and evidence and evidence[0].message_id not in messages:
            errors.append("%s evidence_refs contain external input_id(s)" % row_owner)
        mention_ids = claim.event_mention_ids
        if not isinstance(mention_ids, (tuple, list)):
            errors.append("%s event_mention_ids is malformed" % row_owner)
            mention_ids = ()
        if mentions is not None:
            unknown_mentions = sorted(set(mention_ids) - set(mentions))
            if unknown_mentions:
                errors.append("%s event_mention_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_mentions)))
            source_set = set(source_ids or ())
            for mention_id in mention_ids:
                mention = mentions.get(mention_id)
                if mention is not None and mention.message_id not in source_set:
                    errors.append("%s event mention is outside source messages" % row_owner)
        boundaries, boundary_errors = _p02_scope_boundaries(
            segment_id=claim.dialogue_segment_id,
            block_id=claim.block_id,
            scope=_p01_scope_of_claim(claim),
            owner=row_owner,
        )
        errors.extend(boundary_errors)
        if message is not None and _p01_scope_of_message(message) != _p01_scope_of_claim(claim):
            errors.append("%s scope disagrees with source message" % row_owner)
        if message is not None and claim.dialogue_segment_id != message.dialogue_segment_id:
            errors.append("%s dialogue segment disagrees with source message" % row_owner)
        if message is not None and claim.block_id != message.block_id:
            errors.append("%s block boundary disagrees with source message" % row_owner)
        # Claim provenance is closed over the canonical source message, its
        # event mentions, and its own scoped boundaries.  Declared source IDs
        # are validated independently and must not widen this allow-list.
        allowed = {claim.message_id} | set(mention_ids) | set(boundaries)
        errors.extend(_p02_check_provenance_inputs(
            claim,
            owner=row_owner,
            allowed_ids=allowed,
            required_boundary_ids=boundaries,
            required_ids=tuple(mention_ids) + (claim.message_id,),
        ))
    _p02_raise(errors)
    return output


def _p02_expected_evidence(claims: Iterable[ClaimV2]) -> Tuple[EvidenceRefV2, ...]:
    values = {
        item
        for claim in claims
        for item in (claim.evidence_refs if isinstance(claim, ClaimV2) else ())
        if isinstance(item, EvidenceRefV2)
    }
    return tuple(sorted(values, key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text)))


def _p02_validate_candidates(
    candidates: Sequence[CandidatePairV2],
    run_id: str,
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "candidate_pairs",
) -> Dict[str, CandidatePairV2]:
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("%s must be a sequence of CandidatePairV2 objects" % owner)
    output: Dict[str, CandidatePairV2] = {}
    errors: List[str] = []
    for index, candidate in enumerate(candidates):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(candidate, CandidatePairV2):
            errors.append("%s is not CandidatePairV2" % row_owner)
            continue
        candidate_id = candidate.candidate_id
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            errors.append("%s candidate_id is empty/non-string" % row_owner)
            continue
        if candidate_id in output:
            errors.append("duplicate candidate_id: %s" % candidate_id)
            continue
        output[candidate_id] = candidate
        errors.extend(_p02_metadata_errors(candidate, run_id, row_owner, expected_stages={"candidate_generation_p02"}))
        left_id, right_id = candidate.left_claim_id, candidate.right_claim_id
        if not isinstance(left_id, str) or not isinstance(right_id, str) or not left_id or not right_id or left_id == right_id:
            errors.append("%s has invalid claim pair" % row_owner)
            left = right = None
        else:
            left, right = claims.get(left_id), claims.get(right_id)
        if left is None or right is None:
            errors.append("%s claim pair contains external input_id(s)" % row_owner)
        boundaries = tuple(dict.fromkeys(
            (_p01_claim_boundary_ids(left) if left else ())
            + (_p01_claim_boundary_ids(right) if right else ())
        ))
        if left is not None and right is not None and not _p02_same_scope(left, right):
            errors.append("%s crosses account/chat boundary" % row_owner)
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        expected_sources = set(left.source_message_ids if left else ()) | set(right.source_message_ids if right else ())
        source_ids = candidate.source_message_ids
        if not isinstance(source_ids, (tuple, list)) or not set(source_ids).issubset(expected_sources):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        elif set(source_ids) != expected_sources:
            errors.append("%s source_message_ids do not equal claim sources" % row_owner)
        expected_evidence = _p02_expected_evidence((item for item in (left, right) if item is not None))
        if tuple(candidate.evidence_refs) != expected_evidence:
            errors.append("%s evidence_refs do not equal claim evidence" % row_owner)
        errors.extend(_p02_check_provenance_inputs(
            candidate,
            owner=row_owner,
            # Candidate lineage is exactly the two claims plus their scoped
            # boundaries.  Source-message fields are derived evidence, not a
            # provenance authority.
            allowed_ids={left_id, right_id} | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=(left_id, right_id),
        ))
    _p02_raise(errors)
    return output


def _p02_validate_decisions(
    decisions: Sequence[PairDecisionV2],
    run_id: str,
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "pair_decisions",
) -> Dict[str, PairDecisionV2]:
    if not isinstance(decisions, Sequence) or isinstance(decisions, (str, bytes)):
        raise ValueError("%s must be a sequence of PairDecisionV2 objects" % owner)
    output: Dict[str, PairDecisionV2] = {}
    errors: List[str] = []
    for index, decision in enumerate(decisions):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(decision, PairDecisionV2):
            errors.append("%s is not PairDecisionV2" % row_owner)
            continue
        decision_id = decision.decision_id
        if not isinstance(decision_id, str) or not decision_id.strip():
            errors.append("%s decision_id is empty/non-string" % row_owner)
            continue
        if decision_id in output:
            errors.append("duplicate decision_id: %s" % decision_id)
            continue
        output[decision_id] = decision
        errors.extend(_p02_metadata_errors(decision, run_id, row_owner, expected_stages={"pair_classification_p02"}))
        left_id, right_id = decision.left_claim_id, decision.right_claim_id
        left = claims.get(left_id) if isinstance(left_id, str) else None
        right = claims.get(right_id) if isinstance(right_id, str) else None
        if left is None or right is None or left_id == right_id:
            errors.append("%s claim pair contains external/duplicate input_id(s)" % row_owner)
        boundaries = tuple(dict.fromkeys(
            (_p01_claim_boundary_ids(left) if left else ())
            + (_p01_claim_boundary_ids(right) if right else ())
        ))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        if decision.relation not in EVENT_RELATIONS:
            errors.append("%s relation is unknown" % row_owner)
        if decision.relation == RELATION_SAME_EVENT and (
            decision.must_not_link
            or left is None
            or right is None
            or not _p02_same_scope(left, right)
            or bool(_p02_global_conflict_reasons(left, right))
        ):
            errors.append("%s same_event violates hard boundary" % row_owner)
        expected_sources = set(left.source_message_ids if left else ()) | set(right.source_message_ids if right else ())
        source_ids = decision.source_message_ids
        if not isinstance(source_ids, (tuple, list)) or not set(source_ids).issubset(expected_sources):
            errors.append("%s source_message_ids contain external input_id(s)" % row_owner)
        elif set(source_ids) != expected_sources:
            errors.append("%s source_message_ids do not equal claim sources" % row_owner)
        expected_evidence = _p02_expected_evidence((item for item in (left, right) if item is not None))
        if tuple(decision.evidence_refs) != expected_evidence:
            errors.append("%s evidence_refs do not equal claim evidence" % row_owner)
        if decision.must_not_link and not decision.must_not_link_reason_codes:
            errors.append("%s must_not_link has no reason codes" % row_owner)
        errors.extend(_p02_check_provenance_inputs(
            decision,
            owner=row_owner,
            # Decision lineage is closed over its two claim inputs and their
            # scoped boundaries; source-message metadata cannot widen it.
            allowed_ids={left_id, right_id} | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=(left_id, right_id),
        ))
    _p02_raise(errors)
    return output


def _p02_validate_events(
    events: Sequence[EventV2],
    run_id: str,
    claims: Mapping[str, ClaimV2],
    decisions: Optional[Mapping[str, PairDecisionV2]] = None,
    *,
    owner: str = "events",
) -> Dict[str, EventV2]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("%s must be a sequence of EventV2 objects" % owner)
    output: Dict[str, EventV2] = {}
    errors: List[str] = []
    decisions = decisions or {}
    for index, event in enumerate(events):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(event, EventV2):
            errors.append("%s is not EventV2" % row_owner)
            continue
        event_id = event.event_id
        if not isinstance(event_id, str) or not event_id.strip():
            errors.append("%s event_id is empty/non-string" % row_owner)
            continue
        if event_id in output:
            errors.append("duplicate event_id: %s" % event_id)
            continue
        output[event_id] = event
        errors.extend(_p02_metadata_errors(event, run_id, row_owner, expected_stages={"event_construction_p02"}))
        claim_ids = event.claim_ids
        if not isinstance(claim_ids, (tuple, list)) or not claim_ids:
            errors.append("%s has no claims" % row_owner)
            claim_ids = ()
        unknown_claims = sorted(set(claim_ids) - set(claims))
        if unknown_claims:
            errors.append("%s claim_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_claims)))
        event_claims = [claims[value] for value in claim_ids if value in claims]
        scopes = {_p01_scope_of_claim(item) for item in event_claims}
        if len(scopes) > 1:
            errors.append("%s merges claims across account/chat boundaries" % row_owner)
        boundaries = tuple(dict.fromkeys(boundary for item in event_claims for boundary in _p01_claim_boundary_ids(item)))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        relation_ids = event.relation_decision_ids
        if not isinstance(relation_ids, (tuple, list)):
            errors.append("%s relation_decision_ids is malformed" % row_owner)
            relation_ids = ()
        if decisions and not set(relation_ids).issubset(set(decisions)):
            errors.append("%s relation_decision_ids contain external input_id(s)" % row_owner)
        for relation_id in relation_ids:
            decision = decisions.get(relation_id)
            if decision is not None and (decision.relation != RELATION_SAME_EVENT or decision.must_not_link):
                errors.append("%s contains a non-merge decision" % row_owner)
        expected_sources = {value for claim in event_claims for value in claim.source_message_ids}
        if set(event.source_message_ids) != expected_sources:
            errors.append("%s source_message_ids do not equal claim sources" % row_owner)
        expected_evidence = _p02_expected_evidence(event_claims)
        if tuple(event.evidence_refs) != expected_evidence:
            errors.append("%s evidence_refs do not equal claim evidence" % row_owner)
        # Events may cite only their claim inputs, merge decisions, and the
        # scoped boundaries carried by those claims.  Message IDs remain
        # auditable evidence but are not provenance authorities.
        allowed = set(claim_ids) | set(relation_ids) | set(boundaries)
        errors.extend(_p02_check_provenance_inputs(
            event,
            owner=row_owner,
            allowed_ids=allowed,
            required_boundary_ids=boundaries,
            required_ids=tuple(claim_ids),
        ))
    _p02_raise(errors)
    return output


def _p02_validate_families(
    families: Sequence[TopicFamilyV2],
    run_id: str,
    events: Mapping[str, EventV2],
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "topic_families",
) -> Dict[str, TopicFamilyV2]:
    if not isinstance(families, Sequence) or isinstance(families, (str, bytes)):
        raise ValueError("%s must be a sequence of TopicFamilyV2 objects" % owner)
    output: Dict[str, TopicFamilyV2] = {}
    errors: List[str] = []
    for index, family in enumerate(families):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(family, TopicFamilyV2):
            errors.append("%s is not TopicFamilyV2" % row_owner)
            continue
        family_id = family.topic_family_id
        if not isinstance(family_id, str) or not family_id.strip():
            errors.append("%s topic_family_id is empty/non-string" % row_owner)
            continue
        if family_id in output:
            errors.append("duplicate topic_family_id: %s" % family_id)
            continue
        output[family_id] = family
        errors.extend(_p02_metadata_errors(family, run_id, row_owner, expected_stages={"topic_family_derivation_p02"}))
        event_ids = family.event_ids
        if not isinstance(event_ids, (tuple, list)) or not event_ids:
            errors.append("%s has no events" % row_owner)
            event_ids = ()
        unknown_events = sorted(set(event_ids) - set(events))
        if unknown_events:
            errors.append("%s event_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_events)))
        family_events = [events[value] for value in event_ids if value in events]
        family_claims = [claims[claim_id] for event in family_events for claim_id in event.claim_ids if claim_id in claims]
        boundaries = tuple(dict.fromkeys(boundary for claim in family_claims for boundary in _p01_claim_boundary_ids(claim)))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        expected_sources = {value for claim in family_claims for value in claim.source_message_ids}
        if set(family.source_message_ids) != expected_sources:
            errors.append("%s source_message_ids do not equal claim sources" % row_owner)
        if tuple(family.evidence_refs) != _p02_expected_evidence(family_claims):
            errors.append("%s evidence_refs do not equal claim evidence" % row_owner)
        claim_ids = {claim.claim_id for claim in family_claims}
        errors.extend(_p02_check_provenance_inputs(
            family,
            owner=row_owner,
            # Family lineage is closed over its event/claim graph and scoped
            # boundaries; source-message fields are derived from that graph.
            allowed_ids=set(event_ids) | claim_ids | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=tuple(event_ids),
        ))
    _p02_raise(errors)
    return output


def _p02_validate_trends(
    trends: Sequence[TrendV2],
    run_id: str,
    families: Mapping[str, TopicFamilyV2],
    events: Mapping[str, EventV2],
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "trends",
) -> Dict[str, TrendV2]:
    if not isinstance(trends, Sequence) or isinstance(trends, (str, bytes)):
        raise ValueError("%s must be a sequence of TrendV2 objects" % owner)
    output: Dict[str, TrendV2] = {}
    errors: List[str] = []
    for index, trend in enumerate(trends):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(trend, TrendV2):
            errors.append("%s is not TrendV2" % row_owner)
            continue
        trend_id = trend.trend_id
        if not isinstance(trend_id, str) or not trend_id.strip():
            errors.append("%s trend_id is empty/non-string" % row_owner)
            continue
        if trend_id in output:
            errors.append("duplicate trend_id: %s" % trend_id)
            continue
        output[trend_id] = trend
        errors.extend(_p02_metadata_errors(trend, run_id, row_owner, expected_stages={"trend_derivation_p02"}))
        if trend.topic_family_id not in families:
            errors.append("%s topic_family_id is external" % row_owner)
        event_ids = trend.event_ids
        claim_ids = trend.claim_ids
        if not isinstance(event_ids, (tuple, list)):
            errors.append("%s event_ids is malformed" % row_owner)
            event_ids = ()
        if not isinstance(claim_ids, (tuple, list)):
            errors.append("%s claim_ids is malformed" % row_owner)
            claim_ids = ()
        unknown_events = sorted(set(event_ids) - set(events))
        unknown_claims = sorted(set(claim_ids) - set(claims))
        if unknown_events:
            errors.append("%s event_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_events)))
        if unknown_claims:
            errors.append("%s claim_ids contain external input_id(s): %s" % (row_owner, ",".join(unknown_claims)))
        trend_events = [events[value] for value in event_ids if value in events]
        expected_claim_ids = {claim_id for event in trend_events for claim_id in event.claim_ids}
        if set(claim_ids) != expected_claim_ids:
            errors.append("%s claim_ids do not equal event claims" % row_owner)
        trend_claims = [claims[value] for value in claim_ids if value in claims]
        boundaries = tuple(dict.fromkeys(boundary for claim in trend_claims for boundary in _p01_claim_boundary_ids(claim)))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        expected_sources = {value for claim in trend_claims for value in claim.source_message_ids}
        if set(trend.source_message_ids) != expected_sources:
            errors.append("%s source_message_ids do not equal claim sources" % row_owner)
        if tuple(trend.evidence_refs) != _p02_expected_evidence(trend_claims):
            errors.append("%s evidence_refs do not equal claim evidence" % row_owner)
        errors.extend(_p02_check_provenance_inputs(
            trend,
            owner=row_owner,
            # Trend lineage is closed over the family/event/claim graph and
            # scoped boundaries, never over caller-declared source metadata.
            allowed_ids={trend.topic_family_id} | set(event_ids) | set(claim_ids) | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=tuple(event_ids),
        ))
    _p02_raise(errors)
    return output


def _p02_validate_presentations(
    presentations: Sequence[PresentationV2],
    run_id: str,
    events: Mapping[str, EventV2],
    claims: Mapping[str, ClaimV2],
    *,
    owner: str = "presentations",
) -> Dict[str, PresentationV2]:
    if not isinstance(presentations, Sequence) or isinstance(presentations, (str, bytes)):
        raise ValueError("%s must be a sequence of PresentationV2 objects" % owner)
    output: Dict[str, PresentationV2] = {}
    errors: List[str] = []
    for index, card in enumerate(presentations):
        row_owner = "%s[%d]" % (owner, index)
        if not isinstance(card, PresentationV2):
            errors.append("%s is not PresentationV2" % row_owner)
            continue
        presentation_id = card.presentation_id
        if not isinstance(presentation_id, str) or not presentation_id.strip():
            errors.append("%s presentation_id is empty/non-string" % row_owner)
            continue
        if presentation_id in output:
            errors.append("duplicate presentation_id: %s" % presentation_id)
            continue
        output[presentation_id] = card
        errors.extend(_p02_metadata_errors(card, run_id, row_owner, expected_stages={"presentation_derivation_p02"}))
        event = events.get(card.event_id)
        if event is None:
            errors.append("%s event_id is external" % row_owner)
            event_claim_ids: Tuple[str, ...] = ()
            event_claims: Tuple[ClaimV2, ...] = ()
            boundaries: Tuple[str, ...] = ()
        else:
            event_claim_ids = tuple(event.claim_ids)
            event_claims = tuple(claims[value] for value in event_claim_ids if value in claims)
            boundaries = tuple(dict.fromkeys(boundary for claim in event_claims for boundary in _p01_claim_boundary_ids(claim)))
        errors.extend(_p01_boundary_errors(boundaries, owner=row_owner))
        supported = card.supported_claim_ids
        title_supported = card.title_support_claim_ids
        if not isinstance(supported, (tuple, list)) or not set(supported).issubset(set(event_claim_ids)):
            errors.append("%s supported_claim_ids are outside event" % row_owner)
        if not isinstance(title_supported, (tuple, list)) or not set(title_supported).issubset(set(event_claim_ids)):
            errors.append("%s title_support_claim_ids are outside event" % row_owner)
        expected_sources = {value for claim in event_claims for value in claim.source_message_ids}
        if set(card.source_message_ids) != expected_sources:
            errors.append("%s source_message_ids do not equal event sources" % row_owner)
        if tuple(card.evidence_refs) != _p02_expected_evidence(event_claims):
            errors.append("%s evidence_refs do not equal event evidence" % row_owner)
        errors.extend(_p02_check_provenance_inputs(
            card,
            owner=row_owner,
            # Presentation lineage is closed over its event/claim graph and
            # scoped boundaries; source-message metadata is evidence only.
            allowed_ids={card.event_id} | set(event_claim_ids) | set(boundaries),
            required_boundary_ids=boundaries,
            required_ids=(card.event_id,),
        ))
    _p02_raise(errors)
    return output


_P02_CONTINUATION_MAX_SECONDS = 15 * 60
_P02_CONTINUATION_MAX_TURNS = 3
_P02_INSTANCE_SENTINELS = frozenset(
    {
        "unknown",
        "unknown_instance",
        "unknown-instance",
        "instance_unknown",
        "instance-unknown",
        "none",
        "null",
        "nil",
        "n/a",
        "na",
        "missing",
        "unset",
        "not_set",
        "not-set",
        "sentinel",
        "placeholder",
        "?",
        "-",
    }
)
_P02_UNKNOWN_ENTITY_PATTERN = re.compile(
    r"^(?:\[?(?:PERSON|USER|PHONE|URL)_\d+(?::[^\]]+)?\]?|"
    r"(?:ENTITY_)?UNKNOWN(?:_ENTITY)?|entity:unknown:[0-9a-f]+)$",
    re.I,
)


def _p02_is_instance_sentinel(value: Any) -> bool:
    """Return whether a source value is a missing/placeholder instance key."""

    text = re.sub(r"\s+", "_", str(value or "").strip()).casefold()
    if not text:
        return True
    if text in _P02_INSTANCE_SENTINELS:
        return True
    return bool(
        re.fullmatch(
            r"(?:instance[_:-])?(?:unknown|none|null|missing|unset|sentinel|placeholder)",
            text,
            re.I,
        )
    )


def _p02_is_known_entity_id(value: Any) -> bool:
    """Keep opaque redaction placeholders and unknown fallbacks out of joins."""

    text = str(value or "").strip()
    if not text or _p01_unknown_entity_id(text) or _P02_UNKNOWN_ENTITY_PATTERN.fullmatch(text):
        return False
    rule = _P01_ENTITY_BY_ID.get(text)
    # The development extractor includes generic placeholder rules in the
    # mention layer.  They remain useful evidence, but never identify a core
    # object for relation merging.
    if rule is not None and str(rule.family_key).casefold() == "unknown":
        return False
    return True


def _p02_action_family(action: Any) -> Optional[str]:
    """Map canonical action labels to coarse compatibility families.

    Relation blocking should compare compatible action families rather than
    relying on a private product vocabulary.  Unknown labels are omitted so
    an opaque action can only be used when a verified instance is shared.
    """

    text = str(action or "").strip().casefold()
    if not text or text in {"unknown", "none", "null", "n/a", "na"}:
        return None
    families = {
        "reset": "service_operation",
        "outage": "service_operation",
        "state_update": "service_operation",
        "handle": "service_operation",
        "usage": "service_operation",
        "performance": "service_operation",
        "email_delivery": "account_access",
        "register": "account_access",
        "login": "account_access",
        "account_access": "account_access",
        "cost": "commercial",
        "purchase_or_upgrade": "commercial",
        "top_up": "commercial",
        "subscribe": "commercial",
        "split_order": "commercial",
        "schedule": "planning",
        "course_selection": "planning",
        "confirm_schedule": "planning",
        "risk": "security",
        "security": "security",
        "research": "workflow",
        "work_task": "workflow",
        "tool_configuration": "configuration",
        "configure": "configuration",
        "inspect": "diagnostic",
        "ask": "information_request",
        "job_search": "workflow",
        "release": "delivery",
        "publish": "delivery",
    }
    return families.get(text)


def _p02_known_action_label(action: Any) -> bool:
    text = str(action or "").strip().casefold()
    return bool(text and _p02_action_family(text) is not None)


def _p02_action_families(actions: Iterable[Any]) -> frozenset[str]:
    return frozenset(
        family
        for family in (_p02_action_family(value) for value in actions)
        if family
    )


def _p02_observable_instance_value(text: str) -> Optional[str]:
    """Extract one concrete URL/error token without treating topic text as ID."""

    values: List[Tuple[int, int, str, str]] = []
    for pattern, kind in ((_P01_ERROR_INSTANCE, "error"), (_P01_LINK_INSTANCE, "url")):
        for match in pattern.finditer(text):
            raw = match.group(0).rstrip("，,。；;！？!?)]}")
            if raw:
                values.append((match.start(), match.end(), kind, _normalized_text(raw)))
    # URL redaction tokens are observable instance evidence even when the
    # source has already removed the hostname/path.
    for match in re.finditer(r"(?i)\bURL_\d+(?::[^\]\s，,。；;！？!?]+)?", text):
        raw = match.group(0).rstrip("，,。；;！？!?)]}")
        if raw:
            values.append((match.start(), match.end(), "url", _normalized_text(raw)))
    if not values:
        return None
    # A single token is the safest observable key.  When a message names
    # multiple objects/tokens, retaining the earliest token still prevents a
    # cross-object merge through the object/family scope that is hashed below.
    _, _, kind, value = min(values, key=lambda item: (item[0], item[1], item[2]))
    return "%s:%s" % (kind, value)


def _p02_instance_key(
    message: MessageV2,
    text: str,
    *,
    entity_ids: Iterable[Any] = (),
    action_types: Iterable[Any] = (),
) -> Optional[str]:
    """Extract an explicit, source-derived event instance identity.

    A caller-supplied instance key is accepted as metadata and hashed before
    it enters a DTO.  Otherwise only a concrete error/status token or a
    resource URL (including a redacted URL token) can create an instance key.
    Generic topic/entity overlap is deliberately *not* an instance identity.
    """

    raw_key = str(message.explicit_instance_id or "").strip()
    if raw_key and _p02_is_instance_sentinel(raw_key):
        raw_key = ""
    if raw_key:
        value = "explicit:" + _normalized_text(raw_key)
        verified = True
    else:
        observable = _p02_observable_instance_value(text)
        if observable is None:
            return None
        value = "observable:" + observable
        verified = True

    known_entities = tuple(sorted({
        str(value) for value in entity_ids if _p02_is_known_entity_id(value)
    }))
    action_families = tuple(sorted(_p02_action_families(action_types)))
    # Scope and object/action context are intentionally part of the digest.
    # Thus the same URL/error code in two objects or chats is not one event.
    payload = json.dumps(
        {
            "account_id": str(message.account_id or "default"),
            "chat_id": str(message.chat_id or "unknown"),
            "entity_ids": known_entities,
            "action_families": action_families,
            "value": value,
            "verified": verified,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "instance:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _p02_action_matches(text: str) -> Tuple[Tuple[_P01ActionRule, int, int], ...]:
    """Return all distinct recognized action spans for aggregate blocking."""

    values = _p01_action_matches(text)
    selected: Dict[str, Tuple[_P01ActionRule, int, int]] = {}
    for item in values:
        action = item[0].action
        current = selected.get(action)
        if current is None or (item[2] - item[1], -item[1]) > (
            current[2] - current[1], -current[1]
        ):
            selected[action] = item
    return tuple(sorted(selected.values(), key=lambda item: (item[1], item[2], item[0].action)))


def _p02_claim_type(text: str) -> str:
    """Use the existing canonical type vocabulary on the full message span."""

    # Running the P0.1 precedence over the full message is intentional: it
    # preserves the established vocabulary while the span itself is repaired
    # at the message level.  A question mark anywhere in the exact span is
    # therefore retained as a question signal.
    return _p01_claim_type(text)


def _p02_trimmed_span(content: str) -> Tuple[int, int, str]:
    value = str(content or "")
    stripped = value.strip()
    if not stripped:
        return (0, 0, "")
    start = len(value) - len(value.lstrip())
    end = len(value.rstrip())
    return start, end, value[start:end]


def extract_mentions_and_claims_p02(
    messages: Sequence[MessageV2],
    analysis_run_id: str,
    created_at: str,
    *,
    segment_by_message: Optional[Mapping[str, str]] = None,
    message_roles: Optional[Mapping[str, str]] = None,
    eligible_message_ids: Optional[Iterable[str]] = None,
) -> Tuple[Tuple[MentionV2, ...], Tuple[ClaimV2, ...]]:
    """Extract one canonical claim per event-eligible message.

    Mentions remain broad and auditable, but the claim layer intentionally
    does not split conjunctions into separate rows.  This matches the
    development annotation contract and keeps exact evidence spans stable.
    """
    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    message_by_id = _p02_validate_messages(
        messages,
        segment_by_message=segment_by_message,
        owner="P0.2 extraction messages",
    )
    if message_roles is not None:
        if not isinstance(message_roles, Mapping):
            raise ValueError("P0.2 message_roles must be a mapping")
        external_roles = sorted(set(message_roles) - set(message_by_id))
        if external_roles:
            raise ValueError(
                "P0.2 message_roles has external input_id(s): %s"
                % ",".join(map(str, external_roles))
            )
    if eligible_message_ids is not None:
        if isinstance(eligible_message_ids, (str, bytes)):
            raise ValueError("P0.2 eligible_message_ids must be an ID sequence")
        eligible_values = tuple(eligible_message_ids)
        if any(not isinstance(value, str) or not value.strip() for value in eligible_values):
            raise ValueError("P0.2 eligible_message_ids contains an empty/non-string ID")
        external_eligible = sorted(set(eligible_values) - set(message_by_id))
        if external_eligible:
            raise ValueError(
                "P0.2 eligible_message_ids has external input_id(s): %s"
                % ",".join(external_eligible)
            )
    prepared = [_p02_prepare_message(item) for item in message_by_id.values()]
    ordered = sorted(
        prepared,
        key=lambda item: (
            str(segment_by_message.get(item.message_id, "") if segment_by_message else "")
            or str(item.dialogue_segment_id or ""),
            item.chat_id,
            _parsed_timestamp(item.timestamp) or datetime.max.replace(tzinfo=timezone.utc),
            item.message_id,
        ),
    )
    roles = dict(message_roles or {})
    eligible = (
        set(eligible_values)
        if eligible_message_ids is not None
        else {
            item.message_id
            for item in ordered
            if roles.get(item.message_id, ROLE_SUBSTANTIVE) == ROLE_SUBSTANTIVE
        }
    )
    all_mentions: List[MentionV2] = []
    claims: List[ClaimV2] = []
    previous_message_by_scope: Dict[Tuple[str, str, str], MessageV2] = {}
    for message in ordered:
        segment_id = (
            (segment_by_message.get(message.message_id, "") if segment_by_message else "")
            or message.dialogue_segment_id
            or ""
        )
        block_id = message.block_id
        # _p02_validate_messages has already compared the optional segment
        # mapping and rejected unscoped labels; do not normalize/repair here.
        if segment_id != message.dialogue_segment_id:
            raise ValueError("P0.2 extraction segment boundary changed after validation")
        entities = _p01_non_overlapping_mentions(
            message, _P01_ENTITY_RULES, analysis_run_id, created_at
        )
        entities.extend(
            _p01_entity_fallback(
                message,
                _p01_clauses(message.content),
                entities,
                analysis_run_id,
                created_at,
            )
        )
        entities = sorted(
            {item.mention_id: item for item in entities}.values(),
            key=lambda item: (item.span_start, item.span_end, item.normalized_id),
        )
        auxiliary = _p01_auxiliary_mentions(message, analysis_run_id, created_at)
        all_mentions.extend(_p02_version_mention(item) for item in entities)
        all_mentions.extend(_p02_version_mention(item) for item in auxiliary)
        if message.message_id not in eligible:
            continue
        if roles and roles.get(message.message_id) in {
            ROLE_CONTEXT_ONLY,
            ROLE_CONVERSATION_OPENER,
        }:
            continue
        start, end, claim_text = _p02_trimmed_span(message.content)
        if not claim_text:
            continue
        allowed_entities = tuple(
            item
            for item in entities
            if _p01_claim_target_allowed(item)
        )
        target_ids = tuple(sorted({item.normalized_id for item in allowed_entities}))
        entity_ids = tuple(sorted({item.normalized_id for item in entities}))
        action_matches = _p02_action_matches(claim_text)
        action_types = tuple(sorted({item[0].action for item in action_matches}))
        if action_matches:
            primary_rule, primary_start, primary_end = max(
                action_matches,
                key=lambda item: (
                    _P01_ACTION_PRIORITY.get(item[0].action, 0),
                    item[2] - item[1],
                    item[0].action,
                ),
            )
            action = primary_rule.action
        elif _P01_QUESTION_CUE.search(claim_text) and target_ids:
            action = "ask"
            action_types = ("ask",)
            primary_start, primary_end = max(0, len(claim_text) - 1), len(claim_text)
        else:
            action = "unknown"
            action_types = ()
            primary_start, primary_end = max(0, len(claim_text) - 1), len(claim_text)
        claim_type = _p02_claim_type(claim_text)
        status = _p01_status(claim_text, claim_type)
        request = _p01_request(action, claim_type, claim_text)
        trigger_mentions: List[MentionV2] = []
        for rule, local_start, local_end in action_matches:
            trigger_mentions.append(
                _mention(
                    message,
                    start + local_start,
                    start + local_end,
                    "event_trigger",
                    "action:" + rule.action,
                    rule.action,
                    None,
                    status,
                    request,
                    analysis_run_id,
                    created_at,
                    0.9,
                    pipeline_version=P02_PIPELINE_VERSION,
                    ruleset_version=P02_RULESET_VERSION,
                    provenance_stage="event_trigger_extraction_p02",
                )
            )
        if not trigger_mentions:
            trigger_mentions.append(
                _mention(
                    message,
                    start + primary_start,
                    start + primary_end,
                    "event_trigger",
                    "action:" + action,
                    action,
                    None,
                    status,
                    request,
                    analysis_run_id,
                    created_at,
                    0.75,
                    pipeline_version=P02_PIPELINE_VERSION,
                    ruleset_version=P02_RULESET_VERSION,
                    provenance_stage="event_trigger_extraction_p02",
                )
            )
        all_mentions.extend(trigger_mentions)
        known_entity_ids = tuple(
            sorted({item for item in entity_ids if _p02_is_known_entity_id(item)})
        )
        instance_key = _p02_instance_key(
            message,
            claim_text,
            entity_ids=known_entity_ids,
            action_types=action_types,
        )
        # Continuation is intentionally narrower than dialogue membership: it
        # requires an explicit block, a nearby preceding turn, and a local
        # timestamp.  A segment alone is never authority to merge events.
        previous_scope_key = (
            message.account_id,
            message.chat_id,
            str(message.block_id or ""),
        )
        previous_message = (
            previous_message_by_scope.get(previous_scope_key)
            if message.block_id
            else None
        )
        continuation = bool(
            previous_message
            and _p01_is_reference_like(claim_text)
            and _p02_message_is_local_continuation(message, previous_message)
        )
        uncertainties: List[str] = []
        if not target_ids:
            uncertainties.append("core_entity_unknown")
        if not action_types:
            uncertainties.append("action_unknown")
        if instance_key:
            uncertainties.append("explicit_instance_evidence")
        if continuation:
            uncertainties.append("explicit_continuation_cue")
        evidence = EvidenceRefV2(message.message_id, start, end, claim_text)
        mention_ids = tuple(
            sorted(
                {
                    *(item.mention_id for item in entities),
                    *(item.mention_id for item in trigger_mentions),
                }
            )
        )
        claim_id = stable_id(
            "claim",
            {
                "message_id": message.message_id,
                "span": [start, end],
                "claim_type": claim_type,
                "targets": target_ids,
                "action_types": action_types,
                "instance_key": instance_key,
            },
            pipeline_version=P02_PIPELINE_VERSION,
            ruleset_version=P02_RULESET_VERSION,
        )
        boundary_ids = _p01_message_boundary_ids(message)
        claims.append(
            ClaimV2(
                claim_id=claim_id,
                speaker_id=message.speaker_id,
                speaker_name=message.speaker_name,
                claim_text=claim_text,
                claim_type=claim_type,
                target_entity_ids=target_ids,
                event_mention_ids=mention_ids,
                action=action,
                request=request,
                stance_or_polarity=(
                    "negative" if _P01_NEGATIVE_CUE.search(claim_text) else "neutral_or_positive"
                ),
                status_or_modality=status,
                timestamp=message.timestamp,
                message_id=message.message_id,
                reply_to_message_id=message.reply_to_message_id,
                evidence_span=evidence,
                confidence=0.88 if target_ids else 0.62,
                source_message_ids=(message.message_id,),
                evidence_refs=(evidence,),
                provenance=ProvenanceV2(
                    tuple(dict.fromkeys((message.message_id,) + mention_ids + boundary_ids)),
                    "claim_extraction_p02",
                    P02_RULESET_VERSION,
                ),
                analysis_run_id=analysis_run_id,
                created_at=created_at,
                pipeline_version=P02_PIPELINE_VERSION,
                ruleset_version=P02_RULESET_VERSION,
                uncertainties=tuple(sorted(set(uncertainties))),
                dialogue_segment_id=segment_id or None,
                attribution=str(message.attribution or "direct").strip() or "direct",
                context_message_ids=(),
                explicit_instance_id=instance_key,
                block_id=message.block_id,
                entity_ids=entity_ids,
                account_id=message.account_id,
                chat_id=message.chat_id,
                action_types=action_types,
                position_in_block=message.position_in_block,
            )
        )
        previous_message_by_scope[previous_scope_key] = message
    unique_mentions = {item.mention_id: item for item in all_mentions}
    unique_claims = {item.claim_id: item for item in claims}
    _p02_validate_mentions(
        tuple(unique_mentions.values()),
        run_id,
        messages=message_by_id,
        owner="P0.2 extracted mentions",
    )
    _p02_validate_claims(
        tuple(unique_claims.values()),
        run_id,
        messages=message_by_id,
        mentions={item.mention_id: item for item in unique_mentions.values()},
        owner="P0.2 extracted claims",
    )
    return (
        tuple(sorted(unique_mentions.values(), key=lambda item: item.mention_id)),
        tuple(sorted(unique_claims.values(), key=lambda item: item.claim_id)),
    )


def _p02_known_entity_ids(claim: ClaimV2) -> frozenset[str]:
    values = claim.entity_ids or claim.target_entity_ids
    return frozenset(
        str(value)
        for value in values
        if _p02_is_known_entity_id(value)
    )


def _p02_known_action_ids(claim: ClaimV2) -> frozenset[str]:
    values = claim.action_types or ((claim.action,) if claim.action else ())
    return frozenset(
        str(value)
        for value in values
        if _p02_known_action_label(value)
    )


def _p02_known_instance(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and not _p02_is_instance_sentinel(text)


def _p02_shared_instance(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(
        _p02_known_instance(left.explicit_instance_id)
        and _p02_known_instance(right.explicit_instance_id)
        and str(left.explicit_instance_id).casefold()
        == str(right.explicit_instance_id).casefold()
    )


def _p02_same_segment(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(
        left.dialogue_segment_id
        and right.dialogue_segment_id
        and left.dialogue_segment_id == right.dialogue_segment_id
    )


def _p02_same_block(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(left.block_id and right.block_id and left.block_id == right.block_id)


def _p02_same_boundary(left: ClaimV2, right: ClaimV2) -> bool:
    """Return whether a structural/semantic index may compare two claims.

    Blocks are the strongest supplied boundary and intentionally take
    precedence over segments.  For rows without blocks, a shared scoped
    segment is the fallback.  Missing boundaries do not create a broad index;
    only claims from the same message can still be compared.
    """

    if _p02_same_block(left, right):
        return True
    if left.block_id or right.block_id:
        return False
    return bool(
        left.dialogue_segment_id
        and right.dialogue_segment_id
        and left.dialogue_segment_id == right.dialogue_segment_id
    ) or left.message_id == right.message_id


def _p02_boundary_key(claim: ClaimV2) -> Optional[Tuple[str, str]]:
    """Return the strongest available scoped blocking boundary."""

    block = str(claim.block_id or "").strip()
    if block:
        return ("block", block)
    segment = str(claim.dialogue_segment_id or "").strip()
    if segment:
        return ("segment", segment)
    return None


def _p02_claim_sort_key(claim: ClaimV2) -> Tuple[Any, ...]:
    """Sort claims deterministically for all bounded index neighborhoods."""

    return (
        _parsed_timestamp(claim.timestamp) or datetime.max.replace(tzinfo=timezone.utc),
        getattr(claim, "position_in_block", None)
        if getattr(claim, "position_in_block", None) is not None
        else 10**9,
        claim.claim_id,
    )


def _p02_same_scope(left: ClaimV2, right: ClaimV2) -> bool:
    return (
        str(left.account_id or "default") == str(right.account_id or "default")
        and str(left.chat_id or "unknown") == str(right.chat_id or "unknown")
    )


def _p02_explicit_reply(left: ClaimV2, right: ClaimV2) -> bool:
    return bool(
        left.reply_to_message_id == right.message_id
        or right.reply_to_message_id == left.message_id
    )


def _p02_continuation(left: ClaimV2, right: ClaimV2) -> bool:
    # Continuation is supporting evidence only.  It never acts as the strong
    # link that permits same_event on its own.
    if not _p02_same_block(left, right):
        return False
    if not (
        "explicit_continuation_cue" in left.uncertainties
        or "explicit_continuation_cue" in right.uncertainties
    ):
        return False
    return _p02_claims_are_local(left, right)


def _p02_time_gap(left: ClaimV2, right: ClaimV2) -> Optional[float]:
    left_time = _parsed_timestamp(left.timestamp)
    right_time = _parsed_timestamp(right.timestamp)
    if left_time is None or right_time is None:
        return None
    return abs((left_time - right_time).total_seconds())


def _p02_claims_are_local(left: ClaimV2, right: ClaimV2) -> bool:
    """Check the local turn/time window required by continuation evidence."""

    gap = _p02_time_gap(left, right)
    if gap is None or gap > _P02_CONTINUATION_MAX_SECONDS:
        return False
    left_position = getattr(left, "position_in_block", None)
    right_position = getattr(right, "position_in_block", None)
    if left_position is None or right_position is None:
        return True
    try:
        return abs(int(left_position) - int(right_position)) <= _P02_CONTINUATION_MAX_TURNS
    except (TypeError, ValueError):
        return False


def _p02_message_is_local_continuation(current: MessageV2, previous: MessageV2) -> bool:
    """Message-level counterpart used while extracting continuation cues."""

    if not current.block_id or current.block_id != previous.block_id:
        return False
    left_time = _parsed_timestamp(current.timestamp)
    right_time = _parsed_timestamp(previous.timestamp)
    if left_time is None or right_time is None:
        return False
    if abs((left_time - right_time).total_seconds()) > _P02_CONTINUATION_MAX_SECONDS:
        return False
    left_position = current.position_in_block
    right_position = previous.position_in_block
    if left_position is None or right_position is None:
        return True
    try:
        return abs(int(left_position) - int(right_position)) <= _P02_CONTINUATION_MAX_TURNS
    except (TypeError, ValueError):
        return False


def _p02_candidate_stats(
    claims: Sequence[ClaimV2],
    candidates: Sequence[CandidatePairV2],
    *,
    neighbor_window: int,
) -> Dict[str, Any]:
    from collections import Counter, defaultdict

    reason_counts = Counter(
        reason
        for candidate in candidates
        for reason in candidate.blocking_reasons
    )
    group_counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
    for claim in claims:
        group_counts[
            (
                str(claim.account_id or "default"),
                str(claim.chat_id or "unknown"),
                str(claim.block_id or claim.dialogue_segment_id or ""),
            )
        ] += 1
    upper_bound = sum(
        count * (count - 1) // 2
        if count <= neighbor_window
        else neighbor_window * count - neighbor_window * (neighbor_window + 1) // 2
        for count in group_counts.values()
    )
    return {
        "claim_count": len(claims),
        "candidate_count": len(candidates),
        "candidate_upper_bound_same_boundary": int(upper_bound),
        "neighbor_window": int(neighbor_window),
        "blocking_reason_counts": dict(sorted(reason_counts.items())),
        "candidate_scale_ratio": round(len(candidates) / len(claims), 6) if claims else 0.0,
    }


def generate_candidate_pairs_p02(
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
    *,
    neighbor_window: int = 8,
) -> Tuple[CandidatePairV2, ...]:
    """Generate a bounded union of structural and semantic blocking signals.

    Same-block candidates are rank-near rather than a full Cartesian product.
    Shared known entity/action/request/instance/continuation signals are
    indexed independently and unioned into the candidate set.  Scope is a
    hard boundary; a relation classifier still decides whether an edge is an
    event merge.
    """

    from collections import defaultdict

    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    if neighbor_window < 1:
        raise ValueError("neighbor_window must be positive")
    claim_by_id = _p02_validate_claims(
        claims,
        run_id,
        owner="P0.2 candidate input claims",
    )
    ordered = sorted(
        claim_by_id.values(),
        key=lambda item: (
            str(item.account_id or "default"),
            str(item.chat_id or "unknown"),
            _parsed_timestamp(item.timestamp) or datetime.max.replace(tzinfo=timezone.utc),
            item.claim_id,
        ),
    )
    by_id = {item.claim_id: item for item in ordered}
    candidate_reasons: Dict[Tuple[str, str], set[str]] = defaultdict(set)

    def add(left: ClaimV2, right: ClaimV2, *reasons: str) -> None:
        if left.claim_id == right.claim_id or not _p02_same_scope(left, right):
            return
        # Every ordinary blocking signal is scoped to one block (or, when a
        # block is unavailable, one dialogue segment).  Only an explicit
        # reply or a verified shared instance may cross that boundary, and
        # the account/chat scope check above still applies.
        cross_boundary = not _p02_same_boundary(left, right)
        exception_reasons = {"explicit_reply", "explicit_shared_instance"}
        if cross_boundary and not exception_reasons.intersection(reasons):
            return
        pair = tuple(sorted((left.claim_id, right.claim_id)))
        candidate_reasons[pair].update(str(value) for value in reasons if value)

    # Rank-near structural groups.  A supplied block takes precedence; a
    # segment is only used when no block label is available.
    groups: Dict[Tuple[str, str, str], List[ClaimV2]] = defaultdict(list)
    for claim in ordered:
        boundary = str(claim.block_id or "") or str(claim.dialogue_segment_id or "")
        if boundary:
            groups[(str(claim.account_id or "default"), str(claim.chat_id or "unknown"), boundary)].append(claim)
    for group in groups.values():
        values = sorted(
            group,
            key=lambda item: (
                _parsed_timestamp(item.timestamp) or datetime.max.replace(tzinfo=timezone.utc),
                item.claim_id,
            ),
        )
        for index, left in enumerate(values):
            for right in values[index + 1 : index + 1 + neighbor_window]:
                same_block = _p02_same_block(left, right)
                add(
                    left,
                    right,
                    "same_block_neighbor" if same_block else "same_dialogue_neighbor",
                    "same_block" if same_block else "same_dialogue_segment",
                )

    claims_by_entity: Dict[Tuple[Any, ...], List[ClaimV2]] = defaultdict(list)
    claims_by_action: Dict[Tuple[Any, ...], List[ClaimV2]] = defaultdict(list)
    claims_by_request: Dict[Tuple[Any, ...], List[ClaimV2]] = defaultdict(list)
    # The boundary-scoped index handles the common case.  The scope-only
    # index is used solely for the verified cross-block instance exception.
    claims_by_instance: Dict[Tuple[Any, ...], List[ClaimV2]] = defaultdict(list)
    claims_by_instance_cross_boundary: Dict[Tuple[Any, ...], List[ClaimV2]] = defaultdict(list)
    claims_by_message: Dict[Tuple[Any, ...], List[ClaimV2]] = defaultdict(list)
    for claim in ordered:
        scope = (str(claim.account_id or "default"), str(claim.chat_id or "unknown"))
        boundary = _p02_boundary_key(claim)
        if boundary is None:
            # Unbounded claims do not enter semantic indexes.  A same-message
            # pair remains available below because the message itself is the
            # scope in that case.
            if claim.message_id:
                claims_by_message[scope + ("message", str(claim.message_id))].append(claim)
            continue
        for value in _p02_known_entity_ids(claim):
            claims_by_entity[scope + boundary + (value,)].append(claim)
        for value in _p02_known_action_ids(claim):
            claims_by_action[scope + boundary + (value,)].append(claim)
        request = str(claim.request or "").strip()
        if request and request.casefold() not in {"unknown", "report_state"}:
            claims_by_request[scope + boundary + (request,)].append(claim)
        if _p02_known_instance(claim.explicit_instance_id):
            instance = str(claim.explicit_instance_id).casefold()
            claims_by_instance[scope + boundary + (instance,)].append(claim)
            claims_by_instance_cross_boundary[scope + (instance,)].append(claim)
        claims_by_message[scope + boundary + (str(claim.message_id),)].append(claim)

    def add_groups(index: Mapping[Any, Sequence[ClaimV2]], reason: str) -> None:
        for values in index.values():
            ordered_values = sorted(values, key=_p02_claim_sort_key)
            for index, left in enumerate(ordered_values):
                # Semantic indexes are bounded by the same local-neighbor
                # budget as structural blocking.  On the development sample
                # each block has at most nine claims, so the default window
                # still covers the complete 704-pair boundary upper bound.
                for right in ordered_values[index + 1 : index + 1 + neighbor_window]:
                    add(left, right, reason)

    add_groups(claims_by_entity, "shared_known_entity")
    add_groups(claims_by_action, "shared_known_action")
    add_groups(claims_by_request, "shared_request")
    add_groups(claims_by_instance, "explicit_shared_instance")
    # Verified instances are the only semantic signal permitted to cross a
    # block.  The scope-only index is never used for ordinary entity/action/
    # request blocking.
    for values in claims_by_instance_cross_boundary.values():
        ordered_values = sorted(values, key=_p02_claim_sort_key)
        for index, left in enumerate(ordered_values):
            for right in ordered_values[index + 1 :]:
                if not _p02_same_boundary(left, right):
                    add(left, right, "explicit_shared_instance")
    add_groups(claims_by_message, "same_message")
    # Replies are indexed by message ID and are allowed to cross a supplied
    # block, provided account/chat scope is unchanged.
    claims_by_message_id: Dict[str, List[ClaimV2]] = defaultdict(list)
    for claim in ordered:
        claims_by_message_id[str(claim.message_id)].append(claim)
    for claim in ordered:
        target = str(claim.reply_to_message_id or "").strip()
        if not target:
            continue
        for other in claims_by_message_id.get(target, ()):
            add(claim, other, "explicit_reply")
    # A continuation cue links only to a local neighborhood in the same
    # block.  It is a recall/support signal; classification never treats it
    # as a standalone same-event authority.
    claims_by_block: Dict[str, List[ClaimV2]] = defaultdict(list)
    for claim in ordered:
        if claim.block_id:
            claims_by_block[str(claim.block_id)].append(claim)
    for values in claims_by_block.values():
        values.sort(key=_p02_claim_sort_key)
        for index, claim in enumerate(values):
            if "explicit_continuation_cue" not in claim.uncertainties:
                continue
            for other in values[max(0, index - neighbor_window) : index + neighbor_window + 1]:
                if claim.claim_id != other.claim_id and _p02_continuation(claim, other):
                    add(claim, other, "explicit_continuation")

    values: List[CandidatePairV2] = []
    for (left_id, right_id), reasons in sorted(candidate_reasons.items()):
        left, right = by_id[left_id], by_id[right_id]
        if not _p02_same_scope(left, right):
            continue
        # A pair that only has a shared boundary signal is already bounded by
        # the neighbor loop.  No broad same-segment Cartesian expansion is
        # permitted here.
        if not reasons:
            continue
        shared_entities = bool(_p02_known_entity_ids(left) & _p02_known_entity_ids(right))
        shared_actions = bool(_p02_known_action_ids(left) & _p02_known_action_ids(right))
        shared_instance = _p02_shared_instance(left, right)
        explicit_reply = _p02_explicit_reply(left, right)
        continuation = _p02_continuation(left, right)
        score = 0.3
        score += 0.16 if "same_block_neighbor" in reasons else 0.0
        score += 0.12 if "same_dialogue_neighbor" in reasons else 0.0
        score += 0.18 if shared_entities else 0.0
        score += 0.12 if shared_actions else 0.0
        score += 0.10 if "shared_request" in reasons else 0.0
        score += 0.24 if shared_instance else 0.0
        score += 0.2 if explicit_reply else 0.0
        score += 0.16 if continuation else 0.0
        evidence = tuple(
            sorted(
                set(left.evidence_refs + right.evidence_refs),
                key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
            )
        )
        candidate_id = stable_id(
            "candidate",
            {
                "left": left_id,
                "right": right_id,
                "blocking_reasons": sorted(reasons),
            },
            pipeline_version=P02_PIPELINE_VERSION,
            ruleset_version=P02_RULESET_VERSION,
        )
        provenance_ids = tuple(
            dict.fromkeys(
                (left_id, right_id)
                + _p01_claim_boundary_ids(left)
                + _p01_claim_boundary_ids(right)
            )
        )
        values.append(
            CandidatePairV2(
                candidate_id=candidate_id,
                left_claim_id=left_id,
                right_claim_id=right_id,
                blocking_reasons=tuple(sorted(reasons)),
                source_message_ids=tuple(sorted({left.message_id, right.message_id})),
                evidence_refs=evidence,
                score=round(min(1.0, score), 6),
                provenance=ProvenanceV2(
                    provenance_ids,
                    "candidate_generation_p02",
                    P02_RULESET_VERSION,
                ),
                analysis_run_id=run_id,
                created_at=created_at,
                pipeline_version=P02_PIPELINE_VERSION,
                ruleset_version=P02_RULESET_VERSION,
            )
        )
    output = tuple(sorted(values, key=lambda item: item.candidate_id))
    _p02_validate_candidates(output, run_id, claim_by_id, owner="P0.2 generated candidates")
    return output


def _p02_global_conflict_reasons(left: ClaimV2, right: ClaimV2) -> Tuple[str, ...]:
    """Return same-event vetoes that are independent of linkage strength."""

    reasons: List[str] = []
    left_entities = _p02_known_entity_ids(left)
    right_entities = _p02_known_entity_ids(right)
    if left_entities and right_entities and not left_entities.intersection(right_entities):
        reasons.append("CORE_OBJECT_CONFLICT")

    left_actions = _p02_known_action_ids(left)
    right_actions = _p02_known_action_ids(right)
    left_families = _p02_action_families(left_actions)
    right_families = _p02_action_families(right_actions)
    if left_families and right_families and not left_families.intersection(right_families):
        reasons.append("ACTION_TYPE_CONFLICT")

    request_unknown = {"", "unknown", "none", "null", "n/a", "na", "report_state"}
    left_request = str(left.request or "").strip().casefold()
    right_request = str(right.request or "").strip().casefold()
    if (
        left_request not in request_unknown
        and right_request not in request_unknown
        and left_request != right_request
    ):
        reasons.append("INTENT_CONFLICT")

    attribution_unknown = {"", "unknown", "none", "null", "n/a", "na"}
    left_attribution = str(left.attribution or "").strip().casefold()
    right_attribution = str(right.attribution or "").strip().casefold()
    if (
        left_attribution not in attribution_unknown
        and right_attribution not in attribution_unknown
        and left_attribution != right_attribution
    ):
        reasons.append("ATTRIBUTION_CONFLICT")
    return tuple(sorted(set(reasons)))


def classify_claim_pair_p02(
    left: ClaimV2,
    right: ClaimV2,
    analysis_run_id: str,
    created_at: str,
) -> PairDecisionV2:
    """Classify a bounded candidate with a strict same-event gate."""

    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    claim_map = _p02_validate_claims(
        (left, right),
        run_id,
        owner="P0.2 pair input claims",
    )
    if len(claim_map) != 2:
        raise ValueError("P0.2 candidate pair requires two distinct claims")
    if left.claim_id == right.claim_id:
        raise ValueError("candidate pair requires two distinct claims")
    left_entities = _p02_known_entity_ids(left)
    right_entities = _p02_known_entity_ids(right)
    left_actions = _p02_known_action_ids(left)
    right_actions = _p02_known_action_ids(right)
    shared_entities = left_entities & right_entities
    shared_actions = left_actions & right_actions
    left_families = set(_p01_families_for_entities(tuple(left_entities)))
    right_families = set(_p01_families_for_entities(tuple(right_entities)))
    shared_families = left_families & right_families
    left_action_families = _p02_action_families(left_actions)
    right_action_families = _p02_action_families(right_actions)
    shared_action_families = left_action_families & right_action_families
    same_scope = _p02_same_scope(left, right)
    same_message = left.message_id == right.message_id
    explicit_reply = _p02_explicit_reply(left, right)
    shared_instance = _p02_shared_instance(left, right)
    continuation = _p02_continuation(left, right)
    # Continuation is supporting evidence only.  It never acts as the strong
    # link that permits same_event on its own.
    strong_link = same_message or explicit_reply or shared_instance
    core_known = bool(left_entities) and bool(right_entities)
    action_known = bool(left_actions) and bool(right_actions)
    hard: List[str] = []
    support: List[str] = []
    conflicts: List[str] = []
    uncertainties: List[str] = []

    # Global contradiction vetoes are collected before the positive linkage
    # branch below.  A reply or shared instance can provide context, but it
    # cannot erase an evidenced object/action/intent/attribution conflict.
    if shared_entities:
        support.append("known_entity")
    elif left_entities and right_entities:
        conflicts.append("core_entity")
        hard.append("CORE_OBJECT_CONFLICT")
    else:
        uncertainties.append("core_entity_unknown")
    if shared_action_families:
        support.append("action_family")
        if shared_actions:
            support.append("known_action")
    elif left_action_families and right_action_families:
        conflicts.append("action_family")
        hard.append("ACTION_TYPE_CONFLICT")
    elif shared_actions:
        # Defensive fallback for a custom action vocabulary that happens to
        # share a literal action but has no configured family alias.
        support.append("known_action")
    elif left_actions and right_actions:
        conflicts.append("action")
        hard.append("ACTION_TYPE_CONFLICT")
    else:
        uncertainties.append("action_unknown")
    left_request = str(left.request or "").strip()
    right_request = str(right.request or "").strip()
    request_unknown = {"", "unknown", "none", "null", "n/a", "na", "report_state"}
    left_request_known = left_request.casefold() not in request_unknown
    right_request_known = right_request.casefold() not in request_unknown
    if left_request_known and right_request_known and left_request.casefold() == right_request.casefold():
        support.append("request")
    elif left_request_known and right_request_known:
        conflicts.append("request")
        hard.append("INTENT_CONFLICT")
    else:
        uncertainties.append("request_missing")
    attribution_unknown = {"", "unknown", "none", "null", "n/a", "na"}
    left_attribution = str(left.attribution or "").strip().casefold()
    right_attribution = str(right.attribution or "").strip().casefold()
    if left_attribution not in attribution_unknown and right_attribution not in attribution_unknown:
        if left_attribution == right_attribution:
            support.append("attribution")
        else:
            conflicts.append("attribution")
            hard.append("ATTRIBUTION_CONFLICT")
    else:
        uncertainties.append("attribution_missing")
    if same_message:
        support.append("same_message")
    elif explicit_reply:
        support.append("explicit_reply")
    elif shared_instance:
        support.append("shared_instance")
    elif continuation:
        support.append("explicit_continuation")
    else:
        uncertainties.append("missing_strong_event_linkage")
    status_pair = {str(left.status_or_modality), str(right.status_or_modality)}
    status_conflict = (
        len(status_pair) > 1
        and bool(status_pair & {"failed", "recovered", "recurring", "failed_or_negative"})
        and bool(status_pair & {"reported", "failed", "recovered", "recurring", "failed_or_negative"})
    )
    if status_conflict:
        conflicts.append("status")
        if not strong_link:
            hard.append("STATUS_CONTRADICTION_WITHOUT_SHARED_INSTANCE")
    elif left.status_or_modality == right.status_or_modality:
        support.append("status")
    time_gap = _p02_time_gap(left, right)
    if time_gap is None:
        uncertainties.append("timestamp_missing_or_unparseable")
    elif time_gap <= 24 * 60 * 60:
        support.append("time_window")
    elif not explicit_reply:
        hard.append("TEMPORAL_INSTANCE_CONFLICT")
        conflicts.append("time")

    # Keep the final hard-veto set authoritative even if a custom action or
    # claim DTO took an unusual path through the slot bookkeeping above.
    for reason in _p02_global_conflict_reasons(left, right):
        if reason not in hard:
            hard.append(reason)

    if not same_scope:
        relation = RELATION_UNRELATED
        confidence = 0.99
        hard.append("CROSS_ACCOUNT_OR_CHAT_SCOPE")
    elif not (core_known and action_known) and not shared_instance:
        # Unknown identity/action is never enough for a merge, even on a
        # reply.  A verified observable/explicit instance is the narrow
        # exception, provided no global contradiction veto fired.
        relation = RELATION_INSUFFICIENT_CONTEXT
        confidence = 0.65
    elif strong_link and not hard:
        relation = RELATION_SAME_EVENT
        confidence = 0.97 if shared_instance or explicit_reply else 0.93
    elif shared_entities:
        relation = RELATION_RELATED_EVENT
        confidence = 0.87
    elif shared_families:
        relation = RELATION_SAME_TOPIC_ONLY
        confidence = 0.88
        hard.append("TOPIC_ONLY_EVIDENCE")
    else:
        relation = RELATION_UNRELATED
        confidence = 0.9
    mnl_reasons = tuple(sorted(set(hard)))
    must_not_link = bool(mnl_reasons) and relation != RELATION_SAME_EVENT
    if relation == RELATION_SAME_TOPIC_ONLY and "TOPIC_ONLY_EVIDENCE" not in mnl_reasons:
        mnl_reasons = tuple(sorted(set(mnl_reasons + ("TOPIC_ONLY_EVIDENCE",))))
        must_not_link = True
    left_id, right_id = sorted((left.claim_id, right.claim_id))
    evidence = tuple(
        sorted(
            set(left.evidence_refs + right.evidence_refs),
            key=lambda item: (item.message_id, item.span_start, item.span_end, item.evidence_text),
        )
    )
    decision_id = stable_id(
        "pair",
        {"left": left_id, "right": right_id, "relation": relation, "hard": mnl_reasons},
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
    )
    provenance_ids = tuple(
        dict.fromkeys(
            (left_id, right_id)
            + _p01_claim_boundary_ids(left)
            + _p01_claim_boundary_ids(right)
        )
    )
    return PairDecisionV2(
        decision_id=decision_id,
        left_claim_id=left_id,
        right_claim_id=right_id,
        relation=relation,
        supporting_slots=tuple(sorted(set(support))),
        conflicting_slots=tuple(sorted(set(conflicts))),
        hard_conflict_reasons=mnl_reasons,
        source_message_ids=tuple(sorted({left.message_id, right.message_id})),
        evidence_refs=evidence,
        confidence=confidence,
        provenance=ProvenanceV2(provenance_ids, "pair_classification_p02", P02_RULESET_VERSION),
        analysis_run_id=run_id,
        created_at=created_at,
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
        uncertainties=tuple(sorted(set(uncertainties))),
        must_not_link=must_not_link,
        must_not_link_reason_codes=mnl_reasons if must_not_link else (),
    )


def classify_candidate_pairs_p02(
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
    *,
    neighbor_window: int = 8,
) -> Tuple[PairDecisionV2, ...]:
    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    candidates = generate_candidate_pairs_p02(
        claims, run_id, created_at, neighbor_window=neighbor_window
    )
    claim_map = _p02_validate_claims(claims, run_id, owner="P0.2 classification input claims")
    by_id = dict(claim_map)
    output = tuple(
        sorted(
            (
                classify_claim_pair_p02(
                    by_id[item.left_claim_id],
                    by_id[item.right_claim_id],
                    run_id,
                    created_at,
                )
                for item in candidates
            ),
            key=lambda item: item.decision_id,
        )
    )
    _p02_validate_decisions(output, run_id, claim_map, owner="P0.2 classified decisions")
    return output


def _p02_version_event(event: EventV2) -> EventV2:
    return replace(
        event,
        provenance=_p02_version_provenance(event.provenance),
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
    )


def _p02_version_topic(family: TopicFamilyV2) -> TopicFamilyV2:
    return replace(
        family,
        provenance=_p02_version_provenance(family.provenance),
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
    )


def _p02_version_trend(trend: TrendV2) -> TrendV2:
    return replace(
        trend,
        provenance=_p02_version_provenance(trend.provenance),
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
    )


def _p02_version_presentation(card: PresentationV2) -> PresentationV2:
    return replace(
        card,
        provenance=_p02_version_provenance(card.provenance),
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
    )


def build_events_p02(
    claims: Sequence[ClaimV2],
    pair_decisions: Sequence[PairDecisionV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[EventV2, ...]:
    """Construct P0.2 events without routing P0.2 objects through P0.1 gates."""

    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    claim_map = _p02_validate_claims(claims, run_id, owner="P0.2 event input claims")
    decision_map = _p02_validate_decisions(
        pair_decisions,
        run_id,
        claim_map,
        owner="P0.2 event input decisions",
    )

    decisions = {
        frozenset((item.left_claim_id, item.right_claim_id)): item
        for item in decision_map.values()
    }
    groups: List[List[ClaimV2]] = []
    for claim in sorted(claim_map.values(), key=lambda item: item.claim_id):
        placed = False
        for group in groups:
            relations = [
                decisions.get(frozenset((claim.claim_id, member.claim_id)))
                for member in group
            ]
            if relations and all(
                item is not None
                and item.relation == RELATION_SAME_EVENT
                and not item.must_not_link
                for item in relations
            ):
                group.append(claim)
                placed = True
                break
        if not placed:
            groups.append([claim])

    events: List[EventV2] = []
    for raw_group in groups:
        group = sorted(raw_group, key=lambda item: item.claim_id)
        claim_ids = tuple(item.claim_id for item in group)
        entity_ids = tuple(sorted({value for item in group for value in item.target_entity_ids}))
        actions = tuple(sorted({item.action for item in group if item.action}))
        evidence = _p01_evidence_union(group)
        source_message_ids = tuple(sorted({value for item in group for value in item.source_message_ids}))
        mention_ids = tuple(sorted({value for item in group for value in item.event_mention_ids}))
        timestamps = sorted(item.timestamp for item in group if item.timestamp != "unknown")
        related_decisions = tuple(
            sorted(
                item.decision_id
                for item in decision_map.values()
                if item.left_claim_id in claim_ids
                and item.right_claim_id in claim_ids
                and item.relation == RELATION_SAME_EVENT
            )
        )
        event_id = stable_id(
            "event",
            {"claim_ids": sorted(claim_ids), "core_entities": sorted(entity_ids), "actions": sorted(actions)},
            pipeline_version=P02_PIPELINE_VERSION,
            ruleset_version=P02_RULESET_VERSION,
        )
        provenance_ids = tuple(
            dict.fromkeys(
                claim_ids
                + related_decisions
                + tuple(
                    boundary_id
                    for item in group
                    for boundary_id in _p01_claim_boundary_ids(item)
                )
            )
        )
        events.append(
            EventV2(
                event_id=event_id,
                event_type=actions[0] if len(actions) == 1 else "compound_unknown",
                core_entity_ids=entity_ids,
                actions=actions,
                start_at=timestamps[0] if timestamps else "unknown",
                end_at=timestamps[-1] if timestamps else "unknown",
                statuses=tuple(sorted({item.status_or_modality for item in group})),
                requests=tuple(sorted({item.request for item in group})),
                participant_ids=tuple(sorted({item.speaker_id for item in group})),
                claim_ids=claim_ids,
                mention_ids=mention_ids,
                supporting_evidence_refs=evidence,
                conflicting_evidence_refs=(),
                relation_decision_ids=related_decisions,
                confidence=min(item.confidence for item in group),
                source_message_ids=source_message_ids,
                evidence_refs=evidence,
                provenance=ProvenanceV2(
                    provenance_ids, "event_construction_p02", P02_RULESET_VERSION
                ),
                analysis_run_id=run_id,
                created_at=created_at,
                pipeline_version=P02_PIPELINE_VERSION,
                ruleset_version=P02_RULESET_VERSION,
                uncertainties=tuple(sorted({value for item in group for value in item.uncertainties})),
            )
        )
    output = tuple(sorted(events, key=lambda item: item.event_id))
    _p02_validate_events(output, run_id, claim_map, decision_map, owner="P0.2 built events")
    return output


def derive_topic_families_p02(
    events: Sequence[EventV2],
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[TopicFamilyV2, ...]:
    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    claim_by_id = _p02_validate_claims(claims, run_id, owner="P0.2 family input claims")
    event_by_id = _p02_validate_events(events, run_id, claim_by_id, owner="P0.2 family input events")
    buckets: Dict[str, List[EventV2]] = {}
    for event in event_by_id.values():
        families = _p01_families_for_entities(event.core_entity_ids) or ("unknown",)
        for family in families:
            buckets.setdefault(family, []).append(event)
    output: List[TopicFamilyV2] = []
    for family_key, family_events in sorted(buckets.items()):
        event_ids = tuple(sorted(item.event_id for item in family_events))
        family_claims = [
            claim_by_id[claim_id]
            for event in family_events
            for claim_id in event.claim_ids
            if claim_id in claim_by_id
        ]
        evidence = _p01_evidence_union(family_claims)
        output.append(
            TopicFamilyV2(
                topic_family_id=stable_id(
                    "topic_family", {"family_key": family_key},
                    pipeline_version=P02_PIPELINE_VERSION,
                    ruleset_version=P02_RULESET_VERSION,
                ),
                family_key=family_key,
                label=_P01_FAMILY_LABELS.get(family_key, family_key),
                event_ids=event_ids,
                confidence=0.94 if family_key != "unknown" else 0.4,
                source_message_ids=tuple(sorted({item.message_id for item in family_claims})),
                evidence_refs=evidence,
                provenance=ProvenanceV2(
                    tuple(
                        dict.fromkeys(
                            event_ids
                            + tuple(
                                boundary_id
                                for item in family_claims
                                for boundary_id in _p01_claim_boundary_ids(item)
                            )
                        )
                    ),
                    "topic_family_derivation_p02",
                    P02_RULESET_VERSION,
                ),
                analysis_run_id=run_id,
                created_at=created_at,
                pipeline_version=P02_PIPELINE_VERSION,
                ruleset_version=P02_RULESET_VERSION,
                uncertainties=("family_unknown",) if family_key == "unknown" else (),
            )
        )
    result = tuple(output)
    _p02_validate_families(result, run_id, event_by_id, claim_by_id, owner="P0.2 derived families")
    return result


def derive_trends_p02(
    events: Sequence[EventV2],
    families: Sequence[TopicFamilyV2],
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[TrendV2, ...]:
    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    claim_by_id = _p02_validate_claims(claims, run_id, owner="P0.2 trend input claims")
    event_by_id = _p02_validate_events(events, run_id, claim_by_id, owner="P0.2 trend input events")
    family_by_id = _p02_validate_families(families, run_id, event_by_id, claim_by_id, owner="P0.2 trend input families")
    output: List[TrendV2] = []
    for family in family_by_id.values():
        by_action: Dict[str, List[EventV2]] = {}
        for event_id in family.event_ids:
            event = event_by_id.get(event_id)
            if event is None:
                continue
            for action in event.actions:
                by_action.setdefault(action, []).append(event)
        for action, action_events in sorted(by_action.items()):
            if len(action_events) < 2:
                continue
            event_ids = tuple(sorted(item.event_id for item in action_events))
            claim_ids = tuple(sorted({value for item in action_events for value in item.claim_ids}))
            trend_claims = [claim_by_id[value] for value in claim_ids if value in claim_by_id]
            output.append(
                TrendV2(
                    trend_id=stable_id(
                        "trend", {"family": family.topic_family_id, "action": action, "events": event_ids},
                        pipeline_version=P02_PIPELINE_VERSION,
                        ruleset_version=P02_RULESET_VERSION,
                    ),
                    topic_family_id=family.topic_family_id,
                    signal_key=action,
                    event_ids=event_ids,
                    claim_ids=claim_ids,
                    confidence=0.68,
                    source_message_ids=tuple(sorted({item.message_id for item in trend_claims})),
                    evidence_refs=_p01_evidence_union(trend_claims),
                    provenance=ProvenanceV2(
                        tuple(
                            dict.fromkeys(
                                event_ids
                                + tuple(
                                    boundary_id
                                    for item in trend_claims
                                    for boundary_id in _p01_claim_boundary_ids(item)
                                )
                            )
                        ),
                        "trend_derivation_p02",
                        P02_RULESET_VERSION,
                    ),
                    analysis_run_id=run_id,
                    created_at=created_at,
                    pipeline_version=P02_PIPELINE_VERSION,
                    ruleset_version=P02_RULESET_VERSION,
                    uncertainties=("trend_is_candidate_not_fact",),
                )
            )
    result = tuple(sorted(output, key=lambda item: item.trend_id))
    _p02_validate_trends(result, run_id, family_by_id, event_by_id, claim_by_id, owner="P0.2 derived trends")
    return result


def derive_presentations_p02(
    events: Sequence[EventV2],
    claims: Sequence[ClaimV2],
    analysis_run_id: str,
    created_at: str,
) -> Tuple[PresentationV2, ...]:
    run_id = _p02_run_id(analysis_run_id)
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("P0.2 created_at must be a non-empty string")
    claim_by_id = _p02_validate_claims(claims, run_id, owner="P0.2 presentation input claims")
    event_by_id = _p02_validate_events(events, run_id, claim_by_id, owner="P0.2 presentation input events")
    output: List[PresentationV2] = []
    for event in sorted(event_by_id.values(), key=lambda item: item.event_id):
        event_claims = [claim_by_id[value] for value in event.claim_ids if value in claim_by_id]
        if not event_claims:
            raise ValueError("presentation cannot be created for an event without claims")
        evidence = _p01_evidence_union(event_claims)
        source_message_ids = tuple(sorted({value for item in event_claims for value in item.source_message_ids}))
        if evidence != event.evidence_refs or source_message_ids != event.source_message_ids:
            raise ValueError("presentation evidence must equal, not expand, event evidence")
        visible = bool(event.core_entity_ids and event.actions and event.event_type != "unknown")
        entity_text = "、".join(_p01_entity_label(value) for value in event.core_entity_ids[:2])
        action_text = "、".join(_ACTION_LABELS.get(value, value) for value in event.actions[:2])
        title = "%s：%s" % (entity_text or "对象待确认", action_text or "事项待确认")
        sentences = (
            tuple(
                PresentationSentenceV2(
                    text="%s：%s" % (item.speaker_name, item.claim_text),
                    claim_ids=(item.claim_id,),
                    message_ids=tuple(sorted(item.source_message_ids)),
                )
                for item in event_claims
            )
            if visible
            else ()
        )
        supported = event.claim_ids if visible else ()
        presentation_id = stable_id(
            "presentation", {"event_id": event.event_id, "role": "brief" if visible else "do_not_display"},
            pipeline_version=P02_PIPELINE_VERSION,
            ruleset_version=P02_RULESET_VERSION,
        )
        provenance_ids = tuple(
            dict.fromkeys(
                (event.event_id,)
                + event.claim_ids
                + tuple(
                    boundary_id
                    for item in event_claims
                    for boundary_id in _p01_claim_boundary_ids(item)
                )
            )
        )
        output.append(
            PresentationV2(
                presentation_id=presentation_id,
                event_id=event.event_id,
                presentation_role="brief" if visible else "do_not_display",
                title=title,
                summary="；".join(item.text for item in sentences),
                title_support_claim_ids=supported,
                sentences=sentences,
                supported_claim_ids=supported,
                source_message_ids=source_message_ids,
                evidence_refs=evidence,
                confidence=event.confidence,
                source=SHADOW_SOURCE,
                provenance=ProvenanceV2(
                    provenance_ids, "presentation_derivation_p02", P02_RULESET_VERSION
                ),
                analysis_run_id=run_id,
                created_at=created_at,
                pipeline_version=P02_PIPELINE_VERSION,
                ruleset_version=P02_RULESET_VERSION,
                uncertainties=event.uncertainties,
            )
        )
    result = tuple(sorted(output, key=lambda item: item.presentation_id))
    _p02_validate_presentations(result, run_id, event_by_id, claim_by_id, owner="P0.2 derived presentations")
    return result


def validate_p02_invariants(
    result: SemanticResultV2,
    *,
    event_eligible_message_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Validate P0.2 safety invariants without exposing record contents.

    This is intentionally a total validator: malformed/tampered DTOs produce
    a failed report rather than an accidental pass or an uncaught attribute
    error.  Stage-specific gates above perform the same checks eagerly and
    raise when a caller tries to continue with invalid input.
    """

    errors: List[str] = []
    if not isinstance(result, SemanticResultV2):
        return {
            "passed": False,
            "error_count": 1,
            "errors": ("result_not_semantic_dto",),
            "context_only_event_evidence_count": 0,
            "presentation_evidence_expansion_count": 0,
        }
    run_id = result.analysis_run_id if isinstance(result.analysis_run_id, str) else ""
    if not run_id.strip():
        errors.append("analysis_run_id_invalid")
    if result.schema_version != SCHEMA_VERSION:
        errors.append("schema_version_not_semantic_v2")
    if result.pipeline_version != P02_PIPELINE_VERSION:
        errors.append("pipeline_version_not_p02")
    if result.ruleset_version != P02_RULESET_VERSION:
        errors.append("ruleset_version_not_p02")
    if result.source != SHADOW_SOURCE:
        errors.append("non_shadow_source")

    def _tuple_field(name: str) -> Tuple[Any, ...]:
        value = getattr(result, name, ())
        if not isinstance(value, (tuple, list)):
            errors.append("%s_not_a_sequence" % name)
            return ()
        return tuple(value)

    messages = _tuple_field("messages")
    mentions = _tuple_field("mentions")
    claims = _tuple_field("claims")
    candidates = _tuple_field("candidate_pairs")
    decisions = _tuple_field("pair_decisions")
    events = _tuple_field("events")
    families = _tuple_field("topic_families")
    trends = _tuple_field("trends")
    presentations = _tuple_field("presentations")
    message_map = {
        item.message_id: item for item in messages
        if isinstance(item, MessageV2) and isinstance(item.message_id, str)
    }
    mention_map = {
        item.mention_id: item for item in mentions
        if isinstance(item, MentionV2) and isinstance(item.mention_id, str)
    }
    claim_map = {
        item.claim_id: item for item in claims
        if isinstance(item, ClaimV2) and isinstance(item.claim_id, str)
    }
    candidate_map = {
        item.candidate_id: item for item in candidates
        if isinstance(item, CandidatePairV2) and isinstance(item.candidate_id, str)
    }
    decision_map = {
        item.decision_id: item for item in decisions
        if isinstance(item, PairDecisionV2) and isinstance(item.decision_id, str)
    }
    event_map = {
        item.event_id: item for item in events
        if isinstance(item, EventV2) and isinstance(item.event_id, str)
    }
    family_map = {
        item.topic_family_id: item for item in families
        if isinstance(item, TopicFamilyV2) and isinstance(item.topic_family_id, str)
    }

    if run_id:
        gates = (
            ("messages", lambda: _p02_validate_messages(messages, owner="P0.2 invariant messages")),
            ("mentions", lambda: _p02_validate_mentions(mentions, run_id, messages=message_map, owner="P0.2 invariant mentions")),
            ("claims", lambda: _p02_validate_claims(claims, run_id, messages=message_map, mentions=mention_map, owner="P0.2 invariant claims")),
            ("candidate_pairs", lambda: _p02_validate_candidates(candidates, run_id, claim_map, owner="P0.2 invariant candidates")),
            ("pair_decisions", lambda: _p02_validate_decisions(decisions, run_id, claim_map, owner="P0.2 invariant decisions")),
            ("events", lambda: _p02_validate_events(events, run_id, claim_map, decision_map, owner="P0.2 invariant events")),
            ("topic_families", lambda: _p02_validate_families(families, run_id, event_map, claim_map, owner="P0.2 invariant families")),
            ("trends", lambda: _p02_validate_trends(trends, run_id, family_map, event_map, claim_map, owner="P0.2 invariant trends")),
            ("presentations", lambda: _p02_validate_presentations(presentations, run_id, event_map, claim_map, owner="P0.2 invariant presentations")),
        )
        for name, gate in gates:
            try:
                gate()
            except (TypeError, ValueError, AttributeError) as exc:
                errors.append("%s_invalid:%s" % (name, str(exc)))

    eligible_supplied = event_eligible_message_ids is not None
    if eligible_supplied:
        if isinstance(event_eligible_message_ids, (str, bytes)):
            errors.append("eligible_message_ids_malformed")
            eligible = set()
        else:
            try:
                eligible_values = tuple(event_eligible_message_ids)
            except TypeError:
                eligible_values = ()
                errors.append("eligible_message_ids_malformed")
            if any(not isinstance(value, str) or not value.strip() for value in eligible_values):
                errors.append("eligible_message_ids_non_string")
            eligible = {value for value in eligible_values if isinstance(value, str)}
            external = sorted(eligible - set(message_map))
            if external:
                errors.append("eligible_message_ids_external:%s" % ",".join(external))
    else:
        eligible = set()
    for claim in claims:
        if not isinstance(claim, ClaimV2):
            continue
        if not claim.evidence_refs or not claim.source_message_ids:
            errors.append("claim_missing_evidence:%s" % claim.claim_id)
        if eligible_supplied and claim.message_id not in eligible:
            errors.append("claim_context_leak:%s" % claim.claim_id)
        if len(claim.evidence_refs) != 1 or claim.evidence_refs[0] != claim.evidence_span:
            errors.append("claim_evidence_not_canonical:%s" % claim.claim_id)
    for decision in decisions:
        if not isinstance(decision, PairDecisionV2) or decision.relation != RELATION_SAME_EVENT:
            continue
        left = claim_map.get(decision.left_claim_id)
        right = claim_map.get(decision.right_claim_id)
        if left is None or right is None:
            errors.append("same_event_unknown_claim:%s" % decision.decision_id)
            continue
        if decision.must_not_link:
            errors.append("same_event_mnl:%s" % decision.decision_id)
        elif not _p02_same_scope(left, right):
            errors.append("same_event_cross_scope:%s" % decision.decision_id)
        elif _p02_global_conflict_reasons(left, right):
            errors.append("same_event_global_conflict:%s" % decision.decision_id)
        elif (not _p02_known_entity_ids(left) or not _p02_known_entity_ids(right)) and not _p02_shared_instance(left, right):
            errors.append("same_event_unknown_entity:%s" % decision.decision_id)
        elif (not _p02_known_action_ids(left) or not _p02_known_action_ids(right)) and not _p02_shared_instance(left, right):
            errors.append("same_event_unknown_action:%s" % decision.decision_id)
        elif not (left.message_id == right.message_id or _p02_explicit_reply(left, right) or _p02_shared_instance(left, right)):
            errors.append("same_event_without_strong_link:%s" % decision.decision_id)
    for event in events:
        if not isinstance(event, EventV2):
            continue
        if not event.claim_ids:
            errors.append("event_without_claim:%s" % event.event_id)
            continue
        event_claims = [claim_map[value] for value in event.claim_ids if value in claim_map]
        if _p01_evidence_union(event_claims) != event.evidence_refs:
            errors.append("event_evidence_expansion:%s" % event.event_id)
        if eligible_supplied and set(event.source_message_ids) - eligible:
            errors.append("event_context_leak:%s" % event.event_id)
        for decision in decisions:
            if isinstance(decision, PairDecisionV2) and decision.must_not_link and decision.left_claim_id in event.claim_ids and decision.right_claim_id in event.claim_ids:
                errors.append("event_mnl:%s" % event.event_id)
    for card in presentations:
        if not isinstance(card, PresentationV2):
            continue
        event = event_map.get(card.event_id)
        if event is None:
            errors.append("presentation_unknown_event:%s" % card.presentation_id)
        elif card.evidence_refs != event.evidence_refs:
            errors.append("presentation_evidence_expansion:%s" % card.presentation_id)
        if eligible_supplied and set(card.source_message_ids) - eligible:
            errors.append("presentation_context_leak:%s" % card.presentation_id)
    return {
        "passed": not errors,
        "error_count": len(errors),
        "errors": tuple(sorted(set(errors))),
        "context_only_event_evidence_count": 0,
        "presentation_evidence_expansion_count": sum(
            value.startswith("presentation_evidence_expansion:") for value in errors
        ),
    }


def run_semantic_pipeline_p02(
    legacy_messages: Iterable[Mapping[str, Any]],
    *,
    analysis_run_id: Optional[str] = None,
    created_at: Optional[str] = None,
    max_gap_seconds: float = 15 * 60,
    neighbor_window: int = 8,
) -> SemanticResultV2:
    """Run the offline P0.2 development refinement.

    This function accepts only caller-provided mappings and remains isolated
    from production analysis, files, databases, configuration, and network
    services.
    """

    raw_messages = list(legacy_messages)
    if any(not isinstance(message, Mapping) for message in raw_messages):
        raise TypeError("legacy_messages must contain mapping objects")
    raw_ids = [str(message.get("message_id") or "").strip() for message in raw_messages]
    if any(not value for value in raw_ids):
        raise ValueError("semantic_p0_2 requires every message to have message_id")
    if len(set(raw_ids)) != len(raw_ids):
        raise ValueError("duplicate message_id in semantic_p0_2 input")
    roles, annotations, raw_by_id, segment_dicts, ordered_ids = _p01_segment_context(
        raw_messages, max_gap_seconds=max_gap_seconds
    )
    segment_by_message = {
        message_id: str(annotation.get("segment_id") or "")
        for message_id, annotation in annotations.items()
    }
    eligible_ids: set[str] = set()
    for message_id in ordered_ids:
        raw = raw_by_id[message_id]
        role = _p01_role_value(raw, roles.get(message_id))
        if _p01_is_event_eligible(raw, role, annotations.get(message_id)):
            eligible_ids.add(message_id)
    messages = legacy_messages_to_v2(
        raw_messages,
        scope_boundaries=True,
        allow_legacy_instance_aliases=False,
    )
    messages = tuple(
        replace(
            item,
            dialogue_segment_id=segment_by_message.get(item.message_id) or item.dialogue_segment_id,
            block_id=str(annotations.get(item.message_id, {}).get("block_id") or item.block_id or "") or None,
            account_id=str(
                annotations.get(item.message_id, {}).get("account_id") or item.account_id or "default"
            ),
        )
        for item in messages
    )
    message_identity = tuple(
        sorted(
            stable_id(
                "message_input",
                {
                    "message_id": item.message_id,
                    "account_id": item.account_id,
                    "chat_id": item.chat_id,
                    "speaker_id": item.speaker_id,
                    "content": _normalized_text(item.content),
                    "timestamp": item.timestamp,
                    "reply_to": item.reply_to_message_id,
                    "explicit_instance_id": item.explicit_instance_id,
                    "dialogue_segment_id": item.dialogue_segment_id,
                    "block_id": item.block_id,
                    "attribution": item.attribution,
                    "position_in_block": item.position_in_block,
                },
                pipeline_version=P02_PIPELINE_VERSION,
                ruleset_version=P02_RULESET_VERSION,
            )
            for item in messages
        )
    )
    boundary_identity = tuple(
        sorted(
            (
                message_id,
                str(annotation.get("account_id") or raw_by_id[message_id].get("account_id") or "default"),
                str(annotation.get("chat_id") or raw_by_id[message_id].get("chat_id") or "unknown-chat"),
                str(annotation.get("segment_id") or ""),
                str(annotation.get("block_id") or ""),
            )
            for message_id, annotation in annotations.items()
        )
    )
    if analysis_run_id is None:
        resolved_run_id = stable_id(
            "analysis_run",
            {"messages": message_identity, "boundaries": boundary_identity},
            pipeline_version=P02_PIPELINE_VERSION,
            ruleset_version=P02_RULESET_VERSION,
        )
    else:
        resolved_run_id = _p02_run_id(analysis_run_id)
    resolved_created_at = _timestamp_text(created_at) if created_at else _default_created_at(messages)
    mentions, claims = extract_mentions_and_claims_p02(
        messages,
        resolved_run_id,
        resolved_created_at,
        segment_by_message=segment_by_message,
        message_roles=roles,
        eligible_message_ids=eligible_ids,
    )
    candidates = generate_candidate_pairs_p02(
        claims,
        resolved_run_id,
        resolved_created_at,
        neighbor_window=neighbor_window,
    )
    by_id = {item.claim_id: item for item in claims}
    pair_decisions = tuple(
        sorted(
            (
                classify_claim_pair_p02(
                    by_id[item.left_claim_id],
                    by_id[item.right_claim_id],
                    resolved_run_id,
                    resolved_created_at,
                )
                for item in candidates
            ),
            key=lambda item: item.decision_id,
        )
    )
    events = build_events_p02(claims, pair_decisions, resolved_run_id, resolved_created_at)
    families = derive_topic_families_p02(events, claims, resolved_run_id, resolved_created_at)
    trends = derive_trends_p02(events, families, claims, resolved_run_id, resolved_created_at)
    presentations = derive_presentations_p02(events, claims, resolved_run_id, resolved_created_at)
    warnings = tuple(
        sorted(
            "%s:%s" % (message.message_id, warning)
            for message in messages
            for warning in message.compatibility_warnings
        )
    )
    result = SemanticResultV2(
        analysis_run_id=resolved_run_id,
        created_at=resolved_created_at,
        messages=messages,
        mentions=mentions,
        claims=claims,
        pair_decisions=pair_decisions,
        events=events,
        topic_families=families,
        trends=trends,
        presentations=presentations,
        warnings=warnings,
        pipeline_version=P02_PIPELINE_VERSION,
        ruleset_version=P02_RULESET_VERSION,
        candidate_pairs=tuple(candidates),
        message_roles={key: roles[key] for key in sorted(roles)},
        dialogue_segments=tuple(segment_dicts),
        candidate_diagnostics=_p02_candidate_stats(
            claims, candidates, neighbor_window=neighbor_window
        ),
    )
    invariant = validate_p02_invariants(result, event_eligible_message_ids=eligible_ids)
    if not invariant["passed"]:
        raise ValueError("semantic_p0_2 invariant failure: %s" % "; ".join(invariant["errors"]))
    return result


def p02_result_to_legacy_preview(result: SemanticResultV2) -> Dict[str, Any]:
    """Return an explicit read-only P0.2 preview with aggregate candidate stats."""

    preview = v2_result_to_legacy_preview(result)
    preview["candidate_pairs"] = [item.to_dict() for item in result.candidate_pairs]
    preview["candidate_diagnostics"] = dict(result.candidate_diagnostics)
    preview["message_roles"] = dict(result.message_roles)
    preview["dialogue_segments"] = [dict(item) for item in result.dialogue_segments]
    return preview
__all__ = [
    "SCHEMA_VERSION", "PIPELINE_VERSION", "RULESET_VERSION",
    "P01_PIPELINE_VERSION", "P01_RULESET_VERSION", "P01_VERSION", "SHADOW_SOURCE",
    "EVENT_RELATIONS", "MessageV2", "MentionV2", "ClaimV2", "PairDecisionV2", "CandidatePairV2",
    "EventV2", "TopicFamilyV2", "TrendV2", "PresentationSentenceV2", "PresentationV2", "SemanticResultV2",
    "legacy_messages_to_v2", "extract_mentions_and_claims", "classify_claim_pair",
    "classify_candidate_pairs", "build_events", "derive_topic_families", "derive_trends",
    "derive_presentations", "run_semantic_pipeline", "v2_result_to_legacy_preview",
    "score_pairwise_relations", "score_must_not_link", "stable_id",
    "extract_mentions_and_claims_p01", "generate_candidate_pairs_p01",
    "classify_claim_pair_p01", "classify_candidate_pairs_p01", "build_events_p01",
    "derive_topic_families_p01", "derive_trends_p01", "derive_presentations_p01",
    "validate_p01_invariants", "run_semantic_pipeline_p01", "p01_result_to_legacy_preview",
    "P02_PIPELINE_VERSION", "P02_RULESET_VERSION", "P02_VERSION",
    "extract_mentions_and_claims_p02", "generate_candidate_pairs_p02",
    "classify_claim_pair_p02", "classify_candidate_pairs_p02", "build_events_p02",
    "derive_topic_families_p02", "derive_trends_p02", "derive_presentations_p02",
    "validate_p02_invariants", "run_semantic_pipeline_p02", "p02_result_to_legacy_preview",
]
