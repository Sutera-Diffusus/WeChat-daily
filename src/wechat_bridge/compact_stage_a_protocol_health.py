"""Synthetic-only health probe for the compact Stage-A wire.

K17 deliberately stops at a *synthetic* protocol health check.  It does not
construct an HTTP client and it never reads a development or frozen split.
Callers must inject a small in-memory model (normally
``FakeCompactStageAHealthModel``) if they want the one allowed model-shaped
exchange.  Leaving ``model`` unset writes a blocked, body-free diagnostic and
performs no provider call.

The durable :mod:`persistent_call_budget` ledger is used before an injected
model is entered.  A reservation is never released, including when the model
raises or returns an invalid/truncated result.  Consequently a second process
with the same ``authorization_id`` cannot silently obtain another call by
choosing a new output directory.

This module is intentionally narrower than the later Stage-B/C semantic
pipeline.  The compact wire only assigns messages to topics; no persons,
objects, claims, states, summaries, or evidence bodies are accepted here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .compact_stage_a_protocol import (
    MAX_INPUT_TOKEN_PROXY,
    MAX_OUTPUT_TOKENS,
    SYSTEM_PROMPT,
    TOPIC_LIMIT_RULE,
    build_compact_stage_a_request,
    canonical_json,
    measure_wire_size,
    parse_compact_stage_a_output,
    stable_hash,
    topic_limit_for_request,
)
from .persistent_call_budget import (
    AuthorizationBindingMismatch,
    CallAuthorizationLedger,
    CallBudgetExceeded,
    ReservationRejected,
)


ARTIFACT_VERSION = "compact_stage_a_protocol_health_v2"
REPORT_SCHEMA_VERSION = "compact_stage_a_protocol_health_report_v2"
RUNNER_SCHEMA_VERSION = "compact_stage_a_protocol_health_runner_v2"
PROTOCOL_VERSION = "stage_a_topic_assignment_compact_v2"
PROMPT_VERSION = "stage_a_topic_assignment_compact_prompt_v2"
MODEL_ID = "deepseek-v4-flash"
PROVIDER_ID = "openai-compatible"
SOURCE_ID = "synthetic"
RESPONSE_FORMAT_MODE = "omitted"
THINKING_DISABLED = True
MAX_PROVIDER_CALLS = 1
MAX_RETRIES = 0
HEALTH_MAX_OUTPUT_TOKENS = 400
AUTHORIZATION_ID = "K17_COMPACT_STAGE_A_HEALTH_V1"
ARTIFACT_NAMESPACE = "compact-stage-a-health-v1"

DEFAULT_AUTHORITY_ROOT = Path(".runtime") / "compact-stage-a-authorizations"
DEFAULT_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/compact_stage_a_protocol_health_v2"
)
OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "diagnostic": "diagnostic.private.json",
    "ledger": "ledger.private.jsonl",
    "errors": "errors.private.jsonl",
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FINISH = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
_SAFE_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")
_SAFE_AUTHORIZATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FORBIDDEN_PATH_PARTS = frozenset({"frozen", "frozen_test", "frozen-test"})
_BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "message",
        "messages",
        "prompt",
        "raw",
        "raw_response",
        "response",
        "text",
        "user_input",
    }
)


class CompactStageAHealthError(RuntimeError):
    """Body-free error used by the synthetic health boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class CompactStageAHealthModel(Protocol):
    """The only model surface K17 accepts.

    An implementation is injected by tests or by a separately authorized
    future experiment.  This module itself supplies no network implementation.
    ``response_format`` is intentionally absent: the reviewed protocol mode
    is omitted, while ``extra_body`` contains only the per-call thinking flag.
    """

    model_id: str
    source: str
    configured: bool

    def complete(
        self,
        system_prompt: str,
        request: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Mapping[str, Any],
    ) -> Any:
        ...


@dataclass(frozen=True)
class CompactStageAHealthResponse:
    """In-memory provider-shaped result; never persisted verbatim."""

    content: str
    model: str = MODEL_ID
    source: str = SOURCE_ID
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    reasoning_length: int = 0


