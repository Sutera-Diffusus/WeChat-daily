from wechat_bridge.source_identity import repair_synced_self_attribution


def row(chat, sender, status, self_sent, *, group=False):
    return {
        "chat_id": chat,
        "is_group": group,
        "is_self": self_sent,
        "sender_id": sender,
        "sender_name": "联系人",
        "raw_message": {"message": {"_bridge_status": status}},
    }


def test_repairs_synced_self_only_after_two_direct_chat_confirmations():
    rows = [
        row("a", "1", 2, True),
        row("b", "1", 2, True),
        row("a", "1", 3, False),
        row("a", "9", 3, False),
        row("group", "1", 3, False, group=True),
    ]
    result = repair_synced_self_attribution(rows)
    assert result[2]["is_self"] is True
    assert result[2]["sender_name"] == "我"
    assert result[2]["_attribution_repaired"] is True
    assert result[3]["is_self"] is False
    assert result[4]["is_self"] is False
    assert rows[2]["is_self"] is False


def test_single_chat_cannot_establish_sender_identity():
    result = repair_synced_self_attribution([
        row("a", "1", 2, True),
        row("a", "1", 3, False),
    ])
    assert result[1]["is_self"] is False


def test_group_repairs_only_same_chat_sender_without_conflicting_nickname():
    conflicting = row("group-a", "1", 3, False, group=True)
    conflicting["sender_name_source"] = "group_nickname"
    result = repair_synced_self_attribution([
        row("group-a", "1", 2, True, group=True),
        row("group-a", "1", 3, False, group=True),
        row("group-b", "1", 3, False, group=True),
        conflicting,
    ])
    assert result[1]["is_self"] is True
    assert result[2]["is_self"] is False
    assert result[3]["is_self"] is False
