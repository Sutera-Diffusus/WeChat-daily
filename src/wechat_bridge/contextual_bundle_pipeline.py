"""Shadow orchestration for the registry -> gate -> dialogue-bundle path.

This module is deliberately a side-car.  It does not import the event, title,
or frontend layers and it has no filesystem side effects.  Callers provide an
in-memory public message sequence; :class:`ContextualBundlePipeline` returns a
replayable, body-free audit projection and the fixed bundle-semantic outputs.

The four stages are kept explicit because a context window is not an event:

``public messages -> MessageRegistry -> SemanticGate -> DialogueBundleBuilder
-> BundleSemanticPipeline``

An optional model is reached only through an injected adapter.  ``disabled``
uses the conservative ``contextual_fragments`` fallback, ``fake`` is intended
for deterministic tests, and ``real`` uses the existing OpenAI-compatible
configuration shape without ever serializing a key.  A missing provider is a
recorded block followed by the conservative fallback; it is never replaced by
fabricated model output.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .bundle_semantics import (
    BUNDLE_PROMPT_VERSION,
    BUNDLE_RULESET_VERSION,
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_FIELDS,
    BundleSemanticError,
    BundleSchemaError,
    BundleSemanticPipeline,
    EncodingOutcome,
    VersionedBundleCache,
    bundle_schema,
    empty_bundle,
    stable_hash,
    validate_bundle,
)
from .dialogue_bundle import (
    BUNDLE_PIPELINE_VERSION,
    BUNDLE_RULESET_VERSION as DIALOGUE_RULESET_VERSION,
    DialogueBundle,
    DialogueBundleBuilder,
    DialogueBundleResult,
    SCALE_COLD,
    SCALE_LOCAL,
    SCALE_MICRO,
    SCALE_SESSION,
    SCALE_TURN,
)
from .semantic_gate import SemanticGate
from .semantic_registry import MessageRegistry, RegisteredMessage, UNKNOWN
from .semantic_frame import (
    SEMANTIC_FRAME_VERSION,
    SemanticFrameParseError,
    parse_bundle_frame,
    parse_health_frame,
)


PIPELINE_SCHEMA_VERSION = "contextual_bundle_pipeline_v1"
PIPELINE_VERSION = "workstream_c_shadow_v1"
PIPELINE_RULESET_VERSION = "contextual_bundle_pipeline_rules_v1"
SUPPORTED_MODES = frozenset({"disabled", "fake", "real"})
DEFAULT_MODEL = "gpt-5.2"
DEFAULT_MAX_LLM_BUNDLE_CALLS = 14
DEFAULT_MAX_INPUT_TOKENS = 2000
DEFAULT_MAX_OUTPUT_TOKENS = 400


class PipelineConfigurationError(ValueError):
    """Raised for an unsafe or incomplete shadow-pipeline configuration."""


class ProviderUnavailable(RuntimeError):
    """Raised internally when a real provider cannot be constructed."""


class BudgetExceeded(RuntimeError):
    """An LLM call was not permitted by the fixed shadow budget."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        super().__init__(message or self.code)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _estimate_tokens(value: Any) -> int:
    text = _canonical_json(value)
    return max(1, len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))) if text else 0


def _known(value: Any) -> bool:
    return value not in (None, "", UNKNOWN, "UNKNOWN")


def _string(value: Any, default: str = UNKNOWN) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _usage_tokens(value: Any) -> Tuple[int, int]:
    """Read common provider usage shapes without depending on a provider SDK."""

    usage: Any = value.get("usage") if isinstance(value, Mapping) else None
    if usage is None and hasattr(value, "usage"):
        usage = getattr(value, "usage")
    if usage is None:
        return 0, 0

    def read(*names: str) -> int:
        for name in names:
            if isinstance(usage, Mapping):
                item = usage.get(name)
            else:
                item = getattr(usage, name, None)
            try:
                if item is not None:
                    return max(0, int(item))
            except (TypeError, ValueError):
                continue
        return 0

    return (
        read("prompt_tokens", "input_tokens", "prompt_token_count"),
        read("completion_tokens", "output_tokens", "completion_token_count"),
    )


def _provider_error_code(exc: BaseException) -> str:
    """Map provider/model failures to stable, body-free audit codes."""

    if isinstance(exc, BudgetExceeded):
        return exc.code
    if isinstance(exc, SemanticFrameParseError):
        return exc.code
    if isinstance(exc, ProviderUnavailable):
        safe_codes = {"provider_not_configured", "openai_sdk_unavailable", "model_interface_missing"}
        if exc.args and str(exc.args[0]).strip() in safe_codes:
            return str(exc.args[0]).strip()
        return "provider_unavailable"
    if isinstance(exc, BundleSchemaError):
        return "schema_validation_failed"
    if isinstance(exc, BundleSemanticError):
        safe_codes = {
            "provider_response_missing_text",
            "provider_response_not_json",
            "provider_response_not_object",
            "provider_health_response_not_text",
            "provider_health_response_not_json",
            "provider_health_schema_invalid",
            "provider_health_multiple_json_objects",
            "model_interface_missing",
            "provider_not_configured",
            "openai_sdk_unavailable",
        }
        if exc.args and str(exc.args[0]).strip() in safe_codes:
            return str(exc.args[0]).strip()
        return "semantic_contract_error"

    name = type(exc).__name__.casefold()
    if "badrequest" in name or "unsupported" in name or "invalidrequest" in name:
        return "provider_protocol_error"
    if "jsondecode" in name or "parse" in name:
        return "provider_response_not_json"
    if "timeout" in name:
        return "provider_timeout"
    if "ratelimit" in name or "rate_limit" in name:
        return "provider_rate_limited"
    if "authentication" in name or "permission" in name or "authorization" in name:
        return "provider_authentication_error"
    if "connection" in name or "network" in name:
        return "provider_network_error"
    if "semanticframe" in name:
        return "semantic_frame_protocol_error"
    return "provider_error"


def _safe_public_message(entry: RegisteredMessage) -> Dict[str, Any]:
    """Create the deliberately narrow message envelope sent to bundle code."""

    metadata = entry.metadata
    value: Dict[str, Any] = {
        "message_id": entry.message_id,
        "account_id": metadata.account_id,
        "chat_id": metadata.chat_id,
        "speaker_id": metadata.speaker_id,
        "message_type": metadata.message_type,
        "content": entry.content,
    }
    optional = {
        "timestamp": metadata.timestamp,
        "time_offset_seconds": metadata.time_offset_seconds,
        "sequence_in_chat": metadata.sequence_in_chat,
        "reply_to_message_id": metadata.reply_to_message_id,
        "segment_id": metadata.dialogue_segment_id,
    }
    value.update({key: item for key, item in optional.items() if item is not None})
    return value


def _body_free(value: Any) -> Any:
    """Project an object to a safe audit surface, dropping body-like keys."""

    body_names = {
        "text",
        "content",
        "body",
        "raw",
        "raw_text",
        "raw_message",
        "message_text",
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
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lower = name.casefold()
            if name in body_names or lower.endswith("_text") or lower.endswith("_content") or lower.endswith("_surface"):
                continue
            output[name] = _body_free(item)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_body_free(item) for item in value]
    return value


def _dialogue_bundle_projection(bundle: DialogueBundle) -> Dict[str, Any]:
    """Serialize only structural bundle fields; no fragment or claim body."""

    value = _body_free(bundle.to_dict())
    value["bundle_hash"] = stable_hash(value)
    return value


def _dialogue_relation_projection(value: Any) -> Dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    projected = _body_free(value)
    if isinstance(projected, Mapping):
        projected = dict(projected)
        projected["relation_hash"] = stable_hash(projected)
    return dict(projected) if isinstance(projected, Mapping) else {"value": projected}


@dataclass(frozen=True)
class AIProviderConfig:
    """Public provider configuration with the credential intentionally hidden."""

    provider: str = "openai"
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = field(default=None, repr=False, compare=False)
    base_url: Optional[str] = field(default=None, repr=False, compare=False)
    timeout_seconds: Optional[float] = None

    @classmethod
    def from_environment(
        cls,
        *,
        model_env: str = "OPENAI_WECHAT_ANALYSIS_MODEL",
        key_env: str = "OPENAI_API_KEY",
        base_url_env: str = "OPENAI_BASE_URL",
    ) -> "AIProviderConfig":
        """Read only standard names; the secret never appears in ``to_dict``."""

        return cls(
            provider="openai",
            model=os.environ.get(model_env) or DEFAULT_MODEL,
            api_key=os.environ.get(key_env) or None,
            base_url=os.environ.get(base_url_env) or None,
        )

    @classmethod
    def from_workbench_settings(cls, settings: Any) -> "AIProviderConfig":
        """Load the same local settings object used by ``web.py``.

        ``WorkbenchSettings.snapshot(include_secrets=True)`` is called only
        inside this boundary.  The returned credential is retained in the
        private config object for the provider client, never returned by
        ``public_dict`` and never put into a manifest or error record.
        """

        if isinstance(settings, (str, os.PathLike)):
            from .settings import WorkbenchSettings

            settings = WorkbenchSettings(str(settings))
        snapshot_method = getattr(settings, "snapshot", None)
        if not callable(snapshot_method):
            raise PipelineConfigurationError("workbench settings object must expose snapshot")
        snapshot = snapshot_method(include_secrets=True)
        if not isinstance(snapshot, Mapping):
            raise PipelineConfigurationError("workbench settings snapshot is not an object")
        ai = snapshot.get("ai") or {}
        if not isinstance(ai, Mapping):
            ai = {}
        environment = cls.from_environment()
        return cls(
            provider="openai",
            model=str(ai.get("model") or environment.model or DEFAULT_MODEL),
            api_key=str(ai.get("api_key") or environment.api_key or "") or None,
            base_url=str(ai.get("base_url") or environment.base_url or "") or None,
            timeout_seconds=environment.timeout_seconds,
        )

    @classmethod
    def from_workbench_settings_path(cls, path: Any) -> "AIProviderConfig":
        """Path-based convenience wrapper used by the offline runner."""

        return cls.from_workbench_settings(path)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "base_url_configured": bool(self.base_url),
            "timeout_seconds": self.timeout_seconds,
        }

    to_dict = public_dict


