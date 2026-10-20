"""Extract readable daily-brief cards, grounded in archived chat messages."""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Mapping, Sequence

from .evidence_brief import build_evidence_cards


class QuoteSelectionError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


_RECEIPT_CHECK = re.compile(
    r"(?:收到|签收|到货).{0,18}(?:先|请|记得|要|需|及时).{0,8}(?:试机|测试|检查|验收)", re.I,
)


def _direct_receipt_check(candidate: Mapping[str, Any]) -> bool:
    content = str(candidate.get("content") or "")
    return (
        candidate.get("is_group") is False
        and candidate.get("is_self") is False
        and bool(re.search(r"您|你", content))
        and bool(_RECEIPT_CHECK.search(content))
    )


def _model_item(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "ref": str(candidate.get("evidence_ref") or ""),
        "chat": str(candidate.get("chat_name") or ""),
        "time": str(candidate.get("timestamp") or "")[11:16],
        "sender": str(candidate.get("sender_name") or ""),
        "self": candidate.get("is_self") is True,
        "content": str(candidate.get("content") or ""),
        "context": [str(row.get("content") or "") for row in (candidate.get("context") or [])[:3]
                    if isinstance(row, Mapping)],
    }


def _batches(candidates: Sequence[Mapping[str, Any]], max_items: int = 30) -> list[list[Dict[str, Any]]]:
    """Keep nearby messages from one conversation together within model limits."""
    chats: OrderedDict[str, list[Dict[str, Any]]] = OrderedDict()
    for candidate in candidates:
        item = _model_item(candidate)
        if item["ref"]:
            chats.setdefault(item["chat"], []).append(item)
    batches: list[list[Dict[str, Any]]] = []
    current: list[Dict[str, Any]] = []
    for chat_items in chats.values():
        for start in range(0, len(chat_items), max_items):
            part = chat_items[start:start + max_items]
            if current and len(current) + len(part) > max_items:
                batches.append(current)
                current = []
            current.extend(part)
    if current:
        batches.append(current)
    return batches


def _instructions(profile: Mapping[str, Any]) -> str:
    name = str(profile.get("self_name") or "读者").strip()
    return (
        '为%s从一天的聊天中提取有独立阅读价值的信息卡。只返回 JSON：'
        '{"stories":[{"topic":"中性话题名","refs":["m-001"]}]}。'
        '先找信息，再考虑展示；不要按预设类别或工具名单设准入门槛，也不设固定卡片数。'
        '具体任务、决定、进展、障碍、时间变化、风险观察、可复查的资料、'
        '课程与项目消息、具体工具使用经验都可以入选，不限定行业或工具。'
        '每张卡必须回答读者「今天新增知道了什么，之后可以据此做什么或查什么」；'
        '如果删掉这张卡不会损失具体信息，就不要选。'
        '私人对话也可选，但只保留有实际信息的内容；群聊中的他人经验须保留为个人观察。'
        '同一会话的同一事项合成一张卡，通常只选能使事项独立可读的 1 到 5 条关键原话；'
        '不要把不同会话的无关消息拼成同一事项。'
        '排除广告、闲聊、玩笑、个人作息、普通提问、无结果的讨论、无新信息的重复、'
        '缺乏具体依据的断言、空泛评价，以及只有猜测且没有查证价值的传闻。'
        '不要把一组松散聊天包装成重要事项，也不要因同一群很热闹就多出卡。'
        '只引用输入 ref；context 仅用于理解，不可独立引用。'
        'topic 只用中性标签，不写未经证实的结论；不要写摘要、行动建议或编造事实。'
    ) % name


