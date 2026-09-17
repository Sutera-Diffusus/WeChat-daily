"""Development-only runner for the contextual bundle shadow pipeline.

The runner is intentionally narrower than the general in-memory pipeline:
only one explicitly supplied ``development/messages.private.jsonl`` is read,
and no frozen/frozen_test directory is ever traversed.  The file may contain
already-redacted message text.  That text is mapped into the in-memory public
``content`` field and is never written to any artifact by this runner.

All output files are under a caller-selected new version directory.  The
registry, gate, bundle, snapshot, decision, relation, request, cost, error,
aggregate and manifest projections are body-free and include hashes sufficient
for a local replay.  This module does not load gold labels or call event/title/
frontend code.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contextual_bundle_pipeline import (
    AIProviderConfig,
    ContextualBundlePipeline,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_LLM_BUNDLE_CALLS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    PIPELINE_SCHEMA_VERSION,
    PIPELINE_VERSION,
    ProviderHealthResult,
    PipelineRunResult,
    SemanticFrameBundleModel,
    _body_free,
    _jsonable,
    run_provider_health_check,
)


RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v1"
SPLIT_DEVELOPMENT = "development"
LOCAL_DAY = "2026-08-25"
INPUT_FILENAME = "messages.private.jsonl"
OUTPUT_FILENAMES = {
    "registry": "registry.private.json",
    "gate": "gate.private.json",
    "bundles": "bundles.private.jsonl",
    "snapshots": "snapshots.private.json",
    "open_context_snapshots": "open_context_snapshots.private.jsonl",
    "decisions": "decisions.private.jsonl",
    "relations": "relations.private.jsonl",
    "requests": "requests.private.jsonl",
    "cost": "cost.private.json",
    "errors": "errors.private.jsonl",
    "aggregate": "aggregate.private.json",
    "manifest": "manifest.private.json",
}
HEALTH_FILENAME = "provider_health.private.json"
HEALTH_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_1"
CAPABILITY_OVERRIDE_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_3"
SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_4"
SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_5"
SEMANTIC_FRAME_FINAL_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_6"
SEMANTIC_FRAME_AGGREGATE_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_7"
SEMANTIC_WIRE_RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_8"


@dataclass(frozen=True)
class ShadowPilotResult:
    """A body-free pointer set for one completed development pilot."""

    input_directory: str
    output_directory: str
    message_count: int
    candidate_bundle_count: int
    decision_count: int
    mode: str
    manifest_path: str
    artifact_paths: Mapping[str, str]
    aggregate: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "message_count": self.message_count,
            "candidate_bundle_count": self.candidate_bundle_count,
            "decision_count": self.decision_count,
            "mode": self.mode,
            "manifest_path": self.manifest_path,
            "artifact_paths": dict(self.artifact_paths),
            "aggregate": _body_free(self.aggregate),
        }


@dataclass(frozen=True)
class ProviderPilotResult:
    """Health-first v2.1 result; blocked runs contain no development read."""

    health: Mapping[str, Any]
    pilot: Optional[ShadowPilotResult]
    output_directory: str
    manifest_path: str
    diagnostic: Mapping[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.health.get("ok")) and self.pilot is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "health": _body_free(self.health),
            "pilot": self.pilot.to_dict() if self.pilot is not None else None,
            "output_directory": self.output_directory,
            "manifest_path": self.manifest_path,
            "diagnostic": _body_free(self.diagnostic),
        }


def _guard_development_directory(directory: Union[str, Path]) -> Path:
    root = Path(directory)
    if root.name.casefold() != SPLIT_DEVELOPMENT:
        raise ValueError("contextual bundle pilot input must end in development")
    if any(part.casefold() in {"frozen", "frozen_test"} for part in root.parts):
        raise ValueError("contextual bundle pilot refuses frozen directories")
    if not root.is_dir():
        raise ValueError("development input directory does not exist")
    return root


def _guard_output_directory(directory: Union[str, Path], input_directory: Path) -> Path:
    root = Path(directory)
    if root.resolve() == input_directory.resolve():
        raise ValueError("output directory must differ from development input")
    if any(part.casefold() in {"frozen", "frozen_test"} for part in root.parts):
        raise ValueError("contextual bundle pilot refuses frozen output paths")
    if root.exists():
        raise FileExistsError("refusing to overwrite existing pilot output")
    return root


def _read_messages(directory: Union[str, Path]) -> Tuple[Tuple[Dict[str, Any], ...], bytes]:
    root = _guard_development_directory(directory)
    path = root / INPUT_FILENAME
    if not path.is_file():
        raise ValueError("development messages.private.jsonl is missing")
    raw = path.read_bytes()
    messages: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid development message JSON at line %d" % line_number) from exc
        if not isinstance(value, Mapping):
            raise ValueError("development message line %d is not an object" % line_number)
        if value.get("split") != SPLIT_DEVELOPMENT:
            raise ValueError("development input contains a non-development message")
        if value.get("local_day") not in (None, LOCAL_DAY):
            raise ValueError("development input contains a message outside 2026-08-25")
        message_id = str(value.get("message_id") or "")
        if not message_id:
            raise ValueError("development message line %d is missing message_id" % line_number)
        if message_id in seen:
            raise ValueError("duplicate development message_id")
        seen.add(message_id)
        messages.append(dict(value))
    if not messages:
        raise ValueError("development message file is empty")
    return tuple(messages), raw


def _public_pipeline_messages(messages: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Any], ...]:
    """Map only public/redacted fields into the in-memory pipeline envelope."""

    allowed = (
        "message_id",
        "account_id",
        "chat_id",
        "chat_type",
        "speaker_id",
        "sender_id",
        "direction",
        "content",
        "text",
        "message_text",
        "message_type",
        "timestamp",
        "time_offset_seconds",
        "time_offset",
        "sequence_in_chat",
        "sequence",
        "reply_to_message_id",
        "reply_to_id",
        "quoted_message_id",
        "quote_message_id",
        "referenced_message_id",
        "reference_message_id",
        "parent_message_id",
        "in_reply_to",
        "dialogue_segment_id",
        "segment_id",
        "dialogue_role",
        "message_role",
        "source_mode",
        "adapter_version",
        "event_time_precision",
        "language_hint",
        "metadata_revision",
        "context_message_ids",
        "semantic_channel",
        "gate_channel",
        "cold_recoverable",
        "media_state",
        "fragment_candidates",
        "fragments",
        "claims",
        "mentioned_persons",
        "mentioned_people",
        "person_mentions",
        "mentions",
        "object",
        "object_ref",
        "objects",
        "object_refs",
        "target",
        "target_entity",
        "split",
    )
    result: List[Dict[str, Any]] = []
    for message in messages:
        value = {field: message[field] for field in allowed if field in message}
        if "content" not in value:
            redacted = message.get("redacted_text")
            if redacted is not None:
                value["content"] = redacted
        result.append(value)
    return tuple(result)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_code() -> str:
    """Hash the independent pipeline/runner and active semantic adapter code.

    The wire adapter is lazy-imported by the runner, so it must be included in
    the replay identity explicitly; otherwise a wire-only implementation change
    could leave a pilot manifest claiming the old code hash.
    """

    here = Path(__file__).resolve()
    source_paths = (
        here.with_name("contextual_bundle_pipeline.py"),
        here,
        here.with_name("bundle_semantics.py"),
        here.with_name("semantic_frame.py"),
        here.with_name("semantic_wire.py"),
    )
    digest = hashlib.sha256()
    for path in source_paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[Any]) -> None:
    rows = [_jsonable(value) for value in values]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _aggregate(result: PipelineRunResult, *, input_sha256: str, code_sha256: str, message_count: int) -> Dict[str, Any]:
    statuses = Counter(str(item.get("status", "unknown")) for item in result.decisions)
    sources = Counter(str(item.get("source", "unknown")) for item in result.decisions)
    request_statuses = Counter(str(item.get("status", "unknown")) for item in result.requests)
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "message_count": int(message_count),
        "candidate_bundle_count": len(result.bundles),
        "decision_count": len(result.decisions),
        "relation_count": len(result.relations),
        "open_context_snapshot_count": len(result.snapshots.get("open_context_snapshots", ())),
        "error_count": len(result.errors),
        "input_sha256": input_sha256,
        "code_sha256": code_sha256,
        "status_counts": dict(sorted(statuses.items())),
        "source_counts": dict(sorted(sources.items())),
        "request_status_counts": dict(sorted(request_statuses.items())),
        "cost": _body_free(result.cost),
        "replay_key": result.manifest.get("replay_key"),
        "scoring": "N/A",
    }


def run_development_shadow_pilot(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    mode: str = "disabled",
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    provider_config: Optional[AIProviderConfig] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
    overwrite: bool = False,
) -> ShadowPilotResult:
    """Run one explicitly scoped development pilot and write body-free files.

    ``overwrite`` is retained as an explicit compatibility argument but is
    intentionally rejected: a new version directory is required for replay
    and comparison.  This prevents an accidental replacement of a prior
    artifact.
    """

    if overwrite:
        raise ValueError("pilot artifacts are immutable; choose a new output directory")
    input_root = _guard_development_directory(input_directory)
    output_root = _guard_output_directory(output_directory, input_root)
    messages, raw = _read_messages(input_root)
    code_sha256 = _sha256_code()
    input_sha256 = _sha256_bytes(raw)
    pipeline_messages = _public_pipeline_messages(messages)
    pipeline = ContextualBundlePipeline(
        mode=mode,
        model=model,
        embedder=embedder,
        provider_config=provider_config,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    result = pipeline.run(pipeline_messages, split=SPLIT_DEVELOPMENT)
    aggregate = _aggregate(result, input_sha256=input_sha256, code_sha256=code_sha256, message_count=len(messages))
    manifest = dict(result.manifest)
    manifest.update(
        {
            "runner_schema_version": RUNNER_SCHEMA_VERSION,
            "split": SPLIT_DEVELOPMENT,
            "local_day": LOCAL_DAY,
            "input_directory_name": input_root.name,
            "input_filename": INPUT_FILENAME,
            "input_sha256": input_sha256,
            "pipeline_input_sha256": result.input_sha256,
            "code_sha256": code_sha256,
            "message_count": len(messages),
            "candidate_bundle_count": len(result.bundles),
            "decision_count": len(result.decisions),
            "output_directory_name": output_root.name,
            "artifact_version": output_root.name,
            "output_files": dict(OUTPUT_FILENAMES),
            "gold_loaded": False,
            "frozen_read": False,
            "accuracy": "N/A",
            "scoring": "N/A",
        }
    )
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["registry"], result.registry_snapshot)
    _write_json(output_root / OUTPUT_FILENAMES["gate"], result.gate_snapshot)
    _write_jsonl(output_root / OUTPUT_FILENAMES["bundles"], result.bundles)
    _write_json(output_root / OUTPUT_FILENAMES["snapshots"], result.snapshots)
    _write_jsonl(output_root / OUTPUT_FILENAMES["open_context_snapshots"], result.snapshots.get("open_context_snapshots", ()))
    _write_jsonl(output_root / OUTPUT_FILENAMES["decisions"], result.decisions)
    _write_jsonl(output_root / OUTPUT_FILENAMES["relations"], result.relations)
    _write_jsonl(output_root / OUTPUT_FILENAMES["requests"], result.requests)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], result.cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], result.errors)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return ShadowPilotResult(
        input_directory=str(input_root),
        output_directory=str(output_root),
        message_count=len(messages),
        candidate_bundle_count=len(result.bundles),
        decision_count=len(result.decisions),
        mode=result.mode,
        manifest_path=paths["manifest"],
        artifact_paths=paths,
        aggregate=aggregate,
    )


def _provider_blocked_artifact(
    output_root: Path,
    *,
    health: ProviderHealthResult,
    code_sha256: str,
    settings_source: str,
    runner_schema_version: str = HEALTH_RUNNER_SCHEMA_VERSION,
    extra_manifest: Optional[Mapping[str, Any]] = None,
    extra_aggregate: Optional[Mapping[str, Any]] = None,
) -> ProviderPilotResult:
    """Write a body-free diagnostic without opening the development file."""

    health_payload = health.to_dict()
    error = {
        "code": health.error_code or "provider_health_failed",
        "source": health.source,
        "severity": "high",
        "stage": "provider_health",
        "development_input_read": False,
    }
    error["error_hash"] = hashlib.sha256(
        json.dumps(error, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "runner_schema_version": runner_schema_version,
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "artifact_version": output_root.name,
        "mode": "real",
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "settings_source": settings_source,
        "provider_health": health_payload,
        "provider_blocked": True,
        "development_input_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "input_sha256": "N/A",
        "code_sha256": code_sha256,
        "accuracy": "N/A",
        "scoring": "N/A",
        "minimal_configuration": {
            "provider": health.source,
            "model": health.model,
            "credential_required": True,
            "endpoint_required": True,
            "message": "Provide a valid provider credential and reachable OpenAI-compatible endpoint, then rerun the synthetic health probe.",
        },
    }
    if extra_manifest:
        manifest.update(dict(extra_manifest))
    aggregate = {
        "runner_schema_version": runner_schema_version,
        "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "provider_blocked": True,
        "development_input_read": False,
        "message_count": "N/A",
        "candidate_bundle_count": "N/A",
        "decision_count": "N/A",
        "status_counts": {},
        "provider_health": health_payload,
        "error_count": 1,
        "input_sha256": "N/A",
        "code_sha256": code_sha256,
        "accuracy": "N/A",
        "scoring": "N/A",
        "minimal_configuration": manifest["minimal_configuration"],
    }
    if extra_aggregate:
        aggregate.update(dict(extra_aggregate))
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / HEALTH_FILENAME, health_payload)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], (error,))
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    diagnostic = {
        "provider_blocked": True,
        "development_input_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "health_error_code": health.error_code,
        "minimal_configuration": manifest["minimal_configuration"],
    }
    paths = {
        "provider_health": str(output_root / HEALTH_FILENAME),
        "errors": str(output_root / OUTPUT_FILENAMES["errors"]),
        "aggregate": str(output_root / OUTPUT_FILENAMES["aggregate"]),
        "manifest": str(output_root / OUTPUT_FILENAMES["manifest"]),
    }
    return ProviderPilotResult(
        health=health_payload,
        pilot=None,
        output_directory=str(output_root),
        manifest_path=paths["manifest"],
        diagnostic={**diagnostic, "artifact_paths": paths},
    )


def run_development_shadow_pilot_v21(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    workbench_settings_path: Optional[Union[str, Path]] = None,
    provider_config: Optional[AIProviderConfig] = None,
    model: Optional[Any] = None,
    health_model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Health-gate a real provider before reading the development split.

    If the one synthetic probe fails, this function writes only a compact
    blocked diagnostic and does not open ``messages.private.jsonl``.  On
    success it delegates to the ordinary development runner with the same
    provider config and 14-call budget defaults.
    """

    input_root = _guard_development_directory(input_directory)
    output_root = _guard_output_directory(output_directory, input_root)
    settings_source = "environment"
    if provider_config is None:
        if workbench_settings_path is not None:
            provider_config = AIProviderConfig.from_workbench_settings_path(workbench_settings_path)
            settings_source = "workbench_settings"
        else:
            provider_config = AIProviderConfig.from_environment()
    elif workbench_settings_path is not None:
        settings_source = "workbench_settings+explicit"
    health_adapter = health_model
    if health_adapter is None and model is not None and callable(getattr(model, "health_check", None)):
        health_adapter = model
    health = run_provider_health_check(
        provider_config,
        model=health_adapter,
        max_input_tokens=500,
        max_output_tokens=100,
    )
    code_sha256 = _sha256_code()
    if not health.ok:
        return _provider_blocked_artifact(
            output_root,
            health=health,
            code_sha256=code_sha256,
            settings_source=settings_source,
        )
    pilot = run_development_shadow_pilot(
        input_root,
        output_root,
        mode="real",
        model=model,
        embedder=embedder,
        provider_config=provider_config,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    health_path = output_root / HEALTH_FILENAME
    _write_json(health_path, health.to_dict())
    manifest_path = output_root / OUTPUT_FILENAMES["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "runner_schema_version": HEALTH_RUNNER_SCHEMA_VERSION,
            "artifact_version": output_root.name,
            "settings_source": settings_source,
            "provider_health": health.to_dict(),
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
        }
    )
    _write_json(manifest_path, manifest)
    aggregate_path = output_root / OUTPUT_FILENAMES["aggregate"]
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate.update(
        {
            "runner_schema_version": HEALTH_RUNNER_SCHEMA_VERSION,
            "provider_health": health.to_dict(),
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
        }
    )
    _write_json(aggregate_path, aggregate)
    diagnostic = {
        "provider_blocked": False,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "artifact_paths": {**pilot.artifact_paths, "provider_health": str(health_path)},
    }
    return ProviderPilotResult(
        health=health.to_dict(),
        pilot=pilot,
        output_directory=pilot.output_directory,
        manifest_path=pilot.manifest_path,
        diagnostic=diagnostic,
    )


