import http.client
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import wechat_bridge.web as web_module

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.engine import ReplyPolicy
from wechat_bridge.models import HealthStatus, IncomingMessage, ReplyDecision, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.store import SQLiteStore
from wechat_bridge.web import (
    BridgeRequestHandler,
    _clean_limitations,
    _deduplicate_model_claims,
    _evidence_is_question_only,
    _headline_from_summary,
    _preserve_claim_uncertainty,
    _finding_evidence_boundary_reason,
    _finding_evidence_boundary_violation,
    _finding_unsupported_narrative_claims,
    start_dashboard_thread,
)


def test_headline_from_summary_uses_evidence_led_editorial_pattern_without_speaker_prefix():
    headline = _headline_from_summary(
        "huaahuaa 称 deepseek v4.1 出来了；huaahuaa 称 deepseek v4.1 原生多模态",
        [{"content": "我靠 deepseek v4.1 出来了，原生多模态"}],
    )

    assert headline == "DeepSeek V4.1：原生多模态"
    assert "huaahuaa" not in headline
    assert "…" not in headline


def test_headline_from_summary_combines_endpoint_and_speed_without_mechanical_truncation():
    headline = _headline_from_summary(
        "秘圣淼称 DeepSeek 官方端点可以调用临时模型；nɘƨoЯ 称新模型稳定在 400tps 左右",
        [
            {"content": "DeepSeek官方端点可以直接打这个模型：DeepSeek-V4.1-Flash-Expires-On-0910"},
            {"content": "新模型稳定400tps左右"},
        ],
    )

    assert headline == "DeepSeek V4.1：官方端点与400tps"
    assert not headline.endswith("…")


def test_headline_from_summary_turns_goal_bug_into_readable_subject_judgment():
    headline = _headline_from_summary(
        "cy 表示发现一个 bug：goal 点停止后对话还没停止；停止 goal 后执行当前 prompt 但不会 loop",
        [
            {"content": "发现个bug 这个goal点停止 对话还没停止"},
            {"content": "你停止goal他干的是当前prompt的内容 但是不会loop"},
        ],
    )

    assert headline == "Goal停止后：当前任务仍会执行"
    assert "cy" not in headline


def test_apply_editorial_headline_replaces_valid_but_speaker_led_or_clipped_title():
    finding = {
        "title": "秘圣淼称DeepSeek官方端点可以直接调用D…",
        "summary": "秘圣淼称 DeepSeek 官方端点可以调用临时模型；新模型稳定在 400tps 左右",
    }
    evidence = [
        {"sender_name": "秘圣淼", "content": "DeepSeek官方端点可以直接调用 V4.1 Flash"},
        {"sender_name": "nɘƨoЯ", "content": "新模型稳定400tps左右"},
    ]

    title = BridgeRequestHandler._apply_editorial_headline(finding, evidence)

    assert title == "DeepSeek V4.1：官方端点与400tps"
    assert not title.endswith("…")


@pytest.mark.parametrize(
    "raw_title",
    [
        "现在ds的性能很魔幻",
        "要很长时间，可以用Luna做但是效果会不好",
        "这几天做的事情high正正好",
    ],
)
def test_apply_editorial_headline_rewrites_chatty_but_formally_valid_titles(raw_title):
    summaries = {
        "现在ds的性能很魔幻": "种博来称现在 ds 的性能很魔幻；Pro 要在 deepseek harness（DSH）中才能发挥出来",
        "要很长时间，可以用Luna做但是效果会不好": "种博来称 Astra 真的快；可以用 Luna 做，但效果会不好；需要很长时间",
        "这几天做的事情high正正好": "oran-z 称这几天使用 high 强度正合适；Yanbo. 称官方似乎推荐默认 high",
    }
    evidence = [
        {"sender_name": "种博来", "content": summaries[raw_title]},
    ]

    title = BridgeRequestHandler._apply_editorial_headline(
        {"title": raw_title, "summary": summaries[raw_title]},
        evidence,
    )

    assert title != raw_title
    assert "魔幻" not in title
    assert "正正好" not in title


@pytest.mark.parametrize(
    ("summary", "evidence", "expected"),
    [
        (
            "nɘƨoЯ 称新模型稳定在 400tps 左右；有人询问这才是真正的 flash 吗",
            [{"content": "新模型稳定400tps左右"}, {"content": "这才是真正的flash吗"}],
            "Flash实测：速度约400tps",
        ),
        (
            "种博来称 Pro 的5倍价格5倍额度；Pro 20x 是10倍价格20倍额度",
            [{"content": "Pro 的5倍价格5倍额度"}, {"content": "Pro 20x 是10倍价格20倍额度"}],
            "Pro套餐：价格与额度倍数对应",
        ),
        (
            "dualface 和 Echo 均表示 superpowers 已经毫无必要",
            [{"content": "superpowers 已经毫无必要了"}],
            "Superpowers：必要性遭质疑",
        ),
        (
            "oran-z 表示最近 Fable 变耐用了；API 拿不到 Fable usage",
            [{"content": "最近感觉Fable变耐用了"}, {"content": "API拿不到Fable usage"}],
            "Fable体感：更耐用但Usage不可见",
        ),
        (
            "有人建议用 bigmodel 或 z.ai 账号登录；每周末有免费 token",
            [{"content": "可以用bigmodel或者z.ai账号登录"}, {"content": "每周末有免费token用"}],
            "模型接入：Z.ai登录与周末Token",
        ),
        (
            "pzc163 询问有效 token 测试方案，并表示下次可以邀请参与模型测试",
            [{"content": "你有有效 token 的测试方案吗"}, {"content": "下次可以邀请你参与模型测试"}],
            "模型测试：方案征集与参与邀请",
        ),
        (
            "夜飞鸟称 gpt6 计费异常在任何情况下所有人都会出现，且与 sub2api 无关",
            [{"content": "gpt6计费异常在任何情况下所有人都会出现，和sub2api没关"}],
            "GPT-6计费：异常范围仍待核实",
        ),
        (
            "种博来称 Codex 使用第三方模型比较麻烦；可以用 bigmodel 或 z.ai 登录",
            [{"content": "Codex使用第三方模型比较麻烦"}, {"content": "可以用bigmodel或者z.ai账号登录"}],
            "Codex接入：第三方模型配置仍麻烦",
        ),
        (
            "两位成员均表示 gpt 再降也比国模强",
            [{"content": "gpt再降也比国模强"}],
            "模型评价：GPT仍被认为强于国产模型",
        ),
        (
            "最开始做网站都用 Gemini 网页版写代码，倒也是能用；有段时间 Gemini 比 GPT 强",
            [{"content": "最开始做网站都用Gemini的网页版写代码 倒也是能用"}],
            "Gemini编程：网页版早期体验尚可",
        ),
    ],
)
def test_headline_from_summary_uses_readable_subject_judgment_patterns(summary, evidence, expected):
    title = _headline_from_summary(summary, evidence)

    assert title == expected
    assert "…" not in title


@pytest.mark.parametrize(
    ("summary", "evidence", "expected"),
    [
        (
            "种博来称现在 ds 的性能很魔幻；Pro 要在 deepseek harness（DSH）中才能发挥出来",
            [{"content": "现在ds的性能很魔幻"}, {"content": "Pro要在deepseek harness(DSH)中才能发挥出来"}],
            "DeepSeek Pro：DSH被认为是性能前提",
        ),
        (
            "种博来称 Astra 真的快；可以用 Luna 做，但效果会不好；需要很长时间",
            [{"content": "Astra真的快"}, {"content": "要很长时间，可以用Luna做但是效果会不好"}],
            "Astra速度获好评，Luna被指耗时且效果欠佳",
        ),
        (
            "种博来称 Astra Max 处理复杂请求需要很长时间；Luna 可以做但效果不好",
            [
                {"content": "Astra Max处理复杂请求要很长时间"},
                {"content": "Luna可以做但是效果不好"},
            ],
            "Astra复杂请求耗时，Luna替代效果欠佳",
        ),
    ],
)
def test_headline_from_summary_surfaces_the_news_point(summary, evidence, expected):
    assert _headline_from_summary(summary, evidence) == expected


def test_headline_from_summary_turns_stream_overload_claim_into_news_headline():
    title = _headline_from_summary(
        "秘圣淼称上游 OpenAI 存在约 13% 的间歇性 overloaded，流式机制让问题显现",
        [{"content": "上游 OpenAI 本身就有约 13% 的间歇性 overloaded，真正让你看见它的是流式下的一个机制缺陷"}],
    )

    assert title == "OpenAI过载：流式机制暴露间歇异常"


def test_packet_claims_merge_duplicate_text_and_question_only_stays_unresolved():
    claims = _deduplicate_model_claims([
        {"text": "这才是真正的 flash 吗？", "evidence_refs": ["m-1"]},
        {"text": "这才是真正的 flash 吗", "evidence_refs": ["m-2"]},
    ])

    assert claims == [{
        "text": "这才是真正的 flash 吗？",
        "evidence_refs": ["m-1", "m-2"],
    }]
    assert _evidence_is_question_only([
        {"content": "这才是真正的 flash 吗"},
        {"content": "这是 flash 吗？"},
    ]) is True
    assert _evidence_is_question_only([
        {"content": "这才是真正的 flash 吗"},
        {"content": "这是 flash"},
    ]) is False


@pytest.mark.parametrize("marker", ["貌似", "好像", "可能", "感觉", "我记得"])
def test_claim_paraphrase_preserves_source_uncertainty(marker):
    claim = _preserve_claim_uncertainty(
        "甲表示 Fable 已经变耐用",
        [{"sender_name": "甲", "content": "%s Fable 变耐用了" % marker}],
    )

    assert claim == "甲表示，%sFable 已经变耐用" % marker


def test_claim_uncertainty_does_not_repeat_normalized_memory_qualifier():
    claim = _preserve_claim_uncertainty(
        "甲表示，其记得 Fable 已经变耐用",
        [{"sender_name": "甲", "content": "我记得 Fable 变耐用了"}],
    )

    assert claim == "甲表示，其记得 Fable 已经变耐用"
    assert "我记得其记得" not in claim


def test_claim_uncertainty_does_not_leak_from_later_clause():
    claim = _preserve_claim_uncertainty(
        "甲表示，OpenAI送了6个月会员",
        [{"sender_name": "甲", "content": "要不是OpenAI送了6个月会员，我可能暂时不会继续用"}],
    )

    assert claim == "甲表示，OpenAI送了6个月会员"
    assert "可能" not in claim


def test_claim_uncertainty_stays_when_it_modifies_the_claim_clause():
    claim = _preserve_claim_uncertainty(
        "甲表示，官方推荐使用High",
        [{"sender_name": "甲", "content": "貌似官方推荐默认使用High"}],
    )

    assert claim == "甲表示，貌似官方推荐使用High"


def test_claim_attribution_replaces_generic_speaker_with_cited_sender():
    claim = BridgeRequestHandler._ensure_claim_attribution(
        "群内有人表示 GPT 再降也比国模强",
        [{"sender_name": "米昔", "content": "gpt再降也比国模强"}],
    )

    assert claim == "米昔表示，GPT 再降也比国模强"
    assert "群内有人" not in claim


def test_claim_attribution_preserves_existing_named_source():
    claim = BridgeRequestHandler._ensure_claim_attribution(
        "米昔表示 GPT 再降也比国模强",
        [{"sender_name": "米昔", "content": "gpt再降也比国模强"}],
    )

    assert claim == "米昔表示 GPT 再降也比国模强"


class WorkbenchAdapter(WeChatAdapter):
    name = "workbench-test"

    @property
    def version(self):
        return "test"

    def connect(self):
        pass

    def disconnect(self):
        pass

    def health_check(self):
        return HealthStatus(True, self.name, self.version, "ok")

    def start_receive(self, chat_names, callback):
        self.callback = callback

    def stop_receive(self):
        self.callback = None

    def send_text(self, chat_id, chat_name, content):
        return SendResult(True, True, "confirmed")

    def get_chat_history(self, chat_id, chat_name="", limit=50):
        return []


def _message(
    message_id,
    timestamp,
    *,
    chat_id,
    chat_name,
    sender_id,
    sender_name,
    content,
    is_self=False,
    is_group=False,
    sender_name_source=None,
    message_type="text",
):
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_name=chat_name,
        sender_id=sender_id,
        sender_name=sender_name,
        message_type=message_type,
        content=content,
        timestamp=datetime(2026, 8, 21, timestamp, 0, tzinfo=timezone.utc),
        is_self=is_self,
        is_group=is_group,
        sender_name_source=sender_name_source,
        sender_name_confidence=1.0 if sender_name_source else None,
        raw_message={"content": content},
        adapter_name="workbench-test",
        adapter_version="test",
    )


