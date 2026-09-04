"""Provider-assisted, evidence-bound content-line extraction for review cards.

Conversation reconstruction remains provider-free.  This optional layer is
invoked explicitly with source rows and a configured/injected semantic model.
It has no topic vocabulary: titles, summaries, evidence grouping and initial
importance come from the model, while all IDs and promotion constraints are
validated locally.
"""

from __future__ import annotations

from copy import deepcopy
import inspect
import re
from typing import Any, Dict, List, Mapping, MutableMapping, Protocol, Sequence

from .context_reconstruction import _normalise_messages, _stable_hash


SCHEMA_VERSION = "semantic_content_lines_v2"
PROMPT_VERSION = "semantic_content_lines_prompt_v3"
_IMPORTANCE = {"low", "medium", "high"}
_CLAIM_TYPES = {"chat_report", "reported_experience", "opinion", "hypothesis", "question", "mixed"}
_EXTERNAL_VERIFICATION = {"not_applicable", "recommended"}
_EDITORIAL_TEMPLATE_RE = re.compile(
    r"(?:不是.{0,60}而是|不仅.{0,60}(?:而且|还)|一方面.{0,90}另一方面|首先.{0,90}其次)",
    re.S,
)
_UNRESOLVED_OBJECT_RE = re.compile(r"(?:用|改|删|卸载|处理)?(?:这个|那个|这些|那些|它|它们|这件事|那件事|这东西|那东西|this|that|it)(?:了|吧|吗|呢|。|！|？|\s)*$", re.I)
_ELLIPTICAL_ACTION_RE = re.compile(r"^(?:现在|已经|早就|还是|其实|就|也|没必要|不用|不必|可以|不可以|不能|别)?[^A-Za-z0-9_@#]{0,8}(?:用|卸载|删除|删掉|改|做|弄|买|开|关|换|试|处理)(?:了|吧|吗|呢)?[。！？，、\s]*$", re.I)


class ContentLineProtocolError(ValueError):
    pass


class ContentLineModel(Protocol):
    model_id: str
    source: str

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> Any: ...


SYSTEM_PROMPT = (
    "你只负责从一张聊天交流卡中发现内容线，不预设任何领域或关键词。"
    "返回严格 JSON：{content_lines:[{title,summary,evidence_bindings,primary_message_ids,context_message_ids,"
    "importance,key_topic_candidate,importance_reasons,claim_type,external_verification,uncertainties}]}。"
    "一张卡可以有零条、一条或多条内容线；不同对象或问题必须拆开，不能让末尾一句覆盖前面的主线。"
    "若摘要需要用‘随后转向、另外谈到、话题转到’才能连接两部分，通常说明应拆为两条内容线；仅有时间相邻不能合并。"
    "但同一连续讨论中，如果多个工具、方案或观点都在回答同一个上位问题（例如选择、取舍、排障或推进方式），应合并成一条内容线，避免按名词机械拆碎。"
    "primary_message_ids 只放直接支撑标题和摘要的消息；指代不清、仅帮助理解或不可用媒体放 context_message_ids。"
    "摘要中的每个事实性判断都必须由 primary_message_ids 直接支持；context_message_ids 不能成为摘要某项结论的唯一依据。"
    "evidence_bindings 必须逐项列出摘要中的独立判断及其直接支撑消息，格式为 [{statement,message_ids}]；"
    "每个 primary_message_id 至少绑定一项判断，不能用整段消息列表笼统充当证据。"
    "输入中 semantic_evidence_eligible=false 的消息必须放在 context_message_ids，不能放 primary_message_ids。"
    "不可用媒体不得作为语义证据，不得猜测其内容。"
    "importance 只能是 low、medium、high。识别到话题不等于重点话题：普通短讨论通常为 low；"
    "只有存在持续讨论、明确决定、行动、结果或显著影响时才可把 key_topic_candidate 设为 true。"
    "写作采用专业简报口吻：标题直接概括事项，避免以‘讨论、用户、参与者’等空泛词开头；"
    "摘要优先写清具体进展、判断、分歧、行动和未决问题，必要时再交代是谁陈述，不能按消息顺序逐条复述。"
    "不要使用‘不是……而是……’‘不仅……还……’‘一方面……另一方面……’‘首先……其次……’等机械框架，"
    "不要连续使用‘围绕、讨论、提到、同时、此外’组织排比句。"
    "claim_type 只能是 chat_report、reported_experience、opinion、hypothesis、question、mixed。"
    "本阶段不能联网，external_verification 只能是 not_applicable 或 recommended；公开规则、产品状态、价格、性能、"
    "因果关系及梗的来源若仅有聊天陈述，应标为 recommended，绝不能写成已经核验。"
    "importance_reasons 和 uncertainties 必须是字符串数组，即使只有一项也不能返回字符串或对象。"
    "所有消息 ID 必须逐字复制输入；不要输出额外字段或解释。"
)