def select_quote_cards(
    candidates: Sequence[Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
    profile: Mapping[str, Any],
    call_model: Callable[[str, Sequence[Mapping[str, Any]]], Mapping[str, Any]],
) -> Dict[str, Any]:
    """Select general-interest cards; reject a day if any provider call fails."""
    batches = _batches(candidates)
    results: list[Mapping[str, Any]] = []
    retried_batches = 0
    if batches:
        instructions = _instructions(profile)

        def run_batch(batch: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], int]:
            for attempt in range(2):
                try:
                    return call_model(instructions, batch), attempt
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    logger.warning("daily brief selector batch call failed (%s, HTTP %s, attempt %s)", type(exc).__name__, status_code, attempt + 1)
                    if status_code == 402:
                        raise QuoteSelectionError("quote_selection_quota_exhausted") from exc
                    if attempt or status_code in {400, 401, 403, 404}:
                        raise
            raise AssertionError("unreachable")

        with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
            futures = [pool.submit(run_batch, batch) for batch in batches]
            for future in futures:
                try:
                    result, retries = future.result()
                except QuoteSelectionError:
                    raise
                except Exception as exc:
                    raise QuoteSelectionError("quote_selection_provider_failed") from exc
                if not isinstance(result, Mapping) or not isinstance(result.get("stories"), list):
                    raise QuoteSelectionError("quote_selection_invalid_response")
                results.append(result)
                retried_batches += retries

    by_ref = {str(item.get("evidence_ref") or ""): item for item in candidates}
    accepted_stories: list[Dict[str, Any]] = []
    invalid_refs = 0
    used_refs: set[str] = set()
    for batch, result in zip(batches, results):
        allowed = {item["ref"] for item in batch}
        for raw_story in result["stories"]:
            if not isinstance(raw_story, Mapping) or not isinstance(raw_story.get("refs"), list):
                continue
            refs = list(dict.fromkeys(str(ref) for ref in raw_story["refs"] if str(ref)))
            if not refs:
                continue
            if any(ref not in allowed or ref not in by_ref for ref in refs):
                invalid_refs += 1
                continue
            refs = [ref for ref in refs if ref not in used_refs]
            if not refs:
                continue
            accepted_stories.append({
                "topic": str(raw_story.get("topic") or "聊天信息")[:80],
                "candidate_ids": refs,
            })
            used_refs.update(refs)

    # A concrete instruction to check a received device is easily missed in
    # a busy private chat, and has a safe literal quote when it is present.
    for ref, candidate in by_ref.items():
        if ref not in used_refs and _direct_receipt_check(candidate):
            accepted_stories.append({"topic": "收件后检查", "candidate_ids": [ref]})
            used_refs.add(ref)

    source_candidates = {
        ref: {"support_message_ids": [str(item.get("_source_message_id") or "")]}
        for ref, item in by_ref.items()
    }
    cards = []
    seen_sources: set[tuple[str, ...]] = set()
    for story in accepted_stories:
        for card in build_evidence_cards([story], source_candidates, messages):
            source_key = tuple(card["evidence_message_ids"])
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                cards.append(card)
    return {
        "cards": cards,
        "audit": {
            "candidate_count": len(candidates),
            "batch_count": len(batches),
            "batch_input_counts": [len(batch) for batch in batches],
            "retried_batches": retried_batches,
            "selected_story_count": len(accepted_stories),
            "card_count": len(cards),
            "quote_count": sum(len(card["evidence"]) for card in cards),
            "invalid_story_refs": invalid_refs,
        },
    }


def openai_quote_model(model: str, api_key: str, base_url: str | None = None) -> Callable[[str, Sequence[Mapping[str, Any]]], Mapping[str, Any]]:
    """Make a JSON-only selector call for one conversation batch."""

    def call(instructions: str, items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=240)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": instructions},
                      {"role": "user", "content": json.dumps(list(items), ensure_ascii=False)}],
            response_format={"type": "json_object"},
            max_tokens=12000,
            reasoning_effort="low",
        )
        raw = str(response.choices[0].message.content or "")
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise QuoteSelectionError("quote_selection_empty_response")
        return json.loads(match.group(0))

    return call