@dataclass
class FakeCompactStageAHealthModel:
    """Deterministic synthetic model used by K17 tests.

    The default result is a valid compact assignment generated from the
    supplied request.  ``content`` can be replaced with malformed JSON to
    exercise fail-closed parsing.  ``calls`` contains only in-memory call
    metadata and is never copied into an artifact.
    """

    content: Optional[str] = None
    model_id: str = MODEL_ID
    source: str = SOURCE_ID
    configured: bool = True
    input_tokens: int = 64
    output_tokens: int = 20
    latency_ms: float = 1.0
    finish_reason: str = "stop"
    reasoning_length: int = 0
    calls: list[Dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def valid_content(request: Mapping[str, Any]) -> str:
        primary = [str(row["i"]) for row in request["h"] if row["k"] == "m" and row["r"] == "p"]
        context = [str(row["i"]) for row in request["h"] if row["k"] == "m" and row["r"] == "c"]
        if not primary:
            primary = [str(row["i"]) for row in request["h"] if row["k"] == "m"]
        topic: Dict[str, Any] = {
            "i": "t1",
            "p": primary,
            "c": context,
            "u": "certain",
        }
        return canonical_json({"t": [topic]})

    def complete(
        self,
        system_prompt: str,
        request: Mapping[str, Any],
        *,
        max_output_tokens: int,
        extra_body: Mapping[str, Any],
    ) -> CompactStageAHealthResponse:
        # Keep this record deliberately small and non-persistent.  Tests use
        # it to assert response_format was omitted and thinking was per-call.
        self.calls.append(
            {
                "system_prompt_sha256": stable_hash(system_prompt),
                "request_sha256": stable_hash(request),
                "max_output_tokens": int(max_output_tokens),
                "extra_body_field_names": sorted(str(key) for key in extra_body),
            }
        )
        content = self.content if self.content is not None else self.valid_content(request)
        return CompactStageAHealthResponse(
            content=content,
            model=self.model_id,
            source=self.source,
            input_tokens=int(self.input_tokens),
            output_tokens=int(self.output_tokens),
            latency_ms=float(self.latency_ms),
            finish_reason=self.finish_reason,
            reasoning_length=int(self.reasoning_length),
        )


# Friendly aliases used by independent tests and future callers.
FakeStageAHealthModel = FakeCompactStageAHealthModel
SyntheticCompactStageAHealthModel = FakeCompactStageAHealthModel


@dataclass(frozen=True)
class CompactStageAHealthRunResult:
    output_directory: str
    status: str
    success: bool
    strict_complete: bool
    provider_calls: int
    retry_count: int
    error_code: Optional[str]
    artifact_paths: Mapping[str, str]
    aggregate: Mapping[str, Any]

    @property
    def health_ok(self) -> bool:
        return self.strict_complete

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_directory": self.output_directory,
            "status": self.status,
            "success": self.success,
            "strict_complete": self.strict_complete,
            "provider_calls": self.provider_calls,
            "retry_count": self.retry_count,
            "error_code": self.error_code,
            "artifact_paths": dict(self.artifact_paths),
        }


HealthRunResult = CompactStageAHealthRunResult


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return ""


def _safe_output_path(path: Union[str, Path]) -> Path:
    result = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in result.parts):
        raise ValueError("health_refuses_frozen_output")
    if str(result) in {"", "."}:
        raise ValueError("health_output_required")
    return result


def _safe_authority_root(path: Union[str, Path]) -> Path:
    result = Path(path).expanduser().resolve()
    if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in result.parts):
        raise ValueError("health_refuses_frozen_authority")
    return result


def _safe_sha(value: Any, *, allow_empty: bool = False) -> str:
    text = "" if value is None else str(value).strip().lower()
    if allow_empty and not text:
        return ""
    if not _HEX64.fullmatch(text):
        raise ValueError("invalid_sha256")
    return text


