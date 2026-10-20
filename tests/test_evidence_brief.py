from wechat_bridge.evidence_brief import build_evidence_cards


def test_evidence_cards_preserve_source_words_and_direction_boundary():
    candidates = {
        "handover": {"support_message_ids": ["late", "early"]},
    }
    messages = {
        "early": {
            "sender_name": "小林", "chat_name": "小林", "is_self": False,
            "timestamp": "2026-09-16T09:00:00+08:00",
            "content": "有一个交接工作要转给你",
        },
        "late": {
            "sender_name": "我", "chat_name": "小林", "is_self": True,
            "timestamp": "2026-09-16T09:01:00+08:00",
            "content": "申请书我当时没有上传过",
        },
    }

    cards = build_evidence_cards(
        [{"topic": "材料交接", "candidate_ids": ["handover"]}],
        candidates,
        messages,
    )

    assert len(cards) == 1
    assert cards[0]["presentation_mode"] == "source_quotes"
    assert [entry["message_id"] for entry in cards[0]["evidence"]] == ["early", "late"]
    assert cards[0]["evidence"][0]["content"] == "有一个交接工作要转给你"
    assert "小林把" not in str(cards[0])
    assert cards[0]["claim_status"] == "chat_record_only"


def test_evidence_cards_fail_closed_when_a_selected_source_is_missing():
    candidates = {"task": {"support_message_ids": ["missing"]}}
    assert build_evidence_cards(
        [{"topic": "任务", "candidate_ids": ["task"]}],
        candidates,
        {},
    ) == []


def test_evidence_cards_allow_only_direct_source_quote_subset():
    candidates = {"task": {"support_message_ids": ["request", "reply"]}}
    messages = {
        "request": {"content": "麻烦永捷做一下道具吧@吴永捷", "timestamp": "2026-09-16T09:00:00+08:00"},
        "reply": {"content": "收到", "timestamp": "2026-09-16T09:01:00+08:00"},
    }
    story = {"topic": "道具", "candidate_ids": ["task"], "message_ids": ["request"]}
    cards = build_evidence_cards([story], candidates, messages)
    assert cards[0]["evidence_message_ids"] == ["request"]
    assert build_evidence_cards(
        [{**story, "message_ids": ["unrelated"]}], candidates, messages
    ) == []


def test_evidence_cards_separate_unrelated_chats_in_one_story():
    candidates = {
        "a": {"support_message_ids": ["first"]},
        "b": {"support_message_ids": ["second"]},
    }
    messages = {
        "first": {"chat_id": "chat-a", "chat_name": "甲", "content": "材料待核对"},
        "second": {"chat_id": "chat-b", "chat_name": "乙", "content": "申请书明天提交"},
    }
    cards = build_evidence_cards(
        [{"topic": "材料", "candidate_ids": ["a", "b"]}], candidates, messages
    )
    assert len(cards) == 2
    assert [card["chat_name"] for card in cards] == ["甲", "乙"]
    assert [card["candidate_ids"] for card in cards] == [["a"], ["b"]]


def test_evidence_cards_redact_embedded_api_key():
    secret = "sk-" + "a" * 40
    cards = build_evidence_cards(
        [{"topic": "工具", "candidate_ids": ["one"]}],
        {"one": {"support_message_ids": ["message"]}},
        {"message": {"chat_id": "g", "chat_name": "群", "content": f"DeepSeek 试用密钥 {secret}"}},
    )
    assert secret not in str(cards)
    assert "[API 密钥]" in cards[0]["evidence"][0]["content"]


def test_voice_transcript_is_marked_as_transcript_not_chat_text():
    cards = build_evidence_cards(
        [{"topic": "出行提醒", "candidate_ids": ["voice"]}],
        {"voice": {"support_message_ids": ["message"]}},
        {"message": {"chat_id": "private", "chat_name": "家人", "message_type": "voice",
                     "content": "安检排队较久", "timestamp": "2026-09-16T09:00:00+08:00"}},
    )
    assert cards[0]["evidence"][0]["source_kind"] == "voice_transcript"
    assert "可能有识别错误" in cards[0]["boundary"]
