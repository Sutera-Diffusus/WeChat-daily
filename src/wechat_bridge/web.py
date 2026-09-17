"""Small local-only HTTP API and dashboard server."""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .ai import AnalysisGenerationError, OpenAIAnalysisGenerator
from .analysis import (
    _action_evidence,
    _claim_profile,
    _context_terms,
    _DEADLINE,
    _display_content,
    _event_domain_tags,
    _event_domains_compatible,
    _has_concrete_object,
    _redact_ai_content,
    _score_message,
    analyze_messages,
    build_ai_context,
    build_ai_dialogue_packets,
    is_editorial_title,
    normalize_editorial_title,
)
from .engine import ReplyRule
from .editorial_reducer import reduce_verified_findings
from .fact_check import HttpSearchProvider, check_claims
from .image_key import request_image_key_discovery, runtime_image_key
from .media import MediaUnavailable, V2_MAGIC, cache_key, read_media
from .models import IncomingMessage
from .settings import WorkbenchSettings
from .shadow_semantic import ShadowSemanticRunStore
from .shadow_run_v28 import load_shadow_catalog
from .timeutil import get_timezone
from .voice import ASRError, DoubaoASRClient, decode_silk_to_wav, extract_wechat_voice_transcript

logger = logging.getLogger("wechat_bridge.web")
WEB_ROOT = Path(__file__).with_name("web")


_AI_OBJECT_DOMAINS = {
    "wechat",
    "gpt_service",
    "codex_service",
    "developer_account",
}

_AI_OBJECT_FAMILY_PATTERNS = (
    ("gpt_service", re.compile(r"(?:gpt|chatgpt|openai|astra|\bsol\b|\b4o\b)", re.IGNORECASE)),
    ("codex_service", re.compile(r"(?:codex|code\s*x)", re.IGNORECASE)),
    ("claude_service", re.compile(r"(?:claude|fable)", re.IGNORECASE)),
    ("gemini_service", re.compile(r"gemini", re.IGNORECASE)),
    ("cursor_service", re.compile(r"cursor", re.IGNORECASE)),
    ("grok_service", re.compile(r"grok", re.IGNORECASE)),
    ("deepseek_service", re.compile(r"deepseek", re.IGNORECASE)),
    ("kimi_service", re.compile(r"kimi", re.IGNORECASE)),
)


def _ai_object_families(value: Any) -> set:
    """Return named AI product families used by the synthesis boundary."""

    text = str(value or "")
    families = {
        family for family, pattern in _AI_OBJECT_FAMILY_PATTERNS if pattern.search(text)
    }
    families.update(_event_domain_tags(text) & {"wechat", "developer_account"})
    return families


def _cross_object_synthesis_rejection(
    finding_text: str,
    evidence: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """Validate an editorial synthesis whose citations name separate objects.

    A synthesis may combine separate evidence lines when the prose covers all
    named objects and the lines still form one compact story.  Broad model
    roundups remain rejected unless they are one person's contiguous toolchain
    account.
    """

    finding_families = _ai_object_families(finding_text)
    evidence_family_sets = [
        _ai_object_families(item.get("content")) for item in evidence
    ]
    evidence_families = (
        set().union(*evidence_family_sets) if evidence_family_sets else set()
    )
    if finding_families and not finding_families.issubset(evidence_families):
        return "finding_domain_missing_in_evidence"
    if evidence_families and not evidence_families.issubset(finding_families):
        return "evidence_object_missing_in_finding"

    finding_terms = _context_terms(finding_text)
    evidence_terms = [_context_terms(item.get("content")) for item in evidence]
    for families, terms in zip(evidence_family_sets, evidence_terms):
        if not families.intersection(finding_families) and not terms.intersection(finding_terms):
            return "cited_evidence_unrelated_to_synthesis"

    chats = {str(item.get("chat_name") or "") for item in evidence}
    senders = {str(item.get("sender_name") or "") for item in evidence}
    evidence_times: List[datetime] = []
    for item in evidence:
        raw_timestamp = str(item.get("timestamp") or "").strip()
        if not raw_timestamp:
            continue
        try:
            evidence_times.append(datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")))
        except ValueError:
            continue
    close_in_time = (
        len(evidence_times) != len(evidence)
        or max(evidence_times) - min(evidence_times) <= timedelta(minutes=90)
    )
    multi_author_close_in_time = (
        len(evidence_times) == len(evidence)
        and max(evidence_times) - min(evidence_times) <= timedelta(minutes=30)
    )
    single_author_thread = (
        len(evidence) >= 2
        and len(chats) == 1
        and "" not in chats
        and len(senders) == 1
        and "" not in senders
        and close_in_time
    )
    # Three or more named model families is a broad roundup, even when one
    # person authored the messages.  The known toolchain case stays below this
    # threshold because Hermes/OAuth/MCP are workflow terms, not model claims.
    if len(evidence_families) >= 3:
        return "broad_multi_object_synthesis"

    if len(senders) > 1 and not multi_author_close_in_time:
        return "multi_object_synthesis_lacks_shared_event"

    repeated_terms = set()
    for index, terms in enumerate(evidence_terms):
        for other_terms in evidence_terms[index + 1:]:
            repeated_terms.update(terms.intersection(other_terms))
    if not single_author_thread and not repeated_terms:
        return "multi_object_synthesis_lacks_shared_event"
    return None


def _headline_from_summary(summary: Any, evidence: Sequence[Mapping[str, Any]]) -> str:
    """Derive an evidence-led editorial headline without clipping raw prose."""

    summary_text = re.sub(r"\s+", " ", str(summary or "")).strip()
    evidence_text = " ".join(
        re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        for item in evidence
    )
    combined = "%s %s" % (summary_text, evidence_text)
    if not combined.strip():
        return ""

    # Prefer deterministic subject+judgment patterns.  Every inserted detail is
    # taken from the validated summary/evidence, so readability improves without
    # giving the editor permission to invent a new fact.
    deepseek_named_version = re.search(r"deepseek[\s_-]*v?(\d+(?:\.\d+)?)", combined, re.I)
    loose_version = re.search(r"\bv(\d+(?:\.\d+)?)\b", combined, re.I)
    deepseek_version = deepseek_named_version or (
        loose_version if re.search(r"deepseek", combined, re.I) else None
    )
    if deepseek_version:
        subject = "DeepSeek V%s" % deepseek_version.group(1)
        has_multimodal = bool(re.search(r"原生多模态", combined, re.I))
        has_endpoint = bool(re.search(r"官方端点|官方.*(?:调用|直打)|endpoint", combined, re.I))
        speed = re.search(r"(\d+(?:\.\d+)?)\s*tps", combined, re.I)
        if has_endpoint and speed:
            candidate = "%s：官方端点与%stps" % (subject, speed.group(1))
        elif has_multimodal:
            candidate = "%s：原生多模态" % subject
        elif has_endpoint:
            candidate = "%s：官方端点开放" % subject
        else:
            candidate = "%s：新版本进入测试" % subject
        if is_editorial_title(candidate):
            return candidate

    if re.search(r"\bgoal\b", combined, re.I) and re.search(r"停止", combined) and re.search(r"当前\s*prompt|当前任务|还没停止|仍.*执行", combined, re.I):
        candidate = "Goal停止后：当前任务仍会执行"
        if is_editorial_title(candidate):
            return candidate

    speed = re.search(r"(\d+(?:\.\d+)?)\s*tps", combined, re.I)
    if speed and re.search(r"\bflash\b", combined, re.I):
        candidate = "Flash实测：速度约%stps" % speed.group(1)
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"pro", combined, re.I) and re.search(r"\d+\s*倍价格", combined) and re.search(r"\d+\s*倍额度", combined):
        candidate = "Pro套餐：价格与额度倍数对应"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"superpowers?", combined, re.I) and re.search(r"毫无必要|没有必要|不需要|必要性", combined):
        candidate = "Superpowers：必要性遭质疑"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"fable", combined, re.I) and re.search(r"耐用", combined) and re.search(r"usage|用量", combined, re.I) and re.search(r"拿不到|不可见|看不到", combined):
        candidate = "Fable体感：更耐用但Usage不可见"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"z\.ai|bigmodel", combined, re.I) and re.search(r"登录|账号", combined) and re.search(r"周末|免费\s*token", combined, re.I):
        candidate = "模型接入：Z.ai登录与周末Token"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"模型测试", combined) and re.search(r"测试方案", combined) and re.search(r"邀请|参与", combined):
        candidate = "模型测试：方案征集与参与邀请"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"gpt\s*-?\s*6|gpt6", combined, re.I) and re.search(r"计费异常", combined):
        candidate = "GPT-6计费：异常范围仍待核实"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"codex", combined, re.I) and re.search(r"第三方模型", combined) and re.search(r"麻烦|困难|配置", combined):
        candidate = "Codex接入：第三方模型配置仍麻烦"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"gpt", combined, re.I) and re.search(r"国模|国产模型", combined) and re.search(r"更强|比.{0,8}强|强于", combined):
        candidate = "模型评价：GPT仍被认为强于国产模型"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"gemini", combined, re.I) and re.search(r"网页版", combined) and re.search(r"写代码|编程|做网站", combined):
        candidate = "Gemini编程：网页版早期体验尚可"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"deepseek|ds", combined, re.I) and re.search(r"pro", combined, re.I) and re.search(r"harness|\bdsh\b", combined, re.I) and re.search(r"发挥|性能", combined):
        candidate = "DeepSeek Pro：DSH被认为是性能前提"
        if is_editorial_title(candidate):
            return candidate
    if (
        re.search(r"\bastra(?:\s+max)?\b", combined, re.I)
        and re.search(r"复杂请求|复杂任务", combined)
        and re.search(r"很长时间|耗时|花很久", combined)
        and re.search(r"\bluna\b", combined, re.I)
        and re.search(r"效果.*不好|效果.*欠佳|效果受限", combined)
    ):
        candidate = "Astra复杂请求耗时，Luna替代效果欠佳"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"\bastra\b", combined, re.I) and re.search(r"快|速度", combined) and re.search(r"\bluna\b", combined, re.I) and re.search(r"效果.*不好|效果受限", combined):
        candidate = "Astra速度获好评，Luna被指耗时且效果欠佳"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"high", combined, re.I) and re.search(r"正合适|正正好|默认.*high|推荐.*high", combined, re.I):
        candidate = "推理强度：High被认为正合适"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"openai", combined, re.I) and re.search(r"overloaded|过载", combined, re.I) and re.search(r"流式", combined):
        candidate = "OpenAI过载：流式机制暴露间歇异常"
        if is_editorial_title(candidate):
            return candidate
    if re.search(r"kimi", combined, re.I) and re.search(r"部署", combined) and re.search(r"无图形界面|无界面", combined) and re.search(r"释放.*性能|更多性能|算力", combined):
        candidate = "Kimi部署：无界面方案释放更多算力"
        if is_editorial_title(candidate):
            return candidate

    # Remove speaker scaffolding from each claim before considering a fallback.
    claims = [
        part.strip()
        for part in re.split(r"[。；;！!？?]+", summary_text)
        if part.strip()
    ]
    candidates: List[str] = []
    for claim in claims:
        cleaned = re.sub(
            r"^(?:[^，,：:]{1,32}?)(?:在[^，,：:]{1,24}(?:中|里)?)?"
            r"(?:称|说|表示|陈述|提到|指出|转述|询问|确认|反馈|提出)[，,：:\s]*",
            "",
            claim,
        ).strip("，,：:、 “ ”\"")
        if cleaned:
            candidates.append(cleaned)
    candidates.extend(claims)
    for candidate in candidates:
        clause = re.split(r"[，,、]", candidate, maxsplit=1)[0].strip()
        for value in (candidate, clause):
            if value and len(value) <= 24 and is_editorial_title(value):
                return value

    terms = [
        term
        for term in _context_terms(combined)
        if len(term) >= 2 and not re.fullmatch(r"[0-9]+", term)
    ]
    if terms:
        subject = terms[0]
        candidate = "%s：关键进展仍待核实" % subject
        if is_editorial_title(candidate):
            return candidate
    return "讨论主线：关键问题仍待核实"


def _deduplicate_model_claims(
    claims: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge repeated atomic statements while retaining all citations."""

    output: List[Dict[str, Any]] = []
    by_text: Dict[str, Dict[str, Any]] = {}
    for claim in claims:
        text = str(claim.get("text") or "").strip()
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text.casefold())
        if not text or not key:
            continue
        refs = list(dict.fromkeys(
            str(ref) for ref in claim.get("evidence_refs") or [] if str(ref)
        ))
        existing = by_text.get(key)
        if existing is None:
            existing = {"text": text, "evidence_refs": refs}
            by_text[key] = existing
            output.append(existing)
        else:
            existing["evidence_refs"] = list(dict.fromkeys(
                list(existing.get("evidence_refs") or []) + refs
            ))
    return output


_CLAIM_UNCERTAINTY = re.compile(
    r"貌似|好像|可能|感觉|我记得|似乎|大概|也许|据说|听说"
)


def _preserve_claim_uncertainty(
    claim_text: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    """Restore an epistemic qualifier dropped from an evidence paraphrase."""

    text = str(claim_text or "").strip()
    if not text or _CLAIM_UNCERTAINTY.search(text):
        return text
    marker = ""
    marker_item: Optional[Mapping[str, Any]] = None
    marker_match: Optional[re.Match] = None
    for item in evidence:
        match = _CLAIM_UNCERTAINTY.search(str(item.get("content") or ""))
        if match:
            marker = match.group(0)
            marker_item = item
            marker_match = match
            break
    if not marker or marker_item is None or marker_match is None:
        return text
    if marker == "我记得" and "记得" in text:
        return text
    attribution = re.match(
        r"^(.{1,32}?(?:称|说|表示|提到|认为|反馈|指出))[，,：:\s]*",
        text,
    )
    claim_body = (
        text[attribution.end():].lstrip("，,：: ") if attribution else text
    )
    evidence_content = str(marker_item.get("content") or "")
    prefix = evidence_content[:marker_match.start()].rstrip("，,；;。.!！?？ ")
    if prefix and _claim_directly_supported(claim_body, [{"content": prefix}]):
        # The qualifier belongs to a later clause.  If the text before it
        # already supports this atomic claim, importing the later qualifier
        # would distort rather than preserve its scope.
        return text
    if attribution:
        return "%s，%s%s" % (attribution.group(1), marker, claim_body)
    return "%s%s" % (marker, text)


def _evidence_is_question_only(evidence: Sequence[Mapping[str, Any]]) -> bool:
    """Return true when every non-empty cited message is phrased as a question."""

    contents = [
        re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        for item in evidence
        if str(item.get("content") or "").strip()
    ]
    if not contents:
        return False
    return all(
        bool(re.search(r"[?？]\s*$|(?:吗|么|呢|是不是|是否|能否|有没有)[。.!！?？\s]*$", content))
        for content in contents
    )

def _finding_evidence_boundary_reason(
    finding: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """Return a stable reason code when prose crosses its evidence boundary.

    Reference ids are necessary but not sufficient: a model can return valid
    ids while describing a different part of the context.  The local object
    tags provide a hard precision check; for untagged prose, a small lexical
    overlap check catches obviously unrelated evidence without requiring the
    local layer to understand every paraphrase.
    """

    finding_text = " ".join(
        str(finding.get(key) or "")
        for key in (
            "title",
            "summary",
            "narrative",
            "core_conclusion",
            "what_changed",
            "why_it_matters",
            "keywords",
        )
    )
    evidence_text = " ".join(str(item.get("content") or "") for item in evidence)
    finding_domains = _event_domain_tags(finding_text) & _AI_OBJECT_DOMAINS
    evidence_domain_sets = [
        _event_domain_tags(item.get("content")) & _AI_OBJECT_DOMAINS
        for item in evidence
    ]
    evidence_domains = set().union(*evidence_domain_sets) if evidence_domain_sets else set()

    needs_synthesis_review = (
        not _event_domains_compatible(evidence)
        or len(finding_domains) > 1
        or (bool(finding_domains) and any(not tags for tags in evidence_domain_sets))
    )
    if needs_synthesis_review:
        return _cross_object_synthesis_rejection(finding_text, evidence)

    # A generated sentence that names GPT, Codex, WeChat or a developer
    # account must be backed by the same object.  This also rejects a single
    # finding that mentions two objects while citing only one of them.
    if finding_domains and not finding_domains.issubset(evidence_domains):
        return "finding_domain_missing_in_evidence"
    if finding_domains:
        # The clustering layer may keep an untagged follow-up inside a
        # single-domain event.  A published finding is stricter: every cited
        # line must explicitly support the named object, otherwise an
        # unrelated side branch can hide behind a valid reference id.
        if any(
            not tags or not tags.intersection(finding_domains)
            for tags in evidence_domain_sets
        ):
            return "cited_evidence_missing_finding_domain"
    if len(finding_domains) > 1 and not _event_domains_compatible(
        [{"content": item.get("content")} for item in evidence]
    ):
        return "multi_domain_evidence_incompatible"

    # If the prose has no explicit object but the evidence does, an unrelated
    # evidence line is still unsafe.  Keep abstract paraphrases possible when
    # the evidence itself has no hard object tag.
    if not finding_domains and evidence_domains:
        finding_terms = _context_terms(finding_text)
        evidence_terms = _context_terms(evidence_text)
        if finding_terms and evidence_terms and not finding_terms.intersection(evidence_terms):
            return "lexical_no_overlap"
    return None

def _finding_evidence_boundary_violation(
    finding: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Preserve the boolean boundary-check contract used by existing callers."""

    return _finding_evidence_boundary_reason(finding, evidence) is not None

_NARRATIVE_BOILERPLATE = re.compile(r"当前只能确认[^。]*。")

_NARRATIVE_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*")

_NARRATIVE_SUFFIXED_NUMBER = re.compile(
    r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?:[kKmMgGtT](?:[bB])?|%)(?![A-Za-z0-9.])"
)

_NARRATIVE_NUMBER = re.compile(
    r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9.])"
)

_NARRATIVE_CAUSAL = re.compile(
    r"导致|造成|使得|致使|源于|归因于|主因|因为|所以|因此|从而|带来"
)

_NARRATIVE_SENTENCE = re.compile(r"[^。！？；;!?]+")

_NARRATIVE_REPLY_COMPLETION = re.compile(
    r"得到(?:了)?回复|群内回复|对方回谢|已确认|已答复|获得(?:了)?答复"
)

_NARRATIVE_QUESTION = re.compile(
    r"[?？]|是否|能否|可否|吗(?:[\s？?]|$)|怎么|如何|什么|啥|想问|请问|询问"
)

_NARRATIVE_OBJECT_FAMILY = re.compile(
    r"^(?:gpt|chatgpt|openai|astra|sol|4o|codex|claude|fable|gemini|cursor|grok|deepseek|kimi|wechat).*$",
    re.IGNORECASE,
)

_NARRATIVE_GENERIC_TOKENS = frozenset({
    "agent", "agents", "token", "tokens", "model", "models", "pro", "plus",
    "team", "prompt", "prompts", "skill", "skills", "chat", "group", "api",
    "app", "ai", "bot", "llm", "llms", "vibe", "coding", "code", "test",
    "tests", "flash", "max", "mini", "web", "gui", "cli", "ide", "sdk",
    "mcp", "bug", "bugs", "github", "com", "org", "www", "http", "https",
    "url", "link", "links",
})

_CN_DIGITS = "零一二三四五六七八九"

_RECOVERED_GENERIC_TITLES = frozenset({
    "账号风控：微信只读与合规争议",
    "Codex重置：额度状态待核实",
    "AI账号：重置与稳定性争议",
    "GPT困局：封号与成本夹击",
    "模型分野：Claude更受信任",
    "额度重置：消耗焦虑紧随而来",
    "外链汇集：价值仍待核验",
    "工具之问：两种方案尚待比较",
})