@pytest.fixture
def running_workbench():
    with TemporaryDirectory() as tmp:
        store = SQLiteStore(tmp + "/bridge.db")
        adapter = WorkbenchAdapter()
        service = BridgeService(
            adapter,
            store,
            ReplyPolicy.from_values("固定回复", ("文件传输助手",)),
            ("文件传输助手",),
            dry_run=True,
            filehelper_only=False,
            send_enabled=False,
        )
        fixture_messages = [
            _message(
                "m-1",
                1,
                chat_id="chat-a",
                chat_name="Alice",
                sender_id="alice-id",
                sender_name="Alice",
                sender_name_source="contact_remark",
                content="请今天确认项目报价并回复我？",
            ),
            _message(
                "m-2",
                2,
                chat_id="chat-a",
                chat_name="Alice",
                sender_id="me",
                sender_name="我",
                content="已收到",
                is_self=True,
            ),
            _message(
                "m-3",
                3,
                chat_id="chat-group",
                chat_name="研发群",
                sender_id="member-1",
                sender_name="小王",
                sender_name_source="group_nickname",
                content="项目故障，明天请安排处理？",
                is_group=True,
            ),
            _message(
                "m-4",
                4,
                chat_id="chat-a",
                chat_name="Alice",
                sender_id="wxid_alice",
                sender_name="wxid_alice",
                content="补充一下背景",
            ),
            _message(
                "m-5",
                5,
                chat_id="chat-b",
                chat_name="Bob",
                sender_id="bob-id",
                sender_name="Bob",
                sender_name_source="direct_chat_peer",
                content="晚点聊",
            ),
        ]
        for item in fixture_messages:
            store.ingest(item, ReplyDecision(False, "fixture"), create_task=False)
        service.start()
        server, _thread = start_dashboard_thread(service, port=0)
        try:
            yield server, store
        finally:
            server.shutdown()
            server.server_close()
            service.stop()
            store.close()


def _request(server, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("finding", "evidence", "reason"),
    [
        (
            {"summary": "Codex 重置仍在发生。"},
            [{"content": "刚才又重置了。"}],
            "finding_domain_missing_in_evidence",
        ),
        (
            {"summary": "Codex 重置仍在发生。"},
            [{"content": "Codex 已重置。"}, {"content": "随后仍未恢复。"}],
            "cited_evidence_unrelated_to_synthesis",
        ),
        (
            {"summary": "价格明显上涨。"},
            [{"content": "GPT 昨晚再次重置。"}],
            "lexical_no_overlap",
        ),
    ],
)
def test_finding_boundary_reason_codes_preserve_boolean_contract(finding, evidence, reason):
    assert _finding_evidence_boundary_reason(finding, evidence) == reason
    assert _finding_evidence_boundary_violation(finding, evidence) is True


def test_finding_boundary_allows_one_story_with_separate_object_evidence():
    finding = {
        "summary": "我把 Codex 的 MCP 能力接给 Hermes，再由 GPT 统一指挥工具链。"
    }
    evidence = [
        {
            "chat_name": "种博来",
            "sender_name": "我",
            "content": "用 OAuth 把能力调出来给 Hermes 用。",
        },
        {
            "chat_name": "种博来",
            "sender_name": "我",
            "content": "Codex 好就好在 MCP 丰富。",
        },
        {
            "chat_name": "种博来",
            "sender_name": "我",
            "content": "这下可以实现 GPT 指挥所有人替我干活了。",
        },
    ]

    assert _finding_evidence_boundary_reason(finding, evidence) is None
    assert _finding_evidence_boundary_violation(finding, evidence) is False


def test_cross_object_multi_author_story_requires_thirty_minute_window():
    finding = {"summary": "甲认为 Gemini 编程更强；乙认为 GPT 输出更稳定。"}
    evidence = [
        {
            "chat_name": "模型群",
            "sender_name": "甲",
            "timestamp": "2026-09-08T10:00:00+08:00",
            "content": "我认为 Gemini 编程更强。",
        },
        {
            "chat_name": "模型群",
            "sender_name": "乙",
            "timestamp": "2026-09-08T10:47:00+08:00",
            "content": "我认为 GPT 输出更稳定。",
        },
    ]

    assert _finding_evidence_boundary_reason(finding, evidence) == (
        "multi_object_synthesis_lacks_shared_event"
    )

    evidence[1]["sender_name"] = "甲"
    assert _finding_evidence_boundary_reason(finding, evidence) is None


def test_finding_boundary_does_not_treat_distant_same_author_posts_as_one_thread():
    finding = {
        "summary": "GPT 额度反馈与 Codex 插件界面被汇总为同一项工具链变化。"
    }
    evidence = [
        {
            "chat_name": "模型群",
            "sender_name": "我",
            "timestamp": "2026-09-07T08:00:00+08:00",
            "content": "GPT 额度今天消耗很快。",
        },
        {
            "chat_name": "模型群",
            "sender_name": "我",
            "timestamp": "2026-09-07T16:00:00+08:00",
            "content": "Codex 插件界面新增了侧边栏。",
        },
    ]

    assert (
        _finding_evidence_boundary_reason(finding, evidence)
        == "multi_object_synthesis_lacks_shared_event"
    )
    assert _finding_evidence_boundary_violation(finding, evidence) is True


def test_finding_boundary_rejects_broad_multi_model_roundup():
    finding = {
        "summary": "GPT、Claude、Gemini 和 Cursor 被汇总为一次模型选型结论。"
    }
    evidence = [
        {"chat_name": "模型群", "sender_name": "甲", "content": "GPT 额度消耗太快。"},
        {"chat_name": "模型群", "sender_name": "乙", "content": "Claude 写方案更稳。"},
        {"chat_name": "模型群", "sender_name": "丙", "content": "Gemini 前端审美不错。"},
        {"chat_name": "模型群", "sender_name": "丁", "content": "Cursor 已用掉一半额度。"},
    ]

    assert (
        _finding_evidence_boundary_reason(finding, evidence)
        == "broad_multi_object_synthesis"
    )
    assert _finding_evidence_boundary_violation(finding, evidence) is True


def test_narrative_claim_guard_flags_uncited_identifier_number_and_speaker():
    evidence = [
        {
            "chat_name": "Deepthink",
            "sender_name": "pzc163",
            "content": "我下次可以邀请你参与模型测试",
        }
    ]
    finding = {
        "summary": "pzc163 介绍了 minicpm5-2B 的 token 表现。",
        "narrative": "pzc163 提到其团队新发的 minicpm5-2B 模型有效 token 超过 128，"
        "夜航星随后表示想参与评测。",
        "speakers": [{"name": "pzc163"}, {"name": "夜航星"}],
    }

    unsupported = _finding_unsupported_narrative_claims(finding, evidence)

    assert {item["kind"] for item in unsupported} == {"identifier", "number", "speaker"}
    assert {item["token"] for item in unsupported} == {"minicpm5-2B", "128", "夜航星"}


def test_narrative_claim_guard_passes_fully_cited_prose():
    evidence = [
        {
            "chat_name": "种博来",
            "sender_name": "种博来",
            "content": "Pro 的5倍价格5倍额度",
        },
        {
            "chat_name": "种博来",
            "sender_name": "种博来",
            "content": "Pro 20x 是10倍价格20倍额度",
        },
    ]
    finding = {
        "summary": "种博来解释 Pro 5x/20X 的价格与额度倍数关系，称 20X 更划算。",
        "narrative": "种博来在私聊中解释 OpenAI Pro 各档位的定价逻辑：Pro 是 5 倍价格对应 "
        "5 倍额度，Pro 20X 是 10 倍价格对应 20 倍额度。当前只能确认聊天中有人这样陈述，"
        "事实与因果关系尚未联网核验。",
    }

    # OpenAI is an object name guarded by the boundary layer, and the
    # disclaimer boilerplate is attached by the pipeline itself: neither may
    # trip the claim guard when the cited evidence carries the specifics.
    assert _finding_unsupported_narrative_claims(finding, evidence) == []


def test_narrative_claim_guard_accepts_chinese_numeral_paraphrase():
    evidence = [
        {
            "chat_name": "Deepthink",
            "sender_name": "咸鱼嫌鱼咸",
            "content": "Astra一个任务转了四十分钟了",
        }
    ]
    finding = {
        "summary": "咸鱼嫌鱼咸称一个 Astra 任务转了 40 分钟不报错也不推进。",
    }

    assert _finding_unsupported_narrative_claims(finding, evidence) == []


def test_narrative_claim_guard_skips_generic_words_and_speaker_sendernames():
    evidence = [
        {
            "chat_name": "Deepthink x 厦门大学社区",
            "sender_name": "黄培荣",
            "content": "Astra做规划，具体执行开子angent最多三个，模型最高sol-高",
        }
    ]
    finding = {
        "summary": "黄培荣分享 Astra 规划加三个以内子 agent 的执行工作流。",
        "narrative": "黄培荣在 Deepthink 社区分享自己的 Astra 工作流：Astra 负责规划，"
        "具体执行则开启子 agent，数量控制在三个以内，模型档位最高到 sol-高。",
    }

    assert _finding_unsupported_narrative_claims(finding, evidence) == []


def test_narrative_claim_guard_forgives_one_edit_typos_in_evidence():
    evidence = [
        {
            "chat_name": "Vibe Friends",
            "sender_name": "dualface",
            "content": "我让 astra 看看本地哪些 skills 他用不上，他上去把 use-superpowes 给干掉了",
        }
    ]
    finding = {
        "summary": "dualface 让 Astra 清理本地 skills，把 use-superpowers 删掉了。",
    }

    # 证据原文把 use-superpowers 误写成 use-superpowes；一个字符的转写误差
    # 不能当成叙事夹带。
    assert _finding_unsupported_narrative_claims(finding, evidence) == []


def test_narrative_claim_guard_ignores_adverb_fragments_as_speakers():
    evidence = [
        {
            "chat_name": "Vibe Friends",
            "sender_name": "shareworks",
            "content": "机场 IP 用的人太多了，换独享之后几乎没再遇到降智",
        }
    ]
    finding = {
        "narrative": "shareworks 在 Vibe Friends 重复强调机场共享 IP 导致降智，"
        "称换独享后几乎不再遇到；目前对话中没有可验证的对照，具体事实仍待补。",
    }

    # “重复强调”“具体事实”里的动词不能让副词碎片被当成发言人。
    assert _finding_unsupported_narrative_claims(finding, evidence) == []


def test_narrative_claim_guard_matches_numbers_on_exact_boundaries():
    evidence = [
        {
            "chat_name": "Vibe Friends",
            "sender_name": "甲",
            "content": "额度是 120，当前模型版本是 5.1。",
        }
    ]
    finding = {
        "summary": "甲称额度是 20，当前模型版本是 5。",
        "speakers": [{"name": "甲"}],
    }

    unsupported = _finding_unsupported_narrative_claims(finding, evidence)

    assert {item["token"] for item in unsupported if item["kind"] == "number"} == {
        "20",
        "5",
    }


def test_narrative_claim_guard_treats_suffixed_number_as_one_hard_claim():
    evidence = [
        {
            "chat_name": "Deepthink",
            "sender_name": "pzc163",
            "content": "新模型有效 token 超过 128。",
        }
    ]
    finding = {"summary": "pzc163 称新模型有效 token 超过 128k。"}

    unsupported = _finding_unsupported_narrative_claims(finding, evidence)

    assert {tuple(item.values()) for item in unsupported} == {("number", "128k")}


def test_narrative_claim_guard_checks_two_character_letter_digit_model_names():
    evidence = [
        {
            "chat_name": "模型群",
            "sender_name": "甲",
            "content": "目前讨论的是 k30 模型。",
        }
    ]
    finding = {"summary": "甲称目前讨论的是 k3 模型。"}

    unsupported = _finding_unsupported_narrative_claims(finding, evidence)

    assert {tuple(item.values()) for item in unsupported} == {("identifier", "k3")}


def test_narrative_claim_guard_requires_a_real_later_cross_sender_reply():
    questions_only = [
        {
            "chat_name": "公益站群",
            "sender_name": "甲",
            "timestamp": "2026-09-08T10:00:00+08:00",
            "content": "公益站 API 是否只能用于 Codex？",
        },
        {
            "chat_name": "公益站群",
            "sender_name": "乙",
            "timestamp": "2026-09-08T10:02:00+08:00",
            "content": "其他模型的分组和模型名是什么？",
        },
    ]
    finding = {"summary": "关于公益站 API 的问题已得到群内回复。"}

    unsupported = _finding_unsupported_narrative_claims(finding, questions_only)

    assert {item["kind"] for item in unsupported} == {"reply_completion"}

    with_reply = questions_only + [
        {
            "chat_name": "公益站群",
            "sender_name": "丙",
            "timestamp": "2026-09-08T10:03:00+08:00",
            "content": "只能用于 Codex，其他分组暂时没有。",
        }
    ]
    assert _finding_unsupported_narrative_claims(finding, with_reply) == []


