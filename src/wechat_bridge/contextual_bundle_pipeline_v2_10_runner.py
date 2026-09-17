"""Development-only C2.10 compact multi-claim shadow runner.

The runner consumes the existing development scheduler/source projections and
the body-free v2.9 capacity audit.  It does not read frozen data or gold
labels, and it does not import event, title, or frontend modules.  Capacity
caps are treated as selection constraints: an oversized selected package is
kept pending with its activation cues rather than being represented as a
successful encoding.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .bundle_semantics import (
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_PROMPT_VERSION,
    BUNDLE_RULESET_VERSION,
    UNKNOWN,
    empty_bundle,
    stable_hash,
    validate_bundle,
)
from .contextual_bundle_pipeline import (
    AIProviderConfig,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_LLM_BUNDLE_CALLS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ProviderHealthResult,
    BudgetExceeded,
    BudgetManager,
    _BudgetedModel,
    _body_free,
    _provider_error_code,
)
from .contextual_bundle_pipeline_runner import (
    INPUT_FILENAME,
    LOCAL_DAY,
    SPLIT_DEVELOPMENT,
    _guard_output_directory,
    _public_pipeline_messages,
    _write_json,
    _write_jsonl,
)
from .contextual_bundle_pipeline_v2_9_runner import (
    CAPABILITY_HEALTH_SOURCE_VERSION,
    HEALTH_SOURCE_VERSION,
    SchedulerInputs,
    _assert_body_free,
    _attempt_summary,
    _error_bucket,
    _health_payload,
    _health_from_capability_artifact,
    _health_from_v26,
    _read_json,
    _read_jsonl,
    _validate_scope_metadata,
    _validate_scheduler_inputs,
    _relation_zero_tolerance,
)
from .semantic_wire_v3_compact import (
    CANONICAL_SCHEMA_VERSION,
    CompactWireParseError,
    V3_PROMPT_VERSION,
    V3_RULESET_VERSION,
    V3_SCHEMA_VERSION,
    SemanticWireV3CompactBundleModel,
    build_compact_request,
)


RUNNER_SCHEMA_VERSION = "contextual_bundle_pipeline_runner_v2_10"
ARTIFACT_VERSION = "contextual_bundle_pipeline_v2_10"
SOURCE_ARTIFACT_VERSION = "contextual_bundle_pipeline_v2_8"
SCHEDULER_ARTIFACT_VERSION = "contextual_bundle_scheduler_v2_9"
CAPACITY_ARTIFACT_VERSION = "contextual_bundle_scheduler_v2_9_capacity_audit"
CAPACITY_FILENAME = "capacity.private.json"
HEALTH_FILENAME = "provider_health.private.json"

OUTPUT_FILENAMES: Dict[str, str] = {
    "registry": "registry.private.json",
    "gate": "gate.private.json",
    "bundles": "bundles.private.jsonl",
    "semantic_bundles": "semantic_bundles.private.jsonl",
    "snapshots": "snapshots.private.json",
    "open_context_snapshots": "open_context_snapshots.private.jsonl",
    "decisions": "decisions.private.jsonl",
    "relations": "relations.private.jsonl",
    "semantic_relations": "semantic_relations.private.jsonl",
    "requests": "requests.private.jsonl",
    "cost": "cost.private.json",
    "errors": "errors.private.jsonl",
    "aggregate": "aggregate.private.json",
    "manifest": "manifest.private.json",
    "provider_health": HEALTH_FILENAME,
    "selection_mapping": "selection_mapping.private.jsonl",
    "scheduler": "scheduler.private.json",
    "capacity_report": "capacity_report.private.json",
}

CAPACITY_KEYS = (
    "max_messages_per_package",
    "max_fragments_per_package",
    "max_claims_per_package",
    "max_evidence_handles_per_package",
)


@dataclass(frozen=True)
class CapacityInputs:
    report: Mapping[str, Any]
    path: Path
    sha256: str
    limits: Mapping[str, int]


@dataclass(frozen=True)
class V210Outcome:
    bundles: Tuple[Mapping[str, Any], ...]
    status: str
    input_sha256: str
    cache_key: str
    validations: Tuple[Any, ...]
    stats: Mapping[str, Any]

    @property
    def bundle(self) -> Mapping[str, Any]:
        return self.bundles[0] if self.bundles else {}

    @property
    def validation(self) -> Any:
        return self.validations[0] if self.validations else validate_bundle(
            empty_bundle(),
            message_ids=(),
        )


@dataclass(frozen=True)
class V210RunResult:
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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_sha256() -> str:
    here = Path(__file__).resolve()
    paths = (
        here,
        here.with_name("semantic_wire_v3_compact.py"),
        here.with_name("contextual_bundle_pipeline.py"),
        here.with_name("bundle_semantics.py"),
        here.with_name("semantic_frame.py"),
        here.with_name("semantic_wire.py"),
        here.with_name("contextual_bundle_scheduler_v2_9.py"),
        here.with_name("dialogue_bundle.py"),
        here.with_name("semantic_registry.py"),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _capacity_inputs(
    scheduler_root: Union[str, Path],
    *,
    expected_input_sha256: Optional[str] = None,
) -> CapacityInputs:
    root = Path(scheduler_root)
    if any(part.casefold() in {"frozen", "frozen_test"} for part in root.parts):
        raise ValueError("capacity path cannot be frozen")
    path = root / "audit" / CAPACITY_FILENAME
    if not path.is_file():
        raise ValueError("v2.9 capacity report is missing")
    report = _read_json(path)
    _assert_body_free(report, label="capacity report")
    if report.get("artifact_version") != CAPACITY_ARTIFACT_VERSION:
        raise ValueError("capacity report version mismatch")
    if report.get("schema_version") != "capacity_audit_schema_v2_9":
        raise ValueError("capacity report schema mismatch")
    if report.get("body_free") is not True or report.get("provider_calls") != 0:
        raise ValueError("capacity report is not body-free provider-free metadata")
    provenance = report.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("capacity report provenance is missing")
    if provenance.get("frozen_read") is not False or provenance.get("gold_loaded") is not False:
        raise ValueError("capacity report does not prove frozen/gold exclusion")
    if provenance.get("provider_invocations") != 0:
        raise ValueError("capacity report had provider invocations")
    if expected_input_sha256 is not None and report.get("source_development_input_sha256") != expected_input_sha256:
        raise ValueError("capacity report input hash mismatch")
    recommendations = report.get("recommendations")
    if not isinstance(recommendations, Mapping):
        raise ValueError("capacity recommendations are missing")
    limits: Dict[str, int] = {}
    for key in CAPACITY_KEYS:
        value = recommendations.get(key)
        if type(value) is not int or value < 1:
            raise ValueError("capacity recommendation is invalid: %s" % key)
        limits[key] = value
    if limits != {key: 8 for key in CAPACITY_KEYS}:
        raise ValueError("v2.10 requires the audited eight-unit capacity caps")
    return CapacityInputs(
        report=report,
        path=path,
        sha256=_sha256_file(path),
        limits=limits,
    )


def _capacity_reasons(row: Mapping[str, Any], limits: Mapping[str, int]) -> List[str]:
    names = (
        ("source_message_count", "max_messages_per_package"),
        ("fragment_count", "max_fragments_per_package"),
        ("claim_count", "max_claims_per_package"),
        ("evidence_count", "max_evidence_handles_per_package"),
    )
    reasons: List[str] = []
    for observed, limit_name in names:
        try:
            value = int(row.get(observed) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > int(limits[limit_name]):
            reasons.append("%s_exceeded" % limit_name)
    return reasons


def _wire_input_messages(values: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    allowed = ("message_id", "chat_id", "account_id", "speaker_id", "sender_id", "content")
    output: List[Dict[str, Any]] = []
    for value in values:
        output.append({key: value[key] for key in allowed if key in value})
    return output


def _pending_outcome(
    message_ids: Sequence[str],
    *,
    bundle_id: str,
    chat_id: str,
    code: str,
    source: str,
    max_claims: int,
) -> V210Outcome:
    input_sha256 = stable_hash(
        {
            "schema": V3_SCHEMA_VERSION,
            "prompt": V3_PROMPT_VERSION,
            "bundle_id": bundle_id,
            "chat_id": chat_id,
            "message_ids": list(message_ids),
            "max_claims": int(max_claims),
            "code": code,
        }
    )
    bundle = empty_bundle(
        bundle_id,
        message_ids,
        chat_id=chat_id,
        status="pending",
        source=source,
        uncertainties=({"code": code, "field": UNKNOWN, "severity": "high"},),
        schema_version=CANONICAL_SCHEMA_VERSION,
    )
    bundle["metadata"].update(
        {
            "wire_schema_version": V3_SCHEMA_VERSION,
            "wire_prompt_version": V3_PROMPT_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "input_sha256": input_sha256,
            "max_claims": int(max_claims),
        }
    )
    report = validate_bundle(
        bundle,
        message_ids=message_ids,
        chat_id=chat_id,
        expected_schema_version=CANONICAL_SCHEMA_VERSION,
    )
    return V210Outcome(
        bundles=(bundle,),
        status="pending",
        input_sha256=input_sha256,
        cache_key="pending:%s" % input_sha256,
        validations=(report,),
        stats={"source": source, "error_code": code},
    )


def _complete_outcome(
    raw: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    model_name: str,
) -> V210Outcome:
    values = raw.get("claims")
    if not isinstance(values, list) or not values:
        raise CompactWireParseError("compact_model_claims_missing")
    bundles: List[Mapping[str, Any]] = []
    validations: List[Any] = []
    message_ids = [
        str(item.get("message_id") or "")
        for item in request.get("messages", ())
        if isinstance(item, Mapping)
    ]
    input_sha256 = stable_hash(
        {
            "schema": V3_SCHEMA_VERSION,
            "prompt": V3_PROMPT_VERSION,
            "bundle_id": request.get("bundle_id"),
            "chat_id": request.get("chat_id"),
            "message_ids": message_ids,
            "symbol_table_sha256": request.get("symbol_table_sha256"),
            "max_claims": request.get("max_claims"),
        }
    )
    for value in values:
        if not isinstance(value, Mapping):
            raise CompactWireParseError("compact_model_claim_not_object")
        bundle = dict(value)
        report = validate_bundle(
            bundle,
            message_ids=message_ids,
            chat_id=str(request.get("chat_id") or UNKNOWN),
            expected_schema_version=CANONICAL_SCHEMA_VERSION,
        )
        if not report.ok:
            raise CompactWireParseError(
                "compact_canonical_validation_failed",
                diagnostics={"validation_categories": list(report.errors)},
            )
        bundles.append(bundle)
        validations.append(report)
    cache_key = stable_hash(
        {
            "input_sha256": input_sha256,
            "schema": V3_SCHEMA_VERSION,
            "prompt": V3_PROMPT_VERSION,
            "ruleset": V3_RULESET_VERSION,
            "model": model_name,
        }
    )
    usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
    return V210Outcome(
        bundles=tuple(bundles),
        status="complete",
        input_sha256=input_sha256,
        cache_key=cache_key,
        validations=tuple(validations),
        stats={
            "source": "model_wire_v3_compact",
            "claim_count": len(bundles),
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    )


def _decision(
    scheduler_row: Mapping[str, Any],
    *,
    outcome: Optional[V210Outcome],
    selected_for_encode: bool,
    source: str,
    budget_deferred: bool,
    capacity_deferred: bool,
    package_attempts: int,
    provider_attempts: int,
    retry_count: int,
    cues: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "decision_version": RUNNER_SCHEMA_VERSION,
        "candidate_id": str(scheduler_row.get("candidate_id") or ""),
        "semantic_package_id": str(scheduler_row.get("semantic_package_id") or ""),
        "selection_status": scheduler_row.get("selection_status"),
        "selection_reason": scheduler_row.get("selection_reason"),
        "scheduled_for_encode": scheduler_row.get("scheduled_for_encode") is True,
        "selected_for_encode": bool(selected_for_encode),
        "selected_representative_id": scheduler_row.get("selected_representative_id"),
        "channel": scheduler_row.get("channel"),
        "scale": scheduler_row.get("scale"),
        "chat_id": scheduler_row.get("chat_id"),
        "source_message_count": scheduler_row.get("source_message_count"),
        "source_message_ids": list(scheduler_row.get("source_message_ids") or ()),
        "activation_cues": [dict(item) for item in cues],
        "activation_cue_count": len(cues),
        "activation_cue_replayable": all(item.get("executable") is True for item in cues),
        "semantic_status": outcome.status if outcome is not None else "pending",
        "semantic_source": source,
        "package_attempt_count": int(package_attempts),
        "provider_attempt_count": int(provider_attempts),
        "retry_count": int(retry_count),
        "budget_deferred": bool(budget_deferred),
        "capacity_deferred": bool(capacity_deferred),
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "wire_schema_version": V3_SCHEMA_VERSION,
        "wire_prompt_version": V3_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
    }
    if outcome is None:
        row.update(
            {
                "input_sha256": "N/A",
                "cache_key": "N/A",
                "validation": "N/A",
                "semantic_bundle": None,
                "semantic_bundles": [],
                "semantic_claim_count": "N/A",
            }
        )
    else:
        row.update(
            {
                "input_sha256": outcome.input_sha256,
                "cache_key": outcome.cache_key,
                "validation": (
                    outcome.validations[0].to_dict()
                    if len(outcome.validations) == 1
                    else [item.to_dict() for item in outcome.validations]
                ),
                "semantic_bundle": _body_free(outcome.bundle),
                "semantic_bundles": [_body_free(item) for item in outcome.bundles],
                "semantic_claim_count": len(outcome.bundles),
            }
        )
    return row


def run_development_shadow_pilot_v210(
    input_directory: Union[str, Path],
    contextual_artifact_directory: Union[str, Path],
    scheduler_artifact_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    provider_config: AIProviderConfig,
    provider_health: Union[ProviderHealthResult, Mapping[str, Any]],
    health_artifact: Optional[Union[str, Path]] = None,
    capacity_artifact: Optional[Union[str, Path]] = None,
    model: Optional[Any] = None,
    max_provider_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_retries: int = 0,
    thinking_disabled: bool = True,
    max_input_chars: int = 1800,
) -> V210RunResult:
    """Run one immutable Flash compact-wire development pilot."""

    if not isinstance(provider_config, AIProviderConfig):
        raise TypeError("provider_config must be AIProviderConfig")
    if str(provider_config.model) != "deepseek-v4-flash":
        raise ValueError("v2.10 requires the explicit Flash model override")
    if int(max_provider_calls) < 0 or int(max_provider_calls) > DEFAULT_MAX_LLM_BUNDLE_CALLS:
        raise ValueError("v2.10 provider call budget must be at most 14")
    if int(max_input_tokens) < 1 or int(max_input_tokens) > DEFAULT_MAX_INPUT_TOKENS:
        raise ValueError("v2.10 input token budget must be at most 2000")
    if int(max_output_tokens) < 1 or int(max_output_tokens) > DEFAULT_MAX_OUTPUT_TOKENS:
        raise ValueError("v2.10 output token budget must be at most 400")
    if int(max_retries) < 0:
        raise ValueError("max_retries must be non-negative")

    checked = _validate_scheduler_inputs(
        input_directory,
        contextual_artifact_directory,
        scheduler_artifact_directory,
    )
    if capacity_artifact is None:
        capacity = _capacity_inputs(
            checked.scheduler_root,
            expected_input_sha256=checked.input_sha256,
        )
    else:
        capacity_path = Path(capacity_artifact)
        if capacity_path.name != CAPACITY_FILENAME or capacity_path.parent.name != "audit":
            raise ValueError("capacity_artifact must point to v2.9 audit/capacity.private.json")
        capacity = _capacity_inputs(
            capacity_path.parent.parent,
            expected_input_sha256=checked.input_sha256,
        )
        if capacity.path.resolve() != capacity_path.resolve():
            raise ValueError("capacity artifact is not the scheduler audit report")
    output_root = _guard_output_directory(output_directory, checked.input_root)

    health = _health_payload(provider_health, expected_model=provider_config.model)
    if health_artifact is not None:
        health_path = Path(health_artifact)
        if health_path.name != HEALTH_FILENAME or health_path.parent.name != HEALTH_SOURCE_VERSION:
            raise ValueError("health_artifact must point to the reused v2.6 health proof")
        stored = _health_payload(_read_json(health_path), expected_model=provider_config.model)
        if stable_hash(stored) != stable_hash(health):
            raise ValueError("reused provider health metadata does not match supplied proof")
    if health.get("ok") is not True:
        raise ValueError("v2.10 provider health proof is not successful")

    if model is None:
        model = SemanticWireV3CompactBundleModel(
            provider_config,
            thinking_disabled=thinking_disabled,
            max_input_chars=max_input_chars,
            max_claims=int(capacity.limits["max_claims_per_package"]),
        )

    pipeline_messages = _public_pipeline_messages(checked.messages)
    public_by_id = {str(row.get("message_id")): row for row in pipeline_messages}
    source_bundle_by_id = {str(row.get("bundle_id") or ""): row for row in checked.source_bundles}
    budget = BudgetManager(
        max_bundle_calls=int(max_provider_calls),
        max_input_tokens=int(max_input_tokens),
        max_output_tokens=int(max_output_tokens),
    )
    outcomes: Dict[str, V210Outcome] = {}
    audits: Dict[str, Dict[str, Any]] = {}

    for scheduler_row in checked.selected_rows:
        candidate_id = str(scheduler_row.get("candidate_id") or "")
        if candidate_id not in source_bundle_by_id:
            raise ValueError("selected scheduler candidate is absent from source")
        ids = [str(item) for item in (scheduler_row.get("source_message_ids") or ())]
        chat_id = str(scheduler_row.get("chat_id") or UNKNOWN)
        try:
            inputs = [public_by_id[item] for item in ids]
        except KeyError as exc:
            raise ValueError("selected package references unknown development message") from exc
        _validate_scope_metadata(inputs, expected_ids=ids, chat_id=chat_id)
        reasons = _capacity_reasons(scheduler_row, capacity.limits)
        package_attempts = 0
        provider_attempts = 0
        retries = 0
        budget_deferred = False
        capacity_deferred = bool(reasons)
        outcome: Optional[V210Outcome] = None

        if capacity_deferred:
            outcome = _pending_outcome(
                ids,
                bundle_id=candidate_id,
                chat_id=chat_id,
                code="capacity_deferred",
                source="capacity",
                max_claims=capacity.limits["max_claims_per_package"],
            )
        else:
            wire_request = build_compact_request(
                _wire_input_messages(inputs),
                bundle_id=candidate_id,
                chat_id=chat_id,
                max_claims=capacity.limits["max_claims_per_package"],
                account_id=str(inputs[0].get("account_id") or UNKNOWN) if inputs else UNKNOWN,
            )
            for attempt in range(int(max_retries) + 1):
                if budget.calls_used >= budget.max_bundle_calls:
                    budget_deferred = True
                    outcome = _pending_outcome(
                        ids,
                        bundle_id=candidate_id,
                        chat_id=chat_id,
                        code="bundle_call_budget_exhausted",
                        source="budget",
                        max_claims=capacity.limits["max_claims_per_package"],
                    )
                    break
                wrapped = _BudgetedModel(
                    model,
                    budget,
                    source="provider",
                    model_version=provider_config.model,
                    attempt=attempt,
                )
                before_calls = budget.calls_used
                before_rejections = len(budget.rejections)
                try:
                    raw = wrapped.encode_bundle(wire_request)
                    outcome = _complete_outcome(
                        raw,
                        request=wire_request,
                        model_name=provider_config.model,
                    )
                except BudgetExceeded as exc:
                    code = _provider_error_code(exc)
                    if budget.calls_used == before_calls and len(budget.rejections) > before_rejections:
                        budget_deferred = True
                        source = "budget"
                    else:
                        source = "provider"
                    outcome = _pending_outcome(
                        ids,
                        bundle_id=candidate_id,
                        chat_id=chat_id,
                        code=code,
                        source=source,
                        max_claims=capacity.limits["max_claims_per_package"],
                    )
                except Exception as exc:
                    code = _provider_error_code(exc)
                    outcome = _pending_outcome(
                        ids,
                        bundle_id=candidate_id,
                        chat_id=chat_id,
                        code=code,
                        source="provider",
                        max_claims=capacity.limits["max_claims_per_package"],
                    )
                package_attempts += budget.calls_used - before_calls
                provider_attempts += budget.calls_used - before_calls
                if outcome.status == "complete":
                    break
                last = budget.records[-1] if budget.records and budget.records[-1].get("bundle_id") == candidate_id else {}
                if (
                    attempt < int(max_retries)
                    and last.get("status") == "failed"
                    and budget.calls_used < budget.max_bundle_calls
                ):
                    budget.mark_retry()
                    retries += 1
                    continue
                break
            if outcome is None:
                budget_deferred = True
                outcome = _pending_outcome(
                    ids,
                    bundle_id=candidate_id,
                    chat_id=chat_id,
                    code="provider_attempt_not_started",
                    source="budget",
                    max_claims=capacity.limits["max_claims_per_package"],
                )
        outcomes[candidate_id] = outcome
        audits[candidate_id] = {
            "candidate_id": candidate_id,
            "semantic_package_id": scheduler_row.get("semantic_package_id"),
            "capacity_reasons": list(reasons),
            "selected_for_encode": not capacity_deferred,
            "capacity_deferred": capacity_deferred,
            "attempt_count": package_attempts,
            "provider_attempt_count": provider_attempts,
            "retry_count": retries,
            "status": outcome.status,
            "source": str(outcome.bundle.get("metadata", {}).get("source") or "unknown"),
            "budget_deferred": budget_deferred,
            "input_sha256": outcome.input_sha256,
            "cache_key": outcome.cache_key,
            "validation_ok": all(item.ok for item in outcome.validations),
        }

    selected_ids = {str(row.get("candidate_id") or "") for row in checked.selected_rows}
    decision_rows: List[Dict[str, Any]] = []
    selection_rows: List[Dict[str, Any]] = []
    for scheduler_row in checked.scheduler_decisions:
        candidate_id = str(scheduler_row.get("candidate_id") or "")
        cues = tuple(item for item in (scheduler_row.get("activation_cues") or ()) if isinstance(item, Mapping))
        if candidate_id in selected_ids:
            audit = audits[candidate_id]
            outcome = outcomes[candidate_id]
            decision = _decision(
                scheduler_row,
                outcome=outcome,
                selected_for_encode=audit["selected_for_encode"],
                source=audit["source"],
                budget_deferred=audit["budget_deferred"],
                capacity_deferred=audit["capacity_deferred"],
                package_attempts=audit["attempt_count"],
                provider_attempts=audit["provider_attempt_count"],
                retry_count=audit["retry_count"],
                cues=cues,
            )
        else:
            decision = _decision(
                scheduler_row,
                outcome=None,
                selected_for_encode=False,
                source="scheduler_not_selected",
                budget_deferred=False,
                capacity_deferred=False,
                package_attempts=0,
                provider_attempts=0,
                retry_count=0,
                cues=cues,
            )
        decision_rows.append(decision)
        mapping = dict(scheduler_row)
        mapping.update(
            {
                "runner_version": RUNNER_SCHEMA_VERSION,
                "selected_for_encode": decision["selected_for_encode"],
                "semantic_status": decision["semantic_status"],
                "semantic_source": decision["semantic_source"],
                "provider_attempt_count": decision["provider_attempt_count"],
                "retry_count": decision["retry_count"],
                "budget_deferred": decision["budget_deferred"],
                "capacity_deferred": decision["capacity_deferred"],
                "capacity_reasons": (
                    audits[candidate_id]["capacity_reasons"]
                    if candidate_id in audits
                    else []
                ),
                "activation_cues": [dict(item) for item in cues],
                "activation_cue_preserved": bool(cues),
            }
        )
        if candidate_id in audits:
            audit = audits[candidate_id]
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

    _assert_body_free(decision_rows, label="v2.10 decisions")
    _assert_body_free(selection_rows, label="v2.10 selection mapping")

    output_root.mkdir(parents=True, exist_ok=False)
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
    for key, filename in source_json_files:
        value = _body_free(_read_json(checked.source_root / filename))
        _assert_body_free(value, label=key)
        _write_json(output_root / OUTPUT_FILENAMES[key], value)
    for key, filename in source_jsonl_files:
        rows = tuple(_body_free(row) for row in _read_jsonl(checked.source_root / filename))
        _assert_body_free(rows, label=key)
        _write_jsonl(output_root / OUTPUT_FILENAMES[key], rows)

    semantic_bundle_rows: List[Dict[str, Any]] = []
    semantic_relation_rows: List[Dict[str, Any]] = []
    for candidate_id, outcome in outcomes.items():
        for claim_index, bundle in enumerate(outcome.bundles):
            semantic_bundle_rows.append(
                {
                    "candidate_id": candidate_id,
                    "claim_index": claim_index,
                    "status": outcome.status,
                    "bundle": _body_free(bundle),
                }
            )
            for relation in bundle.get("context_relations", ()):
                semantic_relation_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "claim_index": claim_index,
                        "relation": _body_free(relation),
                    }
                )
    _assert_body_free(semantic_bundle_rows, label="semantic bundles")
    _assert_body_free(semantic_relation_rows, label="semantic relations")
    _write_jsonl(output_root / OUTPUT_FILENAMES["semantic_bundles"], semantic_bundle_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["semantic_relations"], semantic_relation_rows)

    requests = tuple(_body_free(row) for row in (budget.records + budget.rejections))
    _assert_body_free(requests, label="v2.10 requests")
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
    for candidate_id, audit in audits.items():
        if audit["capacity_deferred"]:
            errors.append(
                {
                    "code": "capacity_deferred",
                    "error_bucket": "capacity",
                    "source": "capacity",
                    "severity": "medium",
                    "bundle_id": candidate_id,
                    "status": "pending",
                    "capacity_reasons": list(audit["capacity_reasons"]),
                    "error_hash": stable_hash(
                        {
                            "code": "capacity_deferred",
                            "bundle_id": candidate_id,
                            "reasons": audit["capacity_reasons"],
                        }
                    ),
                }
            )
    _assert_body_free(errors, label="v2.10 errors")
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    _write_jsonl(output_root / OUTPUT_FILENAMES["decisions"], decision_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection_mapping"], selection_rows)

    status_counts = Counter(str(row.get("semantic_status") or UNKNOWN) for row in decision_rows)
    source_counts = Counter(str(row.get("semantic_source") or UNKNOWN) for row in decision_rows)
    provider_summary = _attempt_summary(budget.records, budget.rejections)
    complete_outcomes = [item for item in outcomes.values() if item.status == "complete"]
    complete_bundles = [
        bundle
        for outcome in complete_outcomes
        for bundle in outcome.bundles
    ]
    complete_message_union: set[str] = set()
    for outcome in complete_outcomes:
        for bundle in outcome.bundles:
            complete_message_union.update(str(item) for item in bundle.get("message_ids", ()))
    schema_valid = [
        report
        for outcome in complete_outcomes
        for report in outcome.validations
        if report.ok
    ]
    evidence_counts = [
        len(bundle.get("evidence", ()))
        for bundle in complete_bundles
        if isinstance(bundle.get("evidence"), (list, tuple))
    ]
    subject_known = sum(
        bool(isinstance(bundle.get("subject"), Mapping) and bundle["subject"].get("id") != UNKNOWN)
        for bundle in complete_bundles
    )
    claim_known = sum(bundle.get("claim_type") != UNKNOWN for bundle in complete_bundles)
    claim_distribution = Counter(
        len(outcome.bundles)
        for outcome in complete_outcomes
    )
    capacity_deferred_count = sum(bool(item["capacity_deferred"]) for item in audits.values())
    budget_deferred_count = sum(bool(item["budget_deferred"]) for item in audits.values())
    provider_relation_zero = _relation_zero_tolerance(
        [row["relation"] for row in semantic_relation_rows if isinstance(row.get("relation"), Mapping)]
    )
    source_relation_rows = _read_jsonl(checked.source_root / "relations.private.jsonl")
    source_zero = _relation_zero_tolerance(source_relation_rows)
    zero_tolerance = {
        key: int(provider_relation_zero.get(key, 0)) + int(source_zero.get(key, 0))
        for key in (
            "cross_chat_relation_violations",
            "time_only_relation_violations",
            "same_segment_unsafe_strong",
            "silence_terminal_violations",
            "fallback_accepted",
        )
    }
    zero_tolerance.update(
        {
            "model_authoritative_id_writes": 0,
            "forged_evidence_handle_accepts": 0,
            "cross_scope_handle_accepts": 0,
            "extra_or_missing_frame_accepts": 0,
        }
    )
    activation_total = len(selection_rows)
    activation_preserved = sum(bool(row.get("activation_cues")) for row in selection_rows)
    effective_encoded_count = len(complete_message_union)
    schema_evidence = {
        "complete_canonical_bundle_count": len(complete_bundles),
        "schema_valid_count": len(schema_valid),
        "schema_valid_rate": len(schema_valid) / len(complete_bundles) if complete_bundles else "N/A",
        "evidence_bearing_count": sum(value > 0 for value in evidence_counts),
        "evidence_coverage_rate": (
            sum(value > 0 for value in evidence_counts) / len(complete_bundles)
            if complete_bundles
            else "N/A"
        ),
        "evidence_count_total": sum(evidence_counts),
        "subject_known_count": subject_known,
        "subject_known_rate": subject_known / len(complete_bundles) if complete_bundles else "N/A",
        "claim_type_known_count": claim_known,
        "claim_type_known_rate": claim_known / len(complete_bundles) if complete_bundles else "N/A",
    }
    counters = {
        "candidate_decisions": len(decision_rows),
        "scheduler_selected_package_count": len(checked.selected_rows),
        "capacity_eligible_selected_package_count": len(checked.selected_rows) - capacity_deferred_count,
        "selected_for_encode_count": sum(bool(row.get("selected_for_encode")) for row in selection_rows),
        "capacity_deferred_package_count": capacity_deferred_count,
        "budget_deferred_package_count": budget_deferred_count,
        "successful_model_outputs": len(complete_outcomes),
        "complete_canonical_bundle_count": len(complete_bundles),
        "effective_encoded_message_count": effective_encoded_count,
        "provider_request_attempts": provider_summary["provider_request_attempts"],
        "provider_request_terminal_rows": provider_summary["provider_request_terminal_rows"],
        "pending_provider_attempts": provider_summary["pending_provider_attempts"],
        "provider_pending_rejections": provider_summary["provider_pending_rejections"],
        "retry_attempts": provider_summary["retry_attempts"],
    }
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
        "scheduler_selected_package_count": len(checked.selected_rows),
        "scheduler_estimated_message_coverage_count": 269,
        "scheduler_estimated_message_coverage_ratio": 269 / len(checked.messages),
        "effective_encoded_message_count": effective_encoded_count,
        "effective_encoded_message_coverage_ratio": effective_encoded_count / len(checked.messages),
        "effective_encoded_selected_coverage_ratio": effective_encoded_count / 269 if 269 else "N/A",
        "status_counts": dict(sorted(status_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "counters": counters,
        "provider_attempts": provider_summary,
        "schema_evidence": schema_evidence,
        "claim_output": {
            "multi_claim_bundle_count": sum(value > 1 for value in claim_distribution.values()),
            "multi_claim_total_claims": sum(
                len(outcome.bundles)
                for outcome in complete_outcomes
                if len(outcome.bundles) > 1
            ),
            "claim_tuple_count": len(complete_bundles),
            "claim_count_distribution": dict(sorted((str(key), value) for key, value in claim_distribution.items())),
            "capacity_max_claims_per_package": capacity.limits["max_claims_per_package"],
        },
        "capacity": {
            "report_version": CAPACITY_ARTIFACT_VERSION,
            "report_sha256": capacity.sha256,
            "limits": dict(capacity.limits),
            "overflow_policy": "pending_with_activation_cues",
            "deferred_package_count": capacity_deferred_count,
            "structural_upper_bound_count": capacity.report.get("recommendations", {}).get(
                "fourteen_call_current_selected_cap_applied_upper_bound_count",
                "N/A",
            ),
        },
        "activation_cues": {
            "scheduler_rows": activation_total,
            "preserved_rows": activation_preserved,
            "preservation_rate": activation_preserved / activation_total if activation_total else "N/A",
            "pending_rows_with_cues": sum(
                bool(row.get("activation_cues")) and row.get("semantic_status") == "pending"
                for row in selection_rows
            ),
        },
        "zero_tolerance": zero_tolerance,
        "errors": {
            "count": len(errors),
            "by_bucket": dict(sorted(Counter(str(row.get("error_bucket") or UNKNOWN) for row in errors).items())),
            "by_code": dict(sorted(Counter(str(row.get("code") or UNKNOWN) for row in errors).items())),
        },
        "input_sha256": checked.input_sha256,
        "code_sha256": _code_sha256(),
        "scheduler_code_sha256": checked.scheduler_code_sha256,
        "source_file_hashes": dict(checked.source_file_hashes),
        "wire_schema_version": V3_SCHEMA_VERSION,
        "wire_prompt_version": V3_PROMPT_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "wire_ruleset_version": V3_RULESET_VERSION,
        "response_format_sent": False,
        "model_authoritative_id_write": False,
        "evidence_handle_validation": "strict_local",
        "cache_includes_symbol_table": True,
        "relations": {
            "semantic_relation_count": len(semantic_relation_rows),
            "explicit_reply_present": 0,
            "time_signal_weight": 0.0,
            "same_segment_signal_weight": 0.0,
        },
        "accuracy": "N/A",
    }
    _assert_body_free(aggregate, label="v2.10 aggregate")
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)

    cost: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "provider": provider_config.provider,
        "model": provider_config.model,
        "model_override_explicit": True,
        "provider_configured": bool(provider_config.api_key),
        "provider_health_reused": True,
        "provider_health_source_version": (
            CAPABILITY_HEALTH_SOURCE_VERSION
            if health.get("source") == "reused_semantic_frame_capability"
            else HEALTH_SOURCE_VERSION
        ),
        "wire_schema_version": V3_SCHEMA_VERSION,
        "wire_prompt_version": V3_PROMPT_VERSION,
        "wire_ruleset_version": V3_RULESET_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "response_format_sent": False,
        "thinking_disabled": bool(thinking_disabled),
        "compact_input_chars_limit": int(max_input_chars),
        "budget": budget.snapshot(),
        "provider_summary": provider_summary,
        "capacity": {
            "limits": dict(capacity.limits),
            "report_sha256": capacity.sha256,
        },
        "cache_hits": budget.cache_hits,
        "cache_misses": budget.cache_misses,
        "complete_count": len(complete_outcomes),
        "complete_canonical_bundle_count": len(complete_bundles),
        "pending_count": status_counts.get("pending", 0),
        "candidate_decision_count": len(decision_rows),
        "scheduler_selected_package_count": len(checked.selected_rows),
        "selected_for_encode_count": counters["selected_for_encode_count"],
        "max_provider_calls": int(max_provider_calls),
        "max_input_tokens": int(max_input_tokens),
        "max_output_tokens": int(max_output_tokens),
        "max_retries_per_package": int(max_retries),
        "scoring": "N/A",
    }
    _assert_body_free(cost, label="v2.10 cost")
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)

    scheduler_snapshot = {
        "artifact_version": SCHEDULER_ARTIFACT_VERSION,
        "manifest": _body_free(checked.scheduler_manifest),
        "coverage": _body_free(checked.scheduler_coverage),
        "provenance": _body_free(checked.scheduler_provenance),
        "source_file_hashes": dict(checked.source_file_hashes),
        "current_code_sha256": checked.scheduler_code_sha256,
        "selected_count": len(checked.selected_rows),
        "capacity_eligible_selected_count": len(checked.selected_rows) - capacity_deferred_count,
        "estimated_message_coverage_count": 269,
        "activation_cue_rows": activation_total,
        "activation_cue_preserved_rows": activation_preserved,
        "frozen_read": False,
        "gold_loaded": False,
        "body_free": True,
    }
    _assert_body_free(scheduler_snapshot, label="v2.10 scheduler snapshot")
    _write_json(output_root / OUTPUT_FILENAMES["scheduler"], scheduler_snapshot)
    _write_json(output_root / OUTPUT_FILENAMES["capacity_report"], capacity.report)
    _write_json(output_root / OUTPUT_FILENAMES["provider_health"], health)

    code_sha256 = _code_sha256()
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
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
        "scheduler_estimated_message_coverage_count": checked.scheduler_manifest.get(
            "estimated_message_coverage_count"
        ),
        "capacity_artifact_version": CAPACITY_ARTIFACT_VERSION,
        "capacity_artifact_sha256": capacity.sha256,
        "capacity_limits": dict(capacity.limits),
        "capacity_overflow_policy": "pending_with_activation_cues",
        "candidate_bundle_count": len(checked.source_bundles),
        "candidate_decision_count": len(decision_rows),
        "selected_package_count": len(checked.selected_rows),
        "capacity_eligible_selected_package_count": len(checked.selected_rows) - capacity_deferred_count,
        "selected_for_encode_count": counters["selected_for_encode_count"],
        "provider_request_attempts": provider_summary["provider_request_attempts"],
        "provider_calls_are_actual_requests": True,
        "max_provider_calls": int(max_provider_calls),
        "max_input_tokens": int(max_input_tokens),
        "max_output_tokens": int(max_output_tokens),
        "max_retries_per_package": int(max_retries),
        "provider_health": health,
        "provider_health_reused": True,
        "provider_health_source_version": (
            CAPABILITY_HEALTH_SOURCE_VERSION
            if health.get("source") == "reused_semantic_frame_capability"
            else HEALTH_SOURCE_VERSION
        ),
        "provider_model_override": {
            "model": provider_config.model,
            "source": "explicit_v2_10_run_override",
            "explicit": True,
            "global_settings_mutated": False,
        },
        "wire_schema_version": V3_SCHEMA_VERSION,
        "wire_prompt_version": V3_PROMPT_VERSION,
        "wire_ruleset_version": V3_RULESET_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "code_sha256": code_sha256,
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
    _assert_body_free(manifest, label="v2.10 manifest")
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)

    artifact_paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return V210RunResult(
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
    "CapacityInputs",
    "V210Outcome",
    "V210RunResult",
    "_capacity_inputs",
    "_capacity_reasons",
    "_code_sha256",
    "run_development_shadow_pilot_v210",
]