def _settings_fingerprint(
    settings_path: Optional[Union[str, Path]],
    settings: Optional[Mapping[str, Any]],
) -> str:
    if settings_path is not None:
        path = Path(settings_path).expanduser().resolve()
        if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in path.parts):
            raise ValueError("health_refuses_frozen_settings")
        if any(part.casefold() == "private" for part in path.parts):
            raise ValueError("health_refuses_private_settings")
        if path.is_file():
            return _file_sha256(path)
    if settings is not None:
        return stable_hash(settings)
    return stable_hash(
        {
            "model": MODEL_ID,
            "protocol": PROTOCOL_VERSION,
            "response_format": RESPONSE_FORMAT_MODE,
            "thinking_disabled": THINKING_DISABLED,
        }
    )


def _synthetic_request() -> Dict[str, Any]:
    scope = {"account_id": "account-k17-synthetic", "chat_id": "chat-k17-synthetic"}
    messages = [
        {
            "message_handle": f"{scope['account_id']}/{scope['chat_id']}|message|M001",
            "text": "synthetic health primary",
            "role": "primary",
        },
        {
            "message_handle": f"{scope['account_id']}/{scope['chat_id']}|message|M002",
            "text": "synthetic health context",
            "role": "context",
        },
    ]
    candidates = [
        {
            "candidate_handle": f"{scope['account_id']}/{scope['chat_id']}|candidate|C001",
            "relation": "continuity",
        }
    ]
    return build_compact_stage_a_request(scope, messages, candidates)


def build_synthetic_health_request() -> Dict[str, Any]:
    """Return a fresh, fully synthetic compact Stage-A request."""

    return _synthetic_request()


def _request_scope(request: Mapping[str, Any]) -> Dict[str, str]:
    """Return the compact request scope in local-only form."""

    raw = request.get("s")
    if not isinstance(raw, Mapping):
        raise ValueError("health_request_scope_missing")
    account = raw.get("a")
    chat = raw.get("c")
    if type(account) is not str or type(chat) is not str or not account or not chat:
        raise ValueError("health_request_scope_invalid")
    return {"account_id": account, "chat_id": chat}


def _coerce_response(raw: Any, model: Any) -> CompactStageAHealthResponse:
    if isinstance(raw, CompactStageAHealthResponse):
        return raw
    if isinstance(raw, Mapping):
        content = raw.get("content")
        if type(content) is not str:
            raise CompactStageAHealthError("provider_response_shape")
        try:
            input_tokens = int(raw.get("input_tokens", 0))
            output_tokens = int(raw.get("output_tokens", 0))
            latency = float(raw.get("latency_ms", 0.0))
            reasoning = int(raw.get("reasoning_length", raw.get("reasoning_content_length", 0)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise CompactStageAHealthError("provider_response_metadata") from exc
        return CompactStageAHealthResponse(
            content=content,
            model=str(raw.get("model", getattr(model, "model_id", MODEL_ID))),
            source=str(raw.get("source", getattr(model, "source", SOURCE_ID))),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
            finish_reason=str(raw.get("finish_reason", "stop")),
            reasoning_length=reasoning,
        )
    raise CompactStageAHealthError("provider_response_shape")


def _safe_finish(value: Any) -> str:
    text = str(value or "unknown")
    return text if _SAFE_FINISH.fullmatch(text) else "unknown"


def _safe_metadata(value: Any, default: str = "unknown") -> str:
    text = str(value or default)
    return text if _SAFE_METADATA.fullmatch(text) else default


def _safe_nonnegative_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, result)


def _safe_latency(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, result)


def _parse_and_validate(content: str, request: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], str, str]:
    try:
        result = parse_compact_stage_a_output(content, request)
    except Exception as exc:
        code = str(getattr(exc, "code", "invalid_json"))
        if code in {"invalid_json", "json_not_text", "duplicate_json_key", "nonstandard_json_number"}:
            return None, code, "not_run"
        return None, "ok", code
    return result, "ok", "ok"