def test_narrative_claim_guard_accepts_source_metadata_but_not_invented_speaker():
    evidence = [
        {
            "chat_name": "Vibe Friends 999",
            "sender_name": "甲",
            "content": "这个现象今天又出现了。",
        }
    ]
    finding = {
        "summary": "Vibe Friends 999 群中有人反馈该现象。",
        "speakers": [{"name": "甲"}, {"name": "夜航星"}],
    }

    unsupported = _finding_unsupported_narrative_claims(finding, evidence)

    assert {tuple(item.values()) for item in unsupported} == {("speaker", "夜航星")}


def test_narrative_claim_guard_requires_cause_and_effect_in_one_message():
    split_evidence = [
        {
            "chat_name": "Vibe Friends",
            "sender_name": "甲",
            "content": "机场共享 IP 的用户很多。",
        },
        {
            "chat_name": "Vibe Friends",
            "sender_name": "乙",
            "content": "最近经常出现降智。",
        },
    ]
    finding = {"narrative": "机场共享 IP 导致降智。"}

    unsupported = _finding_unsupported_narrative_claims(finding, split_evidence)

    assert {item["kind"] for item in unsupported} == {"causality"}

    explicit_evidence = [
        {
            "chat_name": "Vibe Friends",
            "sender_name": "甲",
            "content": "机场共享 IP 导致降智。",
        }
    ]
    assert _finding_unsupported_narrative_claims(finding, explicit_evidence) == []


def test_recovered_generic_account_title_comes_from_first_quote():
    finding = {
        "title": "AI账号：重置与稳定性争议",
        "evidence_refs": ["m-reset"],
        "evidence": [
            {
                "evidence_ref": "m-reset",
                "message_id": "reset-one",
                "sender_name": "甲",
                "chat_name": "模型群",
                "content": "Claude 现在封号不退款了啊。",
            }
        ],
    }

    lead = BridgeRequestHandler._finding_as_lead(finding, True)

    assert lead["title"] == "原文线索：Claude 现在封号不退款了啊"


def test_clean_limitations_repairs_split_fragments_deduplicates_and_limits():
    raw = [
        "我的多条 for_me 候选集中于与种博来",
        "苏彦凯等人的日常与状态确认",
        "已根据证据强度与内容边界筛选",
        "Chat 群中部分消息按对象做了拆分",
        "避免将 Claude/GPT/Codex 串为单一平台事件。",
        "已根据证据强度与内容边界筛选。",
        "额外说明一",
        "额外说明二",
    ]

    cleaned = _clean_limitations(raw, limit=4)

    assert cleaned == [
        "我的多条 for_me 候选集中于与种博来苏彦凯等人的日常与状态确认。",
        "已根据证据强度与内容边界筛选。",
        "Chat 群中部分消息按对象做了拆分；避免将 Claude/GPT/Codex 串为单一平台事件。",
        "额外说明一。",
    ]


def test_leads_merge_only_on_reference_overlap_and_preserve_provenance():
    generic_title = "模型讨论：方案进入调整阶段"
    leads = [
        {
            "kind": "lead",
            "title": generic_title,
            "evidence_refs": ["m-1", "m-2"],
            "quotes": [{"evidence_ref": "m-1", "message_id": "one", "content": "第一条"}],
            "speakers": [{"name": "甲"}],
            "reason": "第一轮",
            "importance": 50,
            "recovered": True,
        },
        {
            "kind": "lead",
            "title": generic_title,
            "evidence_refs": ["m-2", "m-3"],
            "quotes": [{"evidence_ref": "m-3", "message_id": "three", "content": "第三条"}],
            "speakers": [{"name": "乙"}],
            "reason": "第二轮",
            "importance": 60,
            "recovered": True,
        },
        {
            "kind": "lead",
            "title": generic_title,
            "evidence_refs": ["m-9"],
            "quotes": [{"evidence_ref": "m-9", "message_id": "nine", "content": "同名但不同事件"}],
            "reason": "另一事件",
            "importance": 55,
            "recovered": True,
        },
        {
            "kind": "lead",
            "title": generic_title,
            "evidence_refs": ["m-2"],
            "quotes": [{"evidence_ref": "m-2", "message_id": "two", "content": "模型单句"}],
            "reason": "模型稿",
            "importance": 70,
            "recovered": False,
        },
    ]

    merged = BridgeRequestHandler._merge_leads(leads)

    assert len(merged) == 3
    recovered = next(item for item in merged if set(item["evidence_refs"]) == {"m-1", "m-2", "m-3"})
    assert recovered["merged_count"] == 2
    assert recovered["reasons"] == ["第二轮", "第一轮"]
    assert {item["content"] for item in recovered["quotes"]} == {"第一条", "第三条"}
    assert any(item["evidence_refs"] == ["m-9"] for item in merged)
    assert any(item["recovered"] is False and item["evidence_refs"] == ["m-2"] for item in merged)


def test_related_single_leads_promote_to_quote_card_without_cross_topic_merge():
    leads = [
        {
            "kind": "lead",
            "title": "DeepSeek 临时端点",
            "evidence_refs": ["m-1"],
            "quotes": [{
                "evidence_ref": "m-1",
                "message_id": "one",
                "chat_name": "模型群",
                "sender_name": "甲",
                "timestamp": "2026-09-08T10:00:00+08:00",
                "content": "DeepSeek 官方端点可以直接使用 V4.1 Flash 临时模型。",
            }],
            "importance": 65,
            "recovered": False,
        },
        {
            "kind": "lead",
            "title": "DeepSeek 速度反馈",
            "evidence_refs": ["m-2"],
            "quotes": [{
                "evidence_ref": "m-2",
                "message_id": "two",
                "chat_name": "模型群",
                "sender_name": "乙",
                "timestamp": "2026-09-08T10:08:00+08:00",
                "content": "DeepSeek V4.1 Flash 新模型稳定在 400tps 左右。",
            }],
            "importance": 63,
            "recovered": False,
        },
        {
            "kind": "lead",
            "title": "Gemini 体验",
            "evidence_refs": ["m-3"],
            "quotes": [{
                "evidence_ref": "m-3",
                "message_id": "three",
                "chat_name": "模型群",
                "sender_name": "丙",
                "timestamp": "2026-09-08T10:09:00+08:00",
                "content": "Gemini 写文档还是有很多黑话。",
            }],
            "importance": 62,
            "recovered": False,
        },
    ]

    cards, remaining = BridgeRequestHandler._promote_related_leads(leads)

    assert len(cards) == 1
    assert cards[0]["presentation_mode"] == "verified_quote_card"
    assert cards[0]["evidence_refs"] == ["m-1", "m-2"]
    assert [claim["evidence_refs"] for claim in cards[0]["claims"]] == [["m-1"], ["m-2"]]
    assert [item["evidence_refs"] for item in remaining] == [["m-3"]]


def test_related_leads_already_represented_by_cards_are_not_promoted_again():
    leads = [{
        "title": "Codex 登录",
        "importance": 65,
        "evidence_refs": ["m-1", "m-2"],
        "quotes": [
            {
                "evidence_ref": "m-1",
                "message_id": "m-1",
                "chat_name": "工具群",
                "sender_name": "甲",
                "timestamp": "2026-09-08T10:00:00+08:00",
                "content": "Codex 可以用 z.ai 账号登录。",
            },
            {
                "evidence_ref": "m-2",
                "message_id": "m-2",
                "chat_name": "工具群",
                "sender_name": "甲",
                "timestamp": "2026-09-08T10:05:00+08:00",
                "content": "Codex 每周末有免费 token。",
            },
        ],
    }]

    filtered = BridgeRequestHandler._drop_represented_leads(
        leads,
        [{"evidence_refs": ["m-1", "m-2", "m-3"]}],
    )

    assert filtered == []


def test_related_leads_require_same_chat_close_time_and_specific_anchor():
    def lead(ref, chat, timestamp, content):
        return {
            "kind": "lead",
            "title": content,
            "evidence_refs": [ref],
            "quotes": [{
                "evidence_ref": ref,
                "message_id": ref,
                "chat_name": chat,
                "sender_name": "成员",
                "timestamp": timestamp,
                "content": content,
            }],
            "importance": 60,
            "recovered": False,
        }

    cards, remaining = BridgeRequestHandler._promote_related_leads([
        lead("m-1", "甲群", "2026-09-08T10:00:00+08:00", "这个模型速度不错"),
        lead("m-2", "乙群", "2026-09-08T10:05:00+08:00", "这个模型质量不错"),
        lead("m-3", "甲群", "2026-09-08T12:00:00+08:00", "这个模型速度一般"),
    ])

    assert cards == []
    assert len(remaining) == 3


def test_related_leads_do_not_merge_distinct_events_for_same_model_family():
    leads = [
        {
            "title": "Claude 封号退款",
            "importance": 65,
            "evidence_refs": ["m-1"],
            "quotes": [{
                "evidence_ref": "m-1",
                "message_id": "m-1",
                "chat_name": "模型群",
                "sender_name": "甲",
                "timestamp": "2026-09-08T10:00:00+08:00",
                "content": "Claude 现在封号不退款了。",
            }],
        },
        {
            "title": "Claude 额度重置",
            "importance": 65,
            "evidence_refs": ["m-2"],
            "quotes": [{
                "evidence_ref": "m-2",
                "message_id": "m-2",
                "chat_name": "模型群",
                "sender_name": "乙",
                "timestamp": "2026-09-08T10:05:00+08:00",
                "content": "Claude reset 时如果接近自然 reset，五小时限制会让额度用不了。",
            }],
        },
    ]

    cards, remaining = BridgeRequestHandler._promote_related_leads(leads)

    assert cards == []
    assert len(remaining) == 2


def test_rank_leads_caps_noise_but_keeps_private_and_concrete_items():
    leads = []
    for index in range(25):
        leads.append({
            "title": "普通线索%d" % index,
            "importance": 40 + index,
            "evidence_refs": ["m-%d" % index],
            "quotes": [{
                "evidence_ref": "m-%d" % index,
                "content": "普通群聊内容%d" % index,
                "is_group": True,
            }],
        })
    leads.append({
        "title": "私聊安排",
        "importance": 45,
        "evidence_refs": ["private"],
        "quotes": [{"evidence_ref": "private", "content": "请明天确认安排", "is_group": False}],
    })

    ranked = BridgeRequestHandler._rank_leads(leads, limit=15)

    assert len(ranked) == 15
    assert any(item["title"] == "私聊安排" for item in ranked)


def test_rank_leads_drops_short_emotional_fragments_even_with_high_importance():
    leads = [
        {
            "title": "原文线索：困死我了",
            "importance": 90,
            "evidence_refs": ["noise"],
            "quotes": [{"evidence_ref": "noise", "content": "困死我了"}],
        },
        {
            "title": "DeepSeek 临时模型端点",
            "importance": 59,
            "evidence_refs": ["useful"],
            "quotes": [{
                "evidence_ref": "useful",
                "content": "DeepSeek 官方端点可以直接调用 V4.1 Flash 临时模型。",
            }],
        },
    ]

    ranked = BridgeRequestHandler._rank_leads(leads, limit=15)

    assert [item["evidence_refs"] for item in ranked] == [["useful"]]


def test_contextless_gate_drops_single_quote_fragments_and_keeps_named_objects():
    def lead(ref, title, content):
        return {
            "title": title,
            "importance": 60,
            "evidence_refs": [ref],
            "quotes": [{"evidence_ref": ref, "content": content}],
        }

    contextless = [
        lead("speed-drop", "原文线索：速度降低", "从280token/s降低到240token/s"),
        lead("new-model", "原文线索：新模型性能", "新模型稳定400tps左右"),
        lead("future", "原文线索：明天上午", "明天上午应该会说"),
        lead("omitted-object", "Luna处理效果", "可以用Luna处理但效果不好"),
        lead("meta", "原文线索：模型输出", "这只是模型输出的结果"),
    ]
    self_contained = [
        lead("product", "DeepSeek性能", "DeepSeek V4.1稳定在400tps左右"),
        lead("named-pronoun", "DeepSeek端点开放", "这个DeepSeek端点已经开放"),
        lead("person", "张老师确认评审安排", "张老师确认明天上午公布评审结果"),
        lead("task", "网站登录页修复", "请明天完成网站登录页修复"),
    ]

    filtered = BridgeRequestHandler._drop_contextless_leads(
        contextless + self_contained
    )

    assert [item["evidence_refs"][0] for item in filtered] == [
        "product",
        "named-pronoun",
        "person",
        "task",
    ]


