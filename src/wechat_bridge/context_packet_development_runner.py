"""Development-only K5 material-layer runner.

This module turns the public/redacted 2026-08-25 development message file into
replayable :class:`ContextPacket` material.  It deliberately sits below the
staged DeepSeek analyzer: the local work here is registration, deterministic
window construction and high-recall candidate retrieval.  It does not decide
topics, events, claims, object identity, state transitions or thread closure.

The runner has two output classes:

* ``packets.private.jsonl`` contains the provider-ready redacted text and is
  private by construction; and
* all other output files are body-free accounting/audit projections.

Only an explicitly supplied ``development/messages.private.jsonl`` is read.
The p014 manifest is checked for lineage, but p014 predictions, frozen data,
gold labels, event/title code and provider code are never used as input.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .context_packets import (
    CONTEXT_PACKET_PIPELINE_VERSION,
    CONTEXT_PACKET_RULESET_VERSION,
    CONTEXT_PACKET_VERSION,
    ContextPacketCache,
    build_context_packets,
)
from .contextual_bundle_pipeline_runner import (
    INPUT_FILENAME,
    LOCAL_DAY,
    SPLIT_DEVELOPMENT,
    _guard_development_directory,
    _public_pipeline_messages,
    _read_messages,
    _sha256_bytes,
)
from .dialogue_segments import (
    ROLE_CONVERSATION_OPENER,
    ROLE_CONTEXT_ONLY,
    ROLE_SUBSTANTIVE,
    has_greeting_prefix,
    is_context_only_text,
    is_topic_bearing,
    segment_dialogues,
)
from .semantic_registry import stable_hash


RUNNER_SCHEMA_VERSION = "context_packet_development_runner_v1"
ARTIFACT_VERSION = "context_packet_development_v1"
SELECTION_LIMIT = 20
MIN_SELECTION = 16
MAX_SELECTION = 24
DEFAULT_WINDOW_SIZE = 8
DEFAULT_MAX_CANDIDATES = 3
MAX_BOUNDED_PACKET_MESSAGES = 24

OUTPUT_FILENAMES: Dict[str, str] = {
    "packets": "packets.private.jsonl",
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "errors": "errors.private.jsonl",
    "selection_map": "selection_map.private.jsonl",
    "audit_queue": "audit_queue.private.jsonl",
}

REQUIRED_BUCKETS: Tuple[str, ...] = (
    "greeting_to_new_topic",
    "no_reply_continuation",
    "pronoun_or_ellipsis",
    "person_history",
    "object_history",
    "state_update",
    "topic_shift",
    "media_or_context_only",
    "long_gap_open_boundary",
    "candidate_competition",
)

_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "evidence_text",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_redacted",
    }
)
_SILENT_MESSAGE_TYPES = frozenset({"image", "video", "audio", "file", "sticker", "emoji", "system", "location"})
_GENERIC_TERMS = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "谢谢",
        "多谢",
        "收到",
        "好的",
        "好",
        "嗯",
        "啊",
        "哦",
        "哈哈",
        "最近",
        "怎么样",
        "辛苦了",
        "在吗",
        "hello",
        "hi",
        "ok",
    }
)
_TOKEN_RE = re.compile(r"https?://[^\s，。！？,!?]+|www\.[^\s，。！？,!?]+|[A-Za-z][A-Za-z0-9_.:/@-]{1,}|\d{2,}|[\u4e00-\u9fff]{2,}")
_GREETING_RE = re.compile(r"你好|您好|嗨|哈喽|早上好|晚上好|晚安|辛苦了|最近怎么样|在吗|好久不见|多谢|谢谢|hello|hi", re.I)
_QUESTION_RE = re.compile(r"[?？]|吗[？?。！!，,]?$|怎么|为什么|为何|是否|能否|可不可以|有没有|请问|如何|咋")
_PRONOUN_RE = re.compile(
    r"(?:我(?:们)?|你(?:们)?|您|他(?:们)?|她(?:们)?|它(?:们)?|这(?:个|些|里|样)?|那(?:个|些|里|样)?|"
    r"其|该|自己|对方|本人|谁)"
)
_PERSON_CUE_RE = re.compile(r"(?:客户|同事|老师|朋友|老板|用户|人员|家人|某人|对方|本人|谁)")
_STATE_RE = re.compile(r"完成|成功|失败|开始|结束|解决|取消|重置|恢复|正常|异常|进行中|计划|打算|准备|已经|还在|没|没有|报错|卡住|上线|下线|过期|失效")
# Only explicit topic pivots belong here.  Ordinary contrast/concession
# (notably ``但是``/``不过``) is not a topic boundary by itself.
_SHIFT_RE = re.compile(
    r"(?:换个(?:话题|主题|事情)|换一个(?:话题|主题|事情)|另(?:外|一个)(?:话题|主题|事情)|"
    r"对了(?:[，,:：]|$)|顺便(?:说|问|提)|题外(?:话|说)|说到(?:另|新|这个)|"
    r"然后(?:说|聊)(?:另|新)|转到(?:另|新)|转个(?:话题|主题)|我们(?:先)?聊(?:点|个)?(?:别的|新的?)|"
    r"再说(?:一个|另)|回到(?:之前|刚才)|(?:另起|另开)(?:一个)?(?:话题|主题)|新话题)"
)
_ACK_CONFIRMATION_ONLY_RE = re.compile(
    r"^(?:可以|行|好(?:的)?|收到(?:了|啦)?|明白(?:了)?|了解(?:了)?|知道了|同意|没问题|没事|"
    r"确认(?:了)?|确认收到|ok(?:ay)?|okay)\s*[。.!！?？,，、~～]*$",
    re.I,
)
_URL_RE = re.compile(r"(?:https?://|www\.|[A-Za-z0-9_-]+\.(?:com|cn|org|net|io|dev|me)(?:/|$))", re.I)
_ACTION_RE = re.compile(
    r"(?:处理|查看|看看|实现|开发|测试|部署|修改|修复|安装|运行|发送|接收|选择|比较|确认|解决|上线|重建|生成|读取|保留|更新|检查|需要|请|帮|review|build|test|deploy|fix|install|run|send|check)",
    re.I,
)
_COMPETITION_RE = re.compile(
    r"(?:竞争|竞品|对比|比较|替代|候选|互斥|冲突|方案.{0,8}(?:还是|或者)|(?:还是|或者).{0,8}(?:方案|哪个|哪种)|哪个好|哪个更|二选一|\bvs\.?\b|\bversus\b|\balternative\b|\bcompete\b)",
    re.I,
)


# These are intentionally shallow, body-free selection cues.  They provide
# anchors for an offline review queue; they are never promoted to a final
# person/object/state or relationship decision.  The semantic provider (if a
# later stage is authorised) still owns those decisions.
_SELECTION_CUE_VERSION = "context_packet_selection_cues_v1"
_REPLY_METADATA_FIELDS = (
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
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Any]) -> None:
    path.write_text(
        "".join(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for value in values),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_body_free(value: Any, *, label: str) -> None:
    """Fail closed if a body-bearing key enters a body-free artifact."""

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in _BODY_KEYS:
                    raise ValueError("%s contains body-bearing key: %s" % (label, key))
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)


def _guard_output_directory(directory: Union[str, Path], input_directory: Path) -> Path:
    root = Path(directory)
    if root.resolve() == input_directory.resolve():
        raise ValueError("K5 output directory must differ from development input")
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in root.parts):
        raise ValueError("K5 runner refuses frozen output paths")
    if root.exists():
        raise FileExistsError("K5 artifact output is immutable; choose a new directory")
    return root


def _read_p014_manifest(input_root: Path, raw: bytes, *, strict: bool) -> Tuple[Dict[str, Any], str]:
    manifest_path = input_root / "manifest.json"
    if not manifest_path.is_file():
        if strict:
            raise ValueError("p014 development manifest.json is missing")
        return {}, ""
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("p014 development manifest is invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("p014 development manifest must be an object")
    manifest = dict(value)
    if manifest.get("split") != SPLIT_DEVELOPMENT or manifest.get("status") != SPLIT_DEVELOPMENT:
        raise ValueError("K5 input manifest is not development")
    if manifest.get("local_day") not in (None, LOCAL_DAY):
        raise ValueError("K5 input manifest is not 2026-08-25")
    split_version = str(manifest.get("split_version") or "")
    if strict and split_version != "wechat-2026-08-25-p014-evaluation-split-v1":
        raise ValueError("K5 input is not the p014 development split")
    raw_hash = _sha256_bytes(raw)
    file_hashes = manifest.get("file_sha256")
    expected = file_hashes.get(INPUT_FILENAME) if isinstance(file_hashes, Mapping) else None
    if expected and str(expected) != raw_hash:
        raise ValueError("K5 input hash does not match p014 manifest")
    if strict and not expected:
        raise ValueError("p014 manifest does not bind development messages")
    return manifest, _sha256_file(manifest_path)


def _message_text(row: Mapping[str, Any]) -> str:
    for key in ("content", "text", "message_text", "redacted_text"):
        value = row.get(key)
        if value is not None:
            return str(value)
    return ""


def _is_true_greeting_text(text: Any, *, message_type: Any = "text") -> bool:
    """Return only a genuine social opener, never an acknowledgement.

    ``dialogue_segments.is_greeting_only`` intentionally accepts short social
    words such as ``可以`` for context suppression.  That is useful for role
    segmentation but too broad for the ``greeting_boundary`` candidate: an
    acknowledgement at the start of a segment must remain context-only.  The
    extra explicit greeting-prefix check keeps this projection opener-only
    while still allowing polite greeting/thanks variants.
    """

    value = str(text or "").strip()
    if not value or _ACK_CONFIRMATION_ONLY_RE.fullmatch(value):
        return False
    if not is_context_only_text(value, message_type=message_type):
        return False
    return bool(_GREETING_RE.search(value)) and has_greeting_prefix(value)


def _message_scope(row: Mapping[str, Any]) -> Tuple[str, str]:
    return str(row.get("account_id") or "unknown"), str(row.get("chat_id") or "unknown")


def _number(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _sequence(row: Mapping[str, Any]) -> float:
    value = _number(row.get("sequence_in_chat", row.get("sequence")))
    return value if value is not None else float("inf")


def _surface_terms(text: str) -> Tuple[str, ...]:
    output: List[str] = []
    seen: Set[str] = set()
    for raw in _TOKEN_RE.findall(text or ""):
        term = raw.lower() if re.search(r"[A-Za-z]", raw) else raw
        if term in _GENERIC_TERMS or len(term) < 2 or len(term) > 96:
            continue
        if term not in seen:
            seen.add(term)
            output.append(term)
    return tuple(output[:48])


@dataclass(frozen=True)
class SurfaceSignals:
    terms: Tuple[str, ...] = ()
    greeting: bool = False
    question: bool = False
    pronoun_or_ellipsis: bool = False
    state: bool = False
    topic_shift: bool = False
    textual: bool = True
    url_count: int = 0
    number_count: int = 0

    @classmethod
    def for_row(cls, row: Mapping[str, Any]) -> "SurfaceSignals":
        text = _message_text(row)
        message_type = str(row.get("message_type") or "").casefold()
        textual = bool(text.strip()) and message_type not in _SILENT_MESSAGE_TYPES
        terms = _surface_terms(text)
        return cls(
            terms=terms,
            greeting=bool(_GREETING_RE.search(text)),
            question=bool(_QUESTION_RE.search(text)),
            pronoun_or_ellipsis=bool(_PRONOUN_RE.search(text) or _PERSON_CUE_RE.search(text)),
            state=bool(_STATE_RE.search(text)),
            topic_shift=bool(_SHIFT_RE.search(text)),
            textual=textual,
            url_count=len(_URL_RE.findall(text)),
            number_count=len(re.findall(r"\d+", text)),
        )

    def to_private_dict(self, message_id: str) -> Dict[str, Any]:
        return {
            "message_id": message_id,
            "sparse_terms": list(self.terms),
            "greeting_signal": self.greeting,
            "question_signal": self.question,
            "pronoun_or_ellipsis_signal": self.pronoun_or_ellipsis,
            "state_signal": self.state,
            "topic_shift_signal": self.topic_shift,
            "textual": self.textual,
            "url_count": self.url_count,
            "number_count": self.number_count,
        }


def _message_ref(message_id: str, row: Mapping[str, Any], *, source: str = "material_layer") -> Dict[str, Any]:
    text = _message_text(row)
    return {
        "type": "message",
        "id": message_id,
        "message_id": message_id,
        "span": {"start": 0, "end": len(text)},
        "source": source,
    }


def _ordered_packet_message_ids(packet: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]]) -> Tuple[str, ...]:
    """Return packet message IDs in authoritative local order.

    K2 packets can expose a fragment ID and a message ID in more than one
    view.  Selection cues are message-level evidence, so de-duplicate those
    views and sort by the source sequence while retaining a deterministic
    lexical fallback.  No message body is used for ordering.
    """

    values: List[str] = []
    for key in ("source_message_ids",):
        value = packet.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(str(item) for item in value if item not in (None, ""))
    for key in ("primary_fragments", "adjacent_context"):
        value = packet.get(key)
        if not isinstance(value, (list, tuple, set, frozenset)):
            continue
        for item in value:
            if isinstance(item, Mapping):
                message_id = item.get("message_id") or item.get("source_message_id")
                if message_id not in (None, ""):
                    values.append(str(message_id))
            elif item not in (None, ""):
                values.append(str(item))
    unique = list(dict.fromkeys(value for value in values if value in rows))
    unique.sort(key=lambda value: (_sequence(rows[value]), value))
    return tuple(unique)


def _message_annotation(
    message_id: str,
    row: Mapping[str, Any],
    annotations: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Resolve conservative role/topic annotations for one source message."""

    annotation = dict(annotations.get(message_id) or {})
    message_type = row.get("message_type", "text")
    text = _message_text(row)
    role = str(
        annotation.get("role")
        or row.get("dialogue_role")
        or row.get("message_role")
        or (ROLE_CONTEXT_ONLY if is_context_only_text(text, message_type=message_type) else ROLE_SUBSTANTIVE)
    )
    topic_bearing = bool(
        annotation.get("topic_bearing")
        if "topic_bearing" in annotation
        else is_topic_bearing(text, message_type=message_type)
    )
    # Do not trust a broad upstream ``greeting_only``/segment-opener marker:
    # acknowledgements such as ``可以`` may be context-only without being a
    # conversation opener.  The text-level gate is deliberately stricter and
    # requires a real greeting prefix.
    greeting_only = _is_true_greeting_text(text, message_type=message_type)
    opener = greeting_only and role in {ROLE_CONVERSATION_OPENER, ROLE_CONTEXT_ONLY}
    return {
        "role": role,
        "topic_bearing": topic_bearing,
        "greeting_only": greeting_only,
        "opener": opener,
        "segment_id": str(annotation.get("segment_id") or row.get("dialogue_segment_id") or ""),
    }