@dataclass(frozen=True)
class ProviderHealthResult:
    """Body-free result of one provider availability probe."""

    ok: bool
    status: str
    source: str
    model: str
    request_sha256: str
    input_tokens: int
    output_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    latency_ms: Any
    error_code: Optional[str] = None
    config: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "status": self.status,
            "source": self.source,
            "model": self.model,
            "request_sha256": self.request_sha256,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "max_input_tokens": int(self.max_input_tokens),
            "max_output_tokens": int(self.max_output_tokens),
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "config": _jsonable(self.config),
            "diagnostics": _jsonable(self.diagnostics),
        }


class OpenAIBundleModel:
    """OpenAI-compatible adapter for the fixed bundle schema.

    Importing this class is side-effect free.  A client is constructed only on
    the first call and only when a key was explicitly configured.  Responses
    are reduced to a JSON mapping plus provider usage; request bodies are never
    retained in an audit record.
    """

    source = "openai"

    def __init__(self, config: Optional[AIProviderConfig] = None) -> None:
        self.config = config or AIProviderConfig.from_environment()
        self.model_version = self.config.model
        self._client: Any = None

    @property
    def configured(self) -> bool:
        return self.config.configured

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.configured:
            raise ProviderUnavailable("provider_not_configured")
        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency is optional in tests
            raise ProviderUnavailable("openai_sdk_unavailable") from exc
        kwargs: Dict[str, Any] = {"api_key": self.config.api_key}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.timeout_seconds is not None:
            kwargs["timeout"] = self.config.timeout_seconds
        self._client = OpenAI(**kwargs)
        return self._client

    @staticmethod
    def _response_text(response: Any) -> str:
        if isinstance(response, Mapping):
            for key in ("output_text", "text", "content"):
                value = response.get(key)
                if isinstance(value, str):
                    return value
            choices = response.get("choices")
        else:
            value = getattr(response, "output_text", None)
            if isinstance(value, str):
                return value
            choices = getattr(response, "choices", None)
        if choices:
            first = choices[0]
            message = first.get("message") if isinstance(first, Mapping) else getattr(first, "message", None)
            content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif isinstance(getattr(item, "text", None), str):
                        parts.append(getattr(item, "text"))
                if parts:
                    return "".join(parts)
        raise BundleSemanticError("provider_response_missing_text")

    @staticmethod
    def _parse_health_response(text: Any) -> Mapping[str, bool]:
        """Extract exactly one strict health object from provider text.

        OpenAI-compatible gateways sometimes wrap a structured response in a
        Markdown fence or a short explanation.  We tolerate that envelope,
        but do not repair JSON, coerce values, or select a nested/one-of-many
        object.  The only accepted schema is ``{"ok": <boolean>}``.
        """

        if not isinstance(text, str):
            raise BundleSemanticError("provider_health_response_not_text")
        decoder = json.JSONDecoder()
        candidates: List[Tuple[Mapping[str, Any], int, int]] = []
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                value, end = decoder.raw_decode(text, index)
            except (TypeError, ValueError):
                continue
            if isinstance(value, Mapping):
                candidates.append((value, index, end))

        if not candidates:
            raise BundleSemanticError("provider_health_response_not_json")

        # A nested object or a second object in the surrounding text must not
        # be mistaken for the top-level response.  Bracket characters outside
        # the candidate similarly reject arrays/wrapped JSON structures while
        # still allowing ordinary prose and Markdown fences around one object.
        strict: List[Mapping[str, bool]] = []
        for value, start, end in candidates:
            surrounding = text[:start] + text[end:]
            if any(character in "{}[]" for character in surrounding):
                continue
            if set(value.keys()) != {"ok"} or type(value.get("ok")) is not bool:
                continue
            strict.append({"ok": bool(value["ok"])})

        if len(strict) > 1:
            raise BundleSemanticError("provider_health_multiple_json_objects")
        if len(strict) != 1:
            raise BundleSemanticError("provider_health_schema_invalid")
        return strict[0]

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        client = self._get_client()
        schema = request.get("response_schema") or bundle_schema(BUNDLE_SCHEMA_VERSION)
        # The semantic encoder already limits fields to the safe in-memory
        # envelope.  Keep the prompt stable and use no display names.
        user_payload = {
            "bundle_id": request.get("bundle_id"),
            "chat_id": request.get("chat_id"),
            "messages": request.get("messages", ()),
        }
        instructions = (
            "Return one JSON object and no prose using exactly these keys: "
            "schema_version,bundle_id,message_ids,speaker,subject,mentioned_person,"
            "target,object,action,claim_type,state,modality,coreference_candidates,"
            "context_relations,uncertainties,evidence,metadata. Keep speaker, subject, "
            "and mentioned_person separate. Use unknown for unsupported or insufficient "
            "values. Every known entity, action, claim_type, state, modality, or non-"
            "insufficient relation must cite evidence_ids from the supplied messages. "
            "Do not create cross-chat links; time alone cannot make a strong relation."
        )
        if self.config.base_url:
            response = client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": _canonical_json(user_payload)},
                ],
                # The existing WorkbenchSettings base_url denotes an
                # OpenAI-compatible gateway.  Some such gateways reject the
                # Responses-style ``json_schema`` envelope even though they
                # support JSON mode.  Keep the fixed schema in the prompt and
                # enforce it again with normalize_bundle/validate_bundle below.
                response_format={"type": "json_object"},
                max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
        else:
            response = client.responses.create(
                model=self.config.model,
                instructions=instructions,
                input=_canonical_json(user_payload),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "bundle_semantics",
                        "strict": True,
                        "schema": schema,
                    }
                },
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
        text = self._response_text(response)
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise BundleSemanticError("provider_response_not_json") from exc
        if not isinstance(parsed, Mapping):
            raise BundleSemanticError("provider_response_not_object")
        input_tokens, output_tokens = _usage_tokens(response)
        result = dict(parsed)
        result["usage"] = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        }
        return result

    def health_check(
        self,
        *,
        max_input_tokens: int = 500,
        max_output_tokens: int = 100,
        clock: Optional[Callable[[], float]] = None,
    ) -> ProviderHealthResult:
        """Perform one tiny, non-identifying JSON health request.

        The payload is a constant synthetic probe and is never returned.  A
        successful HTTP/provider response is enough to mark availability; the
        response body is parsed only to make sure the endpoint returned a JSON
        object, while its contents are discarded.
        """

        clock_fn = clock or time.perf_counter
        request = {
            "health_check": "synthetic",
            "purpose": "provider_availability",
            "response": {"ok": "boolean"},
        }
        request_hash = stable_hash(request)
        input_estimate = _estimate_tokens(request)
        if input_estimate > int(max_input_tokens):
            return ProviderHealthResult(
                False,
                "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_estimate,
                0,
                int(max_input_tokens),
                int(max_output_tokens),
                0.0,
                "input_token_limit_exceeded",
                self.config.public_dict(),
            )
        started = clock_fn()
        try:
            client = self._get_client()
            instructions = "Return only a JSON object with boolean field ok=true. This is a synthetic availability probe."
            if self.config.base_url:
                response = client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": _canonical_json(request)},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=int(max_output_tokens),
                )
            else:
                response = client.responses.create(
                    model=self.config.model,
                    instructions=instructions,
                    input=_canonical_json(request),
                    store=False,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "provider_health",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        }
                    },
                    max_output_tokens=int(max_output_tokens),
                )
            text = self._response_text(response)
            parsed = self._parse_health_response(text)
            input_tokens, output_tokens = _usage_tokens(response)
            input_tokens = input_tokens or input_estimate
            output_tokens = output_tokens or _estimate_tokens(parsed)
            latency_ms = round((clock_fn() - started) * 1000.0, 3)
            if input_tokens > int(max_input_tokens):
                return ProviderHealthResult(
                    False,
                    "blocked",
                    self.source,
                    self.model_version,
                    request_hash,
                    input_tokens,
                    output_tokens,
                    int(max_input_tokens),
                    int(max_output_tokens),
                    latency_ms,
                    "input_token_limit_exceeded",
                    self.config.public_dict(),
                )
            if output_tokens > int(max_output_tokens):
                return ProviderHealthResult(
                    False,
                    "blocked",
                    self.source,
                    self.model_version,
                    request_hash,
                    input_tokens,
                    output_tokens,
                    int(max_input_tokens),
                    int(max_output_tokens),
                    latency_ms,
                    "output_token_limit_exceeded",
                    self.config.public_dict(),
                )
            return ProviderHealthResult(
                True,
                "available",
                self.source,
                self.model_version,
                request_hash,
                input_tokens,
                output_tokens,
                int(max_input_tokens),
                int(max_output_tokens),
                latency_ms,
                None,
                self.config.public_dict(),
            )
        except Exception as exc:
            error_code = _provider_error_code(exc)
            return ProviderHealthResult(
                False,
                "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_estimate,
                0,
                int(max_input_tokens),
                int(max_output_tokens),
                round((clock_fn() - started) * 1000.0, 3),
                error_code,
                self.config.public_dict(),
            )