def test_select_editorial_cards_caps_at_ten_and_demotes_low_information_or_duplicate_cards():
    def card(index, refs, summary, importance=65, claim_type="reported_claim"):
        return {
            "title": "卡片%d" % index,
            "summary": summary,
            "narrative": summary,
            "core_conclusion": summary,
            "what_changed": summary,
            "why_it_matters": "",
            "importance": importance,
            "confidence": 68,
            "claim_type": claim_type,
            "evidence_refs": list(refs),
            "evidence": [
                {
                    "evidence_ref": ref,
                    "message_id": ref,
                    "chat_name": "测试群",
                    "sender_name": "成员",
                    "timestamp": "2026-09-08T10:%02d:00+08:00" % index,
                    "content": summary,
                }
                for ref in refs
            ],
            "claims": [{"text": summary, "evidence_refs": list(refs)}],
        }

    cards = [
        card(1, ["m-1", "m-2"], "DeepSeek 官方端点与速度反馈"),
        card(2, ["m-1"], "DeepSeek 官方端点"),  # 被第一张覆盖
        card(3, ["m-3", "m-4"], "两位成员重复提出同一个问题", claim_type="question"),
        card(4, ["m-5"], "困死我了", importance=90),
    ]
    cards.extend(
        card(index, ["m-%d" % (index + 10), "m-%d" % (index + 20)], "明确事件%d发生变化" % index)
        for index in range(5, 16)
    )

    selected, demoted = BridgeRequestHandler._select_editorial_cards(cards, limit=10)

    assert len(selected) == 10
    assert selected[0]["evidence_refs"] == ["m-1", "m-2"]
    assert all(item["evidence_refs"] != ["m-1"] for item in selected)
    assert all(item["summary"] != "困死我了" for item in selected)
    assert any(item["evidence_refs"] == ["m-1"] for item in demoted)
    assert any(item["summary"] == "困死我了" for item in demoted)


def test_select_editorial_cards_demotes_repeated_emotional_message_and_incoherent_story():
    emotional = {
        "title": "苏彦凯称困死我了",
        "summary": "苏彦凯称困死我了",
        "importance": 90,
        "confidence": 68,
        "claim_type": "reported_claim",
        "evidence_refs": ["m-1", "m-2"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "苏彦凯", "content": "困死我了"},
            {"evidence_ref": "m-2", "sender_name": "苏彦凯", "content": "困死我了"},
        ],
        "claims": [{"text": "苏彦凯称困死我了", "evidence_refs": ["m-1", "m-2"]}],
    }
    incoherent = {
        "title": "Codex动态",
        "summary": "Codex客户端做了音乐律动；OpenAI赠送6个月20X会员",
        "importance": 80,
        "confidence": 68,
        "claim_type": "reported_claim",
        "evidence_refs": ["m-3", "m-4"],
        "evidence": [
            {"evidence_ref": "m-3", "sender_name": "甲", "content": "Codex客户端搞了一个音乐律动"},
            {"evidence_ref": "m-4", "sender_name": "乙", "content": "OpenAI送了6个月的20X会员"},
        ],
        "claims": [
            {"text": "甲称Codex客户端做了音乐律动", "evidence_refs": ["m-3"]},
            {"text": "乙称OpenAI赠送6个月20X会员", "evidence_refs": ["m-4"]},
        ],
    }

    selected, demoted = BridgeRequestHandler._select_editorial_cards([emotional, incoherent], limit=10)

    assert selected == []
    assert {item["title"] for item in demoted} == {"苏彦凯称困死我了", "Codex动态"}
    split_leads = BridgeRequestHandler._finding_as_leads(incoherent, False)
    assert [item["evidence_refs"] for item in split_leads] == [["m-3"], ["m-4"]]
    assert all(len(item["quotes"]) == 1 for item in split_leads)


def test_select_editorial_cards_demotes_card_with_four_unrelated_opinion_claims():
    card = {
        "title": "High强度讨论",
        "summary": "High正合适；官方默认High；Gemini强于GPT；Gemini文档未核查",
        "importance": 75,
        "confidence": 68,
        "claim_type": "opinion",
        "evidence_refs": ["m-1", "m-2", "m-3", "m-4"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "high正正好"},
            {"evidence_ref": "m-2", "sender_name": "乙", "content": "官方推荐默认high"},
            {"evidence_ref": "m-3", "sender_name": "丙", "content": "Gemini直接爆了gpt全家"},
            {"evidence_ref": "m-4", "sender_name": "丁", "content": "Gemini写文档都是黑话，内容没有核查"},
        ],
        "claims": [
            {"text": "甲称high正合适", "evidence_refs": ["m-1"]},
            {"text": "乙称官方推荐默认high", "evidence_refs": ["m-2"]},
            {"text": "丙称Gemini强于GPT", "evidence_refs": ["m-3"]},
            {"text": "丁称Gemini文档内容未核查", "evidence_refs": ["m-4"]},
        ],
    }

    selected, demoted = BridgeRequestHandler._select_editorial_cards([card], limit=10)

    assert selected == []
    assert demoted == [card]


def test_select_editorial_cards_private_evidence_receives_operator_value_boost():
    def card(title, importance, ref, is_group):
        return {
            "title": title,
            "summary": "已确认具体交付进展",
            "importance": importance,
            "confidence": 68,
            "claim_type": "reported_claim",
            "evidence_refs": [ref],
            "evidence": [{
                "evidence_ref": ref,
                "sender_name": "甲",
                "content": "已确认具体交付进展",
                "is_group": is_group,
            }],
            "claims": [{"text": "已确认具体交付进展", "evidence_refs": [ref]}],
        }

    selected, demoted = BridgeRequestHandler._select_editorial_cards(
        [
            card("群聊交付进展", 70, "group", True),
            card("私聊交付进展", 65, "private", False),
        ],
        limit=1,
    )

    assert [item["title"] for item in selected] == ["私聊交付进展"]
    assert [item["title"] for item in demoted] == ["群聊交付进展"]


@pytest.mark.parametrize(
    "statement",
    [
        "甲认为GPT比国产模型强",
        "甲记得Gemini比GPT强",
        "甲表示网页版倒也能用",
    ],
)
def test_select_editorial_cards_demotes_unanchored_subjective_judgment(statement):
    card = {
        "title": "模型体验观点",
        "summary": statement,
        "importance": 90,
        "confidence": 68,
        "claim_type": "opinion",
        "evidence_refs": ["m-1"],
        "evidence": [{
            "evidence_ref": "m-1",
            "sender_name": "甲",
            "timestamp": "2026-09-08T10:00:00+08:00",
            "content": statement,
        }],
        "claims": [{"text": statement, "evidence_refs": ["m-1"]}],
    }

    selected, demoted = BridgeRequestHandler._select_editorial_cards([card])

    assert selected == []
    assert demoted == [card]


def test_select_editorial_cards_keeps_consensus_and_concrete_operational_anchors():
    def card(title, contents):
        evidence = [
            {
                "evidence_ref": "%s-%d" % (title, index),
                "sender_name": "成员%d" % index,
                "timestamp": "2026-09-08T10:%02d:00+08:00" % index,
                "content": content,
            }
            for index, content in enumerate(contents, 1)
        ]
        return {
            "title": title,
            "summary": "；".join(contents),
            "importance": 70,
            "confidence": 68,
            "claim_type": "opinion",
            "evidence_refs": [item["evidence_ref"] for item in evidence],
            "evidence": evidence,
            "claims": [
                {"text": item["content"], "evidence_refs": [item["evidence_ref"]]}
                for item in evidence
            ],
        }

    cards = [
        card(
            "Superpowers必要性遭质疑",
            [
                "superpowers已经毫无必要",
                "superpowers现在没有必要",
                "superpowers确实不需要",
            ],
        ),
        card("High默认配置建议", ["貌似官方推荐默认使用High"]),
        card("Fable用量信息不可见", ["感觉Fable变耐用，但API拿不到Fable usage"]),
    ]

    selected, demoted = BridgeRequestHandler._select_editorial_cards(cards)

    assert {item["title"] for item in selected} == {item["title"] for item in cards}
    assert demoted == []


def test_split_claim_findings_separates_fable_usage_from_high_reasoning_discussion():
    finding = {
        "title": "Fable与High讨论",
        "summary": "Fable变耐用；API拿不到Fable usage；high强度正合适；官方似乎默认推荐high",
        "evidence_refs": ["m-1", "m-2", "m-3", "m-4"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "Fable变耐用了"},
            {"evidence_ref": "m-2", "sender_name": "甲", "content": "API拿不到Fable usage"},
            {"evidence_ref": "m-3", "sender_name": "甲", "content": "high正合适"},
            {"evidence_ref": "m-4", "sender_name": "乙", "content": "官方推荐默认high"},
        ],
        "claims": [
            {"text": "甲称Fable变耐用", "evidence_refs": ["m-1"]},
            {"text": "甲称API拿不到Fable usage", "evidence_refs": ["m-2"]},
            {"text": "甲称high强度正合适", "evidence_refs": ["m-3"]},
            {"text": "乙称官方似乎默认推荐high", "evidence_refs": ["m-4"]},
        ],
    }

    split = BridgeRequestHandler._split_claim_findings_by_subject([finding])

    assert len(split) == 2
    assert {tuple(item["evidence_refs"]) for item in split} == {
        ("m-1", "m-2"),
        ("m-3", "m-4"),
    }
    assert {item["title"] for item in split} == {
        "Fable体感：更耐用但Usage不可见",
        "推理强度：High被认为正合适",
    }


def test_split_claim_findings_separates_token_question_from_deepseek_dsh_claims():
    finding = {
        "title": "混合讨论",
        "summary": "是否是另一家公司token；ds性能很魔幻；Pro要在DSH中才能发挥",
        "evidence_refs": ["m-1", "m-2", "m-3"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "是另一家公司的token吗"},
            {"evidence_ref": "m-2", "sender_name": "乙", "content": "现在ds的性能很魔幻"},
            {"evidence_ref": "m-3", "sender_name": "乙", "content": "Pro要在deepseek harness（DSH）中才能发挥出来"},
        ],
        "claims": [
            {"text": "甲询问是否是另一家公司的token", "evidence_refs": ["m-1"]},
            {"text": "乙称现在ds性能很魔幻", "evidence_refs": ["m-2"]},
            {"text": "乙称Pro要在deepseek harness（DSH）中才能发挥", "evidence_refs": ["m-3"]},
        ],
    }

    split = BridgeRequestHandler._split_claim_findings_by_subject([finding])

    assert len(split) == 2
    assert {tuple(item["evidence_refs"]) for item in split} == {
        ("m-1",),
        ("m-2", "m-3"),
    }
    assert any(item["title"] == "DeepSeek Pro：DSH被认为是性能前提" for item in split)


def test_split_claim_findings_separates_display_claim_from_kimi_deployment_claims():
    finding = {
        "title": "设备与部署讨论",
        "summary": "显示屏性能比华为手机强；部署无限量Kimi；无图形界面释放更多性能",
        "evidence_refs": ["m-1", "m-2", "m-3"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "显示屏性能比华为手机强"},
            {"evidence_ref": "m-2", "sender_name": "甲", "content": "部署了无限量Kimi"},
            {"evidence_ref": "m-3", "sender_name": "甲", "content": "无图形界面能释放更多性能"},
        ],
        "claims": [
            {"text": "甲称显示屏性能比华为手机强", "evidence_refs": ["m-1"]},
            {"text": "甲称部署了无限量Kimi", "evidence_refs": ["m-2"]},
            {"text": "甲称无图形界面能释放更多性能", "evidence_refs": ["m-3"]},
        ],
    }

    split = BridgeRequestHandler._split_claim_findings_by_subject([finding])

    assert len(split) == 2
    assert {tuple(item["evidence_refs"]) for item in split} == {
        ("m-1",),
        ("m-2", "m-3"),
    }