def _message_signal_codes(
    row: Mapping[str, Any],
    signal: SurfaceSignals,
    annotation: Mapping[str, Any],
) -> Tuple[str, ...]:
    """Project only shallow, explainable signal names (never their text)."""

    text = _message_text(row)
    values: List[str] = []
    if _is_true_greeting_text(text, message_type=row.get("message_type", "text")):
        values.append("greeting_opener")
    if bool(signal.question):
        values.append("question")
    if bool(signal.pronoun_or_ellipsis):
        values.append("pronoun_or_ellipsis")
    if int(signal.url_count) > 0:
        values.append("url")
    if int(signal.number_count) > 0:
        values.append("number")
    if signal.terms:
        values.append("entity_like_span")
    if bool(signal.state):
        values.append("state_cue")
    if _ACTION_RE.search(text):
        values.append("action_cue")
    if _reference_target_ids(row):
        values.append("explicit_reply_quote")
    if _SHIFT_RE.search(text):
        values.append("explicit_transition_cue")
    if bool(annotation.get("topic_bearing")):
        values.append("topic_bearing")
    if str(annotation.get("role")) in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER}:
        values.append("context_only")
    return tuple(dict.fromkeys(values))


def _grounding_refs(
    message_id: str,
    row: Mapping[str, Any],
    signal: SurfaceSignals,
) -> Tuple[str, ...]:
    """Return opaque grounded-cue handles for recall candidates.

    URLs, numbers, sparse entity-like terms, and shallow state/action cues are
    retained only as hashed handles.  They are useful for finding possible
    alternatives or carryover, but are deliberately not written as
    ``object_id``/``state`` decisions.
    """

    values: List[str] = []
    scope = _message_scope(row)
    if signal.url_count:
        values.append("GROUNDING_URL_%s" % stable_hash({"scope": scope, "message_id": message_id})[:20])
    if signal.number_count:
        values.append("GROUNDING_NUMBER_%s" % stable_hash({"scope": scope, "message_id": message_id})[:20])
    if signal.state:
        values.append("GROUNDING_STATE_%s" % stable_hash({"scope": scope, "message_id": message_id})[:20])
    if _ACTION_RE.search(_message_text(row)):
        values.append("GROUNDING_ACTION_%s" % stable_hash({"scope": scope, "message_id": message_id})[:20])
    for term in signal.terms[:8]:
        values.append("GROUNDING_ENTITY_%s" % stable_hash({"scope": scope, "term": term})[:20])
    return tuple(dict.fromkeys(values))