def _diagnostic_candidate(
    content: Optional[str],
    parsed: Optional[Mapping[str, Any]],
    parse_code: str,
    validation_code: str,
) -> Dict[str, Any]:
    # The candidate is strictly diagnostic.  It is never used as the health
    # result, even when it is a JSON object with a superficially valid shape.
    return {
        "present": bool(content),
        "json_object_candidate": bool(content and content.lstrip().startswith("{")),
        "candidate_sha256": _sha256_bytes(content.encode("utf-8")) if content is not None else "",
        "schema_valid": bool(parsed is not None and parse_code == "ok" and validation_code == "ok"),
        "accepted_for_complete": False,
        "diagnostic_only": True,
    }


def _assert_body_free(value: Any, *, label: str) -> None:
    encoded = canonical_json(value)
    if "K17_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED" in encoded:
        raise AssertionError("synthetic body marker escaped %s" % label)

    bad: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key).casefold()
                is_ref = key_text.endswith(("_ref", "_refs", "_handle", "_handles"))
                if key_text in _BODY_KEYS and child not in (None, "", [], {}, ()) and not is_ref:
                    bad.append(str(key))
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    if bad:
        raise AssertionError("body-bearing health artifact %s: %s" % (label, bad[:6]))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    text = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    path.write_text((text + "\n") if text else "", encoding="utf-8")


def _ledger_for(
    *,
    ledger: Optional[CallAuthorizationLedger],
    ledger_path: Optional[Union[str, Path]],
    authority_root: Optional[Union[str, Path]],
    authorization_id: str,
    request_sha256: str,
    settings_sha256: str,
    scope: Mapping[str, str],
) -> CallAuthorizationLedger:
    if ledger is not None:
        return ledger
    if ledger_path is not None:
        path = Path(ledger_path).expanduser().resolve()
        if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in path.parts):
            raise ValueError("health_refuses_frozen_ledger")
        return CallAuthorizationLedger(
            path,
            authorization_id=authorization_id,
            max_calls=MAX_PROVIDER_CALLS,
            provider=PROVIDER_ID,
            model=MODEL_ID,
            protocol=PROTOCOL_VERSION,
            settings_sha256=settings_sha256,
            scope=scope,
            input_sha256=request_sha256,
            artifact_namespace=ARTIFACT_NAMESPACE,
        )
    root = _safe_authority_root(authority_root or DEFAULT_AUTHORITY_ROOT)
    return CallAuthorizationLedger.for_authorization(
        root,
        authorization_id=authorization_id,
        max_calls=MAX_PROVIDER_CALLS,
        provider=PROVIDER_ID,
        model=MODEL_ID,
        protocol=PROTOCOL_VERSION,
        settings_sha256=settings_sha256,
        scope=scope,
        input_sha256=request_sha256,
        artifact_namespace=ARTIFACT_NAMESPACE,
    )


def _empty_result_diagnostics(error_code: str) -> Dict[str, Any]:
    return {
        "content_length": 0,
        "reasoning_length": 0,
        "finish_reason": "not_run",
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0.0,
        "output_sha256": "",
        "strict_parse_code": "not_run",
        "strict_validation_code": "not_run",
        "strict_complete": False,
        "error_code": error_code,
    }