def test_split_claim_findings_separates_claude_access_risk_from_reset_quota():
    finding = {
        "title": "Claude讨论",
        "evidence_refs": ["m-1", "m-2"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "Claude目前封号不退款"},
            {"evidence_ref": "m-2", "sender_name": "乙", "content": "reset接近自然reset，加上五小时限制，额度用不了"},
        ],
        "claims": [
            {"text": "甲称Claude目前封号不退款", "evidence_refs": ["m-1"]},
            {"text": "乙称Claude reset接近自然reset时，五小时限制会令额度无法使用", "evidence_refs": ["m-2"]},
        ],
    }

    split = BridgeRequestHandler._split_claim_findings_by_subject([finding])

    assert len(split) == 2
    assert {tuple(item["evidence_refs"]) for item in split} == {("m-1",), ("m-2",)}


def test_split_claim_findings_separates_agent_repair_from_codex_model_access():
    finding = {
        "title": "Codex讨论",
        "evidence_refs": ["m-1", "m-2", "m-3", "m-4"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "一般都是agent修agent"},
            {"evidence_ref": "m-2", "sender_name": "甲", "content": "Codex使用第三方模型比较麻烦"},
            {"evidence_ref": "m-3", "sender_name": "甲", "content": "可以用bigmodel或者z.ai账号登录"},
            {"evidence_ref": "m-4", "sender_name": "甲", "content": "每周末有免费token可用"},
        ],
        "claims": [
            {"text": "甲称一般都是agent修agent", "evidence_refs": ["m-1"]},
            {"text": "甲称Codex使用第三方模型比较麻烦", "evidence_refs": ["m-2"]},
            {"text": "甲建议用bigmodel或者z.ai账号登录", "evidence_refs": ["m-3"]},
            {"text": "甲称每周末有免费token可用", "evidence_refs": ["m-4"]},
        ],
    }

    split = BridgeRequestHandler._split_claim_findings_by_subject([finding])

    assert len(split) == 2
    assert {tuple(item["evidence_refs"]) for item in split} == {
        ("m-1",),
        ("m-2", "m-3", "m-4"),
    }
    assert any(item["title"] == "模型接入：Z.ai登录与周末Token" for item in split)


def test_headline_from_summary_turns_kimi_deployment_into_news_headline():
    title = _headline_from_summary(
        "种博来称主席自行部署无限量Kimi，无图形界面能释放更多性能",
        [{"content": "主席自己部署的无限量Kimi"}, {"content": "无图形界面能释放更多性能"}],
    )

    assert title == "Kimi部署：无界面方案释放更多算力"


def test_select_editorial_cards_demotes_single_vague_question_after_subject_split():
    card = {
        "title": "Token来源待确认",
        "summary": "甲询问是否是另一家公司的token",
        "importance": 80,
        "confidence": 68,
        "claim_type": "question",
        "evidence_refs": ["m-1"],
        "evidence": [{"evidence_ref": "m-1", "sender_name": "甲", "content": "是另一家公司的token吗"}],
        "claims": [{"text": "甲询问是否是另一家公司的token", "evidence_refs": ["m-1"]}],
    }

    selected, demoted = BridgeRequestHandler._select_editorial_cards([card], limit=10)

    assert selected == []
    assert demoted == [card]


def test_select_editorial_cards_demotes_vague_flash_question_and_fragment_pair():
    cards = [
        {
            "title": "这才是flash嘛",
            "summary": "这才是flash嘛；这才是真正的flash吗",
            "importance": 70,
            "confidence": 68,
            "claim_type": "question",
            "evidence_refs": ["m-1", "m-2"],
            "evidence": [
                {"evidence_ref": "m-1", "sender_name": "甲", "content": "这才是flash嘛"},
                {"evidence_ref": "m-2", "sender_name": "乙", "content": "这才是真正的flash吗"},
            ],
            "claims": [{"text": "甲称这才是flash嘛", "evidence_refs": ["m-1"]}],
        },
        {
            "title": "Astra片段",
            "summary": "fast astra max；吓哭了",
            "importance": 80,
            "confidence": 68,
            "claim_type": "reported_claim",
            "evidence_refs": ["m-3", "m-4"],
            "evidence": [
                {"evidence_ref": "m-3", "sender_name": "我", "content": "fast astra max"},
                {"evidence_ref": "m-4", "sender_name": "我", "content": "吓哭了"},
            ],
            "claims": [{"text": "我发出fast astra max", "evidence_refs": ["m-3"]}],
        },
    ]

    selected, demoted = BridgeRequestHandler._select_editorial_cards(cards, limit=10)

    assert selected == []
    assert len(demoted) == 2


def test_recovered_leads_split_cross_chat_and_derive_titles_from_evidence():
    generic_title = "模型讨论：方案进入调整阶段"
    finding = {
        "title": generic_title,
        "evidence_refs": ["m-1", "m-2"],
        "evidence": [
            {
                "evidence_ref": "m-1",
                "message_id": "one",
                "chat_name": "模型群",
                "sender_name": "甲",
                "content": "DeepSeek 官方端点可以直接使用临时模型。",
            },
            {
                "evidence_ref": "m-2",
                "message_id": "two",
                "chat_name": "工具群",
                "sender_name": "乙",
                "content": "做了一个聚合搜索 CLI，邀请大家试用反馈。",
            },
        ],
        "speakers": [{"name": "甲"}, {"name": "乙"}],
        "reason": "跨对象恢复",
        "importance": 80,
    }

    leads = BridgeRequestHandler._finding_as_leads(finding, True)

    assert len(leads) == 2
    assert [item["evidence_refs"] for item in leads] == [["m-1"], ["m-2"]]
    assert all(item["title"] != generic_title for item in leads)
    assert "DeepSeek" in leads[0]["title"]
    assert "聚合搜索" in leads[1]["title"]
    assert [item["speakers"] for item in leads] == [
        [{"name": "甲"}],
        [{"name": "乙"}],
    ]


def test_demoted_claim_leads_keep_only_citation_connected_claims_together():
    finding = {
        "title": "混合讨论",
        "evidence_refs": ["m-1", "m-2", "m-3"],
        "evidence": [
            {"evidence_ref": "m-1", "sender_name": "甲", "content": "方案完成初测。"},
            {"evidence_ref": "m-2", "sender_name": "甲", "content": "初测结果已经复核。"},
            {"evidence_ref": "m-3", "sender_name": "乙", "content": "另一产品正在退款。"},
        ],
        "claims": [
            {"text": "甲称方案完成初测", "evidence_refs": ["m-1", "m-2"]},
            {"text": "甲称初测结果已经复核", "evidence_refs": ["m-2"]},
            {"text": "乙称另一产品正在退款", "evidence_refs": ["m-3"]},
        ],
        "speakers": [{"name": "甲"}, {"name": "乙"}],
    }

    leads = BridgeRequestHandler._finding_as_leads(finding, False)

    assert [item["evidence_refs"] for item in leads] == [["m-1", "m-2"], ["m-3"]]
    assert [len(item["quotes"]) for item in leads] == [2, 1]
    assert [item["speakers"] for item in leads] == [[{"name": "甲"}], [{"name": "乙"}]]


def test_recovered_leads_split_distant_or_missing_times_within_same_chat():
    finding = {
        "title": "模型讨论：方案进入调整阶段",
        "evidence_refs": ["m-1", "m-2", "m-3", "m-4"],
        "evidence": [
            {
                "evidence_ref": "m-1",
                "message_id": "one",
                "chat_name": "模型群",
                "sender_name": "甲",
                "timestamp": "2026-09-08T10:00:00+08:00",
                "content": "Codex 额度刚刚重置。",
            },
            {
                "evidence_ref": "m-2",
                "message_id": "two",
                "chat_name": "模型群",
                "sender_name": "乙",
                "timestamp": "2026-09-08T10:30:00+08:00",
                "content": "Codex 重置后的额度已经可复。",
            },
            {
                "evidence_ref": "m-3",
                "message_id": "three",
                "chat_name": "模型群",
                "sender_name": "丙",
                "timestamp": "2026-09-08T11:01:00+08:00",
                "content": "Codex 晚些时候又出现异常。",
            },
            {
                "evidence_ref": "m-4",
                "message_id": "four",
                "chat_name": "模型群",
                "sender_name": "丁",
                "timestamp": "无法解析",
                "content": "Codex 的第三方模型接入比较麻烦。",
            },
        ],
        "speakers": [{"name": name} for name in ("甲", "乙", "丙", "丁")],
        "reason": "本地恢复",
        "importance": 70,
    }

    leads = BridgeRequestHandler._finding_as_leads(finding, True)

    assert [item["evidence_refs"] for item in leads] == [
        ["m-1", "m-2"],
        ["m-3"],
        ["m-4"],
    ]
    assert [set(speaker["name"] for speaker in item["speakers"]) for item in leads] == [
        {"甲", "乙"},
        {"丙"},
        {"丁"},
    ]


def test_recovered_leads_keep_close_utc_z_timestamps_in_one_group():
    finding = {
        "title": "Codex额度：恢复进展",
        "evidence_refs": ["m-1", "m-2"],
        "evidence": [
            {
                "evidence_ref": "m-1",
                "chat_name": "研发群",
                "sender_name": "甲",
                "timestamp": "2026-09-08T02:00:00Z",
                "content": "Codex额度已经重置。",
            },
            {
                "evidence_ref": "m-2",
                "chat_name": "研发群",
                "sender_name": "乙",
                "timestamp": "2026-09-08T02:20:00Z",
                "content": "Codex额度恢复后可以继续使用。",
            },
        ],
    }

    leads = BridgeRequestHandler._finding_as_leads(finding, True)

    assert len(leads) == 1
    assert leads[0]["evidence_refs"] == ["m-1", "m-2"]


def test_recovered_quote_card_requires_linked_same_object_and_event_quotes():
    candidates = {
        "m-1": {
            "_source_message_id": "source-1",
            "content": "Codex 今天的额度又重置了。",
            "sender_name": "甲",
            "chat_name": "研发群",
        },
        "m-2": {
            "_source_message_id": "source-2",
            "content": "Codex 的额度重置时间也提前了。",
            "sender_name": "乙",
            "chat_name": "研发群",
        },
    }
    finding = {
        "title": "Codex重置：额度状态待核实",
        "category": "事件",
        "importance": 70,
        "confidence": 70,
        "evidence_refs": ["m-1", "m-2"],
    }

    card = BridgeRequestHandler._recovered_quote_card(finding, candidates)

    assert card is not None
    assert card["presentation_mode"] == "recovered_quote_card"
    assert card["evidence_refs"] == ["m-1", "m-2"]
    assert [item["content"] for item in card["quotes"]] == [
        candidates["m-1"]["content"],
        candidates["m-2"]["content"],
    ]
    assert card["claim_boundary"] == "仅为聊天原文汇编，未作事实核验。"
    for forbidden in ("summary", "narrative", "what_changed", "why_it_matters", "core_conclusion"):
        assert forbidden not in card


def test_recovered_quote_card_fails_closed_for_duplicates_or_weak_followup():
    duplicate_candidates = {
        "m-1": {"_source_message_id": "same", "content": "Codex 额度重置了。"},
        "m-2": {"_source_message_id": "same", "content": "Codex 额度重置了。"},
    }
    assert BridgeRequestHandler._recovered_quote_card(
        {"evidence_refs": ["m-1", "m-2"]}, duplicate_candidates
    ) is None

    weak_candidates = {
        "m-1": {"_source_message_id": "one", "content": "Codex 额度重置了。"},
        "m-2": {"_source_message_id": "two", "content": "主要他也没承认。"},
    }
    assert BridgeRequestHandler._recovered_quote_card(
        {"evidence_refs": ["m-1", "m-2"]}, weak_candidates
    ) is None
    assert BridgeRequestHandler._recovered_quote_card(
        {"evidence_refs": ["m-1", "missing"]}, weak_candidates
    ) is None