def _clean_limitations(value: Any, limit: int = 8) -> List[str]:
    """Repair comma-split model limitations without rewriting their claims."""

    if not isinstance(value, list):
        return []
    merged: List[str] = []
    dangling = re.compile(
        r"(?:与|和|及|或|包括|集中于|涉及|来自|例如|如|以及|针对|关于|其中|集中于与[^，。；]{1,12})$"
    )
    continuation = re.compile(r"^(?:并|且|以及|但|而|避免|仅|仍|其中|同时|因此|从而|以免)")
    for raw in value:
        text = re.sub(r"\s+", " ", str(raw or "")).strip(" ，,；;")
        if not text:
            continue
        if merged and (dangling.search(merged[-1]) or continuation.search(text)):
            separator = "" if dangling.search(merged[-1]) else "；"
            merged[-1] = merged[-1].rstrip("。；;") + separator + text
        else:
            merged.append(text)

    result: List[str] = []
    seen = set()
    for text in merged:
        normalized = text.rstrip("。；;") + "。"
        key = re.sub(r"[\s。；;，,]", "", normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result

def _narrative_haystack(evidence: Sequence[Mapping[str, Any]]) -> str:
    """Normalize cited evidence into one lookup string for claim checks."""

    raw = " ".join(
        "%s %s %s"
        % (
            item.get("content") or "",
            item.get("sender_name") or "",
            item.get("chat_name") or "",
        )
        for item in evidence
    )
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", raw.lower())

def _chinese_numeral_variants(value: int) -> set:
    """Map small integers to the Chinese spellings a paraphrase may use."""

    if not 1 <= value <= 99:
        return set()
    if value < 10:
        variants = {_CN_DIGITS[value]}
        if value == 2:
            variants.add("两")
        return variants
    tens, ones = divmod(value, 10)
    return {_CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")}

def _number_supported_by_evidence(number: str, evidence_text: str) -> bool:
    """Match a number as a value, never as a substring of another value."""

    if re.search(r"(?<![\d.])%s(?![\d.])" % re.escape(number), evidence_text):
        return True
    try:
        numeric = float(number)
        variants = _chinese_numeral_variants(int(numeric)) if numeric.is_integer() else set()
    except (TypeError, ValueError, OverflowError):
        variants = set()
    numeral_chars = "零一二两三四五六七八九十百千万"
    return any(
        re.search(
            r"(?<![%s])%s(?![%s])"
            % (numeral_chars, re.escape(variant), numeral_chars),
            evidence_text,
        )
        for variant in variants
    )

def _causal_claim_supported(sentence: str, evidence: Sequence[Mapping[str, Any]]) -> bool:
    """Require an asserted causal link to exist inside one cited message.

    Merely citing one message for the cause and another for the outcome must
    not allow the model to invent the relation between them.
    """

    match = _NARRATIVE_CAUSAL.search(sentence)
    if match is None:
        return True
    left_terms = _context_terms(sentence[:match.start()])
    right_terms = _context_terms(sentence[match.end():])
    for item in evidence:
        content = str(item.get("content") or "")
        explicit_intervention = re.search(
            r"(?:换|改|停|启用|使用).{0,24}后.{0,32}(?:没|不再|减少|降低|改善|正常|解决)",
            content,
        )
        if not (_NARRATIVE_CAUSAL.search(content) or explicit_intervention):
            continue
        content_terms = _context_terms(content)
        if left_terms and not left_terms.intersection(content_terms):
            continue
        if right_terms and not right_terms.intersection(content_terms):
            continue
        return True
    return False

def _parse_evidence_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

def _reply_completion_supported(evidence: Sequence[Mapping[str, Any]]) -> bool:
    """Require a real later reply, not two questions presented as closure."""

    questions = []
    replies = []
    for item in evidence:
        timestamp = _parse_evidence_timestamp(item.get("timestamp"))
        chat = str(item.get("chat_name") or "").strip()
        sender = str(item.get("sender_name") or "").strip()
        content = str(item.get("content") or "").strip()
        if timestamp is None or not chat or not sender or not content:
            continue
        row = (timestamp, chat, sender, content)
        if _NARRATIVE_QUESTION.search(content):
            questions.append(row)
        else:
            replies.append(row)
    return any(
        reply_time > question_time
        and reply_chat == question_chat
        and reply_sender != question_sender
        for question_time, question_chat, question_sender, _ in questions
        for reply_time, reply_chat, reply_sender, _ in replies
    )

def _claim_bearing_token(token: str) -> bool:
    """Return whether a latin token is a specific claim, not a generic word."""

    lowered = token.lower()
    if lowered in _NARRATIVE_GENERIC_TOKENS or _NARRATIVE_OBJECT_FAMILY.match(token):
        return False
    if len(token) >= 4:
        return True
    if any(char.isdigit() for char in token) or any(char in "._-" for char in token):
        return True
    # Short all-lowercase words (bug, api) are generic; mixed/upper case short
    # tokens (DTO, iOS) tend to name something specific.
    return len(token) == 3 and not token.islower()

def _within_one_edit(left: str, right: str) -> bool:
    """Cheap Levenshtein<=1 check for typo-tolerant identifier matching."""

    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    if len(left) > len(right):
        left, right = right, left
    index = 0
    while index < len(left) and left[index] == right[index]:
        index += 1
    return left[index:] == right[index + 1:]

def _finding_unsupported_narrative_claims(
    finding: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> List[Dict[str, str]]:
    """Return hard narrative claims that none of the cited evidence supports.

    The boundary check guards *which objects* a finding may mention; this
    guard covers the remaining failure mode where the prose states specific
    numbers, product identifiers, or speaker attributions that appear nowhere
    in the cited evidence.  Such details cannot be traced back to a local
    message, so the finding must not be published as a card.  The check is
    deliberately conservative: generic words, AI object names (guarded by the
    boundary layer), and Chinese-numeral paraphrases of cited digits all pass.
    """

    prose = " ".join(
        str(finding.get(key) or "")
        for key in (
            "title",
            "summary",
            "narrative",
            "core_conclusion",
            "what_changed",
            "why_it_matters",
            "keywords",
        )
    )
    prose = _NARRATIVE_BOILERPLATE.sub("", prose)
    if not prose.strip() or not evidence:
        return []
    haystack = _narrative_haystack(evidence)
    # Numbers and identifiers may legitimately come from citation metadata.
    # A group name such as ``Vibe Friends 999`` is part of the cited source,
    # even when ``999`` is not repeated in the message body.  Speaker
    # attribution remains stricter below and only accepts ``sender_name``.
    evidence_text = " ".join(
        "%s %s %s"
        % (
            item.get("content") or "",
            item.get("sender_name") or "",
            item.get("chat_name") or "",
        )
        for item in evidence
    )
    senders = {str(item.get("sender_name") or "") for item in evidence}
    # Raw identifier tokens from the evidence keep their boundaries, which
    # allows forgiving one-edit typos in the source (use-superpowes, angent).
    evidence_identifiers = {
        re.sub(r"[^0-9a-z]+", "", token.lower())
        for item in evidence
        for token in _NARRATIVE_LATIN_TOKEN.findall(
            "%s %s %s"
            % (
                item.get("content") or "",
                item.get("sender_name") or "",
                item.get("chat_name") or "",
            )
        )
    }
    evidence_identifiers.discard("")
    unsupported: List[Dict[str, str]] = []
    seen = set()

    def flag(kind: str, token: str) -> None:
        key = (kind, token)
        if key not in seen:
            seen.add(key)
            unsupported.append({"kind": kind, "token": token})

    for token in _NARRATIVE_LATIN_TOKEN.findall(prose):
        if not _claim_bearing_token(token):
            continue
        normalized = re.sub(r"[^0-9a-z]+", "", token.lower())
        if normalized and normalized in evidence_identifiers:
            continue
        base = token.split(".")[0] if "." in token else token
        if base != token:
            base_normalized = re.sub(r"[^0-9a-z]+", "", base.lower())
            if base_normalized and base_normalized in evidence_identifiers:
                continue
        if len(normalized) >= 6 and any(
            _within_one_edit(normalized, choice) for choice in evidence_identifiers
        ):
            continue
        flag("identifier", token)
    for token in _NARRATIVE_SUFFIXED_NUMBER.findall(prose):
        if re.search(
            r"(?<![A-Za-z0-9.])%s(?![A-Za-z0-9.])" % re.escape(token),
            evidence_text,
            re.IGNORECASE,
        ):
            continue
        flag("number", token)
    for number in _NARRATIVE_NUMBER.findall(prose):
        if _number_supported_by_evidence(number, evidence_text):
            continue
        flag("number", number)
    # Speaker names come from the structured model field and are checked
    # against cited sender metadata.  Do not guess names from arbitrary short
    # Chinese phrases such as “重复表示” or “具体事实说明”.
    for speaker in finding.get("speakers") or []:
        if not isinstance(speaker, Mapping):
            continue
        name = str(speaker.get("name") or "").strip()
        if not name or name in {"我", "用户本人"}:
            continue
        if any(
            name == sender or (sender and (name in sender or sender in name))
            for sender in senders
        ):
            continue
        flag("speaker", name)
    for field in (
        "title", "summary", "narrative", "core_conclusion",
        "what_changed", "why_it_matters",
    ):
        for sentence in _NARRATIVE_SENTENCE.findall(str(finding.get(field) or "")):
            match = _NARRATIVE_CAUSAL.search(sentence)
            if match and not _causal_claim_supported(sentence, evidence):
                flag("causality", match.group(0))
            reply_match = _NARRATIVE_REPLY_COMPLETION.search(sentence)
            if reply_match and not _reply_completion_supported(evidence):
                flag("reply_completion", reply_match.group(0))
    return unsupported

def _model_claim_text(claim: Mapping[str, Any]) -> str:
    """Read the deliberately small claim contract used by packet extraction."""

    for key in ("text", "claim", "claim_text", "statement", "summary"):
        value = re.sub(r"\s+", " ", str(claim.get(key) or "")).strip()
        if value:
            return value
    return ""

def _model_claim_refs(claim: Mapping[str, Any]) -> List[str]:
    values: Any = claim.get("evidence_refs")
    if values is None:
        values = claim.get("ref_ids")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(value) for value in values if str(value)))

def _claim_directly_supported(
    claim_text: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Fail closed unless a claim has substantial overlap with its own refs.

    The ordinary finding validator intentionally permits broad paraphrase. A
    packet extractor has a stronger contract: each atomic claim must remain
    traceable without borrowing uncited neighbouring messages from the packet.
    """

    compact_claim = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", claim_text.casefold())
    compact_evidence = re.sub(
        r"[^0-9a-z\u4e00-\u9fff]",
        "",
        " ".join(str(item.get("content") or "") for item in evidence).casefold(),
    )
    if not compact_claim or not compact_evidence:
        return False
    if compact_claim in compact_evidence:
        return True
    comparison = re.search(r"更(?:高|低|快|慢|强|弱|贵|便宜)|优于|弱于|高于|低于|不如|相比|比.{0,12}(?:高|低|快|慢|强|弱|贵|便宜)", claim_text)
    if comparison and not any(
        re.search(r"更(?:高|低|快|慢|强|弱|贵|便宜)|优于|弱于|高于|低于|不如|相比|比.{0,12}(?:高|低|快|慢|强|弱|贵|便宜)", str(item.get("content") or ""))
        for item in evidence
    ):
        return False

    claim_terms = _context_terms(claim_text)
    evidence_terms = set().union(
        *(_context_terms(item.get("content")) for item in evidence)
    ) if evidence else set()
    if not claim_terms or not evidence_terms:
        return False
    shared = claim_terms & evidence_terms
    # Full Chinese chunks make paraphrases look artificially sparse, so judge
    # primarily on concrete bigrams while retaining identifiers as anchors.
    atomic_terms = {
        term for term in claim_terms
        if len(term) == 2 or re.fullmatch(r"[a-z][a-z0-9_-]{2,}", term)
    }
    shared_atomic = atomic_terms & evidence_terms
    if not atomic_terms:
        return bool(shared)
    required = 1 if len(atomic_terms) <= 2 else max(2, (len(atomic_terms) + 1) // 2)
    return len(shared_atomic) >= required


def _title_supported_by_evidence(
    title: Any,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Keep a model headline only when its concrete subject exists in quotes."""

    text = re.sub(r"\s+", "", str(title or "")).strip()
    if not text or not evidence:
        return False
    title_domains = _event_domain_tags(text) & _AI_OBJECT_DOMAINS
    evidence_domains = set().union(
        *(_event_domain_tags(item.get("content")) & _AI_OBJECT_DOMAINS for item in evidence)
    )
    if title_domains and not title_domains.issubset(evidence_domains):
        return False
    title_terms = _context_terms(text)
    evidence_terms = set().union(*(_context_terms(item.get("content")) for item in evidence))
    return bool(title_terms and title_terms.intersection(evidence_terms))


def _chrome_executable() -> Optional[str]:
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def _render_report_pdf(html: str) -> bytes:
    executable = _chrome_executable()
    if not executable:
        raise RuntimeError("未找到可用于生成 PDF 的 Chrome 或 Edge")
    with tempfile.TemporaryDirectory(prefix="wechat-report-") as directory:
        root = Path(directory)
        html_path = root / "report.html"
        pdf_path = root / "report.pdf"
        html_path.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [
                executable,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-pdf-header-footer",
                "--print-to-pdf=%s" % pdf_path,
                html_path.resolve().as_uri(),
            ],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0 or not pdf_path.is_file():
            detail = result.stderr.decode("utf-8", errors="ignore").strip()[-500:]
            raise RuntimeError("PDF 生成失败%s" % (("：" + detail) if detail else ""))
        return pdf_path.read_bytes()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def _rule_json(rule: ReplyRule) -> Dict[str, Any]:
    return {
        "name": rule.name,
        "enabled": rule.enabled,
        "reply_text": rule.reply_text,
        "keywords": list(rule.keywords),
        "regexes": list(rule.regexes),
        "chats": list(rule.chats),
        "senders": list(rule.senders),
        "message_types": list(rule.message_types),
        "time_ranges": [
            {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
            for start, end in rule.time_ranges
        ],
    }


def _date_range(
    query: Dict[str, Any],
    timezone_name: str,
) -> tuple:
    """Parse inclusive local dates into an aware UTC half-open interval."""

    tz = get_timezone(timezone_name)
    today = datetime.now(tz).date()
    period = str((query.get("period") or [""])[0]).strip().lower()
    start_raw = str((query.get("start") or [""])[0]).strip()
    end_raw = str((query.get("end") or [""])[0]).strip()
    if period == "week" and not start_raw and not end_raw:
        start_day = today - timedelta(days=6)
        end_day = today
    elif period == "day" and not start_raw and not end_raw:
        start_day = end_day = today
    else:
        try:
            start_day = date.fromisoformat(start_raw[:10]) if start_raw else today
            end_day = date.fromisoformat(end_raw[:10]) if end_raw else start_day
        except ValueError as exc:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
    if end_day < start_day:
        raise ValueError("结束日期不能早于开始日期")
    start_at = datetime.combine(start_day, datetime_time.min, tzinfo=tz)
    end_at = datetime.combine(end_day + timedelta(days=1), datetime_time.min, tzinfo=tz)
    return start_at.astimezone(timezone.utc), end_at.astimezone(timezone.utc), start_day, end_day


_WORKBENCH_ROW_LIMIT = 200_000
_FEED_CURSOR_PREFIX = "v1."
_INTERNAL_NAME_RE = re.compile(r"^(?:wxid_|gh_)[\w-]+$", re.IGNORECASE)


def _message_timestamp(value: Any) -> datetime:
    """Normalize a stored timestamp for stable ordering and cursors."""

    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            result = datetime.min.replace(tzinfo=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _row_id(value: Mapping[str, Any]) -> int:
    try:
        return int(value.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _is_self_message(item: Mapping[str, Any]) -> Optional[bool]:
    value = item.get("is_self")
    if value is True or (isinstance(value, int) and value == 1):
        return True
    if value is False or (isinstance(value, int) and value == 0):
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _is_internal_name(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or bool(_INTERNAL_NAME_RE.fullmatch(text)) or text.isdigit()


def _safe_name(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return None if _is_internal_name(text) else text


def _chat_display_name(item: Mapping[str, Any]) -> str:
    value = _safe_name(item.get("chat_name"))
    if value:
        return value
    return "群聊" if bool(item.get("is_group")) else "未命名会话"


def _sender_display_name(item: Mapping[str, Any]) -> str:
    if _is_self_message(item) is True:
        return "我"
    value = _safe_name(item.get("sender_name"))
    if value:
        return value
    if not bool(item.get("is_group")):
        return _chat_display_name(item)
    return "待识别成员"


def _content_preview(item: Mapping[str, Any]) -> str:
    content = str(item.get("content") or "").strip()
    if content:
        return content
    message_type = str(item.get("message_type") or "other").strip()
    return "[%s]" % (message_type or "其他")


def _is_media_message(item: Mapping[str, Any]) -> bool:
    message_type = str(item.get("message_type") or "text").strip().lower()
    if message_type not in {"text", "other", "system"}:
        return True
    content = str(item.get("content") or "").strip()
    return bool(re.match(r"^\s*\[(?:图片|语音|视频|动画表情|文件/链接/卡片|文件|链接|卡片)(?:\s+[^\]]+)?\]\s*$", content))


def _load_message_window(
    store: Any,
    query: Dict[str, Any],
    timezone_name: str,
    *,
    default_all: bool,
    limit: int = _WORKBENCH_ROW_LIMIT,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], datetime, datetime]:
    """Load one bounded archive window without adding methods to SQLiteStore."""

    has_explicit_range = any(key in query for key in ("start", "end", "period"))
    if has_explicit_range or not default_all:
        start_at, end_at, start_day, end_day = _date_range(query, timezone_name)
        window = {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "timezone": timezone_name,
            "mode": "date_range",
        }
    else:
        # SQLiteStore intentionally caps recent_messages at 500.  A broad
        # date query gives the workbench a bounded archive read without
        # reaching into the store's private SQLite connection.
        start_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        end_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        window = {
            "start": None,
            "end": None,
            "timezone": timezone_name,
            "mode": "archive",
        }
    chat = str((query.get("chat") or [""])[0]).strip() or None
    messages = store.messages_between(start_at, end_at, chat, limit)
    return messages, window, start_at, end_at


def _runtime_snapshot(service: Any) -> Dict[str, Any]:
    try:
        value = service.status_snapshot()
        return dict(value) if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("workbench runtime status unavailable: %s", exc)
        return {
            "started": False,
            "receiving": False,
            "status_error": str(exc),
        }


def _live_chat_values(status: Dict[str, Any], service: Any) -> set:
    values = status.get("chats")
    if not isinstance(values, (list, tuple, set)):
        values = getattr(service, "chat_names", ())
    result = {str(value).strip() for value in values if str(value).strip()}
    if "文件传输助手" in result:
        result.add("filehelper")
    if "filehelper" in result:
        result.add("文件传输助手")
    return result


def _chat_is_live(item: Mapping[str, Any], live_values: set) -> bool:
    return bool(
        str(item.get("chat_id") or "").strip() in live_values
        or str(item.get("chat_name") or "").strip() in live_values
    )


def _capture_state(
    *,
    is_live: bool,
    receiving: bool,
    sync_state: str,
    has_messages: bool,
) -> str:
    if is_live and receiving:
        return "fresh"
    if sync_state in {"running", "failed"}:
        return "partial"
    if has_messages:
        return "stale"
    return "unknown"


def _scope_payload(status: Dict[str, Any], service: Any) -> Dict[str, Any]:
    live_values = _live_chat_values(status, service)
    receiving = bool(status.get("receiving"))
    sync = status.get("sync") if isinstance(status.get("sync"), dict) else {}
    sync_state = str(sync.get("state") or "unknown")
    live_names = [
        value
        for value in (status.get("chats") or getattr(service, "chat_names", ()))
        if str(value).strip()
    ]
    history_label = str(status.get("history_scope") or "全部可读会话")
    realtime_state = "fresh" if receiving and live_values else "unknown"
    history_state = "partial" if sync_state in {"running", "failed"} else "unknown"
    realtime = {
        "mode": "live",
        "label": str(status.get("live_scope") or "、".join(map(str, live_names)) or "—"),
        "chats": [str(value) for value in live_names],
        "receiving": receiving,
        "capture_state": realtime_state,
    }
    history = {
        "mode": "history",
        "label": history_label,
        "scope": "all_readable_chats",
        "capture_state": history_state,
        "last_sync_at": status.get("last_sync_at"),
        "sync_state": sync_state,
    }
    return {
        "realtime": realtime,
        "history": history,
        "live_values": sorted(live_values),
        "sync_state": sync_state,
    }


def _quality_with_scope(
    quality: Dict[str, Any],
    status: Dict[str, Any],
    scope: Dict[str, Any],
) -> Dict[str, Any]:
    value = dict(quality or {})
    value.setdefault("capture_completeness", None)
    value.setdefault("capture_completeness_state", "unknown")
    value["realtime_scope"] = scope["realtime"]
    value["history_scope"] = scope["history"]
    value["sync_state"] = scope["sync_state"]
    value["last_sync_at"] = status.get("last_sync_at")
    if value.get("capture_completeness") is None:
        value["capture_completeness_state"] = "unknown"
    return value


def _feed_cursor(item: Mapping[str, Any]) -> str:
    payload = {
        "timestamp": _message_timestamp(item.get("timestamp")).isoformat(timespec="microseconds"),
        "row_id": _row_id(item),
        "message_id": str(item.get("message_id") or ""),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return _FEED_CURSOR_PREFIX + encoded


def _decode_feed_cursor(
    value: Any,
    timezone_name: str,
) -> Tuple[datetime, int, str]:
    text = str(value or "").strip()
    if not text.startswith(_FEED_CURSOR_PREFIX):
        raise ValueError("cursor 格式无效")
    encoded = text[len(_FEED_CURSOR_PREFIX):]
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        timestamp = datetime.fromisoformat(str(payload["timestamp"]))
        row_id = int(payload.get("row_id", 0))
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor 格式无效") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=get_timezone(timezone_name))
    if row_id < 0:
        raise ValueError("cursor 格式无效")
    return timestamp.astimezone(timezone.utc), row_id, str(payload.get("message_id") or "")


def _decode_before(value: Any, timezone_name: str) -> Optional[Tuple[datetime, int]]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(_FEED_CURSOR_PREFIX):
        return _decode_feed_cursor(text, timezone_name)
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("before 必须是 ISO 时间或有效 cursor") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=get_timezone(timezone_name))
    return timestamp.astimezone(timezone.utc), 0


def _feed_filter_name(value: Any) -> str:
    normalized = str(value or "all").strip().lower()
    aliases = {
        "": "all",
        "all": "all",
        "inbound": "inbound",
        "received": "inbound",
        "incoming": "inbound",
        "self": "self",
        "outbound": "self",
        "high": "high_signal",
        "high_signal": "high_signal",
        "important": "high_signal",
        "attention": "attention",
        "needs_attention": "attention",
        "media": "media",
        "text": "text",
        "unresolved": "unresolved",
    }
    if normalized not in aliases:
        raise ValueError("不支持的 filter：%s" % normalized)
    return aliases[normalized]


def _feed_filter_matches(item: Mapping[str, Any], filter_name: str) -> bool:
    direction = _is_self_message(item)
    if filter_name == "all":
        return True
    if filter_name == "inbound":
        return direction is False
    if filter_name == "self":
        return direction is True
    if filter_name == "media":
        return _is_media_message(item)
    if filter_name == "text":
        return not _is_media_message(item)
    if filter_name == "unresolved":
        return direction is False and _is_internal_name(item.get("sender_name"))
    score = _score_message(item)
    if filter_name == "high_signal":
        return direction is False and score.get("level") == "high"
    if filter_name == "attention":
        return direction is False and score.get("level") in {"high", "medium"}
    return True


def _feed_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    value = dict(item)
    value.pop("id", None)
    value["chat_name"] = _chat_display_name(item)
    value["sender_name"] = _sender_display_name(item)
    score = _score_message(item)
    value["signal"] = {
        "level": score.get("level"),
        "score": score.get("score", 0),
        "value_label": score.get("value_label"),
        "tags": list(score.get("tags") or []),
        "reason": score.get("reason"),
    }
    value["display_chat_name"] = value["chat_name"]
    value["display_sender_name"] = value["sender_name"]
    return value


def _contact_choice(
    item: Mapping[str, Any],
) -> List[Tuple[str, str, int]]:
    source = str(item.get("sender_name_source") or "observed").strip() or "observed"
    priorities = {
        "contact_remark": 100,
        "group_nickname": 90,
        "contact_nickname": 80,
        "direct_chat_peer": 75,
        "chat_name": 60,
        "observed": 20,
    }
    options: List[Tuple[str, str, int]] = []
    sender = _safe_name(item.get("sender_name"))
    if sender:
        options.append((sender, source, priorities.get(source, 20)))
    if not bool(item.get("is_group")):
        chat_name = _safe_name(item.get("chat_name"))
        if chat_name and chat_name != sender:
            options.append((chat_name, "chat_name", priorities["chat_name"]))
    return options


def _contact_key(item: Mapping[str, Any]) -> Tuple[str, ...]:
    chat_id = str(item.get("chat_id") or item.get("chat_name") or "unknown").strip()
    if bool(item.get("is_group")):
        # Group sender ids are scoped to the chat by the adapter.  Keeping the
        # chat in the key avoids reintroducing the historic cross-group merge.
        sender_id = str(item.get("sender_id") or "").strip()
        safe_sender = _safe_name(item.get("sender_name")) or "pending"
        return ("group", chat_id, sender_id or safe_sender)
    return ("direct", chat_id)


def _contact_id(key: Sequence[str]) -> str:
    digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()
    return "contact-%s" % digest[:16]


class BridgeHttpServer(ThreadingHTTPServer):
    """HTTP server bound to loopback by default."""

    daemon_threads = True
    allow_reuse_address = True
    # The dashboard loads several read-only endpoints in parallel.  The
    # socketserver default backlog is only five, so a slow overview/messages
    # response can cause later valid requests to be refused before a handler
    # is even created.  Keep enough headroom for one browser refresh plus a
    # background poller.
    request_queue_size = 64

    def __init__(self, address, service, shadow_catalog_path: Optional[Union[str, Path]] = None) -> None:
        self.service = service
        self.store = service.store
        self.settings = WorkbenchSettings.for_service(service)
        # Shadow runs are review-only, in-memory artifacts.  No production
        # selector or homepage path reads this store unless a future explicit
        # review request asks for a run by its opaque analysis_run_id.
        self.shadow_run_store = ShadowSemanticRunStore()
        self.shadow_store = self.shadow_run_store
        # Development shadow artifacts are review-only and must be supplied
        # explicitly.  An ordinary dashboard server never scans the
        # filesystem for runs and therefore keeps the existing production
        # behavior unchanged.
        self.shadow_catalog_path = str(shadow_catalog_path) if shadow_catalog_path is not None else None
        self.shadow_catalog = (
            tuple(load_shadow_catalog(shadow_catalog_path))
            if shadow_catalog_path is not None
            else ()
        )
        self.latest_ai_analysis: Optional[Dict[str, Any]] = None
        self.ai_analysis_by_window: Dict[str, Dict[str, Any]] = {}
        self.ai_raw_by_window: Dict[str, Dict[str, Any]] = {}
        self.ai_packet_meta_by_window: Dict[str, Dict[str, Any]] = {}
        self.ai_analysis_lock = threading.Lock()
        self.fact_checks_by_window: Dict[str, Dict[str, Any]] = {}
        self.fact_check_lock = threading.Lock()
        super().__init__(address, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.info("dashboard %s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_asset("index.html", "text/html; charset=utf-8")
        if parsed.path == "/assets/styles.css":
            return self._serve_asset("styles.css", "text/css; charset=utf-8")
        if parsed.path == "/assets/polish.css":
            return self._serve_asset("polish.css", "text/css; charset=utf-8")
        if parsed.path == "/assets/app.js":
            return self._serve_asset("app.js", "application/javascript; charset=utf-8")
        if parsed.path.startswith("/assets/editorial/"):
            relative_name = unquote(parsed.path[len("/assets/editorial/"):]).replace("\\", "/")
            if not relative_name or ".." in relative_name.split("/"):
                return self._json({"error": "forbidden"}, 403)
            suffix = Path(relative_name).suffix.lower()
            content_type = {
                ".otf": "font/otf",
                ".ttf": "font/ttf",
                ".woff2": "font/woff2",
                ".svg": "image/svg+xml",
                ".txt": "text/plain; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            return self._serve_asset("assets/editorial/" + relative_name, content_type)
        if parsed.path == "/api/status":
            return self._json(self._status())
        if parsed.path == "/api/settings":
            return self._json(self.server.settings.public())
        if parsed.path == "/api/ai-status":
            return self._json(self._ai_status())
        if parsed.path == "/api/shadow-analysis":
            return self._shadow_analysis(parse_qs(parsed.query))
        if parsed.path == "/api/ai-latest":
            latest_query = parse_qs(parsed.query)
            start = str((latest_query.get("start") or [""])[0]).strip()
            end = str((latest_query.get("end") or [start])[0]).strip()
            cached = self.server.ai_analysis_by_window.get("%s|%s" % (start, end)) if start else None
            return self._json(cached or self.server.latest_ai_analysis or {"ok": False, "state": "empty"})
        if parsed.path == "/api/fact-check":
            query = parse_qs(parsed.query)
            start = str((query.get("start") or [""])[0]).strip()
            end = str((query.get("end") or [start])[0]).strip()
            chat = str((query.get("chat") or [""])[0]).strip()
            topic = str((query.get("topic") or [""])[0]).strip()
            prefix = "%s\x1f%s\x1f%s\x1f" % (start, end, chat)
            items = [
                value
                for key, value in self.server.fact_checks_by_window.items()
                if key.startswith(prefix)
                and (not topic or key == prefix + topic)
            ]
            return self._json({"ok": True, "state": "empty" if not items else "checked", "items": items})
        if parsed.path == "/api/brief-feedback":
            return self._json({"items": self.server.store.brief_feedback()})
        if parsed.path == "/api/voice-transcript":
            message_id = str((parse_qs(parsed.query).get("message_id") or [""])[0]).strip()
            if not message_id:
                return self._json({"error": "missing_message_id", "message": "缺少 message_id"}, 400)
            return self._json({"item": self.server.store.voice_transcript(message_id)})
        if parsed.path == "/api/voice-audio":
            return self._voice_audio(parse_qs(parsed.query))
        if parsed.path == "/api/sync-status":
            return self._json(self.server.service.history_sync_status())
        if parsed.path == "/api/overview":
            return self._overview(parse_qs(parsed.query))
        if parsed.path == "/api/feed":
            return self._feed(parse_qs(parsed.query))
        if parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            chat = (query.get("chat") or [None])[0]
            limit = self._int_query(query, "limit", 200, cap=50_000)
            try:
                if any(key in query for key in ("start", "end", "period")):
                    start_at, end_at, start_day, end_day = _date_range(
                        query,
                        self.server.service.policy.timezone_name,
                    )
                    items = self._voice_enrich(self.server.store.messages_between(start_at, end_at, chat, limit))
                    return self._json(
                        {
                            "items": items,
                            "window": {
                                "start": start_day.isoformat(),
                                "end": end_day.isoformat(),
                                "timezone": self.server.service.policy.timezone_name,
                            },
                        }
                    )
            except ValueError as exc:
                return self._json({"error": "invalid_range", "message": str(exc)}, 400)
            return self._json({"items": self._voice_enrich(self.server.store.recent_messages(chat, limit))})
        if parsed.path == "/api/insights":
            query = parse_qs(parsed.query)
            try:
                start_at, end_at, start_day, end_day = _date_range(
                    query,
                    self.server.service.policy.timezone_name,
                )
                limit = self._int_query(query, "limit", 50_000, cap=200_000)
                chat = (query.get("chat") or [None])[0]
                messages = self.server.store.messages_between(start_at, end_at, chat, limit)
                value = analyze_messages(
                    messages,
                    start_at,
                    end_at,
                    self.server.service.policy.timezone_name,
                )
                fact_checks = self._fact_checks_for_window(
                    start_day.isoformat(), end_day.isoformat(), str((query.get("chat") or [""])[0] or "")
                )
                self._attach_fact_checks(value, fact_checks)
                value["window"]["start_date"] = start_day.isoformat()
                value["window"]["end_date"] = end_day.isoformat()
                return self._json(value)
            except ValueError as exc:
                return self._json({"error": "invalid_range", "message": str(exc)}, 400)
        if parsed.path == "/api/sync":
            return self._json({"error": "method_not_allowed", "message": "请使用 POST /api/sync"}, 405)
        if parsed.path == "/api/tasks":
            query = parse_qs(parsed.query)
            status = (query.get("status") or [None])[0]
            limit = self._int_query(query, "limit", 50)
            return self._json({"items": self.server.store.list_tasks(status, limit)})
        if parsed.path == "/api/chats":
            return self._chats(parse_qs(parsed.query))
        if parsed.path == "/api/contacts":
            return self._contacts(parse_qs(parsed.query))
        if parsed.path == "/api/media":
            return self._media(parse_qs(parsed.query))
        if parsed.path.startswith("/api/media/"):
            message_id = unquote(parsed.path[len("/api/media/"):])
            return self._media({"message_id": [message_id]})
        if parsed.path == "/api/sync-runs":
            return self._sync_runs(parse_qs(parsed.query))
        if parsed.path == "/api/rules":
            return self._json(
                {
                    "timezone": self.server.service.policy.timezone_name,
                    "items": [
                        _rule_json(rule)
                        for rule in self.server.service.policy.rules
                    ],
                }
            )
        if parsed.path == "/api/accounts":
            try:
                accounts = self.server.service.adapter.list_accounts()
                return self._json({"items": accounts})
            except Exception as exc:
                return self._json(
                    {"items": [], "error": str(exc)}, status=200
                )
        self._json({"error": "not_found", "message": "资源不存在"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._json({"error": "invalid_json", "message": str(exc)}, 400)
        if parsed.path == "/api/settings":
            try:
                value = self.server.settings.update(body)
            except (TypeError, ValueError, OSError) as exc:
                return self._json({"error": "invalid_settings", "message": str(exc)}, 400)
            return self._json({"ok": True, "settings": value})
        if parsed.path == "/api/report-render":
            return self._report_render(body)
        if parsed.path == "/api/report-email":
            return self._report_email(body)
        if parsed.path == "/api/brief-feedback":
            try:
                value = self.server.store.save_brief_feedback(
                    str(body.get("event_id") or ""),
                    str(body.get("action") or ""),
                    str(body.get("details") or "") or None,
                )
            except (TypeError, ValueError) as exc:
                return self._json({"error": "invalid_feedback", "message": str(exc)}, 400)
            return self._json({"ok": True, "feedback": value})
        if parsed.path == "/api/voice-transcribe":
            return self._voice_transcribe(body)
        if parsed.path == "/api/voice-correct":
            message_id = str(body.get("message_id") or "").strip()
            transcript = str(body.get("transcript") or "").strip()
            if not message_id or not transcript:
                return self._json({"error": "invalid_correction", "message": "缺少消息或校对文本"}, 400)
            try:
                value = self.server.store.save_voice_transcript(
                    message_id, status="corrected", transcript=transcript, manual=True
                )
            except ValueError as exc:
                return self._json({"error": "invalid_correction", "message": str(exc)}, 400)
            return self._json({"ok": True, "transcript": value})
        if parsed.path == "/api/auto-reply":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return self._json(
                    {"error": "invalid_enabled", "message": "enabled 必须是布尔值"},
                    400,
                )
            if enabled:
                self.server.service.resume()
            else:
                self.server.service.pause()
            return self._json(self._status())
        if parsed.path == "/api/sync":
            try:
                limit = int(body.get("limit", 100))
                result = self.server.service.sync_recent_history(limit)
                return self._json({"ok": True, **result})
            except (TypeError, ValueError) as exc:
                return self._json({"error": "invalid_limit", "message": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": "sync_failed", "message": str(exc)}, 502)
        if parsed.path == "/api/sync-range":
            try:
                query = {
                    "start": [str(body.get("start") or body.get("start_date") or "")],
                    "end": [str(body.get("end") or body.get("end_date") or "")],
                    "period": [str(body.get("period") or "")],
                }
                start_at, end_at, start_day, end_day = _date_range(
                    query,
                    self.server.service.policy.timezone_name,
                )
                result = self.server.service.start_history_sync(
                    start_at,
                    end_at,
                    scope=str(body.get("scope") or "all"),
                    limit=int(body.get("limit", 50_000)),
                )
                result["window"] = {
                    "start": start_day.isoformat(),
                    "end": end_day.isoformat(),
                    "timezone": self.server.service.policy.timezone_name,
                }
                return self._json({"ok": True, **result}, 202)
            except (TypeError, ValueError) as exc:
                return self._json({"error": "invalid_range", "message": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": "sync_failed", "message": str(exc)}, 502)
        if parsed.path == "/api/ai-analysis":
            return self._ai_analysis(body)
        if parsed.path == "/api/fact-check":
            return self._fact_check(body)
        if parsed.path == "/api/preview":
            return self._preview(body)
        if parsed.path == "/api/rules":
            return self._replace_rules(body)
        if parsed.path == "/api/retry":
            try:
                task_id = int(body.get("task_id"))
            except (TypeError, ValueError):
                return self._json(
                    {"error": "invalid_task_id", "message": "task_id 必须是整数"},
                    400,
                )
            if not self.server.service.retry_task(task_id):
                return self._json(
                    {"error": "retry_not_allowed", "message": "任务不存在、不是失败态或超出测试范围"},
                    409,
                )
            return self._json({"ok": True, "task_id": task_id})
        if parsed.path == "/api/send-text":
            return self._manual_send(body)
        self._json({"error": "not_found", "message": "资源不存在"}, status=404)

    def _voice_transcribe(self, body: Mapping[str, Any]) -> None:
        message_id = str(body.get("message_id") or "").strip()
        message = self.server.store.get_message(message_id) if message_id else None
        if not message or str(message.get("message_type") or "") != "voice":
            return self._json({"error": "voice_not_found", "message": "没有找到这条语音消息"}, 404)
        raw = message.get("raw_message") if isinstance(message.get("raw_message"), Mapping) else {}
        raw_message = raw.get("message") if isinstance(raw.get("message"), Mapping) else raw
        native_text = extract_wechat_voice_transcript(raw_message.get("_bridge_packed_info"))
        if native_text:
            value = self.server.store.save_voice_transcript(
                message_id,
                status="succeeded",
                transcript=native_text,
                provider="wechat_native",
                audio_path=message.get("media_path"),
            )
            return self._json({"ok": True, "transcript": value})
        voice_settings = self.server.settings.snapshot(include_secrets=True).get("voice") or {}
        if not voice_settings.get("enabled"):
            return self._json({"error": "voice_disabled", "message": "请先在设置中启用语音识别"}, 409)
        app_id = str(voice_settings.get("app_id") or "").strip()
        access_token = str(voice_settings.get("access_token") or "").strip()
        if not app_id or not access_token:
            return self._json({"error": "voice_not_configured", "message": "豆包 APP ID 或 Access Token 未配置"}, 409)
        cache_root = Path(
            str((self.server.settings.snapshot(include_secrets=True).get("media") or {}).get("cache_dir") or "")
        ) / "voice"
        cache_root.mkdir(parents=True, exist_ok=True)
        silk_path = str(message.get("media_path") or "").strip()
        try:
            if not silk_path or not Path(silk_path).is_file():
                local_id = raw_message.get("local_id")
                exporter = getattr(self.server.service.adapter, "export_voice", None)
                if local_id is None or not callable(exporter):
                    raise ValueError("当前消息缺少可定位的语音索引")
                silk_path = str(exporter(message.get("chat_id"), int(local_id), str(cache_root)) or "")
            if not silk_path or not Path(silk_path).is_file():
                raise ValueError("微信媒体库中没有找到对应语音数据")
            wav_path = cache_root / (hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:20] + ".wav")
            wav_bytes = decode_silk_to_wav(silk_path, wav_path)
            result = DoubaoASRClient(app_id=app_id, access_token=access_token).transcribe(
                wav_bytes, audio_format="wav", uid="wechat-bridge"
            )
            confidences = []
            for utterance in result.utterances:
                if isinstance(utterance, Mapping):
                    try:
                        confidences.append(float(utterance.get("confidence")))
                    except (TypeError, ValueError):
                        pass
            confidence = sum(confidences) / len(confidences) if confidences else None
            value = self.server.store.save_voice_transcript(
                message_id,
                status="succeeded",
                transcript=result.text,
                duration_ms=result.duration_ms,
                confidence=confidence,
                provider="doubao_asr_v2",
                audio_path=str(wav_path),
            )
            return self._json({"ok": True, "transcript": value})
        except (ASRError, OSError, TypeError, ValueError) as exc:
            value = self.server.store.save_voice_transcript(
                message_id,
                status="failed",
                provider="doubao_asr_v2",
                audio_path=silk_path or None,
                error=str(exc),
            )
            return self._json(
                {"error": "voice_transcription_failed", "message": "语音提取或识别失败", "transcript": value},
                502,
            )

    def _voice_audio(self, query: Mapping[str, Any]) -> None:
        message_id = str((query.get("message_id") or [""])[0]).strip()
        record = self.server.store.voice_transcript(message_id) if message_id else None
        path = Path(str((record or {}).get("audio_path") or "")).expanduser()
        cache_dir = Path(
            str((self.server.settings.snapshot(include_secrets=True).get("media") or {}).get("cache_dir") or "")
        ).expanduser()
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(cache_dir.resolve()):
                raise OSError
            data = resolved.read_bytes()
        except (OSError, ValueError):
            return self._json({"error": "voice_audio_unavailable", "message": "转码音频尚不可用"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before voice response completed")

    def _overview(self, query: Dict[str, Any]) -> None:
        try:
            timezone_name = self.server.service.policy.timezone_name
            messages, window, start_at, end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=False,
                limit=_WORKBENCH_ROW_LIMIT,
            )
        except ValueError as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        status = _runtime_snapshot(self.server.service)
        scope = _scope_payload(status, self.server.service)
        public_scope = {
            key: value for key, value in scope.items() if key != "live_values"
        }
        analysis = analyze_messages(
            self._analysis_enrich(messages),
            start_at,
            end_at,
            timezone_name,
            profile=(self.server.settings.snapshot().get("profile") or {}),
        )
        fact_checks = self._fact_checks_for_window(
            str(window.get("start") or ""), str(window.get("end") or ""), str((query.get("chat") or [""])[0] or "")
        )
        self._attach_fact_checks(analysis, fact_checks)
        feedback_by_id = {
            item["event_id"]: item
            for item in self.server.store.brief_feedback(
                [str(event.get("id") or "") for event in analysis.get("event_briefs") or []]
            )
        }
        for event in analysis.get("event_briefs") or []:
            event["feedback"] = feedback_by_id.get(str(event.get("id") or ""))
        quality = _quality_with_scope(
            analysis.get("quality") or {},
            status,
            scope,
        )
        freshness = dict(analysis.get("freshness") or {})
        freshness.update(
            {
                "realtime": public_scope["realtime"],
                "history": public_scope["history"],
                "sync_state": public_scope["sync_state"],
            }
        )
        highlights = list(analysis.get("highlights") or [])
        actions = list(analysis.get("actions") or [])
        payload = {
                "ok": True,
                "window": window,
                "summary": analysis.get("summary") or {},
                "narrative": analysis.get("narrative") or "",
                "situation": dict(analysis.get("situation") or {}),
                "method": analysis.get("method") or {},
                "events": list(analysis.get("events") or []),
                "highlight_candidates": highlights,
                "pending_candidates": actions,
                "hourly": list(analysis.get("hourly") or []),
                "top_chats": list(analysis.get("top_chats") or []),
                "topics": list(analysis.get("topics") or []),
                "types": list(analysis.get("types") or []),
                "discoveries": list(analysis.get("discoveries") or []),
                "discussion_episodes": list(analysis.get("discussion_episodes") or []),
                "topic_briefs": list(analysis.get("topic_briefs") or []),
                "primary_insights": list(analysis.get("primary_insights") or []),
                 "event_briefs": list(analysis.get("event_briefs") or []),
                 "for_me": list(analysis.get("for_me") or []),
                 "trending": list(analysis.get("trending") or []),
                 "pending_review": list(analysis.get("pending_review") or []),
                 "unformed_dynamics": list(analysis.get("unformed_dynamics") or []),
                 "fact_checks": fact_checks,
                 "insight_breakdown": list(analysis.get("insight_breakdown") or []),
                "activity": dict(analysis.get("activity") or {}),
                # Keep the analysis names as aliases so the existing dashboard
                # can adopt the new contract incrementally.
                "highlights": highlights,
                "actions": actions,
                "quality": quality,
                "freshness": freshness,
                "scope": public_scope,
                "source": "local_sqlite",
                "read_only": True,
            }
        return self._json(payload)

    def _voice_enrich(self, items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        values = [dict(item) for item in items]
        for item in values:
            if str(item.get("message_type") or "") == "voice":
                transcript = self.server.store.voice_transcript(
                    str(item.get("message_id") or "")
                )
                raw = item.get("raw_message") if isinstance(item.get("raw_message"), Mapping) else {}
                raw_message = raw.get("message") if isinstance(raw.get("message"), Mapping) else raw
                native_text = extract_wechat_voice_transcript(raw_message.get("_bridge_packed_info"))
                if transcript is None and native_text:
                    transcript = {
                        "status": "available",
                        "transcript": native_text,
                        "provider": "wechat_native",
                    }
                item["voice_transcript"] = transcript
        return values

    def _analysis_enrich(self, items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Promote available voice transcripts to analyzable text on copied rows."""

        values = self._voice_enrich(items)
        for item in values:
            if str(item.get("message_type") or "").lower() != "voice":
                continue
            record = item.get("voice_transcript")
            transcript = (
                str(record.get("transcript") or "").strip()
                if isinstance(record, Mapping)
                else str(record or "").strip()
            )
            if not transcript:
                continue
            item["original_content"] = item.get("content")
            item["content"] = transcript
            item["_transcribed_voice"] = True
        return values

    def _fact_check_cache_key(self, start: str, end: str, chat: str, topic: str) -> str:
        return "%s\x1f%s\x1f%s\x1f%s" % (start, end, chat, topic)

    def _fact_checks_for_window(self, start: str, end: str, chat: str = "") -> List[Dict[str, Any]]:
        prefix = "%s\x1f%s\x1f%s\x1f" % (start, end, chat)
        with self.server.fact_check_lock:
            return [
                dict(value)
                for key, value in self.server.fact_checks_by_window.items()
                if key.startswith(prefix)
            ]

    def _attach_fact_checks(
        self,
        analysis: Dict[str, Any],
        fact_checks: Sequence[Mapping[str, Any]],
    ) -> None:
        """Attach cached web checks without making normal refreshes networked."""

        analysis["fact_checks"] = [dict(value) for value in fact_checks]
        by_topic = {
            str(value.get("topic") or ""): value
            for value in fact_checks
            if str(value.get("topic") or "").strip()
        }
        for item in analysis.get("topic_briefs") or []:
            topic = str(item.get("topic") or "")
            if topic in by_topic:
                item["fact_check"] = by_topic[topic]

    def _fact_check(self, body: Dict[str, Any]) -> None:
        if body.get("confirm") is not True:
            return self._json(
                {
                    "error": "confirmation_required",
                    "message": "联网核验会把公开对象和核验词发送到搜索服务，必须显式 confirm=true",
                },
                400,
            )
        try:
            query = {
                "start": [str(body.get("start") or body.get("start_date") or "")],
                "end": [str(body.get("end") or body.get("end_date") or "")],
                "period": [str(body.get("period") or "")],
            }
            start_at, end_at, start_day, end_day = _date_range(
                query,
                self.server.service.policy.timezone_name,
            )
            max_claims = max(1, min(int(body.get("limit", 6)), 12))
        except (TypeError, ValueError) as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        chat = str(body.get("chat") or "").strip()
        topic = str(body.get("topic") or "").strip()
        start_text = start_day.isoformat()
        end_text = end_day.isoformat()
        cache_key = self._fact_check_cache_key(start_text, end_text, chat, topic)
        with self.server.fact_check_lock:
            cached = self.server.fact_checks_by_window.get(cache_key)
        if cached is not None and body.get("force") is not True:
            return self._json(cached)

        messages = self._analysis_enrich(
            self.server.store.messages_between(start_at, end_at, chat or None, 200_000)
        )
        baseline = analyze_messages(
            messages,
            start_at,
            end_at,
            self.server.service.policy.timezone_name,
        )
        selected_ids: set = set()
        if topic:
            for item in list(baseline.get("topic_briefs") or []):
                if str(item.get("topic") or "") != topic:
                    continue
                selected_ids.update(str(value) for value in item.get("source_message_ids") or [] if value)
                detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
                selected_ids.update(
                    str(entry.get("message_id") or "")
                    for entry in detail.get("timeline") or []
                    if isinstance(entry, Mapping) and entry.get("message_id")
                )
            for item in list(baseline.get("event_briefs") or []):
                labels = [
                    str(item.get("title") or ""),
                    *[str(value) for value in item.get("tags") or []],
                    *[str(value) for value in item.get("canonical_topics") or []],
                ]
                if topic in labels or any(topic and topic in label for label in labels):
                    selected_ids.update(str(value) for value in item.get("message_ids") or [] if value)
        message_by_id = {
            str(item.get("message_id") or ""): item
            for item in messages
            if item.get("message_id")
        }
        if selected_ids:
            source_values = [
                _display_content(message_by_id[message_id].get("content"))
                for message_id in selected_ids
                if message_id in message_by_id
            ]
        else:
            source_values = [
                _display_content(item.get("content"))
                for item in messages
                if _display_content(item.get("content"))
            ][:200]

        endpoint = str(os.environ.get("WECHAT_FACT_CHECK_ENDPOINT") or "").strip()
        provider = HttpSearchProvider(endpoint=endpoint or "https://html.duckduckgo.com/html/")
        result = check_claims(source_values, provider, topic=topic, max_claims=max_claims)
        result["window"] = {
            "start": start_text,
            "end": end_text,
            "timezone": self.server.service.policy.timezone_name,
        }
        result["chat"] = chat or None
        with self.server.fact_check_lock:
            self.server.fact_checks_by_window[cache_key] = result
        return self._json(result)

    def _feed(self, query: Dict[str, Any]) -> None:
        timezone_name = self.server.service.policy.timezone_name
        try:
            filter_name = _feed_filter_name((query.get("filter") or ["all"])[0])
            limit = self._int_query(query, "limit", 50, cap=500)
            messages, window, _start_at, _end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=True,
                limit=_WORKBENCH_ROW_LIMIT,
            )
            boundary: Optional[Tuple[datetime, int, Optional[str]]] = None
            cursor_value = (query.get("cursor") or [""])[0]
            if cursor_value:
                timestamp, row_id, message_id = _decode_feed_cursor(
                    cursor_value,
                    timezone_name,
                )
                boundary = (timestamp, row_id, message_id)
            else:
                before_value = (query.get("before") or [""])[0]
                if before_value:
                    timestamp, row_id = _decode_before(before_value, timezone_name)
                    boundary = (timestamp, row_id, None)
        except ValueError as exc:
            code = "invalid_filter" if "filter" in str(exc) else "invalid_cursor"
            if "日期" in str(exc) or "结束日期" in str(exc):
                code = "invalid_range"
            return self._json({"error": code, "message": str(exc)}, 400)

        messages = self._voice_enrich(messages)
        filtered = [
            item for item in messages if _feed_filter_matches(item, filter_name)
        ]
        filtered.sort(
            key=lambda item: (
                _message_timestamp(item.get("timestamp")),
                _row_id(item),
                str(item.get("message_id") or ""),
            ),
            reverse=True,
        )
        if boundary is not None:
            boundary_timestamp, boundary_row_id, boundary_message_id = boundary

            def before_boundary(item: Mapping[str, Any]) -> bool:
                timestamp = _message_timestamp(item.get("timestamp"))
                if timestamp < boundary_timestamp:
                    return True
                if timestamp > boundary_timestamp or boundary_message_id is None:
                    return False
                return (
                    _row_id(item),
                    str(item.get("message_id") or ""),
                ) < (boundary_row_id, boundary_message_id)

            filtered = [item for item in filtered if before_boundary(item)]

        page = filtered[: limit + 1]
        has_more = len(page) > limit
        page_items = page[:limit]
        next_cursor = _feed_cursor(page_items[-1]) if has_more and page_items else None
        payload = {
                "ok": True,
                "items": [_feed_item(item) for item in page_items],
                "window": window,
                "filter": filter_name,
                "sort": "timestamp_desc",
                "has_more": has_more,
                "next_cursor": next_cursor,
                "pagination": {
                    "limit": limit,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                },
                "read_only": True,
            }
        return self._json(payload)

    def _chats(self, query: Dict[str, Any]) -> None:
        try:
            timezone_name = self.server.service.policy.timezone_name
            messages, window, _start_at, _end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=True,
                limit=_WORKBENCH_ROW_LIMIT,
            )
        except ValueError as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        status = _runtime_snapshot(self.server.service)
        scope = _scope_payload(status, self.server.service)
        live_values = set(scope.get("live_values") or [])
        receiving = bool(status.get("receiving"))
        sync_state = scope.get("sync_state") or "unknown"
        grouped: Dict[str, Dict[str, Any]] = {}
        for item in messages:
            key = str(item.get("chat_id") or item.get("chat_name") or "unknown")
            row = grouped.setdefault(
                key,
                {
                    "chat_id": item.get("chat_id") or key,
                    "chat_name": _chat_display_name(item),
                    "messages": 0,
                    "inbound": 0,
                    "high_signal": 0,
                    "is_group": False,
                    "last_message": "",
                    "last_timestamp": None,
                    "last_message_id": None,
                    "last_sender_name": None,
                    "_last_key": (datetime.min.replace(tzinfo=timezone.utc), 0, ""),
                },
            )
            row["messages"] += 1
            if _is_self_message(item) is False:
                row["inbound"] += 1
                if _score_message(item).get("level") == "high":
                    row["high_signal"] += 1
            row["is_group"] = bool(row["is_group"] or item.get("is_group"))
            if row["chat_name"] in {"群聊", "未命名会话"}:
                row["chat_name"] = _chat_display_name(item)
            sort_key = (
                _message_timestamp(item.get("timestamp")),
                _row_id(item),
                str(item.get("message_id") or ""),
            )
            if sort_key > row["_last_key"]:
                row["_last_key"] = sort_key
                row["last_message"] = _content_preview(item)
                row["last_timestamp"] = item.get("timestamp")
                row["last_message_id"] = item.get("message_id")
                row["last_sender_name"] = _sender_display_name(item)

        items: List[Dict[str, Any]] = []
        for row in grouped.values():
            is_live = row["chat_id"] in live_values or row["chat_name"] in live_values
            row["is_live_monitored"] = is_live
            row["source_mode"] = "live" if is_live else "history"
            row["capture_state"] = _capture_state(
                is_live=is_live,
                receiving=receiving,
                sync_state=sync_state,
                has_messages=bool(row["messages"]),
            )
            row.pop("_last_key", None)
            items.append(row)
        items.sort(
            key=lambda value: (
                _message_timestamp(value.get("last_timestamp")),
                int(value.get("messages") or 0),
                str(value.get("chat_name") or ""),
            ),
            reverse=True,
        )
        public_scope = {
            key: value for key, value in scope.items() if key != "live_values"
        }
        payload = {
                "ok": True,
                "items": items,
                "window": window,
                "scope": public_scope,
                "sort": "last_timestamp_desc",
                "read_only": True,
            }
        return self._json(payload)

    def _contacts(self, query: Dict[str, Any]) -> None:
        try:
            timezone_name = self.server.service.policy.timezone_name
            messages, window, _start_at, _end_at = _load_message_window(
                self.server.store,
                query,
                timezone_name,
                default_all=True,
                limit=_WORKBENCH_ROW_LIMIT,
            )
        except ValueError as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        status = _runtime_snapshot(self.server.service)
        scope = _scope_payload(status, self.server.service)
        live_values = set(scope.get("live_values") or [])
        receiving = bool(status.get("receiving"))
        sync_state = scope.get("sync_state") or "unknown"
        grouped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for item in messages:
            if _is_self_message(item) is not False:
                continue
            key = _contact_key(item)
            row = grouped.setdefault(
                key,
                {
                    "contact_id": _contact_id(key),
                    "display_name": None,
                    "name_source": None,
                    "is_group": bool(item.get("is_group")),
                    "chat_ids": set(),
                    "chat_names": set(),
                    "message_count": 0,
                    "inbound_count": 0,
                    "last_message": "",
                    "last_timestamp": None,
                    "last_message_id": None,
                    "last_sender_name": None,
                    "_last_key": (datetime.min.replace(tzinfo=timezone.utc), 0, ""),
                    "_name_options": {},
                },
            )
            row["is_group"] = bool(row["is_group"] or item.get("is_group"))
            row["message_count"] += 1
            row["inbound_count"] += 1
            chat_id = str(item.get("chat_id") or "").strip()
            chat_name = _safe_name(item.get("chat_name"))
            if chat_id:
                row["chat_ids"].add(chat_id)
            if chat_name:
                row["chat_names"].add(chat_name)
            for name, source, priority in _contact_choice(item):
                option = row["_name_options"].setdefault(
                    name,
                    {"source": source, "priority": priority, "count": 0, "last": datetime.min.replace(tzinfo=timezone.utc)},
                )
                option["count"] += 1
                timestamp = _message_timestamp(item.get("timestamp"))
                if (priority, timestamp) > (option["priority"], option["last"]):
                    option["priority"] = priority
                    option["source"] = source
                    option["last"] = timestamp
            sort_key = (
                _message_timestamp(item.get("timestamp")),
                _row_id(item),
                str(item.get("message_id") or ""),
            )
            if sort_key > row["_last_key"]:
                row["_last_key"] = sort_key
                row["last_message"] = _content_preview(item)
                row["last_timestamp"] = item.get("timestamp")
                row["last_message_id"] = item.get("message_id")
                row["last_sender_name"] = _sender_display_name(item)

        items: List[Dict[str, Any]] = []
        for row in grouped.values():
            if row["_name_options"]:
                choice = max(
                    row["_name_options"].values(),
                    key=lambda value: (
                        value["priority"],
                        value["count"],
                        value["last"],
                    ),
                )
                row["display_name"] = next(
                    name
                    for name, value in row["_name_options"].items()
                    if value is choice
                )
                row["name_source"] = choice["source"]
            if not row["display_name"]:
                row["display_name"] = "群成员·待确认" if row["is_group"] else "联系人·待确认"
                row["name_source"] = "unresolved"
            is_live = bool(
                set(row["chat_ids"]).intersection(live_values)
                or set(row["chat_names"]).intersection(live_values)
            )
            row["is_live_monitored"] = is_live
            row["source_mode"] = "live" if is_live else "history"
            row["capture_state"] = _capture_state(
                is_live=is_live,
                receiving=receiving,
                sync_state=sync_state,
                has_messages=True,
            )
            row["chat_ids"] = sorted(row["chat_ids"])
            row["chat_names"] = sorted(row["chat_names"])
            row.pop("_last_key", None)
            row.pop("_name_options", None)
            items.append(row)
        items.sort(
            key=lambda value: (
                _message_timestamp(value.get("last_timestamp")),
                str(value.get("display_name") or ""),
            ),
            reverse=True,
        )
        limit = self._int_query(query, "limit", 500, cap=2_000)
        public_scope = {
            key: value for key, value in scope.items() if key != "live_values"
        }
        payload = {
                "ok": True,
                "items": items[:limit],
                "total": len(items),
                "window": window,
                "scope": public_scope,
                "read_only": True,
            }
        return self._json(payload)

    def _sync_runs(self, query: Dict[str, Any]) -> None:
        limit = self._int_query(query, "limit", 50, cap=200)
        current = {}
        try:
            current = dict(self.server.service.history_sync_status())
        except Exception as exc:
            current = {"state": "unknown", "error": str(exc)}

        result: Any = None
        provider = None
        errors: List[str] = []
        for owner_name, owner in (("store", self.server.store), ("service", self.server.service)):
            for method_name in ("recent_sync_runs", "list_sync_runs", "get_sync_runs", "sync_runs"):
                method = getattr(owner, method_name, None)
                if not callable(method):
                    continue
                try:
                    try:
                        result = method(limit)
                    except TypeError:
                        result = method()
                    provider = "%s.%s" % (owner_name, method_name)
                    break
                except Exception as exc:
                    errors.append("%s: %s" % (method_name, exc))
            if provider:
                break

        if provider is None:
            return self._json(
                {
                    "ok": True,
                    "items": [],
                    "available": False,
                    "state": "not_persisted",
                    "message": "底层尚未提供持久化 sync_runs；以下仅返回当前进程同步状态",
                    "current": current,
                    "errors": errors,
                    "read_only": True,
                }
            )

        if isinstance(result, dict):
            items = result.get("items") or result.get("runs") or []
        elif isinstance(result, (list, tuple)):
            items = result
        else:
            items = []
        if not isinstance(items, list):
            items = list(items) if isinstance(items, tuple) else []
        return self._json(
            {
                "ok": True,
                "items": items[:limit],
                "available": True,
                "state": "available",
                "provider": provider,
                "current": current,
                "errors": errors,
                "read_only": True,
            }
        )

    def _status(self) -> Dict[str, Any]:
        value = self.server.service.status_snapshot()
        value["counts"] = self.server.store.counts()
        value["server_time"] = datetime.now(timezone.utc).isoformat()
        return value

    def _shadow_analysis(self, query: Mapping[str, Any]) -> None:
        """Serve a review-only, body-free shadow envelope.

        The homepage never calls this endpoint.  The endpoint is intentionally
        read-only: a caller must opt in through settings and select an opaque
        ``analysis_run_id``; an unknown id does not fall back to the latest
        run.  Actual run creation remains the responsibility of the separate
        shadow service/orchestrator.
        """

        requested_id = str((query.get("analysis_run_id") or [""])[0] or "").strip()
        settings = self.server.settings.snapshot()
        enabled = settings.get("shadow_analysis_enabled") is True
        timezone_name = self.server.service.policy.timezone_name
        window = {
            "start": str((query.get("start") or [""])[0] or "").strip(),
            "end": str((query.get("end") or [""])[0] or "").strip(),
            "timezone": timezone_name,
        }
        if not enabled:
            return self._json(
                {
                    "ok": True,
                    "analysis_run_id": requested_id or "shadow-disabled",
                    "source": "shadow_disabled",
                    "source_marker": "conservative_fallback",
                    "provider_status": "disabled",
                    "llm_accepted": False,
                    "fallback_reason": "shadow_analysis_disabled",
                    "window": window,
                    "available_run_ids": [],
                    "read_only": True,
                }
            )

        runs = self._shadow_run_entries()
        available_run_ids = [
            str(item.analysis_run_id)
            for item in runs
            if str(getattr(item, "analysis_run_id", "")).strip()
        ]
        if not requested_id:
            return self._json(
                {
                    "ok": True,
                    "analysis_run_id": "shadow-index",
                    "source": "shadow_index",
                    "source_marker": "conservative_fallback",
                    "provider_status": "configured" if runs else "disabled",
                    "llm_accepted": False,
                    "fallback_reason": "analysis_run_id_required" if runs else "no_shadow_runs",
                    "window": window,
                    "available_run_ids": list(dict.fromkeys(available_run_ids)),
                    "read_only": True,
                }
            )

        selected = next(
            (
                item
                for item in runs
                if requested_id in {
                    str(getattr(item, "analysis_run_id", "")),
                    str(getattr(item, "run_id", "")),
                }
            ),
            None,
        )
        if selected is None:
            return self._json(
                {
                    "ok": False,
                    "analysis_run_id": requested_id,
                    "source": "shadow_analysis",
                    "source_marker": "llm_pending",
                    "provider_status": "failed",
                    "llm_accepted": False,
                    "fallback_reason": "analysis_run_not_found",
                    "window": window,
                    "available_run_ids": list(dict.fromkeys(available_run_ids)),
                    "read_only": True,
                },
                404,
            )

        payload = selected.to_dict() if callable(getattr(selected, "to_dict", None)) else {}
        # ``to_dict`` is the shadow service's body-free projection.  Keep the
        # review response explicit about selection and never expose its in-
        # memory dialogue/message DTO handles.
        payload.update(
            {
                "window": window,
                "selected_analysis_run_id": requested_id,
                "available_run_ids": list(dict.fromkeys(available_run_ids)),
                "read_only": True,
            }
        )
        return self._json(payload)

    def _shadow_run_entries(self) -> Tuple[Any, ...]:
        """Return de-duplicated in-memory plus explicitly loaded runs.

        The catalog is deliberately not discovered implicitly.  This helper
        only combines the old in-memory review store with the server's
        explicit body-free catalog projection, while keeping one selected
        opaque run identity from shadowing another.
        """

        store = getattr(self.server, "shadow_run_store", None) or getattr(self.server, "shadow_store", None)
        values = list(store.runs()) if store is not None and callable(getattr(store, "runs", None)) else []
        values.extend(tuple(getattr(self.server, "shadow_catalog", ()) or ()))
        output: List[Any] = []
        seen_run_ids = set()
        seen_analysis_ids = set()
        for item in values:
            run_id = str(getattr(item, "run_id", "") or "").strip()
            analysis_run_id = str(getattr(item, "analysis_run_id", "") or "").strip()
            if run_id and run_id in seen_run_ids:
                continue
            if analysis_run_id and analysis_run_id in seen_analysis_ids:
                continue
            if run_id:
                seen_run_ids.add(run_id)
            if analysis_run_id:
                seen_analysis_ids.add(analysis_run_id)
            output.append(item)
        return tuple(output)

    def _ai_generator(self) -> OpenAIAnalysisGenerator:
        settings = self.server.settings.snapshot(include_secrets=True).get("ai") or {}
        environment_model = str(
            os.environ.get("OPENAI_WECHAT_ANALYSIS_MODEL") or ""
        ).strip()
        reasoning_effort = str(
            os.environ.get("OPENAI_WECHAT_ANALYSIS_REASONING_EFFORT")
            or settings.get("reasoning_effort")
            or ""
        ).strip().lower()
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            reasoning_effort = ""
        raw_max_tokens = str(
            os.environ.get("OPENAI_WECHAT_ANALYSIS_MAX_TOKENS")
            or settings.get("max_tokens")
            or ""
        ).strip()
        try:
            max_tokens = int(raw_max_tokens) if raw_max_tokens else None
        except ValueError:
            max_tokens = None
        if max_tokens is not None and not 1024 <= max_tokens <= 131072:
            max_tokens = None
        return OpenAIAnalysisGenerator(
            model=str(
                environment_model
                or settings.get("model")
                or "gpt-5.2"
            ),
            api_key=str(settings.get("api_key") or "") or None,
            base_url=str(settings.get("base_url") or "") or None,
            reasoning_effort=reasoning_effort or None,
            max_tokens=max_tokens,
            max_findings=20,
        )

    @staticmethod
    def _ai_generator_with_effort(
        generator: Any,
        reasoning_effort: str,
        max_tokens: int,
    ) -> Any:
        """Clone the real provider config for one analysis stage.

        Tests and local adapters may supply duck-typed generators; those are
        returned unchanged so packet execution remains backwards compatible.
        """

        if not isinstance(generator, OpenAIAnalysisGenerator):
            return generator
        return OpenAIAnalysisGenerator(
            model=generator.model,
            api_key=generator.api_key,
            base_url=generator.base_url,
            reasoning_effort=reasoning_effort or None,
            max_tokens=max_tokens,
            max_findings=generator.max_findings,
            packet_reasoning_effort=reasoning_effort or None,
            packet_max_tokens=max_tokens,
        )

    def _ai_status(self) -> Dict[str, Any]:
        generator = self._ai_generator()
        public_ai = (self.server.settings.public().get("ai") or {})
        return {
            "provider": "openai",
            "model": generator.model,
            "configured": generator.configured,
            "base_url": generator.base_url or "OpenAI 默认接口",
            "api_key_configured": bool(public_ai.get("api_key_configured") or generator.api_key),
            "mode": "auto_with_manual_refresh" if generator.configured else "manual_only",
            "send_enabled": bool(self.server.service.send_enabled),
            "privacy": "redacted_candidates_only",
            "message": (
                "已配置；日报更新后自动分析，也可手动刷新"
                if generator.configured
                else "未配置 OPENAI_API_KEY；当前仅使用本地规则分析"
            ),
        }

    def _media(self, query: Dict[str, Any]) -> None:
        message_id = str((query.get("message_id") or [""])[0] or "").strip()
        if not message_id:
            return self._json({"error": "missing_message_id", "message": "缺少 message_id"}, 400)
        item = self.server.store.get_message(message_id)
        if item is None:
            return self._json({"error": "media_not_found", "message": "消息不存在"}, 404)
        if str(item.get("message_type") or "").lower() != "image":
            return self._json({"error": "media_not_image", "message": "当前消息不是图片"}, 409)
        raw_path = str(item.get("media_path") or "").strip()
        path = Path(raw_path).expanduser()
        if not path.is_file():
            return self._json(
                {
                    "error": "media_unavailable",
                    "state": "path_only",
                    "message": "图片路径已记录，但具体文件当前不可读",
                    "media_path": raw_path,
                },
                409,
            )
        try:
            resolved = path.resolve()
        except OSError:
            return self._json({"error": "media_unavailable", "message": "图片路径无法解析"}, 409)

        settings = self.server.settings.snapshot(include_secrets=True)
        media_settings = settings.get("media") or {}
        allowed_roots = []
        configured_cache_dir = str(media_settings.get("cache_dir") or "").strip()
        if configured_cache_dir:
            allowed_roots.append(Path(configured_cache_dir).expanduser())
        adapter_database = getattr(self.server.service.adapter, "database", None)
        account_dir = str(getattr(adapter_database, "account_dir", "") or "")
        if account_dir:
            allowed_roots.append(Path(account_dir).expanduser())
        adapter_status = (self._status().get("adapter") or {}).get("details") or {}
        db_dir = str(adapter_status.get("db_dir") or "")
        if db_dir:
            allowed_roots.append(Path(db_dir).expanduser().parent)
        try:
            allowed = any(resolved.is_relative_to(root.resolve()) for root in allowed_roots if str(root))
        except (AttributeError, OSError, ValueError):
            allowed = False
        if not allowed:
            return self._json({"error": "media_forbidden", "message": "媒体不在允许的本地目录内"}, 403)

        try:
            aes_key = str(media_settings.get("image_aes_key") or "") or runtime_image_key()
            if not aes_key:
                try:
                    if resolved.read_bytes()[:6].startswith(V2_MAGIC):
                        request_image_key_discovery(str(resolved))
                except (OSError, ValueError):
                    aes_key = ""
            data, extension, content_type = read_media(
                str(resolved),
                aes_key=aes_key,
                xor_key=media_settings.get("image_xor_key"),
            )
        except (MediaUnavailable, OSError, ValueError) as exc:
            return self._json(
                {
                    "error": "media_unavailable",
                    "state": "path_only",
                    "message": str(exc),
                    "media_path": str(resolved),
                },
                409,
            )

        # Persist only decoded copies under the user-selected cache directory.
        # The response still works if the cache directory cannot be created.
        cache_dir = Path(str(media_settings.get("cache_dir") or "")).expanduser()
        if str(cache_dir):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached = cache_dir / cache_key(str(resolved), extension)
                if not cached.exists():
                    cached.write_bytes(data)
            except OSError:
                logger.debug("unable to persist decoded media cache", exc_info=True)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before media response completed")

    @staticmethod
    def _ensure_claim_attribution(
        claim_text: Any,
        evidence: Sequence[Mapping[str, Any]],
    ) -> str:
        """Keep explicit speaker attribution while removing generic stand-ins."""

        text = re.sub(r"\s+", " ", str(claim_text or "")).strip()
        senders = list(dict.fromkeys(
            str(item.get("sender_name") or "").strip()
            for item in evidence
            if str(item.get("sender_name") or "").strip()
        ))
        if not text or not senders:
            return text
        if any(sender in text for sender in senders):
            return text
        generic = re.match(
            r"^(?:群内有人|群里有人|有群成员|一位群成员|群内成员|群成员|该成员|该人|有人)"
            r"(?:在群内|在群聊中)?(?:表示|称|说|陈述|提出|认为|询问|建议)?[，,：:\s]*",
            text,
        )
        if generic:
            remainder = text[generic.end():].strip("，,：: ")
            verb_match = re.search(r"(?:表示|称|说|陈述|提出|认为|询问|建议)", generic.group(0))
            verb = verb_match.group(0) if verb_match else "表示"
            return "%s%s，%s" % (senders[0], verb, remainder)
        return text

    @staticmethod
    def _apply_editorial_headline(
        finding: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
    ) -> str:
        """Replace speaker-led, clipped or raw-sentence titles deterministically."""

        raw_title = re.sub(r"\s+", "", str(finding.get("title") or "").strip())
        sender_names = {
            re.sub(r"\s+", "", str(item.get("sender_name") or "").strip())
            for item in evidence
            if str(item.get("sender_name") or "").strip()
        }
        speaker_led = any(
            raw_title.startswith(name + verb)
            for name in sender_names
            for verb in ("称", "说", "表示", "陈述", "提出", "建议", "询问", "认为")
        )
        generic_speaker = bool(re.match(r"^(?:群内有人|有群成员|一位群成员|群内成员|该人)", raw_title))
        mechanically_clipped = "…" in raw_title or raw_title.endswith("...")
        sentence_shaped = bool(re.search(r"(?:称|说|表示|陈述|提出|建议|询问).{8,}", raw_title))
        chatty_wording = bool(re.search(
            r"(?:魔幻|正正好|真的快|要很长时间|效果会不好|我觉得|感觉|倒也是|这个|那个)",
            raw_title,
            re.I,
        ))
        needs_rewrite = (
            not is_editorial_title(raw_title)
            or speaker_led
            or generic_speaker
            or mechanically_clipped
            or sentence_shaped
            or chatty_wording
        )
        if not needs_rewrite:
            return raw_title
        rewritten = _headline_from_summary(finding.get("summary"), evidence)
        if rewritten and is_editorial_title(rewritten):
            return rewritten
        title_context = " ".join(
            str(finding.get(key) or "")
            for key in ("summary", "narrative", "what_changed", "why_it_matters", "core_conclusion", "keywords")
        ) + " " + " ".join(str(entry.get("content") or "") for entry in evidence)
        return normalize_editorial_title(raw_title, title_context)

    @staticmethod
    def _self_contained_single(evidence: Sequence[Mapping[str, Any]]) -> bool:
        """Whether one message alone is complete enough to carry a card.

        A single citation may support an article only when the message itself
        is a complete announcement, decision, commitment, request or risk
        with a concrete object.  Anything shorter is honest as a lead, not
        as an editorial card.
        """

        if len(evidence) != 1:
            return False
        content = str(evidence[0].get("content") or "")
        compact = re.sub(r"\s+", "", content)
        if len(compact) < 20:
            return False
        action = _action_evidence(content)
        if not _has_concrete_object(content):
            return False
        if (
            action.get("decision")
            or action.get("commitment")
            or action.get("request")
            or action.get("risk")
        ):
            return True
        # A colloquial but complete assignment with a deadline also stands
        # alone, e.g. “需要在网站上完成本周论文评论，当天 18 点前提交”。
        return bool(
            re.search(r"(需要|须|务必|记得|别忘了|尽快|请于|明天|今天)", content)
            and _DEADLINE.search(content)
        )

    @staticmethod
    def _finding_as_lead(finding: Mapping[str, Any], recovered: bool) -> Dict[str, Any]:
        """Demote a finding to an honest one-line lead.

        Leads keep the speaker, the original sentence and the reason the item
        was retained.  They never receive an expanded narrative, a template
        title or boilerplate significance fields.
        """

        quotes: List[Dict[str, Any]] = []
        for entry in list(finding.get("evidence") or [])[:3]:
            if not isinstance(entry, Mapping):
                continue
            content = re.sub(r"\s+", " ", str(entry.get("content") or "")).strip()
            if not content:
                continue
            quotes.append(
                {
                    "evidence_ref": str(entry.get("evidence_ref") or ""),
                    "message_id": str(entry.get("message_id") or ""),
                    "sender_name": str(entry.get("sender_name") or ""),
                    "chat_name": str(entry.get("chat_name") or ""),
                    "timestamp": str(entry.get("timestamp") or ""),
                    "content": content[:160],
                }
            )
        title = str(finding.get("title") or "")
        if recovered and quotes:
            generic_suffixes = (
                "方案进入调整阶段",
                "异常信号开始集中",
                "关键问题仍待核实",
                "成本与选择出现分歧",
                "资源线索开始汇合",
                "观点逐步形成共识",
                "出现可回看的新线索",
                "内容开始成形",
            )
            if (
                not title
                or title in _RECOVERED_GENERIC_TITLES
                or any(title.endswith(suffix) for suffix in generic_suffixes)
            ):
                source_text = re.sub(
                    r"https?://\S|www\.\S", "", quotes[0]["content"], flags=re.I
                )
                source_text = re.sub(r"\s+", " ", source_text).strip(" ，,。；;：:")
                source_text = re.sub(
                    r"^(?:我|我们|话说|请问|有没有|有谁知道|怕大家不知道)[，,：:\s]*",
                    "",
                    source_text,
                )
                sentence = re.split(r"[。；;！!？?]", source_text, maxsplit=1)[0].strip()
                if len(sentence) > 28:
                    sentence = sentence[:27] + "…"
                if sentence:
                    title = "原文线索：" + sentence
        if not recovered and quotes and not _title_supported_by_evidence(title, quotes):
            # A demoted model item must not retain a headline borrowed from an
            # uncited neighbour in the packet.  Quote-led wording makes the
            # mismatch visible and keeps the lead useful.
            source_text = re.sub(
                r"https?://\S|www\.\S", "", quotes[0]["content"], flags=re.I
            )
            source_text = re.sub(r"\s+", " ", source_text).strip(" ，,。；;：:")
            sentence = re.split(r"[。；;！!？?]", source_text, maxsplit=1)[0].strip()
            if len(sentence) > 28:
                sentence = sentence[:27] + "…"
            title = "原文线索：" + sentence if sentence else "原文线索"
        return {
            "kind": "lead",
            "title": title,
            "speakers": list(finding.get("speakers") or []),
            "quotes": quotes,
            "reason": str(finding.get("reason") or "单条证据不足以独立成稿"),
            "evidence_refs": list(finding.get("evidence_refs") or []),
            "importance": int(finding.get("importance") or 0),
            "recovered": bool(recovered),
        }

    @staticmethod
    def _finding_as_leads(
        finding: Mapping[str, Any], recovered: bool
    ) -> List[Dict[str, Any]]:
        """Demote a finding, keeping recovered evidence inside one chat.

        A rejected model synthesis can contain valid references from unrelated
        chats.  Keeping all of them in one fallback lead recreates the very
        cross-object story that validation rejected.  Split recovered evidence
        by chat before deriving evidence-led titles; model-written single-line
        leads keep the existing one-item contract.
        """

        evidence = [
            item for item in list(finding.get("evidence") or [])
            if isinstance(item, Mapping)
        ]
        raw_claims = [
            dict(item) for item in list(finding.get("claims") or [])
            if isinstance(item, Mapping)
            and str(item.get("text") or "").strip()
            and any(str(ref) for ref in item.get("evidence_refs") or [])
        ]
        if not recovered and len(evidence) >= 2 and len(raw_claims) >= 2:
            # A value/coherence demotion must not preserve an accidental
            # multi-story packet as one lead.  Claims connected through at
            # least one citation stay together; disconnected claim components
            # become independently auditable quote leads.
            components: List[Dict[str, Any]] = []
            for claim in raw_claims:
                claim_refs = {
                    str(ref) for ref in claim.get("evidence_refs") or [] if str(ref)
                }
                touching = [
                    component for component in components
                    if claim_refs.intersection(component["refs"])
                ]
                if not touching:
                    components.append({"refs": set(claim_refs), "claims": [claim]})
                    continue
                primary = touching[0]
                primary["refs"].update(claim_refs)
                primary["claims"].append(claim)
                for extra in touching[1:]:
                    primary["refs"].update(extra["refs"])
                    primary["claims"].extend(extra["claims"])
                    components.remove(extra)
            if len(components) > 1:
                evidence_by_ref = {
                    str(item.get("evidence_ref") or ""): item
                    for item in evidence
                    if str(item.get("evidence_ref") or "")
                }
                result: List[Dict[str, Any]] = []
                for component in components:
                    refs = [ref for ref in evidence_by_ref if ref in component["refs"]]
                    if not refs:
                        continue
                    component_evidence = [evidence_by_ref[ref] for ref in refs]
                    component_claims = [
                        claim for claim in component["claims"]
                        if any(str(ref) in refs for ref in claim.get("evidence_refs") or [])
                    ]
                    claim_prose = "；".join(
                        str(claim.get("text") or "").strip()
                        for claim in component_claims
                    )
                    part = dict(finding)
                    part.update({
                        "title": _headline_from_summary(claim_prose, component_evidence),
                        "summary": claim_prose,
                        "narrative": claim_prose,
                        "core_conclusion": str(component_claims[0].get("text") or ""),
                        "what_changed": claim_prose,
                        "claims": component_claims,
                        "evidence_refs": refs,
                        "evidence": component_evidence,
                    })
                    sender_names = {
                        str(item.get("sender_name") or "").strip()
                        for item in component_evidence
                    }
                    part["speakers"] = [
                        dict(speaker)
                        for speaker in finding.get("speakers") or []
                        if isinstance(speaker, Mapping)
                        and str(speaker.get("name") or "").strip() in sender_names
                    ]
                    result.append(BridgeRequestHandler._finding_as_lead(part, False))
                if result:
                    return result
        if not recovered or len(evidence) < 2:
            return [BridgeRequestHandler._finding_as_lead(finding, recovered)]

        chat_groups: Dict[str, List[Mapping[str, Any]]] = {}
        for index, item in enumerate(evidence):
            chat_name = str(item.get("chat_name") or "").strip()
            # Missing chat metadata cannot prove that two citations share a
            # conversation, so it fails closed into its own group.
            key = chat_name or "__missing_chat_%d" % index
            chat_groups.setdefault(key, []).append(item)

        groups: List[List[Mapping[str, Any]]] = []
        for entries in chat_groups.values():
            current: List[Mapping[str, Any]] = []
            previous_time: Optional[datetime] = None
            for item in entries:
                raw_time = str(item.get("timestamp") or "").strip()
                try:
                    item_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    item_time = None
                linked = False
                if current and previous_time is not None and item_time is not None:
                    try:
                        linked = abs(item_time - previous_time) <= timedelta(minutes=30)
                    except TypeError:
                        # A naive/aware mismatch is not enough evidence that
                        # the citations belong to one continuous exchange.
                        linked = False
                if not linked:
                    if current:
                        groups.append(current)
                    current = [item]
                else:
                    current.append(item)
                previous_time = item_time
            if current:
                groups.append(current)
        if len(groups) == 1:
            return [BridgeRequestHandler._finding_as_lead(finding, recovered)]

        result: List[Dict[str, Any]] = []
        for entries in groups:
            refs = [
                str(item.get("evidence_ref") or "")
                for item in entries
                if str(item.get("evidence_ref") or "")
            ]
            sender_names = {
                str(item.get("sender_name") or "").strip()
                for item in entries
                if str(item.get("sender_name") or "").strip()
            }
            part = dict(finding)
            part["evidence"] = list(entries)
            part["evidence_refs"] = refs
            part["speakers"] = [
                dict(speaker)
                for speaker in finding.get("speakers") or []
                if isinstance(speaker, Mapping)
                and str(speaker.get("name") or "").strip() in sender_names
            ]
            result.append(BridgeRequestHandler._finding_as_lead(part, True))
        return result

    @staticmethod
    def _merge_leads(leads: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deduplicate leads only when their validated references overlap.

        Generic fallback headlines are deliberately ignored: two unrelated
        stories frequently receive the same rule-generated title.  Recovered
        and model-written leads also stay in separate trust lanes.
        """

        groups: List[Dict[str, Any]] = []
        for lead in leads:
            refs = {str(ref) for ref in lead.get("evidence_refs") or [] if str(ref)}
            placed = None
            for group in groups:
                if bool(lead.get("recovered")) != group["recovered"]:
                    continue
                shared = refs & group["refs"]
                smaller = min(len(refs), len(group["refs"]))
                if shared and len(shared) >= max(1, (smaller + 1) // 2):
                    placed = group
                    break
            if placed is None:
                groups.append(
                    {
                        "recovered": bool(lead.get("recovered")),
                        "refs": set(refs),
                        "items": [lead],
                    }
                )
            else:
                placed["refs"].update(refs)
                placed["items"].append(lead)

        merged: List[Dict[str, Any]] = []
        for group in groups:
            items = list(group["items"])
            best = max(
                items,
                key=lambda item: (
                    int(item.get("importance") or 0),
                    len(item.get("quotes") or []),
                ),
            )
            value = dict(best)
            combined_refs: List[str] = []
            combined_quotes: List[Dict[str, Any]] = []
            combined_speakers: List[Dict[str, Any]] = []
            reasons: List[str] = []
            quote_keys: set = set()
            speaker_names: set = set()
            for item in [best] + [entry for entry in items if entry is not best]:
                for ref in item.get("evidence_refs") or []:
                    ref_text = str(ref)
                    if ref_text and ref_text not in combined_refs:
                        combined_refs.append(ref_text)
                reason = str(item.get("reason") or "").strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
                for quote in item.get("quotes") or []:
                    if not isinstance(quote, Mapping):
                        continue
                    quote_value = dict(quote)
                    key = (
                        str(quote_value.get("evidence_ref") or ""),
                        str(quote_value.get("message_id") or ""),
                        str(quote_value.get("content") or ""),
                    )
                    if key in quote_keys:
                        continue
                    quote_keys.add(key)
                    combined_quotes.append(quote_value)
                for speaker in item.get("speakers") or []:
                    if not isinstance(speaker, Mapping):
                        continue
                    name = str(speaker.get("name") or "").strip()
                    if not name or name in speaker_names:
                        continue
                    speaker_names.add(name)
                    combined_speakers.append(dict(speaker))
            value["evidence_refs"] = combined_refs
            value["quotes"] = combined_quotes
            value["speakers"] = combined_speakers
            value["reasons"] = reasons
            value["reason"] = reasons[0] if reasons else str(value.get("reason") or "")
            value["merged_count"] = len(items)
            value["importance"] = max(int(item.get("importance") or 0) for item in items)
            merged.append(value)
        return merged

    @staticmethod
    def _drop_represented_leads(
        leads: Sequence[Dict[str, Any]],
        findings: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Drop lead remnants whose citations are already covered by a card."""

        represented = {
            str(ref)
            for finding in findings
            for ref in finding.get("evidence_refs") or []
            if str(ref)
        }
        remaining: List[Dict[str, Any]] = []
        for lead in leads:
            refs = {str(ref) for ref in lead.get("evidence_refs") or [] if str(ref)}
            if refs and refs.issubset(represented):
                continue
            remaining.append(dict(lead))
        return remaining

    @staticmethod
    def _promote_related_leads(
        leads: Sequence[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Promote only same-chat, close-time facts about one named product."""

        rows: List[Dict[str, Any]] = []
        for index, lead in enumerate(leads):
            quotes = [dict(item) for item in lead.get("quotes") or [] if isinstance(item, Mapping)]
            if len(quotes) != 1:
                continue
            quote = quotes[0]
            chat = str(quote.get("chat_name") or "").strip()
            timestamp_text = str(quote.get("timestamp") or "").strip()
            content = str(quote.get("content") or "").strip()
            try:
                timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            families = _ai_object_families(content)
            event_patterns = {
                "access_risk": r"封号|封禁|退款|风控|锁区",
                "quota_reset": r"额度|用量|reset|重置|五小时|限额",
                "endpoint": r"端点|endpoint|调用|直打|模型名",
                "performance": r"速度|tps|性能|快|慢",
                "quality": r"质量|效果|降智|好用|稳定|黑话",
                "workflow": r"agent|harness|skill|mcp|cli|规划|执行",
            }
            events = {name for name, pattern in event_patterns.items() if re.search(pattern, content, re.I)}
            if not chat or not content or not families or not events:
                continue
            rows.append({
                "index": index,
                "lead": lead,
                "quote": quote,
                "chat": chat,
                "timestamp": timestamp,
                "families": families,
                "events": events,
            })

        groups: List[Dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: (item["chat"], item["timestamp"], item["index"])):
            placed = None
            for group in groups:
                if row["chat"] != group["chat"]:
                    continue
                try:
                    close = row["timestamp"] - group["last_timestamp"] <= timedelta(minutes=30)
                except TypeError:
                    close = False
                compatible_events = bool(row["events"].intersection(group["events"]))
                event_union = row["events"] | group["events"]
                release_observation = (
                    bool(row["events"] & {"endpoint", "performance"})
                    and bool(group["events"] & {"endpoint", "performance"})
                    and "endpoint" in event_union
                    and "performance" in event_union
                    and not event_union.intersection({"access_risk", "quota_reset", "workflow"})
                )
                if (
                    close
                    and row["families"].intersection(group["families"])
                    and (compatible_events or release_observation)
                ):
                    placed = group
                    break
            if placed is None:
                groups.append({
                    "chat": row["chat"],
                    "last_timestamp": row["timestamp"],
                    "families": set(row["families"]),
                    "events": set(row["events"]),
                    "rows": [row],
                })
            else:
                placed["last_timestamp"] = row["timestamp"]
                placed["families"].intersection_update(row["families"])
                placed["events"].intersection_update(row["events"])
                placed["rows"].append(row)

        promoted_indices: set = set()
        cards: List[Dict[str, Any]] = []
        for group in groups:
            items = list(group["rows"])
            if len(items) < 2:
                continue
            refs: List[str] = []
            quotes: List[Dict[str, Any]] = []
            claims: List[Dict[str, Any]] = []
            for item in items:
                quote = dict(item["quote"])
                ref = str(quote.get("evidence_ref") or "").strip()
                content = str(quote.get("content") or "").strip()
                if not ref or not content or ref in refs:
                    continue
                refs.append(ref)
                quotes.append(quote)
                claims.append({"text": content, "evidence_refs": [ref]})
            if len(refs) < 2:
                continue
            promoted_indices.update(item["index"] for item in items)
            first = items[0]["lead"]
            claim_prose = "；".join(claim["text"] for claim in claims)
            cards.append({
                "kind": "verified_quote_card",
                "presentation_mode": "verified_quote_card",
                "title": _headline_from_summary(claims[0]["text"], quotes) or str(first.get("title") or "原文事实汇编"),
                "category": "事件",
                "value_type": "reported_claim",
                "importance": max(int(item["lead"].get("importance") or 0) for item in items),
                "confidence": 68,
                "summary": claim_prose,
                "narrative": claim_prose,
                "core_conclusion": claims[0]["text"],
                "what_changed": claim_prose,
                "why_it_matters": "",
                "reason": "同一会话、同一明确对象的相邻事实已按原文聚合。",
                "uncertainty": "仅代表所引聊天原文，尚未外部核验。",
                "claim_type": "reported_claim",
                "claim_status": "unverified_chat",
                "claim_basis": "逐条引用绑定",
                "evidence_refs": refs,
                "evidence": quotes,
                "quotes": quotes,
                "claims": claims,
                "speakers": [],
                "keywords": [],
                "next_step": "",
            })
            BridgeRequestHandler._finding_attribution(cards[-1], quotes)
        remaining = [dict(lead) for index, lead in enumerate(leads) if index not in promoted_indices]
        return cards, remaining

    @staticmethod
    def _split_claim_findings_by_subject(
        findings: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Split verified claim cards when their claims name separate subjects."""

        def subject_key(text: str) -> str:
            if re.search(r"claude", text, re.I) and re.search(r"封号|封禁|退款|风控", text):
                return "claude_access_risk"
            if re.search(r"claude|reset|额度|五小时", text, re.I) and re.search(
                r"reset|额度|五小时|限额", text, re.I
            ):
                return "claude_reset_quota"
            if re.search(r"agent", text, re.I) and re.search(r"修agent|修复agent", text, re.I):
                return "agent_repair"
            if re.search(r"codex|bigmodel|z\.ai|第三方模型|免费token|周末", text, re.I):
                return "codex_model_access"
            if re.search(r"fable|fable\s*usage", text, re.I):
                return "fable"
            if re.search(r"high", text, re.I) and re.search(
                r"强度|默认|推荐|正合适|正正好|事情", text
            ):
                return "reasoning_high"
            if re.search(r"token", text, re.I) and re.search(
                r"询问|是否|哪家|另一家|来源|吗", text
            ):
                return "token_question"
            if re.search(r"deepseek|ds|dsh|harness", text, re.I) and re.search(
                r"性能|发挥|pro|dsh|harness|魔幻", text, re.I
            ):
                return "deepseek_dsh"
            if re.search(r"显示屏|华为手机", text, re.I):
                return "display_comparison"
            if re.search(r"kimi|部署|无图形界面|图形界面", text, re.I):
                return "kimi_deployment"
            families = sorted(_ai_object_families(text))
            return "|".join(families) if families else "shared"

        output: List[Dict[str, Any]] = []
        for finding in findings:
            claims = [
                claim
                for claim in finding.get("claims") or []
                if isinstance(claim, Mapping)
            ]
            if len(claims) < 2:
                output.append(dict(finding))
                continue

            groups: Dict[str, List[Mapping[str, Any]]] = {}
            for claim in claims:
                key = subject_key(str(claim.get("text") or ""))
                groups.setdefault(key, []).append(claim)
            meaningful = {
                key: value
                for key, value in groups.items()
                if key != "shared" and value
            }
            if len(meaningful) < 2 or sum(map(len, meaningful.values())) != len(claims):
                output.append(dict(finding))
                continue

            evidence_by_ref = {
                str(item.get("evidence_ref") or ""): dict(item)
                for item in finding.get("evidence") or []
                if isinstance(item, Mapping) and str(item.get("evidence_ref") or "")
            }
            for grouped_claims in meaningful.values():
                refs = list(
                    dict.fromkeys(
                        str(ref)
                        for claim in grouped_claims
                        for ref in claim.get("evidence_refs") or []
                        if str(ref) in evidence_by_ref
                    )
                )
                evidence = [evidence_by_ref[ref] for ref in refs]
                claim_prose = "；".join(
                    str(claim.get("text") or "") for claim in grouped_claims
                )
                part = dict(finding)
                part.update(
                    {
                        "title": "",
                        "claims": [dict(claim) for claim in grouped_claims],
                        "evidence_refs": refs,
                        "evidence": evidence,
                        "summary": claim_prose,
                        "narrative": claim_prose,
                        "what_changed": claim_prose,
                        "core_conclusion": str(grouped_claims[0].get("text") or ""),
                    }
                )
                part["title"] = BridgeRequestHandler._apply_editorial_headline(part, evidence)
                BridgeRequestHandler._finding_attribution(part, evidence)
                output.append(part)
        return output

    @staticmethod
    def _select_editorial_cards(
        cards: Sequence[Dict[str, Any]], limit: int = 10
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Select a compact front page and return the rest for safe demotion."""

        bounded_limit = max(1, min(int(limit), 12))

        def refs(card: Mapping[str, Any]) -> set:
            return {str(ref) for ref in card.get("evidence_refs") or [] if str(ref)}

        def text(card: Mapping[str, Any]) -> str:
            return " ".join(
                str(card.get(key) or "")
                for key in ("title", "summary", "core_conclusion", "what_changed")
            )

        def low_information_card(card: Mapping[str, Any]) -> bool:
            evidence = [item for item in card.get("evidence") or [] if isinstance(item, Mapping)]
            contents = [re.sub(r"\s+", "", str(item.get("content") or "")) for item in evidence]
            unique_contents = {content for content in contents if content}
            emotional_only = bool(
                unique_contents
                and all(
                    len(content) <= 14
                    and re.search(r"(?:困死|累死|笑死|吓哭|无语|哈哈|笑了|绝了|好烦|卧槽|我靠)", content)
                    for content in unique_contents
                )
            )
            vague_flash_only = bool(
                unique_contents
                and re.search(r"flash", " ".join(unique_contents), re.I)
                and all(re.search(r"(?:这才是|真正的|吗|嘛)", content) for content in unique_contents)
            )
            fragment_pair = bool(
                len(unique_contents) <= 2
                and any(re.fullmatch(r"[a-z]+(?:[a-z\s-]+)?", content, re.I) for content in unique_contents)
                and any(re.search(r"(?:吓哭|困死|累死|笑死|无语)", content) for content in unique_contents)
            )
            claim_family_sets = []
            for claim in card.get("claims") or []:
                if not isinstance(claim, Mapping):
                    continue
                families = _ai_object_families(claim.get("text"))
                if families:
                    claim_family_sets.append(families)
            incompatible_story = bool(
                len(claim_family_sets) >= 2
                and not set.intersection(*claim_family_sets)
            )
            claim_count = len([
                claim for claim in card.get("claims") or []
                if isinstance(claim, Mapping) and str(claim.get("text") or "").strip()
            ])
            broad_opinion_roundup = bool(
                claim_count >= 4
                and (
                    str(card.get("claim_type") or "").lower() in {"opinion", "hypothesis", "question"}
                    or re.search(r"(?:认为|评价|感觉|推荐|强于|好用|正合适|黑话|核查)", text(card), re.I)
                )
                and len({
                    str(item.get("sender_name") or "").strip()
                    for item in evidence
                    if str(item.get("sender_name") or "").strip()
                }) >= 3
            )
            card_text = text(card)
            single_vague_question = bool(
                claim_count == 1
                and len(unique_contents) == 1
                and (
                    str(card.get("claim_type") or "").lower() == "question"
                    or re.search(r"询问|是否|吗|？", card_text)
                )
                and re.search(r"(?:token|来源|哪家|另一家|这才是|真正的)", card_text, re.I)
                and not re.search(
                    r"(?:安排|截止|异常|故障|退款|额度|端点|速度|测试方案|课程|二维码|专利|bug)",
                    card_text,
                    re.I,
                )
            )
            source_text = " ".join(
                contents
                + [
                    str(claim.get("text") or "")
                    for claim in card.get("claims") or []
                    if isinstance(claim, Mapping)
                ]
            )
            subjective_signal = bool(re.search(
                r"(?:认为|觉得|感觉|印象|记得|貌似|好像|可能|似乎|"
                r"倒也|能用|好用|不好用|毫无必要|没(?:有)?必要|不需要|"
                r"更(?:强|弱|好|差)|强于|弱于|不如|比.{0,16}(?:强|弱|好|差))",
                source_text,
                re.I,
            ))
            objective_anchor = bool(re.search(
                r"(?:故障|异常|报错|不可见|拿不到|看不到|无法(?:获取|调用|登录|使用)|"
                r"发布|上线|开放|下线|关闭|部署|配置|端点|额度|计费|退款|"
                r"截止|安排|邀请|提交|修复|更新|版本|默认|推荐|耗时|"
                r"\d+(?:\.\d+)?\s*(?:%|倍|tps|个月|元|token))",
                source_text,
                re.I,
            ))
            sender_names = {
                str(item.get("sender_name") or "").strip()
                for item in evidence
                if str(item.get("sender_name") or "").strip()
            }
            evidence_times: List[datetime] = []
            for item in evidence:
                raw_time = str(item.get("timestamp") or "").strip()
                try:
                    evidence_times.append(
                        datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    )
                except (TypeError, ValueError):
                    continue
            close_consensus = False
            if len(evidence_times) == len(evidence) and evidence_times:
                try:
                    close_consensus = (
                        max(evidence_times) - min(evidence_times)
                        <= timedelta(minutes=30)
                    )
                except TypeError:
                    close_consensus = False
            evidence_term_sets = [
                _context_terms(item.get("content")) for item in evidence
            ]
            shared_consensus_terms = (
                set.intersection(*evidence_term_sets) if evidence_term_sets else set()
            )
            shared_consensus_terms = {
                term for term in shared_consensus_terms
                if term not in {
                    "认为", "觉得", "感觉", "可能", "好像", "貌似", "表示",
                    "提到", "这个", "那个", "现在", "已经", "还是", "确实",
                }
            }
            multi_author_consensus = bool(
                len(sender_names) >= 3
                and close_consensus
                and shared_consensus_terms
            )
            pure_subjective = bool(
                subjective_signal and not objective_anchor and not multi_author_consensus
            )
            return (
                emotional_only
                or vague_flash_only
                or fragment_pair
                or incompatible_story
                or broad_opinion_roundup
                or single_vague_question
                or pure_subjective
            )

        def score(card: Mapping[str, Any]) -> Tuple[int, int, int, str]:
            evidence = [item for item in card.get("evidence") or [] if isinstance(item, Mapping)]
            content = text(card)
            private = any(item.get("is_group") is False for item in evidence)
            concrete = bool(
                _ai_object_families(content)
                or re.search(r"(?:请|安排|确认|截止|决定|异常|故障|退款|额度|端点|速度|测试|课程|二维码|专利|bug)", content, re.I)
            )
            value = int(card.get("importance") or 0)
            value += min(18, len(refs(card)) * 5)
            value += 12 if private else 0
            value += 8 if concrete else 0
            value -= 18 if str(card.get("claim_type") or "").lower() == "question" else 0
            return value, len(refs(card)), int(card.get("confidence") or 0), str(card.get("title") or "")

        ordered = sorted((dict(card) for card in cards), key=score, reverse=True)
        selected: List[Dict[str, Any]] = []
        demoted: List[Dict[str, Any]] = []
        selected_refs: List[set] = []
        for card in ordered:
            card_refs = refs(card)
            compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text(card).casefold())
            low_information = low_information_card(card) or bool(
                compact
                and len(compact) <= 18
                and re.search(r"(?:困死|累死|笑死|吓哭|无语|哈哈|笑了|绝了|好烦|卧槽|我靠)", compact)
            )
            duplicate = bool(
                card_refs
                and any(card_refs.issubset(existing) for existing in selected_refs)
            )
            if low_information or duplicate or len(selected) >= bounded_limit:
                demoted.append(card)
                continue
            selected.append(card)
            selected_refs.append(card_refs)
        return selected, demoted

    @staticmethod
    def _rank_leads(
        leads: Sequence[Dict[str, Any]], limit: int = 15
    ) -> List[Dict[str, Any]]:
        """Return a bounded deterministic queue, preserving private requests."""

        bounded_limit = max(1, min(int(limit), 20))

        def score(lead: Mapping[str, Any]) -> Tuple[int, int, int, str]:
            quotes = [item for item in lead.get("quotes") or [] if isinstance(item, Mapping)]
            content = " ".join(str(item.get("content") or "") for item in quotes)
            private = any(item.get("is_group") is False for item in quotes)
            concrete = bool(
                _ai_object_families(content)
                or re.search(r"(?:请|安排|确认|截止|决定|异常|故障|退款|额度|端点|速度|测试|课程|二维码|专利)", content, re.I)
            )
            value = int(lead.get("importance") or 0)
            value += 30 if private else 0
            value += 10 if concrete else 0
            value += min(8, len(quotes) * 2)
            return value, len(quotes), len(content), str(lead.get("title") or "")

        filtered: List[Dict[str, Any]] = []
        for item in leads:
            value = dict(item)
            quotes = [quote for quote in value.get("quotes") or [] if isinstance(quote, Mapping)]
            content = re.sub(r"\s+", " ", " ".join(str(quote.get("content") or "") for quote in quotes)).strip()
            compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", content.casefold())
            unique_contents = {
                re.sub(r"\s+", "", str(quote.get("content") or ""))
                for quote in quotes
                if str(quote.get("content") or "").strip()
            }
            private = any(quote.get("is_group") is False for quote in quotes)
            emotional_only = bool(
                compact
                and len(compact) <= 12
                and re.search(r"(?:困死|累死|笑死|吓哭|无语|牛逼|哈哈|笑了|绝了|好烦|卧槽|我靠)", compact)
                and not re.search(r"(?:请|安排|确认|截止|决定|异常|故障|退款|额度|端点|速度|测试|课程|二维码|专利)", content, re.I)
            )
            vague_flash_only = bool(
                unique_contents
                and re.search(r"flash", " ".join(unique_contents), re.I)
                and all(re.search(r"(?:这才是|真正的|吗|嘛)", quote_text) for quote_text in unique_contents)
            )
            fragment_pair = bool(
                len(unique_contents) <= 2
                and any(re.fullmatch(r"[a-z]+(?:[a-z\s-]+)?", quote_text, re.I) for quote_text in unique_contents)
                and any(re.search(r"(?:吓哭|困死|累死|笑死|无语)", quote_text) for quote_text in unique_contents)
            )
            if not private and (emotional_only or vague_flash_only or fragment_pair):
                continue
            filtered.append(value)
        return sorted(filtered, key=score, reverse=True)[:bounded_limit]

    @staticmethod
    def _drop_contextless_leads(
        leads: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Drop single-quote fragments that cannot stand without prior turns."""

        output: List[Dict[str, Any]] = []
        for lead in leads:
            value = dict(lead)
            quotes = [
                item for item in value.get("quotes") or [] if isinstance(item, Mapping)
            ]
            if len(quotes) != 1:
                output.append(value)
                continue
            content = re.sub(
                r"\s+", " ", str(quotes[0].get("content") or "")
            ).strip()
            title = re.sub(r"\s+", " ", str(value.get("title") or "")).strip()
            combined = "%s %s" % (title, content)

            generic_or_pronominal = bool(re.search(
                r"^(?:这|这个|这些|那|那个|那些|它|其|该(?:模型|结果|方案|东西)|"
                r"新模型(?:\s|，|,|稳定|速度|性能)|从\s*\d)",
                content,
                re.I,
            ))
            bare_future_reply = bool(re.search(
                r"^(?:今天|明天|后天|稍后|晚些时候|上午|下午|晚上).{0,16}"
                r"(?:应该|可能|大概|会).{0,8}(?:说|讲|回复|通知|提|公布)[了。.!！?？]*$",
                content,
            ))
            omitted_processing_object = bool(re.search(
                r"(?:可以|能够|能)\s*(?:用\s*)?[A-Za-z][A-Za-z0-9._-]*\s*"
                r"(?:处理|做|跑|生成)(?:\s*(?:但|不过|只是|效果)|[，,。.!！?？]*$)",
                content,
                re.I,
            ))
            bare_metric_change = bool(
                re.search(r"^(?:从|由)?\s*\d+(?:\.\d+)?\s*(?:token/s|tps|%|倍|元)", content, re.I)
                and not _ai_object_families(content)
            )
            generic_meta_result = bool(re.search(
                r"^(?:这|那|这个|那个)?(?:只|仅)?是?(?:模型|系统|工具)?输出的(?:结果|内容)$",
                re.sub(r"[。.!！?？\s]", "", content),
                re.I,
            ))
            explicit_object = bool(
                _ai_object_families(combined)
                or re.search(
                    r"(?:[A-Z][A-Za-z0-9._-]{2,}|"
                    r"[\u4e00-\u9fff]{2,8}(?:项目|任务|课程|网站|接口|端点|账号|会员|订单|文件|报告|方案))",
                    combined,
                )
                or re.search(
                    r"[\u4e00-\u9fff·]{2,8}(?:称|表示|确认|决定|安排|邀请|提交)",
                    content,
                )
            )
            dependent = (
                bare_future_reply
                or omitted_processing_object
                or bare_metric_change
                or generic_meta_result
                or (generic_or_pronominal and not explicit_object)
            )
            if dependent or not explicit_object:
                continue
            output.append(value)
        return output

    @staticmethod
    def _recovered_quote_card(
        finding: Mapping[str, Any],
        candidate_by_ref: Mapping[str, Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Build a quote-only card for a tightly bound recovered event.

        Two messages are not enough by themselves.  At least two independently
        linkable sources must name the same hard object and the same event
        family.  Any ambiguity fails closed to the lead lane.
        """

        event_patterns = {
            "quota": r"额度|用量|消耗|限额|容量上限|token",
            "reset": r"重置|reset",
            "risk": r"封禁|封号|锁区|风控|异常|故障",
            "decision": r"决定|确认|采用|改成|换成|安排|提交|截止",
            "performance": r"表现|性能|速度|质量|好于|优于|弱于",
        }
        sources: List[Dict[str, Any]] = []
        seen_messages: set = set()
        requested_refs = list(dict.fromkeys(str(ref) for ref in finding.get("evidence_refs") or [] if str(ref)))
        if len(requested_refs) < 2 or any(ref not in candidate_by_ref for ref in requested_refs):
            return None
        for ref_text in requested_refs:
            source = candidate_by_ref.get(ref_text)
            if not ref_text or not isinstance(source, Mapping):
                continue
            content = re.sub(r"\s+", " ", str(source.get("content") or "")).strip()
            message_id = str(source.get("_source_message_id") or source.get("message_id") or ref_text)
            if not content or message_id in seen_messages:
                continue
            seen_messages.add(message_id)
            domains = _event_domain_tags(content) & _AI_OBJECT_DOMAINS
            events = {name for name, pattern in event_patterns.items() if re.search(pattern, content, re.I)}
            sources.append(
                {
                    "evidence_ref": ref_text,
                    "message_id": message_id,
                    "chat_name": source.get("chat_name"),
                    "sender_name": source.get("sender_name"),
                    "timestamp": source.get("timestamp"),
                    "content": content,
                    "_domains": domains,
                    "_events": events,
                }
            )
        if len(sources) < 2 or not _event_domains_compatible(sources):
            return None

        shared_domains = set.intersection(*(source["_domains"] for source in sources))
        shared_events = set.intersection(*(source["_events"] for source in sources))
        if not shared_domains or not shared_events:
            return None

        evidence = [
            {key: value for key, value in source.items() if not key.startswith("_")}
            for source in sources
        ]
        quote_card: Dict[str, Any] = {
            "kind": "recovered_quote_card",
            "presentation_mode": "recovered_quote_card",
            "title": str(finding.get("title") or "原文证据汇编"),
            "category": str(finding.get("category") or "事件"),
            "value_type": str(finding.get("value_type") or "event"),
            "importance": int(finding.get("importance") or 0),
            "confidence": min(65, int(finding.get("confidence") or 55)),
            "claim_type": "reported_claim",
            "claim_status": "unverified_chat",
            "claim_label": "原文汇编",
            "claim_boundary": "仅为聊天原文汇编，未作事实核验。",
            "recovered": True,
            "evidence_refs": [str(item["evidence_ref"]) for item in evidence],
            "evidence": evidence,
            "quotes": [dict(item) for item in evidence],
        }
        BridgeRequestHandler._finding_attribution(quote_card, evidence)
        return quote_card

    def _user_identity(self) -> Dict[str, Any]:
        """Resolve who the brief's subject is for the AI and the UI.

        The operator-configured ``profile.self_name`` wins; otherwise the
        adapter's locally discovered account nickname is used.  The value is
        only a display name, never an internal wxid.
        """

        profile = (self.server.settings.snapshot().get("profile") or {})
        configured = str(profile.get("self_name") or "").strip()
        adapter = getattr(self.server.service, "adapter", None)
        adapter_name = str(getattr(adapter, "self_display_name", "") or "").strip()
        display = configured or (adapter_name if adapter_name and adapter_name != "我" else "") or "我"
        roles = [str(value).strip() for value in (profile.get("roles") or []) if str(value).strip()][:8]
        organizations = [
            str(value).strip()
            for value in (profile.get("organizations") or [])
            if str(value).strip()
        ][:8]
        return {
            "display_name": display,
            "adapter_display_name": adapter_name,
            "configured": bool(configured),
            "roles": roles,
            "organizations": organizations,
            "note": "候选中 is_self=true 的消息均由用户本人发出；整份简报以用户本人为主体视角。",
        }

    @staticmethod
    def _finding_attribution(
        finding: Dict[str, Any],
        evidence: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Attach deterministic who-said-what attribution to one finding.

        Model-provided ``speakers`` are kept only when their names match the
        cited evidence; otherwise the local layer derives the attribution
        from the evidence itself so every published finding always answers
        “谁提出/谁说出了什么”.
        """

        participants: List[Dict[str, Any]] = []
        seen: set = set()
        for entry in evidence:
            name = str(entry.get("sender_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            participants.append(
                {
                    "name": name,
                    "is_self": name == "我",
                    "chat_name": str(entry.get("chat_name") or ""),
                    "statement": re.sub(r"\s+", " ", str(entry.get("content") or "")).strip()[:80],
                }
            )
        known = {item["name"] for item in participants}
        speakers: List[Dict[str, Any]] = []
        raw_speakers = finding.get("speakers")
        if isinstance(raw_speakers, list):
            for item in raw_speakers:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name") or "").strip()
                if not name or (known and name not in known):
                    continue
                speakers.append(
                    {
                        "name": name[:80],
                        "role": str(item.get("role") or "").strip()[:24] or "提到",
                        "statement": str(item.get("statement") or "").strip()[:120],
                    }
                )
                if len(speakers) >= 8:
                    break
        if not speakers:
            speakers = [
                {
                    "name": item["name"],
                    "role": "提出" if index == 0 else "回应",
                    "statement": item["statement"],
                }
                for index, item in enumerate(participants[:8])
            ]
        finding["participants"] = participants[:8]
        finding["speakers"] = speakers
        return finding

    @staticmethod
    def _merge_draw_findings(
        findings: Sequence[Dict[str, Any]],
        candidate_by_ref: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Merge the same story found by independent draws.

        Draws disagree on wording but cite the same evidence.  Citations that
        overlap by at least half of the smaller reference set mark the same
        story; the best-written version wins and the merged card carries the
        union of every validated reference.
        """

        groups: List[Dict[str, Any]] = []
        for finding in findings:
            ref_set = {
                str(ref) for ref in (finding.get("evidence_refs") or []) if str(ref)
            }
            placed = None
            for group in groups:
                if not ref_set or not group["refs"]:
                    continue
                shared = ref_set & group["refs"]
                if not shared:
                    continue
                smaller = min(len(ref_set), len(group["refs"]))
                if len(shared) >= max(1, int(smaller * 0.5)):
                    placed = group
                    break
            if placed is None:
                groups.append({"refs": set(ref_set), "items": [finding]})
            else:
                placed["refs"] |= ref_set
                placed["items"].append(finding)
        merged: List[Dict[str, Any]] = []
        for group in groups:
            items = list(group["items"])
            best = max(
                items,
                key=lambda item: (
                    1 if is_editorial_title(str(item.get("title") or "")) else 0,
                    len(str(item.get("narrative") or "")) >= 150,
                    len(item.get("evidence_refs") or []),
                    int(item.get("importance") or 0),
                ),
            )
            combined_refs: List[str] = []
            for item in [best] + [entry for entry in items if entry is not best]:
                for ref in item.get("evidence_refs") or []:
                    ref_text = str(ref)
                    if ref_text in candidate_by_ref and ref_text not in combined_refs:
                        combined_refs.append(ref_text)
            evidence = []
            for ref in combined_refs[:8]:
                source = candidate_by_ref[ref]
                evidence.append(
                    {
                        "evidence_ref": ref,
                        "message_id": source.get("_source_message_id"),
                        "chat_name": source.get("chat_name"),
                        "sender_name": source.get("sender_name"),
                        "timestamp": source.get("timestamp"),
                        "content": source.get("content"),
                        "is_group": source.get("is_group"),
                    }
                )
            merged_finding = dict(best)
            merged_claims: List[Dict[str, Any]] = []
            claim_keys: set = set()
            for source_item in [best] + [entry for entry in items if entry is not best]:
                for raw_claim in source_item.get("claims") or []:
                    if not isinstance(raw_claim, Mapping):
                        continue
                    text = _model_claim_text(raw_claim)
                    refs = [
                        ref for ref in _model_claim_refs(raw_claim)
                        if ref in candidate_by_ref
                    ]
                    if not text or not refs:
                        continue
                    compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text.casefold())
                    sender_names = [
                        str(candidate_by_ref[ref].get("sender_name") or "").strip()
                        for ref in refs
                        if ref in candidate_by_ref
                    ]
                    semantic = text.casefold()
                    for sender_name in sender_names:
                        if sender_name:
                            semantic = semantic.replace(sender_name.casefold(), "")
                    semantic = re.sub(
                        r"(?:群成员|群内成员|有人|一位成员|表示|陈述|提到|认为|在群内|在群聊中|这个|该|称|说)",
                        "",
                        semantic,
                    )
                    semantic = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", semantic)
                    key = (semantic or compact, tuple(refs))
                    if key in claim_keys:
                        continue
                    claim_keys.add(key)
                    merged_claims.append({"text": text, "evidence_refs": refs})
            merged_finding["evidence_refs"] = combined_refs
            merged_finding["evidence"] = evidence
            if merged_claims:
                merged_finding["claims"] = merged_claims
                claim_prose = "；".join(claim["text"] for claim in merged_claims)
                merged_finding["summary"] = claim_prose
                merged_finding["narrative"] = claim_prose
                merged_finding["what_changed"] = claim_prose
                merged_finding["core_conclusion"] = merged_claims[0]["text"]
            merged_finding["importance"] = max(int(item.get("importance") or 0) for item in items)
            merged_finding["confidence"] = max(int(item.get("confidence") or 0) for item in items)
            merged_finding["draw_count"] = len(items)
            BridgeRequestHandler._bound_ai_finding(merged_finding, evidence)
            BridgeRequestHandler._finding_attribution(merged_finding, evidence)
            merged.append(merged_finding)
        return merged

    def _ai_context_payload(
        self,
        baseline: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Build aggregate context without leaking private message IDs."""

        source_to_ref = {
            str(item.get("_source_message_id")): str(item.get("evidence_ref"))
            for item in candidates
            if item.get("_source_message_id") and item.get("evidence_ref")
        }

        def refs(values: Any) -> List[str]:
            if isinstance(values, str):
                values = [values]
            output = []
            for value in values or []:
                ref = source_to_ref.get(str(value))
                if ref and ref not in output:
                    output.append(ref)
            return output[:8]

        def safe(value: Any, limit: int = 240) -> str:
            return _redact_ai_content(str(value or "").strip())[:limit]

        topic_context = []
        for item in list(baseline.get("topic_briefs") or [])[:10]:
            evidence = item.get("evidence") or []
            examples = []
            for entry in evidence[:3]:
                if not isinstance(entry, Mapping):
                    continue
                ref = source_to_ref.get(str(entry.get("message_id") or ""))
                if not ref:
                    continue
                examples.append(
                    {
                        "evidence_ref": ref,
                        "sender_alias": safe(entry.get("sender_name"), 80),
                        "content": safe(entry.get("content"), 220),
                    }
                )
            topic_context.append(
                {
                    "topic": safe(item.get("topic"), 80),
                    "message_count": int(item.get("message_count") or 0),
                    "chat_count": int(item.get("chat_count") or 0),
                    "resource_count": int(item.get("resource_count") or 0),
                    "high_information_count": int(item.get("high_information_count") or 0),
                    "average_score": int(item.get("average_score") or 0),
                    "summary": safe(item.get("summary"), 220),
                    "why_it_matters": safe(item.get("why_it_matters"), 220),
                    "entities": [safe(value, 80) for value in list(item.get("entities") or [])[:12]],
                    "participants": [safe(value, 80) for value in list(item.get("participants") or [])[:12]],
                    "claim_type": safe(item.get("claim_type"), 40),
                    "claim_status": safe(item.get("claim_status"), 40),
                    "claim_boundary": safe(item.get("claim_boundary"), 220),
                    "speaker_points": [safe(value, 240) for value in list(item.get("speaker_points") or [])[:4]],
                    "examples": examples,
                    "evidence_refs": refs(
                        entry.get("message_id")
                        for entry in evidence
                        if isinstance(entry, Mapping)
                    ),
                }
            )

        discussion_context = []
        for item in list(baseline.get("discussion_episodes") or [])[:10]:
            discussion_context.append(
                {
                    "chat_alias": safe(item.get("chat_name"), 100),
                    "start": safe(item.get("start"), 40),
                    "end": safe(item.get("end"), 40),
                    "message_count": int(item.get("message_count") or 0),
                    "participant_count": int(item.get("participant_count") or 0),
                    "substantive_count": int(item.get("substantive_count") or 0),
                    "resource_count": int(item.get("resource_count") or 0),
                    "topics": [
                        safe(topic.get("topic"), 80)
                        for topic in list(item.get("topics") or [])[:6]
                        if isinstance(topic, Mapping)
                    ],
                    "summary": safe(item.get("summary"), 260),
                    "examples": [
                        {
                            "evidence_ref": source_to_ref.get(str(entry.get("message_id") or "")),
                            "sender_alias": safe(entry.get("sender_name"), 80),
                            "content": safe(entry.get("content"), 220),
                        }
                        for entry in list(item.get("evidence_samples") or [])[:3]
                        if isinstance(entry, Mapping)
                        and source_to_ref.get(str(entry.get("message_id") or ""))
                    ],
                    "evidence_refs": refs(item.get("evidence")),
                }
            )

        event_context = []
        for item in list(baseline.get("event_briefs") or [])[:20]:
            event_context.append(
                {
                    "title": safe(item.get("title"), 120),
                    "lane": safe(item.get("lane"), 30),
                    "start": safe(item.get("start"), 40),
                    "end": safe(item.get("end"), 40),
                    "related_chat_count": int(item.get("related_chat_count") or 0),
                    "related_people_count": int(item.get("related_people_count") or 0),
                    "importance": int(item.get("importance") or 0),
                    "summary": safe(item.get("summary"), 260),
                    "canonical_topics": [safe(value, 80) for value in list(item.get("canonical_topics") or [])[:8]],
                    "people": [safe(value, 80) for value in list(item.get("people") or [])[:12]],
                    "claim_type": safe(item.get("claim_type"), 40),
                    "claim_status": safe(item.get("claim_status"), 40),
                    "claim_boundary": safe(item.get("claim_boundary"), 220),
                    "domain_tags": [safe(value, 40) for value in list(item.get("domain_tags") or [])[:4]],
                    "evidence_refs": refs(
                        entry.get("message_id")
                        for entry in list(item.get("evidence") or [])
                        if isinstance(entry, Mapping)
                    ),
                }
            )

        unformed_context = []
        for item in list(baseline.get("unformed_dynamics") or [])[:60]:
            unformed_context.append(
                {
                    "kind": safe(item.get("kind"), 30),
                    "summary": safe(item.get("summary"), 280),
                    "message_count": int(item.get("message_count") or 0),
                    "start": safe(item.get("start"), 40),
                    "end": safe(item.get("end"), 40),
                    "people": [safe(value, 80) for value in list(item.get("people") or [])[:8]],
                    "chats": [
                        safe(entry.get("chat_name"), 100)
                        for entry in list(item.get("chats") or [])[:4]
                        if isinstance(entry, Mapping)
                    ],
                    "evidence_refs": refs(item.get("message_ids")),
                }
            )

        return {
            "user_identity": self._user_identity(),
            "situation": {
                "headline": safe((baseline.get("situation") or {}).get("headline"), 220),
                "points": [
                    safe(point, 220)
                    for point in list((baseline.get("situation") or {}).get("points") or [])[:4]
                ],
            },
            "summary": dict(baseline.get("summary") or {}),
            "insight_breakdown": list(baseline.get("insight_breakdown") or [])[:8],
            "topic_briefs": topic_context,
            "discussion_windows": discussion_context,
            "event_candidates": event_context,
            "unformed_dynamics": unformed_context,
        }

    @staticmethod
    def _packet_ai_context(
        ai_context: Mapping[str, Any],
        evidence_refs: Sequence[str],
    ) -> Dict[str, Any]:
        """Restrict aggregate context to facts addressable inside one packet."""

        allowed = {str(ref) for ref in evidence_refs if str(ref)}
        context: Dict[str, Any] = {
            "user_identity": dict(ai_context.get("user_identity") or {}),
        }
        for key in (
            "topic_briefs",
            "discussion_windows",
            "event_candidates",
            "unformed_dynamics",
        ):
            selected = []
            for raw in ai_context.get(key) or []:
                if not isinstance(raw, Mapping):
                    continue
                refs = [str(ref) for ref in raw.get("evidence_refs") or [] if str(ref)]
                # An aggregate summary may synthesize every cited row.  Even
                # one out-of-packet ref therefore makes the whole item unsafe
                # for this packet; trimming only the ref list would leave the
                # cross-packet facts in its prose fields.
                if not refs or not set(refs).issubset(allowed):
                    continue
                item = dict(raw)
                item["evidence_refs"] = refs
                if isinstance(item.get("examples"), list):
                    item["examples"] = [
                        dict(example)
                        for example in item["examples"]
                        if isinstance(example, Mapping)
                        and str(example.get("evidence_ref") or "") in allowed
                    ]
                selected.append(item)
            context[key] = selected
        return context

    @staticmethod
    def _bound_ai_finding(
        finding: Dict[str, Any],
        evidence: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Apply a local epistemic boundary after the model returns JSON."""

        evidence_text = " ".join(str(item.get("content") or "") for item in evidence)
        local_profile = _claim_profile(evidence_text)
        requested_type = str(finding.get("claim_type") or "").strip().lower()
        allowed = {"reported_claim", "reported_experience", "opinion", "hypothesis", "question"}
        claim_type = requested_type if requested_type in allowed else str(local_profile.get("claim_type") or "reported_claim")
        if claim_type not in allowed:
            claim_type = "reported_claim"
        labels = {
            "reported_claim": ("聊天事实陈述", "当前只能确认聊天中有人这样陈述，事实与因果关系尚未联网核验。"),
            "reported_experience": ("个人经历", "当前只能确认个人经历或群内转述，不能代表平台规则或普遍事实。"),
            "opinion": ("群内观点", "当前只能确认群内存在这一观点，不能把它当作已验证事实或方案结论。"),
            "hypothesis": ("推测或传闻", "当前只能确认聊天中出现了这一推测或传闻，因果关系和影响范围尚未核实。"),
            "question": ("问题", "当前只能确认原文提出了问题，不能把问题本身当作结论。"),
        }
        label, boundary = labels[claim_type]
        finding["claim_type"] = claim_type
        finding["claim_status"] = "unverified_chat"
        finding["claim_label"] = label
        finding["claim_boundary"] = boundary
        if not str(finding.get("claim_basis") or "").strip():
            finding["claim_basis"] = "聊天证据；尚未联网核验"

        combined = " ".join(
            str(finding.get(key) or "")
            for key in ("summary", "narrative", "core_conclusion", "what_changed", "why_it_matters")
        )
        unsupported = bool(re.search(
            r"(?:因此|所以|说明|表明|证明|证实|导致|成为.{0,8}(?:替代|方案)|合规替代|普遍|一定会|必然)",
            combined,
        ))
        if unsupported or claim_type in {"opinion", "hypothesis", "question"}:
            finding["confidence"] = min(int(finding.get("confidence") or 0), 58)
            finding["core_conclusion"] = boundary
            finding["why_it_matters"] = boundary
        # A model occasionally satisfies the schema by copying one entire
        # source line into summary/narrative.  Keep the line in evidence but
        # replace the visible field with an honest editorial placeholder.
        for key in ("summary", "narrative"):
            text = str(finding.get(key) or "").strip()
            compact_text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
            copied = any(
                len(compact_text) >= 36
                and (
                    compact_text == re.sub(r"[^\w\u4e00-\u9fff]", "", str(item.get("content") or ""))
                    or compact_text in re.sub(r"[^\w\u4e00-\u9fff]", "", str(item.get("content") or ""))
                )
                for item in evidence
            )
            if copied:
                sender = str(evidence[0].get("sender_name") or "相关成员") if evidence else "相关成员"
                finding[key] = "%s的原文涉及该事项；当前未形成独立编辑归纳，证据已保留供回看。" % sender
        return finding

    def _ai_analysis(self, body: Dict[str, Any]) -> None:
        if body.get("confirm") is not True:
            return self._json(
                {
                    "error": "confirmation_required",
                    "message": "AI 分析会把脱敏候选文本发送到配置的 AI 服务，必须显式 confirm=true",
                },
                400,
            )
        try:
            query = {
                "start": [str(body.get("start") or body.get("start_date") or "")],
                "end": [str(body.get("end") or body.get("end_date") or "")],
                "period": [str(body.get("period") or "")],
            }
            start_at, end_at, start_day, end_day = _date_range(
                query,
                self.server.service.policy.timezone_name,
            )
            candidate_limit = max(20, min(int(body.get("limit", 120)), 200))
        except (TypeError, ValueError) as exc:
            return self._json({"error": "invalid_range", "message": str(exc)}, 400)

        messages = self._analysis_enrich(self.server.store.messages_between(
            start_at,
            end_at,
            (body.get("chat") or None),
            200_000,
        ))
        baseline = analyze_messages(
            messages,
            start_at,
            end_at,
            self.server.service.policy.timezone_name,
            profile=(self.server.settings.snapshot().get("profile") or {}),
        )
        priority_message_ids = [
            str(entry.get("message_id") or "")
            for event in list(baseline.get("event_briefs") or [])[:40]
            for entry in list(event.get("evidence") or [])[:3]
            if isinstance(entry, Mapping) and entry.get("message_id")
        ]
        priority_message_ids.extend(
            str(message_id)
            for matter in list(baseline.get("unformed_dynamics") or [])[:80]
            for message_id in list(matter.get("message_ids") or [])[:6]
            if message_id
        )
        candidates = build_ai_context(messages, candidate_limit, priority_message_ids)
        generator = self._ai_generator()
        window = {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "timezone": self.server.service.policy.timezone_name,
        }
        ai_context = self._ai_context_payload(baseline, candidates)
        window_key = "%s|%s" % (start_day.isoformat(), end_day.isoformat())
        packet_mode = body.get("packet_mode") is True
        valid_efforts = {"none", "low", "medium", "high", "xhigh", "max"}

        def stage_effort(body_key: str, environment_key: str, default: str) -> str:
            value = str(
                body.get(body_key)
                or os.environ.get(environment_key)
                or default
            ).strip().lower()
            return value if value in valid_efforts else default

        try:
            draw_count = max(1, min(int(body.get("draws", 2)), 3))
            packet_max_items = max(1, min(int(body.get("packet_max_items", 12)), 50))
            packet_max_span = max(
                1,
                min(
                    int(body.get("packet_max_span", body.get("packet_max_span_minutes", 45))),
                    24 * 60,
                ),
            )
            packet_workers = max(1, min(int(body.get("packet_workers", 4)), 8))
            packet_draws = max(
                1,
                min(
                    int(
                        body.get("packet_draws")
                        or os.environ.get("OPENAI_WECHAT_PACKET_DRAWS")
                        or 1
                    ),
                    3,
                ),
            )
            packet_max_tokens = max(
                1024,
                min(
                    int(
                        body.get("packet_max_tokens")
                        or os.environ.get("OPENAI_WECHAT_PACKET_MAX_TOKENS")
                        or 16384
                    ),
                    131072,
                ),
            )
        except (TypeError, ValueError):
            draw_count = 2
            packet_max_items = 12
            packet_max_span = 45
            packet_workers = 4
            packet_draws = 1
            packet_max_tokens = 16384
        packet_reasoning_effort = stage_effort(
            "packet_reasoning_effort",
            "OPENAI_WECHAT_PACKET_REASONING_EFFORT",
            "medium",
        )
        reducer_reasoning_effort = stage_effort(
            "reducer_reasoning_effort",
            "OPENAI_WECHAT_REDUCER_REASONING_EFFORT",
            "max",
        )
        reducer_enabled = packet_mode and body.get("editorial_reducer") is not False
        packet_generator = self._ai_generator_with_effort(
            generator,
            packet_reasoning_effort,
            packet_max_tokens,
        )
        packet_config = {
            "max_items": packet_max_items,
            "max_span_minutes": packet_max_span,
            "workers": packet_workers,
            "draws": packet_draws,
        }
        packets = (
            build_ai_dialogue_packets(
                candidates,
                max_items=packet_max_items,
                max_span_minutes=packet_max_span,
            )
            if packet_mode else []
        )
        cache_key = window_key
        if packet_mode:
            cache_key += "|packet|%d|%d|%d|draws=%d|%s|%d|reducer=%s|%s" % (
                packet_max_items,
                packet_max_span,
                packet_workers,
                packet_draws,
                packet_reasoning_effort,
                packet_max_tokens,
                int(reducer_enabled),
                reducer_reasoning_effort,
            )
        packet_errors: List[Dict[str, str]] = []
        try:
            with self.server.ai_analysis_lock:
                raw_results = (
                    None if body.get("force") is True
                    else self.server.ai_raw_by_window.get(cache_key)
                )
                packet_meta = dict(self.server.ai_packet_meta_by_window.get(cache_key) or {})
            if raw_results is None:
                raw_results = []
                if packet_mode:
                    def run_packet(
                        packet: Mapping[str, Any], packet_draw_index: int
                    ) -> Tuple[str, int, Dict[str, Any]]:
                        packet_id = str(packet.get("packet_id") or "")
                        packet_candidates = list(packet.get("candidates") or [])
                        packet_context = self._packet_ai_context(
                            ai_context,
                            list(packet.get("evidence_refs") or []),
                        )
                        try:
                            result = packet_generator.analyze(
                                window,
                                packet_candidates,
                                packet_context,
                                packet_mode=True,
                            )
                        except TypeError as exc:
                            # Compatibility for injected/test generators which
                            # predate the packet-specific extraction argument.
                            if "argument" not in str(exc) and "positional" not in str(exc):
                                raise
                            result = packet_generator.analyze(
                                window,
                                packet_candidates,
                                packet_context,
                            )
                        return packet_id, packet_draw_index, result

                    completed: List[Tuple[str, int, Dict[str, Any]]] = []
                    with ThreadPoolExecutor(max_workers=packet_workers) as executor:
                        future_to_packet = {
                            executor.submit(run_packet, packet, packet_draw_index): (
                                str(packet.get("packet_id") or ""),
                                packet_draw_index,
                            )
                            for packet in packets
                            for packet_draw_index in range(packet_draws)
                        }
                        for future in as_completed(future_to_packet):
                            packet_id, packet_draw_index = future_to_packet[future]
                            try:
                                result_packet_id, result_draw_index, draw_value = future.result()
                                completed.append((result_packet_id, result_draw_index, draw_value))
                            except Exception as exc:
                                packet_errors.append({
                                    "packet_id": packet_id,
                                    "packet_draw_index": packet_draw_index,
                                    "message": str(exc),
                                })
                    packet_errors.sort(key=lambda item: (item["packet_id"], int(item.get("packet_draw_index") or 0)))
                    for packet_id, packet_draw_index, draw_value in sorted(
                        completed, key=lambda item: (item[0], item[1])
                    ):
                        value_with_packet = dict(draw_value)
                        value_with_packet["_packet_id"] = packet_id
                        value_with_packet["_packet_draw_index"] = packet_draw_index
                        raw_results.append(value_with_packet)
                else:
                    # The default path deliberately retains its repeated global
                    # draws.  Only packet_mode changes the execution strategy.
                    draw_errors: List[str] = []
                    for _draw_index in range(draw_count):
                        try:
                            try:
                                draw_value = generator.analyze(window, candidates, ai_context)
                            except TypeError as exc:
                                if "positional" not in str(exc) and "argument" not in str(exc):
                                    raise
                                draw_value = generator.analyze(window, candidates)
                            raw_results.append(draw_value)
                        except AnalysisGenerationError as exc:
                            draw_errors.append(str(exc))
                    packet_errors = [
                        {"packet_id": "global-%d" % index, "message": message}
                        for index, message in enumerate(draw_errors)
                    ]
                if not raw_results:
                    error_messages = [item["message"] for item in packet_errors]
                    raise AnalysisGenerationError("；".join(error_messages) or "AI 分析没有返回可用结果")
                packet_meta = {"packet_errors": list(packet_errors)}
                with self.server.ai_analysis_lock:
                    self.server.ai_raw_by_window[cache_key] = raw_results
                    self.server.ai_packet_meta_by_window[cache_key] = packet_meta
            else:
                packet_errors = list(packet_meta.get("packet_errors") or [])
        except AnalysisGenerationError as exc:
            status = 409 if not generator.configured else 502
            return self._json(
                {
                    "error": "ai_not_configured" if not generator.configured else "ai_analysis_failed",
                    "message": str(exc),
                    "source": "rules_fallback",
                    "provider_status": "blocked" if not generator.configured else "failed",
                    "llm_accepted": False,
                    "fallback_reason": "provider_blocked" if not generator.configured else "provider_failed",
                    "window": window,
                    "candidate_count": len(candidates),
                    "rule_baseline": {
                        "narrative": baseline.get("narrative"),
                        "summary": baseline.get("summary"),
                    },
                },
                status,
            )

        candidate_by_ref = {
            str(item.get("evidence_ref")): item for item in candidates
        }
        candidate_by_source = {
            str(item.get("_source_message_id")): str(item.get("evidence_ref"))
            for item in candidates
            if item.get("_source_message_id") and item.get("evidence_ref")
        }
        # The brief-level fields come from the draw with the most usable
        # findings; findings from every draw enter one validated pool.
        value = max(
            raw_results,
            key=lambda item: len(
                [entry for entry in (item.get("findings") or []) if isinstance(entry, dict)]
            ),
        )
        merged_raw_findings: List[Any] = []
        draw_stats: List[Dict[str, Any]] = []
        usage_keys = (
            "prompt_tokens", "input_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens", "completion_tokens", "output_tokens",
            "reasoning_tokens", "attempts", "retries",
        )

        def safe_usage(raw: Any) -> Dict[str, int]:
            source = raw if isinstance(raw, Mapping) else {}
            normalized: Dict[str, int] = {}
            for key in usage_keys:
                try:
                    normalized[key] = max(0, int(source.get(key) or 0))
                except (TypeError, ValueError, OverflowError):
                    normalized[key] = 0
            return normalized

        def summed_usage(values: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
            total = {key: 0 for key in usage_keys}
            for raw in values:
                normalized = safe_usage(raw)
                for key in usage_keys:
                    total[key] += normalized[key]
            return total

        packet_refs_by_id = {
            str(packet.get("packet_id") or ""): {
                str(ref) for ref in packet.get("evidence_refs") or [] if str(ref)
            }
            for packet in packets
        }
        for raw_value in raw_results:
            raw_findings = [
                entry for entry in (raw_value.get("findings") or []) if isinstance(entry, dict)
            ]
            draw_stat = {
                "findings": len(raw_findings),
                "accepted": 0,
                "rejected": 0,
                "reason_counts": {},
                "usage": safe_usage(raw_value.get("_usage")),
            }
            if raw_value.get("_packet_id"):
                draw_stat["packet_id"] = str(raw_value["_packet_id"])
                draw_stat["packet_draw_index"] = int(raw_value.get("_packet_draw_index") or 0)
            draw_stats.append(draw_stat)
            for finding_index, entry in enumerate(raw_findings):
                entry["_draw_index"] = len(draw_stats) - 1
                entry["_draw_finding_index"] = finding_index
                if packet_mode:
                    entry["_packet_id"] = str(raw_value.get("_packet_id") or "")
            merged_raw_findings.extend(raw_findings)
        findings = []
        rejected_findings: List[Dict[str, Any]] = []
        rejected_claims: List[Dict[str, Any]] = []
        incoherent_ref_ids = set()
        object_boundary_repaired = False

        def reject_finding(
            item: Mapping[str, Any],
            reason_code: str,
            raw_refs: Sequence[Any],
            valid_refs: Sequence[str],
            evidence: Sequence[Mapping[str, Any]],
            extra: Optional[Mapping[str, Any]] = None,
        ) -> None:
            draw_index = int(item.get("_draw_index") or 0)
            stats = draw_stats[draw_index]
            stats["rejected"] += 1
            reason_counts = stats["reason_counts"]
            reason_counts[reason_code] = int(reason_counts.get(reason_code) or 0) + 1
            finding_text = " ".join(
                str(item.get(key) or "")
                for key in (
                    "title", "summary", "narrative", "core_conclusion",
                    "what_changed", "why_it_matters", "keywords",
                )
            )
            evidence_domains = set()
            for entry in evidence:
                evidence_domains.update(_event_domain_tags(entry.get("content")))
            rejected_findings.append(
                {
                    "draw_index": draw_index,
                    "finding_index": int(item.get("_draw_finding_index") or 0),
                    "reason_code": reason_code,
                    "title": str(item.get("title") or ""),
                    "finding": {
                        key: item.get(key)
                        for key in (
                            "title", "summary", "narrative", "core_conclusion",
                            "what_changed", "why_it_matters", "keywords",
                        )
                    },
                    "ref_ids": [str(ref) for ref in raw_refs],
                    "valid_ref_ids": list(valid_refs),
                    "unknown_ref_ids": [
                        str(ref) for ref in raw_refs if str(ref) not in candidate_by_ref
                    ],
                    "finding_domains": sorted(
                        _event_domain_tags(finding_text) & _AI_OBJECT_DOMAINS
                    ),
                    "evidence_domains": sorted(evidence_domains),
                    "evidence": [dict(entry) for entry in evidence],
                }
            )
            if extra:
                rejected_findings[-1].update(dict(extra))

        for item in merged_raw_findings:
            if not isinstance(item, dict):
                continue
            raw_refs = item.get("ref_ids", [])
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            refs = [
                str(ref)
                for ref in raw_refs
                if str(ref) in candidate_by_ref
            ]
            if packet_mode:
                packet_id = str(item.get("_packet_id") or "")
                packet_refs = packet_refs_by_id.get(packet_id, set())
                outside_refs = [ref for ref in refs if ref not in packet_refs]
                if outside_refs:
                    reject_finding(
                        item,
                        "packet_ref_outside_packet",
                        raw_refs,
                        refs,
                        [],
                        extra={"packet_id": packet_id, "outside_ref_ids": outside_refs},
                    )
                    continue
            if not refs:
                # No local evidence means the model made an unsupported claim.
                reject_finding(item, "no_valid_evidence_refs", raw_refs, refs, [])
                continue
            evidence = []
            for ref in refs:
                source = candidate_by_ref[ref]
                evidence.append(
                    {
                        "evidence_ref": ref,
                        "message_id": source.get("_source_message_id"),
                        "chat_name": source.get("chat_name"),
                        "sender_name": source.get("sender_name"),
                        "timestamp": source.get("timestamp"),
                        "content": source.get("content"),
                        "is_group": source.get("is_group"),
                    }
                )
            if packet_mode:
                raw_claims = item.get("claims")
                if not isinstance(raw_claims, list) or not raw_claims:
                    reject_finding(
                        item,
                        "packet_claims_missing",
                        raw_refs,
                        refs,
                        evidence,
                        extra={"packet_id": str(item.get("_packet_id") or "")},
                    )
                    continue
                supported_claims: List[Dict[str, Any]] = []
                for claim_index, raw_claim in enumerate(raw_claims):
                    claim_reason = ""
                    claim_text = ""
                    claim_refs: List[str] = []
                    claim_evidence: List[Mapping[str, Any]] = []
                    if not isinstance(raw_claim, Mapping):
                        claim_reason = "claim_malformed"
                    else:
                        claim_text = _model_claim_text(raw_claim)
                        claim_refs = _model_claim_refs(raw_claim)
                        attribution_evidence = [
                            {
                                "sender_name": candidate_by_ref[ref].get("sender_name"),
                                "content": candidate_by_ref[ref].get("content"),
                            }
                            for ref in claim_refs
                            if ref in candidate_by_ref
                        ]
                        claim_text = self._ensure_claim_attribution(claim_text, attribution_evidence)
                        if not claim_text:
                            claim_reason = "claim_text_missing"
                        elif re.search(r"[:：]\s*$", claim_text):
                            # A label/introduction without the promised body is
                            # not an atomic fact.  Reject only this fragment so
                            # complete sibling claims in the card can survive.
                            claim_reason = "claim_incomplete_fragment"
                        elif not claim_refs:
                            claim_reason = "claim_evidence_refs_missing"
                        elif any(ref not in refs for ref in claim_refs):
                            claim_reason = "claim_ref_outside_finding"
                        else:
                            claim_evidence = [
                                {
                                    "evidence_ref": ref,
                                    "message_id": candidate_by_ref[ref].get("_source_message_id"),
                                    "chat_name": candidate_by_ref[ref].get("chat_name"),
                                    "sender_name": candidate_by_ref[ref].get("sender_name"),
                                    "timestamp": candidate_by_ref[ref].get("timestamp"),
                                    "content": candidate_by_ref[ref].get("content"),
                                }
                                for ref in claim_refs
                                if ref in candidate_by_ref
                            ]
                            claim_text = _preserve_claim_uncertainty(
                                claim_text, claim_evidence
                            )
                            claim_finding = {"summary": claim_text}
                            boundary = _finding_evidence_boundary_reason(
                                claim_finding, claim_evidence
                            )
                            unsupported = _finding_unsupported_narrative_claims(
                                claim_finding, claim_evidence
                            )
                            if boundary:
                                claim_reason = boundary
                            elif unsupported:
                                claim_reason = "claim_specific_missing_in_evidence"
                            elif not _claim_directly_supported(claim_text, claim_evidence):
                                claim_reason = "claim_not_directly_supported"
                    if claim_reason:
                        rejected_claims.append(
                            {
                                "packet_id": str(item.get("_packet_id") or ""),
                                "draw_index": int(item.get("_draw_index") or 0),
                                "finding_index": int(item.get("_draw_finding_index") or 0),
                                "claim_index": claim_index,
                                "reason_code": claim_reason,
                                "text": claim_text,
                                "evidence_refs": claim_refs,
                            }
                        )
                        continue
                    supported_claims.append(
                        {"text": claim_text, "evidence_refs": claim_refs}
                    )
                # The extractor may repeat the same atomic statement once per
                # speaker.  Preserve every citation while publishing the fact
                # only once.
                supported_claims = _deduplicate_model_claims(supported_claims)
                if not supported_claims:
                    reject_finding(
                        item,
                        "packet_no_supported_claims",
                        raw_refs,
                        refs,
                        evidence,
                        extra={"packet_id": str(item.get("_packet_id") or "")},
                    )
                    continue
                refs = list(dict.fromkeys(
                    ref
                    for claim in supported_claims
                    for ref in claim["evidence_refs"]
                ))
                evidence = [
                    {
                        "evidence_ref": ref,
                        "message_id": candidate_by_ref[ref].get("_source_message_id"),
                        "chat_name": candidate_by_ref[ref].get("chat_name"),
                        "sender_name": candidate_by_ref[ref].get("sender_name"),
                        "timestamp": candidate_by_ref[ref].get("timestamp"),
                        "content": candidate_by_ref[ref].get("content"),
                        "is_group": candidate_by_ref[ref].get("is_group"),
                    }
                    for ref in refs
                ]
                claim_prose = "；".join(claim["text"] for claim in supported_claims)
                item = dict(item)
                item.update(
                    {
                        "title": _headline_from_summary(claim_prose, evidence),
                        "summary": claim_prose,
                        "narrative": claim_prose,
                        "core_conclusion": supported_claims[0]["text"],
                        "what_changed": claim_prose,
                        "why_it_matters": "",
                        "reason": "仅保留逐条绑定到引用的事实声明。",
                        "uncertainty": "仅代表所引聊天原文，尚未外部核验。",
                        "next_step": "",
                        "claim_basis": "逐条引用绑定",
                        "keywords": [],
                        "speakers": [],
                        "claims": supported_claims,
                    }
                )
            boundary_reason = _finding_evidence_boundary_reason(item, evidence)
            if boundary_reason:
                # The model has crossed a hard object boundary.  Do not let
                # its prose survive merely because every individual ref is
                # valid.  This check also catches the inverse failure mode:
                # prose names one object while the cited refs belong to
                # another (or to no object at all).  The local event layer
                # below will rebuild separate evidence-bound findings.
                incoherent_ref_ids.update(refs)
                object_boundary_repaired = True
                reject_finding(item, boundary_reason, raw_refs, refs, evidence)
                continue
            unsupported_claims = _finding_unsupported_narrative_claims(item, evidence)
            if unsupported_claims:
                # The narrative states specifics (numbers, identifiers, speaker
                # attributions) that none of the cited evidence contains.
                # Those details cannot be traced back to a local message, so
                # the finding is demoted to the quote-only recovery lane
                # instead of being published as a card.
                incoherent_ref_ids.update(refs)
                object_boundary_repaired = True
                reject_finding(
                    item,
                    "narrative_claim_missing_in_evidence",
                    raw_refs,
                    refs,
                    evidence,
                    extra={"unsupported_claims": unsupported_claims},
                )
                continue
            finding = {
                key: item.get(key)
                for key in (
                    "title", "category", "value_type", "importance", "confidence",
                    "summary", "narrative", "core_conclusion", "keywords", "what_changed", "why_it_matters", "reason",
                    "uncertainty", "claim_type", "claim_basis", "next_step", "speakers",
                    "claims",
                )
            }
            title_context = " ".join(
                str(finding.get(key) or "")
                for key in ("summary", "narrative", "what_changed", "why_it_matters", "core_conclusion", "keywords")
            ) + " " + " ".join(str(entry.get("content") or "") for entry in evidence)
            finding["title"] = self._apply_editorial_headline(finding, evidence)
            keywords = finding.get("keywords") or []
            if isinstance(keywords, str):
                keywords = re.split(r"[,，、;；|\n]+", keywords)
            if not isinstance(keywords, list):
                keywords = []
            finding["keywords"] = [str(keyword).strip() for keyword in keywords if str(keyword).strip()][:6]
            try:
                importance = int(finding.get("importance") or 0)
            except (TypeError, ValueError):
                importance = 0
            try:
                confidence = int(finding.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0
            # Some OpenAI-compatible models honor the JSON shape but omit
            # numeric judgments.  Keep evidence as the authority and assign a
            # conservative local score instead of silently discarding a fully
            # grounded synthesis.
            if importance <= 0 and finding.get("why_it_matters") and finding.get("summary"):
                importance = 62 if len(refs) >= 2 else 55
            if confidence <= 0:
                confidence = 68 if len(refs) >= 2 else 56
            finding["importance"] = max(0, min(100, importance))
            finding["confidence"] = max(0, min(100, confidence))
            finding["evidence_refs"] = refs
            finding["evidence"] = evidence
            self._bound_ai_finding(finding, evidence)
            self._finding_attribution(finding, evidence)
            findings.append(finding)
            draw_stats[int(item.get("_draw_index") or 0)]["accepted"] += 1

        # Merge the same story across draws: identical or overlapping
        # citations mark the same event; the best-written version wins and
        # the merged card carries the union of validated references.
        findings = self._merge_draw_findings(findings, candidate_by_ref)

        source_by_message = {
            str(item.get("_source_message_id") or ""): item
            for item in candidates
            if item.get("_source_message_id")
        }
        coherence_recovered = False
        coverage_recovered = False
        represented_source_ids = {
            str(entry.get("message_id") or "")
            for finding in findings
            for entry in finding.get("evidence") or []
            if isinstance(entry, Mapping)
        }

        def recover_local_event(
            local_item: Mapping[str, Any],
            reason: str,
        ) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
            event_sources = [
                source_by_message[message_id]
                for message_id in local_item.get("message_ids") or []
                if message_id in source_by_message
            ]
            if not event_sources:
                return None
            local_domains = {
                str(value)
                for value in local_item.get("domain_tags") or []
                if value
            }
            explicit_local_domains = local_domains & _AI_OBJECT_DOMAINS
            if explicit_local_domains:
                anchored_sources = [
                    source
                    for source in event_sources
                    if _event_domain_tags(source.get("content")) & explicit_local_domains
                ]
                # A local event is not safe to recover if its own evidence
                # list contains only untagged lines while its summary claims
                # a concrete object.  This prevents the fallback lane from
                # reintroducing the same summary/evidence mismatch that the
                # model lane was just rejected for.
                if not anchored_sources:
                    return None
                event_sources = anchored_sources
            event_evidence = [
                {
                    "evidence_ref": str(source.get("evidence_ref") or ""),
                    "message_id": source.get("_source_message_id"),
                    "chat_name": source.get("chat_name"),
                    "sender_name": source.get("sender_name"),
                    "timestamp": source.get("timestamp"),
                    "content": source.get("content"),
                }
                for source in event_sources[:8]
                if source.get("evidence_ref")
            ]
            if not event_evidence or not _event_domains_compatible(event_evidence):
                return None
            local_summary = str(local_item.get("summary") or "本地事件边界已保留，原文证据见下方。")
            local_narrative = str(local_item.get("narrative") or local_summary)
            local_core = str(local_item.get("core_conclusion") or local_item.get("why_it_matters") or "")
            if _finding_evidence_boundary_violation(
                {
                    "title": local_item.get("title"),
                    "summary": local_summary,
                    "narrative": local_narrative,
                    "core_conclusion": local_core,
                    "keywords": local_item.get("tags") or [],
                },
                event_evidence,
            ):
                digest_parts = []
                for entry in event_evidence[:4]:
                    content = re.sub(r"\s+", " ", str(entry.get("content") or "")).strip()
                    if not content:
                        continue
                    if len(content) > 140:
                        content = content[:137].rstrip() + "..."
                    sender = str(entry.get("sender_name") or "相关成员")
                    digest_parts.append("%s提到“%s”" % (sender, content))
                evidence_digest = "；".join(digest_parts)
                local_summary = evidence_digest or "本地事件边界已保留，原文证据见下方。"
                local_narrative = local_summary
                local_core = "本地事件边界已保留；具体含义以证据原文为准。"
            title_context = " ".join(
                str(value or "")
                for value in (local_item.get("title"), local_summary, local_narrative, local_core)
            ) + " " + " ".join(str(entry.get("content") or "") for entry in event_evidence)
            recovery = {
                "title": normalize_editorial_title(
                    local_item.get("title"),
                    title_context,
                ),
                "category": "事件",
                "value_type": "event",
                "importance": max(35, min(90, int(local_item.get("importance") or 55))),
                "confidence": max(35, min(65, int(local_item.get("confidence") or 55))),
                "summary": local_summary,
                "narrative": local_narrative,
                "core_conclusion": local_core,
                "keywords": list(local_item.get("tags") or [])[:4],
                "what_changed": str(local_item.get("what_changed") or ""),
                "why_it_matters": str(local_item.get("why_it_matters") or ""),
                "reason": reason,
                "uncertainty": str(local_item.get("uncertainty") or "本地规则未完成联网核验。"),
                "claim_type": str(local_item.get("claim_type") or "reported_claim"),
                "claim_basis": "本地事件边界；尚未联网核验",
                "next_step": "回到原消息查看该对象的完整上下文",
                "_recovered": True,
                "evidence_refs": [str(entry["evidence_ref"]) for entry in event_evidence],
                "evidence": event_evidence,
            }
            self._bound_ai_finding(recovery, event_evidence)
            self._finding_attribution(recovery, event_evidence)
            return recovery, event_evidence

        if incoherent_ref_ids:
            rejected_source_ids = {
                str(candidate_by_ref[ref].get("_source_message_id") or "")
                for ref in incoherent_ref_ids
                if ref in candidate_by_ref
            }
            for local_item in list(baseline.get("event_briefs") or [])[:40]:
                event_source_ids = {
                    str(value) for value in local_item.get("message_ids") or [] if value
                }
                if not event_source_ids.intersection(rejected_source_ids):
                    continue
                if event_source_ids.intersection(represented_source_ids):
                    continue
                recovered = recover_local_event(
                    local_item,
                    "模型候选跨越了不同对象，已按本地事件边界拆分；这里仅保留可回看的聊天证据。",
                )
                if recovered is None:
                    continue
                recovery, event_evidence = recovered
                findings.append(recovery)
                represented_source_ids.update(
                    str(entry.get("message_id") or "")
                    for entry in event_evidence
                )
                coherence_recovered = True
                if len(findings) >= 12:
                    break

        # An AI response can also omit a high-priority local event without
        # making a formally incoherent finding.  Keep the reduction reversible
        # by promoting a small number of uncovered, object-specific events.
        priority_domains = {"wechat", "gpt_service", "codex_service", "developer_account"}
        for local_item in list(baseline.get("event_briefs") or [])[:80]:
            domain_tags = set(str(value) for value in local_item.get("domain_tags") or [])
            if not domain_tags.intersection(priority_domains):
                continue
            try:
                local_importance = int(local_item.get("importance") or 0)
            except (TypeError, ValueError):
                local_importance = 0
            # Explicit object mentions are intentionally allowed through at a
            # lower threshold.  A short but high-signal line such as
            # "CodeX重置了吗" can score below a multi-message event while
            # still being exactly the item the user asked not to lose.
            minimum_priority = 45 if domain_tags.intersection(priority_domains) else 65
            if local_importance < minimum_priority:
                continue
            event_source_ids = {
                str(value) for value in local_item.get("message_ids") or [] if value
            }
            if not event_source_ids or event_source_ids.intersection(represented_source_ids):
                continue
            recovered = recover_local_event(
                local_item,
                "模型未覆盖这条高优先级本地事件，已保留独立证据稿；它仍只代表聊天内容。",
            )
            if recovered is None:
                continue
            recovery, event_evidence = recovered
            findings.append(recovery)
            represented_source_ids.update(
                str(entry.get("message_id") or "")
                for entry in event_evidence
            )
            coverage_recovered = True
            if len(findings) >= 20:
                break

        # A model may validly return no strict finding for a busy discussion:
        # it does not mean the window is empty.  Keep the read-only result
        # useful by promoting already-evidenced local discoveries into a
        # clearly labelled fallback lane.  This never invents a conclusion or
        # creates a reply task.
        local_fallback = coherence_recovered or coverage_recovered or object_boundary_repaired
        if not findings:
            # AI fallback keeps the broader topic layer for compatibility and
            # exploration; the overview itself remains event-led.
            fallback_items = list(baseline.get("topic_briefs") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("primary_insights") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("discoveries") or [])
            if not fallback_items:
                # A strict local event is still useful evidence when the AI
                # elects not to form an independent finding. Keep the queues
                # separate in the UI, but do not let a valid event make the AI
                # result look empty.
                fallback_items = list(baseline.get("actions") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("events") or [])
            if not fallback_items:
                fallback_items = list(baseline.get("highlights") or [])
            if not fallback_items:
                fallback_items = [
                    {
                        "message_id": item.get("_source_message_id"),
                        "chat_name": item.get("chat_name"),
                        "sender_name": item.get("sender_name"),
                        "content": item.get("content"),
                        "kind": item.get("candidate_type"),
                        "score": item.get("rule_level") == "high" and 80 or 45,
                    }
                    for item in candidates
                    if str(item.get("candidate_state") or "")
                    in {"informative", "reviewable", "context_needed"}
                ]
            category_labels = {
                "theme": "主题",
                "event": "事件",
                "resource": "资源",
                "progress": "进展",
                "knowledge": "知识",
                "discussion": "讨论",
            }
            for local_item in fallback_items:
                evidence_values = local_item.get("evidence") or []
                if isinstance(evidence_values, (str, Mapping)):
                    evidence_values = [evidence_values]
                evidence_ids = [
                    str(value.get("message_id") or "")
                    if isinstance(value, Mapping)
                    else str(value or "")
                    for value in evidence_values
                ]
                source_id = str(
                    local_item.get("message_id")
                    or (evidence_ids[0] if evidence_ids else "")
                )
                source = source_by_message.get(source_id)
                if source is None:
                    source = next(
                        (
                            candidate
                            for candidate in candidates
                            if str(candidate.get("_source_message_id") or "") == source_id
                        ),
                        None,
                    )
                if source is None:
                    continue
                ref = str(source.get("evidence_ref") or "")
                evidence = {
                    "evidence_ref": ref,
                    "message_id": source.get("_source_message_id"),
                    "chat_name": source.get("chat_name"),
                    "sender_name": source.get("sender_name"),
                    "timestamp": source.get("timestamp"),
                    "content": source.get("content"),
                }
                kind = str(local_item.get("kind") or source.get("candidate_type") or "discussion")
                fallback_summary = str(
                    local_item.get("summary")
                    or local_item.get("detail_summary")
                    or "本地规则保留了一条可回看证据，原文已放在证据附录。"
                ).strip()
                finding = {
                        "title": normalize_editorial_title(
                            str(local_item.get("title") or ""),
                            " ".join(
                                str(local_item.get(key) or "")
                                for key in ("summary", "content", "why_it_matters", "reason", "kind")
                            ),
                        ),
                        "category": category_labels.get(kind, "讨论"),
                        "value_type": kind,
                        "importance": max(30, min(90, int(local_item.get("score") or 45))),
                        "confidence": 55,
                        "summary": fallback_summary,
                        "narrative": fallback_summary,
                        "core_conclusion": str(local_item.get("why_it_matters") or local_item.get("reason") or "仍需结合上下文判断其实际影响。"),
                        "keywords": [str(local_item.get("kind") or "讨论")],
                        "what_changed": str(local_item.get("what_changed") or fallback_summary).strip(),
                        "why_it_matters": str(local_item.get("why_it_matters") or local_item.get("reason") or "这条内容已通过本地规则保留，但仍需要结合邻近消息判断影响。"),
                        "reason": "模型没有返回独立 finding；此条来自本地已保留的可回看证据。",
                        "uncertainty": str(local_item.get("uncertainty") or "本地保底没有替代模型完成跨消息归纳。"),
                        "claim_type": str(local_item.get("claim_type") or source.get("claim_type") or "reported_claim"),
                        "claim_basis": "本地规则保留；尚未联网核验",
                        "next_step": "回到原消息查看上下文，判断是否值得沉淀或继续跟进",
                        "_recovered": True,
                        "evidence_refs": [ref],
                        "evidence": [evidence],
                    }
                self._bound_ai_finding(finding, [evidence])
                self._finding_attribution(finding, [evidence])
                findings.append(finding)
                local_fallback = True
                if len(findings) >= 8:
                    break

        local_situation = baseline.get("situation") or {}
        if packet_mode:
            # A packet describes one conversation slice, so none of its global
            # prose may stand in for the whole reporting window.  Findings are
            # merged below; window-level prose comes from the local baseline
            # and, where needed, the final evidence-bound cards.
            brief = str(baseline.get("narrative") or "").strip()
            themes = []
            limitations = _clean_limitations([
                limitation
                for raw_value in raw_results
                for limitation in (
                    raw_value.get("limitations")
                    if isinstance(raw_value.get("limitations"), list)
                    else []
                )
            ])
            situation = str(local_situation.get("headline") or "").strip()
            key_changes = [str(item) for item in list(local_situation.get("points") or [])[:6]]
            open_questions = []
        else:
            brief = str(value.get("brief") or "").strip()
            themes = [str(item) for item in value.get("themes", [])[:12]] if isinstance(value.get("themes"), list) else []
            limitations = _clean_limitations(value.get("limitations"))
            situation = str(value.get("situation") or "").strip()
            key_changes = [str(item) for item in value.get("key_changes", [])[:8]] if isinstance(value.get("key_changes"), list) else []
            open_questions = [str(item) for item in value.get("open_questions", [])[:8]] if isinstance(value.get("open_questions"), list) else []
        if not situation:
            situation = str(
                local_situation.get("headline")
                or baseline.get("narrative")
                or "当前窗口存在可回看的讨论内容。"
            )
        if not key_changes:
            key_changes = [str(item) for item in list(local_situation.get("points") or [])[:6]]
        timeline = []
        model_timeline = [] if packet_mode else (
            value.get("timeline", []) if isinstance(value.get("timeline"), list) else []
        )
        for item in model_timeline:
            if not isinstance(item, dict):
                continue
            raw_refs = item.get("ref_ids", [])
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            valid_refs = [
                str(ref) for ref in raw_refs
                if str(ref) in candidate_by_ref
            ]
            if not valid_refs:
                continue
            timeline.append(
                {
                    "time": str(item.get("time") or ""),
                    "title": str(item.get("title") or "时间节点"),
                    "summary": str(item.get("summary") or ""),
                    "evidence_refs": valid_refs,
                }
            )
        if not timeline:
            for item in list(baseline.get("events") or [])[:6]:
                valid_refs = [candidate_by_source.get(str(ref)) for ref in item.get("evidence", [])]
                valid_refs = [ref for ref in valid_refs if ref]
                if not valid_refs:
                    continue
                timeline.append(
                    {
                        "time": str(item.get("start") or ""),
                        "title": "事件候选 · %s" % str(item.get("chat_name") or "会话"),
                        "summary": str(item.get("summary") or ""),
                        "evidence_refs": valid_refs[:8],
                    }
                )
        if not brief:
            brief = str(
                baseline.get("narrative")
                or "当前窗口存在可回看的讨论内容，但模型没有形成独立摘要。"
            )
        if not themes:
            themes = [str(item.get("topic")) for item in (baseline.get("topics") or [])[:8]]
        if not themes:
            themes = [
                str(topic.get("topic"))
                for episode in (baseline.get("discussion_episodes") or [])
                for topic in (episode.get("topics") or [])
                if topic.get("topic")
            ][:8]
        if coherence_recovered or object_boundary_repaired:
            limitations.append("模型候选的文字与证据对象不一致；已按本地对象边界拆分或重建，结果仍只代表聊天证据。")
        elif coverage_recovered:
            limitations.append("模型遗漏了部分高优先级对象；已用本地事件边界补回独立证据稿，仍只代表聊天内容。")
        elif local_fallback:
            limitations.append("模型未返回可核对的独立结论，已展示本地讨论洞察作为保底产出。")
        if packet_mode and len(raw_results) > 1:
            limitations.append("本期按 %d 个独立对话包完成抽取并合并；全局摘要由本地窗口基线与最终卡片生成。" % len(raw_results))
        elif len(raw_results) > 1:
            limitations.append("本期内容经 %d 次独立抽取并合并评判；同一故事的重复稿件已取写作最好的一版。" % len(raw_results))

        def polish_editorial_text(value: Any) -> str:
            text = str(value or "").strip()
            text = re.sub(r"对于[^，。]{0,36}(?:用户|人)来说[，,]?", "", text)
            text = text.replace("值得注意的是", "").replace("综上所述", "")
            text = text.replace("具有重要意义", "已经产生实际影响")
            text = text.replace("这是一种值得留意的风向", "更清楚的变化")
            text = text.replace("用户需要评估", "后续重点是判断")
            text = text.replace("建议用户关注", "后续重点是")
            text = text.replace("需要进一步关注", "仍需继续核对")
            text = re.sub(r"(?:用户)?需关注", "关键在于", text)
            return re.sub(r"\s+", " ", text).strip()

        flat_importance = len({int(item.get("importance") or 0) for item in findings}) <= 1
        for finding in findings:
            for key in ("summary", "narrative", "core_conclusion", "what_changed", "why_it_matters", "uncertainty", "next_step"):
                finding[key] = polish_editorial_text(finding.get(key))
            evidence_items = list(finding.get("evidence") or [])
            category = str(finding.get("category") or "").lower()
            evidence_chats = {str(item.get("chat_name") or "") for item in evidence_items if item.get("chat_name")}
            personal_evidence = sum(
                1
                for item in evidence_items
                if (source := candidate_by_ref.get(str(item.get("evidence_ref") or "")))
                and not bool(source.get("is_group"))
            )
            editorial_score = 48
            editorial_score += min(15, len(evidence_items) * 3)
            editorial_score += {"risk": 10, "event": 8, "progress": 7, "knowledge": 5, "theme": 4, "resource": 2, "question": 1}.get(category, 3)
            editorial_score += 6 if len(evidence_chats) >= 2 else 0
            editorial_score += min(10, personal_evidence * 5)
            editorial_score += 4 if len(str(finding.get("narrative") or "")) >= 150 else 0
            editorial_score += 4 if re.search(r"决定|确认|截止|风险|成本|安排|变化|影响", " ".join(str(finding.get(key) or "") for key in ("narrative", "core_conclusion", "what_changed"))) else 0
            # A finding built from one-to-one messages is by definition
            # addressed to the operator.  Some providers systematically mark
            # every private-chat item as low importance merely because it was
            # not widely discussed; the local editorial desk keeps a floor so
            # such evidence-bound findings stay visible.
            fully_personal = bool(evidence_items) and personal_evidence >= len(evidence_items)
            if fully_personal:
                editorial_score += 8
            current_score = int(finding.get("importance") or 0)
            if flat_importance or current_score < 50 or current_score in {55, 62} or (personal_evidence and current_score < 58):
                finding["importance"] = max(0, min(100, editorial_score))
            else:
                finding["importance"] = max(0, min(100, round(current_score * .65 + editorial_score * .35)))
            if personal_evidence and finding["importance"] < 55:
                finding["importance"] = 55

        # One consistent promotion bar for every lane: a card needs either at
        # least two pieces of evidence on the same thread or a single
        # self-contained announcement/decision/request.  Model-written prose
        # cannot make a thin source self-contained: recovered local evidence
        # and everything else become one-line leads instead of template-written
        # articles.
        cards: List[Dict[str, Any]] = []
        leads: List[Dict[str, Any]] = []
        for finding in findings:
            evidence_list = [entry for entry in list(finding.get("evidence") or []) if isinstance(entry, Mapping)]
            is_recovered = bool(finding.pop("_recovered", False))
            if is_recovered:
                quote_card = self._recovered_quote_card(finding, candidate_by_ref)
                if quote_card is not None:
                    cards.append(quote_card)
                    continue
            question_only = _evidence_is_question_only(evidence_list)
            if not is_recovered and not question_only and (
                len(evidence_list) >= 2
                or self._self_contained_single(evidence_list)
            ):
                cards.append(finding)
                continue
            leads.extend(self._finding_as_leads(finding, is_recovered))
        leads = self._merge_leads(leads)
        if packet_mode and leads:
            leads = self._drop_represented_leads(leads, cards)
            promoted_cards, leads = self._promote_related_leads(leads)
            cards.extend(promoted_cards)
        if packet_mode and cards:
            cards = self._split_claim_findings_by_subject(cards)
            cards, demoted_cards = self._select_editorial_cards(cards, limit=10)
            for demoted_card in demoted_cards:
                leads.extend(self._finding_as_leads(demoted_card, False))
            leads = self._merge_leads(leads)
        if packet_mode and leads:
            leads = self._drop_represented_leads(leads, cards)
            leads = self._drop_contextless_leads(leads)
            leads = self._rank_leads(leads, limit=15)
        findings = cards
        if leads:
            limitations.append(
                "%d 条内容只有单句证据或来自本地证据重建，已作为线索简讯保留，未展开成稿。"
                % len(leads)
            )
        findings.sort(key=lambda item: (int(item.get("importance") or 0), int(item.get("confidence") or 0), len(item.get("evidence") or [])), reverse=True)

        reducer_meta: Dict[str, Any] = {
            "enabled": bool(reducer_enabled),
            "attempted": False,
            "status": "disabled" if not reducer_enabled else "not_needed",
            "reasoning_effort": reducer_reasoning_effort if packet_mode else None,
            "usage": safe_usage(None),
        }
        reducer_brief_ids: List[str] = []
        reducer_claims_by_id: Dict[str, Dict[str, Any]] = {}
        if reducer_enabled and findings:
            reducer_meta["attempted"] = True
            reducer_generator = self._ai_generator_with_effort(
                generator,
                reducer_reasoning_effort,
                12288,
            )
            try:
                reduction = reduce_verified_findings(reducer_generator, window, findings)
                reducer_meta["usage"] = safe_usage(reduction.get("_usage"))
                if reduction.get("_parse_failed"):
                    raise AnalysisGenerationError(
                        str(reduction.get("_parse_error") or "最终主编输出无法解析")
                    )
                by_claim_id = {
                    "F%03d" % (index + 1): finding
                    for index, finding in enumerate(findings)
                }
                reducer_claims_by_id = by_claim_id
                ordered_ids = [
                    claim_id
                    for claim_id in reduction.get("ordered_claim_ids") or []
                    if claim_id in by_claim_id
                ]
                ordered_ids.extend(
                    claim_id for claim_id in by_claim_id if claim_id not in ordered_ids
                )
                findings = [by_claim_id[claim_id] for claim_id in ordered_ids]

                accepted_headlines = 0
                proposed_headlines = reduction.get("headlines") or {}
                if isinstance(proposed_headlines, Mapping):
                    for claim_id, proposed_title in proposed_headlines.items():
                        finding = by_claim_id.get(str(claim_id))
                        title = str(proposed_title or "").strip()
                        if finding is None or not is_editorial_title(title):
                            continue
                        evidence = [
                            item for item in finding.get("evidence") or []
                            if isinstance(item, Mapping)
                        ]
                        title_probe = {"title": title}
                        original_text = " ".join(
                            str(finding.get(key) or "")
                            for key in (
                                "title", "summary", "core_conclusion",
                                "what_changed", "why_it_matters",
                            )
                        )
                        title_terms = _context_terms(title)
                        source_terms = _context_terms(original_text)
                        # A rewritten title needs a lexical anchor in its own
                        # validated card as well as the ordinary hard guards.
                        if title_terms and source_terms and not title_terms.intersection(source_terms):
                            continue
                        if _finding_evidence_boundary_violation(title_probe, evidence):
                            continue
                        if _finding_unsupported_narrative_claims(title_probe, evidence):
                            continue
                        finding["title"] = title
                        accepted_headlines += 1
                reducer_brief_ids = [
                    claim_id
                    for claim_id in reduction.get("brief_claim_ids") or []
                    if claim_id in by_claim_id
                ][:3]
                reducer_meta.update({
                    "status": "succeeded",
                    "card_count": len(findings),
                    "accepted_headline_count": accepted_headlines,
                })
            except Exception as exc:
                # Editorial reduction is optional: validated cards and local

                reducer_meta.update({"status": "fallback", "message": str(exc)})
                limitations.append("最终主编调用失败；已保留本地校验后的事实卡与基线摘要。")

        if packet_mode:
            # Packet output has already passed claim-level evidence checks, so
            # the visible changes must come from those verified cards rather
            # than the broader rule baseline.
            key_changes = [
                str(item.get("what_changed") or item.get("summary") or item.get("title") or "")
                for item in findings[:6]
                if item.get("what_changed") or item.get("summary") or item.get("title")
            ]
            open_questions = [
                str(item.get("summary") or item.get("title") or "")
                for item in findings
                if str(item.get("claim_type") or "").lower() == "question"
                and (item.get("summary") or item.get("title"))
            ][:8]
            limitations = _clean_limitations(limitations)

        if packet_mode and reducer_brief_ids:
            selected_titles = [
                str(reducer_claims_by_id[claim_id].get("title") or "")
                for claim_id in reducer_brief_ids
                if claim_id in reducer_claims_by_id and reducer_claims_by_id[claim_id].get("title")
            ]
            if selected_titles:
                brief = "本期重点：" + "；".join(selected_titles) + "。"

        # When every draw left the brief empty, compose it from the actual
        # card headlines instead of repeating generic local event titles.
        if findings and (not brief if packet_mode else not str(value.get("brief") or "").strip()):
            card_titles = list(dict.fromkeys(
                str(item.get("title") or "") for item in findings[:3] if item.get("title")
            ))
            if card_titles:
                brief = "本期重点：" + "；".join(card_titles) + "。"

        identity = dict(ai_context.get("user_identity") or self._user_identity())
        baseline_summary = baseline.get("summary") or {}
        source_label = "ai_assisted_with_local_fallback" if local_fallback else "ai_assisted"
        successful_packet_ids = {
            str(item.get("_packet_id") or "")
            for item in raw_results
            if str(item.get("_packet_id") or "")
        }
        failed_packet_ids = {
            str(item.get("packet_id") or "")
            for item in packet_errors
            if str(item.get("packet_id") or "") and str(item.get("packet_id") or "") not in successful_packet_ids
        }
        packet_draw_success_count = len(raw_results) if packet_mode else 0
        packet_draw_failure_count = len(packet_errors) if packet_mode else 0
        packet_usage = summed_usage([stats.get("usage") or {} for stats in draw_stats])
        reducer_usage = safe_usage(reducer_meta.get("usage"))
        total_usage = summed_usage([packet_usage, reducer_usage])
        usage_summary = {
            "packet": packet_usage,
            "reducer": reducer_usage,
            "total": total_usage,
        }
        masthead = {
            "product": "微语",
            "document": "微信情报简报",
            "edition": "daily" if start_day == end_day else "period",
            "window": dict(window),
            "subject": identity.get("display_name"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {
                "messages": int(baseline_summary.get("messages") or 0),
                "chats": int(baseline_summary.get("chats") or 0),
                "candidates": len(candidates),
                "findings": len(findings),
                "leads": len(leads),
                "draws": len(raw_results),
                "draw_stats": draw_stats,
            },
            "source": source_label,
            "model": generator.model,
            "packet_mode": packet_mode,
            "packet_count": len(packets) if packet_mode else 0,
            "packet_success_count": len(successful_packet_ids) if packet_mode else 0,
            "packet_failure_count": len(failed_packet_ids) if packet_mode else 0,
            "packet_draw_success_count": packet_draw_success_count,
            "packet_draw_failure_count": packet_draw_failure_count,
            "packet_config": dict(packet_config),
            "packet_reasoning_effort": packet_reasoning_effort if packet_mode else None,
            "editorial_reducer": dict(reducer_meta),
            "usage": usage_summary,
        }

        validation_reason_counts: Dict[str, int] = {}
        for stats in draw_stats:
            for reason_code, count in stats["reason_counts"].items():
                validation_reason_counts[reason_code] = (
                    validation_reason_counts.get(reason_code, 0) + int(count)
                )
        validation_diagnostics = {
            "rejected_count": len(rejected_findings),
            "reason_counts": validation_reason_counts,
            "rejected_findings": rejected_findings,
            "rejected_claim_count": len(rejected_claims),
            "rejected_claims": rejected_claims,
        }

        payload = {
                "ok": True,
                "source": "ai_assisted_with_local_fallback" if local_fallback else "ai_assisted",
                "provider_status": "succeeded",
                "llm_accepted": not local_fallback,
                "fallback_reason": "local_fallback" if local_fallback else None,
                "provider": "openai",
                "model": generator.model,
                "packet_mode": packet_mode,
                "packet_count": len(packets) if packet_mode else 0,
                "packet_success_count": len(successful_packet_ids) if packet_mode else 0,
                "packet_failure_count": len(failed_packet_ids) if packet_mode else 0,
                "packet_draw_success_count": packet_draw_success_count,
                "packet_draw_failure_count": packet_draw_failure_count,
                "packet_config": dict(packet_config),
                "packet_errors": list(packet_errors) if packet_mode else [],
                "packet_reasoning_effort": packet_reasoning_effort if packet_mode else None,
                "editorial_reducer": dict(reducer_meta),
                "usage": usage_summary,
                "window": window,
                "coherence_repaired": coherence_recovered,
                "object_boundary_repaired": object_boundary_repaired,
                "coverage_recovered": coverage_recovered,
                "draw_stats": draw_stats,
                "validation_diagnostics": validation_diagnostics,
                "candidate_count": len(candidates),
                "context_count": sum(
                    len(ai_context.get(key) or [])
                    for key in ("topic_briefs", "discussion_windows", "event_candidates")
                ),
                "rule_baseline": {
                    "narrative": baseline.get("narrative"),
                    "summary": baseline.get("summary"),
                },
                "analysis": {
                    "brief": brief,
                    "situation": situation,
                    "key_changes": key_changes,
                    "themes": themes,
                    "open_questions": open_questions,
                    "timeline": timeline,
                    "limitations": limitations,
                    "findings": findings,
                    "leads": leads,
                    "user_identity": identity,
                    "masthead": masthead,
                    "discoveries": baseline.get("discoveries") or [],
                    "discussion_episodes": baseline.get("discussion_episodes") or [],
                    "topic_briefs": baseline.get("topic_briefs") or [],
                    "primary_insights": baseline.get("primary_insights") or [],
                    "activity": baseline.get("activity") or {},
                },
                "will_send": False,
                "creates_reply_tasks": False,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        with self.server.ai_analysis_lock:
            self.server.latest_ai_analysis = payload
            self.server.ai_analysis_by_window[cache_key] = payload
        return self._json(payload)

    def _preview(self, body: Dict[str, Any]) -> None:
        content = str(body.get("content") or "").strip()
        if not content:
            return self._json(
                {"error": "empty_content", "message": "请输入要预览的消息"},
                400,
            )
        chat_name = str(body.get("chat_name") or "文件传输助手")
        chat_id = str(body.get("chat_id") or "filehelper")
        message = IncomingMessage(
            message_id="preview:%s" % uuid4().hex,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=str(body.get("sender_id") or "preview-user"),
            sender_name=str(body.get("sender_name") or "预览用户"),
            message_type=str(body.get("message_type") or "text"),
            content=content,
            timestamp=datetime.now(timezone.utc),
            is_self=False,
            raw_message={"preview": True},
            adapter_name=self.server.service.adapter.name,
            adapter_version=self.server.service.adapter.version,
        )
        decision = self.server.service.policy.decide(message)
        if not decision.should_reply:
            return self._json(
                {
                    "ok": False,
                    "reason": decision.reason,
                    "message": "当前安全策略不会回复这条消息",
                },
                200,
            )
        try:
            reply_text = decision.reply_text
            if reply_text is None:
                reply_text = self.server.service.policy.generate_reply(
                    message,
                    self.server.store.recent_messages(
                        chat_id=chat_id,
                        limit=self.server.service.policy.context_limit,
                    ),
                )
            return self._json(
                {
                    "ok": True,
                    "reason": decision.reason,
                    "reply_text": reply_text,
                    "will_send": False,
                    "scope": chat_name,
                }
            )
        except Exception as exc:
            return self._json(
                {"ok": False, "reason": "ai_generation_failed", "message": str(exc)},
                200,
            )

    def _replace_rules(self, body: Dict[str, Any]) -> None:
        values = body.get("rules")
        if not isinstance(values, list):
            return self._json(
                {"error": "invalid_rules", "message": "rules 必须是数组"},
                400,
            )
        try:
            rules = tuple(
                ReplyRule.from_dict(item, index)
                for index, item in enumerate(values)
                if isinstance(item, dict)
            )
        except (TypeError, ValueError, re.error) as exc:
            return self._json(
                {"error": "invalid_rules", "message": str(exc)},
                400,
            )
        self.server.service.policy.rules = rules
        return self._json({"ok": True, "items": [_rule_json(rule) for rule in rules]})

    def _manual_send(self, body: Dict[str, Any]) -> None:
        if not self.server.service.send_enabled:
            return self._json(
                {
                    "error": "sending_disabled",
                    "message": "当前处于只接收/分析模式，发送功能已由操作员锁定",
                },
                403,
            )
        if body.get("confirm") is not True:
            return self._json(
                {"error": "confirmation_required", "message": "真实发送必须显式 confirm=true"},
                400,
            )
        if self.server.service.dry_run:
            return self._json(
                {"error": "dry_run_enabled", "message": "当前为演练模式，不会发送微信消息"},
                409,
            )
        if self.server.service.is_paused:
            return self._json(
                {"error": "auto_reply_paused", "message": "自动回复已暂停"},
                409,
            )
        chat_id = str(body.get("chat_id") or "filehelper")
        chat_name = str(body.get("chat_name") or "文件传输助手")
        content = str(body.get("content") or "").strip()
        if self.server.service.filehelper_only and chat_id != "filehelper":
            return self._json(
                {"error": "filehelper_only_test_scope", "message": "第一阶段只允许文件传输助手"},
                403,
            )
        if not content:
            return self._json(
                {"error": "empty_content", "message": "发送内容不能为空"},
                400,
            )
        health = self.server.service.adapter.health_check()
        if not health.ok:
            return self._json(
                {"error": "adapter_unavailable", "message": health.message},
                503,
            )
        try:
            result = self.server.service.adapter.send_text(chat_id, chat_name, content)
        except Exception as exc:
            return self._json({"error": "send_failed", "message": str(exc)}, 502)
        if not result.accepted:
            return self._json(
                {
                    "ok": False,
                    "accepted": False,
                    "confirmed": result.confirmed,
                    "confirmation": result.confirmation,
                    "error": result.error,
                },
                502,
            )
        return self._json(
            {
                "ok": True,
                "accepted": True,
                "confirmed": result.confirmed,
                "confirmation": result.confirmation,
                "sent_message_id": result.sent_message_id,
                "warning": "这是人工控制接口发送，结果已返回但不会创建自动回复任务",
            }
        )

    def _serve_asset(self, name: str, content_type: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if WEB_ROOT.resolve() not in path.parents:
            return self._json({"error": "forbidden"}, 403)
        try:
            data = path.read_bytes()
        except OSError:
            return self._json({"error": "asset_not_found"}, 404)
        if name == "index.html" and b"WeChat Bridge" not in data:
            # Keep the legacy application identifier available to existing
            # local clients/tests without changing the visible frontend.
            data = data.replace(
                b"    <title>",
                b'    <meta name="application-name" content="WeChat Bridge" />\n    <title>',
                1,
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before asset response completed")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _report_document(body: Mapping[str, Any]) -> Tuple[str, str]:
        html = str(body.get("html") or "")
        if "<html" not in html.lower() or "<body" not in html.lower():
            raise ValueError("日报 HTML 不完整")
        if len(html.encode("utf-8")) > 6_000_000:
            raise ValueError("日报 HTML 超过大小限制")
        raw_name = str(body.get("filename") or "wechat-daily").strip()
        filename = re.sub(r"[^0-9A-Za-z._-]+", "-", raw_name).strip("-.")[:100] or "wechat-daily"
        return html, filename

    def _report_render(self, body: Mapping[str, Any]) -> None:
        try:
            html, filename = self._report_document(body)
            output_format = str(body.get("format") or "html").strip().lower()
            if output_format == "html":
                return self._binary(html.encode("utf-8"), "text/html; charset=utf-8", filename + ".html")
            if output_format == "pdf":
                return self._binary(_render_report_pdf(html), "application/pdf", filename + ".pdf")
            raise ValueError("仅支持 HTML 或 PDF")
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return self._json({"error": "report_render_failed", "message": str(exc)}, 400)

    def _report_email(self, body: Mapping[str, Any]) -> None:
        if body.get("confirm") is not True:
            return self._json({"error": "confirmation_required", "message": "邮件发送需要明确确认"}, 400)
        try:
            html, filename = self._report_document(body)
            recipient = str(body.get("recipient") or "").strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
                raise ValueError("收件人邮箱格式不正确")
            formats = [str(item).lower() for item in body.get("formats", []) if str(item).lower() in {"html", "pdf"}]
            if not formats:
                raise ValueError("至少选择一种附件格式")
            config = self.server.settings.snapshot(include_secrets=True).get("email", {})
            host = str(config.get("host") or "").strip()
            sender = str(config.get("sender") or config.get("username") or "").strip()
            if not host or not sender:
                raise ValueError("请先在设置中填写 SMTP 主机和发件人")
            message = EmailMessage()
            message["Subject"] = str(body.get("subject") or "微信情报日报 %s" % filename)[:180]
            message["From"] = sender
            message["To"] = recipient
            message.set_content("微信情报日报已生成，详见附件。")
            message.add_alternative(html, subtype="html")
            if "html" in formats:
                message.add_attachment(html.encode("utf-8"), maintype="text", subtype="html", filename=filename + ".html")
            if "pdf" in formats:
                message.add_attachment(_render_report_pdf(html), maintype="application", subtype="pdf", filename=filename + ".pdf")
            security = str(config.get("security") or "ssl")
            port = int(config.get("port") or (465 if security == "ssl" else 587))
            username = str(config.get("username") or "").strip()
            password = str(config.get("password") or "")
            context = ssl.create_default_context()
            client = smtplib.SMTP_SSL(host, port, timeout=30, context=context) if security == "ssl" else smtplib.SMTP(host, port, timeout=30)
            try:
                if security == "starttls":
                    client.starttls(context=context)
                if username:
                    client.login(username, password)
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    client.close()
            return self._json({"ok": True, "recipient": recipient, "formats": formats})
        except (ValueError, RuntimeError, OSError, smtplib.SMTPException, subprocess.SubprocessError) as exc:
            return self._json({"error": "report_email_failed", "message": str(exc)}, 400)

    @staticmethod
    def _int_query(
        query: Dict[str, Any],
        key: str,
        default: int,
        cap: int = 500,
    ) -> int:
        try:
            return max(1, min(int(cap), int((query.get(key) or [default])[0])))
        except (TypeError, ValueError):
            return default

    def _json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            default=_json_default,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before JSON response completed")

    def _binary(self, data: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before file response completed")


def serve_dashboard(
    service,
    host: str = "127.0.0.1",
    port: int = 8765,
    shadow_catalog_path: Optional[Union[str, Path]] = None,
) -> None:
    """Serve the dashboard until interrupted."""
    server = BridgeHttpServer((host, int(port)), service, shadow_catalog_path=shadow_catalog_path)
    logger.info("dashboard listening at http://%s:%s", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def start_dashboard_thread(
    service,
    host: str = "127.0.0.1",
    port: int = 8765,
    shadow_catalog_path: Optional[Union[str, Path]] = None,
):
    server = BridgeHttpServer((host, int(port)), service, shadow_catalog_path=shadow_catalog_path)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="wechat-bridge-dashboard",
        daemon=True,
    )
    thread.start()
    return server, thread
