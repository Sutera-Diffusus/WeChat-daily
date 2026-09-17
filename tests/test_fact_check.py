from datetime import datetime, timedelta, timezone

from wechat_bridge.analysis import analyze_messages
from wechat_bridge.fact_check import (
    HttpSearchProvider,
    extract_claim_candidates,
    verify_claim,
)


class _Response:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


def test_fact_check_only_extracts_public_factual_candidates():
    values = [
        "Linux.do 要求 GitHub 老号登录，但拒绝 QQ 邮箱注册的账号。",
        "用中转站没有性价比啊，我觉得不值得。",
        "这是不是平台规则？",
    ]
    candidates = extract_claim_candidates(values, topic="账号与平台")
    assert candidates
    assert any("Linux.do" in item["entities"] for item in candidates)
    assert not any("性价比" in item["query"] for item in candidates)
    assert not any(item["claim"].endswith("？") for item in candidates)
    assert all(item["topic"] == "账号与平台" for item in candidates)


def test_http_search_provider_parses_sources_and_verifier_stays_conservative():
    html = """
    <a class="result__a" href="https://example.com/rules">GitHub account rules</a>
    <a class="result__snippet">GitHub account registration and email rules.</a>
    """
    provider = HttpSearchProvider(opener=lambda request, timeout: _Response(html))
    sources = provider.search("GitHub account email", limit=3)
    assert sources[0]["url"] == "https://example.com/rules"
    assert sources[0]["domain"] == "example.com"

    result = verify_claim(
        {
            "claim": "GitHub 要求账号注册遵守邮箱规则",
            "query": "GitHub account email",
            "claim_type": "reported_claim",
        },
        provider,
    )
    assert result["status"] in {"mixed", "supported"}
    assert result["sources"][0]["url"] == "https://example.com/rules"


def test_small_matters_merge_duplicates_and_rewrite_raw_question():
    start = datetime(2026, 8, 25, tzinfo=timezone.utc)
    values = [
        ("m1", "曾雨欣", "用语音问起开学时间，也谈到家人准备一同去广东的安排。", 1),
        ("m2", "曾杭鑫", "用语音问起开学时间，也谈到家人准备一同去广东的安排。", 2),
        (
            "m3",
            "韩劲松 26级博士生",
            "韩劲松26级博士生提到抱歉这么晚打扰你哦~想问下你选课是怎么选的呀，那个通知我有点没看明白。",
            3,
        ),
    ]
    messages = [
        {
            "message_id": message_id,
            "chat_id": chat,
            "chat_name": chat,
            "sender_name": chat,
            "content": content,
            "timestamp": (start + timedelta(minutes=minute)).isoformat(),
            "is_self": False,
            "is_group": False,
            "message_type": "text",
        }
        for message_id, chat, content, minute in values
    ]
    result = analyze_messages(messages, start, start + timedelta(days=1))
    summaries = [item["summary"] for item in result["unformed_dynamics"]]
    duplicate = next(item for item in result["unformed_dynamics"] if item.get("duplicate_count") == 2)
    assert "开学时间" in duplicate["summary"]
    assert "曾雨欣" in duplicate["people"] and "曾杭鑫" in duplicate["people"]
    han = next(summary for summary in summaries if "韩劲松" in summary)
    assert "询问选课方式" in han
    assert "韩劲松26级博士生提到抱歉" not in han