def run_compact_stage_a_protocol_health(
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    model: Optional[CompactStageAHealthModel] = None,
    provider: Optional[CompactStageAHealthModel] = None,
    authorization_id: str = AUTHORIZATION_ID,
    ledger: Optional[CallAuthorizationLedger] = None,
    ledger_path: Optional[Union[str, Path]] = None,
    authority_root: Optional[Union[str, Path]] = None,
    settings_path: Optional[Union[str, Path]] = None,
    settings: Optional[Mapping[str, Any]] = None,
    request: Optional[Mapping[str, Any]] = None,
) -> CompactStageAHealthRunResult:
    """Run one synthetic health exchange and write body-free artifacts.

    ``model`` and ``provider`` are synonyms.  If both are supplied they must
    refer to the same object.  No injected object is called more than once;
    no retry path exists.  The default path is a no-call blocked artifact,
    which is useful for checking guards without any model capability.
    """

    if model is not None and provider is not None and model is not provider:
        raise ValueError("health_model_provider_alias_mismatch")
    model_object = model if model is not None else provider
    output_root = _safe_output_path(output_directory)
    if output_root.exists():
        raise FileExistsError("health_output_is_immutable")
    if not isinstance(authorization_id, str) or not _SAFE_AUTHORIZATION_ID.fullmatch(authorization_id):
        raise ValueError("authorization_id_required")

    request_value: Dict[str, Any] = dict(request) if request is not None else _synthetic_request()
    # Validate before creating a ledger, while still retaining source cues in
    # memory only.  The persisted artifacts below contain hashes/counts only.
    request_stats = measure_wire_size(request_value)
    request_scope = _request_scope(request_value)
    primary_count = topic_limit_for_request(request_value)
    topic_limit = primary_count
    request_sha256 = stable_hash(request_value)
    settings_before = _settings_fingerprint(settings_path, settings)
    settings_after = settings_before
    system_sha256 = stable_hash(SYSTEM_PROMPT)
    user_sha256 = stable_hash(request_value)

    provider_calls = 0
    retry_count = 0
    error_code: Optional[str] = None
    response: Optional[CompactStageAHealthResponse] = None
    parsed: Optional[Dict[str, Any]] = None
    parse_code = "not_run"
    validation_code = "not_run"
    reservation: Any = None
    ledger_object: Optional[CallAuthorizationLedger] = None

    if model_object is None:
        error_code = "synthetic_model_required"
    elif not bool(getattr(model_object, "configured", True)):
        error_code = "provider_unconfigured"
    elif str(getattr(model_object, "model_id", MODEL_ID)) != MODEL_ID:
        error_code = "diagnostic_model_override_refused"
    elif request_stats.http_token_proxy > MAX_INPUT_TOKEN_PROXY:
        error_code = "health_input_token_proxy_exceeded"
    else:
        ledger_object = _ledger_for(
            ledger=ledger,
            ledger_path=ledger_path,
            authority_root=authority_root,
            authorization_id=authorization_id,
            request_sha256=request_sha256,
            settings_sha256=settings_before,
            scope=request_scope,
        )
        try:
            reservation = ledger_object.reserve(
                request_sha256=request_sha256,
                unit_ref="synthetic-health",
                attempt=0,
                provider=PROVIDER_ID,
                model=MODEL_ID,
                protocol=PROTOCOL_VERSION,
                settings_sha256=settings_before,
                scope=request_scope,
                input_sha256=request_sha256,
                artifact_namespace=ARTIFACT_NAMESPACE,
                input_tokens_estimate=request_stats.http_token_proxy,
            )
        except CallBudgetExceeded:
            error_code = "authorization_call_budget_exhausted"
        except ReservationRejected as exc:
            error_code = str(getattr(exc, "code", "input_token_limit_exceeded"))
        except AuthorizationBindingMismatch:
            error_code = "authorization_binding_mismatch"
        if reservation is not None:
            provider_calls = 1
            try:
                # No response_format kwarg is sent.  This is the exact K13/K16
                # protocol choice; only the per-call thinking flag is present.
                ledger_object.mark_started(reservation)
                raw = model_object.complete(
                    SYSTEM_PROMPT,
                    request_value,
                    max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                response = _coerce_response(raw, model_object)
                parsed, parse_code, validation_code = _parse_and_validate(response.content, request_value)
                if response.input_tokens < 0 or response.output_tokens < 0 or response.latency_ms < 0:
                    error_code = "provider_response_metadata"
                elif response.input_tokens > MAX_INPUT_TOKEN_PROXY:
                    error_code = "input_token_limit_exceeded"
                elif response.output_tokens > MAX_OUTPUT_TOKENS:
                    error_code = "output_token_limit_exceeded"
                elif response.finish_reason not in {"stop", ""}:
                    error_code = "output_token_limit_exceeded"
                elif response.model != MODEL_ID:
                    error_code = "diagnostic_model_override_refused"
                elif parse_code != "ok":
                    error_code = "provider_invalid_json"
                elif validation_code != "ok":
                    error_code = "schema_validation_failed"
                else:
                    error_code = None
            except Exception as exc:
                # Never serialize exception text: provider messages can contain
                # request material or secrets.  Only a stable category leaves
                # this boundary.
                error_code = str(getattr(exc, "code", "provider_error"))
                if error_code not in {
                    "provider_invalid_json",
                    "schema_validation_failed",
                    "output_token_limit_exceeded",
                    "input_token_limit_exceeded",
                    "provider_response_shape",
                    "provider_response_metadata",
                }:
                    error_code = "provider_error"
                if response is None:
                    parse_code = "not_run"
                    validation_code = "not_run"

            settings_after = _settings_fingerprint(settings_path, settings)
            if settings_after != settings_before:
                error_code = "settings_mutated"

            input_tokens = _safe_nonnegative_int(response.input_tokens if response else 0)
            output_tokens = _safe_nonnegative_int(response.output_tokens if response else 0)
            latency_ms = _safe_latency(response.latency_ms if response else 0.0)
            try:
                if error_code is None:
                    ledger_object.mark_complete(
                        reservation,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=latency_ms,
                    )
                else:
                    ledger_object.mark_failed(
                        reservation,
                        error_code=error_code,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=latency_ms,
                    )
            except Exception:
                # The reservation remains consumed.  Do not let a ledger
                # finalization detail turn a provider result into a retry.
                if error_code is None:
                    error_code = "authorization_ledger_finalize_error"

    strict_complete = bool(response is not None and error_code is None and parsed is not None)
    status = "available" if strict_complete else "blocked"
    success = strict_complete
    response_diagnostics = (
        {
            "content_length": len(response.content) if response else 0,
            "reasoning_length": _safe_nonnegative_int(response.reasoning_length if response else 0),
            "finish_reason": _safe_finish(response.finish_reason if response else "not_run"),
            "input_tokens": _safe_nonnegative_int(response.input_tokens if response else 0),
            "output_tokens": _safe_nonnegative_int(response.output_tokens if response else 0),
            "latency_ms": _safe_latency(response.latency_ms if response else 0.0),
            "output_sha256": _sha256_bytes(response.content.encode("utf-8")) if response else "",
            "strict_parse_code": parse_code,
            "strict_validation_code": validation_code,
            "strict_complete": strict_complete,
        }
        if response is not None
        else _empty_result_diagnostics(error_code or "not_run")
    )
    candidate = _diagnostic_candidate(
        response.content if response else None,
        parsed,
        parse_code,
        validation_code,
    )
    strict_result = {
        "strict_parse_code": parse_code,
        "strict_validation_code": validation_code,
        "strict_complete": strict_complete,
        "accepted_for_complete": strict_complete,
        "diagnostic_candidate_separate": True,
    }
    source = _safe_metadata(
        getattr(response, "source", getattr(model_object, "source", SOURCE_ID))
        if response
        else (getattr(model_object, "source", SOURCE_ID) if model_object else SOURCE_ID),
        SOURCE_ID,
    )
    model_name = MODEL_ID
    request_hash = request_sha256
    ledger_snapshot: Dict[str, Any]
    ledger_rows: list[Dict[str, Any]]
    ledger_rejections: list[Dict[str, Any]]
    if ledger_object is not None:
        ledger_snapshot = ledger_object.snapshot()
        ledger_rows = [dict(row) for row in ledger_object.records()]
        ledger_rejections = [dict(row) for row in ledger_object.rejections()]
    else:
        ledger_snapshot = {
            "schema_version": "persistent_call_budget_v1",
            "authorization_id": authorization_id,
            "calls_used": 0,
            "calls_remaining": MAX_PROVIDER_CALLS,
            "reservation_count": 0,
            "rejection_count": 0,
            "body_free": True,
        }
        ledger_rows = []
        ledger_rejections = []

    safe_error = error_code
    errors = []
    if safe_error:
        errors.append(
            {
                "phase": "synthetic_health",
                "error_code": safe_error,
                "authorization_id": authorization_id,
                "provider_calls": provider_calls,
            }
        )

    authorization_public = {
        "authorization_id": authorization_id,
        "provider": PROVIDER_ID,
        "model": MODEL_ID,
        "protocol": PROTOCOL_VERSION,
        "scope_sha256": stable_hash(request_scope),
        "settings_sha256": settings_before,
        "input_sha256": request_sha256,
        "artifact_namespace": ARTIFACT_NAMESPACE,
        "max_calls": MAX_PROVIDER_CALLS,
    }
    aggregate: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "status": status,
        "success": success,
        "diagnostic_only": True,
        "synthetic_only": True,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": retry_count,
        "development_input_read": False,
        "development_calls": 0,
        "frozen_read": False,
        "private_input_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "model": model_name,
        "source": source,
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "extra_body": {
            "sent": provider_calls > 0,
            "field_names": ["thinking"],
            "per_call": True,
            "value_shape": "disabled",
            "global_settings_mutated": False,
        },
        "authorization": authorization_public,
        "authorization_ledger": ledger_snapshot,
        "request": {
            "message_count": sum(row["k"] == "m" for row in request_value["h"]),
            "candidate_count": sum(row["k"] == "c" for row in request_value["h"]),
            "primary_count": primary_count,
            "topic_limit": topic_limit,
            "topic_limit_rule": TOPIC_LIMIT_RULE,
            "request_sha256": request_hash,
            "system_prompt_sha256": system_sha256,
            "user_packet_sha256": user_sha256,
            "input_token_proxy": request_stats.http_token_proxy,
            "input_token_proxy_limit": MAX_INPUT_TOKEN_PROXY,
        },
        "strict_result": strict_result,
        "diagnostic_candidate": candidate,
        "response_diagnostics": response_diagnostics,
        "settings_before_sha256": settings_before,
        "settings_after_sha256": settings_after,
        "settings_unchanged": settings_before == settings_after,
        "errors": {"count": len(errors), "codes": sorted({str(row["error_code"]) for row in errors})},
    }
    cost = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "provider_calls": provider_calls,
        "retry_count": retry_count,
        "input_tokens": response_diagnostics["input_tokens"],
        "output_tokens": response_diagnostics["output_tokens"],
        "latency_ms": response_diagnostics["latency_ms"],
        "input_token_proxy": request_stats.http_token_proxy,
        "input_token_proxy_limit": MAX_INPUT_TOKEN_PROXY,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "cache_hit": False,
        "authorization_calls_used": int(ledger_snapshot.get("calls_used", provider_calls) or 0),
    }
    diagnostic = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "strict_result": strict_result,
        "diagnostic_candidate": candidate,
        "response_diagnostics": response_diagnostics,
        "model": model_name,
        "source": source,
        "protocol_version": PROTOCOL_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "thinking_disabled": THINKING_DISABLED,
    }
    # The copied ledger rows contain only persistent_call_budget's body-free
    # hashes/enums.  Rejections are kept separate so preflight rejects cannot
    # be mistaken for provider calls.
    manifest: Dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "local_day": "2026-08-25",
        "split": "synthetic",
        "status": status,
        "success": success,
        "diagnostic_only": True,
        "synthetic_only": True,
        "provider_called": provider_calls > 0,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": retry_count,
        "development_input_read": False,
        "private_input_read": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": {"id": PROVIDER_ID, "model": MODEL_ID, "source": source},
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "response_format_mode": RESPONSE_FORMAT_MODE,
        "response_format_sent": False,
        "thinking_disabled": THINKING_DISABLED,
        "authorization": authorization_public,
        "authorization_ledger": ledger_snapshot,
        "settings_before_sha256": settings_before,
        "settings_after_sha256": settings_after,
        "settings_unchanged": settings_before == settings_after,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    ledger_file_rows = list(ledger_rows)
    if ledger_rejections:
        ledger_file_rows.extend(
            {
                "record_type": "preflight_rejection",
                "authorization_id": row.get("authorization_id", authorization_id),
                "request_sha256": row.get("request_sha256", ""),
                "unit_ref_sha256": row.get("unit_ref_sha256", ""),
                "attempt": row.get("attempt", 0),
                "error_code": row.get("code", "unknown"),
            }
            for row in ledger_rejections
        )
    outputs: Dict[str, Any] = {
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": diagnostic,
        "ledger": ledger_file_rows,
        "errors": errors,
    }
    for label, value in outputs.items():
        _assert_body_free(value, label=label)
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_json(output_root / OUTPUT_FILENAMES["diagnostic"], diagnostic)
    _write_jsonl(output_root / OUTPUT_FILENAMES["ledger"], ledger_file_rows)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _file_sha256(output_root / filename)
        for key, filename in OUTPUT_FILENAMES.items()
        if key != "manifest"
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return CompactStageAHealthRunResult(
        output_directory=str(output_root),
        status=status,
        success=success,
        strict_complete=strict_complete,
        provider_calls=provider_calls,
        retry_count=retry_count,
        error_code=error_code,
        artifact_paths=paths,
        aggregate=aggregate,
    )