def test_ai_lead_ui_has_no_hidden_fourteen_item_cap_and_groups_provenance():
    app = (Path(__file__).parents[1] / "src" / "wechat_bridge" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "leads.slice(0, 14)" not in app
    assert "模型单句线索" in app
    assert "本地证据补回" in app
    assert "quoteOnly" in app


def test_overview_contains_candidates_quality_and_scope(running_workbench):
    server, _store = running_workbench
    assert server.request_queue_size >= 32
    status, value = _request(
        server,
        "GET",
        "/api/overview?start=2026-08-21&end=2026-08-21",
    )
    assert status == 200
    assert value["summary"]["messages"] == 5
    assert value["events"]
    assert value["highlight_candidates"]
    assert value["pending_candidates"]
    assert value["scope"]["realtime"]["mode"] == "live"
    assert value["scope"]["history"]["mode"] == "history"
    assert "analysis_coverage" in value["quality"]
    assert "capture_completeness_state" in value["quality"]
    assert "discoveries" in value
    assert "discussion_episodes" in value
    assert "situation" in value
    assert "topic_briefs" in value
    assert "primary_insights" in value
    assert "unformed_dynamics" in value
    assert all("summary" in item and "message_ids" in item for item in value["unformed_dynamics"])
    assert all(len(str(item.get("title") or "")) <= 24 for item in value["event_briefs"])
    assert len(value["hourly"]) == 24
    assert "chat_activity" in value["activity"]
    assert value["read_only"] is True


def test_fact_check_is_explicit_cached_and_keeps_chat_local(running_workbench, monkeypatch):
    server, store = running_workbench
    store.ingest(
        _message(
            "m-fact",
            6,
            chat_id="chat-a",
            chat_name="Alice",
            sender_id="alice-id",
            sender_name="Alice",
            content="GitHub 登录需要邮箱。",
        ),
        ReplyDecision(False, "fixture"),
        create_task=False,
    )

    class StubProvider:
        def search(self, query, limit=5):
            assert "Alice" not in query
            assert "chat-a" not in query
            return [
                {
                    "url": "https://github.com/",
                    "title": "GitHub",
                    "snippet": "GitHub account and email information.",
                    "domain": "github.com",
                }
            ]

    monkeypatch.setattr(
        "wechat_bridge.web.HttpSearchProvider",
        lambda endpoint: StubProvider(),
    )
    status, rejected = _request(
        server,
        "POST",
        "/api/fact-check",
        {"start": "2026-08-21", "end": "2026-08-21", "confirm": False},
    )
    assert status == 400
    assert "confirm" in rejected["error"]

    status, value = _request(
        server,
        "POST",
        "/api/fact-check",
        {
            "start": "2026-08-21",
            "end": "2026-08-21",
            "topic": "账号与平台",
            "confirm": True,
            "limit": 2,
        },
    )
    assert status == 200
    assert value["state"] == "checked"
    assert value["claim_count"] >= 1
    assert value["privacy"]

    status, cached = _request(
        server,
        "GET",
        "/api/fact-check?start=2026-08-21&end=2026-08-21&topic=%E8%B4%A6%E5%8F%B7%E4%B8%8E%E5%B9%B3%E5%8F%B0",
    )
    assert status == 200
    assert cached["state"] == "checked"
    assert cached["items"][0]["claim_count"] == value["claim_count"]


def test_voice_transcript_is_included_in_overview_analysis(running_workbench):
    server, store = running_workbench
    voice = _message(
        "voice-course-1",
        6,
        chat_id="chat-teacher",
        chat_name="秦昕老师",
        sender_id="teacher-qin",
        sender_name="秦昕老师",
        sender_name_source="contact_remark",
        message_type="voice",
        content="[语音]",
    )
    store.ingest(voice, ReplyDecision(False, "fixture"), create_task=False)
    transcript = "选课方面，组织中的人工智能属于必修课，其他课程应结合研究方向决定。"
    store.save_voice_transcript(
        voice.message_id,
        status="succeeded",
        transcript=transcript,
        duration_ms=18000,
        confidence=0.96,
        provider="wechat_native",
    )

    status, value = _request(
        server,
        "GET",
        "/api/overview?start=2026-08-21&end=2026-08-21",
    )

    assert status == 200
    assert value["summary"]["voice_total"] == 1
    assert value["summary"]["voice_transcribed"] == 1
    assert value["summary"]["voice_transcript_coverage"] == 1.0
    evidence_text = json.dumps(value, ensure_ascii=False)
    assert "选课" in evidence_text
    assert "秦昕老师" in evidence_text


def test_feed_is_reverse_chronological_and_cursor_paginates(running_workbench):
    server, _store = running_workbench
    path = "/api/feed?start=2026-08-21&end=2026-08-21&limit=2"
    status, first = _request(server, "GET", path)
    assert status == 200
    assert [item["message_id"] for item in first["items"]] == ["m-5", "m-4"]
    assert first["sort"] == "timestamp_desc"
    assert first["has_more"] is True
    assert first["next_cursor"]

    status, second = _request(
        server,
        "GET",
        path + "&cursor=" + first["next_cursor"],
    )
    assert status == 200
    assert [item["message_id"] for item in second["items"]] == ["m-3", "m-2"]
    assert set(item["message_id"] for item in first["items"]).isdisjoint(
        item["message_id"] for item in second["items"]
    )

    status, filtered = _request(
        server,
        "GET",
        "/api/feed?start=2026-08-21&end=2026-08-21&filter=high_signal&limit=10",
    )
    assert status == 200
    assert {item["message_id"] for item in filtered["items"]} == {"m-1", "m-3"}


def test_chats_are_enriched_and_high_signal_is_computed(running_workbench):
    server, _store = running_workbench
    status, value = _request(
        server,
        "GET",
        "/api/chats?start=2026-08-21&end=2026-08-21",
    )
    assert status == 200
    chats = {item["chat_id"]: item for item in value["items"]}
    assert chats["chat-a"]["last_message"] == "补充一下背景"
    assert chats["chat-a"]["last_timestamp"].startswith("2026-08-21T04:00")
    assert chats["chat-a"]["high_signal"] == 1
    assert chats["chat-group"]["is_group"] is True
    assert chats["chat-group"]["capture_state"] == "stale"


def test_contacts_deduplicate_and_never_use_wxid_as_display_name(running_workbench):
    server, _store = running_workbench
    status, value = _request(server, "GET", "/api/contacts")
    assert status == 200
    names = [item["display_name"] for item in value["items"]]
    assert "Alice" in names
    assert names.count("Alice") == 1
    assert all(not name.lower().startswith(("wxid_", "gh_")) for name in names)
    assert all(name not in {"alice-id", "bob-id"} for name in names)


def test_sync_runs_are_persisted_and_send_stays_locked(running_workbench):
    server, _store = running_workbench
    status, value = _request(server, "GET", "/api/sync-runs")
    assert status == 200
    assert value["available"] is True
    assert value["state"] == "available"
    assert value["items"] == []
    assert value["current"]["state"] == "idle"

    status, send = _request(
        server,
        "POST",
        "/api/send-text",
        {"chat_id": "filehelper", "content": "不能发送"},
    )
    assert status == 403
    assert send["error"] == "sending_disabled"


def test_ai_analysis_keeps_local_insights_when_model_returns_no_findings(
    running_workbench, monkeypatch
):
    server, store = running_workbench
    message = IncomingMessage(
        message_id="informative-1",
        chat_id="chat-group",
        chat_name="研发群",
        sender_id="member-2",
        sender_name="小李",
        message_type="text",
        content="我认为 agent harness 的关键是把工具边界和评测指标讲清楚，方便团队后续复用和比较。",
        timestamp=datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc),
        is_self=False,
        is_group=True,
        sender_name_source="group_nickname",
        sender_name_confidence=0.95,
        raw_message={"content": "本地测试"},
        adapter_name="workbench-test",
        adapter_version="test",
    )
    store.ingest(message, ReplyDecision(False, "fixture"), create_task=False)
    for extra in (
        IncomingMessage(
            message_id="informative-2",
            chat_id="chat-group",
            chat_name="研发群",
            sender_id="member-3",
            sender_name="小周",
            message_type="text",
            content="相比只看待办，知识解释和资源链接也应该单独保留。",
            timestamp=datetime(2026, 8, 21, 6, 5, tzinfo=timezone.utc),
            is_self=False,
            is_group=True,
            sender_name_source="group_nickname",
            sender_name_confidence=0.95,
            raw_message={"content": "本地测试"},
            adapter_name="workbench-test",
            adapter_version="test",
        ),
        IncomingMessage(
            message_id="informative-3",
            chat_id="chat-group",
            chat_name="研发群",
            sender_id="member-3",
            sender_name="小周",
            message_type="text",
            content="这类讨论可以帮助我们回看方案脉络和技术依据。",
            timestamp=datetime(2026, 8, 21, 6, 8, tzinfo=timezone.utc),
            is_self=False,
            is_group=True,
            sender_name_source="group_nickname",
            sender_name_confidence=0.95,
            raw_message={"content": "本地测试"},
            adapter_name="workbench-test",
            adapter_version="test",
        ),
    ):
        store.ingest(extra, ReplyDecision(False, "fixture"), create_task=False)

    class EmptyFindingGenerator:
        model = "fake-analysis-model"
        configured = True

        def analyze(self, window, candidates):
            assert candidates
            return {"brief": "", "themes": [], "findings": [], "limitations": []}

    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: EmptyFindingGenerator(),
    )
    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {"start": "2026-08-21", "end": "2026-08-21", "confirm": True},
    )

    assert status == 200
    assert value["source"] == "ai_assisted_with_local_fallback"
    # 本地保底证据以一行线索的形式保留，不再被包装成模板正文卡片。
    assert value["analysis"]["findings"] == []
    leads = value["analysis"]["leads"]
    assert leads
    assert all(item["kind"] == "lead" for item in leads)
    assert any(item["quotes"] for item in leads)
    assert value["analysis"]["discoveries"]
    assert value["analysis"]["discussion_episodes"]


def test_ai_analysis_repairs_a_finding_that_crosses_object_boundaries(
    running_workbench, monkeypatch
):
    server, store = running_workbench
    for message in (
        _message(
            "boundary-ai-1",
            7,
            chat_id="chat-vibe",
            chat_name="Vibe Friends",
            sender_id="w0ngpeng",
            sender_name="w0ngpeng",
            content="GPT又不封号，而且不停重置，用中转站没有性价比啊",
            is_group=True,
        ),
        _message(
            "boundary-wx-1",
            8,
            chat_id="chat-vibe",
            chat_name="Vibe Friends",
            sender_id="member-wx-1",
            sender_name="张若彬",
            content="微信一个是分析，主要还是发消息吧，这都是风控的点",
            is_group=True,
        ),
        _message(
            "boundary-wx-2",
            9,
            chat_id="chat-vibe",
            chat_name="Vibe Friends",
            sender_id="member-wx-2",
            sender_name="在路上",
            content="微信风控很讨厌，而且没法合规，我已经让他们全都转到企业微信了",
            is_group=True,
        ),
        _message(
            "boundary-ai-2",
            10,
            chat_id="chat-deepthink",
            chat_name="Deepthink",
            sender_id="member-codex",
            sender_name="🎧",
            content="CodeX重置了吗？为什么我刚才又重置了？",
            is_group=True,
        ),
    ):
        store.ingest(message, ReplyDecision(False, "fixture"), create_task=False)

    class MixedFindingGenerator:
        model = "fake-boundary-model"
        configured = True

        def analyze(self, window, candidates):
            refs = [
                item["evidence_ref"]
                for item in candidates
                if any(marker in str(item.get("content") or "") for marker in ("GPT", "微信", "Codex", "CodeX"))
            ]
            # Candidate ranking may legitimately omit one low-information
            # follow-up, but the remaining refs still span the two hard
            # domains and must not be rendered as one finding.
            assert len(refs) >= 3
            return {
                "brief": "混合对象测试",
                "themes": [],
                "findings": [
                    {
                        "ref_ids": refs,
                        "title": "账号风控：微信与GPT平台使用受限",
                        "category": "risk",
                        "value_type": "risk",
                        "importance": 80,
                        "confidence": 80,
                        "summary": "模型故意把微信风控和AI重置合成一条。",
                        "narrative": "这是一条跨对象的测试归纳。",
                        "core_conclusion": "不能这样合并。",
                        "keywords": ["账号", "重置"],
                        "what_changed": "测试返回了混合证据。",
                        "why_it_matters": "需要拆开。",
                        "reason": "测试",
                        "uncertainty": "测试",
                        "claim_type": "reported_claim",
                        "claim_basis": "测试",
                        "next_step": "拆分",
                    },
                    {
                        "ref_ids": [
                            ref
                            for ref in refs
                            if "微信" not in next(
                                str(item.get("content") or "")
                                for item in candidates
                                if item["evidence_ref"] == ref
                            )
                        ],
                        "title": "GPT 与 Codex 重置反馈并列",
                        "category": "risk",
                        "value_type": "risk",
                        "importance": 75,
                        "confidence": 70,
                        "summary": "GPT 与 Codex 都出现了重置反馈，但两者仍是独立对象。",
                        "narrative": "两组证据分别支持 GPT 与 Codex 的重置反馈。",
                        "core_conclusion": "同一重置议题可并列呈现，不混入其他平台对象。",
                        "keywords": ["GPT", "Codex", "重置"],
                        "what_changed": "两个服务分别出现重置反馈。",
                        "why_it_matters": "需要分别跟踪两个对象的稳定性。",
                        "reason": "测试",
                        "uncertainty": "仅为用户反馈。",
                        "claim_type": "reported_claim",
                        "claim_basis": "测试",
                        "next_step": "继续观察",
                    },
                ],
                "limitations": [],
            }

    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: MixedFindingGenerator(),
    )
    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {"start": "2026-08-21", "end": "2026-08-21", "confirm": True, "force": True},
    )

    assert status == 200
    assert value["coherence_repaired"] is True
    assert value["source"] == "ai_assisted_with_local_fallback"
    diagnostics = value["validation_diagnostics"]
    assert diagnostics["rejected_count"] == len(diagnostics["rejected_findings"])
    assert diagnostics["reason_counts"] == {
        "evidence_object_missing_in_finding": diagnostics["rejected_count"]
    }
    assert all(
        item["reason_code"] == "evidence_object_missing_in_finding"
        and item["finding"]["summary"] == "模型故意把微信风控和AI重置合成一条。"
        and item["valid_ref_ids"]
        and item["evidence"]
        for item in diagnostics["rejected_findings"]
    )
    assert all(
        stats["accepted"] == 1
        and stats["rejected"] == 1
        and stats["reason_counts"] == {"evidence_object_missing_in_finding": 1}
        for stats in value["draw_stats"]
    )
    findings = value["analysis"]["findings"]
    assert any(
        "GPT" in str(item.get("title") or "")
        and "Codex" in str(item.get("title") or "")
        for item in findings
    )
    leads = value["analysis"]["leads"]
    # 被拒稿不能把微信风控混回 AI 重置稿；证据若已由合规稿覆盖，
    # 本地恢复层无需再生成重复线索。
    for finding in findings:
        evidence_text = " ".join(str(item.get("content") or "") for item in finding.get("evidence") or [])
        assert not ("微信" in evidence_text and ("GPT" in evidence_text or "Codex" in evidence_text))
    for lead in leads:
        quote_text = " ".join(
            str(quote.get("content") or "")
            for quote in lead.get("quotes") or []
        )
        assert not ("微信" in quote_text and ("GPT" in quote_text or "Codex" in quote_text))


