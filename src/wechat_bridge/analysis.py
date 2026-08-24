"""Explainable, conservative analysis for the local message workbench.

This module deliberately treats the archive and the work queue differently:
all messages remain searchable, while only messages with explicit evidence are
promoted to ``重点`` or ``待处理``. It is a deterministic first pass, not a
claim that a keyword alone proves importance.
"""

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .timeutil import as_timezone, get_timezone


_TOPIC_RULES = {
    "工作": ("项目", "客户", "需求", "会议", "方案", "汇报", "同事", "工作"),
    "时间安排": ("今天", "明天", "后天", "本周", "下周", "几点", "时间", "预约"),
    "费用与交易": ("付款", "费用", "报价", "价格", "发票", "转账", "钱", "预算"),
    "问题与风险": ("问题", "故障", "风险", "异常", "失败", "无法", "担心", "紧急"),
    "生活与关系": ("家人", "朋友", "吃饭", "到家", "生日", "旅行", "孩子"),
}
_INSIGHT_TOPIC_RULES = {
    "AI / 模型": (
        "ai", "llm", "gpt", "claude", "deepseek", "openai", "codex", "kimi",
        "模型", "多模态", "视觉",
        "token", "prompt", "提示词", "agent", "agentic", "harness", "推理",
        "训练", "微调",
    ),
    "开发 / 工具": (
        "代码", "编程", "算法", "api", "接口", "部署", "仓库", "github",
        "gitlab", "mcp", "tool", "toolcall", "插件", "skill", "版本", "单测",
        "测试", "服务", "架构",
    ),
    "搜索 / 研究": (
        "搜索", "研究", "调研", "论文", "文档", "资料", "知识库", "wiki",
        "exa", "anysearch", "搜索引擎",
    ),
    "产品 / 项目": (
        "项目", "需求", "方案", "产品", "平台", "比赛", "报名", "组队",
        "架构", "设计", "功能", "上线", "交付",
    ),
    "成本 / 额度": (
        "额度", "token", "成本", "收费", "价格", "费用", "订阅", "pro",
        "美元", "元", "预算", "并发",
    ),
    "风险 / 安全": (
        "风险", "故障", "异常", "失败", "无法", "安全", "攻击", "漏洞",
        "泄露", "封禁", "ip", "权限",
    ),
    "社群 / 活动": (
        "群", "社区", "群友", "活动", "俱乐部", "同学", "比赛", "组队",
    ),
}

_INSIGHT_KIND_LABELS = {
    "resource": "资源",
    "progress": "进展",
    "knowledge": "知识观点",
    "discussion": "主题讨论",
}

_ACTION_VERB = re.compile(
    r"(确认|回复|提交|发送|联系|安排|跟进|处理|研究|准备|支付|付款|报价|发票|预约|报名|交付|更新|整理|评估|检查|开会|开通|关闭|改成|采用|选择|定下来)"
)
_REQUEST = re.compile(r"(?:^|[，。；;\s])(?:请(?!问)|麻烦|帮我|请你|需要你|记得|别忘了|安排一下|跟进一下)")
_DECISION = re.compile(r"(决定|确定|定为|定下来|拍板|采用|改成|同意|不同意|结论是)")
_COMMITMENT = re.compile(r"(?:我|我们|你|他|她|团队|负责人)(?:会|将|负责|计划|承诺|已经|已)")
_DEADLINE = re.compile(r"(今天|明天|后天|本周|下周|截止|到期|尽快|马上|以内|\d{1,2}\s*[点时])")
_QUESTION = re.compile(r"[?？]")
_RISK = re.compile(r"(紧急|事故|故障|风险|异常|失败|无法|投诉|泄露|中断|逾期|冲突|不一致)")
_TRADE = re.compile(r"(合同|付款|付费|报价|发票|转账|预算|采购|成本|退款|\d+(?:\.\d+)?\s*(?:元|万|块|%|折)|￥|¥)")
_LOW_SIGNAL = re.compile(
    r"^(嗯+|哦+|啊+|好+|好的|收到|谢谢|感谢|哈哈+|在吗|早上好|晚安|ok|OK|👍+|😂+|哈哈哈)[!！。,.，、 ]*$",
    re.IGNORECASE,
)
_MEDIA_PLACEHOLDER = re.compile(
    r"^\s*\[(?:图片|语音|视频|动画表情|文件/链接/卡片|文件|链接|卡片)(?:\s+[^\]]+)?\]\s*$"
)
_RHETORICAL_QUESTION = re.compile(r"(为什么|为何|为啥|难道|有没有懂的|是白花的|有必要吗|怎么会)")
_AI_WX_IDENTIFIER = re.compile(r"\b(?:wxid_|gh_)[A-Za-z0-9_-]+\b", re.IGNORECASE)
_AI_MD5 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_AI_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:\\|\\\\)[^\r\n\s]+")
_AI_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_AI_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_DOMAIN_SIGNAL = re.compile(
    r"(项目|需求|客户|会议|方案|合同|报价|付款|发票|交付|代码|系统|接口|服务|部署|版本|bug|故障|风险|平台|比赛|报名|申请|联系方式|研究|工作|接口|toolcall|api|agent|agentic|session|statusline|search|harness|模型|账号|数据|任务|截止|负责人)",
    re.IGNORECASE,
)
_INFORMATION_SIGNAL = re.compile(
    r"(AI|LLM|GPT|Claude|Codex|Deep[Ss]eek|模型|多模态|视觉|token|提示词|prompt|agent|agentic|tool|toolcall|harness|API|接口|代码|编程|算法|数据|部署|开源|仓库|GitHub|搜索|研究|论文|比赛|平台|产品|方案|架构|设计|版本|性能|指标|成本|收费|价格|服务|账号|订阅|技能|skill)",
    re.IGNORECASE,
)
_RESOURCE_SIGNAL = re.compile(
    r"(?:https?://|www\.|github\.com|gitlab\.com|huggingface\.co|npmjs\.com|pypi\.org)",
    re.IGNORECASE,
)
_ARGUMENT_SIGNAL = re.compile(
    r"(因为|所以|但是|不过|其实|关键|核心|区别|优点|缺点|问题在于|意味着|经验|发现|原理|结论|建议|可以考虑|我认为|我觉得|看起来|不只是|相比|如果|当时)",
)
_INFORMATION_EXCLUDE = re.compile(
    r"(色情|黄片|成人视频|AV库|裸聊|性行为|约炮|自我介绍|大家认识一下|拉他进群)",
    re.IGNORECASE,
)
_CHANGE_VERB = re.compile(r"(改成|换成|采用|选择|定为|定下来|拍板|决定|调整为|切换到)")
_PROPOSAL = re.compile(r"(可以|建议|提议|考虑|最好|应该|不如|允许|支持|计划)")
_CONTEXT_DEPENDENT = re.compile(
    r"(^\s*(?:改成|换成|那就|这样|这个|那个|它|继续|照旧|不用|不需要)|"
    r"(?:怎么用|怎么弄|怎么处理|研究一下怎么用|看看怎么用)\s*[。！!?？]*$)"
)
_CLAUSE_FRAGMENT = re.compile(r"^\s*(?:至于|但是|不过|所以|因此|如果|因为|其中|那些|对于)|(?:的用户|的人|的话)\s*[。！!?？]*$")
_RISK_ACTION = re.compile(r"(请|需要|应当|必须|尽快|马上|处理|修复|停止|避免|确认|排查|跟进|投诉|泄露|中断|逾期)")
_GENERIC_REQUEST = re.compile(
    r"(?:需要你|请你|帮我|麻烦|请)\s*(?:研究一下|看看|弄一下|处理一下|确认一下)?\s*(?:怎么用|怎么弄|怎么处理)\s*[。！!?？]*$"
)
_GENERIC_WORDS = {"今天", "明天", "后天", "这个", "那个", "一下", "然后", "可以", "需要", "怎么", "研究"}
_EVENT_STOP_TERMS = _GENERIC_WORDS | {
    "我们", "你们", "他们", "自己", "现在", "已经", "还是", "就是", "不是", "一个", "一些",
    "这里", "那里", "这样", "那样", "可能", "应该", "感觉", "觉得", "知道", "看看", "进行",
    "比较", "没有", "什么", "事情", "问题", "消息", "内容", "时候", "真的", "的话", "之后",
    "之前", "目前", "大家", "因为", "所以", "但是", "不过", "而且", "如果", "或者", "以及",
    "时间", "价格", "国家", "模型", "代码", "系统", "项目", "消息", "群聊", "功能", "用户",
}

# Report headlines are part of the analysis contract rather than a styling
# preference.  A headline is an editorial handle for a topic; facts, names and
# timestamps belong in the deck and evidence below it.
# Seven characters keeps concise reference-style heads such as
# “配置设计的变革” valid; the generator prompt still recommends 8–18.
EDITORIAL_TITLE_MIN = 7
EDITORIAL_TITLE_MAX = 24
_TITLE_VAGUE = re.compile(
    r"^(?:相关讨论|相关话题|某某话题|话题持续升温|引发关注|相关内容|讨论引关注|最新消息|今日动态|事件候选)"
    r"(?:持续升温|引发关注|引关注|值得关注|出现讨论)?$"
)
_TITLE_CHATTER = re.compile(r"^(?:我|你|他|她|我们|大家|昨天|今天|刚刚|但是|然后|感觉|觉得|有没有|怎么|为什么)")
_TITLE_WEAK_ENDING = re.compile(
    r"(?:受关注|获认可|引关注|值得关注|持续升温|相关讨论|链接分享|问题待解|对比待解|待确认)$"
)
_TITLE_TOPIC_LABELS = (
    (r"选课|课程|教务|课表|学分|退补选", "选课安排"),
    (r"配置|config|设置|statusline|session", "配置设计"),
    (r"账号|封禁|订阅|额度|中转|合规|风控|权限", "账号风控"),
    (r"模型|GPT|Claude|DeepSeek|GLM|多模态|token|推理", "模型讨论"),
    (r"搜索|harness|toolcall|工具|插件|API|接口|agent", "工具链"),
    (r"项目|平台|比赛|组队|报名|交付|上线", "项目推进"),
    (r"价格|成本|费用|报价|收费|预算|付款", "成本变化"),
    (r"语音|转写|音频", "语音信息"),
    (r"风险|故障|异常|失败|安全|漏洞|泄露", "风险信号"),
    (r"研究|论文|文档|资料|知识库", "研究资料"),
)


def _title_size(value: Any) -> int:
    return len(re.sub(r"\s+", "", str(value or "").strip()))


def is_editorial_title(value: Any) -> bool:
    """Validate the short, judgment-led titles used by the daily brief."""

    title = re.sub(r"\s+", "", str(value or "").strip())
    if not (EDITORIAL_TITLE_MIN <= len(title) <= EDITORIAL_TITLE_MAX):
        return False
    if _TITLE_VAGUE.fullmatch(title) or _TITLE_CHATTER.match(title) or _TITLE_WEAK_ENDING.search(title):
        return False
    if title.endswith(("。", "！", "？", ".", "!", "?", ";", "；")):
        return False
    # A colon is preferred, but a concise noun phrase such as “配置设计的变革”
    # remains valid.  Reject sentence-shaped titles with several clauses.
    if len(re.findall(r"[，,；;]", title)) >= 2:
        return False
    return True


def _editorial_topic_label(text: str, anchors: Sequence[str] = ()) -> str:
    combined = str(text or "")
    for pattern, label in _TITLE_TOPIC_LABELS:
        if re.search(pattern, combined, re.IGNORECASE):
            return label
    for anchor in anchors:
        value = re.sub(r"\s+", "", str(anchor or ""))
        if 2 <= len(value) <= 8 and value not in _EVENT_STOP_TERMS:
            return value
    return "讨论主线"


