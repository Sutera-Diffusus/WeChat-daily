"""Evidence-preserving final ordering for packet analysis.

The packet workers extract facts.  This module gives a final editor only the
already accepted cards, never the original conversation or rejected drafts.
The editor may rank cards and propose shorter headlines, but it cannot rewrite
the facts.  The web layer performs one more evidence check before accepting a
headline proposal.
"""

import json
import logging
from typing import Any, Dict, List, Mapping, Sequence

from .ai import AnalysisGenerationError, _empty_usage, _provider_usage


logger = logging.getLogger("wechat_bridge.editorial_reducer")


REDUCER_MAX_TOKENS = 12288


REDUCER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ordered_claim_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "brief_claim_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
        "headlines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "claim_id": {"type": "string"},
                    "title": {"type": "string", "minLength": 4, "maxLength": 24},
                },
                "required": ["claim_id", "title"],
            },
        },
    },
    "required": ["ordered_claim_ids", "brief_claim_ids", "headlines"],
}


def _json_object(value: Any) -> Dict[str, Any]:
    text = str(value or "").strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AnalysisGenerationError("最终主编没有返回可解析的 JSON")


def _normalize_reduction(value: Mapping[str, Any], allowed_ids: Sequence[str]) -> Dict[str, Any]:
    allowed = set(allowed_ids)

    def ids(raw: Any, limit: int) -> List[str]:
        if not isinstance(raw, list):
            return []
        return list(dict.fromkeys(
            str(item).strip()
            for item in raw[:limit]
            if str(item).strip() in allowed
        ))

    ordered = ids(value.get("ordered_claim_ids"), len(allowed_ids))
    # Omitted ids are appended locally, so an editor failure or omission can
    # never make an already validated card disappear.
    ordered.extend(claim_id for claim_id in allowed_ids if claim_id not in ordered)
    brief_ids = ids(value.get("brief_claim_ids"), 3)
    if not brief_ids:
        brief_ids = ordered[:3]
    headlines: Dict[str, str] = {}
    raw_headlines = value.get("headlines")
    if isinstance(raw_headlines, list):
        for item in raw_headlines:
            if not isinstance(item, Mapping):
                continue
            claim_id = str(item.get("claim_id") or "").strip()
            title = str(item.get("title") or "").strip()
            if claim_id in allowed and 4 <= len(title) <= 24 and claim_id not in headlines:
                headlines[claim_id] = title
    return {
        "ordered_claim_ids": ordered,
        "brief_claim_ids": brief_ids,
        "headlines": headlines,
    }


def reduce_verified_findings(
    generator: Any,
    window: Mapping[str, str],
    findings: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Rank validated cards without granting the model fact-writing power."""

    if not getattr(generator, "api_key", None):
        raise AnalysisGenerationError("最终主编未配置 API key")
    cards: List[Dict[str, Any]] = []
    for index, finding in enumerate(findings):
        claim_id = "F%03d" % (index + 1)
        cards.append(
            {
                "claim_id": claim_id,
                "title": str(finding.get("title") or "")[:48],
                "summary": str(finding.get("summary") or "")[:360],
                "core_conclusion": str(finding.get("core_conclusion") or "")[:240],
                "what_changed": str(finding.get("what_changed") or "")[:240],
                "why_it_matters": str(finding.get("why_it_matters") or "")[:240],
                "claim_type": str(finding.get("claim_type") or "reported_claim")[:40],
                "evidence_refs": [
                    str(ref) for ref in list(finding.get("evidence_refs") or [])[:8] if str(ref)
                ],
            }
        )
    if not cards:
        return {
            "ordered_claim_ids": [],
            "brief_claim_ids": [],
            "headlines": {},
            "_usage": _empty_usage(),
        }

    instructions = (
        "你是微信情报简报的最终主编。输入只包含已经通过本地证据校验的事实卡。"
        "文体质量对标《人民日报》《新华社》《先锋》《文汇》等权威媒体：行文庄重平实、准确凝练、鲜活有力，"
        "剔除口语化、模板化 AI 表述、空话套话和重复冗余，理顺主次与层级。该对标仅限文体，不得虚构权威口吻。"
        "谁说了什么必须保留明确归属，不得为了文风删除发言人、改变说话主体，"
        "也不得使用有人、群友、成员等无主表述替代事实卡中已有的姓名。"
        "你只能排序、选择前三条重点，并为单张卡压缩标题；禁止新增、补全、合并或改写任何事实，"
        "禁止输出摘要、正文、数字、人物、产品、因果、建议或外部知识。"
        "排序与前三条重点的优先级必须是：具体故障、明确变更、可行动事实、数字证据，"
        "优先于产品使用判断；产品使用判断优先于泛主观比较、问题和片段。"
        "前三条重点不得被泛主观比较、问题或片段等低价值内容挤占。"
        "标题只能复述同一 claim_id 卡片已有的判断，不能跨 claim_id 拼接。"
        "所有输出 claim_id 必须逐字复制输入，且不得遗漏 ordered_claim_ids 中的任何卡片。"
        "输出必须符合 JSON Schema。"
    )
    prompt = "窗口：%s 至 %s\n已验证事实卡：\n%s" % (
        str(window.get("start") or ""),
        str(window.get("end") or ""),
        json.dumps(cards, ensure_ascii=False),
    )
    try:
        from openai import OpenAI

        client = generator._client(OpenAI)
        if getattr(generator, "base_url", None):
            request: Dict[str, Any] = {
                "model": generator.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": min(
                    int(getattr(generator, "max_tokens", REDUCER_MAX_TOKENS)),
                    REDUCER_MAX_TOKENS,
                ),
            }
            effort = str(getattr(generator, "reasoning_effort", "") or "").strip()
            if effort:
                request["reasoning_effort"] = effort
            response = client.chat.completions.create(**request)
            message = getattr(response.choices[0], "message", None)
            output_text = str(getattr(message, "content", "") or "").strip()
            if not output_text:
                output_text = str(getattr(message, "reasoning_content", "") or "").strip()
        else:
            response = client.responses.create(
                model=generator.model,
                instructions=instructions,
                input=prompt,
                store=False,
                max_output_tokens=REDUCER_MAX_TOKENS,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "wechat_verified_editorial_order",
                        "strict": True,
                        "schema": REDUCER_SCHEMA,
                    },
                    "verbosity": "low",
                },
            )
            output_text = str(getattr(response, "output_text", "") or "").strip()
    except AnalysisGenerationError:
        raise
    except Exception as exc:
        raise AnalysisGenerationError("最终主编调用失败: %s" % exc) from exc

    allowed_ids = [card["claim_id"] for card in cards]
    usage = _provider_usage(getattr(response, "usage", None))
    try:
        value = _json_object(output_text)
    except AnalysisGenerationError as exc:
        # The provider may spend the entire output budget on reasoning and
        # return no JSON.  Preserve its metering while falling back to the
        # already-safe local order; a parse failure must not look like a
        # zero-cost request or a successful editorial pass.
        normalized = _normalize_reduction({}, allowed_ids)
        normalized.update({
            "_usage": usage,
            "_parse_failed": True,
            "_parse_error": str(exc),
        })
        logger.warning("最终主编输出解析失败，保留本地顺序: %s", exc)
        return normalized
    normalized = _normalize_reduction(value, allowed_ids)
    normalized["_usage"] = usage
    logger.info("最终主编完成排序: cards=%d headlines=%d", len(cards), len(normalized["headlines"]))
    return normalized