def run_development_shadow_pilot_v23(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: ProviderHealthResult,
    capability_artifact: Optional[Union[str, Path]] = None,
    settings_source: str = "workbench_settings",
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Run development with a successful capability-probe model override.

    ``provider_health`` is intentionally supplied by the capability probe so
    this stage does not spend a fourth health call or mutate WorkbenchSettings.
    A failed or model-mismatched probe is written as a blocked diagnostic and
    the development message file is never opened.
    """

    if not isinstance(provider_config, AIProviderConfig):
        raise TypeError("provider_config must be an AIProviderConfig")
    if not isinstance(provider_health, ProviderHealthResult):
        raise TypeError("provider_health must be a ProviderHealthResult")
    input_root = _guard_development_directory(input_directory)
    output_root = _guard_output_directory(output_directory, input_root)
    health = provider_health
    if health.ok and health.model != provider_config.model:
        health = ProviderHealthResult(
            ok=False,
            status="blocked",
            source=health.source,
            model=health.model,
            request_sha256=health.request_sha256,
            input_tokens=health.input_tokens,
            output_tokens=health.output_tokens,
            max_input_tokens=health.max_input_tokens,
            max_output_tokens=health.max_output_tokens,
            latency_ms=health.latency_ms,
            error_code="provider_health_model_mismatch",
            config=provider_config.public_dict(),
        )
    code_sha256 = _sha256_code()
    if not health.ok:
        return _provider_blocked_artifact(
            output_root,
            health=health,
            code_sha256=code_sha256,
            settings_source=settings_source,
        )

    pilot = run_development_shadow_pilot(
        input_root,
        output_root,
        mode="real",
        model=model,
        embedder=embedder,
        provider_config=provider_config,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    health_path = output_root / HEALTH_FILENAME
    _write_json(health_path, health.to_dict())
    manifest_path = output_root / OUTPUT_FILENAMES["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    override = {
        "model": provider_config.model,
        "source": "capability_matrix",
        "explicit": True,
        "global_settings_mutated": False,
    }
    manifest.update(
        {
            "runner_schema_version": CAPABILITY_OVERRIDE_RUNNER_SCHEMA_VERSION,
            "artifact_version": output_root.name,
            "settings_source": settings_source,
            "provider_health": health.to_dict(),
            "provider_health_reused": True,
            "provider_model_override": override,
            "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
        }
    )
    _write_json(manifest_path, manifest)
    aggregate_path = output_root / OUTPUT_FILENAMES["aggregate"]
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate.update(
        {
            "runner_schema_version": CAPABILITY_OVERRIDE_RUNNER_SCHEMA_VERSION,
            "provider_health": health.to_dict(),
            "provider_health_reused": True,
            "provider_model_override": override,
            "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
        }
    )
    _write_json(aggregate_path, aggregate)
    diagnostic = {
        "provider_blocked": False,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_health_reused": True,
        "provider_model_override": override,
        "artifact_paths": {**pilot.artifact_paths, "provider_health": str(health_path)},
    }
    return ProviderPilotResult(
        health=health.to_dict(),
        pilot=pilot,
        output_directory=pilot.output_directory,
        manifest_path=pilot.manifest_path,
        diagnostic=diagnostic,
    )


def run_development_shadow_pilot_v24(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: ProviderHealthResult,
    capability_artifact: Optional[Union[str, Path]] = None,
    settings_source: str = "workbench_settings",
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Run development with a successful semantic-frame model override.

    The successful frame health object is reused from the bounded capability
    probe, so this stage performs no additional health request.  The default
    model is the no-``response_format`` adapter; callers may inject a fake for
    deterministic tests.
    """

    if not isinstance(provider_config, AIProviderConfig):
        raise TypeError("provider_config must be an AIProviderConfig")
    if not isinstance(provider_health, ProviderHealthResult):
        raise TypeError("provider_health must be a ProviderHealthResult")
    input_root = _guard_development_directory(input_directory)
    output_root = _guard_output_directory(output_directory, input_root)
    health = provider_health
    if health.ok and health.model != provider_config.model:
        health = ProviderHealthResult(
            ok=False,
            status="blocked",
            source=health.source,
            model=health.model,
            request_sha256=health.request_sha256,
            input_tokens=health.input_tokens,
            output_tokens=health.output_tokens,
            max_input_tokens=health.max_input_tokens,
            max_output_tokens=health.max_output_tokens,
            latency_ms=health.latency_ms,
            error_code="provider_health_model_mismatch",
            config=provider_config.public_dict(),
        )
    code_sha256 = _sha256_code()
    override = {
        "model": provider_config.model,
        "source": "semantic_frame_capability_matrix",
        "protocol": "semantic_frame_v1",
        "explicit": True,
        "global_settings_mutated": False,
    }
    if not health.ok:
        return _provider_blocked_artifact(
            output_root,
            health=health,
            code_sha256=code_sha256,
            settings_source=settings_source,
            runner_schema_version=SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION,
            extra_manifest={
                "provider_health_reused": True,
                "provider_model_override": override,
                "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            },
            extra_aggregate={
                "provider_health_reused": True,
                "provider_model_override": override,
                "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            },
        )

    resolved_model = model if model is not None else SemanticFrameBundleModel(provider_config)
    pilot = run_development_shadow_pilot(
        input_root,
        output_root,
        mode="real",
        model=resolved_model,
        embedder=embedder,
        provider_config=provider_config,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    health_path = output_root / HEALTH_FILENAME
    _write_json(health_path, health.to_dict())
    manifest_path = output_root / OUTPUT_FILENAMES["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "runner_schema_version": SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION,
            "artifact_version": output_root.name,
            "settings_source": settings_source,
            "provider_health": health.to_dict(),
            "provider_health_reused": True,
            "provider_model_override": override,
            "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
        }
    )
    _write_json(manifest_path, manifest)
    aggregate_path = output_root / OUTPUT_FILENAMES["aggregate"]
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate.update(
        {
            "runner_schema_version": SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION,
            "provider_health": health.to_dict(),
            "provider_health_reused": True,
            "provider_model_override": override,
            "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
        }
    )
    _write_json(aggregate_path, aggregate)
    diagnostic = {
        "provider_blocked": False,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_health_reused": True,
        "provider_model_override": override,
        "artifact_paths": {**pilot.artifact_paths, "provider_health": str(health_path)},
    }
    return ProviderPilotResult(
        health=health.to_dict(),
        pilot=pilot,
        output_directory=pilot.output_directory,
        manifest_path=pilot.manifest_path,
        diagnostic=diagnostic,
    )


def run_development_shadow_pilot_v25(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: ProviderHealthResult,
    capability_artifact: Optional[Union[str, Path]] = None,
    settings_source: str = "workbench_settings",
    thinking_disabled: bool = True,
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Run the compact semantic-frame pilot after a successful bundle health probe.

    The supplied health result is reused verbatim, so this stage never spends
    another synthetic health call.  A failed or model-mismatched probe writes
    only a body-free blocked artifact and does not open the development input.
    ``thinking_disabled`` is an explicit per-run adapter option; it is recorded
    for replay and never written to global WorkbenchSettings.
    """

    if not isinstance(provider_config, AIProviderConfig):
        raise TypeError("provider_config must be an AIProviderConfig")
    if not isinstance(provider_health, ProviderHealthResult):
        raise TypeError("provider_health must be a ProviderHealthResult")
    input_root = _guard_development_directory(input_directory)
    output_root = _guard_output_directory(output_directory, input_root)
    health = provider_health
    if health.ok and health.model != provider_config.model:
        health = ProviderHealthResult(
            ok=False,
            status="blocked",
            source=health.source,
            model=health.model,
            request_sha256=health.request_sha256,
            input_tokens=health.input_tokens,
            output_tokens=health.output_tokens,
            max_input_tokens=health.max_input_tokens,
            max_output_tokens=health.max_output_tokens,
            latency_ms=health.latency_ms,
            error_code="provider_health_model_mismatch",
            config=provider_config.public_dict(),
            diagnostics=health.diagnostics,
        )
    code_sha256 = _sha256_code()
    override = {
        "model": provider_config.model,
        "source": "semantic_frame_bundle_health_capability_matrix",
        "protocol": "semantic_frame_v1",
        "explicit": True,
        "thinking_disabled": bool(thinking_disabled),
        "global_settings_mutated": False,
    }
    extra_manifest = {
        "provider_health_reused": True,
        "provider_health_probe": "synthetic_bundle",
        "provider_model_override": override,
        "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
        "response_format_sent": False,
        "compact_input_chars_limit": 1800,
        "provider_health_metadata_only": True,
    }
    extra_aggregate = dict(extra_manifest)
    if not health.ok:
        return _provider_blocked_artifact(
            output_root,
            health=health,
            code_sha256=code_sha256,
            settings_source=settings_source,
            runner_schema_version=SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION,
            extra_manifest=extra_manifest,
            extra_aggregate=extra_aggregate,
        )

    resolved_model = (
        model
        if model is not None
        else SemanticFrameBundleModel(
            provider_config,
            thinking_disabled=bool(thinking_disabled),
            max_input_chars=1800,
        )
    )
    pilot = run_development_shadow_pilot(
        input_root,
        output_root,
        mode="real",
        model=resolved_model,
        embedder=embedder,
        provider_config=provider_config,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    health_path = output_root / HEALTH_FILENAME
    _write_json(health_path, health.to_dict())
    manifest_path = output_root / OUTPUT_FILENAMES["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "runner_schema_version": SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION,
            "artifact_version": output_root.name,
            "settings_source": settings_source,
            "provider_health": health.to_dict(),
            "provider_health_reused": True,
            "provider_health_probe": "synthetic_bundle",
            "provider_model_override": override,
            "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
            "response_format_sent": False,
            "compact_input_chars_limit": 1800,
            "provider_health_metadata_only": True,
        }
    )
    _write_json(manifest_path, manifest)
    aggregate_path = output_root / OUTPUT_FILENAMES["aggregate"]
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate.update(
        {
            "runner_schema_version": SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION,
            "provider_health": health.to_dict(),
            "provider_health_reused": True,
            "provider_health_probe": "synthetic_bundle",
            "provider_model_override": override,
            "capability_artifact": str(capability_artifact) if capability_artifact is not None else "N/A",
            "provider_blocked": False,
            "development_input_read": True,
            "frozen_read": False,
            "gold_loaded": False,
            "response_format_sent": False,
            "compact_input_chars_limit": 1800,
            "provider_health_metadata_only": True,
        }
    )
    _write_json(aggregate_path, aggregate)
    diagnostic = {
        "provider_blocked": False,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "provider_health_reused": True,
        "provider_health_probe": "synthetic_bundle",
        "provider_model_override": override,
        "response_format_sent": False,
        "compact_input_chars_limit": 1800,
        "artifact_paths": {**pilot.artifact_paths, "provider_health": str(health_path)},
    }
    return ProviderPilotResult(
        health=health.to_dict(),
        pilot=pilot,
        output_directory=pilot.output_directory,
        manifest_path=pilot.manifest_path,
        diagnostic=diagnostic,
    )


def run_development_shadow_pilot_v26(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: ProviderHealthResult,
    capability_artifact: Optional[Union[str, Path]] = None,
    settings_source: str = "workbench_settings",
    thinking_disabled: bool = True,
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Finalize the compact-frame attempt and stop on incompatibility.

    C2.6 permits exactly two bundle-shaped health attempts upstream.  Once
    they are exhausted, a non-success is normalized to the public
    ``provider_incompatible`` disposition; the earlier parser code remains in
    body-free diagnostics.  The implementation delegates the common immutable
    artifact writing to v2.5 and then upgrades only its version/disposition
    metadata, never its model output.
    """

    if not isinstance(provider_config, AIProviderConfig):
        raise TypeError("provider_config must be an AIProviderConfig")
    if not isinstance(provider_health, ProviderHealthResult):
        raise TypeError("provider_health must be a ProviderHealthResult")
    health = provider_health
    if not health.ok and health.error_code != "provider_incompatible":
        diagnostics = dict(health.diagnostics)
        if health.error_code:
            diagnostics.setdefault("underlying_error_code", health.error_code)
        diagnostics.setdefault("probe_attempts_exhausted", True)
        health = ProviderHealthResult(
            ok=False,
            status="blocked",
            source=health.source,
            model=health.model,
            request_sha256=health.request_sha256,
            input_tokens=health.input_tokens,
            output_tokens=health.output_tokens,
            max_input_tokens=health.max_input_tokens,
            max_output_tokens=health.max_output_tokens,
            latency_ms=health.latency_ms,
            error_code="provider_incompatible",
            config=health.config or provider_config.public_dict(),
            diagnostics=diagnostics,
        )
    result = run_development_shadow_pilot_v25(
        input_directory,
        output_directory,
        provider_config=provider_config,
        provider_health=health,
        capability_artifact=capability_artifact,
        settings_source=settings_source,
        thinking_disabled=thinking_disabled,
        model=model,
        embedder=embedder,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    override = {
        "model": provider_config.model,
        "source": "semantic_frame_compact_final_v2_6",
        "protocol": "semantic_frame_v1",
        "explicit": True,
        "thinking_disabled": bool(thinking_disabled),
        "global_settings_mutated": False,
    }
    artifact_root = Path(result.output_directory)
    for filename in (OUTPUT_FILENAMES["manifest"], OUTPUT_FILENAMES["aggregate"]):
        path = artifact_root / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "runner_schema_version": SEMANTIC_FRAME_FINAL_RUNNER_SCHEMA_VERSION,
                "provider_model_override": override,
                "provider_health": health.to_dict(),
                "provider_health_reused": True,
                "provider_health_probe": "synthetic_bundle_compact_final",
                "provider_incompatible": not health.ok,
                "provider_attempts_exhausted": not health.ok,
                "response_format_sent": False,
                "compact_input_chars_limit": 1800,
                "provider_health_metadata_only": True,
            }
        )
        _write_json(path, payload)
    health_path = artifact_root / HEALTH_FILENAME
    _write_json(health_path, health.to_dict())
    diagnostic = dict(result.diagnostic)
    diagnostic.update(
        {
            "provider_model_override": override,
            "provider_health_probe": "synthetic_bundle_compact_final",
            "provider_incompatible": not health.ok,
            "provider_attempts_exhausted": not health.ok,
            "response_format_sent": False,
            "compact_input_chars_limit": 1800,
            "artifact_paths": {**result.diagnostic.get("artifact_paths", {}), "provider_health": str(health_path)},
        }
    )
    return ProviderPilotResult(
        health=health.to_dict(),
        pilot=result.pilot,
        output_directory=result.output_directory,
        manifest_path=result.manifest_path,
        diagnostic=diagnostic,
    )


def run_development_shadow_pilot_v27(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: ProviderHealthResult,
    capability_artifact: Optional[Union[str, Path]] = None,
    settings_source: str = "workbench_settings",
    thinking_disabled: bool = True,
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Run the schema-focused v2.7 shadow pilot using an existing health proof.

    This wrapper does not perform health discovery.  It reuses the successful
    v2.6 proof and only changes the artifact version/diagnostic namespace so
    the v2.7 request ledger can be compared independently.
    """

    result = run_development_shadow_pilot_v26(
        input_directory,
        output_directory,
        provider_config=provider_config,
        provider_health=provider_health,
        capability_artifact=capability_artifact,
        settings_source=settings_source,
        thinking_disabled=thinking_disabled,
        model=model,
        embedder=embedder,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    artifact_root = Path(result.output_directory)
    override = {
        "model": provider_config.model,
        "source": "semantic_frame_schema_focus_v2_7",
        "protocol": "semantic_frame_v1",
        "explicit": True,
        "thinking_disabled": bool(thinking_disabled),
        "global_settings_mutated": False,
    }
    effective_health = dict(result.health)
    effective_health_ok = bool(effective_health.get("ok"))
    for filename in (OUTPUT_FILENAMES["manifest"], OUTPUT_FILENAMES["aggregate"]):
        path = artifact_root / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "runner_schema_version": SEMANTIC_FRAME_AGGREGATE_RUNNER_SCHEMA_VERSION,
                "provider_model_override": override,
                "provider_health": effective_health,
                "provider_health_reused": True,
                "provider_health_probe": "synthetic_bundle_compact_final_reused",
                "provider_incompatible": not effective_health_ok,
                "provider_attempts_exhausted": False if effective_health_ok else True,
                "error_taxonomy_version": "semantic_frame_safe_taxonomy_v1",
                "response_format_sent": False,
                "compact_input_chars_limit": 1800,
                "provider_health_metadata_only": True,
            }
        )
        _write_json(path, payload)
    health_path = artifact_root / HEALTH_FILENAME
    _write_json(health_path, effective_health)
    diagnostic = dict(result.diagnostic)
    diagnostic.update(
        {
            "provider_model_override": override,
            "provider_health_probe": "synthetic_bundle_compact_final_reused",
            "provider_incompatible": not effective_health_ok,
            "provider_attempts_exhausted": False if effective_health_ok else True,
            "error_taxonomy_version": "semantic_frame_safe_taxonomy_v1",
            "response_format_sent": False,
            "compact_input_chars_limit": 1800,
            "artifact_paths": {**result.diagnostic.get("artifact_paths", {}), "provider_health": str(health_path)},
        }
    )
    return ProviderPilotResult(
        health=effective_health,
        pilot=result.pilot,
        output_directory=result.output_directory,
        manifest_path=result.manifest_path,
        diagnostic=diagnostic,
    )


def run_development_shadow_pilot_v28(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: ProviderHealthResult,
    capability_artifact: Optional[Union[str, Path]] = None,
    settings_source: str = "workbench_settings",
    thinking_disabled: bool = True,
    model: Optional[Any] = None,
    embedder: Optional[Any] = None,
    max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
) -> ProviderPilotResult:
    """Run v2.8 with a local symbol-table wire model and canonical assembly."""

    from .semantic_wire import SemanticWireBundleModel, WIRE_PROMPT_VERSION

    resolved_model = (
        model
        if model is not None
        else SemanticWireBundleModel(
            provider_config,
            thinking_disabled=bool(thinking_disabled),
            max_input_chars=1800,
        )
    )
    result = run_development_shadow_pilot_v27(
        input_directory,
        output_directory,
        provider_config=provider_config,
        provider_health=provider_health,
        capability_artifact=capability_artifact,
        settings_source=settings_source,
        thinking_disabled=thinking_disabled,
        model=resolved_model,
        embedder=embedder,
        max_bundle_calls=max_bundle_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
    )
    artifact_root = Path(result.output_directory)
    effective_health = dict(result.health)
    effective_health_ok = bool(effective_health.get("ok"))
    override = {
        "model": provider_config.model,
        "source": "semantic_wire_symbol_table_v2_8",
        "protocol": "semantic_wire_v1",
        "explicit": True,
        "thinking_disabled": bool(thinking_disabled),
        "global_settings_mutated": False,
    }
    metadata = {
        "runner_schema_version": SEMANTIC_WIRE_RUNNER_SCHEMA_VERSION,
        "provider_model_override": override,
        "provider_health": effective_health,
        "provider_health_reused": True,
        "provider_health_probe": "synthetic_bundle_compact_final_reused",
        "provider_incompatible": not effective_health_ok,
        "provider_attempts_exhausted": False if effective_health_ok else True,
        "wire_schema_version": "semantic_wire_v1",
        "wire_prompt_version": WIRE_PROMPT_VERSION,
        "canonical_schema_version": "bundle_semantics_v1",
        "wire_symbol_table_enforced": True,
        "authoritative_metadata_source": "registry_pipeline",
        "model_authoritative_id_write": False,
        "evidence_handle_validation": "strict_local",
        "cache_includes_symbol_table": True,
        "error_taxonomy_version": "semantic_wire_safe_taxonomy_v1",
        "response_format_sent": False,
        "compact_input_chars_limit": 1800,
        "provider_health_metadata_only": True,
    }
    for filename in (OUTPUT_FILENAMES["manifest"], OUTPUT_FILENAMES["aggregate"]):
        path = artifact_root / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(metadata)
        _write_json(path, payload)
    health_path = artifact_root / HEALTH_FILENAME
    _write_json(health_path, effective_health)
    diagnostic = dict(result.diagnostic)
    diagnostic.update(
        {
            key: value
            for key, value in metadata.items()
            if key
            not in {
                "runner_schema_version",
                "provider_health",
            }
        }
    )
    diagnostic["provider_health"] = effective_health
    diagnostic["artifact_paths"] = {**result.diagnostic.get("artifact_paths", {}), "provider_health": str(health_path)}
    return ProviderPilotResult(
        health=effective_health,
        pilot=result.pilot,
        output_directory=result.output_directory,
        manifest_path=result.manifest_path,
        diagnostic=diagnostic,
    )


__all__ = [
    "RUNNER_SCHEMA_VERSION",
    "SPLIT_DEVELOPMENT",
    "LOCAL_DAY",
    "INPUT_FILENAME",
    "OUTPUT_FILENAMES",
    "ShadowPilotResult",
    "ProviderPilotResult",
    "HEALTH_FILENAME",
    "HEALTH_RUNNER_SCHEMA_VERSION",
    "CAPABILITY_OVERRIDE_RUNNER_SCHEMA_VERSION",
    "SEMANTIC_FRAME_RUNNER_SCHEMA_VERSION",
    "SEMANTIC_FRAME_COMPACT_RUNNER_SCHEMA_VERSION",
    "SEMANTIC_FRAME_FINAL_RUNNER_SCHEMA_VERSION",
    "SEMANTIC_FRAME_AGGREGATE_RUNNER_SCHEMA_VERSION",
    "SEMANTIC_WIRE_RUNNER_SCHEMA_VERSION",
    "run_development_shadow_pilot",
    "run_development_shadow_pilot_v21",
    "run_development_shadow_pilot_v23",
    "run_development_shadow_pilot_v24",
    "run_development_shadow_pilot_v25",
    "run_development_shadow_pilot_v26",
    "run_development_shadow_pilot_v27",
    "run_development_shadow_pilot_v28",
]