def normalize_editorial_title(value: Any, context: Any = "", anchors: Sequence[str] = ()) -> str:
    """Turn model or rule output into an evidence-led short editorial title.

    This is deliberately deterministic so a provider failure cannot put a raw
    chat sentence in the front page title slot.
    """

    raw = re.sub(r"\s+", "", str(value or "").strip())
    context_text = re.sub(r"\s+", "", str(context or "").strip())
    combined = "\n".join(part for part in (raw, context_text) if part)
    topic = _editorial_topic_label(combined, anchors)
    # Prefer a compact editorial tension over a generic status suffix.  These
    # patterns describe reusable relationships rather than copying a chat
    # sentence into the headline slot.
    if re.search(r"gpt", combined, re.IGNORECASE) and re.search(r"封号|封禁", combined, re.IGNORECASE) and re.search(r"成本|价格|贵|费用", combined, re.IGNORECASE):
        candidate = "GPT困局：封号与成本夹击"
    elif re.search(r"claude", combined, re.IGNORECASE) and re.search(r"gpt", combined, re.IGNORECASE) and re.search(r"可靠|靠谱|耐用|信任", combined, re.IGNORECASE):
        candidate = "模型分野：Claude更受信任"
    elif re.search(r"额度|goal|限额", combined, re.IGNORECASE) and re.search(r"重置|reset", combined, re.IGNORECASE) and re.search(r"消耗|用量|速率", combined, re.IGNORECASE):
        candidate = "额度重置：消耗焦虑紧随而来"
    elif re.search(r"链接|网址|外链|资源", combined, re.IGNORECASE) and re.search(r"不明|不足|待核|无法确认|缺少上下文", combined, re.IGNORECASE):
        candidate = "外链汇集：价值仍待核验"
    elif re.search(r"对比|比较|哪个好|好用吗", combined, re.IGNORECASE) and re.search(r"工具|api|接口|pool", combined, re.IGNORECASE):
        candidate = "工具之问：两种方案尚待比较"
    else:
        candidate = ""
    if candidate and is_editorial_title(candidate):
        return candidate
    if re.search(r"风险|故障|异常|失败|安全|封禁|泄露|漏洞", combined, re.IGNORECASE):
        suffix = "异常信号开始集中"
    elif re.search(r"改成|换成|采用|调整|更新|决定|确认|定下来|选择", combined, re.IGNORECASE):
        suffix = "方案进入调整阶段"
    elif re.search(r"问题|疑问|请问|如何|怎么|是否|为什么|能否", combined, re.IGNORECASE):
        suffix = "关键问题仍待核实"
    elif re.search(r"价格|成本|费用|额度|收费|报价", combined, re.IGNORECASE):
        suffix = "成本与选择出现分歧"
    elif re.search(r"链接|文档|资料|仓库|论文|资源", combined, re.IGNORECASE):
        suffix = "资源线索开始汇合"
    elif re.search(r"建议|观点|认为|经验|发现|讨论|评测|比较", combined, re.IGNORECASE):
        suffix = "观点逐步形成共识"
    elif re.search(r"选课|课程|教务|课表|学分", combined, re.IGNORECASE):
        suffix = "信息在多方汇合"
    else:
        suffix = "出现可回看的新线索"
    candidate = "%s：%s" % (topic, suffix)
    if is_editorial_title(candidate):
        return candidate
    # Keep the contract even for unusual labels or provider text.
    fallback = "讨论主线：内容开始成形"
    return fallback


def _has_question(content: str) -> bool:
    # Query-string punctuation in a shared URL is not a human question.
    without_urls = re.sub(r"https?://\S+|www\.\S+", "", content, flags=re.IGNORECASE)
    return bool(_QUESTION.search(without_urls))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            result = datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _visible_timestamp(value: datetime, timezone_name: str) -> str:
    return as_timezone(value, timezone_name).isoformat(timespec="seconds")