class SemanticFrameBundleModel(OpenAIBundleModel):
    """OpenAI-compatible adapter using the provider-independent frame wire format."""

    source = "openai-semantic-frame"
    protocol_version = SEMANTIC_FRAME_VERSION

    def __init__(
        self,
        config: Optional[AIProviderConfig] = None,
        *,
        thinking_disabled: bool = False,
        max_input_chars: int = 1800,
    ) -> None:
        super().__init__(config)
        self.thinking_disabled = bool(thinking_disabled)
        self.max_input_chars = max(256, int(max_input_chars))

    @staticmethod
    def _compact_frame_exemplar() -> str:
        """Return a shortest valid all-unknown frame for prompt anchoring."""

        example = empty_bundle(
            "semantic-frame-health-bundle",
            ("semantic-frame-health-message",),
            chat_id="semantic-frame-health-chat",
            status="complete",
            source="health",
        )
        # The metadata object is intentionally empty: it is optional at the
        # frame boundary and keeps the exemplar well below the input cap.
        example["metadata"] = {}
        lines = [
            "%s\t%s" % (field, json.dumps(example[field], ensure_ascii=False, separators=(",", ":")))
            for field in BUNDLE_FIELDS
        ]
        return "BEGIN_SEMANTIC_FRAME_V1_EXAMPLE\n" + "\n".join(lines) + "\nEND_SEMANTIC_FRAME_V1_EXAMPLE"

    @staticmethod
    def _safe_response_metadata(response: Any, *, response_text: Optional[str] = None) -> Dict[str, Any]:
        """Inspect provider response metadata without retaining response bodies."""

        if isinstance(response, Mapping):
            field_names = sorted(str(key) for key in response.keys())
            usage = response.get("usage")
            choices = response.get("choices")
            status_code = response.get("status_code")
            output_text = response.get("output_text")
            content = response.get("content")
            reasoning_content = response.get("reasoning_content")
            raw_response = response.get("_response")
        else:
            field_names = sorted(str(key) for key in vars(response).keys()) if hasattr(response, "__dict__") else []
            usage = getattr(response, "usage", None)
            choices = getattr(response, "choices", None)
            status_code = getattr(response, "status_code", None)
            output_text = getattr(response, "output_text", None)
            content = getattr(response, "content", None)
            reasoning_content = getattr(response, "reasoning_content", None)
            raw_response = getattr(response, "_response", None)
        if choices:
            first = choices[0]
            message = first.get("message") if isinstance(first, Mapping) else getattr(first, "message", None)
            if isinstance(message, Mapping):
                if content is None:
                    content = message.get("content")
                if reasoning_content is None:
                    reasoning_content = message.get("reasoning_content")
            elif message is not None:
                if content is None:
                    content = getattr(message, "content", None)
                if reasoning_content is None:
                    reasoning_content = getattr(message, "reasoning_content", None)
        if status_code is None and raw_response is not None:
            status_code = getattr(raw_response, "status_code", None)
        finish_reasons: List[str] = []
        if choices:
            for choice in choices:
                value = choice.get("finish_reason") if isinstance(choice, Mapping) else getattr(choice, "finish_reason", None)
                if isinstance(value, str):
                    finish_reasons.append(value if value in {"stop", "length", "content_filter", "tool_calls"} else "unknown")
        input_tokens, output_tokens = _usage_tokens(response)

        def length(value: Any) -> int:
            if isinstance(value, str):
                return len(value)
            if isinstance(value, (list, tuple, Mapping)):
                return len(value)
            return 0

        text_length = length(response_text) if response_text is not None else max(length(output_text), length(content))
        reasoning_length = length(reasoning_content)
        raw_hash = hashlib.sha256()
        for value in (response_text, content, reasoning_content):
            if isinstance(value, str):
                raw_hash.update(value.encode("utf-8", errors="replace"))
                raw_hash.update(b"\0")
        try:
            status = int(status_code) if status_code is not None else "N/A"
        except (TypeError, ValueError):
            status = "N/A"
        return {
            "response_fields": field_names,
            "content_length": text_length,
            "reasoning_content_length": reasoning_length,
            "finish_reasons": finish_reasons,
            "usage_keys": sorted(str(key) for key in usage.keys()) if isinstance(usage, Mapping) else [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "http_status": status,
            "response_sha256": raw_hash.hexdigest(),
            "raw_content_saved": False,
            "raw_reasoning_saved": False,
        }

    def _extra_body(self) -> Optional[Mapping[str, Any]]:
        if not self.thinking_disabled:
            return None
        # This fixed option is passed only when explicitly requested by the
        # caller; it is never loaded from or written back to global settings.
        return {"thinking": {"type": "disabled"}}

    def _compact_payload(self, request: Mapping[str, Any]) -> Tuple[Dict[str, Any], int]:
        """Bound provider input while retaining every message id and chat id."""

        source_messages = request.get("messages", ())
        messages: List[Dict[str, Any]] = []
        for item in source_messages if isinstance(source_messages, (list, tuple)) else ():
            if not isinstance(item, Mapping):
                continue
            row: Dict[str, Any] = {
                "message_id": item.get("message_id"),
                "chat_id": item.get("chat_id"),
                "speaker_id": item.get("speaker_id"),
                "message_type": item.get("message_type"),
            }
            content = item.get("content")
            if isinstance(content, str):
                row["content"] = content[:240]
            messages.append({key: value for key, value in row.items() if value is not None})
        payload = {
            "protocol": self.protocol_version,
            "bundle_id": request.get("bundle_id"),
            "chat_id": request.get("chat_id"),
            "messages": messages,
        }
        encoded = _canonical_json(payload)
        if len(encoded) > self.max_input_chars:
            for row in messages:
                if isinstance(row.get("content"), str):
                    row["content"] = row["content"][:64]
            payload["messages"] = messages
            encoded = _canonical_json(payload)
        if len(encoded) > self.max_input_chars:
            for row in messages:
                row.pop("content", None)
            payload["messages"] = messages
            encoded = _canonical_json(payload)
        # IDs are never dropped.  If a pathological synthetic id set alone is
        # larger than the cap, the caller records the bounded-input violation
        # instead of silently changing the bundle's scope.
        return payload, len(encoded)

    def health_check(
        self,
        *,
        max_input_tokens: int = 500,
        max_output_tokens: int = 100,
        clock: Optional[Callable[[], float]] = None,
    ) -> ProviderHealthResult:
        request = {
            "protocol": self.protocol_version,
            "health_check": "synthetic",
            "frame": "OK\\ttrue",
        }
        request_hash = stable_hash(request)
        input_estimate = _estimate_tokens(request)
        clock_fn = clock or time.perf_counter
        if input_estimate > int(max_input_tokens):
            return ProviderHealthResult(
                False,
                "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_estimate,
                0,
                int(max_input_tokens),
                int(max_output_tokens),
                0.0,
                "input_token_limit_exceeded",
                self.config.public_dict(),
            )
        started = clock_fn()
        try:
            client = self._get_client()
            instructions = (
                "This is a synthetic availability probe. Return exactly one line "
                "OK<TAB>true and no other frame. Do not use Markdown or JSON."
            )
            if self.config.base_url:
                call_kwargs: Dict[str, Any] = {
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": "semantic_frame_v1 health probe"},
                    ],
                    "max_tokens": int(max_output_tokens),
                }
                extra_body = self._extra_body()
                if extra_body is not None:
                    call_kwargs["extra_body"] = extra_body
                response = client.chat.completions.create(**call_kwargs)
            else:
                response = client.responses.create(
                    model=self.config.model,
                    instructions=instructions,
                    input="semantic_frame_v1 health probe",
                    store=False,
                    max_output_tokens=int(max_output_tokens),
                )
            text = self._response_text(response)
            ok = parse_health_frame(text)
            input_tokens, output_tokens = _usage_tokens(response)
            input_tokens = input_tokens or input_estimate
            output_tokens = output_tokens or max(1, _estimate_tokens({"frame": "OK", "value": ok}))
            latency_ms = round((clock_fn() - started) * 1000.0, 3)
            if input_tokens > int(max_input_tokens):
                error_code = "input_token_limit_exceeded"
                ok = False
            elif output_tokens > int(max_output_tokens):
                error_code = "output_token_limit_exceeded"
                ok = False
            elif not ok:
                error_code = "provider_health_negative"
            else:
                error_code = None
            return ProviderHealthResult(
                bool(ok),
                "available" if ok else "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_tokens,
                output_tokens,
                int(max_input_tokens),
                int(max_output_tokens),
                latency_ms,
                error_code,
                self.config.public_dict(),
            )
        except Exception as exc:
            return ProviderHealthResult(
                False,
                "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_estimate,
                0,
                int(max_input_tokens),
                int(max_output_tokens),
                round((clock_fn() - started) * 1000.0, 3),
                _provider_error_code(exc),
                self.config.public_dict(),
            )

    def bundle_health_check(
        self,
        *,
        max_input_tokens: int = 500,
        max_output_tokens: int = 100,
        clock: Optional[Callable[[], float]] = None,
    ) -> ProviderHealthResult:
        """Probe a complete synthetic bundle and retain metadata only."""

        synthetic = empty_bundle(
            "semantic-frame-health-bundle",
            ("semantic-frame-health-message",),
            chat_id="semantic-frame-health-chat",
            status="complete",
            source="health",
        )
        request = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": synthetic["bundle_id"],
            "chat_id": synthetic["metadata"]["chat_id"],
            "messages": [
                {
                    "message_id": "semantic-frame-health-message",
                    "chat_id": "semantic-frame-health-chat",
                    "speaker_id": "semantic-frame-health-speaker",
                    "message_type": "text",
                    "content": "synthetic",
                }
            ],
        }
        compact_payload, input_chars = self._compact_payload(request)
        request_hash = stable_hash({"protocol": self.protocol_version, "probe": "synthetic_bundle", "payload": compact_payload})
        input_estimate = _estimate_tokens(compact_payload)
        clock_fn = clock or time.perf_counter
        base_diagnostics: Dict[str, Any] = {
            "probe_kind": "synthetic_bundle",
            "protocol": self.protocol_version,
            "input_chars": input_chars,
            "input_chars_limit": self.max_input_chars,
            "thinking_disabled": self.thinking_disabled,
            "response_format_sent": False,
            "missing_field_names": [],
            "extra_field_names": [],
            "duplicate": [],
            "order_error": [],
        }
        if input_chars > self.max_input_chars or input_estimate > int(max_input_tokens):
            base_diagnostics.update({"input_token_estimate": input_estimate, "input_bound_violation": True})
            return ProviderHealthResult(
                False,
                "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_estimate,
                0,
                int(max_input_tokens),
                int(max_output_tokens),
                0.0,
                "input_token_limit_exceeded",
                self.config.public_dict(),
                base_diagnostics,
            )
        started = clock_fn()
        response: Any = None
        response_text: Optional[str] = None
        diagnostics = dict(base_diagnostics)
        try:
            client = self._get_client()
            instructions = (
                "Return a complete semantic_frame_v1 synthetic bundle frame. Emit exactly "
                "the 17 fixed fields in order, one FIELD<TAB><JSON-value> per line, with "
                "no prose, fence, or BEGIN/END marker. Use unknown and empty arrays where "
                "evidence is absent; copy supplied ids exactly, never invent evidence or "
                "spans, and keep every concrete value evidence-backed. The following markers delimit a prompt-only shortest "
                "valid exemplar; copy its shape, not its ids:\n"
                + self._compact_frame_exemplar()
            )
            if self.config.base_url:
                call_kwargs: Dict[str, Any] = {
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": _canonical_json(compact_payload)},
                    ],
                    "max_tokens": int(max_output_tokens),
                }
                extra_body = self._extra_body()
                if extra_body is not None:
                    call_kwargs["extra_body"] = extra_body
                response = client.chat.completions.create(**call_kwargs)
            else:
                response = client.responses.create(
                    model=self.config.model,
                    instructions=instructions,
                    input=_canonical_json(compact_payload),
                    store=False,
                    max_output_tokens=int(max_output_tokens),
                )
            diagnostics.update(self._safe_response_metadata(response))
            try:
                response_text = self._response_text(response)
            except Exception as exc:
                diagnostics.update(
                    {
                        "response_text_extracted": False,
                        "output_text_length": 0,
                    }
                )
                return ProviderHealthResult(
                    False,
                    "blocked",
                    self.source,
                    self.model_version,
                    request_hash,
                    _usage_tokens(response)[0] or input_estimate,
                    _usage_tokens(response)[1],
                    int(max_input_tokens),
                    int(max_output_tokens),
                    round((clock_fn() - started) * 1000.0, 3),
                    _provider_error_code(exc),
                    self.config.public_dict(),
                    diagnostics,
                )
            diagnostics.update(
                {
                    "response_text_extracted": True,
                    "output_text_length": len(response_text),
                }
            )
            finish_reasons = diagnostics.get("finish_reasons", ())
            finish_length = isinstance(finish_reasons, (list, tuple)) and "length" in finish_reasons
            content_length = int(diagnostics.get("content_length") or 0)
            reasoning_length = int(diagnostics.get("reasoning_content_length") or 0)
            if content_length == 0 and reasoning_length > 0 and finish_length:
                response_shape = "reasoning_or_output_length_exhaustion_suspected"
            elif content_length == 0 and finish_length:
                response_shape = "empty_output_length_terminated"
            elif content_length == 0:
                response_shape = "protocol_empty_response"
            elif finish_length:
                response_shape = "nonempty_output_length_terminated"
            else:
                response_shape = "nonempty_output"
            diagnostics["response_shape_diagnosis"] = response_shape
            try:
                parse_bundle_frame(
                    response_text,
                    expected_schema_version=BUNDLE_SCHEMA_VERSION,
                    expected_message_ids=("semantic-frame-health-message",),
                    expected_chat_id="semantic-frame-health-chat",
                )
            except Exception as exc:
                parser_diagnostics = getattr(exc, "diagnostics", None)
                if isinstance(parser_diagnostics, Mapping):
                    for key in ("missing_field_names", "extra_field_names", "duplicate", "order_error"):
                        values = parser_diagnostics.get(key)
                        if isinstance(values, (list, tuple)):
                            diagnostics[key] = [str(value) for value in values if str(value) in BUNDLE_FIELDS]
                input_tokens, output_tokens = _usage_tokens(response)
                diagnostics["input_token_estimate"] = input_tokens or input_estimate
                diagnostics["output_token_estimate"] = output_tokens or _estimate_tokens({"length": len(response_text)})
                return ProviderHealthResult(
                    False,
                    "blocked",
                    self.source,
                    self.model_version,
                    request_hash,
                    input_tokens or input_estimate,
                    output_tokens,
                    int(max_input_tokens),
                    int(max_output_tokens),
                    round((clock_fn() - started) * 1000.0, 3),
                    _provider_error_code(exc),
                    self.config.public_dict(),
                    diagnostics,
                )
            input_tokens, output_tokens = _usage_tokens(response)
            input_tokens = input_tokens or input_estimate
            output_tokens = output_tokens or _estimate_tokens({"length": len(response_text)})
            diagnostics["input_token_estimate"] = input_tokens
            diagnostics["output_token_estimate"] = output_tokens
            if input_tokens > int(max_input_tokens):
                error_code = "input_token_limit_exceeded"
            elif output_tokens > int(max_output_tokens):
                error_code = "output_token_limit_exceeded"
            else:
                error_code = None
            return ProviderHealthResult(
                error_code is None,
                "available" if error_code is None else "blocked",
                self.source,
                self.model_version,
                request_hash,
                input_tokens,
                output_tokens,
                int(max_input_tokens),
                int(max_output_tokens),
                round((clock_fn() - started) * 1000.0, 3),
                error_code,
                self.config.public_dict(),
                diagnostics,
            )
        except Exception as exc:
            if response is not None:
                diagnostics.update(self._safe_response_metadata(response, response_text=response_text))
            return ProviderHealthResult(
                False,
                "blocked",
                self.source,
                self.model_version,
                request_hash,
                _usage_tokens(response)[0] if response is not None else input_estimate,
                _usage_tokens(response)[1] if response is not None else 0,
                int(max_input_tokens),
                int(max_output_tokens),
                round((clock_fn() - started) * 1000.0, 3),
                _provider_error_code(exc),
                self.config.public_dict(),
                diagnostics,
            )

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        client = self._get_client()
        schema_version = _string(request.get("schema_version"), BUNDLE_SCHEMA_VERSION)
        messages = request.get("messages", ())
        user_payload, input_chars = self._compact_payload(request)
        if input_chars > self.max_input_chars:
            # The compacting pass never drops message/chat ids.  Refuse a
            # pathological id-only payload instead of sending an input that
            # violates the per-call cap or silently changing bundle scope.
            raise BudgetExceeded("input_payload_limit_exceeded")
        fields = ",".join(BUNDLE_FIELDS)
        instructions = (
            "Emit exactly one semantic_frame_v1 response: one line per field in this "
            "fixed order, with FIELD<TAB><JSON-value> and no prose/fence. The exact "
            "fields are "
            + fields
            + ". Use unknown and empty arrays where evidence is absent. Every known entity, action, "
            "claim_type, state, modality, and non-insufficient relation must cite "
            "evidence_ids from supplied message ids. Evidence spans and ids must be "
            "local to the supplied messages; copy bundle/chat/message ids exactly and "
            "do not invent evidence. Do not cross chat boundaries; time alone cannot "
            "create a strong relation. BEGIN/END markers below are prompt-only; "
            "do not emit them:\n"
            + self._compact_frame_exemplar()
        )
        if self.config.base_url:
            call_kwargs = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": _canonical_json(user_payload)},
                ],
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            }
            extra_body = self._extra_body()
            if extra_body is not None:
                call_kwargs["extra_body"] = extra_body
            response = client.chat.completions.create(**call_kwargs)
        else:
            response = client.responses.create(
                model=self.config.model,
                instructions=instructions,
                input=_canonical_json(user_payload),
                store=False,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            )
        text = self._response_text(response)
        expected_ids = tuple(
            str(item.get("message_id"))
            for item in messages
            if isinstance(item, Mapping) and item.get("message_id") is not None
        )
        try:
            parsed = parse_bundle_frame(
                text,
                expected_schema_version=schema_version,
                expected_message_ids=expected_ids,
                expected_chat_id=_string(request.get("chat_id"), UNKNOWN),
            )
        except SemanticFrameParseError as exc:
            # Attach only response metadata and fixed parser taxonomy to the
            # exception.  The response text itself is never retained.
            metadata = self._safe_response_metadata(response, response_text=text)
            finish_reasons = metadata.get("finish_reasons", ())
            finish_length = isinstance(finish_reasons, (list, tuple)) and "length" in finish_reasons
            content_length = int(metadata.get("content_length") or 0)
            reasoning_length = int(metadata.get("reasoning_content_length") or 0)
            if content_length == 0 and reasoning_length > 0 and finish_length:
                metadata["response_shape_diagnosis"] = "reasoning_or_output_length_exhaustion_suspected"
            elif content_length == 0 and finish_length:
                metadata["response_shape_diagnosis"] = "empty_output_length_terminated"
            elif content_length == 0:
                metadata["response_shape_diagnosis"] = "protocol_empty_response"
            elif finish_length:
                metadata["response_shape_diagnosis"] = "nonempty_output_length_terminated"
            else:
                metadata["response_shape_diagnosis"] = "nonempty_output"
            metadata.update(exc.diagnostics)
            if exc.validation_categories:
                metadata["validation_categories"] = list(exc.validation_categories)
            exc.provider_metadata = metadata
            raise
        input_tokens, output_tokens = _usage_tokens(response)
        result = dict(parsed)
        result["usage"] = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        }
        return result


