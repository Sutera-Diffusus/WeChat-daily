"""Body-free capability probing for the existing OpenAI-compatible provider.

This side-car intentionally stops at provider capability discovery.  It does
not read a chat archive, gold labels, or any frozen split.  The model-list
response is consumed in memory and reduced to model ids plus conservative
capability hints; the raw response is never retained or written.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contextual_bundle_pipeline import (
    AIProviderConfig,
    OpenAIBundleModel,
    SemanticFrameBundleModel,
    ProviderHealthResult,
    _provider_error_code,
    run_provider_health_check,
    stable_hash,
)


CAPABILITY_SCHEMA_VERSION = "contextual_bundle_provider_capabilities_v1"
CAPABILITY_RUNNER_VERSION = "provider_capability_matrix_v1"
BUNDLE_HEALTH_CAPABILITY_SCHEMA_VERSION = "contextual_bundle_provider_capabilities_v2_6"
BUNDLE_HEALTH_CAPABILITY_RUNNER_VERSION = "provider_bundle_health_matrix_v2_6"
MODELS_METHOD = "GET"
MODELS_PATH = "/models"
DEFAULT_MAX_CANDIDATES = 3
DEFAULT_MAX_HEALTH_CALLS = 3
HEALTH_MAX_INPUT_TOKENS = 500
HEALTH_MAX_OUTPUT_TOKENS = 100
SEMANTIC_FRAME_CAPABILITY_PROTOCOL = "semantic_frame_v1"

# These are deliberately conservative string hints.  A model is considered
# usable only after the strict JSON health probe succeeds; id heuristics never
# claim provider support by themselves.
_VISION_MARKERS = ("vision", "multimodal", "image", "audio", "video")
_TEXT_MARKERS = (
    "chat",
    "claude",
    "deepseek",
    "gemini",
    "glm",
    "gpt",
    "instruct",
    "llama",
    "mistral",
    "moonshot",
    "qwen",
    "reasoner",
    "text",
    "yi-",
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[Any]) -> None:
    path.write_text(
        "".join(
            json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _code_sha256() -> str:
    here = Path(__file__).resolve()
    pipeline = here.with_name("contextual_bundle_pipeline.py")
    digest = hashlib.sha256()
    for path in (here, pipeline):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _model_id(item: Any) -> str:
    if isinstance(item, Mapping):
        value = item.get("id")
    else:
        value = getattr(item, "id", None)
    text = str(value or "").strip()
    # Model ids are metadata, but keep their artifact representation bounded
    # and single-line so an unexpected provider object cannot smuggle a body.
    if not text or len(text) > 240 or "\n" in text or "\r" in text:
        return ""
    return text


def _model_items(response: Any) -> Sequence[Any]:
    if isinstance(response, Mapping):
        values = response.get("data")
    else:
        values = getattr(response, "data", None)
    if isinstance(values, (list, tuple)):
        return values
    return ()


def extract_model_ids(response: Any) -> Tuple[str, ...]:
    """Consume a provider model-list response into stable ids only."""

    result: List[str] = []
    seen = set()
    for item in _model_items(response):
        model_id = _model_id(item)
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append(model_id)
    return tuple(sorted(result, key=lambda value: value.casefold()))


def classify_model_id(model_id: str, configured_model: str = "") -> Dict[str, Any]:
    """Return conservative, body-free capability hints for one model id."""

    text = str(model_id).strip()
    folded = text.casefold()
    configured = bool(configured_model) and text == str(configured_model).strip()
    vision_exp = bool(re.search(r"vision[^a-z0-9]*exp|exp[^a-z0-9]*vision", folded))
    vision_like = any(marker in folded for marker in _VISION_MARKERS)
    text_like = any(marker in folded for marker in _TEXT_MARKERS)
    reasons: List[str] = []
    if vision_exp:
        reasons.append("vision_exp_excluded")
    elif vision_like:
        reasons.append("vision_or_multimodal_excluded")
    if not text_like:
        reasons.append("text_json_capability_unrecognized")
    eligible = bool(text_like and not vision_like)
    return {
        "model_id": text,
        "configured_model": configured,
        "eligible": eligible,
        "capability_hints": {
            "text_likely": text_like,
            "json_object_unverified": True,
            "vision_like": vision_like,
            "vision_exp": vision_exp,
            "source": "model_id_heuristic",
        },
        "selection_reasons": reasons or ["text_json_candidate"],
    }


def select_model_candidates(
    model_ids: Iterable[str],
    *,
    configured_model: str = "",
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> Tuple[Dict[str, Any], ...]:
    """Select at most three likely text/JSON models, configured model first."""

    unique_ids: List[str] = []
    seen = set()
    for model_id in model_ids:
        text = str(model_id).strip()
        if text and text not in seen:
            seen.add(text)
            unique_ids.append(text)
    rows = [classify_model_id(model_id, configured_model) for model_id in unique_ids]
    eligible = [row for row in rows if row["eligible"]]
    eligible.sort(key=lambda row: (not row["configured_model"], row["model_id"].casefold()))
    return tuple(eligible[: max(0, min(DEFAULT_MAX_CANDIDATES, int(max_candidates)))])


def _public_health(health: ProviderHealthResult) -> Dict[str, Any]:
    return health.to_dict()


def _invoke_health(
    config: AIProviderConfig,
    adapter: Any,
    *,
    method_name: str,
    max_input_tokens: int,
    max_output_tokens: int,
    clock: Optional[Callable[[], float]],
) -> ProviderHealthResult:
    if method_name == "health_check":
        return run_provider_health_check(
            config,
            model=adapter,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            clock=clock,
        )
    method = getattr(adapter, method_name, None)
    request_hash = stable_hash({"protocol": method_name, "health_check": "synthetic"})
    if not callable(method):
        return ProviderHealthResult(
            False,
            "blocked",
            str(getattr(adapter, "source", config.provider)),
            str(getattr(adapter, "model_version", config.model)),
            request_hash,
            0,
            0,
            max_input_tokens,
            max_output_tokens,
            "N/A",
            "health_interface_missing",
            config.public_dict(),
        )
    try:
        result = method(
            max_input_tokens=int(max_input_tokens),
            max_output_tokens=int(max_output_tokens),
            clock=clock,
        )
        if isinstance(result, ProviderHealthResult):
            return result
        return ProviderHealthResult(
            False,
            "blocked",
            str(getattr(adapter, "source", config.provider)),
            str(getattr(adapter, "model_version", config.model)),
            request_hash,
            0,
            0,
            max_input_tokens,
            max_output_tokens,
            "N/A",
            "health_result_invalid",
            config.public_dict(),
        )
    except Exception as exc:
        return ProviderHealthResult(
            False,
            "blocked",
            str(getattr(adapter, "source", config.provider)),
            str(getattr(adapter, "model_version", config.model)),
            request_hash,
            0,
            0,
            max_input_tokens,
            max_output_tokens,
            "N/A",
            _provider_error_code(exc),
            config.public_dict(),
        )


@dataclass(frozen=True)
class ProviderCapabilityProbeResult:
    """Body-free result of one model-list request and bounded health probes."""

    status: str
    config: Mapping[str, Any]
    model_list: Mapping[str, Any]
    candidates: Tuple[Mapping[str, Any], ...]
    health_calls: Tuple[Mapping[str, Any], ...]
    selected_model: Optional[str]
    selected_health: Optional[ProviderHealthResult]
    errors: Tuple[Mapping[str, Any], ...]
    continuity_risks: Tuple[str, ...]
    code_sha256: str
    protocol: str = "json_object"

    @property
    def ok(self) -> bool:
        return bool(self.selected_model and self.selected_health and self.selected_health.ok)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "runner_version": CAPABILITY_RUNNER_VERSION,
            "protocol": self.protocol,
            "status": self.status,
            "config": dict(self.config),
            "model_list": dict(self.model_list),
            "candidates": [dict(item) for item in self.candidates],
            "health_calls": [dict(item) for item in self.health_calls],
            "selected_model": self.selected_model,
            "selected_health": _public_health(self.selected_health) if self.selected_health else None,
            "errors": [dict(item) for item in self.errors],
            "continuity_risks": list(self.continuity_risks),
            "development_read": False,
            "frozen_read": False,
            "gold_loaded": False,
            "accuracy": "N/A",
            "scoring": "N/A",
            "code_sha256": self.code_sha256,
        }


def probe_provider_capabilities(
    config: AIProviderConfig,
    *,
    client_factory: Optional[Callable[[AIProviderConfig], Any]] = None,
    health_model_factory: Optional[Callable[[AIProviderConfig], Any]] = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_health_calls: int = DEFAULT_MAX_HEALTH_CALLS,
    protocol: str = "json_object",
    health_method_name: str = "health_check",
    clock: Optional[Callable[[], float]] = None,
) -> ProviderCapabilityProbeResult:
    """GET ``/models`` then health-check at most three text/JSON candidates."""

    clock_fn = clock or time.perf_counter
    request_hash = stable_hash({"method": MODELS_METHOD, "path": MODELS_PATH})
    started = clock_fn()
    errors: List[Dict[str, Any]] = []
    model_ids: Tuple[str, ...] = ()
    try:
        adapter = OpenAIBundleModel(config)
        client = client_factory(config) if client_factory is not None else adapter._get_client()
        models = getattr(client, "models", None)
        list_method = getattr(models, "list", None)
        if not callable(list_method):
            raise RuntimeError("provider_models_interface_missing")
        response = list_method()
        # Do not keep ``response`` beyond this extraction boundary.
        model_ids = extract_model_ids(response)
        model_list = {
            "method": MODELS_METHOD,
            "path": MODELS_PATH,
            "request_sha256": request_hash,
            "status": "available",
            "model_count": len(model_ids),
            "latency_ms": round((clock_fn() - started) * 1000.0, 3),
            "raw_response_saved": False,
        }
    except Exception as exc:
        code = _provider_error_code(exc)
        errors.append({"stage": "models", "code": code, "error_hash": stable_hash({"stage": "models", "code": code})})
        model_list = {
            "method": MODELS_METHOD,
            "path": MODELS_PATH,
            "request_sha256": request_hash,
            "status": "blocked",
            "model_count": 0,
            "latency_ms": round((clock_fn() - started) * 1000.0, 3),
            "error_code": code,
            "raw_response_saved": False,
        }

    candidates = select_model_candidates(
        model_ids,
        configured_model=config.model,
        max_candidates=max_candidates,
    )
    candidate_rows: List[Dict[str, Any]] = [dict(item) for item in candidates]
    health_calls: List[Mapping[str, Any]] = []
    selected_model: Optional[str] = None
    selected_health: Optional[ProviderHealthResult] = None
    limit = max(0, min(DEFAULT_MAX_HEALTH_CALLS, int(max_health_calls)))
    for row in candidate_rows[:limit]:
        candidate_config = replace(config, model=str(row["model_id"]))
        health_model = health_model_factory(candidate_config) if health_model_factory is not None else None
        health = _invoke_health(
            candidate_config,
            health_model,
            method_name=health_method_name,
            max_input_tokens=HEALTH_MAX_INPUT_TOKENS,
            max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
            clock=clock,
        )
        health_payload = _public_health(health)
        health_calls.append(health_payload)
        row.update(
            {
                "health_ok": bool(health.ok),
                "health_status": health.status,
                "health_error_code": health.error_code,
                "health_input_tokens": health.input_tokens,
                "health_output_tokens": health.output_tokens,
                "health_latency_ms": health.latency_ms,
                "json_object_confirmed": bool(health.ok and protocol == "json_object"),
                "semantic_frame_confirmed": bool(health.ok and protocol == "semantic_frame_v1"),
            }
        )
        if health.ok and selected_model is None:
            selected_model = str(row["model_id"])
            selected_health = health

    if selected_model is not None:
        status = "available"
    elif model_list["status"] != "available":
        status = "blocked"
    elif candidate_rows:
        status = "blocked"
        code = "no_candidate_health_success"
        errors.append({"stage": "health", "code": code, "error_hash": stable_hash({"stage": "health", "code": code})})
    else:
        status = "blocked"
        code = "no_text_json_candidate"
        errors.append({"stage": "selection", "code": code, "error_hash": stable_hash({"stage": "selection", "code": code})})

    risks = (
        "model_id_capability_is_heuristic_until_health_success",
        "selected_override_may_differ_from_user_global_model",
        "provider_model_list_and_capabilities_can_change_over_time",
        "health_success_confirms_json_object_probe_only_not_bundle_accuracy",
    )
    return ProviderCapabilityProbeResult(
        status=status,
        config=config.public_dict(),
        model_list=model_list,
        candidates=tuple(candidate_rows),
        health_calls=tuple(health_calls),
        selected_model=selected_model,
        selected_health=selected_health,
        errors=tuple(errors),
        continuity_risks=risks,
        code_sha256=_code_sha256(),
        protocol=str(protocol),
    )


def probe_semantic_frame_capabilities(
    config: AIProviderConfig,
    *,
    client_factory: Optional[Callable[[AIProviderConfig], Any]] = None,
    health_model_factory: Optional[Callable[[AIProviderConfig], Any]] = None,
    max_candidates: int = 2,
    max_health_calls: int = 2,
    clock: Optional[Callable[[], float]] = None,
) -> ProviderCapabilityProbeResult:
    """Probe at most two text candidates with the no-response-format frame adapter."""

    return probe_provider_capabilities(
        config,
        client_factory=client_factory,
        health_model_factory=health_model_factory or (lambda candidate_config: SemanticFrameBundleModel(candidate_config)),
        max_candidates=max_candidates,
        max_health_calls=max_health_calls,
        protocol=SEMANTIC_FRAME_CAPABILITY_PROTOCOL,
        clock=clock,
    )


def probe_semantic_frame_bundle_capabilities(
    config: AIProviderConfig,
    *,
    client_factory: Optional[Callable[[AIProviderConfig], Any]] = None,
    health_model_factory: Optional[Callable[[AIProviderConfig], Any]] = None,
    max_candidates: int = 2,
    max_health_calls: int = 2,
    thinking_disabled: bool = False,
    clock: Optional[Callable[[], float]] = None,
) -> ProviderCapabilityProbeResult:
    """Probe synthetic full-bundle frames while recording metadata only."""

    return probe_provider_capabilities(
        config,
        client_factory=client_factory,
        health_model_factory=health_model_factory
        or (
            lambda candidate_config: SemanticFrameBundleModel(
                candidate_config,
                thinking_disabled=thinking_disabled,
            )
        ),
        max_candidates=max_candidates,
        max_health_calls=max_health_calls,
        protocol="semantic_frame_v1_bundle_health",
        health_method_name="bundle_health_check",
        clock=clock,
    )


def write_capability_artifact(
    result: ProviderCapabilityProbeResult,
    output_directory: Path,
    *,
    settings_source: str = "workbench_settings",
) -> Mapping[str, str]:
    """Persist only the body-free matrix and immutable manifest."""

    output_root = Path(output_directory)
    if output_root.exists():
        raise FileExistsError("refusing to overwrite existing capability artifact")
    output_root.mkdir(parents=True, exist_ok=False)
    matrix_path = output_root / "provider_capabilities.private.json"
    errors_path = output_root / "errors.private.jsonl"
    manifest_path = output_root / "manifest.private.json"
    _write_json(matrix_path, result.to_dict())
    _write_jsonl(errors_path, result.errors)
    manifest = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "runner_version": CAPABILITY_RUNNER_VERSION,
        "protocol": result.protocol,
        "artifact_version": output_root.name,
        "settings_source": settings_source,
        "model_list": dict(result.model_list),
        "candidate_count": len(result.candidates),
        "health_call_count": len(result.health_calls),
        "selected_model": result.selected_model,
        "selected_override": bool(result.selected_model),
        "raw_response_saved": False,
        "development_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "accuracy": "N/A",
        "scoring": "N/A",
        "code_sha256": result.code_sha256,
    }
    _write_json(manifest_path, manifest)
    return {
        "matrix": str(matrix_path),
        "errors": str(errors_path),
        "manifest": str(manifest_path),
    }


def write_bundle_health_artifact(
    config: AIProviderConfig,
    health_calls: Iterable[ProviderHealthResult],
    output_directory: Path,
    *,
    selected_model: Optional[str] = None,
    errors: Iterable[Mapping[str, Any]] = (),
    settings_source: str = "workbench_settings",
    thinking_disabled: bool = True,
    code_sha256: Optional[str] = None,
) -> Mapping[str, str]:
    """Persist a bounded direct bundle-health attempt matrix, body-free.

    C2.6 intentionally probes one explicit model rather than re-running model
    discovery.  The two-call cap and final incompatibility classification are
    therefore represented directly in this immutable artifact.
    """

    if not isinstance(config, AIProviderConfig):
        raise TypeError("config must be an AIProviderConfig")
    health_values = tuple(item for item in health_calls if isinstance(item, ProviderHealthResult))
    selected_health = None
    if selected_model is not None:
        for item in health_values:
            if item.ok and item.model == selected_model:
                selected_health = item
                break
    safe_errors_list: List[Dict[str, Any]] = []
    for item in errors:
        if not isinstance(item, Mapping):
            continue
        # Error records are a public audit surface.  Keep only stable
        # metadata so a caller cannot accidentally persist provider bodies.
        safe_item: Dict[str, Any] = {}
        for key in ("stage", "code", "error_hash", "severity", "source"):
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe_item[key] = value
        if safe_item:
            safe_errors_list.append(safe_item)
    safe_errors = tuple(safe_errors_list)
    status = "available" if selected_health is not None else "blocked"
    protocol = "semantic_frame_v1_bundle_health_compact"
    code = code_sha256 or _code_sha256()
    matrix = {
        "schema_version": BUNDLE_HEALTH_CAPABILITY_SCHEMA_VERSION,
        "runner_version": BUNDLE_HEALTH_CAPABILITY_RUNNER_VERSION,
        "protocol": protocol,
        "status": status,
        "config": config.public_dict(),
        "model_list": {
            "method": "N/A",
            "path": "N/A",
            "status": "not_requested",
            "model_count": 1,
            "raw_response_saved": False,
        },
        "candidates": [
            {
                "model_id": config.model,
                "health_call_count": len(health_values),
                "health_ok": bool(selected_health is not None),
                "health_error_codes": [item.error_code for item in health_values if item.error_code],
            }
        ],
        "health_calls": [_public_health(item) for item in health_values],
        "selected_model": selected_model if selected_health is not None else None,
        "selected_health": _public_health(selected_health) if selected_health is not None else None,
        "errors": list(safe_errors),
        "development_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "accuracy": "N/A",
        "scoring": "N/A",
        "thinking_disabled": bool(thinking_disabled),
        "max_health_calls": 2,
        "code_sha256": code,
    }
    output_root = Path(output_directory)
    if output_root.exists():
        raise FileExistsError("refusing to overwrite existing capability artifact")
    output_root.mkdir(parents=True, exist_ok=False)
    matrix_path = output_root / "provider_capabilities.private.json"
    errors_path = output_root / "errors.private.jsonl"
    manifest_path = output_root / "manifest.private.json"
    _write_json(matrix_path, matrix)
    _write_jsonl(errors_path, safe_errors)
    manifest = {
        "schema_version": BUNDLE_HEALTH_CAPABILITY_SCHEMA_VERSION,
        "runner_version": BUNDLE_HEALTH_CAPABILITY_RUNNER_VERSION,
        "protocol": protocol,
        "artifact_version": output_root.name,
        "settings_source": settings_source,
        "model": config.model,
        "selected_model": matrix["selected_model"],
        "candidate_count": 1,
        "health_call_count": len(health_values),
        "max_health_calls": 2,
        "thinking_disabled": bool(thinking_disabled),
        "raw_response_saved": False,
        "development_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "accuracy": "N/A",
        "scoring": "N/A",
        "code_sha256": code,
    }
    _write_json(manifest_path, manifest)
    return {
        "matrix": str(matrix_path),
        "errors": str(errors_path),
        "manifest": str(manifest_path),
    }


__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_RUNNER_VERSION",
    "BUNDLE_HEALTH_CAPABILITY_SCHEMA_VERSION",
    "BUNDLE_HEALTH_CAPABILITY_RUNNER_VERSION",
    "MODELS_METHOD",
    "MODELS_PATH",
    "HEALTH_MAX_INPUT_TOKENS",
    "HEALTH_MAX_OUTPUT_TOKENS",
    "SEMANTIC_FRAME_CAPABILITY_PROTOCOL",
    "ProviderCapabilityProbeResult",
    "classify_model_id",
    "extract_model_ids",
    "select_model_candidates",
    "probe_provider_capabilities",
    "probe_semantic_frame_capabilities",
    "probe_semantic_frame_bundle_capabilities",
    "write_capability_artifact",
    "write_bundle_health_artifact",
]
