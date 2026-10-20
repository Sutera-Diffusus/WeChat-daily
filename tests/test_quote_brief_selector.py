from wechat_bridge.quote_brief_selector import QuoteSelectionError, select_quote_cards


def candidate(ref, mid, content, *, chat="群聊", group=True):
    return {
        "evidence_ref": ref, "_source_message_id": mid, "content": content,
        "chat_name": chat, "is_group": group, "is_self": False,
        "sender_name": "成员", "timestamp": "2026-09-16T09:00:00+08:00", "context": [],
    }


def messages_for(items):
    return {
        item["_source_message_id"]: {
            "chat_id": item["chat_name"], "chat_name": item["chat_name"],
            "sender_name": item["sender_name"], "content": item["content"],
            "timestamp": item["timestamp"],
        }
        for item in items
    }


def test_general_selector_keeps_useful_cards_outside_profile_topics():
    items = [
        candidate("m-001", "task", "请今天确认项目报价并回复我", chat="工作群"),
        candidate("m-002", "course", "这是聚类分析第二周答疑资料，请查收", chat="课程群"),
        candidate("m-003", "tool", "Claude 用量限额今天发生了变化", chat="技术群"),
    ]

    def model(prompt, inputs):
        assert "不要按预设类别" in prompt
        return {"stories": [{"topic": "聊天信息", "refs": [item["ref"]]} for item in inputs]}

    result = select_quote_cards(items, messages_for(items),
                                {"self_name": "吴永捷", "topics": ["GPT Luna Max"]}, model)
    assert {mid for card in result["cards"] for mid in card["evidence_message_ids"]} == {"task", "course", "tool"}
    assert result["audit"]["candidate_count"] == 3
    assert result["audit"]["quote_count"] == 3


def test_selector_batches_every_candidate_and_preserves_chat_context():
    items = [candidate(f"m-{index:03}", f"id-{index}", f"资料更新 {index}",
                       chat="课程群" if index % 2 else "项目群") for index in range(65)]
    seen = []

    def model(_prompt, batch):
        seen.extend(item["ref"] for item in batch)
        return {"stories": [{"topic": "资料", "refs": [item["ref"]]} for item in batch]}

    result = select_quote_cards(items, messages_for(items), {}, model)
    assert set(seen) == {item["evidence_ref"] for item in items}
    assert result["audit"]["batch_count"] >= 3
    assert max(result["audit"]["batch_input_counts"]) <= 30
    assert result["audit"]["card_count"] == 65


def test_selector_rejects_out_of_batch_and_hallucinated_refs():
    items = [candidate("m-001", "one", "第一项"), candidate("m-002", "two", "第二项")]
    result = select_quote_cards(items, messages_for(items), {}, lambda *_: {
        "stories": [
            {"topic": "越界", "refs": ["m-001", "m-999"]},
            {"topic": "有效", "refs": ["m-002"]},
        ]
    })
    assert [card["evidence_message_ids"] for card in result["cards"]] == [["two"]]
    assert result["audit"]["invalid_story_refs"] == 1


def test_selector_splits_cross_chat_card_and_quotes_source_text():
    items = [candidate("m-001", "one", "第一会话原文", chat="甲"),
             candidate("m-002", "two", "第二会话原文", chat="乙")]
    result = select_quote_cards(items, messages_for(items), {}, lambda _prompt, batch: {
        "stories": [{"topic": "相关信息", "refs": [item["ref"] for item in batch]}]
    })
    assert len(result["cards"]) == 2
    assert {card["evidence"][0]["content"] for card in result["cards"]} == {"第一会话原文", "第二会话原文"}


def test_selector_keeps_explicit_receipt_check_when_model_misses_it():
    item = candidate("m-001", "repair", "您的设备已寄回，收到后先试机，有问题联系我", chat="维修", group=False)
    result = select_quote_cards([item], messages_for([item]), {}, lambda *_: {"stories": []})
    assert [card["evidence_message_ids"] for card in result["cards"]] == [["repair"]]


def test_selector_rejects_whole_day_on_provider_failure():
    item = candidate("m-001", "one", "项目进展")

    def fail(*_):
        raise RuntimeError("provider down")

    try:
        select_quote_cards([item], messages_for([item]), {}, fail)
    except QuoteSelectionError as exc:
        assert str(exc) == "quote_selection_provider_failed"
    else:
        raise AssertionError("provider failure must reject the day")


def test_selector_retries_one_transient_batch_failure():
    item = candidate("m-001", "one", "项目进展")
    attempts = 0

    def flaky(*_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("transient")
        return {"stories": [{"topic": "进展", "refs": ["m-001"]}]}

    result = select_quote_cards([item], messages_for([item]), {}, flaky)
    assert result["audit"]["retried_batches"] == 1
    assert result["audit"]["card_count"] == 1


def test_selector_reports_quota_exhaustion_without_retrying():
    item = candidate("m-001", "one", "项目进展")
    calls = 0

    class QuotaError(RuntimeError):
        status_code = 402

    def exhausted(*_):
        nonlocal calls
        calls += 1
        raise QuotaError("provider quota")

    try:
        select_quote_cards([item], messages_for([item]), {}, exhausted)
    except QuoteSelectionError as exc:
        assert str(exc) == "quote_selection_quota_exhausted"
    else:
        raise AssertionError("quota exhaustion must reject the day")
    assert calls == 1
