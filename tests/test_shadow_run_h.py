"""Synthetic Workstream H checks for v2.8 catalog/API contract."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wechat_bridge.shadow_run_v28 import (
    SHADOW_RUN_V28_ARTIFACT_VERSION,
    SHADOW_RUN_V28_PROVENANCE_VERSION,
    SHADOW_RUN_V28_SCHEMA_VERSION,
    V28_OUTPUT_FILES,
    load_shadow_catalog,
    write_dom_e2e_report,
    write_screenshot_manifest,
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


def _write_catalog(root: Path) -> str:
    analysis_run_id = "shadow-run-h-synthetic"
    run_id = "run-h-synthetic"
    common = {
        "artifact_version": SHADOW_RUN_V28_ARTIFACT_VERSION,
        "analysis_run_id": analysis_run_id,
        "run_id": run_id,
        "source": "llm_pending",
        "source_marker": "llm_pending",
        "provider_status": "failed",
        "llm_accepted": False,
        "fallback_reason": "pending_source_decision",
    }
    manifest = {
        **common,
        "schema_version": SHADOW_RUN_V28_SCHEMA_VERSION,
        "provenance_version": SHADOW_RUN_V28_PROVENANCE_VERSION,
        "model_status": "pending",
        "mode": "real",
        "split": "development",
        "decision_status_counts": {"complete": 1, "pending": 1},
        "thread_count": 1,
        "event_candidate_count": 0,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "output_files": dict(V28_OUTPUT_FILES),
    }
    provenance = {
        **common,
        "provenance_version": SHADOW_RUN_V28_PROVENANCE_VERSION,
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
    payloads = {
        "manifest": manifest,
        "provenance": provenance,
        "run": run_projection,
        "aggregate": {"body_free_outputs": True},
        "api_validation": {"body_free": True},
        "api_dom_e2e": {"body_free": True},
        "screenshot_manifest": {"body_free": True},
    }
    root.mkdir(parents=True, exist_ok=True)
    for key, value in payloads.items():
        (root / V28_OUTPUT_FILES[key]).write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )
    return analysis_run_id


def test_v28_catalog_and_reports_are_body_free(tmp_path):
    catalog = tmp_path / "catalog"
    analysis_run_id = _write_catalog(catalog)
    entry = load_shadow_catalog(catalog)[0]
    assert entry.analysis_run_id == analysis_run_id
    assert entry.artifact_version == SHADOW_RUN_V28_ARTIFACT_VERSION
    assert entry.source == "llm_pending"
    assert entry.provider_status == "failed"
    assert entry.llm_accepted is False
    assert entry.event_candidate_count == 0
    screenshot = write_screenshot_manifest(
        catalog, status="blocked", reason="synthetic_browser_unavailable"
    )
    assert screenshot["status"] == "blocked"
    assert not _body_keys(screenshot)


def test_v28_api_selection_is_explicit_and_unknown_is_404(tmp_path):
    catalog = tmp_path / "catalog"
    analysis_run_id = _write_catalog(catalog)
    service = SimpleNamespace(
        store=SimpleNamespace(path=str(Path(tmp_path) / "bridge.db")),
        policy=SimpleNamespace(timezone_name="Asia/Shanghai"),
    )
    server, _thread = start_dashboard_thread(
        service, port=0, shadow_catalog_path=catalog
    )
    try:
        status, _settings = _request(
            server,
            "/api/settings",
            method="POST",
            body={"shadow_analysis_enabled": True},
        )
        assert status == 200
        selected_status, selected = _request(
            server,
            "/api/shadow-analysis?analysis_run_id=" + analysis_run_id,
        )
        unknown_status, unknown = _request(
            server,
            "/api/shadow-analysis?analysis_run_id=shadow-run-h-unknown",
        )
        assert selected_status == 200
        assert selected["analysis_run_id"] == analysis_run_id
        assert selected["source"] == "llm_pending"
        assert selected["provider_status"] == "failed"
        assert selected["llm_accepted"] is False
        assert unknown_status == 404
        assert unknown["ok"] is False
        assert unknown["fallback_reason"] == "analysis_run_not_found"
        report = write_dom_e2e_report(
            catalog,
            selected=selected,
            unknown=unknown,
            selected_status=selected_status,
            unknown_status=unknown_status,
            dom_checks={"selector_present": True, "selector_value": analysis_run_id},
            screenshot_status="blocked",
        )
        assert report["selected_matches_artifact"] is True
        assert report["unknown_http_404"] is True
        assert report["unknown_run_rejected_without_fallback"] is True
        assert report["body_free"] is True
        assert not _body_keys(selected)
        assert not _body_keys(unknown)
        assert not _body_keys(report)
    finally:
        server.shutdown()
        server.server_close()
