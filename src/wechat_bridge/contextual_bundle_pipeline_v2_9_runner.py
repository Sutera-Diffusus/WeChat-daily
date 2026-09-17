"""Development-only scheduler-integrated provider runner for C2.9.

The v2.9 runner is intentionally an independent boundary around the existing
shadow stages. It consumes one already-produced, body-free v2.8 structural
artifact and the provider-free v2.9 scheduler plan, then sends only the
selected representative packages through the semantic wire adapter. It never
loads frozen data, gold labels, event/title/frontend code, or provider bodies
into an artifact.

The scheduler plan is authoritative for selection. All 848 candidate
decisions are projected to the output so that activation cues for unselected
and pending work remain replayable. Provider request attempts are recorded
separately from candidate decisions; a retry consumes the same global
14-request budget and is never hidden as an extra selected package.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .bundle_semantics import (
    BUNDLE_PROMPT_VERSION,
    BUNDLE_RULESET_VERSION,
    BUNDLE_SCHEMA_VERSION,
    BundleSemanticPipeline,
    VersionedBundleCache,
    stable_hash,
)
from .contextual_bundle_pipeline import (
    AIProviderConfig,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_LLM_BUNDLE_CALLS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ProviderHealthResult,
    BudgetManager,
    _BudgetedModel,
    _body_free,
    _pending_outcome,
)
from .contextual_bundle_pipeline_runner import (
    INPUT_FILENAME,
    LOCAL_DAY,
    SPLIT_DEVELOPMENT,
    _guard_development_directory,
    _guard_output_directory,
    _public_pipeline_messages,
    _read_messages,
    _sha256_bytes,
    _write_json,
    _write_jsonl,
)
from .contextual_bundle_scheduler_v2_9 import _scheduler_code_sha256
from .semantic_wire import (
    CANONICAL_SCHEMA_VERSION,
    WIRE_PROMPT_VERSION,
    WIRE_SCHEMA_VERSION,
    SemanticWireBundleModel,
)


RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_9"
ARTIFACT_VERSION = "contextual_bundle_pipeline_v2_9"
SOURCE_ARTIFACT_VERSION = "contextual_bundle_pipeline_v2_8"
SCHEDULER_ARTIFACT_VERSION = "contextual_bundle_scheduler_v2_9"
HEALTH_SOURCE_VERSION = "contextual_bundle_pipeline_v2_6"
CAPABILITY_HEALTH_SOURCE_VERSION = "contextual_bundle_provider_semantic_frame_v1"
HEALTH_FILENAME = "provider_health.private.json"

OUTPUT_FILENAMES: Dict[str, str] = {
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
    "provider_health": HEALTH_FILENAME,
    "selection_mapping": "selection_mapping.private.jsonl",
    "scheduler": "scheduler.private.json",
}

_ACTUAL_REQUEST_STATUSES = frozenset({"started", "complete", "failed"})
_BODY_FREE_KEYS = frozenset(
    {
        "content",
        "text",
        "body",
        "raw",
        "raw_text",
        "message_text",
        "evidence_text",
        "prompt",
        "response",
        "quote",
        "summary",
    }
)


@dataclass(frozen=True)
class SchedulerInputs:
    """Validated, body-free scheduler/source projection plus in-memory input."""

    input_root: Path
    source_root: Path
    scheduler_root: Path
    messages: Tuple[Dict[str, Any], ...]
    raw_input: bytes
    source_manifest: Mapping[str, Any]
    source_bundles: Tuple[Dict[str, Any], ...]
    source_decisions: Tuple[Dict[str, Any], ...]
    scheduler_manifest: Mapping[str, Any]
    scheduler_coverage: Mapping[str, Any]
    scheduler_provenance: Mapping[str, Any]
    scheduler_decisions: Tuple[Dict[str, Any], ...]
    input_sha256: str
    scheduler_code_sha256: str
    source_file_hashes: Mapping[str, str]
    selected_rows: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class V29RunResult:
    """Body-free pointers and aggregate metrics for one immutable v2.9 run."""

    output_directory: str
    manifest_path: str
    artifact_paths: Mapping[str, str]
    manifest: Mapping[str, Any]
    aggregate: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_directory": self.output_directory,
            "manifest_path": self.manifest_path,
            "artifact_paths": dict(self.artifact_paths),
            "manifest": _body_free(self.manifest),
            "aggregate": _body_free(self.aggregate),
        }


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("expected JSON object: %s" % path.name)
    return dict(value)


def _read_jsonl(path: Path) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError("expected JSON object at %s:%d" % (path.name, line_number))
        rows.append(dict(value))
    return tuple(rows)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _guard_artifact_directory(directory: Union[str, Path], expected_name: str) -> Path:
    root = Path(directory)
    if any(part.casefold() in {"frozen", "frozen_test"} for part in root.parts):
        raise ValueError("v2.9 runner refuses frozen artifact paths")
    if not root.is_dir():
        raise ValueError("artifact directory does not exist")
    if root.name != expected_name:
        raise ValueError("unexpected artifact directory: %s" % root.name)
    return root


def _assert_body_free(value: Any, *, label: str) -> None:
    """Fail closed if any audit projection accidentally contains body keys."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if name in _BODY_FREE_KEYS or name.casefold().endswith(("_text", "_content", "_surface")):
                raise ValueError("%s contains body-like field %s" % (label, name))
            _assert_body_free(item, label=label)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _assert_body_free(item, label=label)


def _safe_hashes(root: Path, filenames: Iterable[str]) -> Dict[str, str]:
    return {filename: _sha256_file(root / filename) for filename in filenames}