def _strings(value: Any, *, field: str, allow_empty: bool = True) -> List[str]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise ContentLineProtocolError(f"{field}_type")
    if not allow_empty and not value:
        raise ContentLineProtocolError(f"{field}_empty")
    if len(value) != len(set(value)):
        raise ContentLineProtocolError(f"{field}_duplicate")
    return list(value)


def validate_content_lines(
    payload: Any,
    *,
    allowed_message_ids: Sequence[str],
    unavailable_media_ids: Sequence[str] = (),
    ineligible_primary_ids: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    if type(payload) is not dict or set(payload) != {"content_lines"}:
        raise ContentLineProtocolError("root_shape")
    rows = payload["content_lines"]
    if type(rows) is not list:
        raise ContentLineProtocolError("content_lines_type")
    allowed = set(allowed_message_ids)
    unavailable = set(unavailable_media_ids)
    ineligible = set(ineligible_primary_ids) | unavailable
    expected = {
        "title", "summary", "evidence_bindings", "primary_message_ids", "context_message_ids",
        "importance", "key_topic_candidate", "importance_reasons", "claim_type",
        "external_verification", "uncertainties",
    }
    validated: List[Dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if type(raw) is not dict or set(raw) != expected:
            raise ContentLineProtocolError(f"line_{index}_shape")
        title = raw["title"]
        summary = raw["summary"]
        if type(title) is not str or not title.strip() or len(title) > 80:
            raise ContentLineProtocolError(f"line_{index}_title")
        if type(summary) is not str or not summary.strip() or len(summary) > 500:
            raise ContentLineProtocolError(f"line_{index}_summary")
        if _EDITORIAL_TEMPLATE_RE.search(f"{title}\n{summary}"):
            raise ContentLineProtocolError(f"line_{index}_mechanical_editorial_template")
        primary = _strings(raw["primary_message_ids"], field=f"line_{index}_primary", allow_empty=False)
        context = _strings(raw["context_message_ids"], field=f"line_{index}_context")
        if not set(primary + context) <= allowed:
            raise ContentLineProtocolError(f"line_{index}_out_of_scope")
        if set(primary) & set(context):
            raise ContentLineProtocolError(f"line_{index}_primary_context_overlap")
        if set(primary) & ineligible:
            raise ContentLineProtocolError(f"line_{index}_ineligible_message_as_evidence")
        bindings = raw["evidence_bindings"]
        if type(bindings) is not list or not bindings:
            raise ContentLineProtocolError(f"line_{index}_evidence_bindings_type")
        validated_bindings: List[Dict[str, Any]] = []
        bound_ids: set[str] = set()
        for binding_index, binding in enumerate(bindings):
            if type(binding) is not dict or set(binding) != {"statement", "message_ids"}:
                raise ContentLineProtocolError(f"line_{index}_binding_{binding_index}_shape")
            statement = binding["statement"]
            if type(statement) is not str or not statement.strip() or len(statement) > 240:
                raise ContentLineProtocolError(f"line_{index}_binding_{binding_index}_statement")
            binding_ids = _strings(
                binding["message_ids"],
                field=f"line_{index}_binding_{binding_index}_message_ids",
                allow_empty=False,
            )
            if not set(binding_ids) <= set(primary):
                raise ContentLineProtocolError(f"line_{index}_binding_{binding_index}_outside_primary")
            bound_ids.update(binding_ids)
            validated_bindings.append({"statement": statement.strip(), "message_ids": binding_ids})
        if bound_ids != set(primary):
            raise ContentLineProtocolError(f"line_{index}_unbound_primary")
        importance = raw["importance"]
        if importance not in _IMPORTANCE:
            raise ContentLineProtocolError(f"line_{index}_importance")
        if type(raw["key_topic_candidate"]) is not bool:
            raise ContentLineProtocolError(f"line_{index}_key_topic_type")
        reasons = _strings(raw["importance_reasons"], field=f"line_{index}_importance_reasons")
        claim_type = raw["claim_type"]
        if claim_type not in _CLAIM_TYPES:
            raise ContentLineProtocolError(f"line_{index}_claim_type")
        external_verification = raw["external_verification"]
        if external_verification not in _EXTERNAL_VERIFICATION:
            raise ContentLineProtocolError(f"line_{index}_external_verification")
        uncertainties = _strings(raw["uncertainties"], field=f"line_{index}_uncertainties")
        # A low-importance line can exist, but can never be promoted locally.
        key_topic = bool(raw["key_topic_candidate"] and importance in {"medium", "high"})
        validated.append({
            "content_line_ref": f"content-line-{_stable_hash((title.strip(), tuple(primary), SCHEMA_VERSION), length=18)}",
            "category": "model_discovered",
            "title": title.strip(),
            "summary_candidate": summary.strip(),
            "topic_detected": True,
            "support_message_ids": primary,
            "context_message_ids": context,
            "evidence_bindings": validated_bindings,
            "evidence_message_count": len(primary),
            "importance_candidate": importance,
            "importance_status": "model_candidate_locally_validated",
            "key_topic_candidate": key_topic,
            "key_topic_status": "eligible_for_review" if key_topic else "not_promoted",
            "key_topic_reason_codes": reasons,
            "claim_type": claim_type,
            "claim_status": "chat_evidence_only",
            "external_verification": external_verification,
            "external_verification_status": "not_performed",
            "candidate_only": True,
            "semantic_status": "candidate_only",
            "uncertainties": uncertainties,
            "extractor": "semantic_model",
        })
    return validated


def enrich_review_content_lines(
    result: Mapping[str, Any],
    source_messages: Sequence[Mapping[str, Any]],
    model: ContentLineModel,
    *,
    max_cards: int | None = None,
    max_output_tokens: int = 1200,
) -> Dict[str, Any]:
    """Return a copy enriched by explicit provider calls, one per review card."""
    enriched = deepcopy(dict(result))
    normalized, private = _normalise_messages(source_messages)
    source_by_ref = {str(row.get("message_ref")): row for row in normalized}
    ledger_by_ref = {str(row.get("message_ref")): row for row in enriched.get("messages") or () if isinstance(row, Mapping)}
    id_to_ref = {
        str(row.get("source_message_id") or row.get("message_id")): ref
        for ref, row in ledger_by_ref.items()
    }
    units = list((enriched.get("review") or {}).get("sample_units") or ())
    if max_cards is not None and max_cards < 1:
        raise ValueError("max_cards_must_be_positive_or_none")
    target_units = units if max_cards is None else units[:max_cards]
    calls = 0
    completed = 0
    failures: List[Dict[str, Any]] = []
    for unit in target_units:
        if not isinstance(unit, MutableMapping):
            continue
        refs = [str(ref) for ref in unit.get("display_message_refs") or unit.get("core_message_refs") or () if str(ref) in source_by_ref]
        message_rows: List[Dict[str, Any]] = []
        unavailable_ids: List[str] = []
        ineligible_primary_ids: List[str] = []
        for ref in refs:
            source = source_by_ref[ref]
            ledger = ledger_by_ref.get(ref, {})
            source_id = str(ledger.get("source_message_id") or ledger.get("message_id") or ref)
            media = ledger.get("media") if isinstance(ledger.get("media"), Mapping) else {}
            if media.get("status") == "unavailable":
                unavailable_ids.append(source_id)
            text = str((private.get(ref) or {}).get("body") or "")
            reply_target = source.get("reply_to_source_message_id") or source.get("reply_to_message_id")
            unresolved_reference = bool(
                not reply_target
                and (
                    _UNRESOLVED_OBJECT_RE.search(text)
                    or (len(text.strip()) <= 9 and _ELLIPTICAL_ACTION_RE.search(text.strip()))
                )
            )
            semantic_evidence_eligible = media.get("semantic_evidence_eligible", True) is not False and not unresolved_reference
            if not semantic_evidence_eligible:
                ineligible_primary_ids.append(source_id)
            message_rows.append({
                "message_id": source_id,
                "timestamp": source.get("timestamp"),
                "speaker_ref": source.get("participant_ref"),
                "message_type": source.get("message_type"),
                "text": text,
                "media_status": media.get("status", "none"),
                "semantic_evidence_eligible": semantic_evidence_eligible,
                "evidence_ineligibility_reason": "unresolved_reference" if unresolved_reference else ("unavailable_media" if media.get("semantic_evidence_eligible", True) is False else "none"),
                "scope": "core" if ref in set(unit.get("core_message_refs") or ()) else "review_context",
            })
        packet = {"schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION, "messages": message_rows}
        lines: List[Dict[str, Any]] | None = None
        error_code = ""
        previous_payload: Any = None
        for attempt in range(3):
            request_packet = packet if attempt == 0 else {
                **packet,
                "format_repair": {
                    "validation_error": error_code,
                    "previous_output": previous_payload,
                    "instruction": "只修复结构和类型，不改变语义、消息归属或重要性判断。",
                },
            }
            complete_parameters = inspect.signature(model.complete).parameters
            call_kwargs: Dict[str, Any] = {"max_output_tokens": max_output_tokens}
            if "extra_body" in complete_parameters:
                call_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            try:
                response = model.complete("CONTENT_LINES", SYSTEM_PROMPT, request_packet, **call_kwargs)
            except Exception as exc:
                calls += 1
                error_code = str(getattr(exc, "code", "") or exc.__class__.__name__)
                previous_payload = None
                continue
            calls += 1
            payload = response.payload if hasattr(response, "payload") else response
            try:
                lines = validate_content_lines(
                    payload,
                    allowed_message_ids=[row["message_id"] for row in message_rows],
                    unavailable_media_ids=unavailable_ids,
                    ineligible_primary_ids=ineligible_primary_ids,
                )
                break
            except ContentLineProtocolError as exc:
                error_code = str(exc)
                previous_payload = payload
        if lines is None:
            unit["content_line_candidates"] = []
            unit["content_line_extraction"] = {
                "status": "failed",
                "error_code": error_code or "content_line_validation_failed",
                "attempt_count": 3,
            }
            failures.append({
                "unit_ref": str(unit.get("unit_ref") or ""),
                "error_code": error_code or "content_line_validation_failed",
                "attempt_count": 3,
            })
            continue
        for line in lines:
            line["support_message_refs"] = [id_to_ref[value] for value in line["support_message_ids"]]
            line["context_message_refs"] = [id_to_ref[value] for value in line["context_message_ids"]]
        unit["content_line_candidates"] = lines
        unit["content_line_extraction"] = {
            "status": "complete",
            "content_line_count": len(lines),
        }
        completed += 1
    review = enriched.setdefault("review", {})
    review["semantic_content_line_extraction"] = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "provider_used": calls > 0,
        "provider_calls": calls,
        "model": str(getattr(model, "model_id", "unknown")),
        "source": str(getattr(model, "source", "unknown")),
        "completed_card_count": completed,
        "eligible_card_count": len(units),
        "requested_card_limit": max_cards,
        "skipped_by_card_limit_count": len(units) - len(target_units),
        "failed_card_count": len(failures),
        "failures": failures,
        "candidate_only": True,
    }
    enriched["provider_used"] = calls > 0
    enriched["provider_calls"] = calls
    return enriched


__all__ = [
    "ContentLineProtocolError", "PROMPT_VERSION", "SCHEMA_VERSION",
    "SYSTEM_PROMPT", "enrich_review_content_lines", "validate_content_lines",
]
