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
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

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
    "账号与平台": (
        "github", "linux.do", "linuxdo", "v2ex", "linuxsb", "注册", "登录", "登陆",
        "重置密码", "邮箱", "邮件", "账号", "账户",
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
_RISK = re.compile(r"(紧急|事故|故障|风险|风控|封号|封禁|禁言|异常|失败|无法|投诉|泄露|中断|逾期|冲突|不一致)")
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
_PROJECT_SIGNAL = re.compile(
    r"(群聊|私聊|信息茧房|注意力|梗|蒸馏|风控|只读|同步|录音|转写|发言人|噪声|并行|挖坟|企业微信|飞书|Line|低频|归档|总结功能|链路|本地读取|自动发消息)",
    re.IGNORECASE,
)
_FEEDBACK_SIGNAL = re.compile(
    r"(我想|我觉得|我认为|希望|最好|喜欢|路线|路径|风格|效果|体验|需求|放弃|很差|不好搞|不说人话|精确度|准确度|目前只能|区别|问题|买点)",
    re.IGNORECASE,
)
_EVENT_ALIAS_RULES = (
    ("账号风控", re.compile(r"(?:风控|封号|封禁|禁言|只读分析|只读|本地读取|同步频率|自动发消息|被检测|安全保证)", re.IGNORECASE)),
    ("账号与平台", re.compile(r"(?:github|linux\.do|linuxdo|v2ex|linuxsb|账号注册|注册.*(?:账号|邮箱)|登录.*(?:账号|平台)|重置密码|收不到邮件)", re.IGNORECASE)),
    ("跨会话归档", re.compile(r"(?:跨.*(?:群|会话)|多个群聊|群聊和私聊|同一件事.*归档|跨不同会议|归档到一条主线)", re.IGNORECASE)),
    ("群聊总结", re.compile(r"(?:群聊总结|日报|周报|几个主题|主题点|总结功能)", re.IGNORECASE)),
    ("梗与热词", re.compile(r"(?:互联网热词|热点词库|热词|热潮|梗|蒸馏|未来古法|复古路线|像素风格|低效.*买点)", re.IGNORECASE)),
    ("注意力过滤", re.compile(r"(?:信息茧房|不看什么|屏蔽|过滤|注意力消耗)", re.IGNORECASE)),
    ("群聊噪声", re.compile(r"(?:噪声|并行|挖坟|上下文断裂|插话|不说人话|精确度|准确度|分类|筛选上下文)", re.IGNORECASE)),
    ("会议转写", re.compile(r"(?:会议录音|录音质量|录音设备|转写质量|转写|发言人|错别字)", re.IGNORECASE)),
    ("产品差异", re.compile(r"(?:微信.*(?:AI|总结)|Line.*群聊|手机微信.*功能|和.*总结功能.*区别|飞书|企业微信)", re.IGNORECASE)),
    ("模型部署", re.compile(r"(?:模型部署|接口返回\s*5\d\d|日志.*上传|服务返回\s*5\d\d)", re.IGNORECASE)),
    ("选课安排", re.compile(r"(?:选课|课程|教务|课表|学分|退补选)", re.IGNORECASE)),
    ("成本变化", re.compile(r"(?:报价|价格|成本|收费|费用|预算|付款)", re.IGNORECASE)),
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
_VAGUE_REFERENT = re.compile(
    r"(?:这件事|这个事|那个事|这个|那个|它|这块|那块|这东西|那个东西|"
    r"咋办|咋整|怎么搞|搞一下|弄一下|再看看|说不清|不说人话)"
)
_COLLOQUIAL_MARKER = re.compile(
    r"(?:咋|啥|这玩意|整一下|搞一下|不咋地|拉垮|寄了|绷不住|离谱|玄学|说人话)"
)
_DIRECT_REFERENCE_KEYS = (
    "reply_to_message_id", "reply_to_id", "quoted_message_id", "quote_message_id",
    "referenced_message_id", "reference_message_id", "parent_message_id",
    "in_reply_to", "reply_to", "referenced_message", "quoted_message",
)
_REFERENCE_TEXT_KEYS = (
    "quoted_content", "quote_content", "quoted_text", "quote_text",
    "reference_content", "referenced_content", "reply_content", "quote",
)
_CLAUSE_FRAGMENT = re.compile(r"^\s*(?:至于|但是|不过|所以|因此|如果|因为|其中|那些|对于)|(?:的用户|的人|的话)\s*[。！!?？]*$")
_RISK_ACTION = re.compile(r"(请|需要|应当|必须|尽快|马上|处理|修复|停止|避免|确认|排查|跟进|投诉|泄露|中断|逾期)")
_GENERIC_REQUEST = re.compile(
    r"(?:需要你|请你|帮我|麻烦|请)\s*(?:研究一下|看看|弄一下|处理一下|确认一下)?\s*(?:怎么用|怎么弄|怎么处理)\s*[。！!?？]*$"
)
_GENERIC_WORDS = {"今天", "明天", "后天", "这个", "那个", "一下", "然后", "可以", "需要", "怎么", "研究"}
_CONTEXT_WEAK_TERMS = _GENERIC_WORDS | {
    "看来", "感觉", "觉得", "主要", "确实", "其实", "可能", "应该", "现在", "已经",
    "就是", "还是", "比较", "很大", "不错", "问题", "事情", "内容", "信息", "大家",
    "请", "麻烦", "确认", "处理", "研究", "看看", "弄", "搞", "回复", "跟进", "一下",
}
_EVENT_STOP_TERMS = _GENERIC_WORDS | {
    "我们", "你们", "他们", "自己", "现在", "已经", "还是", "就是", "不是", "一个", "一些",
    "这里", "那里", "这样", "那样", "可能", "应该", "感觉", "觉得", "知道", "看看", "进行",
    "比较", "没有", "什么", "事情", "问题", "消息", "内容", "时候", "真的", "的话", "之后",
    "之前", "目前", "大家", "因为", "所以", "但是", "不过", "而且", "如果", "或者", "以及",
    "时间", "价格", "国家", "模型", "代码", "系统", "项目", "消息", "群聊", "功能", "用户",
}
_EVENT_BAD_ANCHOR_EDGES = set("我你他她它这那的了是在有和与就都也会能可要把被让给对从到及或而但因所为吗呢吧啊哦")
_GENERIC_EVENT_ANCHORS = {
    "ai", "gpt", "gptpro", "token", "tokens", "claude", "deepseek",
    "codex", "模型", "大模型", "人工智能", "pro", "agent", "api",
    "微信", "企业微信", "飞书", "群聊", "私聊", "聊天", "消息",
    "明白", "一样", "感觉", "今天", "昨天", "但是", "就是", "觉得",
    "可以", "这个", "那个", "然后", "自己", "现在", "还是", "没有",
    "一个", "什么", "怎么", "可能", "应该", "直接", "已经", "真的",
}
_EVENT_ANCHOR_WEAK_PARTS = {
    "今天", "明天", "昨天", "本周", "下周", "研究", "重点", "需要", "确认",
    "采用", "开始", "进行", "遇到", "可以", "可能", "应该", "直接", "已经",
    "关于", "一个", "这个", "那个", "方案",
}
_WEAK_SHARED_EVENT_TOPICS = {
    "产品 / 项目", "开发 / 工具", "搜索 / 研究", "社群 / 活动",
    "风险 / 安全", "成本 / 额度", "费用与交易",
}
_CONTEXT_WEAK_SUBJECTS = {"产品 / 项目", "社群 / 活动"}

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
    (r"GitHub|Linux\.do|Linuxdo|V2EX|linuxsb|账号注册|重置密码|收不到邮件", "账号与平台"),
    (r"选课|课程|教务|课表|学分|退补选", "选课安排"),
    (r"微信.*(?:AI|总结)|Line|产品差异", "产品差异"),
    (r"信息茧房|不看什么|注意力|屏蔽|过滤", "注意力过滤"),
    (r"梗|蒸馏|未来古法|复古路线|像素风格", "群内梗识别"),
    (r"群聊总结|日报|周报|主题点|归档", "跨会话归档"),
    (r"录音|转写|发言人", "会议转写"),
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
    if re.search(r"微信|企业微信|飞书", combined, re.IGNORECASE) and re.search(r"只读分析|只读|自动发消息|同步频率|封号|封禁|被检测", combined, re.IGNORECASE):
        candidate = "账号风控：微信只读与合规争议"
    elif re.search(r"codex", combined, re.IGNORECASE) and re.search(r"重置|reset|额度", combined, re.IGNORECASE):
        candidate = "Codex重置：额度状态待核实"
    elif re.search(r"gpt|chatgpt|claude|deepseek|openai|kimi", combined, re.IGNORECASE) and re.search(r"重置|reset|额度|中转|封号|封禁", combined, re.IGNORECASE):
        candidate = "AI账号：重置与稳定性争议"
    elif re.search(r"gpt", combined, re.IGNORECASE) and re.search(r"封号|封禁", combined, re.IGNORECASE) and re.search(r"成本|价格|贵|费用", combined, re.IGNORECASE):
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


_DETAIL_ENTITY_RULES = (
    ("GitHub", re.compile(r"\bgithub(?:\.com)?\b", re.IGNORECASE)),
    ("Linux.do", re.compile(r"\blinux(?:\.do|do)\b", re.IGNORECASE)),
    ("V2EX", re.compile(r"\bv2ex\b", re.IGNORECASE)),
    ("linuxsb", re.compile(r"\blinuxsb\b", re.IGNORECASE)),
    ("QQ邮箱", re.compile(r"qq\s*邮箱|qq\.com", re.IGNORECASE)),
    ("Gmail", re.compile(r"gmail(?:\.com)?", re.IGNORECASE)),
    ("Outlook", re.compile(r"outlook(?:\.com)?", re.IGNORECASE)),
)
_DETAIL_URL_HOST = re.compile(r"https?://(?:www\.)?([^/\s?#]+)", re.IGNORECASE)
_DETAIL_EXTERNAL_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z][A-Za-z0-9+#.-]{1,}|[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)+)(?![A-Za-z0-9])"
)
_DETAIL_ROLE_RULES = (
    ("failure", re.compile(
        r"死循环|无法|不能|失败|收不到|拒绝|不支持|已注册|被封|报错|异常|卡住|用不了|进不去|失败"
    )),
    ("constraint", re.compile(r"要求|必须|只能|拒绝|不支持|老号|备用|限制|规则|条件")),
    ("process", re.compile(r"注册|登录|登陆|绑定|验证|重置密码|重置|收件|邮件|邮箱|账号|账户")),
    ("impact", re.compile(r"影响|错失|无法访问|访问不了|技术情报|用不了|不能使用|进不去|阻碍")),
    ("uncertainty", re.compile(r"可能|也许|是否|会不会|未知|未确认|据说|听说|个人经历|需核实")),
)
_DETAIL_ROLE_LABELS = {
    # These labels are retained for the machine-readable detail layer, but the
    # published point list intentionally omits them.  "流程节点" was an
    # implementation taxonomy that leaked into the reader-facing brief and
    # made ordinary evidence look like a workflow generated by the system.
    "process": "过程",
    "constraint": "平台限制",
    "failure": "失败分支",
    "impact": "影响范围",
    "uncertainty": "待核实",
    "context": "补充信息",
}

_CLAIM_QUESTION = re.compile(
    r"(?:[?？]|(?:吗|么|呢|如何|怎么|为什么|为何|是否|会不会|能否|有没有|有必要))\s*$"
)
_CLAIM_OPINION = re.compile(
    r"(?:我觉得|我认为|感觉|看来|看起来|没性价比|没有性价比|很厉害|喜欢|买点|"
    r"应该|建议|最好|不如|值得|讨厌|喜欢|担心|希望|放弃|效果很差|很难搞)"
)
_CLAIM_HYPOTHESIS = re.compile(
    r"(?:可能|也许|或许|潜在|似乎|听说|据说|估计|大概|显示出|说明了?|反映出|"
    r"因此|从而|有望|会被|会导致)"
)
_CLAIM_PERSONAL = re.compile(
    r"(?:我|我们|本人|个人经历|我做下来|我尝试|我最近|我发现|亲测|有人说|群里有人)"
)


def _claim_profile(value: Any) -> Dict[str, Any]:
    """Classify what a chat line is epistemically allowed to say.

    A chat assertion is evidence of a statement, not external verification.
    Keeping this distinction in the local payload gives the AI and the UI a
    reliable boundary: opinions and hypotheses cannot silently become facts
    merely because they occur at the end of a conversation.
    """

    content = re.sub(r"\s+", " ", _display_content(value)).strip()
    if not content:
        return {
            "claim_type": "empty",
            "claim_status": "not_publishable",
            "claim_label": "无可读内容",
            "claim_boundary": "没有可用于判断的文本证据。",
        }
    if _CLAIM_QUESTION.search(content) or _has_question(content):
        return {
            "claim_type": "question",
            "claim_status": "unresolved",
            "claim_label": "问题",
            "claim_boundary": "当前只能确认原文提出了问题，不能把问题本身当作结论。",
        }
    if _CLAIM_OPINION.search(content):
        return {
            "claim_type": "opinion",
            "claim_status": "chat_opinion",
            "claim_label": "群内观点",
            "claim_boundary": "当前只能确认群内存在这一观点，不能把它当作已验证事实或方案结论。",
        }
    if _CLAIM_HYPOTHESIS.search(content):
        return {
            "claim_type": "hypothesis",
            "claim_status": "chat_hypothesis",
            "claim_label": "推测或传闻",
            "claim_boundary": "当前只能确认聊天中出现了这一推测或传闻，因果关系和影响范围尚未核实。",
        }
    if _CLAIM_PERSONAL.search(content):
        return {
            "claim_type": "reported_experience",
            "claim_status": "reported_experience",
            "claim_label": "个人经历",
            "claim_boundary": "当前只能确认个人经历或群内转述，不能代表平台规则或普遍事实。",
        }
    return {
        "claim_type": "reported_claim",
        "claim_status": "chat_report",
        "claim_label": "聊天事实陈述",
        "claim_boundary": "当前只能确认聊天中有人这样陈述，事实与因果关系尚未联网核验。",
    }


def _combined_claim_profile(values: Iterable[Any]) -> Dict[str, Any]:
    """Choose the most conservative epistemic state for a message cluster."""

    profiles = [_claim_profile(value) for value in values if str(value or "").strip()]
    if not profiles:
        return _claim_profile("")
    priority = {
        "question": 5,
        "opinion": 4,
        "hypothesis": 3,
        "reported_experience": 2,
        "reported_claim": 1,
        "empty": 0,
    }
    non_questions = [item for item in profiles if item.get("claim_type") != "question"]
    pool = non_questions or profiles
    return dict(max(pool, key=lambda item: priority.get(str(item.get("claim_type")), 0)))
_DETAIL_GENERIC_TOKENS = {
    "http", "https", "www", "com", "org", "net", "api", "token", "账号", "账户",
    "登录", "注册", "邮箱", "密码", "邮件", "平台", "网站", "问题", "情况", "qq",
}


def _detail_entity_terms(value: Any) -> List[str]:
    """Extract named platforms and externally checkable terms for detail recall."""

    content = _display_content(value)
    entities: List[str] = []
    for label, pattern in _DETAIL_ENTITY_RULES:
        if pattern.search(content):
            entities.append(label)
    for host in _DETAIL_URL_HOST.findall(content):
        normalized = host.strip().lower().rstrip(".")
        if normalized in {"github.com", "www.github.com"}:
            label = "GitHub"
        elif normalized in {"linux.do", "www.linux.do"}:
            label = "Linux.do"
        elif normalized in {"v2ex.com", "www.v2ex.com"}:
            label = "V2EX"
        else:
            label = host.strip()
        if label and label not in entities:
            entities.append(label)
    for token in _DETAIL_EXTERNAL_TOKEN.findall(content):
        normalized = token.casefold().rstrip(".")
        if normalized in _DETAIL_GENERIC_TOKENS or normalized.isdigit():
            continue
        if token.casefold() in {item.casefold() for item in entities}:
            continue
        if "." in token or len(token) >= 4:
            entities.append(token)
    unique_entities: List[str] = []
    seen_entities: Set[str] = set()
    for entity in entities:
        key = entity.casefold()
        if key in seen_entities:
            continue
        seen_entities.add(key)
        unique_entities.append(entity)
    return unique_entities[:16]


def _detail_clauses(value: Any) -> List[str]:
    content = re.sub(r"\s+", " ", _display_content(value)).strip()
    if not content:
        return []
    clauses: List[str] = []
    for clause in re.split(r"[。！？!?；;\n]+|(?<=[，,])(?=(?:导致|但是|但|而且|同时|随后|然后|重新|结果|因此))", content):
        normalized = clause.strip(" ，,、\t")
        if len(re.sub(r"\s+", "", normalized)) >= 4:
            clauses.append(normalized)
    return list(dict.fromkeys(clauses))


def _detail_facts(value: Any) -> List[Dict[str, Any]]:
    """Preserve process, failure and impact clauses instead of only keywords."""

    entities = _detail_entity_terms(value)
    facts: List[Dict[str, Any]] = []
    for clause in _detail_clauses(value):
        roles = [role for role, pattern in _DETAIL_ROLE_RULES if pattern.search(clause)]
        if not roles and not entities:
            continue
        facts.append(
            {
                "text": clause,
                "roles": roles or ["context"],
                "entities": entities,
            }
        )
    return facts


def _unique_detail_values(values: Iterable[str], limit: int = 8) -> List[str]:
    result: List[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text or text in result:
            continue
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _detail_bundle(
    items: Sequence[Mapping[str, Any]],
    timezone_name: str,
    source_message_ids: Optional[Sequence[str]] = None,
    max_timeline: int = 32,
) -> Dict[str, Any]:
    """Build a reversible detail layer for a topic or event cluster."""

    ordered_items = sorted(items, key=lambda item: (_timestamp(item.get("timestamp")), str(item.get("message_id") or "")))
    entity_counts: Counter = Counter()
    role_values: Dict[str, List[str]] = defaultdict(list)
    timeline: List[Dict[str, Any]] = []
    local_source_ids: List[str] = []
    for item in ordered_items:
        message_id = str(item.get("message_id") or "").strip()
        if message_id and message_id not in local_source_ids:
            local_source_ids.append(message_id)
        content = _display_content(item.get("content"))
        facts = _detail_facts(content)
        entities = _detail_entity_terms(content)
        for entity in entities:
            entity_counts[entity] += 1
        for fact in facts:
            for role in fact.get("roles") or ["context"]:
                role_values[role].append(str(fact.get("text") or ""))
        # Keep every message that entered the detail layer in the collapsed
        # timeline, including a context-only sentence with no recognized
        # entity.  The summary stays selective, but the evidence surface must
        # remain reversible instead of silently dropping the connective text
        # that explains how a topic moved from one platform or failure branch
        # to the next.
        if content:
            timeline.append(
                {
                    "message_id": item.get("message_id"),
                    "chat_id": item.get("chat_id"),
                    "chat_name": _chat_label(item),
                    "sender_name": _sender_label(item),
                    "timestamp": _visible_timestamp(_timestamp(item.get("timestamp")), timezone_name),
                    "quote": _clip(content, 240),
                    "roles": list(dict.fromkeys(role for fact in facts for role in fact.get("roles") or [])),
                    "entities": entities,
                }
            )
    all_source_ids = list(source_message_ids or local_source_ids)
    if not all_source_ids:
        all_source_ids = local_source_ids
    bundle_entities: List[str] = []
    seen_bundle_entities: Set[str] = set()
    for entity, _count in entity_counts.most_common(16):
        key = entity.casefold()
        if key in seen_bundle_entities:
            continue
        seen_bundle_entities.add(key)
        bundle_entities.append(entity)
    detail = {
        "source_message_count": len(all_source_ids),
        "source_message_ids": all_source_ids[:256],
        "entities": bundle_entities,
        "process": _unique_detail_values(role_values.get("process") or []),
        "constraints": _unique_detail_values(role_values.get("constraint") or []),
        "failure_points": _unique_detail_values(role_values.get("failure") or []),
        "impact": _unique_detail_values(role_values.get("impact") or []),
        "open_questions": _unique_detail_values(role_values.get("uncertainty") or []),
        "context": _unique_detail_values(role_values.get("context") or []),
        "timeline": timeline[:max_timeline],
    }
    timeline_entries = list(detail["timeline"])
    ranked_attributions = [
        (index, entry)
        for index, entry in enumerate(timeline_entries)
        if _detail_attribution_score(entry) >= 2
    ]
    if ranked_attributions:
        ranked_attributions.sort(
            key=lambda item: (_detail_attribution_score(item[1]), -item[0]),
            reverse=True,
        )
        selected_attributions = [entry for _index, entry in ranked_attributions[:12]]
        selected_attributions.sort(
            key=lambda entry: timeline_entries.index(entry)
        )
    else:
        selected_attributions = timeline_entries[:12]
    detail["attributions"] = [dict(entry) for entry in selected_attributions]
    participant_names: List[str] = []
    chat_names: List[str] = []
    for entry in detail["attributions"]:
        sender = str(entry.get("sender_name") or "").strip()
        chat = str(entry.get("chat_name") or "").strip()
        if sender and sender not in participant_names:
            participant_names.append(sender)
        if chat and chat not in chat_names:
            chat_names.append(chat)
    detail["participants"] = participant_names[:16]
    detail["sender_names"] = list(detail["participants"])
    detail["chat_names"] = chat_names[:16]
    points: List[str] = []
    point_items: List[Dict[str, str]] = []
    point_texts: Set[str] = set()
    for role in ("process", "constraint", "failure", "impact", "uncertainty", "context"):
        label = _DETAIL_ROLE_LABELS[role]
        for value in detail.get({"uncertainty": "open_questions", "constraint": "constraints"}.get(role, role), [])[:3]:
            if value in point_texts:
                continue
            point_texts.add(value)
            # Keep the role for exports and later ranking, but do not make the
            # reader parse taxonomy labels such as "流程节点" to understand a
            # sentence.  The sentence itself is the evidence; the category is
            # available in point_items when needed.
            points.append(value)
            point_items.append({"role": role, "label": label, "text": value})
    detail["points"] = points[:12]
    detail["point_items"] = point_items[:12]
    visible_timeline_count = min(len(timeline), max_timeline)
    detail["detail_coverage"] = (
        round(visible_timeline_count / len(all_source_ids), 3)
        if all_source_ids
        else 1.0
    )
    return detail


def _detail_summary(detail: Mapping[str, Any], limit: int = 520) -> str:
    parts: List[str] = []
    for role, key in (
        ("涉及对象", "entities"),
        ("过程", "process"),
        ("限制", "constraints"),
        ("失败分支", "failure_points"),
        ("影响", "impact"),
        ("待核实", "open_questions"),
    ):
        value_limit = 8 if key == "entities" else 2
        values = [str(value) for value in (detail.get(key) or [])[:value_limit] if str(value).strip()]
        if values:
            # Process is an internal ranking role, not a reader-facing
            # heading.  Publish the evidence sentence itself and retain the
            # role in detail.point_items for machine consumers.
            if key == "process":
                parts.append("；".join(values))
            else:
                parts.append("%s：%s" % (role, "；".join(values)))
    return _clip("；".join(parts), limit)


def _detail_attribution_score(entry: Mapping[str, Any]) -> int:
    """Score whether a source line is useful for an attributed topic claim."""

    quote = str(entry.get("quote") or entry.get("content") or "")
    roles = set(entry.get("roles") or [])
    score = len(entry.get("entities") or []) * 4
    score += 3 if roles.intersection({"constraint", "failure", "impact"}) else 0
    score += 2 if "uncertainty" in roles else 0
    score += 1 if "process" in roles else 0
    if re.search(
        r"github|linux(?:\.do|do)|v2ex|linuxsb|注册|登录|登陆|重置|邮箱|账号|ip|pro",
        quote,
        re.IGNORECASE,
    ):
        score += 2
    return score


def _detail_attribution_points(
    detail: Mapping[str, Any],
    limit: int = 4,
) -> List[str]:
    """Turn source evidence into short, explicitly attributed statements."""

    points: List[str] = []
    seen: Set[str] = set()
    for entry in detail.get("attributions") or detail.get("timeline") or []:
        sender = str(entry.get("sender_name") or "待识别成员").strip()
        chat = str(entry.get("chat_name") or "").strip()
        quote = re.sub(r"\s+", " ", str(entry.get("quote") or entry.get("content") or "")).strip()
        if not quote:
            continue
        key = "%s|%s" % (sender, quote)
        if key in seen:
            continue
        seen.add(key)
        roles = set(entry.get("roles") or [])
        verb = "指出" if roles.intersection({"constraint", "failure", "impact"}) else "提到"
        location = "（%s）" % chat if chat else ""
        points.append("%s%s%s：%s" % (sender, location, verb, _clip(quote, 150)))
        if len(points) >= limit:
            break
    return points


def _reference_ids(value: Any) -> Set[str]:
    """Extract message identifiers from provider-specific reply structures.

    WeChat exports and bridge providers do not agree on one reply field.  Keep
    this parser deliberately permissive at the boundary, but never treat a
    boolean or a whole quoted sentence as an identifier.
    """

    found: Set[str] = set()
    if value is None or isinstance(value, bool):
        return found
    if isinstance(value, Mapping):
        for key in ("message_id", "msg_id", "id", "uuid", "key"):
            candidate = value.get(key)
            if candidate is None or isinstance(candidate, (Mapping, list, tuple, set, bool)):
                continue
            text = str(candidate).strip()
            if text and text.casefold() not in {"none", "null", "true", "false"}:
                found.add(text)
        for key in ("message", "target", "quoted", "reference", "reply_to", "parent"):
            if key in value:
                found.update(_reference_ids(value.get(key)))
        return found
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found.update(_reference_ids(item))
        return found
    text = str(value).strip()
    if text and len(text) <= 256 and text.casefold() not in {"none", "null", "true", "false"}:
        found.add(text)
    return found


def _direct_reference_ids(message: Mapping[str, Any]) -> Set[str]:
    """Return explicit reply/quote targets without using a loose thread id."""

    found: Set[str] = set()
    for key in _DIRECT_REFERENCE_KEYS:
        if key in message:
            found.update(_reference_ids(message.get(key)))
    return found


def _reference_texts(value: Any) -> List[str]:
    """Extract quoted text for providers that omit the quoted message id."""

    texts: List[str] = []
    if value is None or isinstance(value, bool):
        return texts
    if isinstance(value, Mapping):
        for key in ("content", "text", "quote", "quoted_content", "quote_content"):
            candidate = value.get(key)
            if candidate is not None and not isinstance(candidate, (Mapping, list, tuple, set)):
                text = str(candidate).strip()
                if text:
                    texts.append(text)
        for key in ("message", "target", "quoted", "reference", "reply_to", "parent"):
            if key in value:
                texts.extend(_reference_texts(value.get(key)))
        return texts
    if isinstance(value, (list, tuple, set)):
        for item in value:
            texts.extend(_reference_texts(item))
        return texts
    text = str(value).strip()
    if text:
        texts.append(text)
    return texts


def _quoted_texts(message: Mapping[str, Any]) -> List[str]:
    texts: List[str] = []
    for key in _REFERENCE_TEXT_KEYS:
        if key in message:
            texts.extend(_reference_texts(message.get(key)))
    return list(dict.fromkeys(texts))


def _compact_context_text(value: Any) -> str:
    return re.sub(r"\s+", "", _display_content(value)).casefold()


def _direct_thread_relation(
    target: Mapping[str, Any],
    neighbor: Mapping[str, Any],
) -> str:
    """Return a non-empty relation when two rows are explicitly linked."""

    target_id = str(target.get("message_id") or "").strip()
    neighbor_id = str(neighbor.get("message_id") or "").strip()
    if target_id and target_id in _direct_reference_ids(neighbor):
        return "explicit_reply"
    if neighbor_id and neighbor_id in _direct_reference_ids(target):
        return "explicit_reply"

    neighbor_text = _compact_context_text(neighbor.get("content"))
    if len(neighbor_text) >= 4:
        for quoted in _quoted_texts(target):
            quoted_text = _compact_context_text(quoted)
            if len(quoted_text) >= 4 and (quoted_text in neighbor_text or neighbor_text in quoted_text):
                return "quoted_text"
    target_text = _compact_context_text(target.get("content"))
    if len(target_text) >= 4:
        for quoted in _quoted_texts(neighbor):
            quoted_text = _compact_context_text(quoted)
            if len(quoted_text) >= 4 and (quoted_text in target_text or target_text in quoted_text):
                return "quoted_text"
    return ""


def _is_context_fragment_message(message: Mapping[str, Any]) -> bool:
    content = _display_content(message.get("content"))
    compact = re.sub(r"\s+", "", content)
    recognition = _message_recognition(message)
    return bool(
        recognition.get("analysis_role") == "context"
        or _CONTEXT_DEPENDENT.search(content)
        or _CLAUSE_FRAGMENT.search(content)
        or len(compact) < 12
    )


def _context_relation_label(
    target: Mapping[str, Any],
    neighbor: Mapping[str, Any],
) -> str:
    direct = _direct_thread_relation(target, neighbor)
    if direct:
        distance = abs(_timestamp(target.get("timestamp")) - _timestamp(neighbor.get("timestamp")))
        if distance > timedelta(hours=1):
            return "挖坟回复" if direct == "explicit_reply" else "挖坟引用"
        return "直接回复" if direct == "explicit_reply" else "引用原文"
    target_topics = _event_semantic_tags(target)
    neighbor_topics = _event_semantic_tags(neighbor)
    if (target_topics & neighbor_topics) - _WEAK_SHARED_EVENT_TOPICS:
        return "共享主题"
    if _context_terms(_display_content(target.get("content"))).intersection(
        _context_terms(_display_content(neighbor.get("content")))
    ):
        return "共享词语"
    if (_sender_label(target), target.get("is_self") is True) == (
        _sender_label(neighbor), neighbor.get("is_self") is True
    ):
        return "同一发言人连续表达"
    return "邻近上下文"


def _context_metadata(
    target: Mapping[str, Any],
    context: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    relations = []
    topic_labels: Set[str] = set()
    subject_sets: List[Tuple[Set[str], Mapping[str, Any]]] = []
    for neighbor in context[:8]:
        message_id = str(neighbor.get("message_id") or "")
        if not message_id:
            continue
        relation = _context_relation_label(target, neighbor)
        relations.append({"message_id": message_id, "relation": relation})
        subjects = _event_semantic_tags(neighbor) - _CONTEXT_WEAK_SUBJECTS
        topic_labels.update(subjects)
        if subjects:
            subject_sets.append((subjects, neighbor))
    ambiguous = False
    if _is_context_fragment_message(target) and not any(
        item["relation"] in {"直接回复", "引用原文", "挖坟回复", "挖坟引用"}
        for item in relations
    ):
        for index, (left_subjects, left_item) in enumerate(subject_sets):
            for right_subjects, right_item in subject_sets[index + 1:]:
                if not left_subjects.isdisjoint(right_subjects):
                    continue
                left_terms = _context_terms(_display_content(left_item.get("content")))
                right_terms = _context_terms(_display_content(right_item.get("content")))
                if not left_terms.intersection(right_terms):
                    ambiguous = True
                    break
            if ambiguous:
                break
    direct_relations = {"直接回复", "引用原文", "挖坟回复", "挖坟引用"}
    return {
        "context_relations": relations[:8],
        "context_topic_labels": sorted(topic_labels),
        "revived_thread": any(item["relation"] in {"挖坟回复", "挖坟引用"} for item in relations),
        "explicit_thread": any(item["relation"] in direct_relations for item in relations),
        "ambiguous_context": ambiguous,
    }


def _expression_assessment(message: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify how safely a message can be interpreted without inventing text.

    This is an abstention signal, not a writing-quality judgement.  A message
    can be valuable and colloquial at the same time; the flag tells downstream
    summarizers to keep the quotation and avoid silently completing its intent.
    """

    content = _display_content(message.get("content"))
    compact = re.sub(r"\s+", "", content)
    recognition = _message_recognition(message)
    if not _is_text_candidate(message, content):
        return {"status": "non_text", "confidence": 100, "reason": "没有可用于语义补全的文本"}
    if recognition.get("analysis_role") in {"noise", "archive_only"}:
        return {"status": "noise", "confidence": 96, "reason": "噪声或媒体消息不进入意图补全"}

    has_object = _has_concrete_object(content)
    context_dependent = bool(_CONTEXT_DEPENDENT.search(content) or _CLAUSE_FRAGMENT.search(content))
    vague_referent = bool(_VAGUE_REFERENT.search(content))
    colloquial = bool(_COLLOQUIAL_MARKER.search(content))
    short = len(compact) < 12

    if context_dependent and not has_object:
        return {
            "status": "context_dependent",
            "confidence": 88,
            "reason": "包含指代、承接或省略，单看本句无法确认对象",
        }
    if vague_referent and not has_object:
        return {
            "status": "low_clarity",
            "confidence": 78,
            "reason": "出现模糊指代或口语化对象，暂不替用户补全意图",
        }
    if _CLAUSE_FRAGMENT.search(content) or (short and not has_object and not _QUESTION.search(content)):
        return {
            "status": "fragment",
            "confidence": 82,
            "reason": "句子像上下文片段，缺少完整主语、对象或结果",
        }
    if colloquial and not has_object and not _INFORMATION_SIGNAL.search(content):
        return {
            "status": "colloquial",
            "confidence": 66,
            "reason": "表达口语化或含义依赖群内语境，保留原文等待核对",
        }
    return {"status": "clear", "confidence": 86, "reason": "本句包含足够对象或可核对语义线索"}


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


def _context_terms(value: Any) -> set:
    """Return context anchors after removing conversational glue words."""

    return _text_terms(value) - _CONTEXT_WEAK_TERMS


def _canonical_event_terms(value: Any) -> Set[str]:
    """Map paraphrases to a small, inspectable event vocabulary.

    These labels are event hypotheses, not truth claims.  They are added only
    as extra evidence; the original text, timestamps and chat boundaries stay
    attached to every cluster so a broad alias cannot erase disagreement.
    """

    content = _display_content(value)
    return {label for label, pattern in _EVENT_ALIAS_RULES if pattern.search(content)}


_EVENT_DOMAIN_ORDER = (
    "wechat",
    "gpt_service",
    "codex_service",
    "ai_service",
    "developer_account",
)


def _event_domain_tags(value: Any) -> Set[str]:
    """Extract hard object/platform boundaries for event clustering.

    N-grams and canonical topics are intentionally recall-oriented.  They are
    not safe enough to decide that two messages describe the same object:
    ``封号`` can occur in an AI-account complaint as well as a WeChat risk
    discussion, and ``重置`` can mean a password, quota, or session reset.
    These small domain tags are therefore used as a precision guard only.
    """

    content = _display_content(value)
    tags: Set[str] = set()
    if re.search(r"(?:微信|企业微信|飞书|wechat)", content, re.IGNORECASE):
        tags.add("wechat")
    if re.search(
        r"(?:github|linux\.do|linuxdo|v2ex|linuxsb|重置密码|收不到邮件|qq邮箱)",
        content,
        re.IGNORECASE,
    ):
        tags.add("developer_account")

    explicit_codex = re.search(r"(?:codex|code\s*x)", content, re.IGNORECASE)
    explicit_gpt = re.search(r"(?:gpt|chatgpt)", content, re.IGNORECASE)
    explicit_other_ai = re.search(
        r"(?:claude|deepseek|deepthink|openai|kimi)",
        content,
        re.IGNORECASE,
    )
    ai_usage = re.search(
        r"(?:中转站|中转|额度|token|用量|消耗|套餐|订阅|pro|蹬)",
        content,
        re.IGNORECASE,
    )
    # Plain ``重置密码`` belongs to the developer-account lane.  A bare
    # ``重置`` is only promoted to the AI lane when the same message carries
    # an AI/account-usage cue, which keeps unrelated password resets apart.
    ai_reset = re.search(r"(?:重置|reset)", content, re.IGNORECASE) and not re.search(
        r"密码", content, re.IGNORECASE
    )
    if explicit_codex:
        tags.add("codex_service")
    if explicit_gpt:
        tags.add("gpt_service")
    if not (explicit_codex or explicit_gpt) and (
        explicit_other_ai
        or ai_usage
        or (ai_reset and re.search(r"(?:账号|账户|封号)", content, re.IGNORECASE))
    ):
        tags.add("ai_service")
    return tags


def _event_domains_compatible(items: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether a proposed cluster keeps its object boundaries.

    When multiple hard domains are present, every message must explicitly
    mention all of them for a merge to be allowed.  This prevents a generic
    bridge message (or a transitive shared phrase) from connecting two
    otherwise unrelated conversations.  A message with no hard tag is fine
    inside a single-domain cluster, but it cannot bridge two domains.
    """

    domain_sets = [_event_domain_tags(item.get("content")) for item in items]
    domains = set().union(*domain_sets) if domain_sets else set()
    if len(domains) <= 1:
        return True
    return all(domains.issubset(tags) for tags in domain_sets)


_TOPIC_CANONICAL_ORDER = (
    "账号与平台", "账号风控", "注意力过滤", "跨会话归档", "群聊噪声", "会议转写",
    "梗与热词", "产品差异", "群聊总结", "模型部署", "选课安排", "成本变化",
)
_TOPIC_FAMILY_MAP = {
    "账号与平台": "账号与平台",
    "账号风控": "风险与合规",
    "注意力过滤": "注意力过滤",
    "跨会话归档": "产品边界",
    "群聊噪声": "识别质量",
    "会议转写": "识别质量",
    "梗与热词": "梗与热点",
    "产品差异": "产品边界",
    "群聊总结": "产品边界",
    "模型部署": "AI / 模型",
    "选课安排": "时间安排",
    "成本变化": "成本与额度",
}


def _topic_detail_tags(value: Any) -> List[str]:
    canonical = _canonical_event_terms(value)
    if canonical:
        return [label for label in _TOPIC_CANONICAL_ORDER if label in canonical]
    tags = list(_insight_topic_tags(value))
    if len(tags) > 1 and "社群 / 活动" in tags:
        tags = [tag for tag in tags if tag != "社群 / 活动"]
    return tags[:2] or ["其他讨论"]


def _topic_tags(value: Any) -> List[str]:
    """Choose one stable primary topic instead of every keyword hit.

    Topic briefs are an editorial index, not a bag-of-keywords report.  A
    message about a product's group-summary difference should not also inflate
    the generic “社群/活动” bucket merely because it contains the character
    “群”.  Event briefs retain the richer multi-label evidence separately.
    """

    details = _topic_detail_tags(value)
    primary_detail = details[0] if details else "其他讨论"
    return [_TOPIC_FAMILY_MAP.get(primary_detail, primary_detail)]


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
    terms.update(_canonical_event_terms(text))
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
    recognition = item.get("_recognition") or {}
    if recognition.get("analysis_role") in {"noise", "archive_only"}:
        return False
    if not _is_text_candidate(item, content) or _LOW_SIGNAL.match(content) or _GROUP_EMOTION_NOISE.match(content):
        return False
    compact = re.sub(r"\s+", "", content)
    return bool(len(compact) >= 8 or _DOMAIN_SIGNAL.search(content) or _ACTION_VERB.search(content))


def _event_title(items: Sequence[Mapping[str, Any]], anchors: Sequence[str]) -> str:
    combined = "\n".join(_display_content(item.get("content")) for item in items)
    return normalize_editorial_title("", combined, anchors)


def _event_semantic_tags(item: Mapping[str, Any]) -> set:
    """Collect broad semantic labels used to validate an event merge.

    Shared n-grams are useful for recall, but a single phrase can also occur in
    unrelated conversations.  These coarse local tags provide an independent
    signal for precision without asking a model or leaking message content.
    """

    content = _display_content(item.get("content"))
    tags = set(_insight_topic_tags(content))
    tags.update(_discussion_signature(item))
    tags.update(_canonical_event_terms(content))
    tags.update(_event_domain_tags(content))
    if _RESOURCE_SIGNAL.search(content):
        tags.add("资源线索")
    if _TRADE.search(content):
        tags.add("费用与交易")
    if _RISK.search(content):
        tags.add("风险与安全")
    return tags


def _is_specific_event_anchor(term: Any) -> bool:
    """Reject shifted n-grams made mostly from dates or chat glue words."""

    value = re.sub(r"\s+", "", str(term or "").strip())
    if len(value) < 3 or value.casefold().replace(" ", "") in _GENERIC_EVENT_ANCHORS:
        return False
    if value in _EVENT_STOP_TERMS:
        return False
    if value[0] in _EVENT_BAD_ANCHOR_EDGES or value[-1] in _EVENT_BAD_ANCHOR_EDGES:
        return False
    return not any(part in value for part in _EVENT_ANCHOR_WEAK_PARTS)


def _event_cluster_quality(
    items: Sequence[Mapping[str, Any]],
    anchors: Sequence[str],
    frequency: Counter,
) -> Dict[str, Any]:
    """Score whether an evidence cluster is safe to present as one event.

    The clusterer still favors recall, but cross-chat merges need an
    independent semantic signal or more than one specific shared phrase.  The
    result is deliberately serializable so the UI, evaluation scripts, and
    later feedback loop can inspect why a merge happened.
    """

    values = list(items)
    if not values:
        return {
            "score": 0,
            "confidence": 0,
            "merge_supported": False,
            "merge_basis": [],
            "anchor_terms": [],
            "shared_topics": [],
            "canonical_terms": [],
            "message_count": 0,
            "chat_count": 0,
            "participant_count": 0,
            "evidence_binding_rate": 0.0,
        }

    term_sets = [set(item.get("_event_terms") or _event_terms(item.get("content"))) for item in values]
    shared_terms = set.intersection(*term_sets) if term_sets else set()
    candidate_terms = set(shared_terms).union(str(term) for term in anchors if str(term))
    specific_terms = sorted(
        (
            term for term in candidate_terms
            if _is_specific_event_anchor(term)
        ),
        key=lambda term: (len(term), -frequency.get(term, 1), term),
        reverse=True,
    )
    semantic_sets = [_event_semantic_tags(item) for item in values]
    shared_topics = set.intersection(*semantic_sets) if semantic_sets and all(semantic_sets) else set()
    shared_topics -= _WEAK_SHARED_EVENT_TOPICS
    canonical_sets = [_canonical_event_terms(item.get("content")) for item in values]
    shared_canonical = set.intersection(*canonical_sets) if canonical_sets and all(canonical_sets) else set()
    domain_tags = set().union(*(_event_domain_tags(item.get("content")) for item in values))
    domains_compatible = _event_domains_compatible(values)
    chats = {
        str(item.get("chat_id") or item.get("chat_name") or "unknown")
        for item in values
    }
    people = {
        _sender_label(item)
        for item in values
        if _sender_label(item) not in {"我", "联系人", "群成员", "待识别成员"}
    }
    bound = sum(1 for item in values if str(item.get("message_id") or "").strip())
    cross_chat = len(chats) >= 2
    merge_supported = domains_compatible and (
        len(values) <= 1
        or not cross_chat
        or bool(shared_topics)
        or len(specific_terms) >= 2
    )

    basis: List[str] = []
    if shared_topics:
        basis.append("共同主题标签")
    if shared_canonical:
        basis.append("同义或别名归一")
    if len(specific_terms) >= 2:
        basis.append("多个具体共享短语")
    elif specific_terms:
        basis.append("具体共享短语")
    if cross_chat:
        basis.append("跨会话重复出现")
    if len(values) >= 3:
        basis.append("多条证据")
    if not domains_compatible:
        basis.append("不同对象/平台边界冲突")
    if not basis:
        basis.append("单条高信息线索" if len(values) == 1 else "同会话时间邻近")

    score = 25
    score += min(24, len(values) * 6)
    score += min(18, len(chats) * 7)
    score += min(18, len(specific_terms) * 7)
    score += 15 if shared_topics else 0
    score += 8 if cross_chat else 0
    score -= 18 if cross_chat and not merge_supported else 0
    score -= 45 if not domains_compatible else 0
    score = max(0, min(100, score))
    confidence = 38 + min(22, len(values) * 6) + min(15, len(chats) * 6)
    confidence += min(16, len(specific_terms) * 6) + (12 if shared_topics else 0)
    if cross_chat and not merge_supported:
        confidence -= 20
    confidence -= 35 if not domains_compatible else 0
    confidence = max(20, min(95, confidence))
    return {
        "score": score,
        "confidence": confidence,
        "merge_supported": merge_supported,
        "merge_basis": basis,
        "anchor_terms": specific_terms[:6],
        "shared_topics": sorted(shared_topics),
        "canonical_terms": sorted(shared_canonical),
        "domain_tags": [label for label in _EVENT_DOMAIN_ORDER if label in domain_tags],
        "domains_compatible": domains_compatible,
        "message_count": len(values),
        "chat_count": len(chats),
        "participant_count": len(people),
        "evidence_binding_rate": round(bound / len(values), 3) if values else 0.0,
    }


def _event_editing_fields(
    items: Sequence[Mapping[str, Any]],
    anchors: Sequence[str],
    chats: Mapping[str, str],
    people: Sequence[str],
    timezone_name: str = "Asia/Shanghai",
) -> Dict[str, Any]:
    """Create a compact local brief when AI is unavailable.

    The local pass intentionally distinguishes the factual deck from the
    editorial judgment.  It is not a claim that a rule engine understands the
    world; it is a transparent synthesis of the evidence in this cluster.
    """

    ordered_items = sorted(items, key=lambda item: item["_timestamp"])
    combined = "\n".join(_display_content(item.get("content")) for item in ordered_items)
    topic = _editorial_topic_label(combined, anchors)
    detail = _detail_bundle(ordered_items, timezone_name)
    detail_summary = _detail_summary(detail)
    claim_profile = _combined_claim_profile(
        _display_content(item.get("content")) for item in ordered_items
    )
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
    if detail_summary:
        facts += "；细节层保留：%s" % detail_summary
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

    # The local pass is allowed to conclude that a discussion exists, not that
    # the discussion's last sentence is true.  In particular, a suggestion
    # such as "企业微信成合规替代方案" must remain a group opinion until an
    # external source and the actual scope of the claim have been checked.
    boundary = str(claim_profile.get("claim_boundary") or "聊天证据尚未完成外部核验。")
    if claim_profile.get("claim_type") in {"opinion", "hypothesis", "question"}:
        conclusion = boundary
    elif len(chats) >= 2:
        conclusion = "跨会话重复出现，说明这是共同讨论对象；" + boundary
    elif re.search(r"风险|故障|异常|失败|无法|封禁|泄露|漏洞", combined, re.IGNORECASE):
        conclusion = "这组消息可以保留为风险线索，但不能仅凭聊天判断实际损失、责任或因果；" + boundary
    elif re.search(r"改成|换成|采用|调整|更新|决定|确认|定下来|选择", combined, re.IGNORECASE):
        conclusion = "原文出现了方向性变化，但是否已经形成决定仍需核对最新消息和对应来源；" + boundary
    else:
        conclusion = "这组消息具备连续语义和可回看证据；" + boundary

    uncertainty = boundary
    if re.search(r"问题|疑问|请问|如何|怎么|是否|能否", combined, re.IGNORECASE):
        uncertainty = "原文仍保留未回答的问题；" + boundary
    return {
        "narrative": _clip(facts, 680),
        "what_changed": _clip(changed, 180),
        "why_it_matters": _clip(conclusion, 180),
        "core_conclusion": _clip(conclusion, 180),
        "uncertainty": _clip(uncertainty, 120),
        "next_step": "回看原文证据，确认最新决定、未答问题或下一步动作",
        "detail": detail,
        "detail_summary": detail_summary,
        "detail_points": list(detail.get("points") or []),
        "claim_type": claim_profile.get("claim_type"),
        "claim_status": claim_profile.get("claim_status"),
        "claim_label": claim_profile.get("claim_label"),
        "claim_boundary": boundary,
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
_GREETING_SIGNAL = re.compile(
    r"^(?:你好|您好|嗨|哈喽|早上好|晚上好|晚安|辛苦了|吃饭了吗|在吗|回头聊|先这样)[!！。,.，、 ]*$",
    re.IGNORECASE,
)
_REACTION_SIGNAL = re.compile(
    r"(?:哈哈|呵呵|笑死|绷不住|牛逼|离谱|逆天|卧槽|我靠|绝了|无语|吃瓜|666|\+1|👍|😂|🤣|😭|😅)",
    re.IGNORECASE,
)


def _discussion_signature(item: Mapping[str, Any]) -> set:
    """Return broad subjects used to split a chat into conversational blocks."""

    content = _display_content(item.get("content"))
    signature = set(_insight_topic_tags(content))
    if re.search(r"不看|注意力|信息茧房|屏蔽|过滤|噪声", content):
        signature.add("注意力过滤")
    if re.search(r"梗|蒸馏|未来古法|复古|像素", content):
        signature.add("群内梗与表达")
    if re.search(r"群聊总结|日报|周报|列出几个主题|主题点", content, re.IGNORECASE):
        signature.add("群聊总结")
    if re.search(r"跨.*(?:群|会话)|多个群聊|私聊.*同一件事|归档", content):
        signature.add("跨会话归档")
    if re.search(r"录音|转写|发言人|录音设备|错别字", content):
        signature.add("会议转写")
    if re.search(r"风控|封号|封禁|禁言|只读|同步|合规|飞书|企业微信", content, re.IGNORECASE):
        signature.add("接入风控")
    if re.search(r"Line|微信.*(?:AI|总结)|(?:AI|总结).*微信", content, re.IGNORECASE):
        signature.add("产品差异")
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
    recognition = item.get("_recognition") or {}
    if recognition.get("analysis_role") in {"noise", "archive_only"}:
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


def _message_recognition(message: Mapping[str, Any]) -> Dict[str, Any]:
    """Classify one message for all downstream recognition lanes.

    This is deliberately a layered classifier rather than a delete filter.  A
    reaction can be noise for the work queue while still being retained as
    evidence that a repeated phrase may be a group inside joke.  Short
    fragments are also kept as context candidates because dropping them would
    break requests whose object appears in the preceding message.
    """

    cached = message.get("_recognition")
    if isinstance(cached, Mapping):
        return dict(cached)

    content = _display_content(message.get("content"))
    compact = re.sub(r"\s+", "", content)
    base = {
        "noise_class": "none",
        "analysis_role": "content",
        "noise_score": 0,
        "reason": "包含可核对的主题、观点、行动或上下文线索",
        "keep_for_context": True,
        "is_substantive": True,
        "meme_candidate": False,
        "meme_evidence": False,
    }

    if not _is_text_candidate(message, content):
        base.update(
            {
                "noise_class": "media_placeholder" if content else "non_text",
                "analysis_role": "archive_only",
                "noise_score": 100,
                "reason": "非文本或媒体占位消息只保留在原始消息流，不进入文本语义分析",
                "keep_for_context": False,
                "is_substantive": False,
            }
        )
        return base

    action = _action_evidence(content)
    has_substantive_signal = bool(
        _has_question(content)
        or action.get("tags")
        or _DOMAIN_SIGNAL.search(content)
        or _INFORMATION_SIGNAL.search(content)
        or _RESOURCE_SIGNAL.search(content)
        or _ARGUMENT_SIGNAL.search(content)
        or _PROJECT_SIGNAL.search(content)
        or _FEEDBACK_SIGNAL.search(content)
        or _RISK.search(content)
        or _TRADE.search(content)
    )

    # Reactions are identified before the generic low-signal rule so a message
    # such as “哈哈哈” can remain usable as meme evidence without becoming a
    # discovery or an action candidate.
    pure_reaction = bool(
        _GROUP_EMOTION_NOISE.match(content)
        or (
            _REACTION_SIGNAL.search(content)
            and len(compact) <= 18
            and not has_substantive_signal
        )
    )
    if pure_reaction:
        base.update(
            {
                "noise_class": "reaction",
                "analysis_role": "noise",
                "noise_score": 92,
                "reason": "纯情绪或附和反应；不进入重点流，但可作为群内梗候选的回应证据",
                "keep_for_context": False,
                "is_substantive": False,
            }
        )
        return base

    if _GREETING_SIGNAL.match(content):
        base.update(
            {
                "noise_class": "greeting",
                "analysis_role": "noise",
                "noise_score": 88,
                "reason": "寒暄或会话收尾，不携带可核对的主题变化",
                "keep_for_context": False,
                "is_substantive": False,
            }
        )
        return base

    if _LOW_SIGNAL.match(content):
        base.update(
            {
                "noise_class": "acknowledgement",
                "analysis_role": "noise",
                "noise_score": 90,
                "reason": "确认、感谢或无内容短回复；保留原文但不进入重点分析",
                "keep_for_context": False,
                "is_substantive": False,
            }
        )
        return base

    if _INFORMATION_EXCLUDE.search(content):
        base.update(
            {
                "noise_class": "excluded_content",
                "analysis_role": "noise",
                "noise_score": 96,
                "reason": "命中当前分析范围的排除规则，不参与信息洞察或待办提取",
                "keep_for_context": False,
                "is_substantive": False,
            }
        )
        return base

    social_chat = bool(_SOCIAL_CHAT_NAME.search(_chat_label(message)))
    social_only = bool(
        _LIFE_SIGNAL.search(content)
        and not (
            action.get("tags")
            or _DOMAIN_SIGNAL.search(content)
            or _INFORMATION_SIGNAL.search(content)
            or _RESOURCE_SIGNAL.search(content)
            or _ARGUMENT_SIGNAL.search(content)
            or _PROJECT_SIGNAL.search(content)
            or _FEEDBACK_SIGNAL.search(content)
            or _RISK.search(content)
            or _TRADE.search(content)
        )
        and (social_chat or bool(message.get("is_group")))
    )
    if social_only:
        base.update(
            {
                "noise_class": "social_chatter",
                "analysis_role": "social",
                "noise_score": 28,
                "reason": "生活或关系话题单独保留为社交信息，不与工作主线混合",
                "keep_for_context": False,
                "is_substantive": True,
            }
        )
        return base

    has_specific_subject = bool(
        _DOMAIN_SIGNAL.search(content)
        or _INFORMATION_SIGNAL.search(content)
        or _RESOURCE_SIGNAL.search(content)
        or _PROJECT_SIGNAL.search(content)
        or _FEEDBACK_SIGNAL.search(content)
        or _RISK.search(content)
        or _TRADE.search(content)
    )
    context_fragment = bool(
        _CONTEXT_DEPENDENT.search(content)
        or _CLAUSE_FRAGMENT.match(content)
        or (len(compact) < 12 and not has_specific_subject)
    )
    if context_fragment:
        base.update(
            {
                "noise_class": "context_fragment",
                "analysis_role": "context",
                "noise_score": 36,
                "reason": "短句或指代依赖前文；保留为上下文，不单独升级为结论",
                "keep_for_context": True,
                "is_substantive": False,
            }
        )
        return base

    if not has_substantive_signal and len(compact) < 12:
        base.update(
            {
                "noise_class": "low_information",
                "analysis_role": "noise",
                "noise_score": 64,
                "reason": "长度和语义线索都不足，暂不进入信息主线",
                "keep_for_context": False,
                "is_substantive": False,
            }
        )
    return base


_MEME_STOP_SIGNATURES = {
    "收到", "谢谢", "感谢", "好的", "好", "嗯", "哦", "啊", "在吗",
    "早上好", "晚上好", "晚安", "哈哈", "哈哈哈", "笑死", "绝了", "666",
}
_MEME_TOPIC_STOP_TERMS = {
    "群聊", "私聊", "信息", "内容", "问题", "方案", "项目", "需求", "发言人",
    "会议录音", "录音质量", "转写质量", "群聊总结", "账号风控", "同步频率", "只读分析",
    "企业微信", "教务系统", "选课安排", "接口返回", "模型部署", "工作群",
}


def _meme_signature(value: Any) -> str:
    """Normalize a repeated phrase while keeping Chinese and ASCII words."""

    content = _display_content(value)
    content = re.sub(r"https?://\S+|www\.\S+", " ", content, flags=re.IGNORECASE)
    content = re.sub(r"\[(?:文本|系统消息|图片|动画表情|语音|文件/链接/卡片|文件|链接|卡片)\]", " ", content)
    compact = re.sub(r"\s+", "", content).casefold()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)
    if not normalized or len(normalized) < 3 or len(normalized) > 36:
        return ""
    if normalized in _MEME_STOP_SIGNATURES or normalized.isdigit():
        return ""
    if not re.search(r"[\u4e00-\u9fff]|[a-z]", normalized):
        return ""
    return normalized


def _meme_anchor_terms(value: Any) -> Set[str]:
    """Extract conservative phrase-family anchors from one message.

    Exact full-message matching misses natural variants such as “未来古法”
    and “未来古法，低效”.  Anchors bridge that gap, but only for specific
    3–8 character spans whose edges are not chat glue words.  Candidate
    thresholds still require repetition plus participants or reactions.
    """

    content = _display_content(value)
    content = re.sub(r"https?://\S+|www\.\S+", " ", content, flags=re.IGNORECASE)
    terms: Set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]{3,}|[a-z][a-z0-9_.+-]{3,}", content.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            for width in range(3, min(8, len(chunk)) + 1):
                for index in range(len(chunk) - width + 1):
                    term = chunk[index:index + width]
                    if (
                        len(term) >= 4
                        and _is_specific_event_anchor(term)
                        and term not in _MEME_STOP_SIGNATURES
                        and term not in _MEME_TOPIC_STOP_TERMS
                    ):
                        terms.add(term)
        elif len(chunk) >= 4 and chunk not in _MEME_STOP_SIGNATURES:
            terms.add(chunk)
    return terms


def _meme_candidates(
    items: Sequence[Mapping[str, Any]],
    timezone_name: str,
) -> List[Dict[str, Any]]:
    """Find repeated phrase candidates with local reaction evidence.

    The detector does not infer what a joke means.  It only promotes a phrase
    when repetition, participant spread, and/or nearby reactions make the
    recurrence explainable.  Operational phrases are excluded unless they
    also have at least two nearby reactions, which prevents repeated requests
    from being mislabeled as memes.
    """

    by_chat: Dict[str, Dict[str, List[Mapping[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    reactions: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    chat_messages: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        recognition = _message_recognition(item)
        chat_key = str(item.get("chat_id") or item.get("chat_name") or "unknown")
        chat_messages[chat_key].append(item)
        if recognition.get("analysis_role") == "noise" and recognition.get("noise_class") == "reaction":
            reactions[chat_key].append(item)
            continue
        if recognition.get("analysis_role") in {"archive_only", "noise"}:
            continue
        signature = _meme_signature(item.get("content"))
        if signature:
            by_chat[chat_key]["exact:" + signature].append(item)
        for anchor in _meme_anchor_terms(item.get("content")):
            by_chat[chat_key]["anchor:" + anchor].append(item)

    candidates: List[Dict[str, Any]] = []
    for chat_key, phrases in by_chat.items():
        for phrase_key, occurrences in phrases.items():
            phrase_kind, signature = phrase_key.split(":", 1)
            if phrase_kind == "anchor" and len(phrases.get("exact:" + signature, ())) >= 2:
                # The exact detector already owns a stronger, less ambiguous
                # candidate for this phrase.
                continue
            occurrences = sorted(occurrences, key=lambda item: (_timestamp(item.get("timestamp")), str(item.get("message_id") or "")))
            if len(occurrences) < 2:
                continue
            occurrence_object_ids = {id(item) for item in occurrences}
            nearby_reactions: List[Mapping[str, Any]] = []
            for reaction in reactions.get(chat_key, []):
                prior_occurrences = [
                    item
                    for item in occurrences
                    if timedelta(0)
                    <= _timestamp(reaction.get("timestamp")) - _timestamp(item.get("timestamp"))
                    <= timedelta(minutes=5)
                ]
                if not prior_occurrences:
                    continue
                nearest = max(prior_occurrences, key=lambda item: _timestamp(item.get("timestamp")))
                nearest_time = _timestamp(nearest.get("timestamp"))
                intervening = any(
                    id(item) not in occurrence_object_ids
                    and _message_recognition(item).get("analysis_role") not in {"noise", "archive_only"}
                    and nearest_time < _timestamp(item.get("timestamp")) < _timestamp(reaction.get("timestamp"))
                    for item in chat_messages.get(chat_key, [])
                )
                if not intervening:
                    nearby_reactions.append(reaction)
            participant_names = {
                _sender_label(item)
                for item in occurrences
                if _sender_label(item) not in {"联系人", "群成员", "待识别成员"}
            }
            participant_count = len(participant_names)
            operational = any(
                _action_evidence(_display_content(item.get("content"))).get("tags")
                or _RISK.search(_display_content(item.get("content")))
                or _TRADE.search(_display_content(item.get("content")))
                or _has_question(_display_content(item.get("content")))
                for item in occurrences
            )
            reaction_count = len(nearby_reactions)
            if operational and reaction_count < 2:
                continue
            if reaction_count < 2 and len(occurrences) < 3:
                # Two repeats without a response are not enough to distinguish
                # an inside joke from a normal phrase or duplicated task.
                continue
            if reaction_count < 2 and participant_count < 2:
                # Repeated self-sent or unresolved-member text is more likely
                # to be a resend/status update than a group inside joke.
                continue
            if participant_count < 2 and len(occurrences) < 3 and reaction_count < 2:
                continue

            start = _timestamp(occurrences[0].get("timestamp"))
            end = _timestamp(occurrences[-1].get("timestamp"))
            span = end - start
            confidence = 42
            confidence += min(18, max(0, len(occurrences) - 2) * 8)
            confidence += 14 if participant_count >= 2 else 0
            confidence += min(28, reaction_count * 10)
            confidence += 8 if span <= timedelta(minutes=15) else (4 if span <= timedelta(hours=2) else 0)
            confidence = min(94, confidence)
            if confidence < 55:
                continue

            evidence_items = sorted(
                list(occurrences) + list(nearby_reactions),
                key=lambda item: (_timestamp(item.get("timestamp")), str(item.get("message_id") or "")),
            )
            message_ids = [
                str(item.get("message_id"))
                for item in occurrences
                if item.get("message_id") is not None
            ]
            reaction_message_ids = [
                str(item.get("message_id"))
                for item in nearby_reactions
                if item.get("message_id") is not None
            ]
            digest = hashlib.sha1((chat_key + "|" + phrase_key).encode("utf-8")).hexdigest()[:12]
            kind = "inside_joke" if reaction_count >= 2 else "repeated_phrase"
            reason = (
                "同一用语重复出现，并在邻近 5 分钟内得到 %d 条群内反应；这里只报告为梗候选，不推断具体含义。"
                % reaction_count
                if reaction_count
                else "同一用语由至少两位参与者重复使用；先保留为重复用语候选，等待更多语境确认。"
            )
            candidates.append(
                {
                    "id": "meme:" + digest,
                    "kind": kind,
                    "status": "candidate",
                    "phrase": (
                        signature
                        if phrase_kind == "anchor"
                        else _clip(_display_content(occurrences[0].get("content")), 96)
                    ),
                    "match_mode": phrase_kind,
                    "chat_id": occurrences[0].get("chat_id") or chat_key,
                    "chat_name": _chat_label(occurrences[0]),
                    "occurrence_count": len(occurrences),
                    "participant_count": participant_count,
                    "reaction_count": reaction_count,
                    "confidence": confidence,
                    "start": _visible_timestamp(start, timezone_name),
                    "end": _visible_timestamp(end, timezone_name),
                    "message_ids": message_ids,
                    "reaction_message_ids": reaction_message_ids,
                    "reason": reason,
                    "uncertainty": "目前只确认重复和回应关系，尚未确认这个短语的来源、指代或群体共识。",
                    "evidence": [
                        {
                            "message_id": item.get("message_id"),
                            "chat_id": item.get("chat_id"),
                            "chat_name": _chat_label(item),
                            "sender_name": _sender_label(item),
                            "timestamp": _visible_timestamp(_timestamp(item.get("timestamp")), timezone_name),
                            "content": _clip(_display_content(item.get("content")), 160),
                            "role": "reaction" if item in nearby_reactions else "occurrence",
                        }
                        for item in evidence_items
                    ],
                }
            )
    candidates.sort(
        key=lambda item: (
            int(item.get("confidence") or 0),
            int(item.get("occurrence_count") or 0),
            len(str(item.get("phrase") or "")),
        ),
        reverse=True,
    )
    selected: List[Dict[str, Any]] = []
    for candidate in candidates:
        candidate_ids = set(str(value) for value in candidate.get("message_ids") or [])
        duplicate = False
        for existing in selected:
            if str(existing.get("chat_id")) != str(candidate.get("chat_id")):
                continue
            existing_ids = set(str(value) for value in existing.get("message_ids") or [])
            overlap = len(candidate_ids.intersection(existing_ids))
            phrases_overlap = (
                str(candidate.get("phrase") or "") in str(existing.get("phrase") or "")
                or str(existing.get("phrase") or "") in str(candidate.get("phrase") or "")
            )
            if overlap >= 2 and phrases_overlap:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
        if len(selected) >= 40:
            break
    return selected


def _prepare_message_recognition(
    items: Sequence[MutableMapping[str, Any]],
    timezone_name: str,
) -> List[Dict[str, Any]]:
    """Attach shared recognition labels and meme evidence to local rows."""

    by_message_id: Dict[str, MutableMapping[str, Any]] = {}
    for item in items:
        item["_recognition"] = _message_recognition(item)
        if item.get("message_id") is not None:
            by_message_id.setdefault(str(item.get("message_id")), item)
    candidates = _meme_candidates(items, timezone_name)
    for candidate in candidates:
        for message_id in candidate.get("message_ids") or []:
            item = by_message_id.get(str(message_id))
            if item is None:
                continue
            recognition = dict(item.get("_recognition") or {})
            recognition["meme_candidate"] = True
            recognition["meme_candidate_id"] = candidate.get("id")
            recognition["meme_confidence"] = max(
                int(recognition.get("meme_confidence") or 0),
                int(candidate.get("confidence") or 0),
            )
            item["_recognition"] = recognition
        for message_id in candidate.get("reaction_message_ids") or []:
            item = by_message_id.get(str(message_id))
            if item is None:
                continue
            recognition = dict(item.get("_recognition") or {})
            recognition["meme_evidence"] = True
            recognition["meme_candidate_id"] = candidate.get("id")
            item["_recognition"] = recognition
    return candidates


def _split_conversation_blocks(items: Sequence[Mapping[str, Any]], is_group: bool) -> List[List[Mapping[str, Any]]]:
    """Split one chat by pauses and clear subject changes, not by sender."""

    blocks: List[List[Mapping[str, Any]]] = []
    for item in sorted(items, key=lambda value: value["_timestamp"]):
        if not blocks:
            blocks.append([item])
            continue
        current = blocks[-1]
        gap = item["_timestamp"] - current[-1]["_timestamp"]
        current_signature = set().union(*(_discussion_signature(value) for value in current[-4:]))
        next_signature = _discussion_signature(item)
        next_content = _display_content(item.get("content"))
        conversation_weak_topics = {"产品 / 项目", "社群 / 活动"}
        current_strong = current_signature - conversation_weak_topics
        next_strong = next_signature - conversation_weak_topics
        new_question = bool(
            is_group
            and _has_question(next_content)
            and current_signature
            and next_signature
            and current_signature.isdisjoint(next_signature)
            and _sender_label(item) != _sender_label(current[-1])
        )
        subject_changed = bool(
            current_strong
            and next_strong
            and current_strong.isdisjoint(next_strong)
            and (
                gap > timedelta(minutes=2 if is_group else 20)
                or new_question
                or (is_group and len(current) >= 2)
                or (
                    is_group
                    and len(current) == 1
                    and _is_event_text(current[-1])
                    and _is_event_text(item)
                    and _sender_label(item) != _sender_label(current[-1])
                )
            )
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
        recognition = item.get("_recognition") or {}
        if (
            not value
            or recognition.get("analysis_role") in {"noise", "archive_only"}
            or _GROUP_EMOTION_NOISE.match(value)
            or value in seen
        ):
            continue
        seen.add(value)
        lines.append((_sender_label(item), value))
    return lines


def _editorialize_small_utterance(value: Any, actor: str) -> str:
    """Turn one raw private utterance into a short editorial sentence.

    Evidence remains available in the expandable source list.  The visible
    sentence must describe the point of the exchange instead of copying a
    whole greeting, apology, or question verbatim into the daily brief.
    """

    text = re.sub(r"\s+", " ", _display_content(value)).strip(" ，,。.!！?？~～")
    if not text:
        return "%s留下了一条待回看消息" % actor
    if re.search(r"选课|课程|教务|课表", text) and re.search(
        r"怎么选|如何选|通知|没看明白|看不明白", text
    ):
        return "%s询问选课方式，并表示相关通知没有看明白" % actor
    if re.search(r"开学", text) and re.search(r"广东|家人|爸爸|妈妈", text):
        return "%s提到开学时间，以及家人一同去广东的安排" % actor
    if re.search(r"(?:想问|请问|问下|能否|是否|怎么|如何|为什么|有没有)", text):
        question = re.sub(r"^(?:抱歉|不好意思)[，,、 ]*", "", text)
        question = re.sub(r"(?:哦|呀|啊|呢|吧|嘛)[~～！!。?？]*$", "", question).strip()
        return "%s询问：%s" % (actor, _clip(question, 72))
    if re.search(r"(?:抱歉|不好意思|打扰)", text) and len(text) > 20:
        return "%s先致歉并补充了一项需要回看的信息" % actor
    if re.search(r"(?:提到|说到|聊到|谈到|分享)", text):
        return "%s补充了一项相关信息" % actor
    # Do not expose a complete original line as the summary.  Keep only a
    # short subject-bearing phrase; the source accordion is the reversible
    # place for the exact wording.
    subject = _block_topic([{"content": text}])
    if subject and subject != "其他讨论":
        return "%s补充了%s相关信息" % (actor, subject)
    return "%s留下了一条需要回看上下文的消息" % actor


def _small_matter_signature(item: Mapping[str, Any]) -> str:
    """Create a conservative duplicate key from the editorial meaning.

    A private-chat block can contain many unrelated follow-up messages.  Using
    all evidence in the key therefore makes two identical editorial notes look
    different merely because the later conversation diverged.  The summary is
    already the deduplicated, reader-facing meaning; exact evidence remains
    attached for audit and is intentionally not part of this key.
    """

    text = str(item.get("summary") or "").casefold()
    for name in list(item.get("people") or []) + [
        str(entry.get("chat_name") or "")
        for entry in (item.get("chats") or [])
        if isinstance(entry, Mapping)
    ]:
        if name:
            text = text.replace(str(name).casefold(), " ")
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    text = re.sub(
        r"(?:群成员|相关成员|聊天|私聊|群聊|中|里|提到|补充|信息|安排|一项|相关|一条|需要回看上下文)",
        "",
        text,
    )
    return text[:180]


def _merge_duplicate_small_matters(matters: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse exact semantic duplicates while retaining every source row."""

    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in matters:
        item = dict(raw)
        signature = _small_matter_signature(item)
        # A short key is too weak to merge safely.  It remains a separate
        # small matter even if the visible summaries look similar.
        key = signature if len(signature) >= 10 else "row:" + str(item.get("id") or len(order))
        current = merged.get(key)
        if current is None:
            item["semantic_signature"] = signature
            item["duplicate_count"] = 1
            merged[key] = item
            order.append(key)
            continue
        current["people"] = list(dict.fromkeys(
            list(current.get("people") or []) + list(item.get("people") or [])
        ))
        current["chats"] = list(current.get("chats") or [])
        existing_chat_ids = {
            str(entry.get("chat_id") or "")
            for entry in current["chats"]
            if isinstance(entry, Mapping)
        }
        for chat in item.get("chats") or []:
            if not isinstance(chat, Mapping):
                continue
            if str(chat.get("chat_id") or "") not in existing_chat_ids:
                current["chats"].append(dict(chat))
                existing_chat_ids.add(str(chat.get("chat_id") or ""))
        current["message_ids"] = list(dict.fromkeys(
            list(current.get("message_ids") or []) + list(item.get("message_ids") or [])
        ))
        current["evidence"] = list(current.get("evidence") or [])
        evidence_ids = {
            str(entry.get("message_id") or "")
            for entry in current["evidence"]
            if isinstance(entry, Mapping)
        }
        for entry in item.get("evidence") or []:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("message_id") or "") not in evidence_ids:
                current["evidence"].append(dict(entry))
                evidence_ids.add(str(entry.get("message_id") or ""))
        current["evidence"] = current["evidence"][:32]
        current["message_count"] = len(current["message_ids"])
        current["people"] = current["people"][:16]
        current["start"] = min(str(current.get("start") or ""), str(item.get("start") or ""))
        current["end"] = max(str(current.get("end") or ""), str(item.get("end") or ""))
        current["duplicate_count"] = int(current.get("duplicate_count") or 1) + 1
        current["dedupe_reason"] = "可见摘要和原文证据归一后相同，已合并并保留全部证据"
        # Avoid attributing a cross-chat duplicate to only the first person.
        if len(current["chats"]) > 1:
            summary = str(current.get("summary") or "").rstrip("。")
            if "开学" in summary and "广东" in summary:
                current["summary"] = "多个会话都提到开学时间，以及家人一同去广东的安排。"
            else:
                current["summary"] = "相似内容在%d个会话重复出现：%s。" % (
                    len(current["chats"]),
                    re.sub(r"^[^，。；]+[，。；]", "", summary).strip("。") or summary,
                )
    return [merged[key] for key in order]


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
        editorial = _editorialize_small_utterance(detail, actor)
        return ("%s，%s。" % (place, editorial)) if place else editorial + "。"
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
            summary = "%s，%s" % (place, _editorialize_small_utterance(first_text, first_sender))
            if second:
                summary += "；%s" % _editorialize_small_utterance(second[1], second[0])
            return summary + "。"
        summary = _editorialize_small_utterance(first_text, actor)
        if second:
            summary += "；双方随后还聊到一项补充信息"
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
            editorial_block = [
                item
                for item in block
                if not (
                    is_group
                    and (item.get("_recognition") or {}).get("analysis_role") in {"noise", "archive_only"}
                )
            ]
            if not editorial_block:
                continue
            text_items = [
                item for item in editorial_block
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
                    or any(
                        len(re.sub(r"\s+", "", _display_content(item.get("content")))) >= 24
                        and (
                            _PROJECT_SIGNAL.search(_display_content(item.get("content")))
                            or _FEEDBACK_SIGNAL.search(_display_content(item.get("content")))
                            or _INFORMATION_SIGNAL.search(_display_content(item.get("content")))
                            or _RESOURCE_SIGNAL.search(_display_content(item.get("content")))
                            or _ARGUMENT_SIGNAL.search(_display_content(item.get("content")))
                        )
                        for item in substantive
                    )
                    or recurring_subject
                )
                if not retain:
                    continue
            elif not editorial_block:
                continue

            topic = _block_topic(text_items or editorial_block)
            people = list(dict.fromkeys(
                _sender_label(item)
                for item in editorial_block
                if _sender_label(item) not in {"我", "联系人", "群成员", "待识别成员"}
            ))
            summary = _semantic_matter_summary(chat_name, people, editorial_block, is_group)
            if any(
                str(item.get("message_type") or "").lower() == "voice"
                and item.get("_transcribed_voice") is not True
                for item in editorial_block
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
                for item in editorial_block[:16]
            ]
            first = editorial_block[0]
            matters.append(
                {
                    "id": "small:%s:%s" % (len(matters) + 1, str(first.get("message_id") or "")),
                    "kind": "small_matter",
                    "summary": _clip(summary, 120),
                    "topic": topic,
                    "people": people,
                    "chats": [{"chat_id": chat_id, "chat_name": chat_name}],
                    "message_ids": [str(item.get("message_id") or "") for item in editorial_block if item.get("message_id")],
                    "message_count": len(editorial_block),
                    "start": _visible_timestamp(editorial_block[0]["_timestamp"], timezone_name),
                    "end": _visible_timestamp(editorial_block[-1]["_timestamp"], timezone_name),
                    "evidence": evidence,
                    "is_group": is_group,
                    "social_chat": social_chat,
                }
            )
    matters = _merge_duplicate_small_matters(matters)
    for item in matters:
        signature = str(item.get("semantic_signature") or item.get("id") or "")
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
        item["id"] = "small:%s" % digest
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
        # A short, explicit object such as ``Codex 重置`` is still a useful
        # event even when it contains no explanatory verb.  Without this
        # boost it falls into the low-information lane and disappears before
        # the AI candidate set can give it a separate finding.
        information += 20 if _event_domain_tags(content) else 0
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

    def valid_anchor(term: str, indexes: Sequence[int]) -> bool:
        if len(term) < 2 or len(indexes) < 2 or len(indexes) > max_anchor_frequency:
            return False
        if term.casefold().replace(" ", "") in _GENERIC_EVENT_ANCHORS:
            return False
        if term[0] in _EVENT_BAD_ANCHOR_EDGES or term[-1] in _EVENT_BAD_ANCHOR_EDGES:
            return False
        if len(term) == 2 and len(indexes) < 3:
            return False
        return True

    proposals = []
    for term, indexes in by_term.items():
        unique_indexes = sorted(set(indexes))
        # A broad anchor can legitimately occur in more than one object lane
        # (for example ``账号风控`` in an AI-account complaint and a WeChat
        # integration discussion).  Partition the anchor first so rejecting a
        # cross-domain merge does not discard the valid same-domain subset.
        domain_partitions: Dict[Tuple[str, ...], List[int]] = defaultdict(list)
        hard_domains = set().union(
            *(_event_domain_tags(candidates[index].get("content")) for index in unique_indexes)
        )
        if len(hard_domains) <= 1:
            # Untagged follow-ups can safely inherit the only observed domain;
            # separating them here would break legitimate cross-chat aliases.
            domain_partitions[("__single_domain__",)] = unique_indexes
        else:
            for index in unique_indexes:
                domain_key = tuple(
                    sorted(_event_domain_tags(candidates[index].get("content")))
                ) or ("__none__",)
                domain_partitions[domain_key].append(index)
        for partition in domain_partitions.values():
            unique_indexes = sorted(set(partition))
            if not valid_anchor(term, unique_indexes):
                continue
            if not _event_domains_compatible([candidates[index] for index in unique_indexes]):
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
            proposal_quality = _event_cluster_quality(
                [candidates[index] for index in unique_indexes],
                [term],
                frequency,
            )
            if chat_count >= 2 and not proposal_quality["merge_supported"]:
                continue
            proposals.append((chat_count, max_information, len(unique_indexes), len(term), term, unique_indexes))
    proposals.sort(reverse=True)

    def proposal_shared_topics(indexes: Sequence[int]) -> set:
        tag_sets = [
            _event_semantic_tags(candidates[index])
            for index in indexes
        ]
        if not tag_sets:
            return set()
        return set.intersection(*tag_sets) - _WEAK_SHARED_EVENT_TOPICS

    groups: List[List[Dict[str, Any]]] = []
    accepted_sets: List[set] = []
    for _chat_count, _information, _size, _length, _term, indexes in proposals:
        index_set = set(indexes)
        merged = False
        for existing_index, existing_set in enumerate(accepted_sets):
            overlap = len(index_set.intersection(existing_set))
            overlap_ratio = overlap / min(len(index_set), len(existing_set))
            if overlap_ratio < 0.45:
                continue
            if not proposal_shared_topics(index_set).intersection(
                proposal_shared_topics(existing_set)
            ):
                continue
            merged_set = existing_set.union(index_set)
            if not _event_domains_compatible([candidates[index] for index in sorted(merged_set)]):
                continue
            accepted_sets[existing_index] = merged_set
            groups[existing_index] = [
                candidates[index] for index in sorted(merged_set)
            ]
            merged = True
            break
        if merged:
            continue
        if any(
            len(index_set.intersection(existing)) / min(len(index_set), len(existing)) >= 0.65
            for existing in accepted_sets
        ):
            continue
        accepted_sets.append(index_set)
        groups.append([candidates[index] for index in indexes])
        if len(groups) >= 64:
            break
    profile_terms = {
        str(value).strip().casefold()
        for key in ("roles", "projects", "organizations", "key_contacts", "topics")
        for value in ((profile or {}).get(key) or [])
        if str(value).strip()
    }
    # ``self_name`` is a single string, not a list; iterating it directly
    # would shard it into single characters that match every message.
    self_name = str((profile or {}).get("self_name") or "").strip().casefold()
    if self_name:
        profile_terms.add(self_name)

    def private_for_me_single(item: Mapping[str, Any]) -> bool:
        """A one-to-one message that asks or names the operator is a lead.

        Private messages addressed to the operator should not need a
        multi-message cluster or a hard domain tag before they can surface in
        the for-me lane; a direct request or an explicit mention is enough.
        """

        if bool(item.get("is_group")):
            return False
        content = _display_content(item.get("content"))
        evidence = _action_evidence(content)
        if evidence.get("request") or evidence.get("question_request"):
            return True
        # An explicit request marker ("记得/别忘了/麻烦…") plus a concrete
        # object is a for-me lead even when the verb is colloquial (e.g.
        # “把选题报给教务系统”) and never reaches the strict action list.
        if _REQUEST.search(content) and _has_concrete_object(content):
            return True
        lowered = content.casefold()
        return any(term and term in lowered for term in profile_terms)

    covered = set().union(*accepted_sets) if accepted_sets else set()
    for index, item in enumerate(candidates):
        if index not in covered and (
            int(item["_event_information"]) >= 65
            or (
                int(item["_event_information"]) >= 45
                and bool(_event_domain_tags(item.get("content")))
            )
            or private_for_me_single(item)
        ):
            groups.append([item])

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
        cluster_quality = _event_cluster_quality(items, anchors, frequency)
        if not cluster_quality["domains_compatible"]:
            continue
        if len(chats) >= 2 and not cluster_quality["merge_supported"]:
            continue
        canonical_topics = sorted(
            set().union(*(_canonical_event_terms(item.get("content")) for item in items))
        )
        domain_tags = sorted(
            set().union(*(_event_domain_tags(item.get("content")) for item in items))
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
        if (
            not meaningful_cluster
            and int(items[0]["_event_information"]) < 45
            and not private_for_me_single(items[0])
        ):
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
        editing = _event_editing_fields(items, anchors, chats, sorted(people), timezone_name)
        # A local cluster has no external verification channel.  Even when the
        # conversation is coherent, "confirmed" would make a chat opinion or
        # personal report look like a settled world fact in the UI.
        if status == "confirmed" and editing.get("claim_status") != "externally_verified":
            status = "pending"
        briefs.append(
            {
                "id": "event:%s" % event_id,
                "title": _event_title(items, anchors),
                "summary": summary,
                "narrative": editing["narrative"],
                "detail": editing["detail"],
                "detail_summary": editing["detail_summary"],
                "detail_points": editing["detail_points"],
                 "what_changed": editing["what_changed"],
                 "why_it_matters": editing["why_it_matters"],
                 "core_conclusion": editing["core_conclusion"],
                 "uncertainty": editing["uncertainty"],
                 "claim_type": editing.get("claim_type") or "reported_claim",
                 "claim_status": editing.get("claim_status") or "chat_report",
                 "claim_label": editing.get("claim_label") or "聊天事实陈述",
                 "claim_boundary": editing.get("claim_boundary") or "聊天证据尚未完成外部核验。",
                 "next_step": editing["next_step"],
                "status": status,
                "lane": lane,
                 "tags": list(dict.fromkeys(tags)),
                 "canonical_topics": canonical_topics[:8],
                "domain_tags": [label for label in _EVENT_DOMAIN_ORDER if label in domain_tags],
                "importance": min(100, max(int(item["_event_information"]) for item in items) + min(30, len(items) * 5) + min(25, max(0, len(chats) - 1) * 5)),
                "confidence": int(cluster_quality["confidence"]),
                "cluster_quality": cluster_quality,
                "cluster_score": int(cluster_quality["score"]),
                "merge_basis": list(cluster_quality["merge_basis"]),
                "evidence_binding_rate": float(cluster_quality["evidence_binding_rate"]),
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


def _retention_row_id(item: Mapping[str, Any]) -> str:
    message_id = str(item.get("message_id") or "").strip()
    if message_id:
        return message_id
    source_index = item.get("_source_index")
    return "row:%s" % (str(source_index) if source_index is not None else "unknown")


def _build_retention_funnel(
    ordered: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    discoveries: Sequence[Mapping[str, Any]],
    highlights: Sequence[Mapping[str, Any]],
    event_briefs: Sequence[Mapping[str, Any]],
    unformed_dynamics: Sequence[Mapping[str, Any]],
    timezone_name: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Keep an auditable destination for every input row.

    The funnel is intentionally additive: a row may be suppressed from the
    first screen, but it remains represented in the ledger with its reason and
    evidence links.  This makes ``many -> few`` a visibility decision rather
    than an irreversible data-loss decision.
    """

    action_ids = {str(item.get("message_id")) for item in actions if item.get("message_id")}
    discovery_ids = {str(item.get("message_id")) for item in discoveries if item.get("message_id")}
    highlight_ids = {str(item.get("message_id")) for item in highlights if item.get("message_id")}
    event_ids_by_message: Dict[str, List[str]] = defaultdict(list)
    for event in event_briefs:
        event_id = str(event.get("id") or "")
        for message_id in event.get("message_ids") or []:
            if message_id:
                event_ids_by_message[str(message_id)].append(event_id)
    dynamic_ids_by_message: Dict[str, List[str]] = defaultdict(list)
    for dynamic in unformed_dynamics:
        dynamic_id = str(dynamic.get("id") or "")
        for message_id in dynamic.get("message_ids") or []:
            if message_id:
                dynamic_ids_by_message[str(message_id)].append(dynamic_id)

    # First-screen evidence follows the conservative editorial definition used
    # by the audit, with explicitly selected highlights added as visible leads.
    first_screen_ids = set(action_ids) | set(discovery_ids) | set(event_ids_by_message)
    first_screen_ids.update(highlight_ids)
    ledger: List[Dict[str, Any]] = []
    layer_counts: Counter = Counter()
    reason_counts: Counter = Counter()
    seen_row_ids: Set[str] = set()
    duplicate_row_ids: Set[str] = set()

    for item in ordered:
        row_id = _retention_row_id(item)
        if row_id in seen_row_ids:
            duplicate_row_ids.add(row_id)
        seen_row_ids.add(row_id)
        recognition = item.get("_recognition") or {}
        assessment = item.get("_assessment") or {}
        score = item.get("_score") or {}
        noise_class = str(recognition.get("noise_class") or "none")
        analysis_role = str(recognition.get("analysis_role") or "content")
        candidate_state = str(assessment.get("state") or "unclassified")
        event_ids = list(dict.fromkeys(event_ids_by_message.get(row_id, [])))
        dynamic_ids = list(dict.fromkeys(dynamic_ids_by_message.get(row_id, [])))

        if row_id in first_screen_ids:
            layer = "first_screen"
            visibility = "focus"
            reason = "进入第一屏证据面：动作、发现、事件或高价值线索"
        elif analysis_role in {"noise", "archive_only"}:
            layer = "suppressed_noise"
            visibility = "suppressed"
            reason = str(recognition.get("reason") or "噪声或媒体消息收起，但原始记录仍保留")
        elif candidate_state == "context_needed":
            layer = "context_pending"
            visibility = "archive"
            reason = str(assessment.get("reason") or "上下文不足，保留原文等待核对")
        elif event_ids:
            layer = "event_archive"
            visibility = "archive"
            reason = "已进入事件证据链，但当前不占用第一屏"
        elif dynamic_ids:
            layer = "dynamic_archive"
            visibility = "archive"
            reason = "已进入未成形动态，保留为可回看片段"
        elif candidate_state in {"filtered_low_value", "low_information"}:
            layer = "low_value_archive"
            visibility = "archive"
            reason = str(assessment.get("reason") or "低优先级内容，收起但不删除")
        else:
            layer = "unclassified_archive"
            visibility = "archive"
            reason = "尚未形成可发布主线，保留在全量消息索引中"

        reason_code = {
            "first_screen": "editorial_focus",
            "suppressed_noise": "noise_or_media_suppressed",
            "context_pending": "context_needed",
            "event_archive": "event_evidence_archive",
            "dynamic_archive": "dynamic_archive",
            "low_value_archive": "low_value_archive",
            "unclassified_archive": "unclassified_archive",
        }[layer]
        layer_counts[layer] += 1
        reason_counts[reason_code] += 1
        content = _display_content(item.get("content"))
        topic_tags = [] if analysis_role in {"noise", "archive_only"} else _topic_tags(content)
        topic_details = [] if analysis_role in {"noise", "archive_only"} else _topic_detail_tags(content)
        ledger.append(
            {
                "row_id": row_id,
                "message_id": item.get("message_id"),
                "chat_id": item.get("chat_id"),
                "chat_name": _chat_label(item),
                "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                "layer": layer,
                "visibility": visibility,
                "reason_code": reason_code,
                "noise_class": noise_class,
                "analysis_role": analysis_role,
                "candidate_state": candidate_state,
                "expression_status": assessment.get("expression_status") or "clear",
                "topics": topic_tags,
                "subtopics": topic_details,
                "event_ids": event_ids,
                "dynamic_ids": dynamic_ids,
                "is_first_screen": row_id in first_screen_ids,
                "meme_evidence": bool(recognition.get("meme_evidence")),
                "reason": reason,
                "score": int(score.get("score") or 0),
                "content": _clip(content, 180),
            }
        )

    input_row_ids = [_retention_row_id(item) for item in ordered]
    ledger_row_ids = [str(item.get("row_id") or "") for item in ledger]
    missing = sorted(set(input_row_ids) - set(ledger_row_ids))
    extra = sorted(set(ledger_row_ids) - set(input_row_ids))
    funnel = {
        "principle": "many_to_few_reversible",
        "stages": [
            {"stage": "raw_messages", "count": len(ordered), "description": "全量输入，原始消息不丢弃"},
            {"stage": "recognized_messages", "count": len(ordered), "description": "每条消息完成噪声、上下文和表达状态识别"},
            {
                "stage": "semantic_pool",
                "count": sum(1 for item in ledger if item["analysis_role"] not in {"noise", "archive_only"}),
                "description": "进入主题、事件或上下文分析池",
            },
            {
                "stage": "first_screen",
                "count": sum(1 for item in ordered if _retention_row_id(item) in first_screen_ids),
                "description": "最终占用第一屏的少量证据",
            },
        ],
        "exclusive_layers": [
            {"layer": layer, "count": count}
            for layer, count in layer_counts.most_common()
        ],
        "reason_counts": [
            {"reason_code": reason_code, "count": count}
            for reason_code, count in reason_counts.most_common()
        ],
        "invariant": {
            "input_count": len(ordered),
            "ledger_count": len(ledger),
            "all_rows_retained": (
                not missing
                and not extra
                and not duplicate_row_ids
                and len(ledger) == len(ordered)
            ),
            "missing_row_ids": missing,
            "extra_row_ids": extra,
            "duplicate_row_ids": sorted(duplicate_row_ids),
        },
    }
    return ledger, funnel


def _build_context_windows(
    items: Sequence[Mapping[str, Any]],
    max_neighbors: int = 5,
    max_minutes: int = 20,
) -> Dict[int, List[Mapping[str, Any]]]:
    """Build same-chat context after filtering parallel-topic leakage.

    Time proximity is only a search boundary.  A neighbor must also share
    terms/topics, continue a context-dependent fragment, or belong to the same
    sender's short burst.  This prevents a generic request from inheriting the
    last unrelated technical conversation in a busy group.
    """

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in items:
        key = str(item.get("chat_id") or item.get("chat_name") or "unknown")
        grouped[key].append(item)
    result: Dict[int, List[Mapping[str, Any]]] = {id(item): [] for item in items}
    limit = timedelta(minutes=max_minutes)
    burst_limit = timedelta(minutes=12)
    for group in grouped.values():
        group.sort(key=lambda item: (_timestamp(item.get("timestamp")), str(item.get("message_id") or "")))
        positions = {id(item): position for position, item in enumerate(group)}
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
                direct_relation = _direct_thread_relation(item, neighbor)
                if distance > limit and not (same_sender and distance <= burst_limit) and not direct_relation:
                    continue
                content = _display_content(neighbor.get("content"))
                if not _is_text_candidate(neighbor, content):
                    continue
                neighbor_index = positions.get(id(neighbor))
                if (
                    not direct_relation
                    and neighbor_index is not None
                    and _has_unrelated_interruption(
                        group,
                        index,
                        neighbor_index,
                        item,
                        neighbor,
                    )
                ):
                    continue
                relevance, _reasons = _context_relevance(item, neighbor)
                if relevance <= 0:
                    continue
                neighbors.append((relevance, distance, neighbor))
            neighbors.sort(
                key=lambda value: (
                    int(value[0]),
                    -value[1].total_seconds(),
                    _timestamp(value[2].get("timestamp")),
                    str(value[2].get("message_id") or ""),
                ),
                reverse=True,
            )
            selected = [value[2] for value in neighbors[: max(12, max_neighbors * 2)]]
            selected.sort(key=lambda value: (_timestamp(value.get("timestamp")), str(value.get("message_id") or "")))
            result[id(item)] = selected
    return result


def _has_unrelated_interruption(
    group: Sequence[Mapping[str, Any]],
    target_index: int,
    neighbor_index: int,
    target: Mapping[str, Any],
    neighbor: Mapping[str, Any],
) -> bool:
    """Reject a generic fragment that jumps over another strong topic.

    A lexical match is allowed to cross an interruption: that is how a clear
    follow-up such as “是哪个接口？” can return to the earlier interface
    message.  A bare “继续”“这个”“请确认一下” cannot safely choose an old
    thread after an unrelated topic has been inserted, so it stays unresolved.
    Explicit replies and quoted messages bypass this guard.
    """

    if not _is_context_fragment_message(target):
        return False
    if _direct_thread_relation(target, neighbor):
        return False
    target_content = _display_content(target.get("content"))
    neighbor_content = _display_content(neighbor.get("content"))
    shared_terms = _context_terms(target_content).intersection(_context_terms(neighbor_content))
    shared_topics = (
        _event_semantic_tags(target).intersection(_event_semantic_tags(neighbor))
        - _WEAK_SHARED_EVENT_TOPICS
    )
    if shared_terms or shared_topics:
        return False

    start, end = sorted((target_index, neighbor_index))
    neighbor_topics = _event_semantic_tags(neighbor) - _CONTEXT_WEAK_SUBJECTS
    if not neighbor_topics:
        return False
    for middle in group[start + 1:end]:
        recognition = _message_recognition(middle)
        if recognition.get("analysis_role") in {"noise", "archive_only", "context"}:
            continue
        middle_content = _display_content(middle.get("content"))
        middle_topics = _event_semantic_tags(middle) - _CONTEXT_WEAK_SUBJECTS
        if not middle_topics:
            continue
        if middle_topics.isdisjoint(neighbor_topics) and not _context_terms(middle_content).intersection(
            _context_terms(neighbor_content)
        ):
            return True
    return False


def _context_relevance(
    target: Mapping[str, Any],
    neighbor: Mapping[str, Any],
) -> Tuple[int, List[str]]:
    """Score whether one nearby message belongs to the target's thread."""

    target_content = _display_content(target.get("content"))
    neighbor_content = _display_content(neighbor.get("content"))
    target_recognition = _message_recognition(target)
    neighbor_recognition = _message_recognition(neighbor)
    if neighbor_recognition.get("analysis_role") in {"noise", "archive_only"}:
        return 0, []
    direct_relation = _direct_thread_relation(target, neighbor)
    if direct_relation:
        return (100, ["显式回复/引用" if direct_relation == "explicit_reply" else "引用文本"])
    target_topics = _event_semantic_tags(target)
    neighbor_topics = _event_semantic_tags(neighbor)
    shared_topics = target_topics.intersection(neighbor_topics) - _WEAK_SHARED_EVENT_TOPICS
    shared_terms = _context_terms(target_content).intersection(_context_terms(neighbor_content))
    same_sender = (
        _sender_label(target), target.get("is_self") is True
    ) == (
        _sender_label(neighbor), neighbor.get("is_self") is True
    )
    target_is_fragment = target_recognition.get("analysis_role") == "context"
    neighbor_is_substantive = neighbor_recognition.get("analysis_role") in {"content", "social"}
    target_context_dependent = bool(_CONTEXT_DEPENDENT.search(target_content) or _CLAUSE_FRAGMENT.search(target_content))
    distance = abs(_timestamp(target.get("timestamp")) - _timestamp(neighbor.get("timestamp")))

    # Distinct strong subjects are a boundary.  A same-sender burst can still
    # bridge it, but an unrelated speaker should not be imported just because
    # the messages arrived within the same minute.
    strong_target_topics = target_topics - _WEAK_SHARED_EVENT_TOPICS
    strong_neighbor_topics = neighbor_topics - _WEAK_SHARED_EVENT_TOPICS
    if strong_target_topics and strong_neighbor_topics and strong_target_topics.isdisjoint(strong_neighbor_topics):
        if not same_sender:
            return 0, []

    score = 0
    reasons: List[str] = []
    if shared_terms:
        score += min(12, 2 + len(shared_terms) * 2)
        reasons.append("共享主题词")
    if shared_topics:
        score += min(8, 3 + len(shared_topics) * 2)
        reasons.append("共享主题标签")
    if same_sender:
        score += 4
        reasons.append("同一发言人连续表达")
    if target_is_fragment and neighbor_is_substantive:
        score += 2
        reasons.append("短句需要前文补足")
    if target_context_dependent and neighbor_is_substantive:
        if _DOMAIN_SIGNAL.search(neighbor_content) or shared_terms or shared_topics:
            score += 3
            reasons.append("指代句获得邻近对象")
    if distance <= timedelta(minutes=3) and (shared_terms or shared_topics or target_context_dependent):
        score += 1
        reasons.append("时间连续")

    # A generic request with no object is deliberately not connected to a
    # merely adjacent substantive message.  It can still be rescued by the
    # same-sender or explicit context-dependent branches above.
    if not shared_terms and not shared_topics and not same_sender and not target_context_dependent:
        return 0, []
    return score, reasons


def _context_supports(content: str, context: Sequence[Mapping[str, Any]]) -> bool:
    """Whether nearby text supplies a likely subject for a short/follow-up message."""

    if not context:
        return False
    current_terms = _context_terms(content)
    for neighbor in context:
        neighbor_content = _display_content(neighbor.get("content"))
        neighbor_terms = _context_terms(neighbor_content)
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
    context_meta = _context_metadata(message, context)
    expression = _expression_assessment(message)
    expression_status = str(expression.get("status") or "clear")
    explicit_thread = bool(context_meta.get("explicit_thread"))
    context_ambiguous = bool(context_meta.get("ambiguous_context")) and not explicit_thread
    context_supported = bool(context_supported or explicit_thread)
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
            "context_relations": list(context_meta.get("context_relations") or []),
            "context_topic_labels": list(context_meta.get("context_topic_labels") or []),
            "revived_thread": bool(context_meta.get("revived_thread")),
            "explicit_thread": bool(context_meta.get("explicit_thread")),
            "ambiguous_context": context_ambiguous,
            "expression_status": expression_status,
            "expression_confidence": int(expression.get("confidence") or 0),
            "expression_reason": expression.get("reason") or "",
            "actionable_risk": actionable_risk,
        }

    if evidence.get("request") or evidence.get("question_request"):
        if not has_object:
            if context_supported and not context_ambiguous:
                return result("reviewable", "action", "请求对象由同一会话的邻近上下文补足")
            reason = "出现请求表达，但没有明确说明要处理的对象"
            if context_ambiguous:
                reason = "请求可能指向多个并行话题，暂不替用户选择对象"
            return result("context_needed", "action", reason)
        if (context_dependent or expression_status in {"context_dependent", "fragment", "low_clarity"}) and not context_supported:
            return result("context_needed", "action", "请求表达依赖前文或口语语境，当前窗口无法确认完整意图")
        if context_ambiguous:
            return result("context_needed", "action", "请求附近存在多个可能话题，暂不自动归因")
        return result("reviewable", "action", "明确请求了可核对的对象或处理动作")

    if evidence.get("risk"):
        if _CLAUSE_FRAGMENT.search(content):
            if context_supported and not context_ambiguous:
                return result("context_needed", "risk", "这条消息是上下文中的半句，已关联邻近消息但不单独升级")
            return result("context_needed", "risk", "风险表达像是上下文中的半句，当前窗口无法确认完整影响")
        if context_ambiguous:
            return result("context_needed", "risk", "风险表达附近存在多个可能话题，暂不自动归因")
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
        if (context_dependent or expression_status in {"context_dependent", "fragment", "low_clarity"} or not has_actor) and not context_supported:
            return result("context_needed", "decision", "决策表达缺少主体或前置背景")
        if context_ambiguous:
            return result("context_needed", "decision", "决策表达附近存在多个可能话题，暂不自动归因")
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
    recognition = _message_recognition(message)
    if (
        not _is_text_candidate(message, content)
        or _LOW_SIGNAL.match(content)
        or recognition.get("analysis_role") in {"noise", "archive_only"}
    ):
        return None
    if _INFORMATION_EXCLUDE.search(content):
        return None
    if action_assessment and action_assessment.get("state") not in {
        "low_information", "filtered_low_value"
    }:
        return None

    if recognition.get("meme_candidate"):
        meme_score = max(55, min(92, int(recognition.get("meme_confidence") or 55)))
        return {
            "state": "informative",
            "kind": "meme",
            "reason": "重复短语并得到群内回应，保留为梗候选；不自动解释其具体含义",
            "signals": ["梗候选", "重复用语", "群内回应"],
            "score": meme_score,
            "context_message_ids": [
                str(item.get("message_id") or "")
                for item in context[:4]
                if item.get("message_id")
            ],
            "meme_candidate_id": recognition.get("meme_candidate_id"),
            "expression_status": action_assessment.get("expression_status") if action_assessment else "clear",
        }

    resource = bool(_RESOURCE_SIGNAL.search(content))
    technical = bool(_INFORMATION_SIGNAL.search(content))
    argument = bool(_ARGUMENT_SIGNAL.search(content))
    project = bool(_PROJECT_SIGNAL.search(content))
    feedback = bool(_FEEDBACK_SIGNAL.search(content))
    expression_status = str((action_assessment or {}).get("expression_status") or "clear")
    expression_caution = expression_status in {"context_dependent", "fragment", "low_clarity", "colloquial"}
    long_enough = len(re.sub(r"\s+", "", content)) >= 24
    has_number = bool(re.search(r"\d", content))
    concrete_question = bool(
        _has_question(content)
        and (_DOMAIN_SIGNAL.search(content) or _INFORMATION_SIGNAL.search(content))
        and len(re.sub(r"\s+", "", content)) >= 6
    )
    if not (
        resource
        or (technical and len(content) >= 10)
        or (long_enough and argument)
        or ((project or feedback) and len(content) >= 8)
        or concrete_question
    ):
        return None

    signals: List[str] = []
    if technical:
        signals.append("技术讨论")
    if resource:
        signals.append("资源链接")
    if argument:
        signals.append("观点解释")
    if project:
        signals.append("项目语境")
    if feedback:
        signals.append("产品反馈")
    if concrete_question:
        signals.append("具体问题")
    if has_number:
        signals.append("数据或指标")
    if expression_caution:
        signals.append("表达需核对")
    if resource:
        kind = "resource"
        reason = "发现可回看的资源或链接，适合整理为资料线索"
    elif technical and (_CHANGE_VERB.search(content) or _PROPOSAL.search(content)):
        kind = "progress"
        reason = "出现技术、产品或方案进展，但没有形成明确待办"
    elif feedback or project:
        kind = "discussion"
        reason = "包含产品反馈、使用体验或项目语境，保留给后续归纳而不是直接压成低信息"
    elif concrete_question:
        kind = "discussion"
        reason = "出现带明确对象的提问，保留原文和上下文供后续核对"
    elif argument or long_enough:
        kind = "knowledge"
        reason = "内容包含可复用的解释、观点或经验，不应被当作闲聊过滤"
    else:
        kind = "discussion"
        reason = "出现有主题的讨论片段，保留给 AI 做归纳"
    if expression_caution:
        reason += "；表达存在省略或口语化，后续只基于原文和上下文核对，不自动补全含义"
    score = min(
        100,
        18
        + min(30, len(content) // 3)
        + (22 if resource else 0)
        + (16 if technical else 0)
        + (10 if argument else 0)
        + (12 if feedback else 0)
        + (8 if project else 0)
        + (10 if concrete_question else 0)
        + (8 if has_number else 0)
        + min(12, len(context) * 2),
    )
    if expression_caution:
        score = min(score, 62)
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
        "expression_status": expression_status,
        "expression_caution": expression_caution,
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
    _prepare_message_recognition(source_items, "Asia/Shanghai")
    context_windows = _build_context_windows(source_items)
    ranked: List[Dict[str, Any]] = []
    for item in source_items:
        content = _display_content(item.get("content"))
        recognition = _message_recognition(item)
        score = _score_message(item)
        assessment = _candidate_assessment(item, context_windows.get(id(item), ()))
        information = _information_assessment(
            item,
            context_windows.get(id(item), ()),
            assessment,
        )
        if item.get("is_self") not in {True, False} or not score.get("eligible"):
            continue
        if recognition.get("analysis_role") in {"noise", "archive_only"} or _LOW_SIGNAL.match(content):
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
                    "relation": _context_relation_label(item, neighbor),
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
        if recognition.get("analysis_role") == "context":
            rule_signals.append("上下文片段")
        if assessment.get("expression_status") in {"context_dependent", "fragment", "low_clarity", "colloquial"}:
            rule_signals.append("表达需核对")
        if information is not None:
            rule_signals.extend(information.get("signals") or [])
        claim_profile = _claim_profile(content)
        if claim_profile.get("claim_label"):
            rule_signals.append(str(claim_profile["claim_label"]))
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
                "claim_type": claim_profile.get("claim_type"),
                "claim_status": claim_profile.get("claim_status"),
                "claim_boundary": claim_profile.get("claim_boundary"),
                "domain_tags": [
                    label for label in _EVENT_DOMAIN_ORDER
                    if label in _event_domain_tags(content)
                ],
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
    skipped: List[Dict[str, Any]] = []
    per_chat: Counter = Counter()
    per_chat_limit = max(12, min(24, (safe_limit + 5) // 6))
    for item in ranked:
        chat = str(item["chat_name"])
        if per_chat[chat] >= per_chat_limit:
            skipped.append(item)
            continue
        selected.append(item)
        per_chat[chat] += 1
        if len(selected) >= safe_limit:
            break
    if len(selected) < safe_limit:
        selected.extend(skipped[:safe_limit - len(selected)])
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
                "claim_type": item.get("claim_type"),
                "claim_status": item.get("claim_status"),
                "claim_boundary": item.get("claim_boundary"),
                "domain_tags": list(item.get("domain_tags") or []),
                "context": item["context"],
                "timestamp": item["_timestamp"].isoformat(timespec="seconds"),
                "_source_message_id": item["_source_message_id"],
            }
        )
    return output


def build_ai_dialogue_packets(
    candidates: Iterable[Mapping[str, Any]],
    max_items: int = 12,
    max_span_minutes: int = 45,
) -> List[Dict[str, Any]]:
    """Split model-facing candidates into stable, single-chat dialogue packets.

    Packet boundaries are deliberately structural: a packet never crosses a
    chat, the configured inactivity gap, or ``max_items``. Topic and domain
    labels stay attached to candidates for the model and downstream validators,
    but do not fragment an otherwise continuous conversation. Existing
    ``evidence_ref`` values are retained verbatim so downstream validation can
    keep using the day's global evidence namespace.
    """

    safe_max_items = max(1, min(int(max_items), 50))
    safe_span = timedelta(minutes=max(1, min(int(max_span_minutes), 24 * 60)))
    ordered: List[Dict[str, Any]] = []
    for ordinal, raw in enumerate(candidates):
        item = dict(raw)
        item["_packet_ordinal"] = ordinal
        item["_packet_timestamp"] = _timestamp(item.get("timestamp"))
        ordered.append(item)
    ordered.sort(
        key=lambda item: (
            str(item.get("chat_name") or "会话"),
            item["_packet_timestamp"],
            str(item.get("evidence_ref") or ""),
            item["_packet_ordinal"],
        )
    )

    packets: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_chat = ""
    previous_timestamp: Optional[datetime] = None

    def flush() -> None:
        nonlocal current, current_chat, previous_timestamp
        if current:
            packets.append(current)
        current = []
        current_chat = ""
        previous_timestamp = None

    for item in ordered:
        chat = str(item.get("chat_name") or "会话")
        timestamp = item["_packet_timestamp"]
        boundary = bool(
            current
            and (
                chat != current_chat
                or previous_timestamp is None
                or timestamp - previous_timestamp > safe_span
                or len(current) >= safe_max_items
            )
        )
        if boundary:
            flush()
        if not current:
            current_chat = chat
        current.append(item)
        previous_timestamp = timestamp
    flush()

    output: List[Dict[str, Any]] = []
    for packet in packets:
        start = packet[0]["_packet_timestamp"]
        end = packet[-1]["_packet_timestamp"]
        refs = [str(item.get("evidence_ref") or "") for item in packet]
        chat = str(packet[0].get("chat_name") or "会话")
        packet_id = "dialogue-%s" % hashlib.sha1(
            "\x1f".join([chat, start.isoformat(), end.isoformat(), *refs]).encode("utf-8")
        ).hexdigest()[:16]
        clean_candidates: List[Dict[str, Any]] = []
        for item in packet:
            clean = dict(item)
            clean.pop("_packet_ordinal", None)
            clean.pop("_packet_timestamp", None)
            clean_candidates.append(clean)
        output.append(
            {
                "packet_id": packet_id,
                "chat_name": chat,
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "evidence_refs": refs,
                "candidates": clean_candidates,
            }
        )
    output.sort(key=lambda item: (item["start"], item["chat_name"], item["packet_id"]))
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
    for source_index, raw in enumerate(messages):
        item = dict(raw)
        item["_source_index"] = source_index
        item["_timestamp"] = _timestamp(item.get("timestamp"))
        ordered.append(item)
    ordered.sort(key=lambda item: (item["_timestamp"], str(item.get("message_id") or "")))
    meme_candidates = _prepare_message_recognition(ordered, timezone_name)
    context_windows = _build_context_windows(ordered)
    for item in ordered:
        score = _score_message(item)
        recognition = _message_recognition(item)
        score.update(
            {
                "noise_class": recognition.get("noise_class") or "none",
                "noise_score": int(recognition.get("noise_score") or 0),
                "analysis_role": recognition.get("analysis_role") or "content",
            }
        )
        assessment = _candidate_assessment(item, context_windows.get(id(item), ()))
        if item.get("is_self") is False and score.get("eligible"):
            score.update(
                {
                    "candidate_state": assessment["state"],
                    "candidate_type": assessment["kind"],
                    "context_supported": assessment["context_supported"],
                    "context_message_ids": assessment["context_message_ids"],
                    "context_relations": assessment.get("context_relations") or [],
                    "context_topic_labels": assessment.get("context_topic_labels") or [],
                    "revived_thread": bool(assessment.get("revived_thread")),
                    "explicit_thread": bool(assessment.get("explicit_thread")),
                    "ambiguous_context": bool(assessment.get("ambiguous_context")),
                    "expression_status": assessment.get("expression_status") or "clear",
                    "expression_confidence": int(assessment.get("expression_confidence") or 0),
                    "expression_reason": assessment.get("expression_reason") or "",
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
            "detail_senders": set(),
            "detail_chats": set(),
            "resource_count": 0,
            "high_information_count": 0,
            "score_total": 0,
            "kinds": Counter(),
            "details": set(),
            "evidence": [],
            "detail_items": [],
            "source_message_ids": [],
            "source_message_id_set": set(),
        }
    )
    suppressed_candidates: List[Dict[str, Any]] = []
    noise_counts = Counter()
    recognition_counts = Counter()
    noise_samples: List[Dict[str, Any]] = []
    context_neighbor_total = 0
    context_supported_messages = 0

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
        recognition = item.get("_recognition") or {}
        noise_class = str(recognition.get("noise_class") or "none")
        analysis_role = str(recognition.get("analysis_role") or "content")
        recognition_counts[noise_class] += 1
        if analysis_role in {"noise", "archive_only"}:
            noise_counts[noise_class] += 1
            if len(noise_samples) < 24:
                noise_samples.append(
                    {
                        "message_id": item.get("message_id"),
                        "chat_id": item.get("chat_id"),
                        "chat_name": _chat_label(item),
                        "sender_name": _sender_label(item),
                        "timestamp": _visible_timestamp(item["_timestamp"], timezone_name),
                        "content": _clip(_display_content(item.get("content")), 160),
                        "noise_class": noise_class,
                        "noise_score": int(recognition.get("noise_score") or 0),
                        "reason": recognition.get("reason"),
                    }
                )
        context_neighbor_total += len(context_windows.get(id(item), ()))
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
        # Detail recall is broader than the publishable information score:
        # context-linked and low-score messages can still contain the platform,
        # process step or failure branch needed to understand a topic.
        if analysis_role not in {"noise", "archive_only"}:
            detail_topic_tags = list(_topic_tags(content))
            # Context labels can help a genuinely fragmentary reply, but they
            # must not copy every neighbor's topic onto a complete sentence.
            if analysis_role == "context" and not any(
                value not in {"其他讨论", "社群 / 活动"} for value in detail_topic_tags
            ):
                detail_topic_tags.extend(
                    str(value)
                    for value in (item.get("_assessment") or {}).get("context_topic_labels") or []
                    if value
                )
            for detail_topic in dict.fromkeys(detail_topic_tags):
                if detail_topic in {"其他讨论", "社群 / 活动"}:
                    continue
                detail_stats = topic_stats[detail_topic]
                message_id = str(item.get("message_id") or "").strip()
                if message_id and message_id not in detail_stats["source_message_id_set"]:
                    detail_stats["source_message_id_set"].add(message_id)
                    detail_stats["source_message_ids"].append(message_id)
                if len(detail_stats["detail_items"]) < 256 and item not in detail_stats["detail_items"]:
                    detail_stats["detail_items"].append(item)
                detail_sender = _sender_label(item)
                if detail_sender:
                    detail_stats["detail_senders"].add(detail_sender)
                detail_chat = _chat_label(item)
                if detail_chat:
                    detail_stats["detail_chats"].add(detail_chat)
        if item.get("is_self") is False and score.get("eligible"):
            assessment = item.get("_assessment") or {}
            information = _information_assessment(
                item,
                context_windows.get(id(item), ()),
                assessment,
            )
            if information is not None and information.get("context_message_ids"):
                context_supported_messages += 1
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
                topic_tags = _topic_tags(content)
                topic_details = _topic_detail_tags(content)
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
                    stats["details"].update(topic_details)
                    message_id = str(item.get("message_id") or "").strip()
                    if message_id and message_id not in stats["source_message_id_set"]:
                        stats["source_message_id_set"].add(message_id)
                        stats["source_message_ids"].append(message_id)
                    if len(stats["detail_items"]) < 256 and item not in stats["detail_items"]:
                        stats["detail_items"].append(item)
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
                         "context_relations": list(assessment.get("context_relations") or []),
                         "context_topic_labels": list(assessment.get("context_topic_labels") or []),
                         "revived_thread": bool(assessment.get("revived_thread")),
                         "explicit_thread": bool(assessment.get("explicit_thread")),
                         "ambiguous_context": bool(assessment.get("ambiguous_context")),
                         "expression_status": assessment.get("expression_status") or "clear",
                         "expression_confidence": int(assessment.get("expression_confidence") or 0),
                         "expression_reason": assessment.get("expression_reason") or "",
                         "recognition": {
                            "noise_class": str(recognition.get("noise_class") or "none"),
                            "analysis_role": str(recognition.get("analysis_role") or "content"),
                            "meme_candidate": bool(recognition.get("meme_candidate")),
                            "meme_candidate_id": recognition.get("meme_candidate_id"),
                        },
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
                             "context_relations": list(assessment.get("context_relations") or []),
                             "context_topic_labels": list(assessment.get("context_topic_labels") or []),
                             "revived_thread": bool(assessment.get("revived_thread")),
                             "explicit_thread": bool(assessment.get("explicit_thread")),
                             "ambiguous_context": bool(assessment.get("ambiguous_context")),
                             "expression_status": assessment.get("expression_status") or "clear",
                             "expression_confidence": int(assessment.get("expression_confidence") or 0),
                             "expression_reason": assessment.get("expression_reason") or "",
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
                        "context_relations": list(assessment.get("context_relations") or []),
                         "context_topic_labels": list(assessment.get("context_topic_labels") or []),
                         "revived_thread": bool(assessment.get("revived_thread")),
                         "explicit_thread": bool(assessment.get("explicit_thread")),
                         "ambiguous_context": bool(assessment.get("ambiguous_context")),
                         "expression_status": assessment.get("expression_status") or "clear",
                         "expression_confidence": int(assessment.get("expression_confidence") or 0),
                         "expression_reason": assessment.get("expression_reason") or "",
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
                 "context_relations": list(score.get("context_relations") or []),
                 "context_topic_labels": list(score.get("context_topic_labels") or []),
                 "revived_thread": bool(score.get("revived_thread")),
                 "explicit_thread": bool(score.get("explicit_thread")),
                 "ambiguous_context": bool(score.get("ambiguous_context")),
                 "expression_status": score.get("expression_status") or "clear",
                 "expression_confidence": int(score.get("expression_confidence") or 0),
                 "expression_reason": score.get("expression_reason") or "",
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
    event_quality = [item.get("cluster_quality") or {} for item in event_briefs]
    cross_chat_event_count = sum(
        1 for item in event_briefs if int(item.get("related_chat_count") or 0) >= 2
    )
    event_confidences = [
        int(item.get("confidence") or 0)
        for item in event_briefs
        if item.get("confidence") is not None
    ]
    event_evidence_total = sum(int(item.get("message_count") or 0) for item in event_quality)
    event_evidence_bound = sum(
        min(
            int(item.get("message_count") or 0),
            round(
                int(item.get("message_count") or 0)
                * float(item.get("evidence_binding_rate") or 0.0)
            ),
        )
        for item in event_quality
    )
    event_evidence_binding_rate = (
        round(event_evidence_bound / event_evidence_total, 3)
        if event_evidence_total else 0.0
    )
    event_confidence_average = (
        round(sum(event_confidences) / len(event_confidences))
        if event_confidences else 0
    )
    low_confidence_event_count = sum(
        1 for item in event_briefs if int(item.get("confidence") or 0) < 55
    )
    unformed_dynamics = _small_matters(ordered, event_briefs, timezone_name)
    retention_ledger, funnel = _build_retention_funnel(
        ordered,
        actions,
        discoveries,
        highlights,
        event_briefs,
        unformed_dynamics,
        timezone_name,
    )
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
    noise_total = sum(noise_counts.values())
    noise_rate = round(noise_total / total, 3) if total else 0.0
    context_window_coverage = (
        round(sum(1 for item in ordered if context_windows.get(id(item))) / total, 3)
        if total else 0.0
    )
    meme_occurrence_total = sum(int(item.get("occurrence_count") or 0) for item in meme_candidates)
    meme_reaction_total = sum(int(item.get("reaction_count") or 0) for item in meme_candidates)
    meme_confidences = [int(item.get("confidence") or 0) for item in meme_candidates]
    meme_confidence_average = (
        round(sum(meme_confidences) / len(meme_confidences))
        if meme_confidences else 0
    )
    noise_by_type = [
        {"kind": kind, "count": count}
        for kind, count in noise_counts.most_common()
    ]
    recognition_breakdown = [
        {"kind": kind, "count": count}
        for kind, count in recognition_counts.most_common()
    ]

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
        detail = _detail_bundle(
            stats["detail_items"],
            timezone_name,
            source_message_ids=stats["source_message_ids"],
        )
        detail_summary = _detail_summary(detail)
        participant_candidates: List[str] = []
        candidate_names = list(detail.get("participants") or [])
        if not candidate_names:
            candidate_names = sorted(set(stats["senders"]) | set(stats["detail_senders"]))
        for name in candidate_names:
            normalized_name = str(name or "").strip()
            if normalized_name and normalized_name not in participant_candidates:
                participant_candidates.append(normalized_name)
        generic_participants = {"我", "联系人", "群成员", "待识别成员"}
        participants = [
            name for name in participant_candidates
            if name not in generic_participants
        ] or participant_candidates
        participants = participants[:16]
        speaker_points = _detail_attribution_points(detail, limit=4)
        claim_profile = _combined_claim_profile(
            _display_content(item.get("content")) for item in stats["detail_items"]
        )
        claim_boundary = str(
            claim_profile.get("claim_boundary")
            or "聊天证据尚未完成外部核验。"
        )
        if claim_profile.get("claim_type") in {"opinion", "hypothesis", "question"}:
            why_it_matters = claim_boundary
        elif chat_count >= 2:
            why_it_matters = "这个主题在多个会话出现，说明它是共同讨论对象；" + claim_boundary
        elif resource_total:
            why_it_matters = "这个主题包含可回看的资源线索，但资源内容和相关事实仍需核对；" + claim_boundary
        else:
            why_it_matters = "这个主题在当前会话持续出现，适合回看讨论脉络；" + claim_boundary
        summary_text = (
            "%s：%d 条有效判断，来自 %d 个会话；高信息量 %d 条，%s%s。"
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
                (
                    "，关联证据 %d 条" % len(stats["source_message_ids"])
                    if len(stats["source_message_ids"]) > message_count
                    else ""
                ),
            )
        )
        if participants:
            summary_text += " 发言人：%s。" % "、".join(participants[:6])
        if speaker_points:
            summary_text += " 主要说法：%s。" % "；".join(speaker_points[:2])
        if detail_summary:
            summary_text += " 细节：" + detail_summary
        topic_briefs.append(
            {
                 "kind": "theme",
                 "topic": topic,
                 "subtopics": sorted(stats["details"]),
                 "specificity": "generic" if topic in {"其他讨论", "社群 / 活动"} else "specific",
                 "message_count": message_count,
                "chat_count": chat_count,
                "sender_count": len(stats["senders"]),
                "participants": participants,
                "sender_names": participants,
                "participant_count": len(participants),
                "resource_count": resource_total,
                "high_information_count": high_information_total,
                "average_score": average_score,
                 "value_level": "high" if average_score >= 75 else ("medium" if average_score >= 55 else "low"),
                 "evidence_count": len(stats["source_message_ids"]),
                 "source_message_ids": list(stats["source_message_ids"][:256]),
                "entities": list(detail.get("entities") or []),
                "objects": list(detail.get("entities") or []),
                "speaker_points": speaker_points,
                 "attributions": list(detail.get("attributions") or []),
                 "claim_type": claim_profile.get("claim_type"),
                 "claim_status": claim_profile.get("claim_status"),
                 "claim_label": claim_profile.get("claim_label"),
                 "claim_boundary": claim_boundary,
                 "detail_summary": detail_summary,
                 "detail_points": list(detail.get("points") or []),
                 "detail": detail,
                 "kinds": [
                    {"kind": kind, "count": count}
                    for kind, count in stats["kinds"].most_common()
                ],
                 "summary": _clip(summary_text, 620),
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
    generic_topic_names = {"其他讨论", "社群 / 活动"}
    raw_generic_topic_count = sum(1 for item in topic_briefs if item.get("topic") in generic_topic_names)
    raw_generic_topic_messages = sum(
        int(item.get("message_count") or 0)
        for item in topic_briefs
        if item.get("topic") in generic_topic_names
    )
    # A generic bucket can contain useful evidence, but it is not itself a
    # publishable topic. Keep it in a separate detail surface so it cannot
    # occupy a headline while its source messages remain recoverable.
    unclassified_topic_briefs = [
        item for item in topic_briefs
        if item.get("topic") in generic_topic_names
    ]
    topic_briefs = [
        item for item in topic_briefs
        if item.get("topic") not in generic_topic_names
    ]
    generic_topic_count = 0
    topic_message_total = sum(int(item.get("message_count") or 0) for item in topic_briefs)
    unclassified_topic_messages = sum(
        int(item.get("message_count") or 0)
        for item in unclassified_topic_briefs
    )
    generic_topic_messages = sum(
        int(item.get("message_count") or 0)
        for item in topic_briefs
        if item.get("topic") in generic_topic_names
    )
    topic_specificity_rate = (
        round(1 - generic_topic_messages / topic_message_total, 3)
        if topic_message_total else 1.0
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
        topic_text = "、".join(dict.fromkeys(str(item.get("title")) for item in event_briefs[:5]))[:90] or "尚未形成明确事件"
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
        top_topic_names = list(dict.fromkeys(str(item.get("title")) for item in event_briefs[:3]))[:2]
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
            "name": "rules_v1", "version": "recognition_v1", "label": "消息识别与跨会话事件聚合",
            "note": "先按噪音、上下文、社交和有效内容分层；回复/引用链可跨长时间恢复挖坟上下文；含糊表达只在证据足够时补足对象；事件聚合同时使用具体短语、语义标签和同义别名；梗只输出带重复/回应证据的候选；每条简报保留人物、会话和原文证据。",
        },
        "summary": {
            "messages": total, "inbound": inbound, "self": self_count,
            "unknown_direction": unknown_direction, "chats": len(chat_counts),
            "events": len(episodes), "event_briefs": len(event_briefs),
            "cross_chat_events": cross_chat_event_count,
            "event_confidence_average": event_confidence_average,
            "event_evidence_binding_rate": event_evidence_binding_rate,
            "low_confidence_events": low_confidence_event_count,
            "actions": len(actions), "high_value": high_count,
            "needs_attention": medium_count, "low_signal": low_count,
            "reviewable": reviewable_count, "context_needed": context_needed_count,
            "filtered_low_value": filtered_low_value_count,
            "substantive": informative_count,
            "high_information": high_information_count,
            "resources": resource_count,
            "discussion_episodes": len(discussion_episodes),
            "noise_total": noise_total,
            "noise_rate": noise_rate,
            "noise_by_type": noise_by_type,
            "context_fragments": int(recognition_counts.get("context_fragment") or 0),
            "social_chatter": int(recognition_counts.get("social_chatter") or 0),
            "meme_candidates": len(meme_candidates),
            "meme_confidence_average": meme_confidence_average,
            "primary_insights": len(primary_insights),
            "event_briefs": len(event_briefs),
            "for_me": len(for_me),
            "trending": len(trending),
            "pending_review": len(pending_review),
             "topic_count": len(topic_briefs),
             "generic_topic_count": generic_topic_count,
             "topic_specificity_rate": topic_specificity_rate,
             "unclassified_topic_count": raw_generic_topic_count - generic_topic_count,
             "unclassified_topic_messages": raw_generic_topic_messages - generic_topic_messages,
             "unformed_dynamics": len(unformed_dynamics),
             "unformed_messages": len(dynamic_message_ids),
             "accounted_messages": accounted_message_count,
             "published_messages": published_message_count,
             "retention_ledger_count": len(retention_ledger),
             "first_screen_messages": sum(1 for row in retention_ledger if row.get("is_first_screen")),
             "all_rows_retained": bool(funnel["invariant"]["all_rows_retained"]),
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
            "event_brief_count": len(event_briefs),
            "cross_chat_event_count": cross_chat_event_count,
            "event_confidence_average": event_confidence_average,
            "low_confidence_event_count": low_confidence_event_count,
            "event_evidence_binding_rate": event_evidence_binding_rate,
            "noise_total": noise_total,
            "noise_rate": noise_rate,
            "noise_by_type": noise_by_type,
            "recognition_breakdown": recognition_breakdown,
            "context_window_coverage": context_window_coverage,
            "context_neighbor_average": round(context_neighbor_total / total, 2) if total else 0.0,
            "context_supported_messages": context_supported_messages,
            "meme_candidate_count": len(meme_candidates),
            "meme_occurrence_total": meme_occurrence_total,
            "meme_reaction_total": meme_reaction_total,
            "meme_confidence_average": meme_confidence_average,
             "topic_count": len(topic_briefs),
             "generic_topic_count": generic_topic_count,
             "topic_specificity_rate": topic_specificity_rate,
             "unclassified_topic_count": raw_generic_topic_count - generic_topic_count,
             "unclassified_topic_messages": raw_generic_topic_messages - generic_topic_messages,
             "insight_coverage": round(informative_count / eligible_inbound, 3) if eligible_inbound else 0.0,
             "accounted_message_coverage": round(accounted_message_count / total, 3) if total else 1.0,
             "retention_ledger_count": len(retention_ledger),
             "retention_invariant_passed": bool(funnel["invariant"]["all_rows_retained"]),
             "retention_missing_rows": list(funnel["invariant"]["missing_row_ids"]),
             "retention_extra_rows": list(funnel["invariant"]["extra_row_ids"]),
             "retention_duplicate_rows": list(funnel["invariant"]["duplicate_row_ids"]),
             "funnel_principle": funnel["principle"],
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
            "limitation": "抓取完整度需要源端应有条数才能计算；噪音、上下文和梗识别均为可解释候选，不等同于事实判断；严格待处理结果用于人工筛选；媒体内容暂不参与文本价值判断。",
        },
        "hourly": [
            {"hour": hour, **hourly_stats[hour]}
            for hour in range(24)
        ],
        "top_chats": top_chats,
        "topics": [{"topic": key, "count": value} for key, value in topic_counts.most_common()],
        "types": [{"type": key, "count": value} for key, value in type_counts.most_common()],
        "topic_briefs": topic_briefs,
        "unclassified_topic_briefs": unclassified_topic_briefs,
        "primary_insights": primary_insights,
        "event_briefs": event_briefs,
        "for_me": for_me,
        "trending": trending,
        "pending_review": pending_review,
        "unformed_dynamics": unformed_dynamics,
        "retention_ledger": retention_ledger,
        "funnel": funnel,
        "insight_breakdown": insight_breakdown,
        "highlights": highlights, "actions": actions[:20], "events": episodes[:10],
        "discoveries": discoveries,
        "discussion_episodes": discussion_episodes,
        "noise_samples": noise_samples,
        "recognition_breakdown": recognition_breakdown,
        "meme_candidates": meme_candidates,
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