def _health_payload(
    value: Union[ProviderHealthResult, Mapping[str, Any]],
    *,
    expected_model: Optional[str] = None,
) -> Dict[str, Any]:
    if isinstance(value, ProviderHealthResult):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("provider_health must be ProviderHealthResult or mapping")
    payload = _body_free(payload)
    required = (
        "ok",
        "status",
        "source",
        "model",
        "request_sha256",
        "input_tokens",
        "output_tokens",
        "max_input_tokens",
        "max_output_tokens",
        "latency_ms",
    )
    if any(key not in payload for key in required):
        raise ValueError("provider health is missing required metadata")
    if payload.get("ok") is not True:
        raise ValueError("provider health is not successful")
    if expected_model is not None and str(payload.get("model") or "") != str(expected_model):
        raise ValueError("provider health model does not match the explicit run override")
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("raw_content_saved") is True:
        raise ValueError("provider health must not retain raw content")
    _assert_body_free(payload, label="provider health")
    return payload


def _validate_scope_metadata(
    messages: Sequence[Mapping[str, Any]],
    *,
    expected_ids: Iterable[Any],
    chat_id: Any,
) -> None:
    expected = [str(item) for item in expected_ids]
    if not expected:
        raise ValueError("selected package has no source messages")
    if len(expected) != len(set(expected)):
        raise ValueError("selected package repeats a source message")
    by_id = {str(item.get("message_id")): item for item in messages}
    if set(by_id) != set(expected):
        raise ValueError("selected package references an unknown message")
    chats = {str(item.get("chat_id") or "unknown") for item in messages}
    if len(chats) > 1:
        raise ValueError("cross-chat selected package is forbidden")
    if str(chat_id or "unknown") != "unknown" and chats and str(chat_id) not in chats:
        raise ValueError("selected package chat metadata mismatch")


def _validate_scheduler_inputs(
    input_directory: Union[str, Path],
    contextual_artifact_directory: Union[str, Path],
    scheduler_artifact_directory: Union[str, Path],
) -> SchedulerInputs:
    input_root = _guard_development_directory(input_directory)
    source_root = _guard_artifact_directory(contextual_artifact_directory, SOURCE_ARTIFACT_VERSION)
    scheduler_root = _guard_artifact_directory(scheduler_artifact_directory, SCHEDULER_ARTIFACT_VERSION)
    messages, raw = _read_messages(input_root)
    input_sha256 = _sha256_bytes(raw)
    source_manifest = _read_json(source_root / "manifest.private.json")
    scheduler_manifest = _read_json(scheduler_root / "manifest.private.json")
    scheduler_coverage = _read_json(scheduler_root / "coverage.private.json")
    scheduler_provenance = _read_json(scheduler_root / "provenance.private.json")
    source_decisions = _read_jsonl(source_root / "decisions.private.jsonl")
    source_bundles = _read_jsonl(source_root / "bundles.private.jsonl")
    scheduler_decisions = _read_jsonl(scheduler_root / "decisions.private.jsonl")

    if source_manifest.get("artifact_version") != SOURCE_ARTIFACT_VERSION:
        raise ValueError("v2.9 source is not v2.8")
    if source_manifest.get("split") != SPLIT_DEVELOPMENT or source_manifest.get("local_day") != LOCAL_DAY:
        raise ValueError("v2.9 source must be the development 2026-08-25 artifact")
    if source_manifest.get("frozen_read") is not False or source_manifest.get("gold_loaded") is not False:
        raise ValueError("v2.9 source does not prove frozen/gold exclusion")
    if source_manifest.get("body_free_outputs") is not True:
        raise ValueError("v2.9 source is not body-free")
    if str(source_manifest.get("input_sha256") or "") != input_sha256:
        raise ValueError("development input does not match v2.8 source")

    required_scheduler = {
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "schema_version": "contextual_bundle_scheduler_schema_v2_9",
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "mode": "dry_run",
        "dry_run": True,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "provider_calls": 0,
        "selected_count": 14,
        "source_artifact_version": SOURCE_ARTIFACT_VERSION,
        "source_artifact_directory_name": source_root.name,
        "candidate_count": len(source_bundles),
        "decision_count": len(source_decisions),
        "estimated_message_coverage_count": 269,
    }
    for key, expected in required_scheduler.items():
        if scheduler_manifest.get(key) != expected:
            raise ValueError("scheduler manifest mismatch: %s" % key)
    if str(scheduler_manifest.get("development_input_sha256") or "") != input_sha256:
        raise ValueError("development input does not match scheduler")
    if str(scheduler_provenance.get("development_input_sha256") or "") != input_sha256:
        raise ValueError("scheduler provenance input mismatch")
    current_scheduler_hash = _scheduler_code_sha256()
    if str(scheduler_manifest.get("code_sha256") or "") != current_scheduler_hash:
        raise ValueError("scheduler code hash is stale")
    if str(scheduler_provenance.get("code_sha256") or "") != current_scheduler_hash:
        raise ValueError("scheduler provenance code hash is stale")
    if str(scheduler_coverage.get("artifact_version") or "") != SCHEDULER_ARTIFACT_VERSION:
        raise ValueError("scheduler coverage artifact mismatch")
    if scheduler_coverage.get("selected_count") != 14 or scheduler_coverage.get("estimated_message_coverage_count") != 269:
        raise ValueError("scheduler coverage does not prove 14/269")
    for field in ("source_artifact_manifest_sha256", "source_artifact_decisions_sha256", "source_artifact_bundles_sha256"):
        if not scheduler_manifest.get(field):
            raise ValueError("scheduler source hash is missing")

    source_hashes = _safe_hashes(
        source_root,
        ("manifest.private.json", "decisions.private.jsonl", "bundles.private.jsonl"),
    )
    hash_fields = {
        "manifest.private.json": "source_artifact_manifest_sha256",
        "decisions.private.jsonl": "source_artifact_decisions_sha256",
        "bundles.private.jsonl": "source_artifact_bundles_sha256",
    }
    for filename, field in hash_fields.items():
        if source_hashes[filename] != str(scheduler_manifest.get(field) or ""):
            raise ValueError("scheduler source hash mismatch: %s" % filename)

    if len(source_bundles) != len(source_decisions) or len(source_bundles) != 848:
        raise ValueError("v2.8 source must contain 848 bundle/decision rows")
    if len(scheduler_decisions) != len(source_bundles):
        raise ValueError("scheduler candidate count mismatch")
    source_bundle_ids = {str(row.get("bundle_id") or "") for row in source_bundles}
    source_decision_ids = {str(row.get("bundle_id") or "") for row in source_decisions}
    if "" in source_bundle_ids or len(source_bundle_ids) != len(source_bundles) or source_bundle_ids != source_decision_ids:
        raise ValueError("v2.8 source bundle ids are not one-to-one")
    scheduler_candidate_ids = [str(row.get("candidate_id") or "") for row in scheduler_decisions]
    if len(set(scheduler_candidate_ids)) != len(scheduler_decisions) or set(scheduler_candidate_ids) != source_bundle_ids:
        raise ValueError("scheduler candidates do not cover source bundles")

    message_by_id = {str(row.get("message_id") or ""): row for row in messages}
    if len(message_by_id) != len(messages) or len(messages) != 280:
        raise ValueError("development input must contain 280 unique messages")
    selected = [
        row
        for row in scheduler_decisions
        if row.get("selection_status") == "selected" and row.get("scheduled_for_encode") is True
    ]
    if len(selected) != 14:
        raise ValueError("scheduler selected representative count is not 14")
    selected_packages = {str(row.get("semantic_package_id") or "") for row in selected}
    if len(selected_packages) != 14:
        raise ValueError("scheduler selected package ids are not distinct")
    selected_union: set[str] = set()
    for row in selected:
        candidate_id = str(row.get("candidate_id") or "")
        if row.get("selected_representative_id") != candidate_id:
            raise ValueError("scheduler selected row is not its representative")
        ids = [str(item) for item in (row.get("source_message_ids") or ())]
        selected_union.update(ids)
        _validate_scope_metadata(
            [message_by_id[item] for item in ids if item in message_by_id],
            expected_ids=ids,
            chat_id=row.get("chat_id"),
        )
    if len(selected_union) != 269:
        raise ValueError("scheduler selected message union is not 269")
    for row in scheduler_decisions:
        cues = row.get("activation_cues")
        if not isinstance(cues, list) or not cues or row.get("activation_cue_replayable") is not True:
            raise ValueError("scheduler decision has missing activation cues")

    _assert_body_free(source_manifest, label="v2.8 manifest")
    _assert_body_free(source_bundles, label="v2.8 bundles")
    _assert_body_free(source_decisions, label="v2.8 decisions")
    _assert_body_free(scheduler_manifest, label="scheduler manifest")
    _assert_body_free(scheduler_coverage, label="scheduler coverage")
    _assert_body_free(scheduler_provenance, label="scheduler provenance")
    _assert_body_free(scheduler_decisions, label="scheduler decisions")
    return SchedulerInputs(
        input_root=input_root,
        source_root=source_root,
        scheduler_root=scheduler_root,
        messages=messages,
        raw_input=raw,
        source_manifest=source_manifest,
        source_bundles=source_bundles,
        source_decisions=source_decisions,
        scheduler_manifest=scheduler_manifest,
        scheduler_coverage=scheduler_coverage,
        scheduler_provenance=scheduler_provenance,
        scheduler_decisions=scheduler_decisions,
        input_sha256=input_sha256,
        scheduler_code_sha256=current_scheduler_hash,
        source_file_hashes=source_hashes,
        selected_rows=tuple(selected),
    )


