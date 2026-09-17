"""A mixed, hand-labeled conversation for end-to-end recognition review.

This is intentionally not a unit-test fixture.  It mixes product feedback,
parallel topics, social chatter, risk discussion, repeated tasks, reactions,
media placeholders and a phrase family that changes slightly between uses.
Run it with:

    .venv\\Scripts\\python.exe -c "import sys; sys.path.insert(0, 'src'); sys.path.insert(0, 'tests'); from recognition_audit import print_report; print_report()"
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Set

from wechat_bridge.analysis import (
    _prepare_message_recognition,
    analyze_messages,
)


START = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


def _message(
    message_id: str,
    chat: str,
    content: str,
    minute: int,
    *,
    sender: str,
    group: bool = True,
    message_type: str = "text",
    expected_noise: str = "none",
    expected_value: bool = False,
    expected_action: bool = False,
    expected_review: bool = False,
    expected_meme_family: str = "",
) -> Dict[str, Any]:
    return {
        "message_id": message_id,
        "chat_id": chat,
        "chat_name": chat,
        "sender_name": sender,
        "content": content,
        "timestamp": (START + timedelta(minutes=minute)).isoformat(),
        "is_self": False,
        "is_group": group,
        "message_type": message_type,
        "_audit": {
            "expected_noise": expected_noise,
            "expected_value": expected_value,
            "expected_action": expected_action,
            "expected_review": expected_review,
            "expected_meme_family": expected_meme_family,
        },
    }


def build_audit_messages() -> List[Dict[str, Any]]:
    """Return a deliberately messy approximation of a real discussion day."""

    return [
        _message("p-need", "项目讨论群", "我想解决的不是要看什么，而是不看什么；很多不感兴趣的内容在无形消耗注意力。", 0, sender="甲", expected_value=True),
        _message("p-cocoon", "项目讨论群", "如果只是解决我要看什么，容易形成自己的信息茧房，信息采集不能只向一个方向发散。", 1, sender="乙", expected_value=True),
        _message("p-method", "项目讨论群", "我现在在挑群聊，从群聊信息提取话题点来做，这是第一个项目，请大家多指点。", 2, sender="甲", expected_value=True),
        _message("p-retro", "项目讨论群", "复古路线也许也是独特的路径。", 3, sender="丙", expected_value=True),
        _message("p-pixel", "项目讨论群", "像素风格，我喜欢。", 4, sender="丁", expected_value=True),
        _message("meme-1", "项目讨论群", "未来古法", 5, sender="甲", expected_value=True, expected_meme_family="future-ancient"),
        _message("meme-r1", "项目讨论群", "哈哈哈哈", 6, sender="乙", expected_noise="reaction", expected_meme_family="future-ancient"),
        _message("p-summary", "项目讨论群", "和微信即将出的 AI 群聊总结功能的区别是什么？", 7, sender="戊", expected_value=True),
        _message("p-diff", "项目讨论群", "群聊总结只是把一段聊天缩短，我还想在此基础上把多个群聊和私聊里的同一件事提炼出来归档。", 8, sender="甲", expected_value=True),
        _message("p-meme-goal", "项目讨论群", "我最想实现的是对群里的梗做蒸馏，但现在效果很差。", 9, sender="甲", expected_value=True),
        _message("p-channel", "项目讨论群", "专业的事情不太多出现在微信里，最好能交叉收集会议录音、微信群聊、微信私聊和企业微信聊天。", 10, sender="己", expected_value=True),
        _message("p-line", "项目讨论群", "Line 之前就有群聊总结了，不过只是简单列出几个主题。", 11, sender="庚", expected_value=True),
        _message("risk-site", "项目讨论群", "这个网站把我禁言了，看来平台风控不是小问题。", 12, sender="辛", expected_value=True),
        _message("n-reaction-1", "项目讨论群", "哈哈哈哈哈", 13, sender="壬", expected_noise="reaction"),
        _message("p-recording", "项目讨论群", "目前只能做到跨不同会议的录音追踪同一件事。", 14, sender="癸", expected_value=True),
        _message("p-demand", "项目讨论群", "我也在做这个，看来需求很大。", 15, sender="子", expected_value=True),
        _message("ctx-then", "项目讨论群", "主要是看完了然后呢？", 16, sender="丑", expected_noise="context_fragment", expected_value=True),
        _message("ctx-im", "项目讨论群", "im 是微信？", 17, sender="寅", expected_noise="context_fragment", expected_value=True),
        _message("risk-detect", "项目讨论群", "这能不被风控吗？", 18, sender="卯", expected_value=True),
        _message("risk-send", "项目讨论群", "微信一个是分析，主要还是发消息吧，这都是风控点。", 19, sender="辰", expected_value=True),
        _message("p-accuracy", "项目讨论群", "精确度还在调，有时候筛选上下文模糊会让效果变差。", 20, sender="巳", expected_value=True),
        _message("risk-wechat", "项目讨论群", "微信风控很讨厌，已经让他们转到企业微信了，花点钱换合规。", 21, sender="午", expected_value=True),
        _message("risk-readonly", "项目讨论群", "现在肯定只敢做只读分析，发消息等会被封号就不好玩了。", 22, sender="未", expected_value=True),
        _message("p-feishu", "项目讨论群", "要是能转到飞书就好了。", 23, sender="申", expected_value=True),
        _message("p-speech", "项目讨论群", "这些信息最大的问题是发言人不说人话。", 24, sender="酉", expected_value=True),
        _message("p-noise", "项目讨论群", "群聊里各种噪声、并行、挖坟。", 25, sender="戌", expected_value=True),
        _message("p-abandon", "项目讨论群", "我尝试了一段时间，后面放弃了。", 26, sender="亥", expected_value=True),
        _message("p-effect", "项目讨论群", "没有我想象中那种效果。", 27, sender="甲", expected_value=True),
        _message("p-speaker", "项目讨论群", "群聊还好，可以通过发言人解决。", 28, sender="乙", expected_value=True),
        _message("p-recording-quality", "项目讨论群", "会议录音不好搞，录音质量差，转写质量也不高，还要纠正错别字和分辨发言人。", 29, sender="丙", expected_value=True),
        _message("meme-2", "项目讨论群", "未来古法，低效，也许也是很好的买点。", 30, sender="丁", expected_value=True, expected_meme_family="future-ancient"),
        _message("meme-r2", "项目讨论群", "笑死", 31, sender="乙", expected_noise="reaction", expected_meme_family="future-ancient"),
        _message("p-retro-variant", "项目讨论群", "复古路线也许也是独特的路径。", 32, sender="戊", expected_value=True),
        _message("n-ack", "项目讨论群", "收到", 33, sender="己", expected_noise="acknowledgement"),
        _message("n-greeting", "项目讨论群", "早上好", 34, sender="庚", expected_noise="greeting"),
        _message("n-media", "项目讨论群", "[图片]", 35, sender="辛", message_type="image", expected_noise="media_placeholder"),
        _message("cross-private-1", "项目私聊", "我先把群聊分析做成只读，不发送消息。", 45, sender="甲", expected_value=True, expected_review=True),
        _message("cross-private-2", "项目私聊", "多个群聊和私聊里的同一件事，需要归档到一条主线。", 46, sender="甲", expected_value=True),
        _message("cross-private-3", "项目私聊", "现在的问题是噪音和上下文串线。", 47, sender="甲", expected_value=True),
        _message("cross-risk-1", "风控讨论群", "只读分析也可能触发风控，先限制同步频率。", 60, sender="乙", expected_value=True, expected_review=True),
        _message("cross-risk-2", "风控讨论群", "不要把一天几次写成安全保证，只能说是降低风险的建议。", 61, sender="乙", expected_value=True, expected_review=True),
        _message("cross-risk-3", "风控讨论群", "这条链路先只做本地读取，不自动发消息。", 62, sender="丙", expected_value=True, expected_review=True),
        _message("model-1", "混合群", "模型部署的日志已经上传，接口返回 500。", 75, sender="模型同学", expected_value=True),
        _message("course-1", "混合群", "选课安排的截止时间是周五，大家记得看教务系统。", 76, sender="课程同学", expected_value=True, expected_review=True),
        _message("parallel-generic", "混合群", "请确认一下。", 77, sender="另一个人", expected_noise="context_fragment"),
        _message("parallel-followup", "混合群", "是哪个接口？", 78, sender="另一个人", expected_value=True),
        _message("task-repeat-1", "工作群", "请明天确认报价。", 90, sender="报价同学", expected_value=True, expected_action=True),
        _message("task-repeat-2", "工作群", "请明天确认报价。", 91, sender="财务同学", expected_value=True, expected_action=True),
        _message("family-1", "家人群", "周末吃饭吗", 105, sender="家人甲", expected_noise="social_chatter"),
        _message("family-2", "家人群", "周六到家以后一起聚餐。", 106, sender="家人乙", expected_noise="social_chatter"),
        _message("family-3", "家人群", "到家了吗", 107, sender="家人甲", expected_noise="social_chatter"),
        _message("family-reaction", "家人群", "哈哈", 108, sender="家人乙", expected_noise="reaction"),
    ]


def _ids(items: Iterable[Mapping[str, Any]]) -> Set[str]:
    return {str(item.get("message_id")) for item in items if item.get("message_id") is not None}


def _metrics(expected: Set[str], actual: Set[str]) -> Dict[str, Any]:
    true_positive = len(expected & actual)
    false_positive = len(actual - expected)
    false_negative = len(expected - actual)
    return {
        "expected": len(expected),
        "actual": len(actual),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(true_positive / len(actual), 3) if actual else 1.0 if not expected else 0.0,
        "recall": round(true_positive / len(expected), 3) if expected else 1.0,
    }


def run_audit() -> Dict[str, Any]:
    messages = build_audit_messages()
    prepared = [dict(item) for item in messages]
    meme_candidates = _prepare_message_recognition(prepared, "Asia/Shanghai")
    recognition_by_id = {
        str(item["message_id"]): dict(item.get("_recognition") or {})
        for item in prepared
    }
    result = analyze_messages(messages, START, START + timedelta(days=1))

    expected_noise = {
        str(item["message_id"])
        for item in messages
        if item["_audit"]["expected_noise"] in {
            "reaction", "acknowledgement", "greeting", "media_placeholder", "low_information"
        }
    }
    actual_noise = {
        message_id
        for message_id, recognition in recognition_by_id.items()
        if recognition.get("analysis_role") in {"noise", "archive_only"}
    }
    expected_actions = {
        str(item["message_id"])
        for item in messages
        if item["_audit"]["expected_action"]
    }
    actual_actions = _ids(result["actions"])
    expected_review = {
        str(item["message_id"])
        for item in messages
        if item["_audit"].get("expected_review")
    }
    editorial_review = set(actual_actions)
    editorial_review.update(
        str(message_id)
        for event in result["event_briefs"]
        for message_id in event.get("message_ids") or []
        if message_id
    )
    editorial_review.update(
        str(message_id)
        for dynamic in result["unformed_dynamics"]
        for message_id in dynamic.get("message_ids") or []
        if message_id
    )
    expected_value = {
        str(item["message_id"])
        for item in messages
        if item["_audit"]["expected_value"]
    }
    surfaced_value = actual_actions | _ids(result["discoveries"])
    surfaced_value.update(
        str(message_id)
        for event in result["event_briefs"]
        for message_id in event.get("message_ids") or []
        if message_id
    )
    surfaced_value.update(
        str(message_id)
        for dynamic in result["unformed_dynamics"]
        for message_id in dynamic.get("message_ids") or []
        if message_id
    )
    focus_value = set(actual_actions)
    focus_value.update(
        str(item.get("message_id"))
        for item in result["discoveries"]
        if item.get("message_id")
    )
    focus_value.update(
        str(message_id)
        for event in result["event_briefs"]
        for message_id in event.get("message_ids") or []
        if message_id
    )
    noise_misclassified = []
    for item in messages:
        message_id = str(item["message_id"])
        expected = item["_audit"]["expected_noise"]
        actual = recognition_by_id[message_id].get("noise_class")
        if expected != "none" and expected != actual:
            noise_misclassified.append({"message_id": message_id, "expected": expected, "actual": actual})
        if expected == "none" and actual in {"reaction", "acknowledgement", "greeting", "media_placeholder", "low_information"}:
            noise_misclassified.append({"message_id": message_id, "expected": expected, "actual": actual})

    expected_meme_ids = {
        str(item["message_id"])
        for item in messages
        if item["_audit"].get("expected_meme_family") == "future-ancient"
        and item["_audit"].get("expected_noise") != "reaction"
    }
    actual_meme_ids = {
        str(message_id)
        for candidate in meme_candidates
        for message_id in candidate.get("message_ids") or []
    }
    return {
        "input": {
            "message_count": len(messages),
            "chat_count": len({str(item["chat_id"]) for item in messages}),
            "label_counts": dict(Counter(item["_audit"]["expected_noise"] for item in messages)),
        },
        "noise": {
            "metrics": _metrics(expected_noise, actual_noise),
            "misclassified": noise_misclassified,
            "algorithm_breakdown": result["quality"].get("noise_by_type") or [],
        },
        "actions": {
            "metrics": _metrics(expected_actions, actual_actions),
            "actual": [
                {
                    "message_id": item.get("message_id"),
                    "content": item.get("content"),
                    "reason": item.get("reason"),
                }
                for item in result["actions"]
            ],
        },
        "review_surface": {
            "expected_ids": sorted(expected_review),
            "surfaced_count": len(editorial_review),
            "covered_expected_count": len(expected_review & editorial_review),
            "coverage": round(
                len(expected_review & editorial_review) / len(expected_review), 3
            ) if expected_review else 1.0,
            "missed_expected_ids": sorted(expected_review - editorial_review),
        },
        "value_surface": {
            "metrics": _metrics(expected_value, surfaced_value),
            "focus_metrics": _metrics(expected_value, focus_value),
            "missed_expected_ids": sorted(expected_value - surfaced_value),
            "false_positive_ids": sorted(surfaced_value - expected_value),
            "surface_noise_ids": sorted(surfaced_value & actual_noise),
            "top_discoveries": [
                {
                    "message_id": item.get("message_id"),
                    "kind": item.get("kind"),
                    "content": item.get("content"),
                    "reason": item.get("reason"),
                }
                for item in result["discoveries"][:16]
            ],
            "dynamics": [
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "summary": item.get("summary"),
                    "message_ids": item.get("message_ids"),
                }
                for item in result["unformed_dynamics"]
            ],
        },
        "memes": {
            "expected_family_message_ids": sorted(expected_meme_ids),
            "actual_metrics": _metrics(expected_meme_ids, actual_meme_ids),
            "candidates": meme_candidates,
            "interpretation": "当前审核专门放入了‘未来古法’与‘未来古法，低效’这种变体；如果没有候选，说明精确重复检测对真实梗的召回不足。",
        },
        "context": {
            "parallel_request": next(
                (
                    item for item in result["suppressed_candidates"]
                    if item.get("message_id") == "parallel-generic"
                ),
                None,
            ),
            "parallel_followup": next(
                (
                    item for item in result["suppressed_candidates"]
                    if item.get("message_id") == "parallel-followup"
                ),
                None,
            ),
            "followup_discovery": next(
                (
                    item for item in result["discoveries"]
                    if item.get("message_id") == "parallel-followup"
                ),
                None,
            ),
            "dynamic_subject_leaks": [
                item.get("message_ids") or []
                for item in result["unformed_dynamics"]
                if {"model-1", "course-1"}.issubset(set(item.get("message_ids") or []))
            ],
        },
        "events": [
            {
                "title": item.get("title"),
                "lane": item.get("lane"),
                "confidence": item.get("confidence"),
                "message_ids": item.get("message_ids"),
                "chat_names": [evidence.get("chat_name") for evidence in item.get("evidence") or []],
            }
            for item in result["event_briefs"][:12]
        ],
        "topics": {
            "summary": {
                "topic_count": result["summary"].get("topic_count"),
                "generic_topic_count": result["summary"].get("generic_topic_count"),
                "unclassified_topic_messages": result["summary"].get("unclassified_topic_messages"),
                "topic_specificity_rate": result["summary"].get("topic_specificity_rate"),
            },
            "briefs": [
                {
                    "topic": item.get("topic"),
                    "subtopics": item.get("subtopics") or [],
                    "message_count": item.get("message_count"),
                    "evidence_count": item.get("evidence_count"),
                    "entities": item.get("entities") or [],
                    "detail_points": item.get("detail_points") or [],
                }
                for item in result["topic_briefs"]
            ],
        },
        "funnel": {
            "principle": result["funnel"].get("principle"),
            "stages": result["funnel"].get("stages") or [],
            "exclusive_layers": result["funnel"].get("exclusive_layers") or [],
            "invariant": result["funnel"].get("invariant") or {},
        },
        "result_summary": {
            "summary": result["summary"],
            "quality": result["quality"],
        },
    }


def print_report() -> None:
    import json

    print(json.dumps(run_audit(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    print_report()
