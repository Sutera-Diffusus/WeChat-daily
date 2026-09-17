"""Development-only Workstream G shadow-run artifacts and catalog loading.

This module replays one already-produced development v2.6 bundle artifact
through :func:`wechat_bridge.shadow_semantic.run_shadow_semantic`.  The replay
adapter returns the single complete bundle from that artifact and keeps every
other bundle pending; it never calls a provider.  The resulting run is written
as a versioned, body-free private artifact and can be loaded by the explicit
review-only HTTP catalog hook.

No production event/title/frontend path imports this module's development
runner.  Catalog loading is opt-in through an explicit server argument and
rejects frozen paths, gold-loaded artifacts, inconsistent hashes, and body
fields before anything is exposed to the API.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contextual_bundle_pipeline import AIProviderConfig
from .contextual_bundle_pipeline_runner import _public_pipeline_messages, _read_messages
from .shadow_semantic import (
    SHADOW_PIPELINE_VERSION,
    SHADOW_RULESET_VERSION,
    SOURCE_MARKERS,
    ShadowRunConfig,
    ShadowSemanticRunResult,
    _body_free,
    run_shadow_semantic,
)


SHADOW_RUN_ARTIFACT_VERSION = "shadow_run_v2_6"
SHADOW_RUN_SCHEMA_VERSION = "shadow_run_artifact_v2_6"
SHADOW_RUN_PROVENANCE_VERSION = "shadow_run_provenance_v2_6"
SHADOW_RUN_VALIDATION_VERSION = "shadow_run_api_validation_v2_6"
SOURCE_ARTIFACT_VERSION = "contextual_bundle_pipeline_v2_6"
DEVELOPMENT_SPLIT = "development"
DEVELOPMENT_LOCAL_DAY = "2026-08-25"
INPUT_FILENAME = "messages.private.jsonl"
SOURCE_DECISIONS_FILENAME = "decisions.private.jsonl"
SOURCE_MANIFEST_FILENAME = "manifest.private.json"
OUTPUT_FILES = {
    "run": "run.private.json",
    "manifest": "manifest.private.json",
    "provenance": "provenance.private.json",
    "aggregate": "aggregate.private.json",
    "api_validation": "api_validation.private.json",
}
PUBLIC_PROVIDER_STATUSES = frozenset(
    {"disabled", "configured", "succeeded", "blocked", "failed"}
)

_BODY_KEYS = frozenset(
    {
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
        "surface_redacted",
        "fragment_text_redacted",
        "evidence_text",
        "claim_text_redacted",
        "quote",
        "summary",
        "narrative",
        "prompt",
        "response",
    }
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _body_fields(value: Any, path: str = "") -> Tuple[str, ...]:
    found: List[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            lower = name.casefold()
            current = "%s.%s" % (path, name) if path else name
            if lower in _BODY_KEYS or lower.endswith(("_text", "_content", "_surface")):
                found.append(current)
            found.extend(_body_fields(item, current))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            found.extend(_body_fields(item, "%s[%d]" % (path, index)))
    return tuple(found)


def _assert_body_free(value: Any, *, label: str) -> None:
    fields = _body_fields(value)
    if fields:
        raise ValueError("%s contains body fields: %s" % (label, ", ".join(fields[:8])))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_code() -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__).with_name("shadow_semantic.py"),
        Path(__file__).with_name("shadow_run_artifact.py"),
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("JSON artifact must be an object: %s" % path.name)
    return dict(value)


def _read_jsonl(path: Path) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError("JSONL artifact row %d is not an object" % line_number)
            rows.append(dict(value))
    return tuple(rows)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _guard_path(path: Union[str, Path], *, must_exist: bool, label: str) -> Path:
    value = Path(path).expanduser()
    if any(part.casefold() in {"frozen", "frozen_test"} for part in value.parts):
        raise ValueError("%s refuses frozen paths" % label)
    if must_exist and not value.is_dir():
        raise ValueError("%s directory does not exist" % label)
    return value.resolve()


def _guard_development_input(path: Union[str, Path]) -> Path:
    root = _guard_path(path, must_exist=True, label="development input")
    if root.name.casefold() != DEVELOPMENT_SPLIT:
        raise ValueError("development input must end in development")
    if not (root / INPUT_FILENAME).is_file():
        raise ValueError("development messages.private.jsonl is missing")
    return root


def _guard_contextual_artifact(path: Union[str, Path]) -> Path:
    root = _guard_path(path, must_exist=True, label="contextual artifact")
    manifest_path = root / SOURCE_MANIFEST_FILENAME
    decisions_path = root / SOURCE_DECISIONS_FILENAME
    if not manifest_path.is_file() or not decisions_path.is_file():
        raise ValueError("contextual v2.6 artifact is missing manifest or decisions")
    return root


def _guard_output(path: Union[str, Path]) -> Path:
    root = _guard_path(path, must_exist=False, label="shadow output")
    if root.exists():
        raise FileExistsError("refusing to overwrite shadow artifact: %s" % root)
    return root


def _load_source(
    input_root: Path,
    artifact_root: Path,
) -> Tuple[Tuple[Dict[str, Any], ...], bytes, Dict[str, Any], Tuple[Dict[str, Any], ...]]:
    messages, raw = _read_messages(input_root)
    manifest_path = artifact_root / SOURCE_MANIFEST_FILENAME
    decisions_path = artifact_root / SOURCE_DECISIONS_FILENAME
    source_manifest = _read_json(manifest_path)
    if source_manifest.get("artifact_version") != SOURCE_ARTIFACT_VERSION:
        raise ValueError("unexpected contextual artifact version")
    if source_manifest.get("split") != DEVELOPMENT_SPLIT:
        raise ValueError("contextual source is not development")
    if source_manifest.get("frozen_read") is not False:
        raise ValueError("contextual source does not prove frozen_read=false")
    if source_manifest.get("gold_loaded") is not False:
        raise ValueError("contextual source does not prove gold_loaded=false")
    input_hash = hashlib.sha256(raw).hexdigest()
    if str(source_manifest.get("input_sha256") or "") != input_hash:
        raise ValueError("development input does not match contextual artifact hash")
    decisions = _read_jsonl(decisions_path)
    if len(decisions) != int(source_manifest.get("decision_count") or len(decisions)):
        raise ValueError("contextual decision count does not match manifest")
    statuses = Counter(str(row.get("status") or "unknown") for row in decisions)
    if statuses.get("complete") != 1 or statuses.get("pending") != len(decisions) - 1:
        raise ValueError("v2.6 source must contain exactly one complete and the rest pending")
    for row in decisions:
        if not str(row.get("bundle_id") or "").strip():
            raise ValueError("contextual decision is missing bundle_id")
        if row.get("status") == "complete" and not isinstance(row.get("semantic_bundle"), Mapping):
            raise ValueError("complete contextual decision is missing semantic_bundle")
    return messages, raw, source_manifest, decisions


class _ArtifactReplayModel:
    """Offline model adapter that preserves source complete/pending truth."""

    def __init__(self, decisions: Sequence[Mapping[str, Any]], model_version: str) -> None:
        self.model_version = model_version
        self._complete_by_bundle = {
            str(row.get("bundle_id")): dict(row.get("semantic_bundle") or {})
            for row in decisions
            if row.get("status") == "complete"
        }
        if len(self._complete_by_bundle) != 1:
            raise ValueError("artifact replay requires exactly one complete bundle")

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        bundle_id = str(request.get("bundle_id") or "")
        value = self._complete_by_bundle.get(bundle_id)
        if value is None:
            # The pipeline records this as a stable pending/model-failure code;
            # no placeholder model semantics are fabricated for pending rows.
            raise RuntimeError("artifact_replay_pending")
        return _body_free(dict(value))


@dataclass(frozen=True)
class ShadowRunArtifact:
    """Private artifact paths plus the in-memory result for API registration."""

    output_directory: str
    artifact_paths: Mapping[str, str]
    manifest: Mapping[str, Any]
    provenance: Mapping[str, Any]
    aggregate: Mapping[str, Any]
    result: ShadowSemanticRunResult = field(repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_directory": self.output_directory,
            "artifact_paths": dict(self.artifact_paths),
            "manifest": _body_free(dict(self.manifest)),
            "provenance": _body_free(dict(self.provenance)),
            "aggregate": _body_free(dict(self.aggregate)),
        }


def run_development_shadow_run_v26(
    input_directory: Union[str, Path],
    contextual_artifact_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    analysis_run_id: str = SHADOW_RUN_ARTIFACT_VERSION,
) -> ShadowRunArtifact:
    """Replay the public development projection into a new shadow artifact.

    The source v2.6 artifact supplies the deterministic model decision map.
    Only the one complete decision is returned by the offline adapter; all
    other bundle requests raise a stable pending error and therefore cannot
    materialize event candidates.
    """

    input_root = _guard_development_input(input_directory)
    source_root = _guard_contextual_artifact(contextual_artifact_directory)
    output_root = _guard_output(output_directory)
    messages, raw, source_manifest, decisions = _load_source(input_root, source_root)
    model_version = str(source_manifest.get("model") or "deepseek-v4-flash")
    replay_model = _ArtifactReplayModel(decisions, model_version)
    provider_config = AIProviderConfig(
        provider="openai",
        model=model_version,
        api_key="artifact-replay",
    )
    config = ShadowRunConfig(
        mode="real",
        analysis_run_id=str(analysis_run_id).strip() or SHADOW_RUN_ARTIFACT_VERSION,
        split=DEVELOPMENT_SPLIT,
        pipeline_kwargs={
            "provider_config": provider_config,
            "max_bundle_calls": int((source_manifest.get("budget_limits") or {}).get("max_bundle_calls") or 14),
            "max_input_tokens": int((source_manifest.get("budget_limits") or {}).get("max_input_tokens") or 2_000),
            "max_output_tokens": int((source_manifest.get("budget_limits") or {}).get("max_output_tokens") or 400),
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
    decisions_out = tuple(result.pipeline_result.decisions if result.pipeline_result is not None else ())
    status_counts = Counter(str(row.get("status") or "unknown") for row in decisions_out)
    expected_status_counts = {"complete": 1, "pending": len(decisions) - 1}
    if dict(status_counts) != expected_status_counts:
        raise ValueError("shadow replay status mismatch: %s" % dict(status_counts))
    if result.event_candidates:
        raise ValueError("pending shadow replay must not materialize event candidates")

    development_input_sha256 = hashlib.sha256(raw).hexdigest()
    artifact_manifest_sha256 = _sha256_file(source_root / SOURCE_MANIFEST_FILENAME)
    artifact_decisions_sha256 = _sha256_file(source_root / SOURCE_DECISIONS_FILENAME)
    manifest: Dict[str, Any] = {
        "artifact_version": SHADOW_RUN_ARTIFACT_VERSION,
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "provenance_version": SHADOW_RUN_PROVENANCE_VERSION,
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
        "development_input_sha256": development_input_sha256,
        "shadow_input_sha256": result.input_sha256,
        "pipeline_input_sha256": result.pipeline_result.input_sha256 if result.pipeline_result is not None else "",
        "source_artifact_version": SOURCE_ARTIFACT_VERSION,
        "source_artifact_directory_name": source_root.name,
        "source_artifact_manifest_sha256": artifact_manifest_sha256,
        "source_artifact_decisions_sha256": artifact_decisions_sha256,
        "candidate_bundle_count": len(decisions),
        "decision_count": len(decisions_out),
        "decision_status_counts": dict(sorted(status_counts.items())),
        "thread_count": len(result.threads),
        "event_candidate_count": len(result.event_candidates),
        "pending_event_materialization": True,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": _sha256_code(),
        "component_versions": {
            "shadow": SHADOW_PIPELINE_VERSION,
            "shadow_ruleset": SHADOW_RULESET_VERSION,
            "source_pipeline": str(source_manifest.get("pipeline_version") or "unknown"),
            "source_runner": str(source_manifest.get("runner_schema_version") or "unknown"),
        },
        "output_files": dict(OUTPUT_FILES),
    }
    provenance: Dict[str, Any] = {
        "provenance_version": SHADOW_RUN_PROVENANCE_VERSION,
        "analysis_run_id": result.analysis_run_id,
        "run_id": result.run_id,
        "source": result.source_marker,
        "source_marker": result.source_marker,
        "provider_status": result.provider_status,
        "llm_accepted": result.llm_accepted,
        "fallback_reason": result.fallback_reason,
        "model_status": result.model_status,
        "development_input_sha256": development_input_sha256,
        "shadow_input_sha256": result.input_sha256,
        "source_artifact_manifest_sha256": artifact_manifest_sha256,
        "source_artifact_decisions_sha256": artifact_decisions_sha256,
        "replay_policy": "one_complete_decision; all_other_decisions_pending",
        "event_materialization_policy": "pending_or_fallback_never_materialize",
        "event_candidate_count": len(result.event_candidates),
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "code_sha256": manifest["code_sha256"],
    }
    run_projection = _body_free(result.to_dict())
    run_projection.update(
        {
            "artifact_version": SHADOW_RUN_ARTIFACT_VERSION,
            "source_artifact_version": SOURCE_ARTIFACT_VERSION,
            "decision_status_counts": dict(sorted(status_counts.items())),
            "thread_count": len(result.threads),
            "event_candidate_count": len(result.event_candidates),
        }
    )
    aggregate = {
        "artifact_version": SHADOW_RUN_ARTIFACT_VERSION,
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "analysis_run_id": result.analysis_run_id,
        "run_id": result.run_id,
        "source": result.source_marker,
        "provider_status": result.provider_status,
        "llm_accepted": result.llm_accepted,
        "fallback_reason": result.fallback_reason,
        "message_count": len(messages),
        "candidate_bundle_count": len(decisions),
        "decision_count": len(decisions_out),
        "decision_status_counts": dict(sorted(status_counts.items())),
        "thread_count": len(result.threads),
        "event_candidate_count": len(result.event_candidates),
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "development_input_sha256": development_input_sha256,
        "source_artifact_manifest_sha256": artifact_manifest_sha256,
        "source_artifact_decisions_sha256": artifact_decisions_sha256,
        "code_sha256": manifest["code_sha256"],
    }
    validation = {
        "validation_version": SHADOW_RUN_VALIDATION_VERSION,
        "kind": "offline_shadow_projection",
        "analysis_run_id": result.analysis_run_id,
        "required_envelope_fields": [
            "analysis_run_id",
            "source",
            "provider_status",
            "llm_accepted",
        ],
        "required_envelope_fields_ok": True,
        "decision_status_counts": dict(sorted(status_counts.items())),
        "event_candidate_count": 0,
        "pending_event_materialization_ok": True,
        "body_free": True,
        "frozen_read": False,
        "gold_loaded": False,
    }
    for label, value in (
        ("shadow run projection", run_projection),
        ("shadow manifest", manifest),
        ("shadow provenance", provenance),
        ("shadow aggregate", aggregate),
        ("shadow validation", validation),
    ):
        _assert_body_free(value, label=label)
    output_root.mkdir(parents=True, exist_ok=False)
    payloads = {
        "run": run_projection,
        "manifest": manifest,
        "provenance": provenance,
        "aggregate": aggregate,
        "api_validation": validation,
    }
    paths: Dict[str, str] = {}
    for key, filename in OUTPUT_FILES.items():
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


@dataclass(frozen=True)
class ShadowRunCatalogEntry:
    """Body-free catalog projection suitable for the review-only API."""

    run_id: str
    analysis_run_id: str
    source: str
    provider_status: str
    llm_accepted: bool
    fallback_reason: Optional[str]
    model_status: str
    mode: str
    artifact_version: str
    manifest: Mapping[str, Any]
    provenance: Mapping[str, Any]
    run_projection: Mapping[str, Any]
    thread_count: int = 0
    event_candidate_count: int = 0

    @property
    def source_marker(self) -> str:
        return self.source

    def to_dict(self) -> Dict[str, Any]:
        value = _body_free(dict(self.run_projection))
        value.update(
            {
                "ok": True,
                "artifact_version": self.artifact_version,
                "run_id": self.run_id,
                "analysis_run_id": self.analysis_run_id,
                "source": self.source,
                "source_marker": self.source,
                "provider_status": self.provider_status,
                "llm_accepted": self.llm_accepted,
                "fallback_reason": self.fallback_reason,
                "model_status": self.model_status,
                "mode": self.mode,
                "manifest": _body_free(dict(self.manifest)),
                "provenance": _body_free(dict(self.provenance)),
                "thread_count": self.thread_count,
                "event_candidate_count": self.event_candidate_count,
                "read_only": True,
            }
        )
        _assert_body_free(value, label="shadow catalog entry")
        return value


def load_shadow_run_catalog(directory: Union[str, Path]) -> Tuple[ShadowRunCatalogEntry, ...]:
    """Load only body-free metadata/projection from one shadow artifact dir."""

    root = _guard_path(directory, must_exist=True, label="shadow catalog")
    manifest = _read_json(root / OUTPUT_FILES["manifest"])
    provenance = _read_json(root / OUTPUT_FILES["provenance"])
    run_projection = _read_json(root / OUTPUT_FILES["run"])
    if manifest.get("artifact_version") != SHADOW_RUN_ARTIFACT_VERSION:
        raise ValueError("unsupported shadow catalog artifact version")
    if manifest.get("frozen_read") is not False or manifest.get("gold_loaded") is not False:
        raise ValueError("shadow catalog must prove frozen_read=false and gold_loaded=false")
    if manifest.get("body_free_outputs") is not True or provenance.get("body_free_outputs") is not True:
        raise ValueError("shadow catalog must prove body_free_outputs=true")
    for label, value in (
        ("shadow catalog manifest", manifest),
        ("shadow catalog provenance", provenance),
        ("shadow catalog run", run_projection),
    ):
        _assert_body_free(value, label=label)
    required = ("analysis_run_id", "source", "provider_status", "llm_accepted")
    for key in required:
        if key not in manifest:
            raise ValueError("shadow catalog manifest is missing %s" % key)
    analysis_run_id = str(manifest.get("analysis_run_id") or "").strip()
    run_id = str(manifest.get("run_id") or "").strip()
    source = str(manifest.get("source") or "").strip()
    source_marker = str(manifest.get("source_marker") or "").strip()
    provider_status = str(manifest.get("provider_status") or "").strip()
    if not analysis_run_id or not run_id or not source or source not in SOURCE_MARKERS:
        raise ValueError("shadow catalog has an empty run identity/source")
    if source_marker != source:
        raise ValueError("shadow catalog source_marker disagrees with source")
    if provider_status not in PUBLIC_PROVIDER_STATUSES:
        raise ValueError("shadow catalog has an invalid provider_status")
    if not isinstance(manifest.get("llm_accepted"), bool):
        raise ValueError("shadow catalog llm_accepted must be bool")
    if manifest.get("llm_accepted") and provider_status != "succeeded":
        raise ValueError("accepted shadow catalog must have succeeded provider_status")
    if not manifest.get("llm_accepted") and not str(manifest.get("fallback_reason") or "").strip():
        raise ValueError("rejected shadow catalog must have fallback_reason")
    for key in required:
        if provenance.get(key) != manifest.get(key):
            raise ValueError("shadow catalog provenance disagrees on %s" % key)
        if run_projection.get(key) != manifest.get(key):
            raise ValueError("shadow run projection disagrees on %s" % key)
    for label, value in (
        ("provenance", provenance),
        ("run projection", run_projection),
    ):
        if str(value.get("source_marker") or "") != source:
            raise ValueError("shadow catalog %s source_marker disagrees" % label)
    status_counts = manifest.get("decision_status_counts")
    if not isinstance(status_counts, Mapping):
        raise ValueError("shadow catalog is missing decision_status_counts")
    thread_count = int(manifest.get("thread_count") or 0)
    event_candidate_count = int(manifest.get("event_candidate_count") or 0)
    if event_candidate_count and not manifest.get("llm_accepted"):
        raise ValueError("rejected shadow catalog cannot expose event candidates")
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
            artifact_version=str(manifest.get("artifact_version")),
            manifest=manifest,
            provenance=provenance,
            run_projection=run_projection,
            thread_count=thread_count,
            event_candidate_count=event_candidate_count,
        ),
    )


def write_api_validation_report(
    directory: Union[str, Path],
    *,
    selected: Mapping[str, Any],
    unknown: Mapping[str, Any],
    selected_status: Optional[int] = None,
    unknown_status: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist a body-free report for the explicit API E2E check."""

    root = _guard_path(directory, must_exist=True, label="shadow validation")
    manifest = _read_json(root / OUTPUT_FILES["manifest"])
    required = ("analysis_run_id", "source", "provider_status", "llm_accepted")
    selected_match = all(selected.get(key) == manifest.get(key) for key in required)
    unknown_safe = (
        unknown.get("ok") is False
        and unknown.get("fallback_reason") == "analysis_run_not_found"
        and unknown.get("analysis_run_id") != manifest.get("analysis_run_id")
    )
    selected_http_ok = selected_status is None or int(selected_status) == 200
    unknown_http_ok = unknown_status is None or int(unknown_status) == 404
    report = {
        "validation_version": SHADOW_RUN_VALIDATION_VERSION,
        "kind": "shadow_api_e2e",
        "analysis_run_id": manifest.get("analysis_run_id"),
        "required_envelope_fields": list(required),
        "selected_matches_artifact": bool(selected_match),
        "selected_http_status": selected_status,
        "selected_http_status_ok": bool(selected_http_ok),
        "unknown_http_status": unknown_status,
        "unknown_http_404": bool(unknown_http_ok and unknown_status is not None),
        "unknown_run_rejected_without_fallback": bool(unknown_safe and unknown_http_ok),
        "selected_provider_status": selected.get("provider_status"),
        "selected_llm_accepted": selected.get("llm_accepted"),
        "selected_source": selected.get("source"),
        "unknown_provider_status": unknown.get("provider_status"),
        "unknown_llm_accepted": unknown.get("llm_accepted"),
        "body_free": not bool(_body_fields(selected) or _body_fields(unknown)),
        "frozen_read": False,
        "gold_loaded": False,
    }
    _assert_body_free(report, label="shadow API validation report")
    _write_json(root / OUTPUT_FILES["api_validation"], report)
    return report


__all__ = [
    "SHADOW_RUN_ARTIFACT_VERSION",
    "SHADOW_RUN_SCHEMA_VERSION",
    "SHADOW_RUN_PROVENANCE_VERSION",
    "SHADOW_RUN_VALIDATION_VERSION",
    "SOURCE_ARTIFACT_VERSION",
    "OUTPUT_FILES",
    "ShadowRunArtifact",
    "ShadowRunCatalogEntry",
    "run_development_shadow_run_v26",
    "load_shadow_run_catalog",
    "write_api_validation_report",
]