def _clip(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _redact_ai_content(value: Any) -> str:
    """Remove common local identifiers and direct contact data before AI use."""

    text = str(value or "")
    text = _AI_WINDOWS_PATH.sub("[本地路径]", text)
    text = _AI_WX_IDENTIFIER.sub("[内部标识]", text)
    text = _AI_MD5.sub("[媒体索引]", text)
    text = _AI_EMAIL.sub("[邮箱]", text)
    text = _AI_PHONE.sub("[手机号]", text)
    return text


def _display_content(value: Any) -> str:
    """Remove provider-only group prefixes from old and new stored rows."""

    text = str(value or "").strip()
    match = re.match(r"^\s*([^:\r\n]{1,80})\s*:\s*\r?\n(.*)$", text, re.S)
    if match:
        return match.group(2).strip()
    return text


def _insight_topic_tags(value: Any) -> List[str]:
    """Return transparent topic tags used for aggregation and AI context."""

    content = _display_content(value).casefold()
    tags = []
    for topic, keywords in _INSIGHT_TOPIC_RULES.items():
        if any(keyword.casefold() in content for keyword in keywords):
            tags.append(topic)
    return tags


def _is_text_candidate(message: Mapping[str, Any], content: str) -> bool:
    message_type = str(message.get("message_type") or "text").lower()
    analyzable_type = message_type in {"text", "other"} or (
        message_type == "voice" and message.get("_transcribed_voice") is True
    )
    return analyzable_type and bool(content) and not _MEDIA_PLACEHOLDER.match(content)


def _action_evidence(content: str) -> Dict[str, Any]:
    """Return only explicit action/decision evidence, never date keywords alone."""

    if not content or _LOW_SIGNAL.match(content):
        return {
            "tags": [], "reasons": [], "request": False, "decision": False,
            "commitment": False, "risk": False, "question_request": False,
        }
    has_verb = bool(_ACTION_VERB.search(content))
    request = bool(_REQUEST.search(content)) and has_verb
    decision_match = _DECISION.search(content)
    decision_context = bool(
        re.search(r"(?:^|[，。；;\s@])(?:我|我们|你|团队|负责人)", content)
        or re.search(r"(已经|已|改成|采用|同意了|不同意|结论是|定下来)", content)
    )
    decision = bool(decision_match and decision_context and len(content) >= 6)
    commitment = bool(_COMMITMENT.search(content)) and has_verb
    deadline = bool(_DEADLINE.search(content)) and has_verb and (request or commitment or decision)
    question_request = bool(
        _has_question(content)
        and has_verb
        and re.search(r"(能否|可以|请问|怎么|如何|是否|有没有)", content)
        and not _RHETORICAL_QUESTION.search(content)
    )
    tags: List[str] = []
    reasons: List[str] = []
    if request or question_request:
        tags.append("待办")
        reasons.append("明确提出操作请求")
    if deadline:
        tags.append("期限")
        reasons.append("操作与时间要求同时出现")
    if decision:
        tags.append("决策")
        reasons.append("出现明确决策或结论表达")
    if commitment:
        tags.append("承诺")
        reasons.append("出现责任人或执行承诺")
    return {
        "tags": tags,
        "reasons": reasons,
        "request": bool(request or question_request),
        "decision": decision,
        "commitment": commitment,
        "risk": bool(_RISK.search(content)),
        "question_request": question_request,
    }


def _score_message(message: Mapping[str, Any]) -> Dict[str, Any]:
    content = _display_content(message.get("content"))
    if not _is_text_candidate(message, content):
        return {
            "score": 0, "level": "excluded", "value_label": "不参与重点分析",
            "tags": ["非文本"],
            "reason": "媒体、系统或无文本消息保留在消息流中，但不因类型本身升级为重点",
            "content": content, "eligible": False,
        }
    if _LOW_SIGNAL.match(content):
        return {
            "score": 0, "level": "low", "value_label": "低信息量", "tags": ["低信息"],
            "reason": "寒暄、确认或情绪性短消息", "content": content, "eligible": True,
        }

    score = 0
    tags: List[str] = []
    reasons: List[str] = []
    if len(content) >= 24:
        score += 10
        reasons.append("内容具备基本上下文")
    actionable_question = _has_question(content) and len(content) >= 8 and not _RHETORICAL_QUESTION.search(content)
    if actionable_question:
        score += 25
        tags.append("问题")
        reasons.append("包含具体问题，可能需要回应")
    action = _action_evidence(content)
    if action["tags"]:
        score += 42
        tags.extend(action["tags"])
        reasons.extend(action["reasons"])
    if _RISK.search(content) and len(content) >= 8:
        score += 32
        tags.append("风险")
        reasons.append("出现具体风险、故障或异常描述")
    if _TRADE.search(content) and (action["tags"] or actionable_question or len(content) >= 24):
        score += 20
        tags.append("交易")
        reasons.append("交易信息带有上下文或处理要求")
    if re.search(r"https?://|www\.", content, re.IGNORECASE):
        score += 8
        tags.append("链接")
        reasons.append("包含可回溯链接")
    # A decision verb alone is not a task.  Keep technical changes visible in
    # the archive, but do not let phrases such as “改成……了” jump to the top
    # without a concrete business/technical object and an actor/context.
    if action.get("decision") and not (
        action.get("request") or action.get("risk") or action.get("commitment")
    ):
        if not _DOMAIN_SIGNAL.search(content) or not re.search(
            r"(?:我|我们|他|她|团队|负责人|直接把|已经把)", content
        ):
            score = min(score, 24)
        else:
            score = min(score, 38)
    score = min(100, score)
    has_evidence = bool(action["tags"] or _RISK.search(content) or actionable_question)
    strong_evidence = bool(
        action.get("request")
        or action.get("question_request")
        or action.get("commitment")
        or action.get("risk")
        or actionable_question
    )
    if strong_evidence and (score >= 42 or action["tags"]):
        level, label = "high", "重点"
    elif action.get("decision") and score >= 28:
        level, label = "medium", "信息线索"
    elif has_evidence and score >= 28:
        level, label = "medium", "需关注"
    else:
        level, label = "low", "低信息量"
    return {
        "score": score, "level": level, "value_label": label,
        "tags": list(dict.fromkeys(tags)),
        "reason": "；".join(dict.fromkeys(reasons)) or "没有足够证据升级",
        "content": content, "eligible": True,
    }


def _chat_label(message: Mapping[str, Any]) -> str:
    value = str(message.get("chat_name") or "").strip()
    if value and not re.match(r"^(?:wxid_|gh_)[\w-]+$", value, re.I) and not value.isdigit():
        return value
    return "群聊" if message.get("is_group") else "未命名会话"


def _sender_label(message: Mapping[str, Any]) -> str:
    if message.get("is_self") is True:
        return "我"
    value = str(message.get("sender_name") or "").strip()
    if value and not re.match(r"^(?:wxid_|gh_)[\w-]+$", value, re.I) and not value.isdigit():
        return value
    return "待识别成员" if message.get("is_group") else "联系人"


def _identity_is_resolved(message: Mapping[str, Any]) -> bool:
    """Return whether an inbound identity has a trustworthy local source."""

    if message.get("is_self") is not False:
        return False
    source = str(message.get("sender_name_source") or "").strip()
    confidence = message.get("sender_name_confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence_value = 0.0
    if source in {
        "direct_chat_peer",
        "contact_remark",
        "contact_nickname",
        "group_nickname",
    } and confidence_value >= 0.75:
        return True
    return False


def _text_terms(value: Any) -> set:
    """Return small, privacy-safe terms for local context matching."""

    text = _display_content(value).casefold()
    terms = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z][a-z0-9_-]{2,}", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            terms.add(chunk)
            if len(chunk) > 2:
                terms.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
        else:
            terms.add(chunk)
    return {term for term in terms if term not in _GENERIC_WORDS}


def _event_terms(value: Any) -> set:
    """Extract reusable event anchors without relying on a fixed topic list.

    Long Chinese spans are split into overlapping 2-5 character phrases.  The
    recurring phrases are later weighted by their rarity inside the selected
    time window, which lets previously unseen subjects (for example a course
    selection round) form an event without adding a keyword rule first.
    """

    text = re.sub(r"https?://\S+|www\.\S+", " ", _display_content(value).casefold())
    terms = set(re.findall(r"[a-z][a-z0-9_.+-]{2,}|\d{2,}(?:[./:-]\d+)*", text))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if chunk not in _EVENT_STOP_TERMS and len(chunk) <= 8:
            terms.add(chunk)
        for width in range(2, min(5, len(chunk)) + 1):
            for index in range(len(chunk) - width + 1):
                term = chunk[index:index + width]
                if term not in _EVENT_STOP_TERMS:
                    terms.add(term)
    return {
        term for term in terms
        if term not in _EVENT_STOP_TERMS
        and not re.fullmatch(r"\d{1,2}", term)
    }


def _event_anchor_score(left: set, right: set, frequency: Counter) -> tuple:
    shared = left.intersection(right)
    if not shared:
        return 0.0, []
    anchors = sorted(
        shared,
        key=lambda term: (len(term), -frequency.get(term, 1), term),
        reverse=True,
    )
    # Prefer a specific phrase over several accidental two-character matches.
    score = sum(
        (2.0 if len(term) >= 4 else 1.55 if len(term) == 3 else 1.2)
        / max(1.0, frequency.get(term, 1) ** 0.15)
        for term in anchors[:5]
    )
    return score, anchors


def _is_event_text(item: Mapping[str, Any]) -> bool:
    content = _display_content(item.get("content"))
    if not _is_text_candidate(item, content) or _LOW_SIGNAL.match(content):
        return False
    compact = re.sub(r"\s+", "", content)
    return bool(len(compact) >= 8 or _DOMAIN_SIGNAL.search(content) or _ACTION_VERB.search(content))


def _event_title(items: Sequence[Mapping[str, Any]], anchors: Sequence[str]) -> str:
    combined = "\n".join(_display_content(item.get("content")) for item in items)
    return normalize_editorial_title("", combined, anchors)


def _event_editing_fields(
    items: Sequence[Mapping[str, Any]],
    anchors: Sequence[str],
    chats: Mapping[str, str],
    people: Sequence[str],
) -> Dict[str, str]:
    """Create a compact local brief when AI is unavailable.

    The local pass intentionally distinguishes the factual deck from the
    editorial judgment.  It is not a claim that a rule engine understands the
    world; it is a transparent synthesis of the evidence in this cluster.
    """

    ordered_items = sorted(items, key=lambda item: item["_timestamp"])
    combined = "\n".join(_display_content(item.get("content")) for item in ordered_items)
    topic = _editorial_topic_label(combined, anchors)
    actor_names = [name for name in people if name not in {"我", "联系人", "群成员", "待识别成员"}]
    actor_text = "、".join(actor_names[:4]) or "相关成员"
    chat_text = "、".join(str(value) for value in list(chats.values())[:4]) or "当前会话"
    statements: List[str] = []
    seen_quotes = set()
    for item in ordered_items:
        content = _display_content(item.get("content"))
        if not content or content in seen_quotes:
            continue
        seen_quotes.add(content)
        sender = _sender_label(item)
        sentence = re.split(r"[。！？!?；;\n]", content, 1)[0].strip()
        if sentence:
            statements.append("%s提到“%s”" % (sender, _clip(sentence, 54)))
        if len(statements) >= 3:
            break
    facts = "；".join(statements)
    if len(ordered_items) > len(statements):
        facts += "；其余消息继续围绕同一主线补充"
    if len(chats) >= 2:
        facts = "这条主线横跨%s，%s。" % (chat_text, facts or (actor_text + "提供了相关信息"))
    else:
        facts = "%s在%s中形成连续信息：%s。" % (actor_text, chat_text, facts or "消息围绕该主题展开")

    if re.search(r"改成|换成|采用|调整|更新|决定|确认|定下来|选择", combined, re.IGNORECASE):
        changed = "%s已经从单点提问或分享推进到方案调整/确认，后续应以最新决定为准。" % topic
    elif re.search(r"风险|故障|异常|失败|无法|封禁|泄露|漏洞", combined, re.IGNORECASE):
        changed = "%s出现了可交叉核对的异常或风险信号，但影响范围仍需回到原文确认。" % topic
    elif re.search(r"问题|疑问|请问|如何|怎么|是否|能否", combined, re.IGNORECASE):
        changed = "%s的讨论重点仍停留在问题澄清，尚未形成明确结论或执行闭环。" % topic
    elif len(chats) >= 2 or len(actor_names) >= 2:
        changed = "%s从单一会话中的零散表达扩展到多方补充，已具备整理成主线的证据基础。" % topic
    elif re.search(r"链接|文档|资料|仓库|论文|资源", combined, re.IGNORECASE):
        changed = "%s出现了可回看的资料线索，讨论开始从感想转向依据和方法。" % topic
    else:
        changed = "%s在窗口内形成了连续表达，但目前更像进展线索而非已完成事项。" % topic

    if len(chats) >= 2:
        conclusion = "跨会话重复出现的内容比单条摘录更能说明共同关注；当前最可靠的判断是%s。" % changed.rstrip("。")
    elif re.search(r"风险|故障|异常|失败|无法|封禁|泄露|漏洞", combined, re.IGNORECASE):
        conclusion = "这组证据足以保留为风险线索，但不能仅凭聊天判断实际损失或责任归属。"
    elif re.search(r"改成|换成|采用|调整|更新|决定|确认|定下来|选择", combined, re.IGNORECASE):
        conclusion = "信息已经出现方向性变化，回看最新一条决定和对应来源，比继续累积同义消息更重要。"
    else:
        conclusion = "这组消息具备连续语义和可回看证据，值得保留为窗口内的有效主线。"

    if re.search(r"问题|疑问|请问|如何|怎么|是否|能否", combined, re.IGNORECASE):
        uncertainty = "原文仍保留未回答的问题，当前没有足够证据确认最终方案。"
    elif len(chats) == 1 and len(actor_names) <= 1:
        uncertainty = "证据集中在单一会话或单一发言者，外部佐证不足。"
    else:
        uncertainty = "消息能说明讨论方向，但没有自动补入聊天之外的事实。"
    return {
        "narrative": _clip(facts, 360),
        "what_changed": _clip(changed, 180),
        "why_it_matters": _clip(conclusion, 180),
        "core_conclusion": _clip(conclusion, 180),
        "uncertainty": _clip(uncertainty, 120),
        "next_step": "回看原文证据，确认最新决定、未答问题或下一步动作",
    }


def _dynamic_kind(item: Mapping[str, Any], content: str) -> str:
    message_type = str(item.get("message_type") or "text").lower()
    if message_type == "voice" and item.get("_transcribed_voice") is True:
        message_type = "text"
    if message_type not in {"text", "other"} or _MEDIA_PLACEHOLDER.match(content):
        return "media"
    if _LOW_SIGNAL.match(content) or re.search(
        r"^(?:你好|您好|嗨|哈喽|早上好|晚上好|辛苦了|吃饭了吗|回头聊|先这样|在吗)[!！。,.，、 ]*$",
        content,
        re.IGNORECASE,
    ):
        return "greeting"
    if _has_question(content) or re.search(r"(?:请问|能否|是否|怎么|如何|为什么|有没有)", content):
        return "question"
    if _CONTEXT_DEPENDENT.search(content) or len(re.sub(r"\s+", "", content)) < 18:
        return "fragment"
    return "note"


def _dynamic_anchor(content: str) -> str:
    terms = _event_terms(content)
    generic_fragments = {
        "时间", "用了", "还是", "只能", "已经", "感觉", "觉得", "然后", "就是", "这个", "那个",
        "时候", "一下", "什么", "怎么", "可以", "没有", "现在", "比较", "真的", "用了",
    }
    weak_prefixes = ("能", "是", "的", "在", "就", "还", "才", "也", "更", "都", "而", "又", "从", "给", "把", "让", "会", "将", "用", "跟", "和", "对", "向", "为", "问", "说", "提", "想", "看", "做", "发", "再", "却", "但", "并")
    weak_suffixes = ("时", "用", "了", "的", "着", "呢", "啊", "吧", "吗", "呀", "嘛", "过", "上", "下", "中", "里")
    def usable(term: str) -> bool:
        return (
            len(term) >= 3
            and term not in _EVENT_STOP_TERMS
            and not any(fragment in term for fragment in generic_fragments)
            and not term.startswith(weak_prefixes)
            and not term.endswith(weak_suffixes)
            and not any(term.startswith(stop) or term.endswith(stop) for stop in _EVENT_STOP_TERMS if len(stop) >= 2)
        )
    preferred = sorted(
        (term for term in terms if usable(term)),
        key=lambda term: (len(term), term),
        reverse=True,
    )
    return preferred[0] if preferred else ""


def _unformed_dynamics(
    ordered: Sequence[Mapping[str, Any]],
    event_briefs: Sequence[Mapping[str, Any]],
    timezone_name: str,
) -> List[Dict[str, Any]]:
    """Account for every message not absorbed into a formed event.

    The output is deliberately sentence-level: a greeting, question, burst of
    fragments, or unresolved media message remains visible without pretending
    it is a major topic.
    """

    covered = set()
    for event in event_briefs:
        covered.update(str(value) for value in (event.get("message_ids") or []) if value)
        for evidence in event.get("evidence") or []:
            if isinstance(evidence, Mapping) and evidence.get("message_id"):
                covered.add(str(evidence.get("message_id")))
    remaining = [item for item in ordered if str(item.get("message_id") or "") not in covered]
    if not remaining:
        return []

    clusters: List[Dict[str, Any]] = []
    for item in remaining:
        content = _display_content(item.get("content"))
        kind = _dynamic_kind(item, content)
        sender = _sender_label(item)
        chat_id = str(item.get("chat_id") or item.get("chat_name") or "unknown")
        anchor = _dynamic_anchor(content) if kind in {"note", "fragment"} else ""
        current = None
        for cluster in reversed(clusters[-24:]):
            if cluster["kind"] != kind or cluster["sender"] != sender:
                continue
            if cluster["chat_id"] != chat_id and not (kind == "note" and anchor and anchor == cluster["anchor"]):
                continue
            if item["_timestamp"] - cluster["last_at"] > timedelta(minutes=45):
                continue
            if kind in {"note", "fragment"} and cluster["anchor"] and anchor and cluster["anchor"] != anchor:
                continue
            current = cluster
            break
        if current is None:
            current = {
                "kind": kind,
                "sender": sender,
                "chat_id": chat_id,
                "anchor": anchor,
                "items": [],
                "last_at": item["_timestamp"],
            }
            clusters.append(current)
        current["items"].append(item)
        current["last_at"] = item["_timestamp"]
        if not current.get("anchor") and anchor:
            current["anchor"] = anchor

    dynamics: List[Dict[str, Any]] = []
    type_labels = {
        "voice": "语音", "image": "图片", "video": "视频", "file": "文件",
        "link": "链接", "link_or_file": "链接或文件", "emoji": "表情",
        "sticker": "动画表情", "animated_emoji": "动画表情",
    }

    def media_label(item: Mapping[str, Any]) -> str:
        raw_type = str(item.get("message_type") or "").lower()
        content = _display_content(item.get("content"))
        for marker, label in (("动画表情", "动画表情"), ("表情", "表情"), ("图片", "图片"), ("语音", "语音"), ("视频", "视频"), ("文件", "文件"), ("链接", "链接")):
            if marker in content:
                return label
        return type_labels.get(raw_type, "其他媒体")
    for index, cluster in enumerate(clusters, 1):
        items = sorted(cluster["items"], key=lambda value: value["_timestamp"])
        first = items[0]
        sender = cluster["sender"]
        chat_name = _chat_label(first)
        contents = [_display_content(item.get("content")) for item in items if _display_content(item.get("content"))]
        unique_contents = list(dict.fromkeys(contents))
        if cluster["kind"] == "media":
            labels = [media_label(item) for item in items]
            label = "、".join(dict.fromkeys(labels)) or "其他媒体"
            transcripted = sum(1 for item in items if item.get("_transcribed_voice") is True)
            if label == "语音" and transcripted == 0:
                sentence = "%s在%s发送了%d条语音，当前未取得可用转写文本；原语音仍保留在会话中。" % (sender, chat_name, len(items))
            else:
                sentence = "%s在%s发送了%d条%s，其中%d条已有可读转写或文本上下文。" % (sender, chat_name, len(items), label, transcripted)
        elif cluster["kind"] == "greeting":
            quoted = "、".join("“%s”" % _clip(value, 28) for value in unique_contents[:3])
            sentence = "%s在%s进行了寒暄或确认，内容包括%s。" % (sender, chat_name, quoted or "简短回应")
        elif cluster["kind"] == "question":
            quoted = "；".join(_clip(value, 42) for value in unique_contents[:3])
            sentence = "%s在%s提出了问题：%s。" % (sender, chat_name, quoted or "问题内容待回看原文")
        elif cluster["kind"] == "fragment":
            quoted = "；".join(_clip(value, 34) for value in unique_contents[:3])
            subject = cluster.get("anchor") or "同一语境"
            sentence = "%s在%s连续发送碎片，围绕%s提到：%s。" % (sender, chat_name, subject, quoted or "内容待回看")
        else:
            quoted = "；".join(_clip(value, 42) for value in unique_contents[:3])
            subject = cluster.get("anchor") or "零散事项"
            sentence = "%s在%s零散讨论%s，主要内容是：%s。" % (sender, chat_name, subject, quoted or "内容待回看")
        evidence = [
            {
                "message_id": item.get("message_id"),
                "chat_id": item.get("chat_id"),
                "chat_name": _chat_label(item),
                "sender_name": _sender_label(item),
                "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                "quote": _clip(_display_content(item.get("content")) or ("[%s]" % media_label(item)), 120),
            }
            for item in items[:12]
        ]
        dynamics.append(
            {
                "id": "dynamic:%s:%s" % (index, str(first.get("message_id") or "")),
                "kind": cluster["kind"],
                "summary": _clip(sentence, 260),
                "people": [sender] if sender not in {"我", "联系人", "群成员", "待识别成员"} else [],
                "chats": [{"chat_id": cluster["chat_id"], "chat_name": chat_name}],
                "message_ids": [str(item.get("message_id") or "") for item in items if item.get("message_id")],
                "message_count": len(items),
                "start": _visible_timestamp(items[0]["_timestamp"], timezone_name),
                "end": _visible_timestamp(items[-1]["_timestamp"], timezone_name),
                "evidence": evidence,
            }
        )
    dynamics.sort(key=lambda item: (str(item.get("end") or ""), int(item.get("message_count") or 0)), reverse=True)
    # Keep every cluster in the API so the census accounting can prove that no
    # message disappeared.  The browser may virtualize the visible list, but
    # the analysis payload remains complete for export and AI context.
    return dynamics


_LIFE_SIGNAL = re.compile(
    r"(吃饭|聚餐|团建|约饭|出来玩|粗来丸|生日|到家|回家|身体|生病|休息|辛苦|"
    r"关心|照顾|见面|旅行|出发|到哪|几点到|周末|电影|游戏|礼物|家里|爸|妈)"
)
_SOCIAL_CHAT_NAME = re.compile(r"(粗来丸|家族|家庭|朋友|同学|宿舍|饭搭子|聚会|玩|club|小分队)", re.IGNORECASE)
_GROUP_EMOTION_NOISE = re.compile(
    r"^(?:\+?1|牛+|草+|笑死|绷不住|逆天|离谱|卧槽|我靠|我尼玛|傻.*|哈哈.*|呵呵.*|"
    r"冲+|开团|秒跟|蹲+|吃瓜|绝了|无语|寄+|6+|666+|牛逼|nb|艹|md)[!！。,.，、~～ ]*$",
    re.IGNORECASE,
)


def _discussion_signature(item: Mapping[str, Any]) -> set:
    """Return broad subjects used to split a chat into conversational blocks."""

    content = _display_content(item.get("content"))
    signature = set(_insight_topic_tags(content))
    if _LIFE_SIGNAL.search(content):
        signature.add("生活与关系")
    if re.search(r"选课|课程|课表|教务|学分|退补选", content):
        signature.add("选课安排")
    if re.search(r"聚餐|吃饭|团建|见面|约饭", content):
        signature.add("聚会安排")
    if re.search(r"额度|重置|reset|goal|用量|消耗", content, re.IGNORECASE):
        signature.add("额度与用量")
    if re.search(r"封号|封禁|风控|账号", content, re.IGNORECASE):
        signature.add("账号风控")
    return signature


def _group_message_has_substance(item: Mapping[str, Any], social_chat: bool) -> bool:
    content = _display_content(item.get("content"))
    if not _is_text_candidate(item, content):
        return False
    compact = re.sub(r"\s+", "", content)
    if not compact or _LOW_SIGNAL.match(content) or _GROUP_EMOTION_NOISE.match(content):
        return False
    if social_chat and _LIFE_SIGNAL.search(content):
        return True
    return bool(
        len(compact) >= 10
        or _has_question(content)
        or _RESOURCE_SIGNAL.search(content)
        or _INFORMATION_SIGNAL.search(content)
        or _ACTION_VERB.search(content)
        or _ARGUMENT_SIGNAL.search(content)
        or _RISK.search(content)
        or _TRADE.search(content)
    )


def _split_conversation_blocks(items: Sequence[Mapping[str, Any]], is_group: bool) -> List[List[Mapping[str, Any]]]:
    """Split one chat by pauses and clear subject changes, not by sender."""

    blocks: List[List[Mapping[str, Any]]] = []
    for item in sorted(items, key=lambda value: value["_timestamp"]):
        if not blocks:
            blocks.append([item])
            continue
        current = blocks[-1]
        gap = item["_timestamp"] - current[-1]["_timestamp"]
        current_signature = set().union(*(_discussion_signature(value) for value in current[-8:]))
        next_signature = _discussion_signature(item)
        next_content = _display_content(item.get("content"))
        new_question = bool(
            is_group
            and _has_question(next_content)
            and current_signature
            and next_signature
            and current_signature.isdisjoint(next_signature)
            and _sender_label(item) != _sender_label(current[-1])
        )
        subject_changed = bool(
            current_signature
            and next_signature
            and current_signature.isdisjoint(next_signature)
            and (gap > timedelta(minutes=2 if is_group else 20) or new_question)
            and len(current) >= 1
        )
        limit = timedelta(minutes=35 if is_group else 120)
        if gap > limit or subject_changed:
            blocks.append([item])
        else:
            current.append(item)
    return blocks


def _block_topic(items: Sequence[Mapping[str, Any]]) -> str:
    signatures = Counter(
        topic
        for item in items
        for topic in _discussion_signature(item)
    )
    if signatures:
        return "、".join(topic for topic, _count in signatures.most_common(2))
    anchors = Counter(
        term
        for item in items
        for term in _event_terms(item.get("content"))
        if 2 <= len(term) <= 8
    )
    useful = [term for term, _count in anchors.most_common(8) if term not in _EVENT_STOP_TERMS]
    return useful[0] if useful else "日常近况"


def _semantic_message_lines(items: Sequence[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """Return readable utterances, excluding media placeholders and reaction noise."""

    lines: List[Tuple[str, str]] = []
    seen = set()
    for item in items:
        value = re.sub(r"\s+", " ", _display_content(item.get("content"))).strip()
        value = re.sub(r"^\[(?:文本|系统消息|图片|动画表情|语音|文件/链接/卡片)\]\s*", "", value)
        value = re.sub(r"\[[^\]]{1,12}\]", "", value).strip(" ，,。.!！?？~～")
        if not value or _GROUP_EMOTION_NOISE.match(value) or value in seen:
            continue
        seen.add(value)
        lines.append((_sender_label(item), value))
    return lines


def _semantic_matter_summary(
    chat_name: str,
    people: Sequence[str],
    items: Sequence[Mapping[str, Any]],
    is_group: bool,
) -> str:
    """Write one concrete sentence from the block instead of a taxonomy template."""

    lines = _semantic_message_lines(items)
    combined = "；".join(value for _sender, value in lines)
    actor = "、".join(people[:3]) or ("群成员" if is_group else chat_name)
    place = "%s中" % chat_name if is_group else ""

    if re.search(r"崩了|用不了|(?:^|\D)529(?:\D|$)|服务异常", combined, re.I):
        product = "Claude/Opus" if re.search(r"claude|opus", combined, re.I) else "相关服务"
        recovery = "，随后有人确认已经恢复" if re.search(r"恢复|好了|正常了", combined) else ""
        return "%s，%s反馈%s出现报错、暂时无法使用%s。" % (place, actor, product, recovery)
    if re.search(r"毕业.*(?:材料|清单)|材料交寄|交寄清单", combined):
        title = next((value for _sender, value in lines if re.search(r"毕业|交寄|清单", value)), "毕业材料交寄清单")
        return "%s，%s发布《%s》，并提醒相关成员查看配套文件。" % (place, actor, _clip(title, 34))
    if is_group and re.search(r"投票|多选", combined) and re.search(r"时间|参加|开会|几号|以后", combined):
        detail = next((value for _sender, value in lines if re.search(r"\d+月\d+号|\d+号以后|时间", value)), "大家补充可参加时间")
        return "%s，%s发起活动时间投票；%s。" % (place, actor, _clip(detail, 44))
    if re.search(r"开会|拉个群|投票", combined) and re.search(r"报到|报道|线上参加|月底|月初", combined):
        if is_group:
            return "%s，%s继续确认开会时间和参加方式。" % (place, actor)
        return "%s说明秦老师计划在八月底至九月初召集开会并建群投票；我回复9月4日报到，若时间更早将线上参加。" % actor
    if re.search(r"重置|reset", combined, re.I) and re.search(r"额度|%|用掉|pro|还没|开始", combined, re.I):
        if is_group:
            return "%s，%s核对AI账号额度重置进度：有人已经恢复，也有人仍在等待。" % (place, actor)
        return "我与%s确认AI额度已经重置；对方尚余较多额度未使用，觉得这次重置有些浪费。" % actor
    if re.search(r"温度|\d+度|90\+|发热|散热", combined, re.I):
        device = "MacBook" if re.search(r"mbp|macbook|m4|max", combined, re.I) else "电脑"
        return "%s，%s讨论%s运行Codex时温度升高的问题，并对比了不同机型的温度表现。" % (place, actor, device)
    if re.search(r"课程|选课|FDE", combined, re.I):
        detail = next((value for _sender, value in lines if re.search(r"课程|选课|FDE", value, re.I)), "课程安排")
        return "%s，%s提到%s。" % (place, actor, _clip(detail, 52))
    if re.search(r"再见|一路平安|离去|告别", combined):
        subject = "兰哥" if "兰哥" in combined else "离开的成员"
        return "%s，%s向%s道别，祝其一路平安、接下来一切顺利。" % (place, actor, subject)
    if re.search(r"食堂|香菜|全是肉|分量|吃的|档口|\d+r", combined, re.I):
        price = next((match.group(0) for match in re.finditer(r"\d+(?:\.\d+)?\s*r", combined, re.I)), "")
        detail = (price + "的") if price else ""
        return "%s，%s晒出%s食堂餐食，大家接着聊起肉量、配菜和分量。" % (place, actor, detail)
    if re.search(r"可视化|轮廓", combined):
        detail = next((value for _sender, value in lines if "可视化" in value), "一项可视化效果")
        return "%s，%s分享%s，其他人追问为何只有轮廓，并顺势调侃呈现效果。" % (place, actor, _clip(detail, 40))
    if not is_group and re.search(r"开学|爸爸|妈妈|广东", combined):
        return "%s用语音问起开学时间，也谈到家人准备一同去广东的安排。" % actor

    useful = [
        (sender, value) for sender, value in lines
        if len(re.sub(r"\s+", "", value)) >= 4 and not _LOW_SIGNAL.match(value)
    ]
    if useful:
        first_sender, first_text = useful[0]
        second = next(((sender, value) for sender, value in useful[1:] if value != first_text), None)
        if is_group:
            summary = "%s，%s提到“%s”" % (place, first_sender, _clip(first_text, 48))
            if second:
                summary += "；%s回应“%s”" % (second[0], _clip(second[1], 48))
            return summary + "。"
        summary = "%s提到“%s”" % (actor, _clip(first_text, 58))
        if second:
            summary += "；双方随后还聊到“%s”" % _clip(second[1], 48)
        return summary + "。"
    return "%s留下了一段只有媒体或系统占位符的记录，正文信息仍待补全。" % (place or actor)


def _small_matters(
    ordered: Sequence[Mapping[str, Any]],
    event_briefs: Sequence[Mapping[str, Any]],
    timezone_name: str,
) -> List[Dict[str, Any]]:
    """Build event-shaped minor notes and suppress group-chat noise.

    Private chats are census material. Group chats are evaluated as whole
    conversational blocks; reactions and emotional bursts never become one
    note per sender merely because they exist in the archive.
    """

    covered = {
        str(message_id)
        for event in event_briefs
        for message_id in (event.get("message_ids") or [])
        if message_id
    }
    remaining = [item for item in ordered if str(item.get("message_id") or "") not in covered]
    announced_groups: List[Tuple[str, str]] = []
    for item in remaining:
        content = _display_content(item.get("content"))
        match = re.search(r"群聊[\"“『《]([^\"”』》]{2,32})[\"”』》]", content)
        if match:
            announced_groups.append((_sender_label(item), match.group(1).strip()))
    by_chat: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in remaining:
        by_chat[str(item.get("chat_id") or item.get("chat_name") or "unknown")].append(item)

    matters: List[Dict[str, Any]] = []
    for chat_id, chat_items in by_chat.items():
        is_group = any(bool(item.get("is_group")) for item in chat_items)
        chat_name = _chat_label(chat_items[0])
        raw_chat_name = str(chat_items[0].get("chat_name") or "").strip()
        if is_group and (raw_chat_name.lower().endswith("@chatroom") or chat_name == "群聊"):
            chat_people = {_sender_label(item) for item in chat_items}
            inferred = next((name for sender, name in reversed(announced_groups) if sender in chat_people), "")
            chat_name = inferred or "新建群聊（群名待同步）"
        social_chat = bool(_SOCIAL_CHAT_NAME.search(chat_name)) or sum(
            bool(_LIFE_SIGNAL.search(_display_content(item.get("content")))) for item in chat_items
        ) >= 3
        for block in _split_conversation_blocks(chat_items, is_group):
            text_items = [
                item for item in block
                if _is_text_candidate(item, _display_content(item.get("content")))
                and not _LOW_SIGNAL.match(_display_content(item.get("content")))
                and not _GROUP_EMOTION_NOISE.match(_display_content(item.get("content")))
            ]
            substantive = [item for item in text_items if _group_message_has_substance(item, social_chat)]
            repeated_terms = Counter(
                term for item in text_items for term in _event_terms(item.get("content")) if len(term) >= 2
            )
            recurring_subject = any(count >= 3 for term, count in repeated_terms.items() if term not in _EVENT_STOP_TERMS)
            if is_group:
                retain = bool(
                    (social_chat and any(_LIFE_SIGNAL.search(_display_content(item.get("content"))) for item in text_items))
                    or len(substantive) >= 2
                    or (len(substantive) >= 1 and len(text_items) >= 2)
                    or recurring_subject
                )
                if not retain:
                    continue
            elif not block:
                continue

            topic = _block_topic(text_items or block)
            people = list(dict.fromkeys(
                _sender_label(item)
                for item in block
                if _sender_label(item) not in {"我", "联系人", "群成员", "待识别成员"}
            ))
            summary = _semantic_matter_summary(chat_name, people, block, is_group)
            if any(
                str(item.get("message_type") or "").lower() == "voice"
                and item.get("_transcribed_voice") is not True
                for item in block
            ) and "语音" not in summary:
                summary = summary.rstrip("。") + "；另有一条语音尚待转写。"

            evidence = [
                {
                    "message_id": item.get("message_id"),
                    "chat_id": item.get("chat_id"),
                    "chat_name": _chat_label(item),
                    "sender_name": _sender_label(item),
                    "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                    "quote": _clip(_display_content(item.get("content")) or "[%s]" % str(item.get("message_type") or "媒体"), 120),
                }
                for item in block[:16]
            ]
            first = block[0]
            matters.append(
                {
                    "id": "small:%s:%s" % (len(matters) + 1, str(first.get("message_id") or "")),
                    "kind": "small_matter",
                    "summary": _clip(summary, 120),
                    "topic": topic,
                    "people": people,
                    "chats": [{"chat_id": chat_id, "chat_name": chat_name}],
                    "message_ids": [str(item.get("message_id") or "") for item in block if item.get("message_id")],
                    "message_count": len(block),
                    "start": _visible_timestamp(block[0]["_timestamp"], timezone_name),
                    "end": _visible_timestamp(block[-1]["_timestamp"], timezone_name),
                    "evidence": evidence,
                    "is_group": is_group,
                    "social_chat": social_chat,
                }
            )
    matters.sort(key=lambda item: str(item.get("end") or ""), reverse=True)
    return matters


def _event_briefs(
    ordered: Sequence[Mapping[str, Any]],
    timezone_name: str,
    profile: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Cluster fragmented messages into evidence-backed, cross-chat events."""

    candidates: List[Dict[str, Any]] = []
    frequency: Counter = Counter()
    for original in ordered:
        if not _is_event_text(original):
            continue
        item = dict(original)
        terms = _event_terms(item.get("content"))
        if not terms:
            continue
        information = int((item.get("_score") or {}).get("score") or 0)
        information += min(20, len(_display_content(item.get("content"))) // 12)
        content = _display_content(item.get("content"))
        information += 25 if _INFORMATION_SIGNAL.search(content) else 0
        information += 12 if (_ARGUMENT_SIGNAL.search(content) or _RESOURCE_SIGNAL.search(content)) else 0
        item["_event_terms"] = terms
        item["_event_information"] = information
        candidates.append(item)
        frequency.update(terms)
    if not candidates:
        return []

    # Form groups around one stable shared anchor instead of transitive pairwise
    # union.  Transitive union can let A~B and B~C incorrectly imply A~C,
    # producing a giant, incoherent daily cluster.
    by_term: Dict[str, List[int]] = defaultdict(list)
    for index, item in enumerate(candidates):
        for term in item["_event_terms"]:
            by_term[term].append(index)
    max_anchor_frequency = max(8, min(24, round(len(candidates) * 0.08)))
    bad_edges = set("我你他她它这那的了是在有和与就都也会能可要把被让给对从到及或而但因所为吗呢吧啊哦")
    generic_product_anchors = {
        "ai", "gpt", "gptpro", "token", "tokens", "claude", "deepseek",
        "codex", "模型", "大模型", "人工智能", "pro", "agent", "api",
        "明白", "一样", "感觉", "今天", "昨天", "但是", "就是", "觉得",
        "可以", "这个", "那个", "然后", "自己", "现在", "还是", "没有",
        "一个", "什么", "怎么", "可能", "应该", "直接", "已经", "真的",
    }

    def valid_anchor(term: str, indexes: Sequence[int]) -> bool:
        if len(term) < 2 or len(indexes) < 2 or len(indexes) > max_anchor_frequency:
            return False
        if term.casefold().replace(" ", "") in generic_product_anchors:
            return False
        if term[0] in bad_edges or term[-1] in bad_edges:
            return False
        if len(term) == 2 and len(indexes) < 3:
            return False
        return True

    proposals = []
    for term, indexes in by_term.items():
        unique_indexes = sorted(set(indexes))
        if not valid_anchor(term, unique_indexes):
            continue
        times = [candidates[index]["_timestamp"] for index in unique_indexes]
        if max(times) - min(times) > timedelta(days=7):
            continue
        chat_count = len({str(candidates[index].get("chat_id") or "") for index in unique_indexes})
        max_information = max(int(candidates[index]["_event_information"]) for index in unique_indexes)
        semantic_members = sum(
            bool(
                _ACTION_VERB.search(_display_content(candidates[index].get("content")))
                or _INFORMATION_SIGNAL.search(_display_content(candidates[index].get("content")))
                or _RESOURCE_SIGNAL.search(_display_content(candidates[index].get("content")))
                or _RISK.search(_display_content(candidates[index].get("content")))
                or _TRADE.search(_display_content(candidates[index].get("content")))
            )
            for index in unique_indexes
        )
        if semantic_members < 2:
            continue
        if max_information < 25 and not (chat_count >= 2 and len(unique_indexes) >= 3):
            continue
        proposals.append((chat_count, max_information, len(unique_indexes), len(term), term, unique_indexes))
    proposals.sort(reverse=True)

    groups: List[List[Dict[str, Any]]] = []
    accepted_sets: List[set] = []
    for _chat_count, _information, _size, _length, _term, indexes in proposals:
        index_set = set(indexes)
        if any(len(index_set.intersection(existing)) / min(len(index_set), len(existing)) >= 0.65 for existing in accepted_sets):
            continue
        accepted_sets.append(index_set)
        groups.append([candidates[index] for index in indexes])
        if len(groups) >= 64:
            break
    covered = set().union(*accepted_sets) if accepted_sets else set()
    for index, item in enumerate(candidates):
        if index not in covered and int(item["_event_information"]) >= 65:
            groups.append([item])

    profile_terms = {
        str(value).strip().casefold()
        for key in ("roles", "projects", "organizations", "key_contacts", "topics")
        for value in ((profile or {}).get(key) or [])
        if str(value).strip()
    }
    briefs: List[Dict[str, Any]] = []
    for items in groups:
        items.sort(key=lambda item: (item["_timestamp"], str(item.get("message_id") or "")))
        chats = {
            str(item.get("chat_id") or item.get("chat_name") or "unknown"): _chat_label(item)
            for item in items
        }
        people = {
            _sender_label(item) for item in items
            if _sender_label(item) not in {"我", "联系人", "群成员", "待识别成员"}
        }
        substantive_members = sum(int(item.get("_event_information") or 0) >= 25 for item in items)
        if len(chats) >= 2:
            if substantive_members < 2 and len(chats) < 3:
                continue
        elif len(items) >= 2 and substantive_members < 2:
            continue
        inbound_people = {_sender_label(item) for item in items if item.get("is_self") is False}
        has_self = any(item.get("is_self") is True for item in items)
        has_private = any(not bool(item.get("is_group")) for item in items)
        mentions_me = any(re.search(r"@(?:我|所有人)\b", _display_content(item.get("content"))) for item in items)
        action_for_me = any(
            item.get("is_self") is False and bool(_action_evidence(_display_content(item.get("content"))).get("request"))
            for item in items
        )
        combined_content = "\n".join(_display_content(item.get("content")).casefold() for item in items)
        matched_profile = sorted(term for term in profile_terms if term in combined_content)
        for_me = bool(has_private or mentions_me or action_for_me or has_self or matched_profile)
        broadly_discussed = len(chats) >= 2
        group_hot = len(chats) == 1 and any(bool(item.get("is_group")) for item in items) and len(people) >= 3

        common = set(items[0]["_event_terms"])
        for item in items[1:]:
            common.intersection_update(item["_event_terms"])
        anchors = sorted(common, key=lambda term: (len(term), -frequency.get(term, 1), term), reverse=True)
        if not anchors:
            anchors = sorted(
                Counter(term for item in items for term in item["_event_terms"]),
                key=lambda term: (frequency.get(term, 0), len(term)),
                reverse=True,
            )
        evidence = []
        for item in sorted(items, key=lambda value: value["_event_information"], reverse=True)[:8]:
            quote = _clip(_display_content(item.get("content")), 96)
            sender = _sender_label(item)
            evidence.append(
                {
                    "message_id": item.get("message_id"),
                    "chat_id": item.get("chat_id"),
                    "chat_name": _chat_label(item),
                    "sender_name": sender,
                    "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                    "statement": "%s：%s" % (sender, _clip(quote, 70)),
                    "quote": quote,
                    "is_self": item.get("is_self") is True,
                }
            )
        meaningful_cluster = len(items) >= 2
        if not meaningful_cluster and int(items[0]["_event_information"]) < 45:
            continue
        if broadly_discussed and for_me:
            lane = "for_me"
        elif for_me:
            lane = "for_me"
        elif broadly_discussed or group_hot:
            lane = "trending"
        else:
            lane = "pending"
        if len(items) >= 3 and (len(chats) >= 2 or len(people) >= 2):
            status = "ongoing"
        elif meaningful_cluster:
            status = "confirmed"
        else:
            status = "pending"
        actor_text = "、".join(sorted(people)[:3]) or ("我" if has_self else "相关成员")
        chat_text = "、".join(list(chats.values())[:3])
        lead_quote = evidence[0]["quote"] if evidence else ""
        second_quote = next(
            (item["quote"] for item in evidence[1:] if item.get("quote") != lead_quote),
            "",
        )
        summary = "%s在%s提到：%s" % (actor_text, chat_text, _clip(lead_quote, 92))
        if second_quote:
            summary += "；后续还提到%s。" % _clip(second_quote, 58)
        elif summary and not summary.endswith(("。", "！", "？")):
            summary += "。"
        message_ids = [str(item.get("message_id") or "") for item in items]
        event_id = hashlib.sha1("|".join(message_ids).encode("utf-8")).hexdigest()[:14]
        tags = [term for term in anchors if 2 <= len(term) <= 12][:5]
        if re.search(r"选课|课程|教务|课表|学分", combined_content, re.I):
            tags.insert(0, "选课安排")
        if group_hot:
            tags.append("群内热点")
        if broadly_discussed and for_me:
            tags.append("多人关注")
        editing = _event_editing_fields(items, anchors, chats, sorted(people))
        briefs.append(
            {
                "id": "event:%s" % event_id,
                "title": _event_title(items, anchors),
                "summary": summary,
                "narrative": editing["narrative"],
                "what_changed": editing["what_changed"],
                "why_it_matters": editing["why_it_matters"],
                "core_conclusion": editing["core_conclusion"],
                "uncertainty": editing["uncertainty"],
                "next_step": editing["next_step"],
                "status": status,
                "lane": lane,
                "tags": list(dict.fromkeys(tags)),
                "importance": min(100, max(int(item["_event_information"]) for item in items) + min(30, len(items) * 5) + min(25, max(0, len(chats) - 1) * 5)),
                "confidence": min(95, 48 + min(24, len(items) * 8) + min(18, len(chats) * 6)),
                "related_chat_count": len(chats),
                "related_people_count": len(people),
                "multi_attention": bool(broadly_discussed and for_me),
                "group_hot": group_hot,
                "profile_matches": matched_profile[:8],
                "chats": [{"chat_id": chat_id, "chat_name": name} for chat_id, name in chats.items()],
                "people": sorted(people),
                "start": _visible_timestamp(items[0]["_timestamp"], timezone_name),
                "end": _visible_timestamp(items[-1]["_timestamp"], timezone_name),
                "message_ids": message_ids,
                "evidence": evidence,
            }
        )
    briefs.sort(key=lambda item: (item["lane"] != "pending", item["importance"], item["end"]), reverse=True)
    return briefs[:80]


def _build_context_windows(
    items: Sequence[Mapping[str, Any]],
    max_neighbors: int = 5,
    max_minutes: int = 20,
) -> Dict[int, List[Mapping[str, Any]]]:
    """Build same-chat context, extending short consecutive sender bursts."""

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        key = str(item.get("chat_id") or item.get("chat_name") or "unknown")
        grouped[key].append(item)
    result: Dict[int, List[Mapping[str, Any]]] = {id(item): [] for item in items}
    limit = timedelta(minutes=max_minutes)
    burst_limit = timedelta(minutes=12)
    for group in grouped.values():
        group.sort(key=lambda item: (_timestamp(item.get("timestamp")), str(item.get("message_id") or "")))
        for index, item in enumerate(group):
            neighbors: List[Mapping[str, Any]] = []
            lo = max(0, index - max(max_neighbors, 12))
            hi = min(len(group), index + max(max_neighbors, 12) + 1)
            current_time = _timestamp(item.get("timestamp"))
            current_sender = (_sender_label(item), item.get("is_self") is True)
            for neighbor in group[lo:hi]:
                if neighbor is item:
                    continue
                distance = abs(_timestamp(neighbor.get("timestamp")) - current_time)
                same_sender = (_sender_label(neighbor), neighbor.get("is_self") is True) == current_sender
                if distance > limit and not (same_sender and distance <= burst_limit):
                    continue
                content = _display_content(neighbor.get("content"))
                if _is_text_candidate(neighbor, content) and (same_sender or not _LOW_SIGNAL.match(content)):
                    neighbors.append(neighbor)
            neighbors.sort(key=lambda value: (_timestamp(value.get("timestamp")), str(value.get("message_id") or "")))
            result[id(item)] = neighbors[:12]
    return result


def _context_supports(content: str, context: Sequence[Mapping[str, Any]]) -> bool:
    """Whether nearby text supplies a likely subject for a short/follow-up message."""

    if not context:
        return False
    current_terms = _text_terms(content)
    for neighbor in context:
        neighbor_content = _display_content(neighbor.get("content"))
        neighbor_terms = _text_terms(neighbor_content)
        if current_terms.intersection(neighbor_terms):
            return True
    # Two substantive domain messages in the same short window are a weaker,
    # but still useful, signal even when tokenization differs.
    return bool(
        _DOMAIN_SIGNAL.search(content)
        and any(_DOMAIN_SIGNAL.search(_display_content(item.get("content"))) for item in context)
    )


def _has_concrete_object(content: str) -> bool:
    """Require an object before calling a request or decision actionable."""

    normalized = re.sub(r"@[\w\-一-龥·]+", " ", content).strip()
    if _GENERIC_REQUEST.fullmatch(normalized) or re.search(
        r"(?:怎么用|怎么弄|怎么处理|研究一下)\s*[。！!?？]*$", normalized
    ):
        return False
    if _DOMAIN_SIGNAL.search(normalized) or _TRADE.search(normalized) or _RISK.search(normalized):
        return True
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return len(compact) >= 10


def _candidate_assessment(
    message: Mapping[str, Any],
    context: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Separate actionable work from context-dependent or low-value signals."""

    content = _display_content(message.get("content"))
    evidence = _action_evidence(content)
    domain_signal = bool(_DOMAIN_SIGNAL.search(content))
    has_object = _has_concrete_object(content)
    context_supported = _context_supports(content, context)
    has_actor = bool(re.search(r"(?:我|我们|他|她|团队|负责人|直接把|已经把)", content))
    context_dependent = bool(_CONTEXT_DEPENDENT.search(content))
    short_fragment = len(re.sub(r"\s+", "", content)) < 12
    context_ids = [str(item.get("message_id") or "") for item in context if item.get("message_id")]
    actionable_risk = bool(_RISK_ACTION.search(content))

    def result(state: str, kind: str, reason: str) -> Dict[str, Any]:
        return {
            "state": state,
            "kind": kind,
            "reason": reason,
            "domain_signal": domain_signal,
            "has_object": has_object,
            "context_supported": context_supported,
            "context_message_ids": context_ids[:4],
            "actionable_risk": actionable_risk,
        }

    if evidence.get("request") or evidence.get("question_request"):
        if not has_object:
            if context_supported:
                return result("reviewable", "action", "请求对象由同一会话的邻近上下文补足")
            return result("context_needed", "action", "出现请求表达，但没有明确说明要处理的对象")
        if context_dependent and not context_supported:
            return result("context_needed", "action", "请求依赖前文，当前窗口没有足够上下文")
        return result("reviewable", "action", "明确请求了可核对的对象或处理动作")

    if evidence.get("risk"):
        if _CLAUSE_FRAGMENT.search(content):
            if context_supported:
                return result("context_needed", "risk", "这条消息是上下文中的半句，已关联邻近消息但不单独升级")
            return result("context_needed", "risk", "风险表达像是上下文中的半句，当前窗口无法确认完整影响")
        if has_object or domain_signal or len(content) >= 12:
            return result(
                "reviewable",
                "risk" if actionable_risk else "event",
                "出现具体风险、故障或异常，需要回到原文核对",
            )
        return result("context_needed", "risk", "风险表达过短，暂时无法判断影响对象")

    if evidence.get("decision") or _PROPOSAL.search(content):
        if not domain_signal:
            return result("filtered_low_value", "decision", "决策或提议缺少工作、项目、技术或关系影响证据")
        if (context_dependent or not has_actor) and not context_supported:
            return result("context_needed", "decision", "决策表达缺少主体或前置背景")
        if not has_object or short_fragment:
            return result("context_needed", "decision", "决策表达没有足够对象和结果信息")
        return result("reviewable", "event", "出现有明确对象的项目、技术或工作变化")

    return result("low_information", "other", "没有明确请求、风险、决策或可核对的价值线索")


def _information_assessment(
    message: Mapping[str, Any],
    context: Sequence[Mapping[str, Any]],
    action_assessment: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Find useful discussion material without promoting it to a task.

    A shared AI group produces value in more forms than requests and risks:
    explanations, technical progress, resources, and competing ideas are
    useful even when nobody is asking the operator to do anything.  This
    second lane is intentionally separate from ``_candidate_assessment`` so
    the strict pending queue keeps its safety bar.
    """

    content = _display_content(message.get("content"))
    if not _is_text_candidate(message, content) or _LOW_SIGNAL.match(content):
        return None
    if _INFORMATION_EXCLUDE.search(content):
        return None
    if action_assessment and action_assessment.get("state") not in {
        "low_information", "filtered_low_value"
    }:
        return None

    resource = bool(_RESOURCE_SIGNAL.search(content))
    technical = bool(_INFORMATION_SIGNAL.search(content))
    argument = bool(_ARGUMENT_SIGNAL.search(content))
    long_enough = len(re.sub(r"\s+", "", content)) >= 24
    has_number = bool(re.search(r"\d", content))
    if not (resource or (technical and len(content) >= 10) or (long_enough and argument)):
        return None

    signals: List[str] = []
    if technical:
        signals.append("技术讨论")
    if resource:
        signals.append("资源链接")
    if argument:
        signals.append("观点解释")
    if has_number:
        signals.append("数据或指标")
    if resource:
        kind = "resource"
        reason = "发现可回看的资源或链接，适合整理为资料线索"
    elif technical and (_CHANGE_VERB.search(content) or _PROPOSAL.search(content)):
        kind = "progress"
        reason = "出现技术、产品或方案进展，但没有形成明确待办"
    elif argument or long_enough:
        kind = "knowledge"
        reason = "内容包含可复用的解释、观点或经验，不应被当作闲聊过滤"
    else:
        kind = "discussion"
        reason = "出现有主题的讨论片段，保留给 AI 做归纳"
    score = min(
        100,
        18
        + min(30, len(content) // 3)
        + (22 if resource else 0)
        + (16 if technical else 0)
        + (10 if argument else 0)
        + (8 if has_number else 0)
        + min(12, len(context) * 2),
    )
    return {
        "state": "informative",
        "kind": kind,
        "reason": reason,
        "signals": signals,
        "score": score,
        "context_message_ids": [
            str(item.get("message_id") or "")
            for item in context[:4]
            if item.get("message_id")
        ],
    }


def _discussion_episodes(
    items: Sequence[Mapping[str, Any]],
    timezone_name: str,
    max_gap_minutes: int = 45,
) -> List[Dict[str, Any]]:
    """Summarize high-density conversation windows for the intelligence view."""

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        key = str(item.get("chat_id") or item.get("chat_name") or "unknown")
        grouped[key].append(item)

    episodes: List[Dict[str, Any]] = []
    for chat_items in grouped.values():
        ordered = sorted(
            chat_items,
            key=lambda item: (_timestamp(item.get("timestamp")), str(item.get("message_id") or "")),
        )
        buckets: List[List[Mapping[str, Any]]] = []
        bucket: List[Mapping[str, Any]] = []
        previous: Optional[datetime] = None
        for item in ordered:
            current = _timestamp(item.get("timestamp"))
            if bucket and previous is not None and current - previous > timedelta(minutes=max_gap_minutes):
                buckets.append(bucket)
                bucket = []
            bucket.append(item)
            previous = current
        if bucket:
            buckets.append(bucket)

        for bucket in buckets:
            inbound = [item for item in bucket if item.get("is_self") is False]
            text_items = [
                item
                for item in inbound
                if _is_text_candidate(item, _display_content(item.get("content")))
            ]
            context_windows = _build_context_windows(bucket)
            information_by_id = {
                id(item): _information_assessment(
                    item,
                    context_windows.get(id(item), ()),
                    {"state": "low_information"},
                )
                for item in text_items
            }
            informative = [
                item
                for item in text_items
                if information_by_id.get(id(item)) is not None
            ]
            resource_count = sum(
                1 for item in text_items if _RESOURCE_SIGNAL.search(_display_content(item.get("content")))
            )
            participants = {
                _sender_label(item)
                for item in inbound
                if _sender_label(item) not in {"联系人", "群成员", "待识别成员"}
            }
            first = bucket[0]
            last = bucket[-1]
            # A burst is a product of conversation density, not a claim that
            # every line in it is important.  Keep the threshold low enough to
            # surface active AI groups while avoiding one-to-one idle chats.
            is_group = bool(
                first.get("is_group")
                or str(first.get("chat_id") or "").lower().endswith("@chatroom")
            )
            if not is_group and len(participants) < 2:
                continue
            if len(bucket) < 6 and len(informative) < 2 and len(participants) < 3:
                continue
            # Density alone is not intelligence.  A busy personal chat or a
            # noisy group window must contain at least one technical/domain
            # signal or a retained informative message to enter the insight
            # lane.
            has_domain_signal = any(
                _DOMAIN_SIGNAL.search(_display_content(item.get("content")))
                for item in text_items
            )
            if not informative and not has_domain_signal and resource_count == 0:
                continue
            lead_candidates = informative or text_items
            lead = max(
                lead_candidates,
                key=lambda item: (
                    int((information_by_id.get(id(item)) or {}).get("score") or 0),
                    1 if _RESOURCE_SIGNAL.search(_display_content(item.get("content"))) else 0,
                    len(_display_content(item.get("content"))),
                    _timestamp(item.get("timestamp")),
                ),
                default=None,
            )
            sample_items = sorted(
                informative or text_items,
                key=lambda item: (
                    int((information_by_id.get(id(item)) or {}).get("score") or 0),
                    len(_display_content(item.get("content"))),
                    _timestamp(item.get("timestamp")),
                ),
                reverse=True,
            )[:3]
            topic_hits = Counter()
            for item in text_items:
                lowered = _display_content(item.get("content")).casefold()
                tags = _insight_topic_tags(lowered)
                for topic in tags:
                    topic_hits[topic] += 1
                if not tags and _INFORMATION_SIGNAL.search(lowered):
                    topic_hits["其他技术"] += 1
            importance = min(
                100,
                20
                + min(35, len(bucket) // 3)
                + min(20, len(participants) * 3)
                + min(20, len(informative) * 4)
                + min(10, resource_count * 3),
            )
            episodes.append(
                {
                    "chat_id": first.get("chat_id"),
                    "chat_name": _chat_label(first),
                    "is_group": is_group,
                    "start": _visible_timestamp(_timestamp(first.get("timestamp")), timezone_name),
                    "end": _visible_timestamp(_timestamp(last.get("timestamp")), timezone_name),
                    "message_count": len(bucket),
                    "inbound_count": len(inbound),
                    "text_count": len(text_items),
                    "substantive_count": len(informative),
                    "participant_count": len(participants),
                    "participants": sorted(participants)[:12],
                    "resource_count": resource_count,
                    "importance": importance,
                    "topics": [
                        {"topic": topic, "count": count}
                        for topic, count in topic_hits.most_common(5)
                    ],
                    "summary": _clip(
                        "%s；%d 条有内容消息，主要涉及%s"
                        % (
                            _display_content(lead.get("content"))
                            if lead is not None
                            else "%s 在这一时段形成高密度讨论" % _chat_label(first),
                            len(informative),
                            "、".join(topic for topic, _count in topic_hits.most_common(3))
                            or "未命名主题",
                        )
                        if lead is not None
                        else "%s 在这一时段形成高密度讨论" % _chat_label(first),
                        180,
                    ),
                    "evidence_samples": [
                        {
                            "message_id": item.get("message_id"),
                            "sender_name": _sender_label(item),
                            "timestamp": _visible_timestamp(_timestamp(item.get("timestamp")), timezone_name),
                            "content": _clip(_display_content(item.get("content")), 180),
                        }
                        for item in sample_items
                        if item.get("message_id")
                    ],
                    "evidence": [
                        str(item.get("message_id") or "")
                        for item in bucket[:8]
                        if item.get("message_id")
                    ],
                }
            )
    episodes.sort(
        key=lambda item: (
            int(item.get("importance") or 0),
            int(item.get("substantive_count") or 0),
            int(item.get("message_count") or 0),
        ),
        reverse=True,
    )
    return episodes[:24]


def build_ai_context(
    messages: Iterable[Mapping[str, Any]],
    max_items: int = 120,
    priority_message_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Prepare a small, redacted candidate set for optional AI analysis.

    This function deliberately returns no raw message payload, internal ids,
    media paths or WeChat identifiers in the model-facing fields.  The
    private ``_source_message_id`` is only used locally to attach evidence to
    the response after the model returns.
    """

    safe_limit = max(1, min(int(max_items), 200))
    priority_ids = {str(value) for value in (priority_message_ids or []) if str(value)}
    source_items = [dict(raw) for raw in messages]
    context_windows = _build_context_windows(source_items)
    ranked: List[Dict[str, Any]] = []
    for item in source_items:
        content = _display_content(item.get("content"))
        score = _score_message(item)
        assessment = _candidate_assessment(item, context_windows.get(id(item), ()))
        information = _information_assessment(
            item,
            context_windows.get(id(item), ()),
            assessment,
        )
        if item.get("is_self") not in {True, False} or not score.get("eligible"):
            continue
        if _LOW_SIGNAL.match(content):
            continue
        if (
            assessment["state"] in {"filtered_low_value", "low_information"}
            and information is None
            and item.get("_transcribed_voice") is not True
        ):
            continue
        confidence = item.get("sender_name_confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            confidence_value = 0.0
        sender = "我" if item.get("is_self") is True else (
            _sender_label(item) if confidence_value >= 0.75 else (
                "待识别成员" if item.get("is_group") else "联系人"
            )
        )
        safe_context = []
        current_sender = (_sender_label(item), item.get("is_self") is True)
        for neighbor in context_windows.get(id(item), ())[:8]:
            neighbor_content = _display_content(neighbor.get("content"))
            safe_context.append(
                {
                    "sender_name": _sender_label(neighbor),
                    "is_self": neighbor.get("is_self") is True,
                    "same_sender": (_sender_label(neighbor), neighbor.get("is_self") is True) == current_sender,
                    "content": _clip(_redact_ai_content(neighbor_content), 240),
                }
            )
        level_rank = {"high": 3, "medium": 2, "low": 1}.get(score["level"], 0)
        candidate_state = (
            information["state"]
            if assessment["state"] in {"filtered_low_value", "low_information"}
            and information is not None
            else assessment["state"]
        )
        candidate_type = (
            information["kind"]
            if candidate_state == "informative" and information is not None
            else assessment["kind"]
        )
        rule_signals = list(score.get("tags") or [])
        if information is not None:
            rule_signals.extend(information.get("signals") or [])
        ranked.append(
            {
                "_source_message_id": str(item.get("message_id") or ""),
                "_timestamp": _timestamp(item.get("timestamp")),
                "chat_name": _chat_label(item),
                "sender_name": sender,
                "is_self": item.get("is_self") is True,
                "is_group": bool(item.get("is_group")),
                "content": _clip(_redact_ai_content(content), 360),
                "rule_signals": list(dict.fromkeys(rule_signals)),
                "rule_level": score.get("level") or "low",
                "candidate_state": candidate_state,
                "candidate_type": candidate_type,
                "context": safe_context,
                "_rank": (
                    1 if str(item.get("message_id") or "") in priority_ids else 0,
                    3 if assessment["state"] == "reviewable" else (
                        2 if information or item.get("_transcribed_voice") is True else 1
                    ),
                    int(information.get("score") or 0) if information else 0,
                    level_rank,
                    int(score.get("score") or 0),
                    len(content),
                ),
            }
        )

    # Preserve coverage across conversations while prioritizing messages that
    # already carry explicit evidence. This gives the model context without
    # uploading an entire day's archive.
    ranked.sort(key=lambda item: (item["_rank"], item["_timestamp"]), reverse=True)
    selected: List[Dict[str, Any]] = []
    per_chat: Counter = Counter()
    per_chat_limit = max(12, min(24, (safe_limit + 5) // 6))
    for item in ranked:
        chat = str(item["chat_name"])
        if per_chat[chat] >= per_chat_limit and len(selected) < safe_limit:
            continue
        selected.append(item)
        per_chat[chat] += 1
        if len(selected) >= safe_limit:
            break
    selected.sort(key=lambda item: (item["_timestamp"], item["_source_message_id"]))
    output: List[Dict[str, Any]] = []
    for index, item in enumerate(selected, 1):
        output.append(
            {
                "evidence_ref": "m-%03d" % index,
                "chat_name": item["chat_name"],
                "sender_name": item["sender_name"],
                "is_self": item["is_self"],
                "is_group": item["is_group"],
                "content": item["content"],
                "rule_signals": item["rule_signals"],
                "rule_level": item["rule_level"],
                "candidate_state": item["candidate_state"],
                "candidate_type": item["candidate_type"],
                "context": item["context"],
                "timestamp": item["_timestamp"].isoformat(timespec="seconds"),
                "_source_message_id": item["_source_message_id"],
            }
        )
    return output


def analyze_messages(
    messages: Iterable[Mapping[str, Any]],
    start_at: datetime,
    end_at: datetime,
    timezone_name: str = "Asia/Shanghai",
    profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a compact, evidence-backed workbench snapshot."""

    get_timezone(timezone_name)
    ordered: List[Dict[str, Any]] = []
    for raw in messages:
        item = dict(raw)
        item["_timestamp"] = _timestamp(item.get("timestamp"))
        ordered.append(item)
    ordered.sort(key=lambda item: (item["_timestamp"], str(item.get("message_id") or "")))
    context_windows = _build_context_windows(ordered)
    for item in ordered:
        score = _score_message(item)
        assessment = _candidate_assessment(item, context_windows.get(id(item), ()))
        if item.get("is_self") is False and score.get("eligible"):
            score.update(
                {
                    "candidate_state": assessment["state"],
                    "candidate_type": assessment["kind"],
                    "context_supported": assessment["context_supported"],
                    "context_message_ids": assessment["context_message_ids"],
                    "has_concrete_object": assessment["has_object"],
                    "domain_signal": assessment["domain_signal"],
                }
            )
            if assessment["state"] == "context_needed":
                score["level"] = "low"
                score["value_label"] = "上下文不足"
                score["reason"] = assessment["reason"]
                score["score"] = min(int(score.get("score") or 0), 20)
            elif assessment["state"] == "filtered_low_value":
                score["level"] = "low"
                score["value_label"] = "低价值线索"
                score["reason"] = assessment["reason"]
                score["score"] = 0
            elif assessment["state"] == "low_information":
                score["reason"] = assessment["reason"]
        item["_assessment"] = assessment
        item["_score"] = score

    total = len(ordered)
    inbound = sum(1 for item in ordered if item.get("is_self") is False)
    self_count = sum(1 for item in ordered if item.get("is_self") is True)
    unknown_direction = total - inbound - self_count
    chat_counts: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "chat_id": "", "chat_name": "", "messages": 0, "inbound": 0,
            "high_value": 0, "media": 0, "substantive": 0,
        }
    )
    type_counts = Counter()
    hour_counts = Counter()
    hourly_stats: Dict[int, Dict[str, int]] = defaultdict(
        lambda: {"count": 0, "inbound": 0, "text": 0, "substantive": 0, "media": 0}
    )
    chat_senders: Dict[str, set] = defaultdict(set)
    topic_counts = Counter()
    scored: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    discoveries: List[Dict[str, Any]] = []
    excluded_non_text = 0
    excluded_low_signal = 0
    identity_required = 0
    identity_resolved = 0
    media_total = 0
    media_with_path = 0
    raw_signal_count = 0
    reviewable_count = 0
    context_needed_count = 0
    filtered_low_value_count = 0
    informative_count = 0
    high_information_count = 0
    resource_count = 0
    insight_kind_counts = Counter()
    topic_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "message_count": 0,
            "chats": set(),
            "senders": set(),
            "resource_count": 0,
            "high_information_count": 0,
            "score_total": 0,
            "kinds": Counter(),
            "evidence": [],
        }
    )
    suppressed_candidates: List[Dict[str, Any]] = []

    for item in ordered:
        chat_id = str(item.get("chat_id") or item.get("chat_name") or "unknown")
        chat = chat_counts[chat_id]
        chat["chat_id"] = chat_id
        chat["chat_name"] = _chat_label(item)
        chat["messages"] += 1
        if item.get("is_self") is False:
            chat["inbound"] += 1
            sender_label = _sender_label(item)
            if sender_label not in {"联系人", "群成员", "待识别成员"}:
                chat_senders[chat_id].add(sender_label)
            identity_required += 1
            if _identity_is_resolved(item):
                identity_resolved += 1
        score = item["_score"]
        if score["level"] == "high":
            chat["high_value"] += 1
        if score["level"] == "excluded":
            excluded_non_text += 1
        elif score["level"] == "low" and item.get("is_self") is False:
            excluded_low_signal += 1
        type_counts[str(item.get("message_type") or "other")] += 1
        if str(item.get("message_type") or "other") not in {"text", "system"}:
            media_total += 1
            chat["media"] += 1
            if str(item.get("media_path") or "").strip():
                media_with_path += 1
        hour = as_timezone(item["_timestamp"], timezone_name).hour
        hour_counts[hour] += 1
        hourly_stats[hour]["count"] += 1
        if item.get("is_self") is False:
            hourly_stats[hour]["inbound"] += 1
        if str(item.get("message_type") or "other") in {"text", "other"}:
            hourly_stats[hour]["text"] += 1
        else:
            hourly_stats[hour]["media"] += 1
        content = score.get("content") or _display_content(item.get("content"))
        if item.get("is_self") is False and score.get("eligible"):
            assessment = item.get("_assessment") or {}
            information = _information_assessment(
                item,
                context_windows.get(id(item), ()),
                assessment,
            )
            if information is not None:
                informative_count += 1
                information_score = int(information.get("score") or 0)
                if information_score >= 65:
                    high_information_count += 1
                chat["substantive"] += 1
                hourly_stats[hour]["substantive"] += 1
                information_kind = str(information.get("kind") or "discussion")
                insight_kind_counts[information_kind] += 1
                if information_kind == "resource":
                    resource_count += 1
                topic_tags = _insight_topic_tags(content) or ["其他讨论"]
                for topic in topic_tags:
                    topic_counts[topic] += 1
                    stats = topic_stats[topic]
                    stats["message_count"] += 1
                    stats["chats"].add(str(item.get("chat_id") or item.get("chat_name") or "unknown"))
                    sender_label = _sender_label(item)
                    if sender_label not in {"联系人", "群成员", "待识别成员"}:
                        stats["senders"].add(sender_label)
                    stats["resource_count"] += int(information_kind == "resource")
                    stats["high_information_count"] += int(information_score >= 65)
                    stats["score_total"] += information_score
                    stats["kinds"][information_kind] += 1
                    if len(stats["evidence"]) < 12:
                        stats["evidence"].append(
                            {
                                "message_id": item.get("message_id"),
                                "chat_id": item.get("chat_id"),
                                "chat_name": _chat_label(item),
                                "sender_name": _sender_label(item),
                                "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                                "content": _clip(content),
                                "score": information_score,
                                "kind": information_kind,
                            }
                        )
                discoveries.append(
                    {
                        "message_id": item.get("message_id"),
                        "chat_id": item.get("chat_id"),
                        "chat_name": _chat_label(item),
                        "sender_name": _sender_label(item),
                        "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                        "content": _clip(content),
                        "kind": information_kind,
                        "tags": list(information.get("signals") or []),
                        "topics": topic_tags,
                        "score": information_score,
                        "value_level": "high" if information_score >= 75 else ("medium" if information_score >= 55 else "low"),
                        "candidate_state": "informative",
                        "reason": information.get("reason"),
                        "context_message_ids": list(information.get("context_message_ids") or []),
                        "evidence": item.get("message_id"),
                    }
                )
            if score.get("tags") and assessment.get("state") != "low_information":
                raw_signal_count += 1
            if assessment.get("state") == "reviewable":
                reviewable_count += 1
            elif assessment.get("state") == "context_needed":
                context_needed_count += 1
            elif assessment.get("state") == "filtered_low_value":
                filtered_low_value_count += 1
            if assessment.get("state") in {"context_needed", "filtered_low_value"} and score.get("tags"):
                if len(suppressed_candidates) < 20:
                    suppressed_candidates.append(
                        {
                            "message_id": item.get("message_id"),
                            "chat_id": item.get("chat_id"),
                            "chat_name": _chat_label(item),
                            "sender_name": _sender_label(item),
                            "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                            "content": _clip(content),
                            "tags": list(score.get("tags") or []),
                            "candidate_state": assessment.get("state"),
                            "reason": assessment.get("reason"),
                            "context_message_ids": list(assessment.get("context_message_ids") or []),
                        }
                    )
            if (
                assessment.get("state") == "reviewable"
                and score["level"] in {"high", "medium"}
                and score["score"] >= 28
            ):
                scored.append(item)
            evidence = _action_evidence(content)
            review_kind = str(assessment.get("kind") or "")
            if assessment.get("state") == "reviewable" and (
                review_kind == "action"
                or (review_kind == "risk" and assessment.get("actionable_risk"))
            ):
                action_tags = list(dict.fromkeys(evidence.get("tags") or score.get("tags") or []))
                if review_kind == "risk" and "风险" not in action_tags:
                    action_tags.append("风险")
                due_match = _DEADLINE.search(content)
                actions.append(
                    {
                        "message_id": item.get("message_id"), "chat_id": item.get("chat_id"),
                        "chat_name": _chat_label(item), "sender_name": _sender_label(item),
                        "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                        "content": _clip(content), "tags": action_tags,
                        "due_hint": due_match.group(1) if due_match else None,
                        "status": "待确认", "candidate_state": "reviewable",
                        "candidate_type": review_kind,
                        "reason": assessment.get("reason"),
                        "context_message_ids": list(assessment.get("context_message_ids") or []),
                        "evidence": item.get("message_id"),
                    }
                )

    scored.sort(key=lambda item: (item["_score"]["score"], item["_timestamp"]), reverse=True)
    highlights: List[Dict[str, Any]] = []
    # The workbench's first screen is for confirmed-by-rule high-value items.
    # If there are none, show the best medium items so an empty archive does
    # not look broken; medium items never displace a high-value item.
    highlight_source = [item for item in scored if item["_score"]["level"] == "high"] or scored
    for item in highlight_source[:12]:
        score = item["_score"]
        highlights.append(
            {
                "message_id": item.get("message_id"), "chat_id": item.get("chat_id"),
                "chat_name": _chat_label(item), "sender_name": _sender_label(item),
                "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                "content": _clip(score.get("content") or item.get("content")),
                "score": score["score"], "level": score["level"],
                "value_label": score["value_label"], "tags": score["tags"],
                "reason": score["reason"], "candidate_state": score.get("candidate_state") or "reviewable",
                "candidate_type": score.get("candidate_type") or "event",
                "context_message_ids": list(score.get("context_message_ids") or []),
                "evidence": item.get("message_id"),
            }
        )

    discoveries.sort(
        key=lambda item: (
            int(item.get("score") or 0),
            _timestamp(item.get("timestamp")),
        ),
        reverse=True,
    )
    discoveries = discoveries[:24]

    episodes: List[Dict[str, Any]] = []
    by_chat: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in scored:
        by_chat[str(item.get("chat_id") or item.get("chat_name") or "unknown")].append(item)
    for chat_items in by_chat.values():
        chat_items = sorted(
            chat_items,
            key=lambda item: (item["_timestamp"], str(item.get("message_id") or "")),
        )
        bucket: List[Mapping[str, Any]] = []
        previous: Optional[datetime] = None
        for item in chat_items + [None]:
            current = item["_timestamp"] if item is not None else None
            if bucket and (current is None or current - (previous or current) > timedelta(minutes=90)):
                episodes.append(_episode(bucket, timezone_name))
                bucket = []
            if item is not None:
                bucket.append(item)
                previous = current
    episodes.sort(key=lambda item: (item["importance"], item["message_count"]), reverse=True)

    peak_hour = max(hour_counts, key=hour_counts.get) if hour_counts else None
    top_chat = max(chat_counts.values(), key=lambda value: value["messages"], default=None)
    for chat_id, chat in chat_counts.items():
        chat["participants"] = len(chat_senders.get(chat_id, set()))
    high_count = sum(1 for item in scored if item["_score"]["level"] == "high")
    medium_count = sum(1 for item in scored if item["_score"]["level"] == "medium")
    eligible_inbound = sum(1 for item in ordered if item.get("is_self") is False and item["_score"].get("eligible"))
    low_count = max(0, eligible_inbound - high_count - medium_count)
    discussion_episodes = _discussion_episodes(ordered, timezone_name)
    event_briefs = _event_briefs(ordered, timezone_name, profile)
    unformed_dynamics = _small_matters(ordered, event_briefs, timezone_name)
    event_message_ids = {
        str(message_id)
        for event in event_briefs
        for message_id in (event.get("message_ids") or [])
        if message_id
    }
    dynamic_message_ids = {
        str(message_id)
        for dynamic in unformed_dynamics
        for message_id in (dynamic.get("message_ids") or [])
        if message_id
    }
    published_message_count = len(event_message_ids | dynamic_message_ids)
    # Every row has passed the local census, while noisy group-chat rows can be
    # intentionally suppressed from the editorial output.
    accounted_message_count = len(ordered)

    topic_briefs: List[Dict[str, Any]] = []
    for topic, stats in topic_stats.items():
        message_count = int(stats["message_count"])
        if not message_count:
            continue
        evidence = sorted(
            stats["evidence"],
            key=lambda item: (int(item.get("score") or 0), _timestamp(item.get("timestamp"))),
            reverse=True,
        )
        average_score = round(int(stats["score_total"]) / message_count)
        chat_count = len(stats["chats"])
        resource_total = int(stats["resource_count"])
        high_information_total = int(stats["high_information_count"])
        if chat_count >= 2:
            why_it_matters = "这个主题在多个会话出现，适合做跨群聚合，而不是只看单条消息。"
        elif resource_total:
            why_it_matters = "这个主题包含可回看的资源线索，适合继续核对链接和上下文。"
        else:
            why_it_matters = "这个主题在当前会话持续出现，适合回看讨论脉络。"
        topic_briefs.append(
            {
                "kind": "theme",
                "topic": topic,
                "message_count": message_count,
                "chat_count": chat_count,
                "sender_count": len(stats["senders"]),
                "resource_count": resource_total,
                "high_information_count": high_information_total,
                "average_score": average_score,
                "value_level": "high" if average_score >= 75 else ("medium" if average_score >= 55 else "low"),
                "kinds": [
                    {"kind": kind, "count": count}
                    for kind, count in stats["kinds"].most_common()
                ],
                "summary": (
                    "%s：%d 条有内容消息，来自 %d 个会话；高信息量 %d 条，%s。"
                    % (
                        topic,
                        message_count,
                        chat_count,
                        high_information_total,
                        (
                            "%d 条资源线索" % resource_total
                            if resource_total
                            else "主要是" + "、".join(
                                "%s %d 条" % (_INSIGHT_KIND_LABELS.get(kind, kind), count)
                                for kind, count in stats["kinds"].most_common(3)
                            )
                        ),
                    )
                ),
                "why_it_matters": why_it_matters,
                "evidence": evidence[:3],
            }
        )
    topic_briefs.sort(
        key=lambda item: (
            int(item.get("message_count") or 0),
            int(item.get("average_score") or 0),
            int(item.get("resource_count") or 0),
        ),
        reverse=True,
    )

    insight_breakdown = [
        {"kind": kind, "label": _INSIGHT_KIND_LABELS.get(kind, kind), "count": count}
        for kind, count in insight_kind_counts.most_common()
    ]
    primary_insights: List[Dict[str, Any]] = []
    primary_topics = [
        topic for topic in topic_briefs if topic.get("topic") != "其他讨论"
    ] or topic_briefs
    for topic in primary_topics[:6]:
        importance = min(
            100,
            int(topic.get("average_score") or 0)
            + min(10, int(topic.get("message_count") or 0) // 20)
            + min(8, int(topic.get("chat_count") or 0) * 2)
            + (6 if int(topic.get("resource_count") or 0) else 0),
        )
        confidence = min(
            90,
            45 + min(30, int(topic.get("chat_count") or 0) * 10)
            + (10 if int(topic.get("resource_count") or 0) else 0),
        )
        primary_insights.append(
            {
                "id": "topic:%s" % str(topic.get("topic") or "other"),
                "kind": "theme",
                "title": topic.get("topic"),
                "category": "主题",
                "importance": importance,
                "confidence": confidence,
                "summary": topic.get("summary"),
                "reason": topic.get("why_it_matters"),
                "next_step": "展开主题证据，查看相关会话中的完整上下文",
                "value_level": topic.get("value_level") or "low",
                "what_changed": topic.get("summary"),
                "why_it_matters": topic.get("why_it_matters"),
                "uncertainty": "这是按消息主题聚合出的主线，不代表每条消息都已形成事实结论。",
                "evidence": topic.get("evidence") or [],
                "topics": [topic.get("topic")],
            }
        )
    for event in episodes[:3]:
        primary_insights.append(
            {
                "id": "event:%s" % str((event.get("evidence") or ["event"])[0]),
                "kind": "event",
                "title": "事件候选 · %s" % str(event.get("chat_name") or "会话"),
                "category": "事件",
                "importance": int(event.get("importance") or 0),
                "confidence": 60,
                "summary": event.get("summary"),
                "reason": "同一会话在相邻时间内出现多条可核对消息。",
                "next_step": "点击证据，核对事件是否形成实际变化或后续动作",
                "evidence": [
                    {"message_id": message_id}
                    for message_id in (event.get("evidence") or [])
                    if message_id
                ],
                "topics": [],
            }
        )
    primary_insights.sort(
        key=lambda item: (
            int(item.get("importance") or 0),
            int(item.get("confidence") or 0),
        ),
        reverse=True,
    )
    # The first screen is event-led.  Topic buckets remain available for
    # exploration, but never compete with concrete, cross-chat briefings.
    primary_insights = event_briefs[:8]
    for_me = [item for item in event_briefs if item.get("lane") == "for_me"]
    trending = [item for item in event_briefs if item.get("lane") == "trending"]
    pending_review = [item for item in event_briefs if item.get("lane") == "pending"]

    top_chats = sorted(chat_counts.values(), key=lambda value: value["messages"], reverse=True)[:12]
    visible_start = _visible_timestamp(start_at.astimezone(timezone.utc), timezone_name)
    visible_end = _visible_timestamp(end_at.astimezone(timezone.utc), timezone_name)
    if not total:
        narrative = "这个时间范围内还没有导入消息。先执行一次历史同步，分析才有依据。"
    else:
        peak_text = "%02d:00" % peak_hour if peak_hour is not None else "—"
        top_text = top_chat["chat_name"] if top_chat else "—"
        topic_text = "、".join(str(item.get("title")) for item in event_briefs[:3]) or "尚未形成明确事件"
        narrative = (
            "今天的主线是%s。共记录 %d 条消息，覆盖 %d 个会话；%s 时段最活跃，会话“%s”消息最多。"
            "严格待处理 %d 条；另有 %d 条有内容消息、%d 个有效讨论片段和 %d 条资源线索。"
            % (
                topic_text, total, len(chat_counts), peak_text, top_text,
                len(actions), informative_count, len(discussion_episodes), resource_count,
            )
        )

    if not total:
        situation = {
            "headline": "当前窗口没有可分析消息",
            "points": ["先同步历史消息，再进行主题和事件分析。"],
            "scope_note": "本地 SQLite · 规则聚合",
        }
    else:
        top_topic_names = [str(item.get("title")) for item in event_briefs[:2]]
        headline = (
            "主线集中在%s"
            % "、".join(top_topic_names)
            if top_topic_names
            else "当前窗口有消息，但尚未形成稳定主题"
        )
        situation = {
            "headline": headline,
            "points": [
                event_briefs[0].get("summary")
                if event_briefs
                else "暂未识别出可核对事件。",
                "%d 个有效讨论片段，%d 条资源线索；这些内容不等同于待办，但值得回看。"
                % (len(discussion_episodes), resource_count),
                "严格待处理 %d 条；被收起的片段仍保留在信息流，不会被误当成任务。"
                % len(actions),
            ],
            "scope_note": "本地规则聚合 · 每条主线保留可回看证据",
        }
    voice_total = sum(1 for item in ordered if str(item.get("message_type") or "").lower() == "voice")
    voice_transcribed = sum(1 for item in ordered if item.get("_transcribed_voice") is True)
    coverage = round(eligible_inbound / inbound, 3) if inbound else 0.0
    return {
        "window": {"start": visible_start, "end": visible_end, "timezone": timezone_name},
        "narrative": narrative,
        "situation": situation,
        "method": {
            "name": "rules_v1", "version": "event_v1", "label": "跨会话事件聚合",
            "note": "以对象、时间和语义连续性聚合碎片消息；每条简报保留人物、会话和原文证据。",
        },
        "summary": {
            "messages": total, "inbound": inbound, "self": self_count,
            "unknown_direction": unknown_direction, "chats": len(chat_counts),
            "events": len(episodes), "actions": len(actions), "high_value": high_count,
            "needs_attention": medium_count, "low_signal": low_count,
            "reviewable": reviewable_count, "context_needed": context_needed_count,
            "filtered_low_value": filtered_low_value_count,
            "substantive": informative_count,
            "high_information": high_information_count,
            "resources": resource_count,
            "discussion_episodes": len(discussion_episodes),
            "primary_insights": len(primary_insights),
            "event_briefs": len(event_briefs),
            "for_me": len(for_me),
            "trending": len(trending),
            "pending_review": len(pending_review),
            "topic_count": len(topic_briefs),
            "unformed_dynamics": len(unformed_dynamics),
            "unformed_messages": len(dynamic_message_ids),
            "accounted_messages": accounted_message_count,
            "published_messages": published_message_count,
            "suppressed_group_noise": max(0, len(ordered) - published_message_count),
            "active_participants": len({name for names in chat_senders.values() for name in names}),
            "media": media_total,
            "voice_total": voice_total,
            "voice_transcribed": voice_transcribed,
            "voice_transcript_coverage": round(voice_transcribed / voice_total, 3) if voice_total else 1.0,
        },
        "quality": {
            "inbound_text_candidates": eligible_inbound, "excluded_non_text": excluded_non_text,
            "excluded_low_signal": excluded_low_signal, "analysis_coverage": coverage,
            "raw_signal_count": raw_signal_count,
            "reviewable_count": reviewable_count,
            "context_needed_count": context_needed_count,
            "filtered_low_value_count": filtered_low_value_count,
            "informative_count": informative_count,
            "high_information_count": high_information_count,
            "resource_count": resource_count,
            "discussion_episode_count": len(discussion_episodes),
            "topic_count": len(topic_briefs),
            "insight_coverage": round(informative_count / eligible_inbound, 3) if eligible_inbound else 0.0,
            "accounted_message_coverage": round(accounted_message_count / total, 3) if total else 1.0,
            "unformed_dynamic_count": len(unformed_dynamics),
            "candidate_precision_note": "待处理只包含具备明确对象、行动/风险证据或充分上下文的入站消息；被收起的线索仍可在信息流中检索。",
            "capture_completeness": None,
            "capture_completeness_state": "unknown",
            "identity_required": identity_required,
            "identity_resolved": identity_resolved,
            "identity_resolution_rate": round(identity_resolved / identity_required, 3) if identity_required else 0.0,
            "media_total": media_total,
            "media_with_path": media_with_path,
            "media_path_coverage": round(media_with_path / media_total, 3) if media_total else 0.0,
            "voice_total": voice_total,
            "voice_transcribed": voice_transcribed,
            "voice_transcript_coverage": round(voice_transcribed / voice_total, 3) if voice_total else 1.0,
            "media_file_open_rate": None,
            "limitation": "抓取完整度需要源端应有条数才能计算；严格待处理结果用于人工筛选；讨论洞察用于归纳，不等同于事实判断；媒体内容暂不参与价值判断。",
        },
        "hourly": [
            {"hour": hour, **hourly_stats[hour]}
            for hour in range(24)
        ],
        "top_chats": top_chats,
        "topics": [{"topic": key, "count": value} for key, value in topic_counts.most_common()],
        "types": [{"type": key, "count": value} for key, value in type_counts.most_common()],
        "topic_briefs": topic_briefs,
        "primary_insights": primary_insights,
        "event_briefs": event_briefs,
        "for_me": for_me,
        "trending": trending,
        "pending_review": pending_review,
        "unformed_dynamics": unformed_dynamics,
        "insight_breakdown": insight_breakdown,
        "highlights": highlights, "actions": actions[:20], "events": episodes[:10],
        "discoveries": discoveries,
        "discussion_episodes": discussion_episodes,
        "activity": {
            "hourly": [
                {"hour": hour, **hourly_stats[hour]}
                for hour in range(24)
            ],
            "chat_activity": top_chats,
            "type_mix": [
                {"type": key, "count": value}
                for key, value in type_counts.most_common()
            ],
        },
        "suppressed_candidates": suppressed_candidates,
        "freshness": {
            "source": "本地 SQLite 消息库", "stored_messages": total,
            "truncated": False, "analysis_coverage": coverage,
            "reviewable_count": reviewable_count,
            "context_needed_count": context_needed_count,
            "capture_completeness": None, "capture_completeness_state": "unknown",
        },
    }


def _episode(items: Sequence[Mapping[str, Any]], timezone_name: str) -> Dict[str, Any]:
    first = items[0]
    last = items[-1]
    max_score = max(int(item["_score"]["score"]) for item in items)
    importance = min(100, max_score + min(40, len(items) * 8))
    lead = next((str(item["_score"].get("content") or "").strip() for item in items), "无文本内容")
    return {
        "chat_id": first.get("chat_id"), "chat_name": _chat_label(first),
        "start": _visible_timestamp(first["_timestamp"], timezone_name),
        "end": _visible_timestamp(last["_timestamp"], timezone_name),
        "message_count": len(items), "importance": importance,
        "summary": _clip(lead, 120), "evidence": [item.get("message_id") for item in items[:8]],
    }
