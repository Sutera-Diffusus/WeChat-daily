"""Independent Workstream E contract checks for the shadow API and selectors.

These checks are deliberately synthetic and source-level.  They do not import
the production web server, call a provider, or open private/frozen artifacts.
The envelope/selector assertions freeze the boundary that production code must
implement; the source assertions are expected to fail until that boundary is
wired into the server and review UI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEB_PATH = ROOT / "src" / "wechat_bridge" / "web.py"
ANALYSIS_PATH = ROOT / "src" / "wechat_bridge" / "analysis.py"
AI_PATH = ROOT / "src" / "wechat_bridge" / "ai.py"
APP_PATH = ROOT / "src" / "wechat_bridge" / "web" / "app.js"
HTML_PATH = ROOT / "src" / "wechat_bridge" / "web" / "index.html"

PROVIDER_STATUSES = frozenset(
    {"disabled", "configured", "succeeded", "blocked", "failed"}
)
FALLBACK_SOURCES = frozenset({"rules_fallback", "local_rules_fallback"})


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _validate_shadow_envelope(value: Mapping[str, Any]) -> None:
    """Validate only the public, body-free shadow response envelope."""

    required = {"analysis_run_id", "source", "provider_status", "llm_accepted"}
    assert required <= set(value), f"missing shadow envelope fields: {required - set(value)}"
    assert isinstance(value["analysis_run_id"], str) and value["analysis_run_id"]
    assert isinstance(value["source"], str) and value["source"]
    assert value["provider_status"] in PROVIDER_STATUSES
    assert isinstance(value["llm_accepted"], bool)


def _selector_accepts_llm(value: Mapping[str, Any]) -> bool:
    """Reference predicate: source is descriptive, acceptance is explicit."""

    return (
        value.get("llm_accepted") is True
        and value.get("provider_status") == "succeeded"
        and value.get("source") not in FALLBACK_SOURCES
    )


def test_synthetic_shadow_envelope_separates_source_and_acceptance():
    """Composite sources are allowed, while rules fallback is never accepted."""

    accepted_composite = {
        "analysis_run_id": "run-synthetic-001",
        "source": "shadow_llm+rules_repair",
        "provider_status": "succeeded",
        "llm_accepted": True,
        "fallback_reason": None,
    }
    rejected_fallback = {
        "analysis_run_id": "run-synthetic-002",
        "source": "rules_fallback",
        "provider_status": "blocked",
        "llm_accepted": False,
        "fallback_reason": "provider_blocked",
    }

    _validate_shadow_envelope(accepted_composite)
    _validate_shadow_envelope(rejected_fallback)
    assert _selector_accepts_llm(accepted_composite)
    assert not _selector_accepts_llm(rejected_fallback)


def test_shadow_api_declares_run_source_provider_and_acceptance_fields():
    """The server must expose one explicit shadow endpoint and its envelope."""

    web = _text(WEB_PATH)
    assert re.search(r"/api/shadow(?:-analysis|/analysis)", web)
    for field in ("analysis_run_id", "provider_status", "llm_accepted"):
        assert field in web, f"shadow API does not expose {field}"
    assert "source" in web


def test_fallback_branch_explicitly_forbids_llm_acceptance():
    """A provider failure/blocked response cannot masquerade as an AI result."""

    web = _text(WEB_PATH)
    fallback = re.search(r"rules_fallback", web)
    assert fallback, "the legacy fallback branch must remain auditable"
    nearby = web[max(0, fallback.start() - 800) : fallback.end() + 800]
    assert re.search(r"[\"']llm_accepted[\"']\s*:\s*False", nearby)
    assert re.search(r"[\"']provider_status[\"']", nearby)


def test_shadow_flag_defaults_off_and_home_refresh_does_not_read_shadow():
    """The default home path remains legacy/local until explicit review opt-in."""

    app = _text(APP_PATH)
    web = _text(WEB_PATH)
    flag_pattern = re.compile(
        r"shadow_analysis_enabled\s*[:=]\s*(?:false|False)|"
        r"shadow_analysis_enabled[^\n]{0,120}(?:default|fallback)[^\n]{0,80}(?:false|False)",
        re.IGNORECASE,
    )
    assert flag_pattern.search(app) or flag_pattern.search(web)

    refresh = re.search(r"function refresh\(options\)(.*?)(?=\n\s*async function |\n\s*function )", app, re.S)
    assert refresh, "the home refresh function must remain an auditable boundary"
    assert not re.search(r"/api/shadow(?:-analysis|/analysis)", refresh.group(1))


def test_review_surface_can_explicitly_select_an_analysis_run():
    """Run selection belongs to review, not an implicit home-data replacement."""

    app = _text(APP_PATH)
    html = _text(HTML_PATH)
    assert "analysis_run_id" in app
    assert re.search(r"analysis[-_]run[-_]selector|data-analysis-run-selector", app + html, re.I)
    assert re.search(r"analysis_run_id|analysis-run-id", app + html)


def test_source_selection_is_not_strict_single_source_equality():
    """UI selection must use explicit acceptance so composite sources survive."""

    app = _text(APP_PATH)
    assert 'state.aiResult.source !== "ai_assisted"' not in app
    assert re.search(r"llm_accepted", app)
    assert re.search(r"ai_assisted_with_local_fallback", app)


def test_public_semantic_entrypoints_are_identified_for_shadow_wiring():
    """The audit covers each current entrypoint before production integration."""

    web = _text(WEB_PATH)
    analysis = _text(ANALYSIS_PATH)
    ai = _text(AI_PATH)
    app = _text(APP_PATH)
    assert "analyze_messages(" in web
    assert "def analyze_messages(" in analysis
    assert "OpenAIAnalysisGenerator" in web and "OpenAIAnalysisGenerator" in ai
    assert "/api/overview" in app and "/api/ai-analysis" in app