def _health_from_v26(path: Union[str, Path]) -> Dict[str, Any]:
    health_path = Path(path)
    if any(part.casefold() in {"frozen", "frozen_test"} for part in health_path.parts):
        raise ValueError("provider health path cannot be frozen")
    if health_path.parent.name != HEALTH_SOURCE_VERSION:
        raise ValueError("v2.9 health must be the reused v2.6 proof")
    return _health_payload(_read_json(health_path), expected_model="deepseek-v4-flash")


def _health_from_capability_artifact(
    path: Union[str, Path],
    *,
    model: str,
) -> Dict[str, Any]:
    """Reuse a prior semantic-frame capability proof without probing.

    The capability artifact intentionally stores only body-free health
    metadata, not a raw provider response or a request hash. ``request_sha256``
    is therefore marked N/A rather than invented. This proof is accepted only
    for a candidate with explicit successful health and frozen/gold exclusion.
    """

    capability_path = Path(path)
    if any(part.casefold() in {"frozen", "frozen_test"} for part in capability_path.parts):
        raise ValueError("provider capability path cannot be frozen")
    if capability_path.parent.name != CAPABILITY_HEALTH_SOURCE_VERSION:
        raise ValueError("unexpected capability health source")
    payload = _read_json(capability_path)
    if payload.get("frozen_read") is not False or payload.get("gold_loaded") is not False:
        raise ValueError("capability health does not prove frozen/gold exclusion")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("capability health candidates are missing")
    matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, Mapping) and str(candidate.get("model_id") or "") == str(model)
    ]
    if len(matches) != 1:
        raise ValueError("capability health model candidate is not unique")
    candidate = matches[0]
    if candidate.get("health_ok") is not True or candidate.get("semantic_frame_confirmed") is not True:
        raise ValueError("capability health candidate is not successful")
    proof = {
        "ok": True,
        "status": str(candidate.get("health_status") or "available"),
        "source": "reused_semantic_frame_capability",
        "model": str(model),
        "request_sha256": "N/A",
        "input_tokens": int(candidate.get("health_input_tokens") or 0),
        "output_tokens": int(candidate.get("health_output_tokens") or 0),
        "max_input_tokens": 500,
        "max_output_tokens": 100,
        "latency_ms": candidate.get("health_latency_ms", "N/A"),
        "error_code": candidate.get("health_error_code"),
        "config": {"model": str(model), "api_key_configured": "N/A", "base_url_configured": "N/A"},
        "diagnostics": {
            "health_artifact_version": CAPABILITY_HEALTH_SOURCE_VERSION,
            "protocol": "semantic_frame_v1",
            "semantic_frame_confirmed": True,
            "json_object_confirmed": bool(candidate.get("json_object_confirmed")),
            "raw_content_saved": False,
            "raw_reasoning_saved": False,
        },
    }
    return _health_payload(proof, expected_model=model)