run_stage_a_compact_health = run_compact_stage_a_protocol_health
run_compact_stage_a_health = run_compact_stage_a_protocol_health
run_compact_stage_a_health_probe = run_compact_stage_a_protocol_health
run_stage_a_protocol_health = run_compact_stage_a_protocol_health
run_synthetic_compact_stage_a_health = run_compact_stage_a_protocol_health
build_health_request = build_synthetic_health_request


class CompactStageAProtocolHealthRunner:
    """Small configured facade around the function-based health boundary."""

    def __init__(self, output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY, **kwargs: Any) -> None:
        self.output_directory = output_directory
        self._kwargs = dict(kwargs)

    def run(self, *, model: Optional[CompactStageAHealthModel] = None, provider: Optional[CompactStageAHealthModel] = None, **kwargs: Any) -> CompactStageAHealthRunResult:
        options = dict(self._kwargs)
        options.update(kwargs)
        return run_compact_stage_a_protocol_health(
            self.output_directory,
            model=model,
            provider=provider,
            **options,
        )


CompactStageAHealthRunner = CompactStageAProtocolHealthRunner


__all__ = [
    "ARTIFACT_VERSION",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "PROTOCOL_VERSION",
    "PROMPT_VERSION",
    "MODEL_ID",
    "PROVIDER_ID",
    "SOURCE_ID",
    "RESPONSE_FORMAT_MODE",
    "THINKING_DISABLED",
    "MAX_PROVIDER_CALLS",
    "MAX_RETRIES",
    "HEALTH_MAX_OUTPUT_TOKENS",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "OUTPUT_FILENAMES",
    "CompactStageAHealthError",
    "CompactStageAHealthModel",
    "CompactStageAHealthResponse",
    "FakeCompactStageAHealthModel",
    "FakeStageAHealthModel",
    "SyntheticCompactStageAHealthModel",
    "CompactStageAProtocolHealthRunner",
    "CompactStageAHealthRunner",
    "CompactStageAHealthRunResult",
    "HealthRunResult",
    "build_synthetic_health_request",
    "build_health_request",
    "run_compact_stage_a_protocol_health",
    "run_stage_a_compact_health",
    "run_compact_stage_a_health",
    "run_compact_stage_a_health_probe",
    "run_stage_a_protocol_health",
    "run_synthetic_compact_stage_a_health",
]


if __name__ == "__main__":  # pragma: no cover - defensive CLI only
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    args = parser.parse_args()
    result = run_compact_stage_a_protocol_health(args.output_directory)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result.success else 1)