def test_ai_analysis_rejects_prose_when_references_point_to_another_object(
    running_workbench, monkeypatch
):
    server, store = running_workbench
    for message in (
        _message(
            "misbound-gpt",
            7,
            chat_id="chat-vibe",
            chat_name="Vibe Friends",
            sender_id="member-gpt",
            sender_name="w0ngpeng",
            content="GPT又不封号，而且不停重置，用中转站没有性价比啊",
            is_group=True,
        ),
        _message(
            "misbound-wx",
            8,
            chat_id="chat-vibe",
            chat_name="Vibe Friends",
            sender_id="member-wx",
            sender_name="在路上",
            content="微信风控很讨厌，而且没法合规，我已经让他们全都转到企业微信了",
            is_group=True,
        ),
        _message(
            "misbound-codex",
            9,
            chat_id="chat-deepthink",
            chat_name="Deepthink",
            sender_id="member-codex",
            sender_name="🎧",
            content="CodeX重置了吗？为什么我刚才又重置了？",
            is_group=True,
        ),
        _message(
            "misbound-codex-2",
            10,
            chat_id="chat-deepthink",
            chat_name="Deepthink",
            sender_id="member-codex-2",
            sender_name="zhw0_0",
            content="Codex在什么都不通知的情况下，重置了。",
            is_group=True,
        ),
    ):
        store.ingest(message, ReplyDecision(False, "fixture"), create_task=False)

    class MisboundFindingGenerator:
        model = "fake-misbound-model"
        configured = True

        def analyze(self, window, candidates):
            gpt_ref = next(
                item["evidence_ref"]
                for item in candidates
                if "GPT" in str(item.get("content") or "")
            )
            return {
                "brief": "对象错绑测试",
                "themes": [],
                "findings": [
                    {
                        "ref_ids": [gpt_ref],
                        "title": "微信与GPT、Codex账号风控",
                        "category": "risk",
                        "value_type": "risk",
                        "importance": 85,
                        "confidence": 85,
                        "summary": "微信风控、GPT重置和Codex重置被模型错误写成一件事。",
                        "narrative": "这段文字故意提到三个对象，但证据只引用GPT消息。",
                        "core_conclusion": "不能把不同对象合并。",
                        "keywords": ["微信", "GPT", "Codex"],
                        "what_changed": "测试返回了对象与证据不一致的归纳。",
                        "why_it_matters": "必须按对象拆开。",
                        "reason": "测试",
                        "uncertainty": "测试",
                        "claim_type": "reported_claim",
                        "claim_basis": "测试",
                        "next_step": "拆分",
                    }
                ],
                "limitations": [],
            }

    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: MisboundFindingGenerator(),
    )
    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {"start": "2026-08-21", "end": "2026-08-21", "confirm": True, "force": True},
    )

    assert status == 200
    assert value["object_boundary_repaired"] is True
    diagnostics = value["validation_diagnostics"]
    assert diagnostics["reason_counts"] == {
        "finding_domain_missing_in_evidence": diagnostics["rejected_count"]
    }
    assert diagnostics["rejected_count"] == len(value["draw_stats"])
    assert all(
        item["reason_code"] == "finding_domain_missing_in_evidence"
        and item["finding_domains"] == ["codex_service", "gpt_service", "wechat"]
        and item["evidence_domains"] == ["gpt_service"]
        and item["unknown_ref_ids"] == []
        for item in diagnostics["rejected_findings"]
    )
    assert all(
        stats["accepted"] == 0
        and stats["rejected"] == 1
        and stats["reason_counts"] == {"finding_domain_missing_in_evidence": 1}
        for stats in value["draw_stats"]
    )
    findings = value["analysis"]["findings"]
    leads = value["analysis"]["leads"]
    # 模型错绑稿件被整体拦截；本地按对象边界重建为线索，不成卡片。
    assert findings == []
    assert any("Codex" in str(item.get("title") or "") for item in leads)
    assert any(
        "微信" in " ".join(str(quote.get("content") or "") for quote in item.get("quotes") or [])
        for item in leads
    )
    for finding in findings:
        text = " ".join(
            str(finding.get(key) or "")
            for key in ("title", "summary", "narrative", "core_conclusion")
        )
        evidence_text = " ".join(
            str(item.get("content") or "")
            for item in finding.get("evidence") or []
        )
        if "微信" in text:
            assert "微信" in evidence_text
        if "Codex" in text or "CodeX" in text:
            assert "Codex" in evidence_text or "CodeX" in evidence_text


def test_ai_analysis_rejects_narrative_claims_without_cited_evidence(
    running_workbench, monkeypatch
):
    server, store = running_workbench
    for message in (
        _message(
            "claim-1",
            7,
            chat_id="chat-deepthink",
            chat_name="Deepthink",
            sender_id="pzc163",
            sender_name="pzc163",
            content="你有有效 token 的测试方案吗？",
            is_group=True,
        ),
        _message(
            "claim-2",
            8,
            chat_id="chat-deepthink",
            chat_name="Deepthink",
            sender_id="pzc163",
            sender_name="pzc163",
            content="我下次可以邀请你参与模型测试",
            is_group=True,
        ),
    ):
        store.ingest(message, ReplyDecision(False, "fixture"), create_task=False)

    class UncitedClaimGenerator:
        model = "fake-claim-model"
        configured = True

        def analyze(self, window, candidates):
            refs = [item["evidence_ref"] for item in candidates]
            assert len(refs) >= 2
            return {
                "brief": "正文夹带未引用细节的测试",
                "themes": [],
                "findings": [
                    {
                        "ref_ids": refs[:2],
                        "title": "模型测试邀请",
                        "category": "event",
                        "value_type": "event",
                        "importance": 70,
                        "confidence": 70,
                        "summary": "pzc163 发出模型测试邀请。",
                        # minicpm5-2B、128 与夜航星均未出现在所引证据中：
                        # 这是叙事夹带，必须整稿拦下而不是带病成卡。
                        "narrative": "pzc163 提到其团队新发的 minicpm5-2B 模型有效 token 超过 128，"
                        "并主动发出测试邀请，夜航星随后表示想参与评测。",
                        "core_conclusion": "测试邀请已发出。",
                        "keywords": ["模型测试"],
                        "what_changed": "pzc163 发出模型测试邀请。",
                        "why_it_matters": "群友可能获得新模型测试资格。",
                        "reason": "测试",
                        "uncertainty": "仅为群内口头说法。",
                        "claim_type": "reported_claim",
                        "claim_basis": "测试",
                        "next_step": "等待邀请落地。",
                        "speakers": [
                            {"name": "pzc163"},
                            {"name": "夜航星"},
                        ],
                    }
                ],
                "limitations": [],
            }

    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: UncitedClaimGenerator(),
    )
    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {"start": "2026-08-21", "end": "2026-08-21", "confirm": True, "force": True},
    )

    assert status == 200
    diagnostics = value["validation_diagnostics"]
    assert diagnostics["reason_counts"] == {
        "narrative_claim_missing_in_evidence": diagnostics["rejected_count"]
    }
    assert all(
        item["reason_code"] == "narrative_claim_missing_in_evidence"
        and {"minicpm5-2B", "128", "夜航星"}
        <= {claim["token"] for claim in item["unsupported_claims"]}
        for item in diagnostics["rejected_findings"]
    )
    assert all(
        stats["accepted"] == 0
        and stats["reason_counts"] == {"narrative_claim_missing_in_evidence": 1}
        for stats in value["draw_stats"]
    )
    # 被拒稿件不发展成卡，只能在本地重建为纯引文线索。
    assert value["analysis"]["findings"] == []
    leads = value["analysis"]["leads"]
    assert any(item["quotes"] for item in leads)
    assert all(
        "minicpm5-2B" not in json.dumps(item, ensure_ascii=False)
        for item in leads
    )


def test_ai_analysis_packet_mode_runs_two_draws_per_packet_and_continues_after_failure(
    running_workbench, monkeypatch
):
    server, _store = running_workbench
    packet_builder_calls = []
    failed_refs = set()

    def fake_packets(candidates, max_items=12, max_span_minutes=45):
        values = list(candidates)
        assert len(values) >= 2
        packet_builder_calls.append((max_items, max_span_minutes))
        midpoint = max(1, len(values) // 2)

        def packet(packet_id, items):
            return {
                "packet_id": packet_id,
                "evidence_refs": [item["evidence_ref"] for item in items],
                "candidates": items,
            }

        # Deliberately return reverse id order; aggregation must be stable.
        result = [packet("packet-b", values[midpoint:]), packet("packet-a", values[:midpoint])]
        failed_refs.add(result[0]["evidence_refs"][0])
        return result

    monkeypatch.setattr(web_module, "build_ai_dialogue_packets", fake_packets)

    class PacketGenerator:
        model = "fake-packet-model"
        configured = True

        def __init__(self):
            self.lock = threading.Lock()
            self.calls = []
            self.active = 0
            self.max_active = 0

        def analyze(self, window, candidates, context):
            refs = tuple(item["evidence_ref"] for item in candidates)
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append((refs, context))
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            if refs and refs[0] in failed_refs:
                raise AnalysisGenerationError("packet failed")
            return {
                "brief": "PACKET_ONLY_BRIEF",
                "situation": "PACKET_ONLY_SITUATION",
                "key_changes": ["PACKET_ONLY_CHANGE"],
                "themes": ["PACKET_ONLY_THEME"],
                "timeline": [
                    {
                        "time": "10:00",
                        "title": "PACKET_ONLY_TIMELINE",
                        "summary": "PACKET_ONLY_TIMELINE_SUMMARY",
                        "ref_ids": [refs[0]],
                    }
                ],
                "findings": [],
                "limitations": ["局部分包限制", "局部分包限制"],
                "_usage": {
                    "prompt_tokens": 10,
                    "input_tokens": 10,
                    "prompt_cache_hit_tokens": 4,
                    "prompt_cache_miss_tokens": 6,
                    "completion_tokens": 5,
                    "output_tokens": 5,
                    "reasoning_tokens": 2,
                    "attempts": 1,
                    "retries": 0,
                },
            }

    generator = PacketGenerator()
    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: generator,
    )

    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {
            "start": "2026-08-21",
            "end": "2026-08-21",
            "confirm": True,
            "force": True,
            "packet_mode": True,
            "packet_draws": 2,
        },
    )

    assert status == 200
    assert packet_builder_calls == [(12, 45)]
    assert value["packet_mode"] is True
    assert value["packet_count"] == 2
    assert value["packet_success_count"] == 1
    assert value["packet_failure_count"] == 1
    assert value["packet_draw_success_count"] == 2
    assert value["packet_draw_failure_count"] == 2
    assert value["packet_config"] == {
        "max_items": 12,
        "max_span_minutes": 45,
        "workers": 4,
        "draws": 2,
    }
    assert len(value["packet_errors"]) == 2
    assert generator.max_active >= 2

    called_refs = [ref for refs, _context in generator.calls for ref in refs]
    assert len(called_refs) == value["candidate_count"] * 2
    assert all(called_refs.count(ref) == 2 for ref in set(called_refs))
    for refs, context in generator.calls:
        allowed = set(refs)
        assert context["user_identity"]
        for key in ("topic_briefs", "discussion_windows", "event_candidates", "unformed_dynamics"):
            assert all(set(item["evidence_refs"]) <= allowed for item in context[key])
    packet_ids = [item.get("packet_id") for item in value["draw_stats"]]
    assert packet_ids == sorted(packet_ids)
    assert [item.get("packet_draw_index") for item in value["draw_stats"]] == [0, 1]
    assert all(item["usage"]["prompt_tokens"] == 10 for item in value["draw_stats"])
    assert value["usage"]["packet"]["prompt_tokens"] == 20
    assert value["usage"]["packet"]["reasoning_tokens"] == 4
    assert value["usage"]["reducer"]["prompt_tokens"] == 0
    assert value["usage"]["total"]["completion_tokens"] == 10
    assert value["analysis"]["masthead"]["usage"] == value["usage"]
    assert value["analysis"]["masthead"]["packet_mode"] is True
    global_fields = json.dumps(
        {
            "brief": value["analysis"]["brief"],
            "situation": value["analysis"]["situation"],
            "key_changes": value["analysis"]["key_changes"],
            "themes": value["analysis"]["themes"],
            "timeline": value["analysis"]["timeline"],
        },
        ensure_ascii=False,
    )
    assert "PACKET_ONLY" not in global_fields
    assert value["analysis"]["limitations"].count("局部分包限制。") == 1