def _error_bucket(code: Any) -> str:
    value = str(code or "unknown").casefold()
    if any(token in value for token in ("evidence", "span", "boundary", "reference")):
        return "evidence"
    if any(token in value for token in ("schema", "parser", "wire", "validation", "field", "enum", "type")):
        return "schema"
    if any(token in value for token in ("token", "input", "output", "truncat", "length")):
        return "token_input_limit"
    if any(token in value for token in ("provider", "protocol", "openai", "http", "model", "response")):
        return "provider_protocol"
    if value in {"unknown", ""}:
        return "unknown"
    return "other"


def _attempt_summary(records: Sequence[Mapping[str, Any]], rejections: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    provider_records = [row for row in records if row.get("source") == "provider"]
    # A BudgetManager record is created only after a call reservation is
    # granted. Therefore even a terminal ``pending`` record (for example an
    # output-limit violation) represents an actual provider request. Budget
    # rejections are kept separate because they never reached the provider.
    attempts = list(provider_records)
    terminal_attempts = [row for row in provider_records if row.get("status") in _ACTUAL_REQUEST_STATUSES]
    failed = [row for row in provider_records if row.get("status") == "failed"]
    unfinished = [row for row in provider_records if row.get("status") == "started"]
    pending = [row for row in provider_records if row.get("status") == "pending"]
    errors = Counter(_error_bucket(row.get("error_code")) for row in list(failed) + list(pending) + list(rejections))
    return {
        "provider_request_attempts": len(attempts),
        "provider_request_terminal_rows": len(terminal_attempts),
        "successful_provider_requests": sum(row.get("status") == "complete" for row in provider_records),
        "failed_provider_attempts": len(failed),
        "unfinished_provider_attempts": len(unfinished),
        "pending_provider_attempts": len(pending),
        "attempt_status_counts": dict(sorted(Counter(str(row.get("status") or "unknown") for row in provider_records).items())),
        "provider_pending_rejections": len(rejections),
        "retry_attempts": sum(bool(row.get("retry")) for row in provider_records),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in provider_records),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in provider_records),
        "latency_ms_total": round(
            sum(
                float(row.get("latency_ms") or 0.0)
                for row in provider_records
                if isinstance(row.get("latency_ms"), (int, float))
            ),
            3,
        ),
        "error_buckets": dict(sorted(errors.items())),
    }


def _canonical_pending(
    messages: Sequence[Mapping[str, Any]],
    *,
    bundle_id: str,
    chat_id: str,
    code: str,
) -> Any:
    return _pending_outcome(
        messages,
        bundle_id=bundle_id,
        chat_id=chat_id,
        source="budget",
        code=code,
    )


def _semantic_decision(
    scheduler_row: Mapping[str, Any],
    *,
    outcome: Optional[Any],
    selected: bool,
    status: str,
    source: str,
    package_attempts: int,
    provider_attempts: int,
    retry_count: int,
    budget_deferred: bool,
    activation_cues: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "decision_version": RUNNER_SCHEMA_VERSION,
        "candidate_id": str(scheduler_row.get("candidate_id") or ""),
        "semantic_package_id": str(scheduler_row.get("semantic_package_id") or ""),
        "selection_status": scheduler_row.get("selection_status"),
        "selection_reason": scheduler_row.get("selection_reason"),
        "scheduled_for_encode": scheduler_row.get("scheduled_for_encode") is True,
        "selected_for_encode": bool(selected),
        "selected_representative_id": scheduler_row.get("selected_representative_id"),
        "channel": scheduler_row.get("channel"),
        "scale": scheduler_row.get("scale"),
        "chat_id": scheduler_row.get("chat_id"),
        "source_message_count": scheduler_row.get("source_message_count"),
        "source_message_ids": list(scheduler_row.get("source_message_ids") or ()),
        "activation_cues": [dict(item) for item in activation_cues],
        "activation_cue_count": len(activation_cues),
        "activation_cue_replayable": all(item.get("executable") is True for item in activation_cues),
        "semantic_status": status,
        "semantic_source": source,
        "package_attempt_count": int(package_attempts),
        "provider_attempt_count": int(provider_attempts),
        "retry_count": int(retry_count),
        "budget_deferred": bool(budget_deferred),
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "wire_prompt_version": WIRE_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
    }
    if outcome is None:
        row.update(
            {
                "input_sha256": "N/A",
                "cache_key": "N/A",
                "validation": "N/A",
                "semantic_bundle": None,
            }
        )
    else:
        row.update(
            {
                "input_sha256": outcome.input_sha256,
                "cache_key": outcome.cache_key,
                "validation": outcome.validation.to_dict(),
                "semantic_bundle": _body_free(outcome.bundle),
            }
        )
    return row


