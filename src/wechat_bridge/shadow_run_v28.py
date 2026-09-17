"""Development-only Workstream H shadow-run and review catalog helpers.

The v2.8 source artifact is replayed through the existing shadow semantic
orchestrator with an in-process adapter.  Complete source decisions are
returned verbatim as semantic evidence; pending decisions remain pending.
The replay expands the bounded call budget only to visit every source
decision, so the emitted status counts remain faithful to the source artifact.

This module has no provider, production selector, event table, title, or
frontend side effect.  It writes only a caller-selected private, body-free
artifact directory and exposes it to the review API through an explicit
catalog path.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .contextual_bundle_pipeline import AIProviderConfig
from .contextual_bundle_pipeline_runner import _public_pipeline_messages, _read_messages
from .shadow_semantic import (
    SHADOW_PIPELINE_VERSION,
    SHADOW_RULESET_VERSION,
    SOURCE_LLM_ACCEPTED,
    SOURCE_MARKERS,
    ShadowRunConfig,
    ShadowSemanticRunResult,
    _body_free,
    run_shadow_semantic,
)
from .shadow_run_artifact import (
    OUTPUT_FILES as V26_OUTPUT_FILES,
    PUBLIC_PROVIDER_STATUSES,
    ShadowRunArtifact,
    ShadowRunCatalogEntry,
    _assert_body_free,
    _body_fields,
    _guard_contextual_artifact,
    _guard_development_input,
    _guard_output,
    _guard_path,
    _read_json,
    _read_jsonl,
    _sha256_file,
    _write_json,
)


SHADOW_RUN_V28_ARTIFACT_VERSION = "shadow_run_v2_8"
SHADOW_RUN_V28_SCHEMA_VERSION = "shadow_run_artifact_v2_8"
SHADOW_RUN_V28_PROVENANCE_VERSION = "shadow_run_provenance_v2_8"
SHADOW_RUN_V28_VALIDATION_VERSION = "shadow_run_api_validation_v2_8"
SHADOW_RUN_V28_DOM_VALIDATION_VERSION = "shadow_run_api_dom_e2e_v2_8"
SHADOW_RUN_V28_SCREENSHOT_VERSION = "shadow_run_screenshot_manifest_v2_8"
SOURCE_ARTIFACT_V28_VERSION = "contextual_bundle_pipeline_v2_8"
DEVELOPMENT_SPLIT = "development"
DEVELOPMENT_LOCAL_DAY = "2026-08-25"
INPUT_FILENAME = "messages.private.jsonl"
SOURCE_DECISIONS_FILENAME = "decisions.private.jsonl"
SOURCE_MANIFEST_FILENAME = "manifest.private.json"
V28_OUTPUT_FILES = {
    "run": "run.private.json",
    "manifest": "manifest.private.json",
    "provenance": "provenance.private.json",
    "aggregate": "aggregate.private.json",
    "api_validation": "api_validation.private.json",
    "api_dom_e2e": "api_dom_e2e.private.json",
    "screenshot_manifest": "screenshot_manifest.private.json",
}


def _read_jsonl_rows(path: Path) -> Tuple[Dict[str, Any], ...]:
    rows = _read_jsonl(path)
    return tuple(dict(row) for row in rows)


def _v28_code_sha256() -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__).with_name("shadow_semantic.py"),
        Path(__file__).with_name("shadow_run_artifact.py"),
        Path(__file__).with_name("shadow_run_v28.py"),
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_v28_source(
    input_root: Path,
    artifact_root: Path,
) -> Tuple[Tuple[Dict[str, Any], ...], bytes, Dict[str, Any], Tuple[Dict[str, Any], ...]]:
    messages, raw = _read_messages(input_root)
    source_manifest = _read_json(artifact_root / SOURCE_MANIFEST_FILENAME)
    if source_manifest.get("artifact_version") != SOURCE_ARTIFACT_V28_VERSION:
        raise ValueError("unexpected contextual v2.8 artifact version")
    if source_manifest.get("split") != DEVELOPMENT_SPLIT:
        raise ValueError("contextual v2.8 source is not development")
    if source_manifest.get("frozen_read") is not False:
        raise ValueError("contextual v2.8 source does not prove frozen_read=false")
    if source_manifest.get("gold_loaded") is not False:
        raise ValueError("contextual v2.8 source does not prove gold_loaded=false")
    if source_manifest.get("body_free_outputs") is not True:
        raise ValueError("contextual v2.8 source does not prove body_free_outputs=true")
    input_hash = hashlib.sha256(raw).hexdigest()
    if str(source_manifest.get("input_sha256") or "") != input_hash:
        raise ValueError("development input does not match contextual v2.8 artifact hash")
    if source_manifest.get("message_count") is not None and int(source_manifest["message_count"]) != len(messages):
        raise ValueError("development message count does not match contextual v2.8 manifest")
    decisions = _read_jsonl_rows(artifact_root / SOURCE_DECISIONS_FILENAME)
    expected_count = int(source_manifest.get("decision_count") or len(decisions))
    if len(decisions) != expected_count:
        raise ValueError("contextual v2.8 decision count does not match manifest")
    statuses = Counter(str(row.get("status") or "unknown") for row in decisions)
    if statuses.get("complete", 0) < 1 or any(
        status not in {"complete", "pending"} for status in statuses
    ):
        raise ValueError("v2.8 source must contain complete and pending decisions only")
    for row in decisions:
        if not str(row.get("bundle_id") or "").strip():
            raise ValueError("contextual v2.8 decision is missing bundle_id")
        if row.get("status") == "complete" and not isinstance(row.get("semantic_bundle"), Mapping):
            raise ValueError("complete contextual v2.8 decision is missing semantic_bundle")
    return messages, raw, source_manifest, decisions


class _V28ArtifactReplayModel:
    """Return only source-complete semantic bundles; pending stays pending."""

    model_version = "deepseek-v4-flash"

    def __init__(self, decisions: Sequence[Mapping[str, Any]], model_version: str) -> None:
        self.model_version = model_version
        self._complete_by_bundle = {
            str(row.get("bundle_id")): dict(row.get("semantic_bundle") or {})
            for row in decisions
            if row.get("status") == "complete"
        }
        if not self._complete_by_bundle:
            raise ValueError("v2.8 artifact replay requires a complete decision")
        self.calls: List[str] = []

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        bundle_id = str(request.get("bundle_id") or "")
        self.calls.append(bundle_id)
        value = self._complete_by_bundle.get(bundle_id)
        if value is None:
            raise RuntimeError("artifact_replay_pending")
        return _body_free(dict(value))


def _has_sufficient_model_slots(bundle: Mapping[str, Any]) -> bool:
    """Count only evidence-complete semantic bundles, without exposing body."""

    subject = bundle.get("subject")
    objects = bundle.get("object") or bundle.get("objects")
    actions = bundle.get("action") or bundle.get("actions")
    state = str(bundle.get("state") or "unknown").casefold()
    evidence = bundle.get("evidence")
    if isinstance(objects, Mapping):
        objects = (objects,)
    if isinstance(actions, Mapping) or isinstance(actions, str):
        actions = (actions,)
    if not isinstance(subject, Mapping) or not str(subject.get("id") or "").strip():
        return False
    if not objects or not actions or state in {"", "unknown", "n/a", "none"}:
        return False
    return bool(evidence)


def run_development_shadow_run_v28(
    input_directory: Union[str, Path],
    contextual_artifact_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    analysis_run_id: str = SHADOW_RUN_V28_ARTIFACT_VERSION,
) -> ShadowRunArtifact:
    """Replay the formal development v2.8 source into a new private run."""

    input_root = _guard_development_input(input_directory)
    source_root = _guard_contextual_artifact(contextual_artifact_directory)
    output_root = _guard_output(output_directory)
    messages, raw, source_manifest, source_decisions = _load_v28_source(input_root, source_root)
    source_status_counts = Counter(str(row.get("status") or "unknown") for row in source_decisions)
    model_version = str(source_manifest.get("model") or "deepseek-v4-flash")
    replay_model = _V28ArtifactReplayModel(source_decisions, model_version)
    budget = source_manifest.get("budget_limits") if isinstance(source_manifest.get("budget_limits"), Mapping) else {}
    config = ShadowRunConfig(
        mode="real",
        analysis_run_id=str(analysis_run_id).strip() or SHADOW_RUN_V28_ARTIFACT_VERSION,
        split=DEVELOPMENT_SPLIT,
        pipeline_kwargs={
            "provider_config": AIProviderConfig(
                provider="openai", model=model_version, api_key="artifact-replay"
            ),
            # The source artifact was budgeted for provider calls.  A replay
            # must visit every source decision to preserve its complete/
            # pending status map, while the adapter itself never calls a
            # provider and all non-complete decisions remain pending.
            "max_bundle_calls": len(source_decisions),
            "max_input_tokens": int(budget.get("max_input_tokens") or 2_000),
            "max_output_tokens": int(budget.get("max_output_tokens") or 400),
            "max_retries": int(source_manifest.get("max_retries") or 1),
            "window_size": int(source_manifest.get("window_size") or 8),
            "time_window_seconds": float(source_manifest.get("time_window_seconds") or 900),
            "max_candidates": int(source_manifest.get("max_candidates") or 3),
        },
    )
    result = run_shadow_semantic(
        _public_pipeline_messages(messages),
        mode="real",
        model=replay_model,
        config=config,
    )
    output_decisions = tuple(
        result.pipeline_result.decisions if result.pipeline_result is not None else ()
    )
    output_status_counts = Counter(str(row.get("status") or "unknown") for row in output_decisions)
    if dict(output_status_counts) != dict(source_status_counts):
        raise ValueError(
            "v2.8 shadow replay status mismatch: source=%s output=%s"
            % (dict(source_status_counts), dict(output_status_counts))
        )
    complete_sufficient_count = sum(
        1
        for row in source_decisions
        if row.get("status") == "complete"
        and isinstance(row.get("semantic_bundle"), Mapping)
        and _has_sufficient_model_slots(row["semantic_bundle"])
    )
    pending_count = int(output_status_counts.get("pending", 0))
    if pending_count and result.event_candidates:
        raise ValueError("pending v2.8 decisions must not materialize event candidates")
    for event in result.event_candidates:
        if (
            getattr(event, "source", None) != SOURCE_LLM_ACCEPTED
            or getattr(event, "materialized", False) is not True
            or not str(getattr(event, "subject_id", "")).strip()
            or not str(getattr(event, "object_id", "")).strip()
            or not getattr(event, "actions", ())
            or str(getattr(event, "state", "unknown")).casefold() == "unknown"
            or not getattr(event, "evidence_refs", ())
        ):
            raise ValueError("v2.8 event candidate failed complete/evidence gate")

    input_sha256 = hashlib.sha256(raw).hexdigest()
    source_manifest_sha256 = _sha256_file(source_root / SOURCE_MANIFEST_FILENAME)
    source_decisions_sha256 = _sha256_file(source_root / SOURCE_DECISIONS_FILENAME)
    code_sha256 = _v28_code_sha256()
    event_count = len(result.event_candidates)
    manifest: Dict[str, Any] = {
        "artifact_version": SHADOW_RUN_V28_ARTIFACT_VERSION,
        "schema_version": SHADOW_RUN_V28_SCHEMA_VERSION,
        "provenance_version": SHADOW_RUN_V28_PROVENANCE_VERSION,
        "analysis_run_id": result.analysis_run_id,
        "run_id": result.run_id,
        "source": result.source_marker,
        "source_marker": result.source_marker,
        "provider_status": result.provider_status,
        "llm_accepted": result.llm_accepted,
        "fallback_reason": result.fallback_reason,
        "model_status": result.model_status,
        "mode": result.mode,
        "split": DEVELOPMENT_SPLIT,
        "local_day": DEVELOPMENT_LOCAL_DAY,
        "input_directory_name": input_root.name,
        "input_filename": INPUT_FILENAME,
        "development_input_sha256": input_sha256,
        "shadow_input_sha256": result.input_sha256,
        "pipeline_input_sha256": result.pipeline_result.input_sha256 if result.pipeline_result else "",
        "source_artifact_version": SOURCE_ARTIFACT_V28_VERSION,
        "source_artifact_directory_name": source_root.name,
        "source_artifact_manifest_sha256": source_manifest_sha256,
        "source_artifact_decisions_sha256": source_decisions_sha256,
        "candidate_bundle_count": len(source_decisions),
        "decision_count": len(output_decisions),
        "source_decision_status_counts": dict(sorted(source_status_counts.items())),
        "decision_status_counts": dict(sorted(output_status_counts.items())),
        "complete_sufficient_source_count": complete_sufficient_count,
        "thread_count": len(result.threads),
        "event_candidate_count": event_count,
        "event_materialization_policy": "only_llm_complete_with_sufficient_evidence",
        "pending_event_materialization": pending_count == 0,
        "event_materialization_blocked_by_pending": pending_count > 0,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": code_sha256,
        "component_versions": {
            "shadow": SHADOW_PIPELINE_VERSION,
            "shadow_ruleset": SHADOW_RULESET_VERSION,
            "source_pipeline": str(source_manifest.get("pipeline_version") or "unknown"),
            "source_runner": str(source_manifest.get("runner_schema_version") or "unknown"),
        },
        "output_files": dict(V28_OUTPUT_FILES),
    }
    provenance: Dict[str, Any] = {
        "provenance_version": SHADOW_RUN_V28_PROVENANCE_VERSION,
        "analysis_run_id": result.analysis_run_id,
        "run_id": result.run_id,
        "source": result.source_marker,
        "source_marker": result.source_marker,
        "provider_status": result.provider_status,
        "llm_accepted": result.llm_accepted,
        "fallback_reason": result.fallback_reason,
        "model_status": result.model_status,
        "development_input_sha256": input_sha256,
        "shadow_input_sha256": result.input_sha256,
        "source_artifact_manifest_sha256": source_manifest_sha256,
        "source_artifact_decisions_sha256": source_decisions_sha256,
        "replay_policy": "complete_decisions_replayed; pending_decisions_remain_pending",
        "replay_budget_policy": "visit_all_source_decisions_without_provider_calls",
        "event_materialization_policy": "only_llm_complete_with_sufficient_evidence",
        "event_candidate_count": event_count,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": code_sha256,
    }
    run_projection = _body_free(result.to_dict())
    run_projection.update(
        {
            "artifact_version": SHADOW_RUN_V28_ARTIFACT_VERSION,
            "source_artifact_version": SOURCE_ARTIFACT_V28_VERSION,
            "source_decision_status_counts": dict(sorted(source_status_counts.items())),
            "decision_status_counts": dict(sorted(output_status_counts.items())),
            "complete_sufficient_source_count": complete_sufficient_count,
            "thread_count": len(result.threads),
            "event_candidate_count": event_count,
            "frozen_read": False,
            "gold_loaded": False,
            "body_free_outputs": True,
        }
    )
    aggregate: Dict[str, Any] = {
        "artifact_version": SHADOW_RUN_V28_ARTIFACT_VERSION,
        "schema_version": SHADOW_RUN_V28_SCHEMA_VERSION,
        "analysis_run_id": result.analysis_run_id,
        "run_id": result.run_id,
        "source": result.source_marker,
        "provider_status": result.provider_status,
        "llm_accepted": result.llm_accepted,
        "fallback_reason": result.fallback_reason,
        "message_count": len(messages),
        "candidate_bundle_count": len(source_decisions),
        "decision_count": len(output_decisions),
        "source_decision_status_counts": dict(sorted(source_status_counts.items())),
        "decision_status_counts": dict(sorted(output_status_counts.items())),
        "complete_sufficient_source_count": complete_sufficient_count,
        "thread_count": len(result.threads),
        "event_candidate_count": event_count,
        "event_materialization_blocked_by_pending": pending_count > 0,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "development_input_sha256": input_sha256,
        "source_artifact_manifest_sha256": source_manifest_sha256,
        "source_artifact_decisions_sha256": source_decisions_sha256,
        "code_sha256": code_sha256,
    }
    offline_validation: Dict[str, Any] = {
        "validation_version": SHADOW_RUN_V28_VALIDATION_VERSION,
        "kind": "offline_shadow_v2_8_projection",
        "analysis_run_id": result.analysis_run_id,
        "required_envelope_fields": [
            "analysis_run_id",
            "source",
            "provider_status",
            "llm_accepted",
        ],
        "required_envelope_fields_ok": True,
        "source_decision_status_counts": dict(sorted(source_status_counts.items())),
        "decision_status_counts": dict(sorted(output_status_counts.items())),
        "status_counts_match": True,
        "complete_sufficient_source_count": complete_sufficient_count,
        "event_candidate_count": event_count,
        "pending_event_materialization_ok": not (pending_count and event_count),
        "body_free": True,
        "frozen_read": False,
        "gold_loaded": False,
    }
    dom_skeleton: Dict[str, Any] = {
        "validation_version": SHADOW_RUN_V28_DOM_VALIDATION_VERSION,
        "kind": "shadow_api_dom_e2e",
        "analysis_run_id": result.analysis_run_id,
        "status": "not_run",
        "blocked_reason": "awaiting_browser_e2e",
        "body_free": True,
        "frozen_read": False,
        "gold_loaded": False,
    }
    screenshot_skeleton: Dict[str, Any] = {
        "validation_version": SHADOW_RUN_V28_SCREENSHOT_VERSION,
        "analysis_run_id": result.analysis_run_id,
        "status": "not_run",
        "reason": "awaiting_browser_e2e",
        "screenshots": [],
        "body_free": True,
        "frozen_read": False,
        "gold_loaded": False,
    }
    for label, value in (
        ("v2.8 shadow run", run_projection),
        ("v2.8 manifest", manifest),
        ("v2.8 provenance", provenance),
        ("v2.8 aggregate", aggregate),
        ("v2.8 offline validation", offline_validation),
        ("v2.8 dom skeleton", dom_skeleton),
        ("v2.8 screenshot skeleton", screenshot_skeleton),
    ):
        _assert_body_free(value, label=label)
    output_root.mkdir(parents=True, exist_ok=False)
    payloads = {
        "run": run_projection,
        "manifest": manifest,
        "provenance": provenance,
        "aggregate": aggregate,
        "api_validation": offline_validation,
        "api_dom_e2e": dom_skeleton,
        "screenshot_manifest": screenshot_skeleton,
    }
    paths: Dict[str, str] = {}
    for key, filename in V28_OUTPUT_FILES.items():
        path = output_root / filename
        _write_json(path, payloads[key])
        paths[key] = str(path)
    return ShadowRunArtifact(
        output_directory=str(output_root),
        artifact_paths=paths,
        manifest=manifest,
        provenance=provenance,
        aggregate=aggregate,
        result=result,
    )


def _load_v28_catalog(root: Path) -> Tuple[ShadowRunCatalogEntry, ...]:
    manifest = _read_json(root / V28_OUTPUT_FILES["manifest"])
    provenance = _read_json(root / V28_OUTPUT_FILES["provenance"])
    run_projection = _read_json(root / V28_OUTPUT_FILES["run"])
    if manifest.get("artifact_version") != SHADOW_RUN_V28_ARTIFACT_VERSION:
        raise ValueError("unsupported shadow v2.8 catalog artifact version")
    if manifest.get("frozen_read") is not False or manifest.get("gold_loaded") is not False:
        raise ValueError("shadow v2.8 catalog must prove frozen_read=false and gold_loaded=false")
    if manifest.get("body_free_outputs") is not True or provenance.get("body_free_outputs") is not True:
        raise ValueError("shadow v2.8 catalog must prove body_free_outputs=true")
    for label, value in (
        ("shadow v2.8 manifest", manifest),
        ("shadow v2.8 provenance", provenance),
        ("shadow v2.8 run", run_projection),
    ):
        _assert_body_free(value, label=label)
    required = ("analysis_run_id", "source", "provider_status", "llm_accepted")
    if any(key not in manifest for key in required):
        raise ValueError("shadow v2.8 catalog manifest is missing envelope fields")
    analysis_run_id = str(manifest.get("analysis_run_id") or "").strip()
    run_id = str(manifest.get("run_id") or "").strip()
    source = str(manifest.get("source") or "").strip()
    source_marker = str(manifest.get("source_marker") or "").strip()
    provider_status = str(manifest.get("provider_status") or "").strip()
    if not analysis_run_id or not run_id or source not in SOURCE_MARKERS or source_marker != source:
        raise ValueError("shadow v2.8 catalog identity/source marker is invalid")
    if provider_status not in PUBLIC_PROVIDER_STATUSES:
        raise ValueError("shadow v2.8 catalog provider_status is invalid")
    if not isinstance(manifest.get("llm_accepted"), bool):
        raise ValueError("shadow v2.8 catalog llm_accepted must be bool")
    if manifest.get("llm_accepted") and provider_status != "succeeded":
        raise ValueError("accepted shadow v2.8 catalog must have succeeded provider_status")
    if not manifest.get("llm_accepted") and not str(manifest.get("fallback_reason") or "").strip():
        raise ValueError("rejected shadow v2.8 catalog must have fallback_reason")
    for key in required:
        if provenance.get(key) != manifest.get(key) or run_projection.get(key) != manifest.get(key):
            raise ValueError("shadow v2.8 catalog disagrees on %s" % key)
    for label, value in (("provenance", provenance), ("run projection", run_projection)):
        if str(value.get("source_marker") or "") != source:
            raise ValueError("shadow v2.8 %s source_marker disagrees" % label)
    status_counts = manifest.get("decision_status_counts")
    if not isinstance(status_counts, Mapping):
        raise ValueError("shadow v2.8 catalog is missing decision_status_counts")
    pending_count = int(status_counts.get("pending") or 0)
    thread_count = int(manifest.get("thread_count") or 0)
    event_candidate_count = int(manifest.get("event_candidate_count") or 0)
    if pending_count and event_candidate_count:
        raise ValueError("pending shadow v2.8 catalog cannot expose event candidates")
    output_files = manifest.get("output_files")
    if not isinstance(output_files, Mapping):
        raise ValueError("shadow v2.8 catalog is missing output_files")
    for key, filename in V28_OUTPUT_FILES.items():
        if str(output_files.get(key) or "") != filename or not (root / filename).is_file():
            raise ValueError("shadow v2.8 catalog output is incomplete: %s" % key)
    return (
        ShadowRunCatalogEntry(
            run_id=run_id,
            analysis_run_id=analysis_run_id,
            source=source,
            provider_status=provider_status,
            llm_accepted=bool(manifest.get("llm_accepted")),
            fallback_reason=(str(manifest.get("fallback_reason")) if manifest.get("fallback_reason") else None),
            model_status=str(manifest.get("model_status") or "unknown"),
            mode=str(manifest.get("mode") or "unknown"),
            artifact_version=SHADOW_RUN_V28_ARTIFACT_VERSION,
            manifest=manifest,
            provenance=provenance,
            run_projection=run_projection,
            thread_count=thread_count,
            event_candidate_count=event_candidate_count,
        ),
    )


def load_shadow_catalog(directory: Union[str, Path]) -> Tuple[ShadowRunCatalogEntry, ...]:
    """Load v2.6 or v2.8 body-free review runs from an explicit directory."""

    root = _guard_path(directory, must_exist=True, label="shadow catalog")
    manifest_path = root / "manifest.private.json"
    manifest = _read_json(manifest_path)
    if manifest.get("artifact_version") == SHADOW_RUN_V28_ARTIFACT_VERSION:
        return _load_v28_catalog(root)
    # Keep the Workstream G catalog readable without changing its contract.
    from .shadow_run_artifact import load_shadow_run_catalog as load_v26_catalog

    return load_v26_catalog(root)


def write_screenshot_manifest(
    directory: Union[str, Path],
    *,
    status: str,
    reason: Optional[str] = None,
    screenshots: Sequence[str] = (),
) -> Dict[str, Any]:
    """Persist a body-free captured/blocked screenshot manifest."""

    root = _guard_path(directory, must_exist=True, label="shadow v2.8 screenshot manifest")
    manifest = _read_json(root / V28_OUTPUT_FILES["manifest"])
    value: Dict[str, Any] = {
        "validation_version": SHADOW_RUN_V28_SCREENSHOT_VERSION,
        "analysis_run_id": manifest.get("analysis_run_id"),
        "status": str(status),
        "reason": reason,
        "screenshots": [str(item) for item in screenshots],
        "body_free": True,
        "frozen_read": False,
        "gold_loaded": False,
    }
    _assert_body_free(value, label="shadow v2.8 screenshot manifest")
    _write_json(root / V28_OUTPUT_FILES["screenshot_manifest"], value)
    return value


def write_dom_e2e_report(
    directory: Union[str, Path],
    *,
    selected: Mapping[str, Any],
    unknown: Mapping[str, Any],
    selected_status: int,
    unknown_status: int,
    dom_checks: Mapping[str, Any],
    screenshot_status: str,
    screenshot_paths: Sequence[str] = (),
) -> Dict[str, Any]:
    """Persist the body-free HTTP/DOM validation result for v2.8."""

    root = _guard_path(directory, must_exist=True, label="shadow v2.8 DOM validation")
    manifest = _read_json(root / V28_OUTPUT_FILES["manifest"])
    required = ("analysis_run_id", "source", "provider_status", "llm_accepted")
    selected_match = all(selected.get(key) == manifest.get(key) for key in required)
    unknown_safe = (
        unknown.get("ok") is False
        and unknown.get("fallback_reason") == "analysis_run_not_found"
        and unknown.get("analysis_run_id") != manifest.get("analysis_run_id")
        and int(unknown_status) == 404
    )
    value: Dict[str, Any] = {
        "validation_version": SHADOW_RUN_V28_DOM_VALIDATION_VERSION,
        "kind": "shadow_api_dom_e2e",
        "analysis_run_id": manifest.get("analysis_run_id"),
        "required_envelope_fields": list(required),
        "selected_http_status": int(selected_status),
        "selected_http_status_ok": int(selected_status) == 200,
        "selected_matches_artifact": bool(selected_match),
        "selected_source": selected.get("source"),
        "selected_provider_status": selected.get("provider_status"),
        "selected_llm_accepted": selected.get("llm_accepted"),
        "unknown_http_status": int(unknown_status),
        "unknown_http_404": int(unknown_status) == 404,
        "unknown_run_rejected_without_fallback": bool(unknown_safe),
        "unknown_source": unknown.get("source"),
        "unknown_provider_status": unknown.get("provider_status"),
        "unknown_llm_accepted": unknown.get("llm_accepted"),
        "dom_checks": dict(dom_checks),
        "screenshot_status": str(screenshot_status),
        "screenshot_paths": [str(item) for item in screenshot_paths],
        "body_free": not bool(_body_fields(selected) or _body_fields(unknown)),
        "frozen_read": False,
        "gold_loaded": False,
    }
    _assert_body_free(value, label="shadow v2.8 DOM validation report")
    _write_json(root / V28_OUTPUT_FILES["api_dom_e2e"], value)
    return value


__all__ = [
    "SHADOW_RUN_V28_ARTIFACT_VERSION",
    "SHADOW_RUN_V28_SCHEMA_VERSION",
    "SHADOW_RUN_V28_PROVENANCE_VERSION",
    "SHADOW_RUN_V28_VALIDATION_VERSION",
    "SHADOW_RUN_V28_DOM_VALIDATION_VERSION",
    "SHADOW_RUN_V28_SCREENSHOT_VERSION",
    "SOURCE_ARTIFACT_V28_VERSION",
    "V28_OUTPUT_FILES",
    "run_development_shadow_run_v28",
    "load_shadow_catalog",
    "write_screenshot_manifest",
    "write_dom_e2e_report",
]
