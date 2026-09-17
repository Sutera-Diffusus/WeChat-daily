"""Conservative, opt-in web verification for public factual claims.

The chat archive remains local.  This module sends only a small public query
constructed from recognizable product/platform names and claim terms; it does
not upload a message, sender name, chat name, or internal identifier.  Search
results are supporting evidence, not an automatic truth oracle.  The caller
must explicitly ask for a check and can cache the returned sources per window.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from .analysis import _claim_profile, _clip, _detail_entity_terms, _display_content


class FactCheckSearchProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        ...


class _DuckDuckGoParser(HTMLParser):
    """Extract only result links, titles and snippets from DDG HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._current: Optional[Dict[str, str]] = None
        self._field: Optional[str] = None
        self._buffer: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._current = {"url": str(attributes.get("href") or ""), "title": "", "snippet": ""}
            self._field = "title"
            self._buffer = []
        elif tag in {"a", "div"} and "result__snippet" in classes and self._current is not None:
            self._field = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None or not self._field:
            return
        if tag not in {"a", "div"}:
            return
        self._current[self._field] = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if self._field == "snippet":
            if self._current.get("url") and self._current.get("title"):
                self.results.append(self._current)
            self._current = None
        self._field = None
        self._buffer = []


class _BingParser(HTMLParser):
    """Extract result cards from Bing's server-rendered HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._current: Optional[Dict[str, str]] = None
        self._field: Optional[str] = None
        self._field_tag: Optional[str] = None
        self._buffer: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self._current = {"url": "", "title": "", "snippet": ""}
            self._field = None
            self._field_tag = None
            self._buffer = []
            return
        if self._current is None:
            return
        if tag == "h2":
            self._field = "title"
            self._field_tag = "h2"
            self._buffer = []
        elif tag == "a" and self._field == "title" and not self._current.get("url"):
            self._current["url"] = str(attributes.get("href") or "")
        elif tag == "p":
            self._field = "snippet"
            self._field_tag = "p"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if self._field and tag == self._field_tag:
            self._current[self._field] = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            self._field = None
            self._field_tag = None
            self._buffer = []
        if tag == "li":
            if self._current.get("url") and self._current.get("title"):
                self.results.append(self._current)
            self._current = None
            self._field = None
            self._field_tag = None
            self._buffer = []


class HttpSearchProvider:
    """Small stdlib-only search adapter with an injectable opener for tests."""

    def __init__(
        self,
        endpoint: str = "https://html.duckduckgo.com/html/",
        timeout: float = 6.0,
        opener: Any = None,
        fallback_endpoint: str = "https://www.bing.com/search",
    ) -> None:
        self.endpoint = endpoint
        self.timeout = max(1.0, float(timeout))
        self.opener = opener or urlopen
        self.fallback_endpoint = fallback_endpoint

    @staticmethod
    def _clean_url(value: str) -> str:
        parsed = urlparse(str(value or ""))
        query = parse_qs(parsed.query)
        if query.get("uddg"):
            return unquote(str(query["uddg"][0]))
        if query.get("u"):
            encoded = str(query["u"][0])
            if encoded.startswith("a1"):
                try:
                    decoded = base64.urlsafe_b64decode(encoded[2:] + "=" * (-len(encoded[2:]) % 4))
                    decoded_url = decoded.decode("utf-8", errors="ignore")
                    if decoded_url.startswith(("http://", "https://")):
                        return decoded_url
                except (ValueError, UnicodeError):
                    pass
        return str(value or "").strip()

    def _fetch(self, endpoint: str, query: str) -> str:
        request = Request(
            endpoint.rstrip("&?") + "?q=" + quote_plus(query),
            headers={
                "User-Agent": "WeiDailyFactCheck/1.0 (+local read-only analysis)",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            },
        )
        with self.opener(request, timeout=self.timeout) as response:
            raw = response.read(1_500_000)
        return raw.decode("utf-8", errors="ignore")

    @staticmethod
    def _parse(endpoint: str, raw: str) -> List[Dict[str, str]]:
        parser: HTMLParser
        if "bing.com" in endpoint.casefold():
            parser = _BingParser()
        else:
            parser = _DuckDuckGoParser()
        parser.feed(raw)
        return list(getattr(parser, "results", []))

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not query:
            return []
        endpoints: List[str] = []
        for endpoint in (self.endpoint, self.fallback_endpoint):
            endpoint = str(endpoint or "").strip()
            if endpoint and endpoint not in endpoints:
                endpoints.append(endpoint)
        parsed: List[Dict[str, str]] = []
        for endpoint in endpoints:
            try:
                raw = self._fetch(endpoint, query)
                parsed = self._parse(endpoint, raw)
            except Exception:
                parsed = []
            if parsed:
                break
        output: List[Dict[str, Any]] = []
        seen: set = set()
        for result in parsed:
            url = self._clean_url(result.get("url") or "")
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            output.append(
                {
                    "url": url,
                    "title": _clip(result.get("title"), 180),
                    "snippet": _clip(result.get("snippet"), 360),
                    "domain": urlparse(url).netloc,
                }
            )
            if len(output) >= max(1, min(int(limit), 10)):
                break
        return output


_PUBLIC_QUERY_STOPWORDS = {
    "这个", "那个", "相关", "用户", "有人", "群里", "群聊", "私聊", "自己", "感觉", "觉得",
    "可能", "也许", "听说", "据说", "然后", "但是", "不过", "已经", "还是", "现在", "今天",
    "比较", "真的", "问题", "情况", "东西", "事情", "一个", "一些", "我们", "他们", "有人",
}
_PUBLIC_ACTION_TERMS = {
    "要求", "必须", "只能", "拒绝", "不支持", "注册", "登录", "登陆", "邮箱", "邮件", "重置",
    "密码", "收不到", "封号", "封禁", "检测", "风控", "价格", "成本", "收费", "性能", "规则",
}


def _public_query_terms(clause: str, entities: Sequence[str]) -> List[str]:
    tokens: List[str] = []
    for token in list(entities) + re.findall(
        r"[A-Za-z][A-Za-z0-9._+#-]{1,}|[\u4e00-\u9fff]{2,8}", clause
    ):
        normalized = str(token).strip()
        if not normalized or normalized.casefold() in {item.casefold() for item in _PUBLIC_QUERY_STOPWORDS}:
            continue
        if normalized in _PUBLIC_ACTION_TERMS or normalized in entities or re.search(r"[A-Za-z.]", normalized):
            if normalized not in tokens:
                tokens.append(normalized)
    return tokens[:12]


def extract_claim_candidates(
    values: Iterable[Any],
    topic: str = "",
    max_claims: int = 8,
) -> List[Dict[str, Any]]:
    """Extract only public, externally checkable statements.

    Questions, pure opinions, and private logistics are deliberately excluded.
    A hypothesis can be returned, but its type is preserved so a search result
    cannot turn it into a fact in the UI.
    """

    candidates: List[Dict[str, Any]] = []
    seen: set = set()
    for value in values:
        content = re.sub(r"\s+", " ", _display_content(value)).strip()
        if not content:
            continue
        clauses = re.split(r"[。！？!?；;\n]+", content)
        for clause in clauses:
            clause = clause.strip(" ，,、\t")
            if len(re.sub(r"\s+", "", clause)) < 12:
                continue
            # A bare link is a resource to open, not a factual proposition to
            # search again.  Keeping it out also prevents URL path fragments
            # from consuming the small claim budget for a topic.
            if re.fullmatch(r"(?:https?://|www\.)\S+", clause, re.IGNORECASE):
                continue
            profile = _claim_profile(clause)
            claim_type = str(profile.get("claim_type") or "")
            if claim_type in {"empty", "question", "opinion"}:
                continue
            entities = _detail_entity_terms(clause)
            if not entities:
                continue
            query_terms = _public_query_terms(clause, entities)
            if not query_terms:
                continue
            query = " ".join(query_terms)
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "claim": _clip(clause, 320),
                    "claim_type": claim_type,
                    "claim_label": profile.get("claim_label"),
                    "claim_boundary": profile.get("claim_boundary"),
                    "entities": list(entities[:10]),
                    "query": query,
                    "topic": str(topic or ""),
                }
            )
            if len(candidates) >= max(1, min(int(max_claims), 12)):
                return candidates
    return candidates


def _claim_tokens(value: Any) -> set:
    text = str(value or "").casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9._+-]{2,}|[\u4e00-\u9fff]{2,8}", text))
    # Chinese search snippets rarely preserve word boundaries.  Add the
    # public action terms explicitly so a generic GitHub home page does not
    # look like evidence for a much narrower email/login rule.
    tokens.update(
        term.casefold()
        for term in _PUBLIC_ACTION_TERMS
        if term.casefold() in text
    )
    return {token for token in tokens if token not in _PUBLIC_QUERY_STOPWORDS}


def verify_claim(
    claim: Mapping[str, Any],
    provider: FactCheckSearchProvider,
    limit: int = 5,
) -> Dict[str, Any]:
    """Search and grade support conservatively; never invent contradiction."""

    result = dict(claim)
    query = str(claim.get("query") or "").strip()
    try:
        sources = provider.search(query, limit=limit) if query else []
    except Exception as exc:  # network failures belong in the visible result
        result.update(
            {
                "status": "error",
                "status_label": "联网失败",
                "reason": "搜索服务未返回结果：%s" % _clip(exc, 160),
                "sources": [],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return result

    claim_tokens = _claim_tokens(claim.get("claim"))
    claim_text = str(claim.get("claim") or "")
    claim_entities = {
        str(value).casefold()
        for value in _detail_entity_terms(claim_text)
        if str(value).strip()
    }
    claim_actions = {
        term.casefold()
        for term in _PUBLIC_ACTION_TERMS
        if term.casefold() in claim_text.casefold()
    }
    scored: List[Dict[str, Any]] = []
    for source in sources[: max(1, min(int(limit), 10))]:
        if not isinstance(source, Mapping):
            continue
        source_copy = dict(source)
        source_tokens = _claim_tokens(
            " ".join(str(source_copy.get(key) or "") for key in ("title", "snippet"))
        )
        overlap = len(claim_tokens.intersection(source_tokens)) / max(1, len(claim_tokens))
        source_text = " ".join(
            str(source_copy.get(key) or "") for key in ("title", "snippet")
        )
        source_entities = {
            str(value).casefold()
            for value in _detail_entity_terms(source_text)
            if str(value).strip()
        }
        source_actions = {
            term.casefold()
            for term in _PUBLIC_ACTION_TERMS
            if term.casefold() in source_text.casefold()
        }
        entity_overlap = claim_entities.intersection(source_entities)
        action_overlap = claim_actions.intersection(source_actions)
        source_copy["match_score"] = round(min(1.0, overlap), 3)
        source_copy["direct_match"] = bool(entity_overlap and action_overlap)
        scored.append(source_copy)
    # A single shared Chinese phrase can make the normalized score look small;
    # keep such results as "related" evidence, but reserve "supported" for
    # the stricter direct-match branch below.
    supporting = [item for item in scored if float(item.get("match_score") or 0) >= 0.12]
    direct_supporting = [
        item for item in supporting
        if item.get("direct_match") is True and float(item.get("match_score") or 0) >= 0.25
    ]
    domains = {
        str(item.get("domain") or urlparse(str(item.get("url") or "")).netloc).casefold()
        for item in supporting
        if str(item.get("domain") or item.get("url") or "").strip()
    }
    direct_domains = {
        str(item.get("domain") or urlparse(str(item.get("url") or "")).netloc).casefold()
        for item in direct_supporting
        if str(item.get("domain") or item.get("url") or "").strip()
    }
    if len(direct_domains) >= 2 and len(direct_supporting) >= 2:
        status, label = "supported", "多来源部分支持"
        reason = "至少两个不同域名的公开结果同时命中主张对象和规则/动作词；仍需打开来源核对原文范围。"
    elif supporting:
        status, label = "mixed", "找到相关来源，支持程度有限"
        reason = "找到相关公开结果，但来源数量或文本匹配不足以确认完整主张。"
    else:
        status, label = "unverified", "未找到足够佐证"
        reason = "搜索结果没有提供足够直接的公开佐证；不把聊天陈述升级为事实。"
    result.update(
        {
            "status": status,
            "status_label": label,
            "reason": reason,
            "sources": scored[: max(1, min(int(limit), 10))],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return result


def check_claims(
    values: Iterable[Any],
    provider: FactCheckSearchProvider,
    topic: str = "",
    max_claims: int = 6,
) -> Dict[str, Any]:
    claims = extract_claim_candidates(values, topic=topic, max_claims=max_claims)
    checked = [verify_claim(claim, provider) for claim in claims]
    return {
        "state": "checked",
        "topic": str(topic or ""),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "claim_count": len(checked),
        "claims": checked,
        "read_only": True,
        "privacy": "只发送公开对象与核验词，不发送聊天原文、发言人或会话名",
    }
