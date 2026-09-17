"""Synthetic K27 selection-strata diagnostics.

This suite uses only opaque synthetic metadata.  It never opens a private
message store, frozen input, or provider adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from wechat_bridge.compact_stage_a_development_pilot_v3 import (
    CATEGORY_NAMES,
    analyze_compact_stage_a_selection,
    run_compact_stage_a_development_pilot_v3,
)


def _page(index: int, scope: Mapping[str, str], category: str | None = None, **metadata: Any) -> dict[str, Any]:
    page: dict[str, Any] = {
        "page_id": f"k27-page-{index:02d}",
        "root_id": f"k27-root-{index:02d}",
        "source_packet_id": f"k27-packet-{index:02d}",
        "page_hash": f"{index + 1:064x}",
        "scope": dict(scope),
        "message_handles": [f"synthetic|message|{index:02d}"],
        "primary_message_handles": [f"synthetic|message|{index:02d}"],
        "candidate_handles": [f"synthetic|candidate|{index:02d}"],
        "status": "complete",
    }
    if category is not None:
        page["categories"] = [category]
    page.update(metadata)
    return page


def _assert_body_free(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert "K27_BODY_MARKER" not in encoded
    forbidden = {"body", "content", "message", "prompt", "raw", "response", "text", "user_input"}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in forbidden and child not in (None, "", [], {}):
                    raise AssertionError(f"body-shaped key escaped: {key}")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)


def test_k27_same_page_with_two_labels_is_ambiguous_not_multi_stratum() -> None:
    scope = {"account_id": "k27-account", "chat_id": "k27-chat"}
    report = analyze_compact_stage_a_selection(
        [_page(0, scope, categories=["candidate_competition", "no_reply"])]
    )
    assert report["selected"][0]["categories"] == []
    assert report["classification_counts"]["ambiguous"] == 1
    assert report["ambiguous_page_ids"] == ["k27-page-00"]
    assert report["page_categories_exclusive"] is True


def test_k27_rare_strata_first_is_stable_and_covers_five_pages() -> None:
    scope = {"account_id": "k27-account", "chat_id": "k27-chat"}
    pages = [_page(index, scope, category) for index, category in enumerate(CATEGORY_NAMES)]
    pages.append(_page(99, scope, candidate_reasons=["time_proximity_weak"], body="K27_BODY_MARKER"))
    first = analyze_compact_stage_a_selection(pages)
    second = analyze_compact_stage_a_selection(pages)
    assert first == second
    assert first["selected_page_count"] == 5
    assert first["missing_strata"] == []
    assert first["global_coverage_plan_available"] is True
    assert first["single_scope_coverage_plan_available"] is True
    assert first["selected_stratum_counts"] == {category: 1 for category in CATEGORY_NAMES}
    assert all(len(row["categories"]) <= 1 for row in first["selected"])
    _assert_body_free(first)


def test_k27_missing_metadata_is_transparent_and_counts_future_pages() -> None:
    scope = {"account_id": "k27-account", "chat_id": "k27-chat"}
    pages = [_page(index, scope) for index in range(7)]
    report = analyze_compact_stage_a_selection(pages)
    assert report["page_count"] == 7
    assert report["selectable_page_count"] == 7
    assert report["classified_page_count"] == 0
    assert report["unclassified_page_count"] == 7
    assert report["available_strata"] == []
    assert report["missing_strata"] == list(CATEGORY_NAMES)
    assert report["classification_counts"] == {
        "explicit": 0,
        "derived": 0,
        "ambiguous": 0,
        "metadata_missing": 7,
    }
    assert len(report["selected"]) == 5
    _assert_body_free(report)


def test_k27_union_coverage_requires_explicit_multi_scope_authorization(tmp_path: Path) -> None:
    scope_a = {"account_id": "k27-account", "chat_id": "k27-chat-a"}
    scope_b = {"account_id": "k27-account", "chat_id": "k27-chat-b"}
    pages = [
        *[_page(index, scope_a, category) for index, category in enumerate(CATEGORY_NAMES[:3])],
        *[_page(index + 3, scope_b, category) for index, category in enumerate(CATEGORY_NAMES[3:])],
    ]
    report = analyze_compact_stage_a_selection(pages)
    assert report["global_coverage_plan_available"] is True
    assert report["global_coverage_plan_scope_count"] == 2
    assert report["single_scope_coverage_plan_available"] is False
    assert report["scope_authorization_required"] is True
    assert report["selected_scope_count"] == 1
    assert report["missing_strata"]
    _assert_body_free(report)

    class MustNotCall:
        model_id = "deepseek-v4-flash"
        source = "synthetic-k27"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("multi-scope diagnostic must not call provider")

    model = MustNotCall()
    result = run_compact_stage_a_development_pilot_v3(
        pages,
        tmp_path / "artifact",
        model=model,
        authority_root=tmp_path / "authority",
        settings_sha256="7" * 64,
    )
    assert model.calls == 0
    assert result.provider_calls == 0
    assert result.status == "partial"
    assert "multi_scope_authorization_required" in result.aggregate["errors"]["codes"]
    assert result.aggregate["selection"]["global_coverage_plan_available"] is True
    _assert_body_free(result.to_dict())
