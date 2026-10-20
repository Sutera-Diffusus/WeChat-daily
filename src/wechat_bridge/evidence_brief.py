"""Render selected daily-brief stories directly from source messages.

Selection and grouping happen upstream. This layer never paraphrases message
content or infers that a request, transfer, or deadline was completed.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

from .analysis import _display_content, _redact_ai_content


def build_evidence_cards(
    stories: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    messages: Mapping[str, Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    """Make auditable quote cards from selected candidate IDs.

    Missing IDs fail closed for the affected story. The caller may pass a
    model-proposed title, but the card labels it as a topic rather than as a
    factual conclusion. All displayed factual content comes from source text.
    """

    cards: list[Dict[str, Any]] = []
    for story in stories:
        candidate_ids = [str(value) for value in story.get("candidate_ids") or []]
        if not candidate_ids or any(value not in candidates for value in candidate_ids):
            continue
        source_ids = list(dict.fromkeys(
            str(message_id)
            for candidate_id in candidate_ids
            for message_id in candidates[candidate_id].get("support_message_ids") or []
            if str(message_id)
        ))
        if not source_ids or any(message_id not in messages for message_id in source_ids):
            continue
        requested_ids = story.get("message_ids")
        if requested_ids is not None:
            if not isinstance(requested_ids, (list, tuple)) or not requested_ids:
                continue
            chosen_ids = list(dict.fromkeys(str(value) for value in requested_ids))
            if any(message_id not in source_ids for message_id in chosen_ids):
                continue
            source_ids = chosen_ids
        evidence = []
        for message_id in source_ids:
            message = messages[message_id]
            content = _redact_ai_content(_display_content(message.get("content"))).strip()
            if not content:
                continue
            evidence.append({
                "message_id": message_id,
                "sender_name": "我" if message.get("is_self") is True else str(message.get("sender_name") or "待识别成员"),
                "chat_name": str(message.get("chat_name") or ""),
                "timestamp": str(message.get("timestamp") or ""),
                "content": content,
                "source_kind": "voice_transcript" if str(message.get("message_type") or "").lower() == "voice" else "message",
            })
        if not evidence:
            continue
        topic = str(story.get("topic") or "").strip()[:80] or "聊天证据"
        by_chat: Dict[str, list[Dict[str, Any]]] = {}
        for item in evidence:
            message = messages[item["message_id"]]
            chat_key = str(message.get("chat_id") or item["chat_name"])
            by_chat.setdefault(chat_key, []).append(item)
        for chat_evidence in by_chat.values():
            chat_evidence.sort(key=lambda item: (item["timestamp"], item["message_id"]))
            chat_ids = {item["message_id"] for item in chat_evidence}
            local_candidate_ids = [
                candidate_id for candidate_id in candidate_ids
                if chat_ids.intersection(str(mid) for mid in candidates[candidate_id].get("support_message_ids") or [])
            ]
            cards.append({
                "topic": topic,
                "chat_name": chat_evidence[0]["chat_name"],
                "presentation_mode": "source_quotes",
                "candidate_ids": local_candidate_ids,
                "evidence": chat_evidence,
                "evidence_message_ids": [item["message_id"] for item in chat_evidence],
                "claim_status": "chat_record_only",
                "boundary": (
                    "含语音转写，可能有识别错误；其余为聊天原文。未据此推断事项已完成或外部事实。"
                    if any(item["source_kind"] == "voice_transcript" for item in chat_evidence)
                    else "以下为聊天原文；未据此推断事项已完成、平台事实或人物交接方向。"
                ),
            })
    return cards
