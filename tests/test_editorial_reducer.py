from types import SimpleNamespace

from wechat_bridge.editorial_reducer import _normalize_reduction, reduce_verified_findings


def test_normalize_reduction_never_drops_validated_cards_or_accepts_unknown_ids():
    value = _normalize_reduction(
        {
            "ordered_claim_ids": ["F002", "UNKNOWN", "F002"],
            "brief_claim_ids": ["UNKNOWN", "F002"],
            "headlines": [
                {"claim_id": "UNKNOWN", "title": "不应接受的标题"},
                {"claim_id": "F002", "title": "证据内的短标题"},
            ],
        },
        ["F001", "F002", "F003"],
    )

    assert value["ordered_claim_ids"] == ["F002", "F001", "F003"]
    assert value["brief_claim_ids"] == ["F002"]
    assert value["headlines"] == {"F002": "证据内的短标题"}


def test_reduce_verified_findings_sends_only_validated_card_fields(monkeypatch):
    captured = {}

    def create(**request):
        captured.update(request)
        message = SimpleNamespace(
            content='{"ordered_claim_ids":["F001"],"brief_claim_ids":["F001"],'
            '"headlines":[{"claim_id":"F001","title":"已验证事实标题"}]}'
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(
                prompt_tokens=30,
                prompt_cache_hit_tokens=20,
                prompt_cache_miss_tokens=10,
                completion_tokens=8,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
            ),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    class Generator:
        api_key = "key"
        base_url = "https://example.test"
        model = "test-model"
        max_tokens = 65536
        reasoning_effort = "max"

        @staticmethod
        def _client(_OpenAI):
            return client

    result = reduce_verified_findings(
        Generator(),
        {"start": "2026-09-08", "end": "2026-09-08"},
        [{
            "title": "原始标题",
            "summary": "已经校验的事实",
            "core_conclusion": "有限结论",
            "what_changed": "发生变化",
            "why_it_matters": "值得回看",
            "claim_type": "reported_claim",
            "evidence_refs": ["E001"],
            "evidence": [{"content": "绝不能发送的原始聊天"}],
            "speakers": [{"name": "绝不能发送的人名"}],
        }],
    )

    assert result["ordered_claim_ids"] == ["F001"]
    prompt = captured["messages"][1]["content"]
    assert "已经校验的事实" in prompt
    assert "E001" in prompt
    assert "绝不能发送的原始聊天" not in prompt
    assert "绝不能发送的人名" not in prompt
    assert captured["reasoning_effort"] == "max"
    assert captured["max_tokens"] == 12288
    instructions = captured["messages"][0]["content"]
    assert "《人民日报》《新华社》《先锋》《文汇》" in instructions
    assert "谁说了什么必须保留明确归属" in instructions
    assert "不得为了文风删除发言人" in instructions
    assert "具体故障、明确变更、可行动事实、数字证据" in instructions
    assert "优先于产品使用判断" in instructions
    assert "产品使用判断优先于泛主观比较、问题和片段" in instructions
    assert "前三条重点不得被泛主观比较、问题或片段等低价值内容挤占" in instructions
    assert "禁止新增、补全、合并或改写任何事实" in instructions
    assert result["_usage"]["prompt_tokens"] == 30
    assert result["_usage"]["prompt_cache_hit_tokens"] == 20
    assert result["_usage"]["reasoning_tokens"] == 5


def test_reduce_verified_findings_preserves_usage_and_local_order_when_json_is_missing():
    captured = {}

    def create(**request):
        captured.update(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="只产生了推理内容，没有 JSON",
            ))],
            usage=SimpleNamespace(
                prompt_tokens=40,
                completion_tokens=12288,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=12288),
            ),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    class Generator:
        api_key = "key"
        base_url = "https://example.test"
        model = "test-model"
        max_tokens = 65536
        reasoning_effort = "max"

        @staticmethod
        def _client(_OpenAI):
            return client

    result = reduce_verified_findings(
        Generator(),
        {"start": "2026-09-08", "end": "2026-09-08"},
        [{"title": "第一条"}, {"title": "第二条"}, {"title": "第三条"}, {"title": "第四条"}],
    )

    assert captured["max_tokens"] == 12288
    assert captured["reasoning_effort"] == "max"
    assert result["ordered_claim_ids"] == ["F001", "F002", "F003", "F004"]
    assert result["brief_claim_ids"] == ["F001", "F002", "F003"]
    assert result["headlines"] == {}
    assert result["_parse_failed"] is True
    assert result["_parse_error"] == "最终主编没有返回可解析的 JSON"
    assert result["_usage"]["prompt_tokens"] == 40
    assert result["_usage"]["completion_tokens"] == 12288
    assert result["_usage"]["reasoning_tokens"] == 12288