def _schema_evidence_metrics(outcomes: Sequence[Any]) -> Dict[str, Any]:
    complete = [item for item in outcomes if item.status == "complete"]
    valid = [item for item in complete if item.validation.ok]
    evidence_counts = [
        int(item.validation.checks.get("evidence_count") or 0)
        for item in complete
        if isinstance(item.validation.checks, Mapping)
    ]
    field_counts: Counter[str] = Counter()
    field_complete_counts: Counter[str] = Counter()
    for item in complete:
        bundle = item.bundle
        for field in (
            "speaker",
            "subject",
            "mentioned_person",
            "target",
            "object",
            "action",
            "claim_type",
            "state",
            "modality",
            "coreference_candidates",
            "context_relations",
            "uncertainties",
            "evidence",
        ):
            field_counts[field] += 1
            value = bundle.get(field)
            if value not in (None, "", "unknown", [], {}):
                field_complete_counts[field] += 1
    return {
        "complete_bundle_count": len(complete),
        "schema_valid_complete_count": len(valid),
        "schema_valid_rate": (len(valid) / len(complete)) if complete else "N/A",
        "complete_with_evidence_count": sum(item > 0 for item in evidence_counts),
        "evidence_coverage_rate": (
            sum(item > 0 for item in evidence_counts) / len(complete)
            if complete
            else "N/A"
        ),
        "evidence_count_total": sum(evidence_counts),
        "canonical_field_observation_counts": dict(sorted(field_counts.items())),
        "canonical_field_known_counts": dict(sorted(field_complete_counts.items())),
    }