def run_provider_health_check(
    config: Optional[AIProviderConfig] = None,
    *,
    model: Optional[Any] = None,
    max_input_tokens: int = 500,
    max_output_tokens: int = 100,
    clock: Optional[Callable[[], float]] = None,
) -> ProviderHealthResult:
    """Run a bounded synthetic health probe through a configurable adapter."""

    resolved_config = config or AIProviderConfig.from_environment()
    adapter = model if model is not None else OpenAIBundleModel(resolved_config)
    health = getattr(adapter, "health_check", None)
    request = {
        "health_check": "synthetic",
        "purpose": "provider_availability",
        "response": {"ok": "boolean"},
    }
    request_hash = stable_hash(request)
    if not callable(health):
        return ProviderHealthResult(
            False,
            "blocked",
            _string(getattr(adapter, "source", resolved_config.provider), resolved_config.provider),
            _string(getattr(adapter, "model_version", resolved_config.model), resolved_config.model),
            request_hash,
            _estimate_tokens(request),
            0,
            int(max_input_tokens),
            int(max_output_tokens),
            "N/A",
            "health_interface_missing",
            resolved_config.public_dict(),
        )
    try:
        result = health(
            max_input_tokens=int(max_input_tokens),
            max_output_tokens=int(max_output_tokens),
            clock=clock,
        )
        if isinstance(result, ProviderHealthResult):
            return result
        if isinstance(result, Mapping):
            return ProviderHealthResult(
                bool(result.get("ok")),
                _string(result.get("status"), "available" if result.get("ok") else "blocked"),
                _string(result.get("source"), resolved_config.provider),
                _string(result.get("model"), resolved_config.model),
                _string(result.get("request_sha256"), request_hash),
                int(result.get("input_tokens") or 0),
                int(result.get("output_tokens") or 0),
                int(result.get("max_input_tokens") or max_input_tokens),
                int(result.get("max_output_tokens") or max_output_tokens),
                result.get("latency_ms", "N/A"),
                result.get("error_code"),
                resolved_config.public_dict(),
            )
        return ProviderHealthResult(
            False,
            "blocked",
            resolved_config.provider,
            resolved_config.model,
            request_hash,
            _estimate_tokens(request),
            0,
            int(max_input_tokens),
            int(max_output_tokens),
            "N/A",
            "health_result_invalid",
            resolved_config.public_dict(),
        )
    except Exception as exc:
        return ProviderHealthResult(
            False,
            "blocked",
            _string(getattr(adapter, "source", resolved_config.provider), resolved_config.provider),
            _string(getattr(adapter, "model_version", resolved_config.model), resolved_config.model),
            request_hash,
            _estimate_tokens(request),
            0,
            int(max_input_tokens),
            int(max_output_tokens),
            "N/A",
            type(exc).__name__.casefold() or "provider_error",
            resolved_config.public_dict(),
        )


