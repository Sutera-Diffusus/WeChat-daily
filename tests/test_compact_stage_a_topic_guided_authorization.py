"""Synthetic and adversarial tests for the fresh topic-guided contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from wechat_bridge.compact_stage_a_development_pilot_v3 import (
    AUTHORITY_BOUND_CANONICAL_MAPPING,
    AUTHORITY_BOUND_SCOPE,
    CATEGORY_NAMES,
    CompactStageADevelopmentError,
    TOPIC_GUIDED_ARTIFACT_NAMESPACE,
    TOPIC_GUIDED_AUTHORIZATION_ID,
    TOPIC_GUIDED_MAX_PROVIDER_CALLS,
    TOPIC_GUIDED_MAX_SELECTED_PAGES,
    TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
    TOPIC_GUIDED_SETTINGS_SHA256,
    run_compact_stage_a_development_pilot_v3,
)


def _synthetic_authority_pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for index, expected in enumerate(AUTHORITY_BOUND_CANONICAL_MAPPING):
        scope = dict(AUTHORITY_BOUND_SCOPE)
        message = f"{scope['account_id']}/{scope['chat_id']}|message|topic-guided-{index}"
        candidate = f"{scope['account_id']}/{scope['chat_id']}|candidate|topic-guided-{index}"
        pages.append(
            {
                "page_id": str(expected["source_page_ref"]),
                "root_id": str(expected["source_root_ref"]),
                "source_packet_id": str(expected["source_source_ref"]),
                "page_hash": str(expected["source_page_hash"]),
                "scope": scope,
                "message_handles": [message],
                "primary_message_handles": [message],
                "message_rows": [{"message_handle": message, "message_type": "text", "text": "synthetic-local-body"}],
                "candidate_handles": [candidate],
                "categories": [CATEGORY_NAMES[index % len(CATEGORY_NAMES)]],
                "status": "complete",
                "_selection_rank": int(expected["selection_rank"]),
                "_source_handle": str(expected["source_source_ref"]),
                "_scope_handle": str(expected["source_scope_ref"]),
            }
        )
    return pages


class _FakeTopicModel:
    model_id = "deepseek-v4-flash"
    source = "synthetic-topic-guided"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt: str, request: Mapping[str, Any], *, max_output_tokens: int) -> Any:
        self.calls += 1
        primary = [row["i"] for row in request["h"] if row.get("k") == "m" and row.get("r") == "p"]
        context = [row["i"] for row in request["h"] if row.get("k") == "m" and row.get("r") == "c"]
        return {
            "topics": [
                {
                    "topic_id": f"synthetic-topic-{self.calls}",
                    "primary_message_ids": primary,
                    "context_message_ids": context,
                    "uncertainty": "unknown",
                }
            ]
        }


def test_topic_guided_historical_code_hash_is_fail_closed_without_reserve(tmp_path: Path) -> None:
    """The historical authorization cannot be resumed after protocol edits.

    The source code changed for the offline context/token repair, while the
    old literal ``TOPIC_GUIDED_CODE_SHA256`` remains intentionally immutable.
    This test proves the stale authorization stops before creating a ledger or
    making a provider call; it must not be "fixed" by rewriting that hash.
    """

    authority = tmp_path / "authority"
    first_model = _FakeTopicModel()
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            _synthetic_authority_pages(),
            tmp_path / "first",
            model=first_model,
            authority_root=authority,
            settings_sha256=TOPIC_GUIDED_SETTINGS_SHA256,
            authorization_id=TOPIC_GUIDED_AUTHORIZATION_ID,
        )
    assert exc_info.value.code == "topic_guided_protocol_drift"
    assert first_model.calls == 0
    # ``ledger_path_for`` creates its root as a convenience, so checking it
    # here would mutate the very pre-reserve boundary this test protects.
    # An absent authority root proves that neither the ledger nor its parent
    # was created.
    assert not authority.exists()


def test_topic_guided_hash_assertion_is_rejected_before_reserve(tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    output = tmp_path / "forged"
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            _synthetic_authority_pages(),
            output,
            model=_FakeTopicModel(),
            authority_root=authority,
            settings_sha256=TOPIC_GUIDED_SETTINGS_SHA256,
            authorization_id=TOPIC_GUIDED_AUTHORIZATION_ID,
            topic_guided_grouping_hints_sha256="0" * 64,
        )
    assert exc_info.value.code == "topic_guided_grouping_hint_drift"
    assert not output.exists()
    assert not authority.exists()


def test_topic_guided_source_page_drift_is_rejected_without_ledger(tmp_path: Path) -> None:
    pages = _synthetic_authority_pages()
    pages[0]["page_hash"] = "f" * 64
    authority = tmp_path / "authority"
    output = tmp_path / "drifted"
    with pytest.raises(CompactStageADevelopmentError) as exc_info:
        run_compact_stage_a_development_pilot_v3(
            pages,
            output,
            model=_FakeTopicModel(),
            authority_root=authority,
            settings_sha256=TOPIC_GUIDED_SETTINGS_SHA256,
            authorization_id=TOPIC_GUIDED_AUTHORIZATION_ID,
        )
    assert exc_info.value.code == "authority_projection_page_drift"
    assert not output.exists()
    assert not authority.exists()