def _relation_zero_tolerance(relations: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    cross_chat = 0
    time_only = 0
    same_segment = 0
    for row in relations:
        left_chat = row.get("left_chat_id")
        right_chat = row.get("right_chat_id")
        if left_chat not in (None, "", "unknown") and right_chat not in (None, "", "unknown") and left_chat != right_chat:
            cross_chat += 1
        strength = str(row.get("strength") or "none")
        signals = {str(item) for item in (row.get("supporting_signals") or ())}
        if strength == "strong" and signals and signals <= {"time", "temporal", "same_segment"}:
            time_only += 1
            if signals <= {"same_segment"}:
                same_segment += 1
    return {
        "cross_chat_relation_violations": cross_chat,
        "time_only_relation_violations": time_only,
        "same_segment_unsafe_strong": same_segment,
        "silence_terminal_violations": 0,
        "fallback_accepted": 0,
    }


def run_development_shadow_pilot_v29(
    input_directory: Union[str, Path],
    contextual_artifact_directory: Union[str, Path],
    scheduler_artifact_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: Union[ProviderHealthResult, Mapping[str, Any]],
    health_artifact: Optional[Union[str, Path]] = None,
    model: Optional[Any] = None,
    max_provider_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 1,
    thinking_disabled: bool = True,
    max_input_chars: int = 1800,
) -> V29RunResult:
    """Run exactly one immutable, scheduler-selected development pilot.

    max_retries is per selected package, while max_provider_calls is a shared
    request-attempt budget. The function raises before creating output when
    scheduler/hash/health invariants fail, so a partial artifact cannot be
    mistaken for a completed run.
    """

    if not isinstance(provider_config, AIProviderConfig):
        raise TypeError("provider_config must be AIProviderConfig")
    if int(max_provider_calls) < 0 or int(max_provider_calls) > DEFAULT_MAX_LLM_BUNDLE_CALLS:
        raise ValueError("v2.9 provider call budget must be at most 14")
    if int(max_input_tokens) < 1 or int(max_input_tokens) > DEFAULT_MAX_INPUT_TOKENS:
        raise ValueError("v2.9 input token budget must be at most 2000")
    if int(max_output_tokens) < 1 or int(max_output_tokens) > DEFAULT_MAX_OUTPUT_TOKENS:
        raise ValueError("v2.9 output token budget must be at most 400")
    if int(max_retries) < 0:
        raise ValueError("max_retries must be non-negative")

    checked = _validate_scheduler_inputs(input_directory, contextual_artifact_directory, scheduler_artifact_directory)
    output_root = _guard_output_directory(output_directory, checked.input_root)
    if str(provider_config.model) not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        raise ValueError("v2.9 explicit provider model override is not in the approved text models")
    health = _health_payload(provider_health, expected_model=provider_config.model)
    if health_artifact is not None:
        health_path = Path(health_artifact)
        if health_path.name != HEALTH_FILENAME or health_path.parent.name != HEALTH_SOURCE_VERSION:
            raise ValueError("health_artifact must point to v2.6 provider_health.private.json")
        stored_health = _health_payload(_read_json(health_path), expected_model=provider_config.model)
        if stable_hash(stored_health) != stable_hash(health):
            raise ValueError("reused provider health metadata does not match supplied proof")

    if model is None:
        model = SemanticWireBundleModel(
            provider_config,
            thinking_disabled=thinking_disabled,
            max_input_chars=max_input_chars,
        )

    pipeline_messages = _public_pipeline_messages(checked.messages)
    public_by_id = {str(row.get("message_id")): row for row in pipeline_messages}
    source_bundle_by_id = {str(row.get("bundle_id")): row for row in checked.source_bundles}
    budget = BudgetManager(
        max_bundle_calls=int(max_provider_calls),
        max_input_tokens=int(max_input_tokens),
        max_output_tokens=int(max_output_tokens),
    )
    semantic = BundleSemanticPipeline(
        model=model,
        cache=VersionedBundleCache(),
        schema_version=BUNDLE_SCHEMA_VERSION,
        model_version=provider_config.model,
        prompt_version=BUNDLE_PROMPT_VERSION,
        ruleset_version=BUNDLE_RULESET_VERSION,
        max_retries=0,
    )

    outcomes_by_candidate: Dict[str, Any] = {}
    package_audits: Dict[str, Dict[str, Any]] = {}
    for scheduler_row in checked.selected_rows:
        candidate_id = str(scheduler_row.get("candidate_id") or "")
        bundle = source_bundle_by_id[candidate_id]
        ids = [str(item) for item in (scheduler_row.get("source_message_ids") or ())]
        chat_id = str(scheduler_row.get("chat_id") or "unknown")
        inputs = [public_by_id[item] for item in ids]
        _validate_scope_metadata(inputs, expected_ids=ids, chat_id=chat_id)
        outcome: Any = None
        package_attempts = 0
        provider_attempts = 0
        retries = 0
        budget_deferred = False
        last_record_count = len(budget.records)

        for attempt in range(int(max_retries) + 1):
            if budget.calls_used >= budget.max_bundle_calls:
                budget_deferred = True
                outcome = _canonical_pending(
                    inputs,
                    bundle_id=candidate_id,
                    chat_id=chat_id,
                    code="bundle_call_budget_exhausted",
                )
                break
            semantic.encoder.model = _BudgetedModel(
                model,
                budget,
                source="provider",
                model_version=provider_config.model,
                attempt=attempt,
            )
            before_calls = budget.calls_used
            before_rejections = len(budget.rejections)
            outcome = semantic.encode_bundle(inputs, bundle_id=candidate_id, chat_id=chat_id)
            new_calls = budget.calls_used - before_calls
            package_attempts += new_calls
            provider_attempts += new_calls
            if new_calls == 0 and len(budget.rejections) > before_rejections:
                budget_deferred = True
                rejection = budget.rejections[-1]
                outcome = _canonical_pending(
                    inputs,
                    bundle_id=candidate_id,
                    chat_id=chat_id,
                    code=str(rejection.get("error_code") or "budget_exhausted"),
                )
                break
            last_record = budget.records[-1] if len(budget.records) > last_record_count else {}
            last_record_count = len(budget.records)
            if outcome.status == "complete":
                break
            if (
                attempt < int(max_retries)
                and last_record.get("status") == "failed"
                and budget.calls_used < budget.max_bundle_calls
            ):
                budget.mark_retry()
                retries += 1
                continue
            break
        if outcome is None:
            outcome = _canonical_pending(
                inputs,
                bundle_id=candidate_id,
                chat_id=chat_id,
                code="provider_attempt_not_started",
            )
            budget_deferred = True
        outcomes_by_candidate[candidate_id] = outcome
        package_audits[candidate_id] = {
            "candidate_id": candidate_id,
            "semantic_package_id": scheduler_row.get("semantic_package_id"),
            "selected": True,
            "attempt_count": package_attempts,
            "provider_attempt_count": provider_attempts,
            "retry_count": retries,
            "status": outcome.status,
            "source": str(outcome.bundle.get("metadata", {}).get("source", "unknown")),
            "budget_deferred": budget_deferred,
            "input_sha256": outcome.input_sha256,
            "cache_key": outcome.cache_key,
            "validation_ok": bool(outcome.validation.ok),
        }

    decision_rows: List[Dict[str, Any]] = []
    selection_rows: List[Dict[str, Any]] = []
    selected_ids = {str(row.get("candidate_id") or "") for row in checked.selected_rows}
    for scheduler_row in checked.scheduler_decisions:
        candidate_id = str(scheduler_row.get("candidate_id") or "")
        cues = tuple(item for item in (scheduler_row.get("activation_cues") or ()) if isinstance(item, Mapping))
        if candidate_id in selected_ids:
            outcome = outcomes_by_candidate[candidate_id]
            audit = package_audits[candidate_id]
            decision = _semantic_decision(
                scheduler_row,
                outcome=outcome,
                selected=True,
                status=outcome.status,
                source=str(outcome.bundle.get("metadata", {}).get("source") or "unknown"),
                package_attempts=audit["attempt_count"],
                provider_attempts=audit["provider_attempt_count"],
                retry_count=audit["retry_count"],
                budget_deferred=audit["budget_deferred"],
                activation_cues=cues,
            )
            selected = True
        else:
            source = "budget" if scheduler_row.get("selection_reason") == "scheduler_budget_exhausted" else "scheduler_not_selected"
            decision = _semantic_decision(
                scheduler_row,
                outcome=None,
                selected=False,
                status="pending",
                source=source,
                package_attempts=0,
                provider_attempts=0,
                retry_count=0,
                budget_deferred=source == "budget",
                activation_cues=cues,
            )
            selected = False
        decision_rows.append(decision)
        mapping = dict(scheduler_row)
        mapping.update(
            {
                "runner_version": RUNNER_SCHEMA_VERSION,
                "selected_for_encode": selected,
                "semantic_status": decision["semantic_status"],
                "semantic_source": decision["semantic_source"],
                "provider_attempt_count": decision["provider_attempt_count"],
                "retry_count": decision["retry_count"],
                "budget_deferred": decision["budget_deferred"],
                "activation_cues": [dict(item) for item in cues],
                "activation_cue_preserved": decision["activation_cues"] == [dict(item) for item in cues],
            }
        )
        if selected:
            audit = package_audits[candidate_id]
            mapping.update(
                {
                    "attempt_count": audit["attempt_count"],
                    "semantic_input_sha256": audit["input_sha256"],
                    "semantic_cache_key": audit["cache_key"],
                    "validation_ok": audit["validation_ok"],
                }
            )
        else:
            mapping.update(
                {
                    "attempt_count": 0,
                    "semantic_input_sha256": "N/A",
                    "semantic_cache_key": "N/A",
                    "validation_ok": "N/A",
                }
            )
        selection_rows.append(mapping)

    _assert_body_free(decision_rows, label="decisions")
    _assert_body_free(selection_rows, label="selection mapping")

    source_json_files = (
        ("registry", "registry.private.json"),
        ("gate", "gate.private.json"),
        ("snapshots", "snapshots.private.json"),
    )
    source_jsonl_files = (
        ("bundles", "bundles.private.jsonl"),
        ("relations", "relations.private.jsonl"),
        ("open_context_snapshots", "open_context_snapshots.private.jsonl"),
    )
    output_root.mkdir(parents=True, exist_ok=False)
    for key, filename in source_json_files:
        value = _body_free(_read_json(checked.source_root / filename))
        _assert_body_free(value, label=key)
        _write_json(output_root / OUTPUT_FILENAMES[key], value)
    for key, filename in source_jsonl_files:
        rows = tuple(_body_free(row) for row in _read_jsonl(checked.source_root / filename))
        _assert_body_free(rows, label=key)
        _write_jsonl(output_root / OUTPUT_FILENAMES[key], rows)

    requests = tuple(_body_free(row) for row in (budget.records + budget.rejections))
    _assert_body_free(requests, label="requests")
    _write_jsonl(output_root / OUTPUT_FILENAMES["requests"], requests)
    errors: List[Dict[str, Any]] = []
    for row in budget.records:
        if row.get("status") in {"failed", "pending"} and row.get("error_code"):
            errors.append(
                {
                    "code": str(row.get("error_code") or "provider_error"),
                    "error_bucket": _error_bucket(row.get("error_code")),
                    "source": "provider",
                    "severity": "high",
                    "bundle_id": row.get("bundle_id"),
                    "request_id": row.get("request_id"),
                    "attempt": row.get("attempt"),
                    "status": row.get("status"),
                    "diagnostics": _body_free(row.get("diagnostics") or {}),
                    "error_hash": stable_hash(
                        {
                            "code": row.get("error_code"),
                            "bundle_id": row.get("bundle_id"),
                            "request_id": row.get("request_id"),
                            "attempt": row.get("attempt"),
                        }
                    ),
                }
            )
    for row in budget.rejections:
        errors.append(
            {
                "code": str(row.get("error_code") or "budget_rejected"),
                "error_bucket": _error_bucket(row.get("error_code")),
                "source": "budget",
                "severity": "medium",
                "bundle_id": row.get("bundle_id"),
                "request_id": row.get("request_id"),
                "attempt": row.get("attempt"),
                "status": row.get("status"),
                "error_hash": stable_hash(
                    {
                        "code": row.get("error_code"),
                        "bundle_id": row.get("bundle_id"),
                        "request_id": row.get("request_id"),
                        "attempt": row.get("attempt"),
                    }
                ),
            }
        )
    _assert_body_free(errors, label="errors")
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    _write_jsonl(output_root / OUTPUT_FILENAMES["decisions"], decision_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection_mapping"], selection_rows)

    outcome_values = tuple(outcomes_by_candidate.values())
    status_counts = Counter(str(row.get("semantic_status") or "unknown") for row in decision_rows)
    source_counts = Counter(str(row.get("semantic_source") or "unknown") for row in decision_rows)
    provider_summary = _attempt_summary(budget.records, budget.rejections)
    complete_rows = [
        row
        for row in checked.selected_rows
        if outcomes_by_candidate[str(row.get("candidate_id") or "")].status == "complete"
    ]
    complete_message_union = (
        set().union(
            *(set(str(item) for item in (row.get("source_message_ids") or ())) for row in complete_rows)
        )
        if complete_rows
        else set()
    )
    schema_metrics = _schema_evidence_metrics(outcome_values)
    relation_rows = _read_jsonl(checked.source_root / "relations.private.jsonl")
    zero_tolerance = _relation_zero_tolerance(relation_rows)
    activation_total = len(checked.scheduler_decisions)
    activation_preserved = sum(bool(row.get("activation_cues")) for row in selection_rows)
    unselected_pending = sum(not bool(row.get("selected_for_encode")) for row in selection_rows)
    counters: Dict[str, Any] = {
        "provider_request_attempts": provider_summary["provider_request_attempts"],
        "provider_request_terminal_rows": provider_summary["provider_request_terminal_rows"],
        "successful_model_outputs": sum(item.status == "complete" for item in outcome_values),
        "failed_provider_attempts": provider_summary["failed_provider_attempts"],
        "unfinished_provider_attempts": provider_summary["unfinished_provider_attempts"],
        "pending_provider_attempts": provider_summary["pending_provider_attempts"],
        "provider_pending_rejections": provider_summary["provider_pending_rejections"],
        "candidate_decisions": len(decision_rows),
        "budget_deferred": sum(
            row.get("semantic_source") == "budget" and row.get("semantic_status") == "pending"
            for row in decision_rows
        ),
        "distinct_selected_package_count": len(
            {str(row.get("semantic_package_id") or "") for row in checked.selected_rows}
        ),
        "selected_representative_count": len(checked.selected_rows),
        "provider_calls_used": {
            "value": provider_summary["provider_request_attempts"],
            "deprecated": True,
            "replacement": "provider_request_attempts",
        },
    }
    cost = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "provider": provider_config.provider,
        "model": provider_config.model,
        "model_override_explicit": True,
        "provider_configured": bool(provider_config.api_key),
        "provider_health_reused": True,
        "provider_health_source_version": HEALTH_SOURCE_VERSION,
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "wire_prompt_version": WIRE_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "response_format_sent": False,
        "thinking_disabled": bool(thinking_disabled),
        "compact_input_chars_limit": int(max_input_chars),
        "budget": budget.snapshot(),
        "provider_summary": provider_summary,
        "cache_hits": budget.cache_hits,
        "cache_misses": budget.cache_misses,
        "complete_count": status_counts.get("complete", 0),
        "pending_count": status_counts.get("pending", 0),
        "fallback_count": status_counts.get("fallback", 0),
        "candidate_decision_count": len(decision_rows),
        "selected_package_count": len(checked.selected_rows),
        "max_provider_calls": int(max_provider_calls),
        "max_input_tokens": int(max_input_tokens),
        "max_output_tokens": int(max_output_tokens),
        "scoring": "N/A",
    }
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)

    scheduler_snapshot = {
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "manifest": _body_free(checked.scheduler_manifest),
        "coverage": _body_free(checked.scheduler_coverage),
        "provenance": _body_free(checked.scheduler_provenance),
        "source_file_hashes": dict(checked.source_file_hashes),
        "current_code_sha256": checked.scheduler_code_sha256,
        "selected_count": len(checked.selected_rows),
        "estimated_message_coverage_count": 269,
        "activation_cue_rows": activation_total,
        "activation_cue_preserved_rows": activation_preserved,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free": True,
    }
    _assert_body_free(scheduler_snapshot, label="scheduler snapshot")
    _write_json(output_root / OUTPUT_FILENAMES["scheduler"], scheduler_snapshot)

    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "scoring": "N/A",
        "message_count": len(checked.messages),
        "candidate_bundle_count": len(checked.source_bundles),
        "candidate_decision_count": len(decision_rows),
        "selected_package_count": len(checked.selected_rows),
        "distinct_selected_package_count": counters["distinct_selected_package_count"],
        "scheduler_estimated_message_coverage_count": 269,
        "scheduler_estimated_message_coverage_ratio": 269 / len(checked.messages),
        "complete_message_count": len(complete_message_union),
        "complete_message_coverage_ratio": len(complete_message_union) / len(checked.messages),
        "selected_to_complete_message_ratio": len(complete_message_union) / 269 if 269 else "N/A",
        "status_counts": dict(sorted(status_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "counters": counters,
        "provider_attempts": provider_summary,
        "schema_evidence": schema_metrics,
        "activation_cues": {
            "scheduler_rows": activation_total,
            "preserved_rows": activation_preserved,
            "preservation_rate": activation_preserved / activation_total if activation_total else "N/A",
            "unselected_pending_rows": unselected_pending,
            "unselected_pending_with_cues": sum(
                bool(row.get("activation_cues")) and not bool(row.get("selected_for_encode"))
                for row in selection_rows
            ),
        },
        "zero_tolerance": zero_tolerance,
        "errors": {
            "count": len(errors),
            "by_bucket": dict(sorted(Counter(str(row.get("error_bucket") or "unknown") for row in errors).items())),
            "by_code": dict(sorted(Counter(str(row.get("code") or "unknown") for row in errors).items())),
        },
        "input_sha256": checked.input_sha256,
        "scheduler_code_sha256": checked.scheduler_code_sha256,
        "source_file_hashes": dict(checked.source_file_hashes),
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "wire_prompt_version": WIRE_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "model_authoritative_id_write": False,
        "evidence_handle_validation": "strict_local",
        "cache_includes_symbol_table": True,
        "relations": {
            "time_signal_weight": 0.0,
            "same_segment_signal_weight": 0.0,
            "pairwise_calls": 0,
        },
        "accuracy": "N/A",
    }
    _assert_body_free(aggregate, label="aggregate")
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)

    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "input_directory_name": checked.input_root.name,
        "input_filename": INPUT_FILENAME,
        "input_sha256": checked.input_sha256,
        "source_artifact_version": SOURCE_ARTIFACT_VERSION,
        "source_artifact_directory_name": checked.source_root.name,
        "source_artifact_file_hashes": dict(checked.source_file_hashes),
        "scheduler_artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "scheduler_artifact_directory_name": checked.scheduler_root.name,
        "scheduler_code_sha256": checked.scheduler_code_sha256,
        "scheduler_input_sha256": checked.scheduler_manifest.get("development_input_sha256"),
        "scheduler_selected_count": checked.scheduler_manifest.get("selected_count"),
        "scheduler_candidate_count": checked.scheduler_manifest.get("candidate_count"),
        "scheduler_decision_count": checked.scheduler_manifest.get("decision_count"),
        "scheduler_estimated_message_coverage_count": checked.scheduler_manifest.get("estimated_message_coverage_count"),
        "scheduler_estimated_message_coverage_ratio": checked.scheduler_manifest.get("estimated_message_coverage_ratio"),
        "candidate_bundle_count": len(checked.source_bundles),
        "candidate_decision_count": len(decision_rows),
        "selected_package_count": len(checked.selected_rows),
        "provider_request_attempts": provider_summary["provider_request_attempts"],
        "provider_calls_are_actual_requests": True,
        "max_provider_calls": int(max_provider_calls),
        "max_input_tokens": int(max_input_tokens),
        "max_output_tokens": int(max_output_tokens),
        "max_retries_per_package": int(max_retries),
        "provider_health": health,
        "provider_health_reused": True,
        "provider_health_source_version": HEALTH_SOURCE_VERSION,
        "provider_model_override": {
            "model": provider_config.model,
            "source": "explicit_v2_9_run_override",
            "explicit": True,
            "global_settings_mutated": False,
        },
        "wire_schema_version": WIRE_SCHEMA_VERSION,
        "wire_prompt_version": WIRE_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "wire_symbol_table_enforced": True,
        "model_authoritative_id_write": False,
        "evidence_handle_validation": "strict_local",
        "cache_includes_symbol_table": True,
        "response_format_sent": False,
        "thinking_disabled": bool(thinking_disabled),
        "compact_input_chars_limit": int(max_input_chars),
        "development_input_read": True,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free_outputs": True,
        "accuracy": "N/A",
        "scoring": "N/A",
        "output_directory_name": output_root.name,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    _assert_body_free(manifest, label="manifest")
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    _write_json(output_root / OUTPUT_FILENAMES["provider_health"], health)

    artifact_paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return V29RunResult(
        output_directory=str(output_root),
        manifest_path=artifact_paths["manifest"],
        artifact_paths=artifact_paths,
        manifest=manifest,
        aggregate=aggregate,
    )


__all__ = [
    "RUNNER_SCHEMA_VERSION",
    "ARTIFACT_VERSION",
    "OUTPUT_FILENAMES",
    "SchedulerInputs",
    "V29RunResult",
    "_validate_scheduler_inputs",
    "_health_from_v26",
    "_health_from_capability_artifact",
    "run_development_shadow_pilot_v29",
]