def _selection_evidence_refs(
    message_ids: Sequence[str],
    rows: Mapping[str, Mapping[str, Any]],
    *,
    evidence_kind: str,
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for message_id in dict.fromkeys(str(item) for item in message_ids if item in rows):
        ref = _message_ref(message_id, rows[message_id], source="development_source_message")
        ref["evidence_kind"] = str(evidence_kind)
        ref["source_message_id"] = message_id
        refs.append(ref)
    return refs


def _selection_cue_row(
    kind: str,
    message_ids: Sequence[str],
    rows: Mapping[str, Mapping[str, Any]],
    *,
    signal_codes: Sequence[str] = (),
    candidate_refs: Sequence[str] = (),
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a candidate-only typed-sidecar row with source spans."""

    refs = [str(item) for item in dict.fromkeys(str(value) for value in message_ids if value in rows)]
    evidence = _selection_evidence_refs(refs, rows, evidence_kind=kind)
    payload = {
        "kind": str(kind),
        "message_ids": refs,
        "signal_codes": sorted({str(value) for value in signal_codes if value}),
        "candidate_refs": sorted({str(value) for value in candidate_refs if value}),
    }
    relation_id = "SELECTION_CUE_%s_%s" % (str(kind).upper(), stable_hash(payload)[:20])
    first = rows[refs[0]] if refs else {}
    row: Dict[str, Any] = {
        "candidate_id": relation_id,
        "candidate_ref": relation_id,
        "candidate_refs": list(dict.fromkeys([relation_id] + [str(value) for value in candidate_refs if value])),
        "message_ref": refs[0] if refs else "",
        "message_refs": refs,
        "endpoint_message_refs": refs,
        "source_message_ids": refs,
        "scoped_evidence_ref": evidence[0] if evidence else {},
        "scoped_evidence_refs": evidence,
        "evidence_refs": evidence,
        "evidence_type": "selection_cue",
        "cue_kind": str(kind),
        "signal_codes": sorted({str(value) for value in signal_codes if value}),
        # These flags are intentionally explicit so a later selector cannot
        # mistake a shallow recall cue for a final semantic relation.
        "selection_cue": True,
        "candidate_only": True,
        "semantic_decision_pending": True,
        "materialized_relation": False,
        "strong_relation": False,
        "account_id": str(first.get("account_id") or "unknown"),
        "chat_id": str(first.get("chat_id") or "unknown"),
    }
    row.update(dict(extra or {}))
    return row


def _unique_sidecar_rows(values: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        key = stable_hash(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _pronoun_support_pairs(
    pronoun_ids: Sequence[str],
    support_ids: Sequence[str],
    rows: Mapping[str, Mapping[str, Any]],
    grounding_by_id: Mapping[str, Sequence[str]],
) -> List[Tuple[str, str, str]]:
    """Bind a pronoun/person cue to independent grounded evidence.

    A short pronoun turn is not evidence of a person/object/state triad by
    itself.  Require a second, distinct source message in the same scope with
    at least one opaque grounded cue.  Explicit reply/quote edges are recorded
    when present; otherwise the pair is still auditable through its distinct
    same-scope message refs, but remains candidate-only.
    """

    result: List[Tuple[str, str, str]] = []
    for pronoun_id in pronoun_ids:
        pronoun_row = rows.get(pronoun_id)
        if pronoun_row is None:
            continue
        for support_id in support_ids:
            if pronoun_id == support_id:
                continue
            support_row = rows.get(support_id)
            if support_row is None or _message_scope(pronoun_row) != _message_scope(support_row):
                continue
            if not grounding_by_id.get(support_id):
                continue
            pronoun_targets = set(_reference_target_ids(pronoun_row))
            support_targets = set(_reference_target_ids(support_row))
            binding = "explicit_reply_or_quote" if support_id in pronoun_targets or pronoun_id in support_targets else "same_scope_distinct_message_refs"
            result.append((str(pronoun_id), str(support_id), binding))
    return result


def _materialize_selection_cues(
    packet: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    signals: Mapping[str, SurfaceSignals],
    annotations: Mapping[str, Mapping[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """Materialize shallow candidate/evidence sidecars for one K2 packet.

    The five arrays intentionally mirror the typed ContextPacket sidecar
    names.  Every generated row remains candidate-only and carries source
    message IDs/spans.  Strong typed rows already emitted by
    ``ContextPacketBuilder`` are merged by the caller and are never replaced.
    """

    message_ids = _ordered_packet_message_ids(packet, rows)
    if not message_ids:
        return {name: [] for name in ("message_metadata", "topic_transitions", "candidate_competition", "reply_status", "pronoun_person_object_state")}, {
            "selection_cue_version": _SELECTION_CUE_VERSION,
            "candidate_row_counts": {},
            "candidate_packet": False,
        }
    ordered = list(message_ids)
    metadata_rows: List[Dict[str, Any]] = []
    transition_rows: List[Dict[str, Any]] = []
    competition_rows: List[Dict[str, Any]] = []
    reply_rows: List[Dict[str, Any]] = []
    triad_rows: List[Dict[str, Any]] = []
    annotations_by_id = {message_id: _message_annotation(message_id, rows[message_id], annotations) for message_id in ordered}
    codes_by_id = {
        message_id: _message_signal_codes(rows[message_id], signals.get(message_id, SurfaceSignals()), annotations_by_id[message_id])
        for message_id in ordered
    }
    grounding_by_id = {
        message_id: _grounding_refs(message_id, rows[message_id], signals.get(message_id, SurfaceSignals()))
        for message_id in ordered
    }

    # Preserve authoritative local metadata and shallow signals for every
    # retained message.  Values are handles/counts/roles only; no text is
    # copied into the typed projection.
    for message_id in ordered:
        source = rows[message_id]
        annotation = annotations_by_id[message_id]
        signal = signals.get(message_id, SurfaceSignals())
        row = _selection_cue_row(
            "message_metadata",
            (message_id,),
            rows,
            signal_codes=codes_by_id[message_id],
            extra={
                "source_message_id": message_id,
                "role": annotation["role"],
                "topic_bearing": bool(annotation["topic_bearing"]),
                "greeting_only": bool(annotation["greeting_only"]),
                "is_opener_or_greeting": bool(annotation["opener"]),
                "reply_target_present": bool(_reference_target_ids(source)),
                "authoritative_metadata": {
                    "account_id": str(source.get("account_id") or "unknown"),
                    "chat_id": str(source.get("chat_id") or "unknown"),
                    "message_type": str(source.get("message_type") or "unknown"),
                    "sequence_in_chat": source.get("sequence_in_chat"),
                    "dialogue_segment_id": annotation.get("segment_id") or "unknown",
                },
            },
        )
        metadata_rows.append(row)

    topic_ids = [
        message_id
        for message_id in ordered
        if bool(annotations_by_id[message_id]["topic_bearing"])
        and annotations_by_id[message_id]["role"] == ROLE_SUBSTANTIVE
    ]
    opener_ids = [message_id for message_id in ordered if annotations_by_id[message_id]["opener"]]
    for opener in opener_ids:
        opener_index = ordered.index(opener)
        followup = next((message_id for message_id in ordered[opener_index + 1 :] if message_id in topic_ids), None)
        if followup is None:
            continue
        row = _selection_cue_row(
            "greeting_boundary",
            (opener, followup),
            rows,
            signal_codes=tuple(dict.fromkeys((*codes_by_id[opener], *codes_by_id[followup], "explicit_boundary"))),
            extra={
                "is_opener_or_greeting": True,
                "topic_bearing_followup": True,
                "topic_bearing_followup_message_ref": followup,
                "topic_bearing_followup_message_refs": [followup],
                "explicit_boundary": True,
                "boundary_reason": "opener_to_substantive_topic_followup",
            },
        )
        metadata_rows.append(row)

    # Only an explicit transition cue can create a transition candidate.  A
    # topic-bearing message must exist on both sides of the cue; time gaps,
    # segment IDs, and generic adjacency are not substitutes.
    for index, message_id in enumerate(ordered):
        if "explicit_transition_cue" not in codes_by_id[message_id]:
            continue
        left = next((value for value in reversed(ordered[:index]) if value in topic_ids), None)
        right = next((value for value in ordered[index + 1 :] if value in topic_ids), None)
        if left is None or right is None:
            continue
        row = _selection_cue_row(
            "topic_shift",
            (left, message_id, right),
            rows,
            signal_codes=tuple(dict.fromkeys((*codes_by_id[left], *codes_by_id[message_id], *codes_by_id[right]))),
            extra={
                "explicit_transition_cue": True,
                "topic_shift_candidate": True,
                "topic_shift_or_boundary": True,
                "topic_bearing_left": True,
                "topic_bearing_right": True,
                "transition_message_ref": message_id,
                "transition_type": "explicit_transition_candidate",
                "boundary_reason": "explicit_transition_cue",
                "endpoint_message_refs": [left, right],
            },
        )
        transition_rows.append(row)

    # Competition candidates require an explicit comparison/alternative cue
    # and at least two distinct grounded recall candidates.  Candidate volume
    # alone is intentionally insufficient.
    for message_id in ordered:
        if not _COMPETITION_RE.search(_message_text(rows[message_id])):
            continue
        grounded_messages = [
            value
            for value in ordered
            if value != message_id
            and annotations_by_id[value]["role"] == ROLE_SUBSTANTIVE
            and grounding_by_id[value]
        ]
        candidate_refs = [
            "RECALL_CANDIDATE_%s" % stable_hash({"message_id": value, "grounding": grounding_by_id[value]})[:20]
            for value in grounded_messages
        ]
        candidate_refs = list(dict.fromkeys(candidate_refs))
        if len(candidate_refs) < 2:
            continue
        evidence_messages = [message_id] + grounded_messages[: min(len(grounded_messages), 4)]
        row = _selection_cue_row(
            "candidate_competition",
            evidence_messages,
            rows,
            signal_codes=tuple(dict.fromkeys((*codes_by_id[message_id], "competition_evidence"))),
            candidate_refs=candidate_refs,
            extra={
                "explicit_competition_cue": True,
                "competition_evidence": True,
                "competing_candidate_refs": candidate_refs,
                "grounded_candidate_refs": candidate_refs,
                "grounded_recall_candidate_count": len(candidate_refs),
            },
        )
        competition_rows.append(row)

    # No-reply is a candidate status only when an explicit reply/quote is
    # absent and at least two independent semantic carryover families support
    # the pair.  Missing reply metadata by itself is never evidence.
    for left_index, left in enumerate(ordered):
        left_source = rows[left]
        left_signal = signals.get(left, SurfaceSignals())
        if _reference_target_ids(left_source):
            continue
        for right in ordered[left_index + 1 :]:
            right_source = rows[right]
            if _reference_target_ids(right_source):
                continue
            right_annotation = annotations_by_id[right]
            if right_annotation["role"] != ROLE_SUBSTANTIVE or not left_signal.textual:
                continue
            right_signal = signals.get(right, SurfaceSignals())
            families: Set[str] = set()
            if left_signal.question and right_signal.textual:
                families.add("question_answer")
            if set(grounding_by_id[left]) & set(grounding_by_id[right]):
                families.add("shared_grounded_cue")
            if (left_signal.state or _ACTION_RE.search(_message_text(left_source))) and (
                right_signal.state or _ACTION_RE.search(_message_text(right_source))
            ):
                families.add("state_action_cue")
            if (left_signal.url_count or left_signal.number_count) and (right_signal.url_count or right_signal.number_count):
                families.add("url_number_cue")
            if len(families) < 2:
                continue
            row = _selection_cue_row(
                "no_reply",
                (left, right),
                rows,
                signal_codes=tuple(dict.fromkeys((*codes_by_id[left], *codes_by_id[right], *families))),
                extra={
                    "authoritative_status": "awaiting_reply",
                    "reply_status": "awaiting_reply",
                    "no_explicit_reply": True,
                    "reply_edge_checked": True,
                    "semantic_continuation_signals": sorted(families),
                    "continuation_signal_count": len(families),
                    "reply_to_absent": True,
                },
            )
            reply_rows.append(row)

    # Keep unknown typed slots explicit while preserving this as a recall cue;
    # it must never masquerade as a resolved person/object/state triad.
    pronoun_ids = [
        message_id
        for message_id in ordered
        if annotations_by_id[message_id]["role"] == ROLE_SUBSTANTIVE
        and "pronoun_or_ellipsis" in codes_by_id[message_id]
    ]
    support_ids = [
        message_id
        for message_id in ordered
        if annotations_by_id[message_id]["role"] == ROLE_SUBSTANTIVE
        and message_id not in pronoun_ids
        and grounding_by_id[message_id]
    ]
    pronoun_support_pairs = _pronoun_support_pairs(pronoun_ids, support_ids, rows, grounding_by_id)
    if pronoun_support_pairs:
        selected_pairs = pronoun_support_pairs[:6]
        pair_message_ids = [value for pair in selected_pairs for value in pair[:2]]
        candidate_refs = [
            "PRONOUN_RECALL_%s" % stable_hash(
                {
                    "pronoun_message_id": pronoun_id,
                    "support_message_id": support_id,
                    "grounding": grounding_by_id[support_id],
                    "binding": binding,
                }
            )[:20]
            for pronoun_id, support_id, binding in selected_pairs
        ]
        row = _selection_cue_row(
            "pronoun_person_object_state",
            tuple(dict.fromkeys(pair_message_ids)),
            rows,
            signal_codes=tuple(
                dict.fromkeys(
                    code
                    for message_id in dict.fromkeys(pair_message_ids)
                    for code in codes_by_id[message_id]
                )
            ),
            candidate_refs=candidate_refs,
            extra={
                "pronoun_signal": True,
                "person_cue_message_refs": [pronoun_id for pronoun_id, _, _ in selected_pairs],
                "grounded_support_message_refs": [support_id for _, support_id, _ in selected_pairs],
                "grounded_cue_codes": sorted(
                    {
                        code
                        for _, support_id, _ in selected_pairs
                        for code in codes_by_id[support_id]
                        if code in {"entity_like_span", "url", "number", "state_cue", "action_cue"}
                    }
                ),
                "binding_evidence": [
                    {
                        "pronoun_message_ref": pronoun_id,
                        "grounded_support_message_ref": support_id,
                        "binding_type": binding,
                    }
                    for pronoun_id, support_id, binding in selected_pairs
                ],
                "independent_grounded_support": True,
                "typed_evidence_fields": [],
                "typed_evidence_slots": {"person": "unknown", "object": "unknown", "state": "unknown"},
                "typed_evidence_unknown": True,
            },
        )
        triad_rows.append(row)

    values = {
        "message_metadata": _unique_sidecar_rows(metadata_rows),
        "topic_transitions": _unique_sidecar_rows(transition_rows),
        "candidate_competition": _unique_sidecar_rows(competition_rows),
        "reply_status": _unique_sidecar_rows(reply_rows),
        "pronoun_person_object_state": _unique_sidecar_rows(triad_rows),
    }
    counts = {key: len(value) for key, value in values.items()}
    return values, {
        "selection_cue_version": _SELECTION_CUE_VERSION,
        "candidate_row_counts": counts,
        "candidate_packet": any(counts.values()),
    }


def _fragment_ids(packet: Mapping[str, Any], message_ids: Sequence[str]) -> List[str]:
    by_message: Dict[str, List[str]] = defaultdict(list)
    for item in tuple(packet.get("primary_fragments") or ()) + tuple(packet.get("adjacent_context") or ()):
        if isinstance(item, Mapping):
            message_id = str(item.get("message_id") or "")
            fragment_id = str(item.get("fragment_id") or "")
            if message_id and fragment_id:
                by_message[message_id].append(fragment_id)
    result: List[str] = []
    for message_id in message_ids:
        result.extend(by_message.get(message_id, ()))
    return list(dict.fromkeys(result))


def _packet_primary_ids(packet: Mapping[str, Any]) -> Tuple[str, ...]:
    values = []
    for item in packet.get("primary_fragments") or ():
        if isinstance(item, Mapping) and item.get("message_id"):
            values.append(str(item["message_id"]))
    if not values:
        values = [str(item) for item in (packet.get("source_message_ids") or ()) if item]
    return tuple(dict.fromkeys(values))


def _packet_adjacent_ids(packet: Mapping[str, Any]) -> Tuple[str, ...]:
    values = []
    for item in packet.get("adjacent_context") or ():
        if isinstance(item, Mapping) and item.get("message_id"):
            values.append(str(item["message_id"]))
    return tuple(dict.fromkeys(values))


def _packet_scale(packet: Mapping[str, Any]) -> str:
    dynamic = packet.get("dynamic_part") if isinstance(packet.get("dynamic_part"), Mapping) else {}
    candidates = dynamic.get("open_thread_candidates") if isinstance(dynamic, Mapping) else None
    if isinstance(candidates, (list, tuple)) and candidates and isinstance(candidates[0], Mapping):
        value = candidates[0].get("window_scale")
        if value:
            return str(value)
    value = packet.get("window_scale")
    if value:
        return str(value)
    return {"micro": "W0", "turn": "W0", "local": "W1", "session": "W2", "sparse": "W3", "cold": "W4"}.get(str(packet.get("scale")), "unknown")


def _packet_all_ids(packet: Mapping[str, Any]) -> Tuple[str, ...]:
    values = [str(item) for item in (packet.get("source_message_ids") or ()) if item]
    values.extend(_packet_adjacent_ids(packet))
    return tuple(dict.fromkeys(values))


def _reference_target_ids(row: Mapping[str, Any]) -> Tuple[str, ...]:
    values: List[str] = []
    for key in ("reply_to_message_id", "quoted_message_id", "quote_message_id", "referenced_message_id", "reference_message_id", "parent_message_id", "in_reply_to"):
        value = row.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    for key in ("context_message_ids",):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value if item)
    return tuple(dict.fromkeys(values))


def _candidate_id(kind: str, payload: Mapping[str, Any]) -> str:
    return "%s_%s" % (str(kind).upper(), stable_hash(payload)[:20])


def _pair_candidate(
    left_id: str,
    right_id: str,
    rows: Mapping[str, Mapping[str, Any]],
    signals: Mapping[str, SurfaceSignals],
    *,
    kind: str = "continuity",
) -> Optional[Dict[str, Any]]:
    left = rows.get(left_id)
    right = rows.get(right_id)
    if left is None or right is None or _message_scope(left) != _message_scope(right):
        return None
    left_sig, right_sig = signals[left_id], signals[right_id]
    reasons: List[str] = []
    support: List[str] = []
    explicit = set(_reference_target_ids(left)) | set(_reference_target_ids(right))
    if left_id in explicit or right_id in explicit:
        reasons.append("explicit_reply_or_quote_metadata")
        support.append("explicit_reference")
    overlap = sorted(set(left_sig.terms) & set(right_sig.terms))
    if overlap:
        reasons.append("sparse_term_overlap")
        support.append("lexical_overlap")
    if str(left.get("speaker_id") or "unknown") == str(right.get("speaker_id") or "unknown") and str(left.get("speaker_id") or "unknown") != "unknown":
        reasons.append("same_speaker_metadata")
        support.append("same_speaker")
    same_segment = bool(left.get("dialogue_segment_id") and left.get("dialogue_segment_id") == right.get("dialogue_segment_id"))
    if same_segment:
        reasons.append("same_segment_weak")
    left_time, right_time = _number(left.get("time_offset_seconds")), _number(right.get("time_offset_seconds"))
    time_distance = abs(left_time - right_time) if left_time is not None and right_time is not None else None
    if time_distance is not None:
        reasons.append("time_proximity_weak")
    # Time and same-segment are deliberately not enough to create a candidate.
    if not support and not (left_sig.question and right_sig.textual):
        return None
    if left_sig.question and right_sig.textual:
        reasons.append("question_followup_surface")
        support.append("question_signal")
    primary_id, secondary_id = (left_id, right_id) if _sequence(left) <= _sequence(right) else (right_id, left_id)
    payload = {
        "kind": kind,
        "left_message_id": primary_id,
        "right_message_id": secondary_id,
        "reason_codes": tuple(dict.fromkeys(reasons)),
    }
    evidence_refs = [_message_ref(primary_id, rows[primary_id]), _message_ref(secondary_id, rows[secondary_id])]
    strong = bool(explicit)
    return {
        "candidate_id": _candidate_id(kind, payload),
        "left_message_id": primary_id,
        "right_message_id": secondary_id,
        "relation_label": "candidate_only",
        "relation_subtype": "explicit_reference_candidate" if strong else "surface_context_candidate",
        "supporting_slot_codes": list(dict.fromkeys(support)),
        "semantic_support": list(dict.fromkeys(support)),
        "candidate_reason": list(dict.fromkeys(reasons)),
        "confidence": "high" if strong else "low" if not overlap else "medium",
        "evidence_strength_candidate": "explicit" if strong else "weak",
        "explicit_reply_present": strong,
        "strong_relation": False,
        "time_distance_seconds": time_distance,
        "time_is_weak_only": True,
        "same_segment_is_weak_only": True,
        "evidence_refs": evidence_refs,
        "source_refs": evidence_refs,
        "uncertainties": ["candidate_only", "semantic_relation_pending"],
        "candidate_only": True,
    }


def _history_ids(
    message_id: str,
    rows: Mapping[str, Mapping[str, Any]],
    *,
    predicate: Any,
    limit: int = 3,
) -> List[str]:
    source = rows.get(message_id)
    if source is None:
        return []
    scope = _message_scope(source)
    candidates = [
        mid
        for mid, row in rows.items()
        if mid != message_id and _message_scope(row) == scope and predicate(row)
    ]
    candidates.sort(key=lambda mid: (abs(_sequence(rows[mid]) - _sequence(source)), _sequence(rows[mid]), mid))
    return candidates[:limit]


def _candidate_history_row(
    kind: str,
    message_id: str,
    history_ids: Sequence[str],
    rows: Mapping[str, Mapping[str, Any]],
    *,
    ref_key: str,
    ref_value: str,
    reason: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    values = list(dict.fromkeys([message_id] + [str(item) for item in history_ids if item]))
    refs = [_message_ref(mid, rows[mid]) for mid in values if mid in rows]
    payload = {"kind": kind, "message_ids": values, "ref": ref_value, "reason": reason}
    result: Dict[str, Any] = {
        "candidate_id": _candidate_id(kind, payload),
        ref_key: ref_value,
        "message_ids": values,
        "candidate_reason": [reason],
        "reason_codes": [reason],
        "confidence": "low",
        "evidence_refs": refs,
        "source_refs": refs,
        "uncertainties": ["candidate_only", "semantic_identity_pending"],
        "candidate_only": True,
    }
    result.update(dict(extra or {}))
    return result


@dataclass(frozen=True)
class PreparedPacket:
    """Private packet plus body-free selection metadata."""

    value: Mapping[str, Any]
    categories: Tuple[str, ...]
    primary_ids: Tuple[str, ...]
    adjacent_ids: Tuple[str, ...]
    candidate_count: int
    evidence_count: int
    surface_signal_count: int
    window_scale: str

    @property
    def packet_id(self) -> str:
        return str(self.value.get("packet_id") or "")

    @property
    def packet_hash(self) -> str:
        return str(self.value.get("packet_hash") or "")

    @property
    def scope(self) -> Tuple[str, str]:
        return str(self.value.get("account_id") or "unknown"), str(self.value.get("chat_id") or "unknown")

    @property
    def message_ids(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(self.primary_ids + self.adjacent_ids))

    @property
    def message_count(self) -> int:
        return len(self.message_ids)


@dataclass(frozen=True)
class K5MaterialRunResult:
    input_directory: str
    output_directory: str
    input_sha256: str
    packet_count: int
    selected_packet_count: int
    manifest_path: str
    artifact_paths: Mapping[str, str]
    manifest: Mapping[str, Any]
    aggregate: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "input_sha256": self.input_sha256,
            "packet_count": self.packet_count,
            "selected_packet_count": self.selected_packet_count,
            "manifest_path": self.manifest_path,
            "artifact_paths": dict(self.artifact_paths),
            "manifest": dict(self.manifest),
            "aggregate": dict(self.aggregate),
        }


def _runner_code_sha256() -> str:
    here = Path(__file__).resolve()
    paths = (
        here,
        here.with_name("context_packets.py"),
        here.with_name("dialogue_bundle.py"),
        here.with_name("semantic_registry.py"),
        here.with_name("semantic_gate.py"),
        here.with_name("contextual_bundle_pipeline_runner.py"),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _candidate_message_ids(value: Mapping[str, Any]) -> Tuple[str, ...]:
    result: List[str] = []
    for key in (
        "left_message_id",
        "right_message_id",
        "question_id",
        "answer_id",
        "reply_to_message_id",
        "message_id",
    ):
        item = value.get(key)
        if isinstance(item, str) and item:
            result.append(item)
    for key in ("message_ids", "source_message_ids"):
        item = value.get(key)
        if isinstance(item, (list, tuple)):
            result.extend(str(child) for child in item if child)
    return tuple(dict.fromkeys(result))


def _build_enriched_packet(
    base: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    signals: Mapping[str, SurfaceSignals],
    scope_message_ids: Mapping[Tuple[str, str], Tuple[str, ...]],
    term_index: Mapping[Tuple[Tuple[str, str], str], Tuple[str, ...]],
    state_index: Mapping[Tuple[str, str], Tuple[str, ...]],
    annotations: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> PreparedPacket:
    packet = {str(key): _jsonable(value) for key, value in base.items()}
    primary_ids = _packet_primary_ids(packet)
    adjacent_ids = _packet_adjacent_ids(packet)
    all_ids = tuple(dict.fromkeys(primary_ids + adjacent_ids))
    anchor = primary_ids[0] if primary_ids else (all_ids[0] if all_ids else "unknown")
    anchor_row = rows.get(anchor, {})
    scope = _message_scope(anchor_row) if anchor_row else (str(packet.get("account_id") or "unknown"), str(packet.get("chat_id") or "unknown"))
    # A K2 packet is already scope-bound.  This check prevents a future adapter
    # from accidentally turning a candidate packet into a cross-chat window.
    if any(_message_scope(rows[mid]) != scope for mid in all_ids if mid in rows):
        raise ValueError("packet contains cross-chat primary/adjacent messages")

    continuity: List[Dict[str, Any]] = []
    qa: List[Dict[str, Any]] = []
    people: List[Dict[str, Any]] = []
    objects: List[Dict[str, Any]] = []
    states: List[Dict[str, Any]] = []
    for left_id in primary_ids:
        for right_id in adjacent_ids:
            if left_id == right_id:
                continue
            candidate = _pair_candidate(left_id, right_id, rows, signals)
            if candidate is None:
                continue
            continuity.append(candidate)
            if signals.get(left_id, SurfaceSignals()).question and signals.get(right_id, SurfaceSignals()).textual:
                qa.append(dict(candidate, candidate_id=_candidate_id("qa", {"left": left_id, "right": right_id}), relation_subtype="question_followup_candidate"))

    for message_id in primary_ids:
        row = rows.get(message_id)
        if row is None:
            continue
        speaker = str(row.get("speaker_id") or "unknown")
        if speaker != "unknown":
            history = _history_ids(message_id, rows, predicate=lambda item, speaker=speaker: str(item.get("speaker_id") or "unknown") == speaker)
            if history:
                people.append(
                    _candidate_history_row(
                        "person_history",
                        message_id,
                        history,
                        rows,
                        ref_key="person_ref_id",
                        ref_value=speaker,
                        reason="same_speaker_metadata",
                        extra={"history_scope": {"account_id": scope[0], "chat_id": scope[1]}},
                    )
                )
        anchor_terms = signals.get(message_id, SurfaceSignals()).terms
        seen_object_terms: Set[str] = set()
        for term in anchor_terms:
            history = [mid for mid in term_index.get((scope, term), ()) if mid != message_id][:3]
            if not history or term in seen_object_terms:
                continue
            seen_object_terms.add(term)
            token_ref = "SPARSE_TOKEN_%s" % stable_hash({"scope": scope, "term": term})[:16]
            objects.append(
                _candidate_history_row(
                    "object_history",
                    message_id,
                    history,
                    rows,
                    ref_key="object_ref_id",
                    ref_value=token_ref,
                    reason="sparse_term_overlap",
                    extra={
                        "object_resolution": "unknown",
                        "surface_term": term,
                        "scope_bound": True,
                    },
                )
            )
            if len(objects) >= DEFAULT_MAX_CANDIDATES:
                break
        state_history = [mid for mid in state_index.get(scope, ()) if mid != message_id][:3]
        if signals.get(message_id, SurfaceSignals()).state and state_history:
            state_ref = "STATE_SIGNAL_%s" % stable_hash({"scope": scope, "message_id": message_id})[:16]
            states.append(
                _candidate_history_row(
                    "state_history",
                    message_id,
                    state_history,
                    rows,
                    ref_key="state_ref_id",
                    ref_value=state_ref,
                    reason="surface_state_signal",
                    extra={"state_resolution": "unknown"},
                )
            )

    # Keep candidate rows deterministic and avoid flooding a packet with the
    # same pair discovered through several weak signals.
    def unique(values: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for value in values:
            candidate_id = str(value.get("candidate_id") or stable_hash(value))
            if candidate_id not in seen:
                result.append(dict(value))
                seen.add(candidate_id)
        return result

    continuity = unique(continuity)
    qa = unique(qa)
    people = unique(people)
    objects = unique(objects)
    states = unique(states)
    all_candidates = unique(continuity + qa + people + objects + states)

    cues = [dict(item) for item in (packet.get("activation_cues") or ()) if isinstance(item, Mapping)]
    cue_payload = {
        "cue_type": "material_replay",
        "message_ids": list(all_ids),
        "fragment_ids": _fragment_ids(packet, all_ids),
        "detail": {"candidate_only": True, "scope": {"account_id": scope[0], "chat_id": scope[1]}},
    }
    cue_payload["replay_key"] = "MATERIAL_REPLAY_%s" % stable_hash(cue_payload)[:20]
    if cue_payload["replay_key"] not in {str(item.get("replay_key")) for item in cues}:
        cues.append(cue_payload)

    reasons: List[str] = [str(item) for item in (packet.get("candidate_reason") or ()) if isinstance(item, str)]
    reasons.extend(("material_candidate_retrieval", "candidate_only", "semantic_decision_pending"))
    if continuity:
        reasons.append("continuity_candidate_available")
    if qa:
        reasons.append("qa_candidate_available")
    if people:
        reasons.append("person_history_candidate_available")
    if objects:
        reasons.append("object_history_candidate_available")
    if states:
        reasons.append("state_history_candidate_available")
    reasons = list(dict.fromkeys(reasons))

    candidate_context = {
        "continuity_candidates": continuity,
        "qa_candidates": qa,
        "person_history": people,
        "object_history": objects,
        "state_history": states,
        "open_threads": [dict(item) for item in (packet.get("open_thread_candidates") or ()) if isinstance(item, Mapping)],
        "activation_cues": cues,
        "candidate_reasons": [
            {
                "candidate_id": str(item.get("candidate_id") or "unknown"),
                "reason_codes": list(item.get("candidate_reason") or item.get("reason_codes") or ()),
                "confidence": str(item.get("confidence") or "low"),
                "evidence_refs": [dict(ref) for ref in (item.get("evidence_refs") or ()) if isinstance(ref, Mapping)],
            }
            for item in all_candidates
        ],
        "surface_signals": {
            "primary": [signals[mid].to_private_dict(mid) for mid in primary_ids if mid in signals],
            "adjacent": [signals[mid].to_private_dict(mid) for mid in adjacent_ids if mid in signals],
        },
        "candidate_only": True,
    }

    # Project shallow, source-anchored selection cues beside the existing K2
    # candidates.  Strong typed rows emitted by ContextPacketBuilder are kept;
    # these additional rows are explicitly candidate-only and therefore do
    # not claim a resolved person/object/state or semantic relationship.
    annotation_map = annotations if annotations is not None else segment_dialogues(tuple(rows.values())).message_annotations
    selection_cues, selection_cue_metrics = _materialize_selection_cues(packet, rows, signals, annotation_map)
    for key, values in selection_cues.items():
        existing = packet.get(key)
        existing_rows = list(existing) if isinstance(existing, (list, tuple, set, frozenset)) else []
        merged = _unique_sidecar_rows([item for item in existing_rows if isinstance(item, Mapping)] + values)
        packet[key] = merged
        candidate_context[key] = merged
    candidate_context["selection_cue_version"] = selection_cue_metrics["selection_cue_version"]
    candidate_context["selection_cue_metrics"] = dict(selection_cue_metrics)

    scale = _packet_scale(packet)
    overlap_group_id = "OVERLAP_%s" % stable_hash({"scope": scope, "primary_ids": primary_ids})[:20]
    boundary = {
        "start": {
            "resolution": "explicit" if anchor != "unknown" else "unknown",
            "message_id": anchor,
            "evidence_ref": "message:%s" % anchor if anchor != "unknown" else "unknown",
        },
        "end": {"resolution": "unknown", "message_id": "unknown", "evidence_ref": "unknown"},
    }
    window = {
        "scale": scale,
        "message_ids": list(all_ids),
        "fragment_ids": _fragment_ids(packet, all_ids),
        "claim_ids": list(packet.get("claim_ids") or ()),
    }
    facts = [dict(item) for item in (packet.get("authoritative_facts") or ()) if isinstance(item, Mapping)]
    fact_projection = {
        "message_metadata": facts,
        "reply_edges": [
            {
                "message_id": item.get("message_id"),
                "reply_to_message_id": item.get("reply_to_message_id"),
                "evidence_ref": "message:%s" % item.get("message_id"),
            }
            for item in facts
            if item.get("reply_to_message_id")
        ],
        "quote_edges": [],
        "fragment_spans": [
            {
                "fragment_id": item.get("fragment_id"),
                "message_id": item.get("message_id"),
                "span": dict(item.get("span") or {}),
                "evidence_ref": "fragment:%s" % item.get("fragment_id"),
            }
            for item in tuple(packet.get("primary_fragments") or ()) + tuple(packet.get("adjacent_context") or ())
            if isinstance(item, Mapping) and item.get("fragment_id")
        ],
        "metadata_authoritative": True,
    }
    fixed = dict(packet.get("fixed_part") or {})
    fixed.update(
        {
            "analysis_run_id": ARTIFACT_VERSION,
            "scope": {"account_id": scope[0], "chat_id": scope[1]},
            "authoritative_facts_contract": fact_projection,
        }
    )
    dynamic = dict(packet.get("dynamic_part") or {})
    dynamic.update(
        {
            "analysis_run_id": ARTIFACT_VERSION,
            "window": window,
            "boundary": boundary,
            "overlap_group_id": overlap_group_id,
            "candidate_context": candidate_context,
            "surface_signals": candidate_context["surface_signals"],
            "status": "open",
            "selection_cue_version": selection_cue_metrics["selection_cue_version"],
            "selection_cue_metrics": dict(selection_cue_metrics),
        }
    )
    for key, values in selection_cues.items():
        dynamic[key] = packet.get(key, values)
    fixed_hash = stable_hash(fixed)
    dynamic_hash = stable_hash(dynamic)
    packet_hash = stable_hash({"packet_version": CONTEXT_PACKET_VERSION, "fixed_hash": fixed_hash, "dynamic_hash": dynamic_hash})
    cache_key = ContextPacketCache.make_key(CONTEXT_PACKET_VERSION, fixed_hash, dynamic_hash, packet_hash)
    packet.update(
        {
            "analysis_run_id": ARTIFACT_VERSION,
            "context_packet_version": str(packet.get("packet_version") or CONTEXT_PACKET_VERSION),
            "scope": {"account_id": scope[0], "chat_id": scope[1]},
            "anchor_fragment_ids": [str(item) for item in (packet.get("anchor_fragment_ids") or packet.get("primary_fragment_ids") or _fragment_ids(packet, primary_ids))],
            "anchor_claim_ids": [str(item) for item in (packet.get("anchor_claim_ids") or packet.get("claim_ids") or ())],
            "boundary": boundary,
            "window": window,
            "overlap_group_id": overlap_group_id,
            "authoritative_facts_contract": fact_projection,
            "candidate_qa_links": qa,
            "candidate_person_history": people,
            "candidate_object_history": objects,
            "candidate_state_history": states,
            "continuity_candidates": continuity,
            "candidate_context": candidate_context,
            "activation_cues": cues,
            "activation_cue_codes": [str(item.get("cue_type") or "") for item in cues],
            "candidate_reason": reasons,
            "uncertainties": list(dict.fromkeys([str(item) for item in (packet.get("uncertainties") or ())] + ["semantic_decision_pending"])),
            "fixed_part": fixed,
            "dynamic_part": dynamic,
            "fixed_hash": fixed_hash,
            "dynamic_hash": dynamic_hash,
            "packet_hash": packet_hash,
            "hash": packet_hash,
            "cache_key": cache_key,
            "status": "open",
            "open_boundary": True,
            "candidate_only": True,
            "provenance": {
                "input_fingerprint": str(packet.get("packet_hash") or ""),
                "material_packet_hash": packet_hash,
                "pipeline_version": CONTEXT_PACKET_PIPELINE_VERSION,
                "ruleset_version": CONTEXT_PACKET_RULESET_VERSION,
                "runner_schema_version": RUNNER_SCHEMA_VERSION,
            },
        }
    )

    surface_count = sum(
        int(bool(signals.get(mid, SurfaceSignals()).greeting))
        + int(bool(signals.get(mid, SurfaceSignals()).question))
        + int(bool(signals.get(mid, SurfaceSignals()).pronoun_or_ellipsis))
        + int(bool(signals.get(mid, SurfaceSignals()).state))
        + int(bool(signals.get(mid, SurfaceSignals()).topic_shift))
        for mid in all_ids
    )
    categories = _classify_categories(
        packet,
        rows,
        signals,
        scope_message_ids,
        objects,
        states,
        continuity,
        qa,
        people,
        all_ids,
        annotations=annotation_map,
    )
    return PreparedPacket(
        value=packet,
        categories=categories,
        primary_ids=primary_ids,
        adjacent_ids=adjacent_ids,
        candidate_count=len(all_candidates),
        evidence_count=len(packet.get("evidence_refs") or ()) + sum(len(item.get("evidence_refs") or ()) for item in all_candidates),
        surface_signal_count=surface_count,
        window_scale=scale,
    )


def _classify_categories(
    packet: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    signals: Mapping[str, SurfaceSignals],
    scope_message_ids: Mapping[Tuple[str, str], Tuple[str, ...]],
    objects: Sequence[Mapping[str, Any]],
    states: Sequence[Mapping[str, Any]],
    continuity: Sequence[Mapping[str, Any]],
    qa: Sequence[Mapping[str, Any]],
    people: Sequence[Mapping[str, Any]],
    all_ids: Sequence[str],
    *,
    annotations: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[str, ...]:
    primary_ids = _packet_primary_ids(packet)
    adjacent_ids = _packet_adjacent_ids(packet)
    anchor = primary_ids[0] if primary_ids else (all_ids[0] if all_ids else "")
    row = rows.get(anchor, {})
    sig = signals.get(anchor, SurfaceSignals())
    categories: List[str] = []
    rest = [mid for mid in all_ids if mid != anchor and mid in rows]
    annotation_map = annotations if annotations is not None else {
        message_id: _message_annotation(message_id, rows[message_id], {})
        for message_id in all_ids
        if message_id in rows
    }
    greeting_positions = [
        index
        for index, message_id in enumerate(all_ids)
        if message_id in rows
        and bool(annotation_map.get(message_id, {}).get("opener"))
        and _is_true_greeting_text(rows[message_id].get("content", rows[message_id].get("text", "")), message_type=rows[message_id].get("message_type", "text"))
    ]
    topic_positions = [
        index
        for index, message_id in enumerate(all_ids)
        if message_id in rows
        and bool(annotation_map.get(message_id, {}).get("topic_bearing"))
        and annotation_map.get(message_id, {}).get("role") == ROLE_SUBSTANTIVE
    ]
    if any(greeting_index < topic_index for greeting_index in greeting_positions for topic_index in topic_positions):
        categories.append("greeting_to_new_topic")
    if not row.get("reply_to_message_id") and sig.textual and (bool(adjacent_ids) or sig.question or sig.pronoun_or_ellipsis):
        categories.append("no_reply_continuation")
    if sig.pronoun_or_ellipsis:
        categories.append("pronoun_or_ellipsis")
    scope = _message_scope(row) if row else (str(packet.get("account_id") or "unknown"), str(packet.get("chat_id") or "unknown"))
    scope_ids = scope_message_ids.get(scope, ())
    speaker = str(row.get("speaker_id") or "unknown")
    if speaker != "unknown" and sum(str(rows[mid].get("speaker_id") or "unknown") == speaker for mid in scope_ids if mid in rows) > 1:
        categories.append("person_history")
    if objects:
        categories.append("object_history")
    if sig.state and any(signals.get(mid, SurfaceSignals()).state for mid in rest) or states:
        categories.append("state_update")
    # A new segment/time gap is not a topic shift.  Only the strict surface
    # pivot cue is eligible here (the typed sidecar applies the same gate).
    if sig.topic_shift:
        categories.append("topic_shift")
    message_type = str(row.get("message_type") or "").casefold()
    role = str(row.get("message_role") or row.get("dialogue_event_role") or "").casefold()
    if message_type in _SILENT_MESSAGE_TYPES or role in {"context", "context_only", "acknowledgement"} or str(row.get("media_state") or "") == "placeholder":
        categories.append("media_or_context_only")
    anchor_offset = _number(row.get("time_offset_seconds"))
    if anchor_offset is not None:
        long_gap = any(
            abs(anchor_offset - float(_number(rows[mid].get("time_offset_seconds")) or anchor_offset)) >= 1800
            for mid in scope_ids
            if mid in rows and _number(rows[mid].get("time_offset_seconds")) is not None and mid != anchor
        )
        if long_gap or bool(packet.get("open_boundary")):
            categories.append("long_gap_open_boundary")
    if len(continuity) + len(qa) + len(people) + len(objects) + len(states) >= 2 or len(rest) >= 2:
        categories.append("candidate_competition")
    return tuple(dict.fromkeys(categories))


def _selection_sort_key(item: PreparedPacket, category: str = "") -> Tuple[Any, ...]:
    scale_order = {"W1": 0, "W0": 1, "W2": 2, "W3": 3, "W4": 4, "unknown": 5}
    primary_sequence = min(
        [float("inf")],
        key=lambda _: 0,
    )
    if item.primary_ids:
        # Sequence is stored in the packet's authoritative facts and remains
        # body-free.  The fallback is lexical packet identity for determinism.
        facts = item.value.get("authoritative_facts") or ()
        by_id = {str(fact.get("message_id")): fact for fact in facts if isinstance(fact, Mapping)}
        values = [_number(by_id[mid].get("sequence_in_chat")) for mid in item.primary_ids if mid in by_id]
        values = [value for value in values if value is not None]
        if values:
            primary_sequence = min(values)
    return (
        0 if category and category in item.categories else 1,
        scale_order.get(item.window_scale, 5),
        item.message_count,
        primary_sequence,
        item.packet_id,
    )


def _select_packets(prepared: Sequence[PreparedPacket], selected_count: int) -> Tuple[Tuple[PreparedPacket, ...], Tuple[str, ...]]:
    bounded = [item for item in prepared if len(item.primary_ids) <= MAX_BOUNDED_PACKET_MESSAGES and item.message_count <= MAX_BOUNDED_PACKET_MESSAGES]
    selected: List[PreparedPacket] = []
    missing: List[str] = []
    by_id = {item.packet_id: item for item in bounded}
    for category in REQUIRED_BUCKETS:
        choices = sorted((item for item in bounded if category in item.categories), key=lambda item, category=category: _selection_sort_key(item, category))
        if not choices:
            missing.append(category)
            continue
        candidate = next((item for item in choices if item.packet_id not in {value.packet_id for value in selected}), None)
        if candidate is not None:
            selected.append(candidate)
    remaining = sorted((item for item in bounded if item.packet_id not in {value.packet_id for value in selected}), key=_selection_sort_key)
    # Add one deterministic spread across scopes/scales before filling by rank.
    covered_scopes: Set[Tuple[str, str]] = {item.scope for item in selected}
    covered_scales: Set[str] = {item.window_scale for item in selected}
    for item in remaining:
        if len(selected) >= selected_count:
            break
        if item.scope not in covered_scopes or item.window_scale not in covered_scales:
            selected.append(item)
            covered_scopes.add(item.scope)
            covered_scales.add(item.window_scale)
    for item in remaining:
        if len(selected) >= selected_count:
            break
        if item.packet_id not in {value.packet_id for value in selected}:
            selected.append(item)
    if len(selected) < selected_count:
        raise ValueError("development packet selection has only %d bounded packets; need %d" % (len(selected), selected_count))
    return tuple(selected[:selected_count]), tuple(missing)


def _layer_coverage(values: Sequence[PreparedPacket], layer: str, all_message_ids: Set[str]) -> Dict[str, Any]:
    observed: Set[str] = set()
    if layer == "primary":
        for item in values:
            observed.update(item.primary_ids)
    elif layer == "adjacent":
        for item in values:
            observed.update(item.adjacent_ids)
    elif layer == "candidate":
        for item in values:
            for key in ("continuity_candidates", "candidate_qa_links", "candidate_person_history", "candidate_object_history", "candidate_state_history"):
                for candidate in item.value.get(key) or ():
                    if isinstance(candidate, Mapping):
                        observed.update(_candidate_message_ids(candidate))
    observed &= all_message_ids
    return {
        "unique_message_count": len(observed),
        "message_count": len(all_message_ids),
        "coverage_rate": len(observed) / len(all_message_ids) if all_message_ids else "N/A",
    }


def _metadata_completeness(rows: Mapping[str, Mapping[str, Any]], selected_ids: Set[str]) -> Dict[str, Any]:
    fields = (
        "message_id",
        "account_id",
        "chat_id",
        "speaker_id",
        "direction",
        "message_type",
        "sequence_in_chat",
        "time_offset_seconds",
        "dialogue_segment_id",
        "split",
    )
    def score(subset: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        expected = len(subset) * len(fields)
        present = sum(1 for row in subset for field in fields if row.get(field) not in (None, ""))
        return {"present": present, "expected": expected, "rate": present / expected if expected else "N/A", "fields": list(fields)}
    all_values = list(rows.values())
    selected_values = [row for mid, row in rows.items() if mid in selected_ids]
    return {"all_messages": score(all_values), "selected_primary_or_context": score(selected_values)}


def _zero_tolerance(values: Sequence[PreparedPacket]) -> Dict[str, int]:
    cross_chat = 0
    strong_time_segment = 0
    for item in values:
        scope = item.scope
        for candidate in item.value.get("continuity_candidates") or ():
            if not isinstance(candidate, Mapping):
                continue
            ids = _candidate_message_ids(candidate)
            scopes = {item.value.get("chat_id")}
            # The source row scope is not copied into body-free candidate rows;
            # K5 verifies this by matching IDs against the packet authority.
            fact_scopes = {
                (str(fact.get("account_id") or "unknown"), str(fact.get("chat_id") or "unknown"))
                for fact in item.value.get("authoritative_facts") or ()
                if isinstance(fact, Mapping) and fact.get("message_id") in ids
            }
            if fact_scopes and any(value != scope for value in fact_scopes):
                cross_chat += 1
            reasons = set(str(value) for value in (candidate.get("candidate_reason") or ()))
            if bool(candidate.get("strong_relation")) and reasons <= {"same_segment_weak", "time_proximity_weak"}:
                strong_time_segment += 1
    return {"cross_chat_violations": cross_chat, "time_same_segment_strong_relation_violations": strong_time_segment}


def _selection_cue_metrics(values: Sequence[PreparedPacket]) -> Dict[str, Any]:
    """Summarise cue candidates and role false-positive guards."""

    names = (
        "message_metadata",
        "topic_transitions",
        "candidate_competition",
        "reply_status",
        "pronoun_person_object_state",
    )
    row_counts = {name: 0 for name in names}
    packet_counts = {name: 0 for name in names}
    candidate_only_violations = 0
    for item in values:
        for name in names:
            rows = [row for row in (item.value.get(name) or ()) if isinstance(row, Mapping)]
            row_counts[name] += len(rows)
            packet_counts[name] += bool(rows)
            candidate_only_violations += sum(
                not bool(row.get("selection_cue"))
                or not bool(row.get("candidate_only"))
                or bool(row.get("strong_relation"))
                for row in rows
            )

    pure_confirmation_packets = 0
    pure_confirmation_semantic_primary_nonzero = 0
    mixed_packets = 0
    mixed_substantive_primary_retained = 0
    for item in values:
        primary = [
            row
            for row in (item.value.get("primary_fragments") or ())
            if isinstance(row, Mapping)
        ]
        if not primary:
            continue
        pure = all(
            is_context_only_text(row.get("content") or row.get("text") or "", message_type=row.get("message_type", "text"))
            or str(row.get("role") or row.get("dialogue_role") or "").casefold() in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER}
            for row in primary
        )
        substantive = any(
            not (
                is_context_only_text(row.get("content") or row.get("text") or "", message_type=row.get("message_type", "text"))
                or str(row.get("role") or row.get("dialogue_role") or "").casefold() in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER}
            )
            for row in primary
        )
        if pure:
            pure_confirmation_packets += 1
            # A pure context/confirmation packet must never project a
            # substantive semantic primary.  K29 keeps it recoverable only.
            if substantive:
                pure_confirmation_semantic_primary_nonzero += 1
        if pure is False and any(
            is_context_only_text(row.get("content") or row.get("text") or "", message_type=row.get("message_type", "text"))
            or str(row.get("role") or row.get("dialogue_role") or "").casefold() in {ROLE_CONTEXT_ONLY, ROLE_CONVERSATION_OPENER}
            for row in primary
        ):
            mixed_packets += 1
            if substantive:
                mixed_substantive_primary_retained += 1
    return {
        "selection_cue_version": _SELECTION_CUE_VERSION,
        "candidate_row_counts": row_counts,
        "candidate_packet_counts": packet_counts,
        "candidate_only_contract_violations": candidate_only_violations,
        "false_positive_guards": {
            "pure_confirmation_candidate_count": pure_confirmation_packets,
            "pure_confirmation_semantic_primary_nonzero_count": pure_confirmation_semantic_primary_nonzero,
            "pure_confirmation_semantic_primary_zero_rate": 1.0 if pure_confirmation_packets == 0 else 1.0 - (pure_confirmation_semantic_primary_nonzero / pure_confirmation_packets),
            "mixed_content_candidate_count": mixed_packets,
            "mixed_content_substantive_primary_retained_count": mixed_substantive_primary_retained,
            "mixed_content_substantive_primary_retention_rate": 1.0 if mixed_packets == 0 else mixed_substantive_primary_retained / mixed_packets,
        },
    }


def _prepare_packets(
    messages: Sequence[Mapping[str, Any]],
    *,
    window_size: int,
    max_candidates: int,
    packet_version: str,
) -> Tuple[Tuple[PreparedPacket, ...], Mapping[str, Any]]:
    public_messages = _public_pipeline_messages(messages)
    cache = ContextPacketCache()
    started = time.perf_counter()
    first = build_context_packets(
        public_messages,
        window_size=window_size,
        max_candidates=max_candidates,
        max_packets=4096,
        cache=cache,
        packet_version=packet_version,
    )
    # A second in-memory build proves replay identity and exercises the stable
    # K2 packet cache.  It never calls a provider and does not write an artifact.
    replay = build_context_packets(
        public_messages,
        window_size=window_size,
        max_candidates=max_candidates,
        max_packets=4096,
        cache=cache,
        packet_version=packet_version,
    )
    elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    rows = {str(item.get("message_id")): dict(item) for item in messages if item.get("message_id")}
    signals = {message_id: SurfaceSignals.for_row(row) for message_id, row in rows.items()}
    # Reuse the conservative dialogue segment annotations for message-level
    # role/topic evidence.  This is a local deterministic pass over the same
    # public/redacted messages and does not invoke a provider.
    annotations = segment_dialogues(public_messages).message_annotations
    scope_values: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    term_index_mut: Dict[Tuple[Tuple[str, str], str], List[str]] = defaultdict(list)
    state_index_mut: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for message_id, row in rows.items():
        scope = _message_scope(row)
        scope_values[scope].append(message_id)
        for term in signals[message_id].terms:
            term_index_mut[(scope, term)].append(message_id)
        if signals[message_id].state:
            state_index_mut[scope].append(message_id)
    for values in scope_values.values():
        values.sort(key=lambda mid: (_sequence(rows[mid]), mid))
    for values in term_index_mut.values():
        values.sort(key=lambda mid: (_sequence(rows[mid]), mid))
    for values in state_index_mut.values():
        values.sort(key=lambda mid: (_sequence(rows[mid]), mid))
    scope_index = {key: tuple(value) for key, value in scope_values.items()}
    term_index = {key: tuple(value) for key, value in term_index_mut.items()}
    state_index = {key: tuple(value) for key, value in state_index_mut.items()}
    prepared = tuple(
        _build_enriched_packet(item.to_dict(), rows, signals, scope_index, term_index, state_index, annotations)
        for item in first.packets
    )
    replay_hashes = tuple(str(item.packet_hash) for item in replay.packets)
    base_hashes = tuple(str(item.packet_hash) for item in first.packets)
    if base_hashes != replay_hashes:
        raise ValueError("K2 packet replay changed packet hashes")
    return prepared, {
        "builder_input_hash": first.input_hash,
        "builder_cache_hits_first": first.cache_hits,
        "builder_cache_misses_first": first.cache_misses,
        "builder_cache_hits_replay": replay.cache_hits,
        "builder_cache_misses_replay": replay.cache_misses,
        "builder_packet_count": len(first.packets),
        "builder_bundle_count": len(first.bundles),
        "builder_fragment_count": len(first.fragments),
        "builder_claim_count": len(first.dialogue_result.claims) if first.dialogue_result is not None else 0,
        "builder_relation_count": len(first.dialogue_result.relations) if first.dialogue_result is not None else 0,
        "selection_cue_version": _SELECTION_CUE_VERSION,
        "selection_cue_packet_count": sum(
            any(bool(item.value.get(key)) for key in ("message_metadata", "topic_transitions", "candidate_competition", "reply_status", "pronoun_person_object_state"))
            for item in prepared
        ),
        "selection_cue_row_counts": {
            key: sum(len(item.value.get(key) or ()) for item in prepared)
            for key in ("message_metadata", "topic_transitions", "candidate_competition", "reply_status", "pronoun_person_object_state")
        },
        "builder_elapsed_ms": elapsed_ms,
        "cache_persistent": False,
        "cache_key_schema": "context_packet_cache_v1",
    }


def run_development_context_packet_material(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    selected_packet_count: int = SELECTION_LIMIT,
    window_size: int = DEFAULT_WINDOW_SIZE,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    packet_version: str = CONTEXT_PACKET_VERSION,
    strict_p014_manifest: bool = True,
) -> K5MaterialRunResult:
    """Build a private K5 material artifact from one development input.

    No provider is constructed or called.  ``selected_packet_count`` defaults
    to 20 and must remain inside the 16–24 development pilot range.
    """

    if int(selected_packet_count) < 1 or int(selected_packet_count) > MAX_SELECTION:
        raise ValueError("selected_packet_count must be between 1 and %d" % MAX_SELECTION)
    if int(window_size) < 1 or int(max_candidates) < 1:
        raise ValueError("window_size and max_candidates must be positive")
    input_root = _guard_development_directory(input_directory)
    output_root = _guard_output_directory(output_directory, input_root)
    messages, raw = _read_messages(input_root)
    p014_manifest, p014_manifest_sha256 = _read_p014_manifest(input_root, raw, strict=strict_p014_manifest)
    input_sha256 = _sha256_bytes(raw)
    prepared, builder_metrics = _prepare_packets(
        messages,
        window_size=int(window_size),
        max_candidates=int(max_candidates),
        packet_version=str(packet_version),
    )
    selected, missing_buckets = _select_packets(prepared, int(selected_packet_count))
    selected_ids = {mid for item in selected for mid in item.message_ids}
    all_message_ids = {str(item.get("message_id")) for item in messages if item.get("message_id")}
    errors: List[Dict[str, Any]] = []
    if len(prepared) != builder_metrics["builder_packet_count"]:
        errors.append({"code": "packet_count_mismatch", "stage": "material_validation", "severity": "high"})
    for item in prepared:
        if not item.value.get("activation_cues"):
            errors.append({"code": "activation_cue_missing", "stage": "material_validation", "packet_id": item.packet_id, "severity": "high"})
        packet_scope = item.scope
        for candidate_key in ("continuity_candidates", "candidate_qa_links", "candidate_person_history", "candidate_object_history", "candidate_state_history"):
            for candidate in item.value.get(candidate_key) or ():
                if not isinstance(candidate, Mapping):
                    continue
                ids = _candidate_message_ids(candidate)
                if any(mid not in all_message_ids for mid in ids):
                    errors.append({"code": "candidate_message_out_of_input", "stage": "material_validation", "packet_id": item.packet_id, "severity": "high"})
                    continue
                # Candidate rows are generated from a scope-local index.  The
                # check below is the actual hard boundary, not a reason hint.
                source_rows = [next((row for row in messages if str(row.get("message_id")) == mid), None) for mid in ids]
                if any(row is not None and _message_scope(row) != packet_scope for row in source_rows):
                    errors.append({"code": "cross_chat_candidate", "stage": "material_validation", "packet_id": item.packet_id, "severity": "high"})
                if bool(candidate.get("strong_relation")) and set(str(value) for value in (candidate.get("candidate_reason") or ())) <= {"same_segment_weak", "time_proximity_weak"}:
                    errors.append({"code": "time_or_segment_strong_relation", "stage": "material_validation", "packet_id": item.packet_id, "severity": "high"})
    zero = _zero_tolerance(prepared)
    if zero["cross_chat_violations"] or zero["time_same_segment_strong_relation_violations"]:
        errors.append({"code": "zero_tolerance_violation", "stage": "material_validation", "severity": "high", "counts": zero})

    packet_rows = [dict(item.value) for item in prepared]
    selection_rows: List[Dict[str, Any]] = []
    selected_by_id = {item.packet_id: rank for rank, item in enumerate(selected, 1)}
    for item in prepared:
        rank = selected_by_id.get(item.packet_id)
        selection_rows.append(
            {
                "packet_id": item.packet_id,
                "selected": rank is not None,
                "selection_rank": rank,
                "material_buckets": list(item.categories),
                "selection_reason": "stratified_bucket_coverage" if rank is not None else "not_selected_replayable",
                "account_id": item.scope[0],
                "chat_id": item.scope[1],
                "window_scale": item.window_scale,
                "primary_message_count": len(item.primary_ids),
                "adjacent_message_count": len(item.adjacent_ids),
                "message_count": item.message_count,
                "candidate_count": item.candidate_count,
                "evidence_count": item.evidence_count,
                "activation_cue_count": len(item.value.get("activation_cues") or ()),
                "packet_hash": item.packet_hash,
                "fixed_hash": str(item.value.get("fixed_hash") or ""),
                "dynamic_hash": str(item.value.get("dynamic_hash") or ""),
                "candidate_only": True,
            }
        )
    audit_rows: List[Dict[str, Any]] = []
    for item in selected:
        target_ref = stable_hash({"artifact": ARTIFACT_VERSION, "packet_hash": item.packet_hash})[:24]
        audit_rows.append(
            {
                "target_ref": target_ref,
                "packet_id": item.packet_id,
                "bucket": list(item.categories),
                "scope": {"account_id": item.scope[0], "chat_id": item.scope[1]},
                "window_scale": item.window_scale,
                "message_count": item.message_count,
                "primary_message_count": len(item.primary_ids),
                "adjacent_message_count": len(item.adjacent_ids),
                "candidate_count": item.candidate_count,
                "evidence_count": item.evidence_count,
                "activation_cue_count": len(item.value.get("activation_cues") or ()),
                "packet_hash": item.packet_hash,
                "material_status": "candidate_only_pending_semantic_review",
            }
        )

    metadata_metrics = _metadata_completeness(
        {str(item.get("message_id")): item for item in messages if item.get("message_id")},
        selected_ids,
    )
    selection_cue_metrics = _selection_cue_metrics(prepared)
    selected_packet_values = list(selected)
    layers = {
        "all": {layer: _layer_coverage(prepared, layer, all_message_ids) for layer in ("primary", "adjacent", "candidate")},
        "selected": {layer: _layer_coverage(selected_packet_values, layer, all_message_ids) for layer in ("primary", "adjacent", "candidate")},
    }
    eligible_message_ids = {
        str(item.get("message_id"))
        for item in messages
        if str(item.get("message_type") or "").casefold() not in _SILENT_MESSAGE_TYPES and _message_text(item).strip()
    }
    evidence_message_ids: Set[str] = set()
    for item in prepared:
        for ref in item.value.get("evidence_refs") or ():
            if isinstance(ref, Mapping) and ref.get("message_id") in all_message_ids:
                evidence_message_ids.add(str(ref.get("message_id")))
    material_metrics = {
        "message_count": len(messages),
        "packet_count": len(prepared),
        "selected_packet_count": len(selected),
        "selected_packet_limit": int(selected_packet_count),
        "layers": layers,
        "authoritative_metadata_completeness": metadata_metrics,
        "context_evidence_recall_proxy": {
            "gold_context_recall": "N/A",
            "gold_loaded": False,
            "evidence_source_message_count": len(evidence_message_ids),
            "evidence_eligible_message_count": len(eligible_message_ids),
            "evidence_source_coverage_rate": len(evidence_message_ids & eligible_message_ids) / len(eligible_message_ids) if eligible_message_ids else "N/A",
            "all_message_primary_coverage_rate": layers["all"]["primary"]["coverage_rate"],
            "definition": "proxy only; source/evidence and candidate placement, not gold semantic recall",
        },
        "distractor_proxy": {
            "definition": "candidate rows with only weak metadata/time/segment signals divided by candidate rows",
            "candidate_count": sum(item.candidate_count for item in prepared),
            "weak_only_candidate_count": sum(
                1
                for item in prepared
                for candidate in item.value.get("continuity_candidates") or ()
                if isinstance(candidate, Mapping)
                and set(str(value) for value in (candidate.get("candidate_reason") or ())) <= {"same_segment_weak", "time_proximity_weak"}
            ),
            "rate": 0.0,
            "gold_distractor_rate": "N/A",
        },
        "zero_tolerance": zero,
        "selection_cues": selection_cue_metrics,
        "activation_cues": {
            "packet_count": len(prepared),
            "covered_packet_count": sum(bool(item.value.get("activation_cues")) for item in prepared),
            "coverage_rate": sum(bool(item.value.get("activation_cues")) for item in prepared) / len(prepared) if prepared else "N/A",
            "selected_covered_packet_count": sum(bool(item.value.get("activation_cues")) for item in selected),
            "selected_coverage_rate": sum(bool(item.value.get("activation_cues")) for item in selected) / len(selected) if selected else "N/A",
        },
        "required_bucket_coverage": {
            category: sum(category in item.categories for item in selected)
            for category in REQUIRED_BUCKETS
        },
        "missing_required_buckets": list(missing_buckets),
        "window_scale_counts": dict(sorted(Counter(item.window_scale for item in prepared).items())),
        "selected_window_scale_counts": dict(sorted(Counter(item.window_scale for item in selected).items())),
        "channel_counts": dict(sorted(Counter(str((item.value.get("primary_fragments") or [{}])[0].get("channel") if item.value.get("primary_fragments") else "unknown") for item in prepared).items())),
        "selected_channel_counts": dict(sorted(Counter(str((item.value.get("primary_fragments") or [{}])[0].get("channel") if item.value.get("primary_fragments") else "unknown") for item in selected).items())),
    }
    packet_sizes: List[int] = []
    selected_packet_sizes: List[int] = []
    fixed_sizes: List[int] = []
    dynamic_sizes: List[int] = []
    fixed_hashes: Set[str] = set()
    dynamic_hashes: Set[str] = set()
    for item in prepared:
        size = len(_canonical_json(item.value))
        packet_sizes.append(size)
        fixed_sizes.append(len(_canonical_json(item.value.get("fixed_part") or {})))
        dynamic_sizes.append(len(_canonical_json(item.value.get("dynamic_part") or {})))
        fixed_hashes.add(str(item.value.get("fixed_hash") or ""))
        dynamic_hashes.add(str(item.value.get("dynamic_hash") or ""))
    for item in selected:
        selected_packet_sizes.append(len(_canonical_json(item.value)))
    cost = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "provider_called": False,
        "provider_calls": 0,
        "provider_tokens": {"input": 0, "output": 0},
        "local_registry_messages": len(messages),
        "local_packet_count": len(prepared),
        "selected_packet_count": len(selected),
        "estimated_packet_chars": {"all_total": sum(packet_sizes), "selected_total": sum(selected_packet_sizes), "all_max": max(packet_sizes) if packet_sizes else 0, "selected_max": max(selected_packet_sizes) if selected_packet_sizes else 0},
        "estimated_packet_tokens": {"all_total": math.ceil(sum(packet_sizes) / 4), "selected_total": math.ceil(sum(selected_packet_sizes) / 4), "all_max": math.ceil(max(packet_sizes) / 4) if packet_sizes else 0, "selected_max": math.ceil(max(selected_packet_sizes) / 4) if selected_packet_sizes else 0, "estimator": "chars_div_4_proxy"},
        "fixed_part_chars": {"total": sum(fixed_sizes), "max": max(fixed_sizes) if fixed_sizes else 0},
        "dynamic_part_chars": {"total": sum(dynamic_sizes), "max": max(dynamic_sizes) if dynamic_sizes else 0},
        "cache": {
            "cache_schema": builder_metrics["cache_key_schema"],
            "persistent": builder_metrics["cache_persistent"],
            "first_run_hits": builder_metrics["builder_cache_hits_first"],
            "first_run_misses": builder_metrics["builder_cache_misses_first"],
            "replay_hits": builder_metrics["builder_cache_hits_replay"],
            "replay_misses": builder_metrics["builder_cache_misses_replay"],
            "fixed_hash_count": len(fixed_hashes),
            "dynamic_hash_count": len(dynamic_hashes),
        },
        "builder": builder_metrics,
        "latency_ms": builder_metrics["builder_elapsed_ms"],
        "cost": 0,
        "scoring": "N/A",
    }
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "body_free_summary_outputs": True,
        "packets_private_may_contain_body": True,
        "message_count": len(messages),
        "packet_count": len(prepared),
        "selected_packet_count": len(selected),
        "selected_packet_limit": int(selected_packet_count),
        "input_sha256": input_sha256,
        "p014_manifest_sha256": p014_manifest_sha256,
        "p014_split_version": p014_manifest.get("split_version", "unknown"),
        "builder_input_hash": builder_metrics["builder_input_hash"],
        "selection_cues": selection_cue_metrics,
        "material_metrics": material_metrics,
        "cost": cost,
        "error_count": len(errors),
        "error_codes": dict(sorted(Counter(str(item.get("code") or "unknown") for item in errors).items())),
        "accuracy": "N/A",
        "scoring": "N/A",
    }
    code_sha256 = _runner_code_sha256()
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "packet_version": packet_version,
        "context_packet_pipeline_version": CONTEXT_PACKET_PIPELINE_VERSION,
        "context_packet_ruleset_version": CONTEXT_PACKET_RULESET_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "input_directory_name": input_root.name,
        "input_filename": INPUT_FILENAME,
        "input_sha256": input_sha256,
        "p014_manifest_sha256": p014_manifest_sha256,
        "p014_split_version": p014_manifest.get("split_version", "unknown"),
        "code_sha256": code_sha256,
        "analysis_run_id": ARTIFACT_VERSION,
        "window_size": int(window_size),
        "max_candidates": int(max_candidates),
        "selection_cue_version": _SELECTION_CUE_VERSION,
        "selection": {
            "selected_packet_count": len(selected),
            "selected_packet_limit": int(selected_packet_count),
            "required_buckets": list(REQUIRED_BUCKETS),
            "missing_required_buckets": list(missing_buckets),
            "bounded_packet_message_limit": MAX_BOUNDED_PACKET_MESSAGES,
        },
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_called": False,
        "provider_calls": 0,
        "body_free_summary_outputs": True,
        "packets_private_may_contain_body": True,
        "audit_queue_body_free": True,
        "accuracy": "N/A",
        "scoring": "N/A",
        "status": "complete_with_diagnostics" if errors or missing_buckets else "complete",
        "output_directory_name": output_root.name,
        "output_files": dict(OUTPUT_FILENAMES),
        "body_free_files": [OUTPUT_FILENAMES[key] for key in ("manifest", "aggregate", "cost", "errors", "selection_map", "audit_queue")],
        "private_body_files": [OUTPUT_FILENAMES["packets"]],
    }
    _assert_body_free(aggregate, label="aggregate")
    _assert_body_free(cost, label="cost")
    _assert_body_free(manifest, label="manifest")
    _assert_body_free(errors, label="errors")
    _assert_body_free(selection_rows, label="selection_map")
    _assert_body_free(audit_rows, label="audit_queue")

    output_root.mkdir(parents=True, exist_ok=False)
    _write_jsonl(output_root / OUTPUT_FILENAMES["packets"], packet_rows)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection_map"], selection_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["audit_queue"], audit_rows)
    manifest["artifact_hashes"] = {
        filename: _sha256_file(output_root / filename)
        for filename in OUTPUT_FILENAMES.values()
        if filename != OUTPUT_FILENAMES["manifest"]
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    artifact_paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return K5MaterialRunResult(
        input_directory=str(input_root),
        output_directory=str(output_root),
        input_sha256=input_sha256,
        packet_count=len(prepared),
        selected_packet_count=len(selected),
        manifest_path=artifact_paths["manifest"],
        artifact_paths=artifact_paths,
        manifest=manifest,
        aggregate=aggregate,
    )


# Short aliases make the K5 boundary discoverable without introducing a second
# implementation or encouraging callers to bypass the development guard.
run_k5_development_packet_material = run_development_context_packet_material
run_development_context_packet_pilot = run_development_context_packet_material


__all__ = [
    "ARTIFACT_VERSION",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_WINDOW_SIZE",
    "K5MaterialRunResult",
    "MAX_SELECTION",
    "MIN_SELECTION",
    "OUTPUT_FILENAMES",
    "REQUIRED_BUCKETS",
    "RUNNER_SCHEMA_VERSION",
    "SurfaceSignals",
    "run_development_context_packet_material",
    "run_development_context_packet_pilot",
    "run_k5_development_packet_material",
]