# Descriptive aliases keep this boundary easy to discover for callers.
AIProviderAdapter = OpenAIBundleModel
ExistingAIProviderAdapter = OpenAIBundleModel


@dataclass(frozen=True)
class BudgetReservation:
    request_id: str
    bundle_id: str
    attempt: int
    request_sha256: str
    input_tokens_estimate: int
    max_output_tokens: int


class BudgetManager:
    """Deterministic global budget with an inspectable request ledger."""

    def __init__(
        self,
        *,
        max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if int(max_bundle_calls) < 0 or int(max_input_tokens) < 1 or int(max_output_tokens) < 1:
            raise ValueError("budget limits must be non-negative/positive")
        self.max_bundle_calls = int(max_bundle_calls)
        self.max_input_tokens = int(max_input_tokens)
        self.max_output_tokens = int(max_output_tokens)
        self.clock = clock or time.perf_counter
        self.calls_used = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.retry_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.latency_ms_total = 0.0
        self.records: List[Dict[str, Any]] = []
        self.rejections: List[Dict[str, Any]] = []
        self.last_rejection: Optional[Dict[str, Any]] = None

    def reserve(
        self,
        request: Mapping[str, Any],
        *,
        bundle_id: str,
        attempt: int = 0,
        source: str = "llm",
        model: str = UNKNOWN,
        version: str = PIPELINE_VERSION,
    ) -> Optional[BudgetReservation]:
        request_hash = stable_hash(request)
        input_estimate = _estimate_tokens(request)
        reason: Optional[str] = None
        if input_estimate > self.max_input_tokens:
            reason = "input_token_limit_exceeded"
        elif self.calls_used >= self.max_bundle_calls:
            reason = "bundle_call_budget_exhausted"
        if reason:
            rejection = {
                "request_id": "REQUEST_%06d" % (len(self.records) + len(self.rejections) + 1),
                "bundle_id": _string(bundle_id),
                "attempt": int(attempt),
                "request_sha256": request_hash,
                "input_tokens_estimate": input_estimate,
                "max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens,
                "status": "pending",
                "error_code": reason,
                "source": source,
                "model": model,
                "version": version,
            }
            self.rejections.append(rejection)
            self.last_rejection = rejection
            return None
        self.last_rejection = None
        self.calls_used += 1
        request_id = "LLM_CALL_%06d" % self.calls_used
        record = {
            "request_id": request_id,
            "bundle_id": _string(bundle_id),
            "attempt": int(attempt),
            "request_sha256": request_hash,
            "input_tokens_estimate": input_estimate,
            "input_tokens": 0,
            "output_tokens": 0,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "status": "started",
            "error_code": None,
            "latency_ms": 0.0,
            "cache_hit": False,
            "retry": bool(attempt > 0),
            "retry_index": int(attempt),
            "source": source,
            "model": model,
            "version": version,
        }
        self.records.append(record)
        return BudgetReservation(
            request_id=request_id,
            bundle_id=_string(bundle_id),
            attempt=int(attempt),
            request_sha256=request_hash,
            input_tokens_estimate=input_estimate,
            max_output_tokens=self.max_output_tokens,
        )

    def complete(
        self,
        reservation: BudgetReservation,
        *,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> None:
        record = self._record(reservation.request_id)
        record["input_tokens"] = int(max(0, input_tokens))
        record["output_tokens"] = int(max(0, output_tokens))
        record["latency_ms"] = round(max(0.0, float(latency_ms)), 3)
        self.input_tokens += record["input_tokens"]
        self.output_tokens += record["output_tokens"]
        self.latency_ms_total += record["latency_ms"]
        if record["input_tokens"] > self.max_input_tokens:
            record["status"] = "pending"
            record["error_code"] = "input_token_limit_exceeded"
            raise BudgetExceeded("input_token_limit_exceeded")
        if record["output_tokens"] > self.max_output_tokens:
            record["status"] = "pending"
            record["error_code"] = "output_token_limit_exceeded"
            raise BudgetExceeded("output_token_limit_exceeded")
        record["status"] = "complete"

    def fail(
        self,
        reservation: BudgetReservation,
        *,
        error_code: str,
        latency_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> None:
        record = self._record(reservation.request_id)
        record["input_tokens"] = int(max(0, input_tokens))
        record["output_tokens"] = int(max(0, output_tokens))
        record["latency_ms"] = round(max(0.0, float(latency_ms)), 3)
        record["status"] = "failed"
        record["error_code"] = _string(error_code, "provider_error")
        if isinstance(diagnostics, Mapping):
            safe: Dict[str, Any] = {}
            for key in ("missing_field_names", "extra_field_names", "duplicate", "order_error"):
                values = diagnostics.get(key)
                if isinstance(values, (list, tuple)):
                    safe[key] = [str(value) for value in values if str(value) in BUNDLE_FIELDS]
            categories = diagnostics.get("validation_categories")
            if isinstance(categories, (list, tuple)):
                safe["validation_categories"] = [
                    str(value)
                    for value in categories
                    if str(value)
                    in {
                        "evidence_boundary",
                        "evidence_reference",
                        "schema_enum_or_type",
                        "cross_chat_or_relation",
                        "conflict",
                        "other_validation",
                    }
                ]
            for key in (
                "response_fields",
                "content_length",
                "reasoning_content_length",
                "finish_reasons",
                "usage_keys",
                "http_status",
                "response_sha256",
                "response_shape_diagnosis",
                "raw_content_saved",
                "raw_reasoning_saved",
            ):
                value = diagnostics.get(key)
                if key in {"response_fields", "usage_keys"} and isinstance(value, (list, tuple)):
                    safe[key] = [str(item) for item in value]
                elif key == "finish_reasons" and isinstance(value, (list, tuple)):
                    safe[key] = [
                        str(item)
                        for item in value
                        if str(item) in {"stop", "length", "content_filter", "tool_calls", "unknown"}
                    ]
                elif key in {"content_length", "reasoning_content_length", "http_status"} and isinstance(value, (int, float, str)):
                    safe[key] = value
                elif key == "response_shape_diagnosis" and isinstance(value, str):
                    safe[key] = value
                elif key in {"response_sha256", "raw_content_saved", "raw_reasoning_saved"} and isinstance(value, (str, bool)):
                    safe[key] = value
            if safe:
                record["diagnostics"] = safe
        self.input_tokens += record["input_tokens"]
        self.output_tokens += record["output_tokens"]
        self.latency_ms_total += record["latency_ms"]

    def _record(self, request_id: str) -> Dict[str, Any]:
        for record in reversed(self.records):
            if record["request_id"] == request_id:
                return record
        raise KeyError(request_id)

    def mark_retry(self) -> None:
        self.retry_count += 1

    def mark_cache(self, hit: bool) -> None:
        if hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "max_bundle_calls": self.max_bundle_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "calls_used": self.calls_used,
            "calls_remaining": max(0, self.max_bundle_calls - self.calls_used),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retry_count": self.retry_count,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "latency_ms_total": round(self.latency_ms_total, 3),
            "rejection_count": len(self.rejections),
            "requests_hash": stable_hash(self.records + self.rejections),
        }


class _BudgetedModel:
    """One-call adapter used by ``BundleSemanticEncoder`` (retries are C-owned)."""

    def __init__(
        self,
        model: Any,
        budget: BudgetManager,
        *,
        source: str,
        model_version: str,
        attempt: int = 0,
    ) -> None:
        self.model = model
        self.budget = budget
        self.source = source
        self.model_version = model_version
        self.attempt = int(attempt)

    def cache_context(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Expose request-local cache material without weakening the budget wrapper.

        Wire-aware models use this hook to bind their request-local symbol table
        into the cache/hash.  Older models do not need it, so the wrapper keeps
        the optional interface and returns an empty context when unavailable.
        The result is metadata only; it is never sent to the provider by this
        adapter.
        """
        method = getattr(self.model, "cache_context", None)
        if not callable(method):
            return {}
        context = method(request)
        return dict(context) if isinstance(context, Mapping) else {}

    def encode_bundle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        reservation = self.budget.reserve(
            request,
            bundle_id=_string(request.get("bundle_id")),
            attempt=self.attempt,
            source=self.source,
            model=self.model_version,
            version=PIPELINE_VERSION,
        )
        if reservation is None:
            reason = self.budget.last_rejection or {}
            raise BudgetExceeded(_string(reason.get("error_code"), "budget_exhausted"))
        started = self.budget.clock()
        try:
            method = getattr(self.model, "encode_bundle", None) or getattr(self.model, "encode", None)
            if not callable(method):
                raise ProviderUnavailable("model_interface_missing")
            raw = method(request)
            input_tokens, output_tokens = _usage_tokens(raw)
            input_tokens = input_tokens or _estimate_tokens(request)
            output_tokens = output_tokens or _estimate_tokens(raw)
            self.budget.complete(
                reservation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=(self.budget.clock() - started) * 1000.0,
            )
            return raw
        except BudgetExceeded:
            # ``complete`` already marked the record with a bounded-output
            # reason.  Keep it pending and do not hide the budget decision.
            raise
        except Exception as exc:
            input_tokens, output_tokens = _usage_tokens(locals().get("raw"))
            provider_metadata = getattr(exc, "provider_metadata", None)
            if isinstance(provider_metadata, Mapping):
                input_tokens = int(provider_metadata.get("input_tokens") or input_tokens or 0)
                output_tokens = int(provider_metadata.get("output_tokens") or output_tokens or 0)
            attempt_diagnostics: Dict[str, Any] = dict(provider_metadata) if isinstance(provider_metadata, Mapping) else {}
            parser_diagnostics = getattr(exc, "diagnostics", None)
            if isinstance(parser_diagnostics, Mapping):
                attempt_diagnostics.update(parser_diagnostics)
            categories = getattr(exc, "validation_categories", None)
            if isinstance(categories, (list, tuple)):
                attempt_diagnostics["validation_categories"] = list(categories)
            self.budget.fail(
                reservation,
                error_code=_provider_error_code(exc),
                latency_ms=(self.budget.clock() - started) * 1000.0,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                diagnostics=attempt_diagnostics,
            )
            raise


def _semantic_request(messages: Sequence[Mapping[str, Any]], bundle_id: str, chat_id: str) -> Dict[str, Any]:
    """Mirror the fixed encoder request for preflight/pending hashes."""

    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "prompt_version": BUNDLE_PROMPT_VERSION,
        "ruleset_version": BUNDLE_RULESET_VERSION,
        "bundle_id": _string(bundle_id),
        "chat_id": _string(chat_id),
        "messages": [_jsonable(item) for item in messages],
        "response_schema": bundle_schema(BUNDLE_SCHEMA_VERSION),
    }


def _pending_outcome(
    messages: Sequence[Mapping[str, Any]],
    *,
    bundle_id: str,
    chat_id: str,
    source: str,
    code: str,
) -> EncodingOutcome:
    ids = [_string(item.get("message_id")) for item in messages]
    request_hash = stable_hash(_semantic_request(messages, bundle_id, chat_id))
    bundle = empty_bundle(
        bundle_id,
        ids,
        chat_id=chat_id,
        status="pending",
        source=source,
        uncertainties=({"code": code, "field": UNKNOWN, "severity": "high"},),
        schema_version=BUNDLE_SCHEMA_VERSION,
    )
    bundle["metadata"]["input_sha256"] = request_hash
    report = validate_bundle(bundle, message_ids=ids, chat_id=chat_id)
    return EncodingOutcome(
        bundle=bundle,
        status="pending",
        input_sha256=request_hash,
        cache_key="pending:%s" % request_hash,
        validation=report,
        stats={"source": source, "error_code": code},
    )


@dataclass(frozen=True)
class SemanticDecision:
    """One body-free semantic decision paired with its dialogue bundle."""

    bundle_id: str
    scale: str
    chat_id: str
    message_ids: Tuple[str, ...]
    status: str
    source: str
    input_sha256: str
    cache_key: str
    validation: Mapping[str, Any]
    semantic_bundle: Mapping[str, Any]
    information_value: str = UNKNOWN
    event_completeness: str = UNKNOWN
    channel: str = UNKNOWN
    selection_rank: int = 0
    selection_score: Tuple[int, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "scale": self.scale,
            "chat_id": self.chat_id,
            "message_ids": list(self.message_ids),
            "status": self.status,
            "source": self.source,
            "input_sha256": self.input_sha256,
            "cache_key": self.cache_key,
            "validation": _jsonable(self.validation),
            "semantic_bundle": _body_free(self.semantic_bundle),
            "information_value": self.information_value,
            "event_completeness": self.event_completeness,
            "channel": self.channel,
            "selection_rank": self.selection_rank,
            "selection_score": list(self.selection_score),
            "decision_hash": stable_hash(
                {
                    "bundle_id": self.bundle_id,
                    "status": self.status,
                    "input_sha256": self.input_sha256,
                    "semantic_bundle": _body_free(self.semantic_bundle),
                }
            ),
        }


@dataclass(frozen=True)
class PipelineRunResult:
    """Complete in-memory shadow result; writer code lives in the runner."""

    mode: str
    input_sha256: str
    registry_snapshot: Mapping[str, Any]
    gate_snapshot: Mapping[str, Any]
    dialogue_snapshot: Mapping[str, Any]
    bundles: Tuple[Mapping[str, Any], ...]
    decisions: Tuple[Mapping[str, Any], ...]
    relations: Tuple[Mapping[str, Any], ...]
    requests: Tuple[Mapping[str, Any], ...]
    errors: Tuple[Mapping[str, Any], ...]
    cost: Mapping[str, Any]
    snapshots: Mapping[str, Any]
    manifest: Mapping[str, Any]
    # The DTO view is retained only in memory so a later shadow stage can
    # consume the exact fragments/claims/relations that produced the public
    # bundle projections.  ``artifacts()`` intentionally omits this field;
    # it may contain redacted fragment text and is never part of the
    # body-free manifest or runner files.
    dialogue_result: Optional[DialogueBundleResult] = field(default=None, repr=False, compare=False)

    def artifacts(self) -> Dict[str, Any]:
        """Return names used by the runner's replayable artifact files."""

        return {
            "registry": dict(self.registry_snapshot),
            "gate": dict(self.gate_snapshot),
            "bundles": list(self.bundles),
            "snapshots": dict(self.snapshots),
            "decisions": list(self.decisions),
            "relations": list(self.relations),
            "requests": list(self.requests),
            "cost": dict(self.cost),
            "errors": list(self.errors),
            "manifest": dict(self.manifest),
        }


class ContextualBundlePipeline:
    """Connect the four shadow stages with strict scope and budget checks."""

    def __init__(
        self,
        *,
        mode: str = "disabled",
        model: Optional[Any] = None,
        embedder: Optional[Any] = None,
        provider_config: Optional[AIProviderConfig] = None,
        cache: Optional[VersionedBundleCache] = None,
        max_bundle_calls: int = DEFAULT_MAX_LLM_BUNDLE_CALLS,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_retries: int = 1,
        window_size: int = 8,
        time_window_seconds: float = 15 * 60,
        max_candidates: int = 3,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        normalized_mode = _string(mode, "disabled").casefold()
        if normalized_mode not in SUPPORTED_MODES:
            raise PipelineConfigurationError("mode must be one of %s" % ", ".join(sorted(SUPPORTED_MODES)))
        self.mode = normalized_mode
        self.max_retries = max(0, int(max_retries))
        self.window_size = int(window_size)
        self.time_window_seconds = float(time_window_seconds)
        self.max_candidates = int(max_candidates)
        self.provider_config = provider_config or AIProviderConfig.from_environment()
        self.model_version = _string(
            getattr(model, "model_version", None)
            or getattr(model, "version", None)
            or self.provider_config.model,
            UNKNOWN,
        )
        self.embedder = embedder
        self.cache = cache if cache is not None else VersionedBundleCache()
        self.clock = clock or time.perf_counter
        self.budget = BudgetManager(
            max_bundle_calls=max_bundle_calls,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            clock=self.clock,
        )
        self._budget_limits = {
            "max_bundle_calls": int(max_bundle_calls),
            "max_input_tokens": int(max_input_tokens),
            "max_output_tokens": int(max_output_tokens),
        }
        self.errors: List[Dict[str, Any]] = []
        self.provider_blocked = False
        self.provider_blocked_reason: Optional[str] = None
        self.provider_model: Optional[Any] = None
        self.provider_source = "disabled"
        if self.mode == "fake":
            if model is None:
                self.provider_blocked = True
                self.provider_blocked_reason = "provider_not_injected"
                self._error("provider_not_injected", source="fake")
            else:
                self.provider_model = model
                self.provider_source = "fake"
        elif self.mode == "real":
            candidate = model if model is not None else OpenAIBundleModel(self.provider_config)
            if model is None and not self.provider_config.configured:
                self.provider_blocked = True
                self.provider_blocked_reason = "provider_not_configured"
                self._error("provider_not_configured", source="openai")
            else:
                self.provider_model = candidate
                self.provider_source = "openai"

    def _error(self, code: str, *, source: str = PIPELINE_VERSION, bundle_id: Optional[str] = None) -> None:
        item: Dict[str, Any] = {
            "code": _string(code),
            "source": source,
            "severity": "high" if code in {"provider_not_configured", "provider_sdk_unavailable"} else "medium",
            "pipeline_version": PIPELINE_VERSION,
        }
        if bundle_id is not None:
            item["bundle_id"] = _string(bundle_id)
        item["error_hash"] = stable_hash(item)
        self.errors.append(item)

    @staticmethod
    def _selection_score(bundle: DialogueBundle) -> Tuple[int, ...]:
        scale_score = {
            SCALE_SESSION: 5,
            SCALE_LOCAL: 4,
            SCALE_TURN: 3,
            SCALE_MICRO: 2,
            SCALE_COLD: 1,
        }.get(bundle.scale, 0)
        channel_score = {"immediate": 3, "pending_context": 2, "cold_recoverable": 1, "background": 0}.get(bundle.channel, 0)
        closed_score = 1 if bundle.closed else 0
        info_score = {"high": 3, "medium": 2, "low": 1}.get(bundle.information_value, 0)
        completeness_score = {"complete": 3, "partial": 2, "incomplete": 1}.get(bundle.event_completeness, 0)
        return (scale_score, channel_score, closed_score, info_score, completeness_score, len(bundle.source_message_ids))

    @classmethod
    def _ordered_bundles(cls, bundles: Iterable[DialogueBundle]) -> List[DialogueBundle]:
        # Sort by structural value only.  No message-body terms influence
        # selection, making a replay independent from lexical model behavior.
        return sorted(
            tuple(bundles),
            key=lambda item: (cls._selection_score(item), item.bundle_id),
            reverse=True,
        )

    def _bundle_inputs(
        self,
        bundle: DialogueBundle,
        registry: MessageRegistry,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        entries: List[RegisteredMessage] = []
        for message_id in bundle.source_message_ids:
            entry = registry.maybe_get(message_id)
            if entry is None:
                return [], "message_not_registered"
            entries.append(entry)
        if not entries:
            return [], "bundle_has_no_messages"
        chat_ids = {_string(entry.chat_id) for entry in entries if _known(entry.chat_id)}
        if len(chat_ids) > 1:
            return [], "cross_chat_bundle_forbidden"
        bundle_chat = _string(bundle.chat_id)
        if _known(bundle_chat) and chat_ids and bundle_chat not in chat_ids:
            return [], "bundle_chat_mismatch"
        return [_safe_public_message(entry) for entry in entries], None

    def _encode_one(
        self,
        semantic: BundleSemanticPipeline,
        bundle: DialogueBundle,
        messages: Sequence[Mapping[str, Any]],
        *,
        attempt: int,
    ) -> EncodingOutcome:
        if self.provider_model is None:
            return semantic.encode_bundle(messages, bundle_id=bundle.bundle_id, chat_id=bundle.chat_id)
        # C owns retries so that every retry consumes an inspectable budget
        # reservation and a provider response is never silently replaced.
        semantic.encoder.model = _BudgetedModel(
            self.provider_model,
            self.budget,
            source=self.provider_source,
            model_version=self.model_version,
            attempt=attempt,
        )
        before_calls = self.budget.calls_used
        outcome = semantic.encode_bundle(messages, bundle_id=bundle.bundle_id, chat_id=bundle.chat_id)
        after_calls = self.budget.calls_used
        if after_calls == before_calls and self.budget.last_rejection is not None:
            # Keep a single explicit pending result when a preflight budget
            # check rejected the call; no retry can make the package smaller.
            return _pending_outcome(
                messages,
                bundle_id=bundle.bundle_id,
                chat_id=bundle.chat_id,
                source="budget",
                code=_string(self.budget.last_rejection.get("error_code"), "budget_exhausted"),
            )
        return outcome

    def run(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        fragments: Optional[Iterable[Any]] = None,
        claims: Optional[Iterable[Any]] = None,
        split: Optional[str] = None,
    ) -> PipelineRunResult:
        """Run the shadow path over supplied public mappings.

        The input objects are registered through the existing whitelist.  A
        caller may provide redacted text under ``content``/``redacted_text``;
        all later result projections are body-free.
        """

        # A pipeline instance may be replayed with a retained versioned cache.
        # Budget and error ledgers are per-run, otherwise a harmless replay
        # would inherit the previous run's call count and appear exhausted.
        self.budget = BudgetManager(**self._budget_limits, clock=self.clock)
        self.errors = []
        if self.provider_blocked and self.provider_blocked_reason:
            self._error(self.provider_blocked_reason, source=self.provider_source)
        values = tuple(messages or ())
        registry = MessageRegistry()
        try:
            entries = registry.register_many(values)
        except Exception as exc:
            self._error(type(exc).__name__.casefold() or "registration_failed", source="registry")
            raise
        if split is not None and _string(split) in {"frozen", "frozen_test"}:
            raise PipelineConfigurationError("frozen inputs are not accepted")
        for entry in entries:
            if entry.metadata.split in {"frozen", "frozen_test"}:
                raise PipelineConfigurationError("frozen inputs are not accepted")

        gate = SemanticGate(registry)
        for entry in entries:
            try:
                gate.route(entry)
            except Exception as exc:
                self._error(type(exc).__name__.casefold() or "gate_failed", source="gate", bundle_id=entry.message_id)
                raise
        builder = DialogueBundleBuilder(
            registry=registry,
            gate=gate,
            window_size=self.window_size,
            time_window_seconds=self.time_window_seconds,
            max_candidates=self.max_candidates,
        )
        dialogue_result: DialogueBundleResult = builder.ingest(entries, fragments=fragments, claims=claims)
        ordered = self._ordered_bundles(dialogue_result.bundles)
        semantic_model = None
        if self.provider_model is not None:
            # One-call adapter is installed per attempt below.
            semantic_model = self.provider_model
        semantic = BundleSemanticPipeline(
            model=semantic_model if semantic_model is not None else None,
            embedder=self.embedder,
            cache=self.cache,
            schema_version=BUNDLE_SCHEMA_VERSION,
            model_version=self.model_version,
            prompt_version=BUNDLE_PROMPT_VERSION,
            ruleset_version=BUNDLE_RULESET_VERSION,
            max_retries=0,
            clock=self.clock,
        )
        decisions: List[SemanticDecision] = []
        request_audits: List[Dict[str, Any]] = []
        selected = 0
        for position, bundle in enumerate(ordered):
            inputs, reason = self._bundle_inputs(bundle, registry)
            if reason:
                self._error(reason, source="bundle_scope", bundle_id=bundle.bundle_id)
                outcome = _pending_outcome(
                    inputs,
                    bundle_id=bundle.bundle_id,
                    chat_id=bundle.chat_id,
                    source="scope",
                    code=reason,
                ) if inputs else _pending_outcome(
                    [{"message_id": item, "chat_id": bundle.chat_id} for item in bundle.source_message_ids],
                    bundle_id=bundle.bundle_id,
                    chat_id=bundle.chat_id,
                    source="scope",
                    code=reason,
                )
                request_audits.append(
                    {
                        "request_id": "SEMANTIC_REQUEST_%s" % outcome.input_sha256[:20],
                        "bundle_id": bundle.bundle_id,
                        "attempt": 0,
                        "retry": False,
                        "retry_index": 0,
                        "request_sha256": outcome.input_sha256,
                        "input_tokens_estimate": _estimate_tokens(_semantic_request(inputs, bundle.bundle_id, bundle.chat_id)),
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "max_input_tokens": self.budget.max_input_tokens,
                        "max_output_tokens": self.budget.max_output_tokens,
                        "status": "pending",
                        "cache_hit": False,
                        "cache_miss": False,
                        "latency_ms": "N/A",
                        "source": "scope",
                        "model": self.model_version,
                        "version": PIPELINE_VERSION,
                    }
                )
            else:
                outcome: EncodingOutcome
                before_stats = semantic.stats.snapshot()
                attempt_used = 0
                if self.provider_model is None:
                    outcome = self._encode_one(semantic, bundle, inputs, attempt=0)
                else:
                    outcome = self._encode_one(semantic, bundle, inputs, attempt=0)
                    # Retry only provider/schema failures.  Budget rejection
                    # has no safe recovery path and is already pending.
                    for attempt in range(1, self.max_retries + 1):
                        last_call = self.budget.records[-1] if self.budget.records else {}
                        if outcome.status != "pending" or last_call.get("status") != "failed":
                            break
                        self.budget.mark_retry()
                        attempt_used = attempt
                        outcome = self._encode_one(semantic, bundle, inputs, attempt=attempt)
                after_stats = semantic.stats.snapshot()
                cache_hit = int(after_stats.get("cache_hits", 0)) > int(before_stats.get("cache_hits", 0))
                cache_miss = int(after_stats.get("cache_misses", 0)) > int(before_stats.get("cache_misses", 0))
                self.budget.mark_cache(cache_hit)
                request_audits.append(
                    {
                        "request_id": "SEMANTIC_REQUEST_%s" % outcome.input_sha256[:20],
                        "bundle_id": bundle.bundle_id,
                        "attempt": attempt_used,
                        "retry": bool(attempt_used > 0),
                        "retry_index": attempt_used,
                        "request_sha256": outcome.input_sha256,
                        "input_tokens_estimate": _estimate_tokens(_semantic_request(inputs, bundle.bundle_id, bundle.chat_id)),
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "max_input_tokens": self.budget.max_input_tokens,
                        "max_output_tokens": self.budget.max_output_tokens,
                        "status": outcome.status,
                        "cache_hit": cache_hit,
                        "cache_miss": cache_miss,
                        "latency_ms": "N/A",
                        "source": _string(outcome.bundle.get("metadata", {}).get("source"), "unknown"),
                        "model": self.model_version,
                        "version": PIPELINE_VERSION,
                    }
                )
                if outcome.status == "pending" and self.provider_model is not None:
                    code = "model_pending"
                    if self.budget.last_rejection:
                        code = _string(self.budget.last_rejection.get("error_code"), code)
                    elif self.budget.records:
                        code = _string(self.budget.records[-1].get("error_code"), code)
                    self._error(code, source=self.provider_source, bundle_id=bundle.bundle_id)
            selected += 1
            decisions.append(
                SemanticDecision(
                    bundle_id=bundle.bundle_id,
                    scale=bundle.scale,
                    chat_id=bundle.chat_id,
                    message_ids=tuple(bundle.source_message_ids),
                    status=outcome.status,
                    source=_string(outcome.bundle.get("metadata", {}).get("source"), "unknown"),
                    input_sha256=outcome.input_sha256,
                    cache_key=outcome.cache_key,
                    validation=outcome.validation.to_dict(),
                    semantic_bundle=outcome.bundle,
                    information_value=bundle.information_value,
                    event_completeness=bundle.event_completeness,
                    channel=bundle.channel,
                    selection_rank=position + 1,
                    selection_score=self._selection_score(bundle),
                )
            )

        # ``ordered`` is fully processed in disabled mode; in model modes the
        # budget adapter turns later calls into pending without fabricating a
        # result.  This preserves one decision per candidate bundle.
        registry_snapshot = registry.snapshot().to_dict()
        gate_snapshot = gate.snapshot().to_dict()
        dialogue_snapshot = {
            "input_hash": dialogue_result.input_hash,
            "cache_key": dialogue_result.cache_key,
            "bundle_count": len(dialogue_result.bundles),
            "fragment_count": len(dialogue_result.fragments),
            "claim_count": len(dialogue_result.claims),
            "relation_count": len(dialogue_result.relations),
            "open_context_snapshot_count": len(dialogue_result.open_context_snapshots),
            "forced_snapshot": dialogue_result.forced_snapshot,
            "snapshot_reason": dialogue_result.snapshot_reason,
            "schema_version": dialogue_result.schema_version,
            "context_schema_version": dialogue_result.context_schema_version,
            "pipeline_version": dialogue_result.pipeline_version,
            "ruleset_version": dialogue_result.ruleset_version,
        }
        bundles_projection = tuple(_dialogue_bundle_projection(item) for item in ordered)
        decision_projection = tuple(item.to_dict() for item in decisions)
        relations_projection = tuple(_dialogue_relation_projection(item) for item in dialogue_result.relations)
        open_context_snapshots = tuple(item.to_dict() for item in dialogue_result.open_context_snapshots)
        semantic_stats = semantic.stats.snapshot()
        cost = {
            "mode": self.mode,
            "provider": self.provider_source,
            "provider_configured": bool(self.provider_model is not None),
            "model": self.model_version,
            "pipeline_version": PIPELINE_VERSION,
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "prompt_version": BUNDLE_PROMPT_VERSION,
            "ruleset_version": BUNDLE_RULESET_VERSION,
            "candidate_bundle_count": len(ordered),
            "decision_count": len(decisions),
            "complete_count": sum(item.status == "complete" for item in decisions),
            "fallback_count": sum(item.status == "fallback" for item in decisions),
            "pending_count": sum(item.status == "pending" for item in decisions),
            "semantic_stats": semantic_stats,
            "budget": self.budget.snapshot(),
            "selected_count": selected,
        }
        if self.provider_blocked:
            cost["blocked"] = True
        input_hash = stable_hash(
            {
                "registry_input_hash": registry_snapshot["input_hash"],
                "dialogue_input_hash": dialogue_result.input_hash,
                "mode": self.mode,
            }
        )
        snapshots = {
            "registry": registry_snapshot,
            "gate": gate_snapshot,
            "dialogue": dialogue_snapshot,
            "open_context_snapshots": open_context_snapshots,
            "input_hash": input_hash,
            "snapshot_hash": stable_hash({"registry": registry_snapshot, "gate": gate_snapshot, "dialogue": dialogue_snapshot}),
        }
        manifest = {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "ruleset_version": PIPELINE_RULESET_VERSION,
            "mode": self.mode,
            "split": _string(split, "development"),
            "input_sha256": input_hash,
            "registry_input_sha256": registry_snapshot["input_hash"],
            "dialogue_input_sha256": dialogue_result.input_hash,
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_prompt_version": BUNDLE_PROMPT_VERSION,
            "bundle_ruleset_version": BUNDLE_RULESET_VERSION,
            "dialogue_pipeline_version": BUNDLE_PIPELINE_VERSION,
            "dialogue_ruleset_version": DIALOGUE_RULESET_VERSION,
            "model": self.model_version,
            "provider": self.provider_source,
            "provider_configured": bool(self.provider_model is not None),
            "budget_limits": self.budget.snapshot(),
            "max_retries": self.max_retries,
            "window_size": self.window_size,
            "time_window_seconds": self.time_window_seconds,
            "max_candidates": self.max_candidates,
            "frozen_read": False,
            "gold_loaded": False,
            "body_free_outputs": True,
            "candidate_bundle_count": len(ordered),
            "decision_count": len(decisions),
            "error_count": len(self.errors),
            "replay_key": stable_hash(
                {
                    "pipeline_version": PIPELINE_VERSION,
                    "input_sha256": input_hash,
                    "mode": self.mode,
                    "model": self.model_version,
                }
            ),
        }
        return PipelineRunResult(
            mode=self.mode,
            input_sha256=input_hash,
            registry_snapshot=registry_snapshot,
            gate_snapshot=gate_snapshot,
            dialogue_snapshot=dialogue_snapshot,
            bundles=bundles_projection,
            decisions=decision_projection,
            relations=relations_projection,
            requests=tuple(self.budget.records + self.budget.rejections + request_audits),
            errors=tuple(self.errors),
            cost=cost,
            snapshots=snapshots,
            manifest=manifest,
            dialogue_result=dialogue_result,
        )

    # ``process`` is a descriptive alias for adapters that use pipeline
    # terminology rather than runner terminology.
    process = run


def run_contextual_bundle_pipeline(
    messages: Iterable[Mapping[str, Any]],
    **kwargs: Any,
) -> PipelineRunResult:
    """One-shot convenience wrapper for synthetic/offline callers."""

    pipeline = ContextualBundlePipeline(**{key: value for key, value in kwargs.items() if key not in {"fragments", "claims", "split"}})
    return pipeline.run(
        messages,
        fragments=kwargs.get("fragments"),
        claims=kwargs.get("claims"),
        split=kwargs.get("split"),
    )


__all__ = [
    "PIPELINE_SCHEMA_VERSION",
    "PIPELINE_VERSION",
    "PIPELINE_RULESET_VERSION",
    "SUPPORTED_MODES",
    "DEFAULT_MAX_LLM_BUNDLE_CALLS",
    "DEFAULT_MAX_INPUT_TOKENS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "PipelineConfigurationError",
    "ProviderUnavailable",
    "BudgetExceeded",
    "AIProviderConfig",
    "ProviderHealthResult",
    "OpenAIBundleModel",
    "SemanticFrameBundleModel",
    "AIProviderAdapter",
    "ExistingAIProviderAdapter",
    "run_provider_health_check",
    "BudgetReservation",
    "BudgetManager",
    "SemanticDecision",
    "PipelineRunResult",
    "ContextualBundlePipeline",
    "run_contextual_bundle_pipeline",
]
