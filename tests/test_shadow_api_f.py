"""Synthetic Workstream F checks for the review-only shadow API boundary.

The fixtures in this file are invented.  They exercise settings, the local
HTTP envelope, explicit run selection, and body-free projections without
loading provider data or any private/frozen artifact.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wechat_bridge.discourse_event_candidates import DiscourseEventResult
from wechat_bridge.shadow_semantic import (
    SOURCE_LLM_ACCEPTED,
    ShadowSemanticRunResult,
)
from wechat_bridge.settings import WorkbenchSettings
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
            if name.casefold() in body_names or name.casefold().endswith(
                ("_text", "_content", "_surface")
            ):
                found.append(name)
            found.extend(_body_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_body_keys(item))
    return found


def _synthetic_run() -> ShadowSemanticRunResult:
    return ShadowSemanticRunResult(
        run_id="run-f-synthetic-001",
        analysis_run_id="analysis-f-synthetic-001",
        source_marker=SOURCE_LLM_ACCEPTED,
        model_status="accepted",
        mode="fake",
        input_sha256="hash-f-synthetic-001",
        cache_key="cache-f-synthetic-001",
        replay_key="replay-f-synthetic-001",
        feature_flags={"shadow_enabled": True},
        discourse_result=DiscourseEventResult(),
    )


def test_shadow_setting_is_explicit_and_fail_closed(tmp_path):
    settings = WorkbenchSettings(str(tmp_path / "settings.json"))
    assert settings.snapshot()["shadow_analysis_enabled"] is False
    assert settings.update({"shadow_analysis_enabled": "true"})[
        "shadow_analysis_enabled"
    ] is False
    assert settings.update({"shadow_analysis_enabled": True})[
        "shadow_analysis_enabled"
    ] is True
    assert WorkbenchSettings(str(tmp_path / "settings.json")).public()[
        "shadow_analysis_enabled"
    ] is True


def test_shadow_api_is_disabled_by_default_and_body_free(tmp_path):
    service = SimpleNamespace(
        store=SimpleNamespace(path=str(Path(tmp_path) / "bridge.db")),
        policy=SimpleNamespace(timezone_name="Asia/Shanghai"),
    )
    server, _thread = start_dashboard_thread(service, port=0)
    try:
        status, value = _request(
            server,
            "/api/shadow-analysis?analysis_run_id=analysis-f-synthetic-unknown",
        )
        assert status == 200
        assert value["analysis_run_id"] == "analysis-f-synthetic-unknown"
        assert value["source"] == "shadow_disabled"
        assert value["provider_status"] == "disabled"
        assert value["llm_accepted"] is False
        assert value["fallback_reason"] == "shadow_analysis_disabled"
        assert set(value["window"]) == {"start", "end", "timezone"}
        assert not _body_keys(value)
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_api_requires_explicit_run_and_never_falls_back_to_latest(tmp_path):
    service = SimpleNamespace(
        store=SimpleNamespace(path=str(Path(tmp_path) / "bridge.db")),
        policy=SimpleNamespace(timezone_name="Asia/Shanghai"),
    )
    server, _thread = start_dashboard_thread(service, port=0)
    try:
        status, settings = _request(
            server,
            "/api/settings",
            method="POST",
            body={"shadow_analysis_enabled": True},
        )
        assert status == 200
        assert settings["settings"]["shadow_analysis_enabled"] is True
        server.shadow_run_store.append(_synthetic_run())

        status, index = _request(server, "/api/shadow-analysis")
        assert status == 200
        assert index["analysis_run_id"] == "shadow-index"
        assert index["provider_status"] == "configured"
        assert index["llm_accepted"] is False
        assert index["available_run_ids"] == ["analysis-f-synthetic-001"]

        status, unknown = _request(
            server, "/api/shadow-analysis?analysis_run_id=analysis-f-unknown"
        )
        assert status == 404
        assert unknown["ok"] is False
        assert unknown["analysis_run_id"] == "analysis-f-unknown"
        assert unknown["fallback_reason"] == "analysis_run_not_found"
        assert unknown["provider_status"] == "failed"
        assert unknown["llm_accepted"] is False

        status, selected = _request(
            server,
            "/api/shadow-analysis?analysis_run_id=analysis-f-synthetic-001",
        )
        assert status == 200
        assert selected["analysis_run_id"] == "analysis-f-synthetic-001"
        assert selected["provider_status"] == "succeeded"
        assert selected["llm_accepted"] is True
        assert selected["selected_analysis_run_id"] == "analysis-f-synthetic-001"
        assert selected["read_only"] is True
        assert not _body_keys(selected)
    finally:
        server.shutdown()
        server.server_close()
