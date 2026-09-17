"""Synthetic Workstream G checks for the explicit shadow-run catalog.

The fixture is invented and body-free.  It verifies that a versioned rejected
run can be discovered only through an explicit catalog path, that selection
does not fall back to another run, and that an unknown id is a 404.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wechat_bridge.shadow_run_artifact import (
    OUTPUT_FILES,
    SHADOW_RUN_ARTIFACT_VERSION,
    SHADOW_RUN_PROVENANCE_VERSION,
    SHADOW_RUN_SCHEMA_VERSION,
    load_shadow_run_catalog,
    write_api_validation_report,
)
from wechat_bridge.web import start_dashboard_thread


def _request(server: Any, path: str, *, method: str = "GET", body: Any = None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=3
    )
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def _body_keys(value: Any) -> list[str]:
    body_names = {
        "text",
        "content",
        "body",
        "raw",
        "raw_text",
        "raw_message",
        "message_text",
        "redacted_text",
        "surface",
        "surface_text",
        "fragment_text_redacted",
        "evidence_text",
        "claim_text_redacted",
        "quote",
        "summary",
        "narrative",
        "prompt",
        "response",
    }
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            lower = name.casefold()
            if lower in body_names or lower.endswith(("_text", "_content", "_surface")):
                found.append(name)
            found.extend(_body_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_body_keys(item))
    return found


def _write_synthetic_catalog(root: Path) -> str:
    analysis_run_id = "shadow-run-g-synthetic"
    run_id = "run-g-synthetic"
    common = {
        "artifact_version": SHADOW_RUN_ARTIFACT_VERSION,
        "analysis_run_id": analysis_run_id,
        "run_id": run_id,
        "source": "llm_pending",
        "source_marker": "llm_pending",
        "provider_status": "failed",
        "llm_accepted": False,
        "fallback_reason": "artifact_replay_pending",
    }
    manifest = {
        **common,
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "provenance_version": SHADOW_RUN_PROVENANCE_VERSION,
        "model_status": "pending",
        "mode": "real",
        "split": "development",
        "decision_status_counts": {"complete": 1, "pending": 1},
        "thread_count": 0,
        "event_candidate_count": 0,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
    }
    provenance = {
        **common,
        "provenance_version": SHADOW_RUN_PROVENANCE_VERSION,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
    }
    run_projection = {
        **common,
        "ok": True,
        "model_status": "pending",
        "mode": "real",
        "threads": [],
        "event_candidates": [],
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
    }
    root.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "manifest": manifest,
        "provenance": provenance,
        "run": run_projection,
    }.items():
        (root / OUTPUT_FILES[key]).write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )
    return analysis_run_id


def test_shadow_catalog_loader_is_versioned_and_body_free(tmp_path):
    analysis_run_id = _write_synthetic_catalog(tmp_path / "catalog")
    entries = load_shadow_run_catalog(tmp_path / "catalog")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.analysis_run_id == analysis_run_id
    assert entry.source == "llm_pending"
    assert entry.provider_status == "failed"
    assert entry.llm_accepted is False
    assert entry.event_candidate_count == 0
    assert not _body_keys(entry.to_dict())


def test_shadow_catalog_api_selects_explicit_run_and_rejects_unknown(tmp_path):
    catalog = tmp_path / "catalog"
    analysis_run_id = _write_synthetic_catalog(catalog)
    service = SimpleNamespace(
        store=SimpleNamespace(path=str(Path(tmp_path) / "bridge.db")),
        policy=SimpleNamespace(timezone_name="Asia/Shanghai"),
    )
    server, _thread = start_dashboard_thread(
        service, port=0, shadow_catalog_path=catalog
    )
    try:
        status, settings = _request(
            server,
            "/api/settings",
            method="POST",
            body={"shadow_analysis_enabled": True},
        )
        assert status == 200
        assert settings["settings"]["shadow_analysis_enabled"] is True

        status_selected, selected = _request(
            server,
            "/api/shadow-analysis?analysis_run_id=" + analysis_run_id,
        )
        assert status_selected == 200
        assert selected["analysis_run_id"] == analysis_run_id
        assert selected["source"] == "llm_pending"
        assert selected["provider_status"] == "failed"
        assert selected["llm_accepted"] is False
        assert selected["event_candidate_count"] == 0
        assert selected["read_only"] is True
        assert not _body_keys(selected)

        status_unknown, unknown = _request(
            server,
            "/api/shadow-analysis?analysis_run_id=shadow-run-g-unknown",
        )
        assert status_unknown == 404
        assert unknown["ok"] is False
        assert unknown["fallback_reason"] == "analysis_run_not_found"
        assert unknown["llm_accepted"] is False
        assert not _body_keys(unknown)

        report = write_api_validation_report(
            catalog,
            selected=selected,
            unknown=unknown,
            selected_status=status_selected,
            unknown_status=status_unknown,
        )
        assert report["selected_matches_artifact"] is True
        assert report["selected_http_status_ok"] is True
        assert report["unknown_http_404"] is True
        assert report["unknown_run_rejected_without_fallback"] is True
        assert report["body_free"] is True
        assert not _body_keys(report)
    finally:
        server.shutdown()
        server.server_close()
