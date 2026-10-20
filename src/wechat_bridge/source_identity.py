"""Conservatively repair synced self messages in copied archive rows.

Some WeChat database rows have status=3 even though their sender index is the
same index used by outgoing status=2 rows across independent direct chats.
The database adapter can only trust status at ingest time; a full-day read can
use the cross-chat evidence without changing the stored archive.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence


def repair_synced_self_attribution(items: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    rows = [dict(item) for item in items]
    confirmed_chats: dict[str, set[str]] = defaultdict(set)
    confirmed_group_pairs: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("is_self") is not True:
            continue
        raw = row.get("raw_message")
        source = raw.get("message") if isinstance(raw, Mapping) else None
        if not isinstance(source, Mapping) or source.get("_bridge_status") != 2:
            continue
        sender_id = str(row.get("sender_id") or "")
        chat_id = str(row.get("chat_id") or "")
        if sender_id and chat_id:
            if row.get("is_group") is True:
                confirmed_group_pairs.add((chat_id, sender_id))
            else:
                confirmed_chats[sender_id].add(chat_id)

    # Two independent direct chats are needed so one malformed thread cannot
    # establish an account-wide identity mapping.
    self_ids = {sender_id for sender_id, chats in confirmed_chats.items() if len(chats) >= 2}
    if not self_ids and not confirmed_group_pairs:
        return rows
    for row in rows:
        if row.get("is_self") is not False:
            continue
        sender_id = str(row.get("sender_id") or "")
        if row.get("is_group") is True:
            if (str(row.get("chat_id") or ""), sender_id) not in confirmed_group_pairs:
                continue
            if row.get("sender_name_source") == "group_nickname":
                continue
        elif sender_id not in self_ids:
            continue
        raw = row.get("raw_message")
        source = raw.get("message") if isinstance(raw, Mapping) else None
        if not isinstance(source, Mapping) or source.get("_bridge_status") != 3:
            continue
        row["is_self"] = True
        row["sender_name"] = "我"
        row["sender_name_source"] = "cross_chat_synced_self"
        row["sender_name_confidence"] = 0.95
        row["_attribution_repaired"] = True
    return rows