def test_merge_draw_findings_unions_distinct_claims_and_deduplicates_same_claim():
    candidate_by_ref = {
        "m-1": {
            "_source_message_id": "one",
            "chat_name": "模型群",
            "sender_name": "甲",
            "timestamp": "2026-09-08T10:00:00+08:00",
            "content": "DeepSeek 官方端点可以调用 V4.1 Flash。",
        },
        "m-2": {
            "_source_message_id": "two",
            "chat_name": "模型群",
            "sender_name": "乙",
            "timestamp": "2026-09-08T10:02:00+08:00",
            "content": "DeepSeek V4.1 Flash 稳定在 400tps 左右。",
        },
    }
    common = {
        "title": "DeepSeek V4.1 Flash",
        "category": "event",
        "importance": 65,
        "confidence": 68,
        "summary": "DeepSeek V4.1 Flash 新信息",
        "narrative": "DeepSeek V4.1 Flash 新信息",
        "core_conclusion": "DeepSeek V4.1 Flash 新信息",
        "what_changed": "DeepSeek V4.1 Flash 新信息",
        "why_it_matters": "",
        "claim_type": "reported_claim",
    }
    findings = [
        {
            **common,
            "evidence_refs": ["m-1"],
            "claims": [{"text": "DeepSeek 官方端点可以调用 V4.1 Flash。", "evidence_refs": ["m-1"]}],
        },
        {
            **common,
            "evidence_refs": ["m-1", "m-2"],
            "claims": [
                {"text": "DeepSeek 官方端点可以调用 V4.1 Flash。", "evidence_refs": ["m-1"]},
                {"text": "DeepSeek V4.1 Flash 稳定在 400tps 左右。", "evidence_refs": ["m-2"]},
            ],
        },
    ]

    merged = BridgeRequestHandler._merge_draw_findings(findings, candidate_by_ref)

    assert len(merged) == 1
    assert merged[0]["evidence_refs"] == ["m-1", "m-2"]
    assert merged[0]["claims"] == [
        {"text": "DeepSeek 官方端点可以调用 V4.1 Flash。", "evidence_refs": ["m-1"]},
        {"text": "DeepSeek V4.1 Flash 稳定在 400tps 左右。", "evidence_refs": ["m-2"]},
    ]


def test_merge_draw_findings_deduplicates_attribution_paraphrases_on_same_refs():
    candidate_by_ref = {
        "m-1": {
            "_source_message_id": "one",
            "chat_name": "模型群",
            "sender_name": "夜飞鸟",
            "timestamp": "2026-09-08T10:00:00+08:00",
            "content": "gpt6计费异常在任何情况下所有人都会出现。",
        },
    }
    common = {
        "title": "GPT6计费异常",
        "category": "event",
        "importance": 65,
        "confidence": 68,
        "summary": "GPT6计费异常",
        "narrative": "GPT6计费异常",
        "core_conclusion": "GPT6计费异常",
        "what_changed": "GPT6计费异常",
        "why_it_matters": "",
        "claim_type": "reported_claim",
        "evidence_refs": ["m-1"],
    }
    findings = [
        {**common, "claims": [{"text": "夜飞鸟称 gpt6 计费异常在任何情况下所有人都会出现", "evidence_refs": ["m-1"]}]},
        {**common, "claims": [{"text": "夜飞鸟说这个 gpt6 计费异常在任何情况下所有人都会出现。", "evidence_refs": ["m-1"]}]},
    ]

    merged = BridgeRequestHandler._merge_draw_findings(findings, candidate_by_ref)

    assert len(merged) == 1
    assert len(merged[0]["claims"]) == 1


def test_packet_ai_context_drops_aggregate_items_with_any_outside_ref():
    context = {
        "user_identity": {"display_name": "测试用户"},
        "topic_briefs": [
            {"summary": "包内事实", "evidence_refs": ["m-1", "m-2"]},
            {"summary": "混入包外事实", "evidence_refs": ["m-1", "m-9"]},
            {"summary": "没有证据", "evidence_refs": []},
        ],
        "discussion_windows": [],
        "event_candidates": [],
        "unformed_dynamics": [],
    }

    cropped = BridgeRequestHandler._packet_ai_context(context, ["m-1", "m-2"])

    assert cropped["user_identity"] == context["user_identity"]
    assert cropped["topic_briefs"] == [
        {"summary": "包内事实", "evidence_refs": ["m-1", "m-2"]}
    ]


def test_ai_analysis_default_path_does_not_build_packets(running_workbench, monkeypatch):
    server, _store = running_workbench
    calls = []

    def packets_must_not_run(*_args, **_kwargs):
        raise AssertionError("default analysis must not build dialogue packets")

    class DefaultGenerator:
        model = "fake-default-model"
        configured = True

        def analyze(self, window, candidates, context):
            calls.append(tuple(item["evidence_ref"] for item in candidates))
            return {"brief": "", "themes": [], "findings": [], "limitations": []}

    monkeypatch.setattr(web_module, "build_ai_dialogue_packets", packets_must_not_run)
    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: DefaultGenerator(),
    )

    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {
            "start": "2026-08-21",
            "end": "2026-08-21",
            "confirm": True,
            "force": True,
            "draws": 1,
        },
    )

    assert status == 200
    assert len(calls) == 1
    assert len(calls[0]) == value["candidate_count"]
    assert value["packet_mode"] is False
    assert value["packet_count"] == 0
    assert value["packet_success_count"] == 0
    assert value["packet_failure_count"] == 0


def test_non_recovered_lead_replaces_title_not_supported_by_its_quotes():
    lead = BridgeRequestHandler._finding_as_lead(
        {
            "title": "钛合金支架进入量产",
            "evidence_refs": ["m-1"],
            "evidence": [
                {
                    "evidence_ref": "m-1",
                    "message_id": "source-1",
                    "sender_name": "小林",
                    "chat_name": "测试群",
                    "timestamp": "2026-08-21T10:00:00+08:00",
                    "content": "吓哭了",
                }
            ],
        },
        False,
    )

    assert lead["title"] == "原文线索：吓哭了"
    assert "钛合金" not in json.dumps(lead, ensure_ascii=False)


def test_packet_mode_publishes_only_claims_bound_to_their_own_evidence(
    running_workbench, monkeypatch
):
    server, store = running_workbench
    for message in (
        _message(
            "strict-claim-1",
            12,
            chat_id="strict-claim-chat",
            chat_name="项目验收群",
            sender_id="strict-a",
            sender_name="小林",
            content="请今天确认支架样品打磨完成情况",
            is_group=True,
        ),
        _message(
            "strict-claim-2",
            13,
            chat_id="strict-claim-chat",
            chat_name="项目验收群",
            sender_id="strict-b",
            sender_name="小周",
            content="请明天下午安排支架样品验收",
            is_group=True,
        ),
    ):
        store.ingest(message, ReplyDecision(False, "fixture"), create_task=False)

    def strict_packet(candidates, max_items=12, max_span_minutes=45):
        selected = [
            item for item in candidates
            if "支架样品" in str(item.get("content") or "")
        ]
        assert len(selected) == 2
        return [{
            "packet_id": "strict-packet",
            "evidence_refs": [item["evidence_ref"] for item in selected],
            "candidates": selected,
        }]

    class StrictClaimGenerator:
        model = "fake-strict-claim-model"
        configured = True

        def analyze(self, window, candidates, context, packet_mode=False):
            assert packet_mode is True
            refs = [item["evidence_ref"] for item in candidates]
            contents = [str(item["content"]) for item in candidates]
            common = {
                "ref_ids": refs,
                "category": "event",
                "value_type": "event",
                "importance": 70,
                "confidence": 70,
                "claim_type": "reported_claim",
            }
            return {
                "brief": "",
                "themes": [],
                "limitations": [],
                "findings": [
                    {
                        **common,
                        "title": "钛合金支架已量产",
                        "summary": "模型原稿混入了未引用上下文。",
                        "narrative": "钛合金支架已经量产并出口。",
                        "claims": [
                            {"text": contents[0], "evidence_refs": [refs[0]]},
                            {"text": contents[1], "evidence_refs": [refs[1]]},
                            {"text": "钛合金支架已经量产", "evidence_refs": [refs[0]]},
                            {"text": "建议您添加三个模型：", "evidence_refs": [refs[0]]},
                        ],
                    },
                    {
                        **common,
                        "title": "缺少声明的旧格式稿",
                        "summary": contents[0],
                        "narrative": contents[0],
                    },
                ],
            }

    monkeypatch.setattr(web_module, "build_ai_dialogue_packets", strict_packet)
    monkeypatch.setattr(
        BridgeRequestHandler,
        "_ai_generator",
        lambda _handler: StrictClaimGenerator(),
    )
    reducer_usage = {
        "prompt_tokens": 40,
        "input_tokens": 40,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 40,
        "completion_tokens": 12288,
        "output_tokens": 12288,
        "reasoning_tokens": 12288,
        "attempts": 1,
        "retries": 0,
    }
    monkeypatch.setattr(
        web_module,
        "reduce_verified_findings",
        lambda _generator, _window, _findings: {
            "ordered_claim_ids": ["F001"],
            "brief_claim_ids": ["F001"],
            "headlines": {},
            "_usage": reducer_usage,
            "_parse_failed": True,
            "_parse_error": "最终主编没有返回可解析的 JSON",
        },
    )
    status, value = _request(
        server,
        "POST",
        "/api/ai-analysis",
        {
            "start": "2026-08-21",
            "end": "2026-08-21",
            "confirm": True,
            "force": True,
            "packet_mode": True,
        },
    )

    assert status == 200
    assert value["packet_config"]["draws"] == 1
    reducer_meta = value["editorial_reducer"]
    assert reducer_meta["status"] == "fallback"
    assert reducer_meta["message"] == "最终主编没有返回可解析的 JSON"
    assert reducer_meta["usage"] == reducer_usage
    assert value["usage"]["reducer"] == reducer_usage
    cards = [item for item in value["analysis"]["findings"] if item.get("claims")]
    assert len(cards) == 1
    card = cards[0]
    assert [claim["text"] for claim in card["claims"]] == [
        "请今天确认支架样品打磨完成情况",
        "请明天下午安排支架样品验收",
    ]
    rendered = json.dumps(card, ensure_ascii=False)
    assert "钛合金" not in rendered
    assert "出口" not in rendered
    assert all("添加三个模型" not in claim["text"] for claim in card["claims"])
    assert all(item.get("is_group") is True for item in card["evidence"])
    diagnostics = value["validation_diagnostics"]
    assert diagnostics["reason_counts"]["packet_claims_missing"] == 1
    assert diagnostics["rejected_claim_count"] == 2
    assert {item["reason_code"] for item in diagnostics["rejected_claims"]} == {
        "claim_not_directly_supported",
        "claim_incomplete_fragment",
    }
