"""K12 one-shot protocol diagnostic for the isolated linear Stage-A health path.

This module is deliberately a side-car to the K11 pilot.  It reads only the
body-free K11 artifact/audit and already-produced provider capability metadata,
then (when those gates agree) makes at most one synthetic health request using
the non-vision ``deepseek-v4-flash`` model.  It never reads a development
packet, frozen data, gold labels, production state, events, or frontend data.

The provider response is consumed in memory.  The artifact contains lengths,
hashes, response shape, finish/usage metadata, and local strict/diagnostic
classification only; raw response and reasoning text are never persisted.
Markdown fences and a single JSON object surrounded by prose are useful
diagnostic candidates, but are intentionally never promoted to a complete
Stage-A result.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, Union

from .settings import WorkbenchSettings


ARTIFACT_VERSION = "linear_stage_a_protocol_diagnostic_v1"
REPORT_SCHEMA_VERSION = "linear_stage_a_protocol_diagnostic_report_v1"
RUNNER_SCHEMA_VERSION = "linear_stage_a_protocol_diagnostic_runner_v1"
STAGE_A_SCHEMA_VERSION = "stage_a_topic_map_small_v1"
LOCAL_DAY = "2026-08-25"
FALLBACK_MODEL = "deepseek-v4-flash"
K11_MODEL = "deepseek-v4-flash-vision-exp"
MAX_PROVIDER_CALLS = 1
MAX_RETRIES = 0
HEALTH_MAX_INPUT_PROXY = 900
HEALTH_MAX_OUTPUT_TOKENS = 300
RESPONSE_FORMAT_OMITTED = "omitted"
RESPONSE_FORMAT_JSON_OBJECT = "json_object"

DEFAULT_K11_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_a_pilot_v1"
)
DEFAULT_CAPABILITY_DIRECTORIES = (
    Path("data/private/gold_standard/2026-08-25/contextual_bundle_provider_capabilities_v1"),
    Path("data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10"),
    Path("data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11"),
)
DEFAULT_SETTINGS_PATH = Path("data/workbench_settings.json")
DEFAULT_ARTIFACT_DIRECTORY = Path(
    "data/private/gold_standard/2026-08-25/linear_stage_a_protocol_diagnostic_v1"
)

STAGE_A_SYSTEM_PROMPT = (
    "You are a strict Stage A topic mapper. Return JSON only with exactly "
    "{topics:[{topic_id,message_handles,candidate_handles,evidence_handles,relation}]}. "
    "Group every supplied message_handle exactly once and every supplied "
    "candidate_handle exactly once. Evidence handles may be assigned only "
    "from the supplied list. Use only opaque handles from this page; never "
    "invent handles, IDs, entities, claims, or facts. Keep topic_id short, "
    "use relation from [same_topic,continuation,answer,question_followup,"
    "request_followup,contrast,new_topic,unrelated,unknown], and output no "
    "message text, reasoning, prose, or canonical 17-field semantic object."
)

RELATIONS = frozenset(
    {
        "same_topic",
        "continuation",
        "answer",
        "question_followup",
        "request_followup",
        "contrast",
        "new_topic",
        "unrelated",
        "unknown",
    }
)

REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "stage",
        "root_id",
        "page_id",
        "scope",
        "message_handles",
        "materials",
        "candidate_handles",
        "candidate_materials",
        "evidence_handles",
    }
)
MESSAGE_MATERIAL_KEYS = frozenset({"message_handle", "message_id", "roles", "material"})
CANDIDATE_MATERIAL_KEYS = frozenset(
    {"candidate_handle", "message_handles", "evidence_handles", "relation_type", "material"}
)
OUTPUT_KEYS = frozenset({"topics"})
TOPIC_KEYS = frozenset(
    {"topic_id", "message_handles", "candidate_handles", "evidence_handles", "relation"}
)
REQUEST_FORBIDDEN_KEYS = frozenset(
    {
        "speaker",
        "subject",
        "mentioned",
        "mentioned_person",
        "target",
        "object",
        "action",
        "claim_type",
        "state",
        "modality",
        "coreference_candidates",
        "context_relations",
        "claims",
        "findings",
    }
)

# These names are used only by the local body-free assertions.  Values under
# them are never copied to the artifact; ``*_length``/``*_sha256`` are safe
# telemetry fields and intentionally not included here.
BODY_KEYS = frozenset(
    {
        "body",
        "content",
        "content_body",
        "content_text",
        "evidence_text",
        "html",
        "markdown",
        "message_text",
        "prompt",
        "quote",
        "raw",
        "raw_text",
        "redacted_text",
        "response",
        "summary",
        "text",
        "text_body",
        "transcript",
    }
)
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "content",
        "headers",
        "key",
        "markdown",
        "password",
        "prompt",
        "quote",
        "raw",
        "raw_response",
        "raw_reasoning",
        "reasoning",
        "secret",
        "text",
        "token",
    }
)
SAFE_RESPONSE_FIELDS = frozenset(
    {
        "_request_id",
        "choices",
        "created",
        "id",
        "model",
        "object",
        "service_tier",
        "system_fingerprint",
        "usage",
    }
)
OUTPUT_SHAPES = frozenset({"brace", "fence", "prose", "empty"})
OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "diagnostic": "diagnostic.private.json",
    "ledger": "ledger.private.jsonl",
    "errors": "errors.private.jsonl",
}


class DiagnosticProtocolError(ValueError):
    """Stable body-free protocol error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


class DiagnosticProviderError(RuntimeError):
    """Stable body-free provider/adapter error."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=str)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _body_free(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.casefold() in BODY_KEYS and child not in (None, "", [], (), {}):
                continue
            result[key] = _body_free(child)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_body_free(child) for child in value]
    return deepcopy(value)


def _assert_body_free(value: Any, *, label: str = "value") -> None:
    hits: List[str] = []

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key.casefold() in BODY_KEYS and child not in (None, "", [], (), {}):
                    hits.append(path + key)
                visit(child, path + key + ".")
        elif isinstance(item, (list, tuple, set, frozenset)):
            for index, child in enumerate(item):
                visit(child, path + str(index) + ".")

    visit(value)
    if hits:
        raise ValueError("%s contains body-bearing fields: %s" % (label, ", ".join(hits[:5])))


def _guard_safe_scalar(value: Any, code: str, *, max_length: int = 240, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise DiagnosticProtocolError(code)
    if not allow_empty and not value:
        raise DiagnosticProtocolError(code)
    if value != value.strip() or len(value) > max_length or any(ord(char) < 32 for char in value):
        raise DiagnosticProtocolError(code)
    return value


def _string_list(value: Any, code: str, *, allow_empty: bool = True) -> List[str]:
    if type(value) is not list or (not allow_empty and not value):
        raise DiagnosticProtocolError(code)
    result: List[str] = []
    for item in value:
        result.append(_guard_safe_scalar(item, code, max_length=400))
    if len(result) != len(set(result)):
        raise DiagnosticProtocolError(code + "_duplicate")
    return result


def _scope_pair(scope: Any) -> Tuple[str, str]:
    if not isinstance(scope, Mapping):
        raise DiagnosticProtocolError("scope_shape")
    account = scope.get("account_id", scope.get("account"))
    chat = scope.get("chat_id", scope.get("chat"))
    return (
        _guard_safe_scalar(account, "scope_account", max_length=120),
        _guard_safe_scalar(chat, "scope_chat", max_length=120),
    )


def _handle_scope(handle: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(handle, str) or "|" not in handle:
        return None
    prefix = handle.split("|", 1)[0]
    if "/" not in prefix:
        return None
    account, chat = prefix.split("/", 1)
    return (account, chat) if account and chat else None


def _assert_handle_scope(handle: str, scope: Tuple[str, str], code: str) -> None:
    parsed = _handle_scope(handle)
    if parsed is not None and parsed != scope:
        raise DiagnosticProtocolError(code)


def validate_stage_a_request(user_packet: Mapping[str, Any]) -> None:
    """Validate the exact small Stage-A request used by K11 health."""

    if not isinstance(user_packet, Mapping) or set(user_packet) != set(REQUEST_KEYS):
        raise DiagnosticProtocolError("stage_a_request_keys")
    if user_packet.get("schema_version") != STAGE_A_SCHEMA_VERSION or user_packet.get("stage") != "A":
        raise DiagnosticProtocolError("stage_a_request_version")
    scope = _scope_pair(user_packet.get("scope"))
    root_id = _guard_safe_scalar(user_packet.get("root_id"), "stage_a_request_root_id")
    page_id = _guard_safe_scalar(user_packet.get("page_id"), "stage_a_request_page_id", max_length=400)
    if not root_id or not page_id:
        raise DiagnosticProtocolError("stage_a_request_identity")
    messages = _string_list(user_packet.get("message_handles"), "stage_a_request_messages", allow_empty=False)
    candidates = _string_list(user_packet.get("candidate_handles"), "stage_a_request_candidates")
    evidence = _string_list(user_packet.get("evidence_handles"), "stage_a_request_evidence")
    for handle in messages + candidates + evidence:
        _assert_handle_scope(handle, scope, "stage_a_request_cross_scope")

    materials = user_packet.get("materials")
    if type(materials) is not list or len(materials) != len(messages):
        raise DiagnosticProtocolError("stage_a_request_materials")
    material_handles: List[str] = []
    for row in materials:
        if not isinstance(row, Mapping) or set(row) != set(MESSAGE_MATERIAL_KEYS):
            raise DiagnosticProtocolError("stage_a_request_message_material_shape")
        material_handles.append(_guard_safe_scalar(row.get("message_handle"), "stage_a_request_message_handle", max_length=400))
        _guard_safe_scalar(row.get("message_id"), "stage_a_request_message_id", max_length=200)
        _string_list(row.get("roles"), "stage_a_request_roles")
        material = row.get("material")
        if type(material) is not str or len(material) > 512:
            raise DiagnosticProtocolError("stage_a_request_material_text")
        if any(str(key).casefold() in REQUEST_FORBIDDEN_KEYS for key in row):
            raise DiagnosticProtocolError("stage_a_request_17_field")
    if material_handles != messages:
        raise DiagnosticProtocolError("stage_a_request_material_alignment")

    candidate_materials = user_packet.get("candidate_materials")
    if type(candidate_materials) is not list or len(candidate_materials) != len(candidates):
        raise DiagnosticProtocolError("stage_a_request_candidate_materials")
    candidate_material_handles: List[str] = []
    for row in candidate_materials:
        if not isinstance(row, Mapping) or set(row) != set(CANDIDATE_MATERIAL_KEYS):
            raise DiagnosticProtocolError("stage_a_request_candidate_material_shape")
        candidate_material_handles.append(
            _guard_safe_scalar(row.get("candidate_handle"), "stage_a_request_candidate_handle", max_length=400)
        )
        related_messages = _string_list(row.get("message_handles"), "stage_a_request_candidate_messages")
        related_evidence = _string_list(row.get("evidence_handles"), "stage_a_request_candidate_evidence")
        if not set(related_messages) <= set(messages) or not set(related_evidence) <= set(evidence):
            raise DiagnosticProtocolError("stage_a_request_candidate_scope")
        relation_type = row.get("relation_type")
        if relation_type is not None and type(relation_type) is not str:
            raise DiagnosticProtocolError("stage_a_request_candidate_relation")
        material = row.get("material")
        if not isinstance(material, Mapping) or set(material) - {"views", "reason_codes", "uncertainties"}:
            raise DiagnosticProtocolError("stage_a_request_candidate_material")
        for key in ("views", "reason_codes", "uncertainties"):
            _string_list(material.get(key, []), "stage_a_request_candidate_%s" % key)
        if any(str(key).casefold() in REQUEST_FORBIDDEN_KEYS for key in row):
            raise DiagnosticProtocolError("stage_a_request_17_field")
        if any(str(key).casefold() in REQUEST_FORBIDDEN_KEYS for key in material):
            raise DiagnosticProtocolError("stage_a_request_17_field")
    if candidate_material_handles != candidates:
        raise DiagnosticProtocolError("stage_a_request_candidate_alignment")


def validate_stage_a_output(value: Any, user_packet: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly validate a small Stage-A result without retaining its body."""

    validate_stage_a_request(user_packet)
    if not isinstance(value, Mapping) or set(value) != set(OUTPUT_KEYS):
        raise DiagnosticProtocolError("stage_a_output_keys")
    topics = value.get("topics")
    if type(topics) is not list or not topics or len(topics) > len(user_packet["message_handles"]):
        raise DiagnosticProtocolError("stage_a_output_topics")
    allowed_messages = set(str(item) for item in user_packet["message_handles"])
    allowed_candidates = set(str(item) for item in user_packet["candidate_handles"])
    allowed_evidence = set(str(item) for item in user_packet["evidence_handles"])
    seen_topics: Set[str] = set()
    seen_messages: Set[str] = set()
    seen_candidates: Set[str] = set()
    seen_evidence: Set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for row in topics:
        if not isinstance(row, Mapping) or set(row) != set(TOPIC_KEYS):
            raise DiagnosticProtocolError("stage_a_output_topic_keys")
        topic_id = _guard_safe_scalar(row.get("topic_id"), "stage_a_output_topic_id", max_length=120)
        if topic_id in seen_topics:
            raise DiagnosticProtocolError("stage_a_output_duplicate_topic")
        seen_topics.add(topic_id)
        messages = _string_list(row.get("message_handles"), "stage_a_output_messages", allow_empty=False)
        candidates = _string_list(row.get("candidate_handles"), "stage_a_output_candidates")
        evidence = _string_list(row.get("evidence_handles"), "stage_a_output_evidence")
        relation = row.get("relation")
        if relation not in RELATIONS:
            raise DiagnosticProtocolError("stage_a_output_relation")
        if not set(messages) <= allowed_messages:
            raise DiagnosticProtocolError("stage_a_output_message_scope")
        if not set(candidates) <= allowed_candidates:
            raise DiagnosticProtocolError("stage_a_output_candidate_scope")
        if not set(evidence) <= allowed_evidence:
            raise DiagnosticProtocolError("stage_a_output_evidence_scope")
        if seen_messages.intersection(messages) or seen_candidates.intersection(candidates) or seen_evidence.intersection(evidence):
            raise DiagnosticProtocolError("stage_a_output_duplicate_handle")
        seen_messages.update(messages)
        seen_candidates.update(candidates)
        seen_evidence.update(evidence)
        normalized.append(
            {
                "topic_id": topic_id,
                "message_handles": list(messages),
                "candidate_handles": list(candidates),
                "evidence_handles": list(evidence),
                "relation": relation,
            }
        )
    if seen_messages != allowed_messages:
        raise DiagnosticProtocolError("stage_a_output_message_coverage")
    if seen_candidates != allowed_candidates:
        raise DiagnosticProtocolError("stage_a_output_candidate_coverage")
    return {"topics": normalized}


def _health_request() -> Dict[str, Any]:
    packet = {
        "schema_version": STAGE_A_SCHEMA_VERSION,
        "stage": "A",
        "root_id": "SYNTHETIC_HEALTH_ROOT",
        "page_id": "SYNTHETIC_HEALTH_PAGE",
        "scope": {"account_id": "SYNTHETIC_ACCOUNT", "chat_id": "SYNTHETIC_CHAT"},
        "message_handles": [
            "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|message|HEALTH_001",
            "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|message|HEALTH_002",
        ],
        "materials": [
            {
                "message_handle": "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|message|HEALTH_001",
                "message_id": "HEALTH_001",
                "roles": ["primary"],
                "material": "Synthetic first message about a small topic.",
            },
            {
                "message_handle": "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|message|HEALTH_002",
                "message_id": "HEALTH_002",
                "roles": ["adjacent"],
                "material": "Synthetic follow-up message on the same topic.",
            },
        ],
        "candidate_handles": ["SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|candidate|HEALTH_CANDIDATE"],
        "candidate_materials": [
            {
                "candidate_handle": "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|candidate|HEALTH_CANDIDATE",
                "message_handles": [
                    "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|message|HEALTH_001",
                    "SYNTHETIC_ACCOUNT/SYNTHETIC_CHAT|message|HEALTH_002",
                ],
                "evidence_handles": [],
                "relation_type": "continuation_candidate",
                "material": {"views": ["synthetic"], "reason_codes": ["synthetic_probe"], "uncertainties": []},
            }
        ],
        "evidence_handles": [],
    }
    validate_stage_a_request(packet)
    return packet


def _token_proxy(value: Any, system_prompt: str = "") -> int:
    return (len(canonical_json(value)) + len(str(system_prompt)) + 3) // 4


def _safe_field_names(value: Any) -> Tuple[str, ...]:
    """Return an allowlisted top-level response field-name tuple only."""

    if isinstance(value, Mapping):
        names = [str(key) for key in value]
    else:
        names = [str(key) for key in vars(value)] if hasattr(value, "__dict__") else []
    output = []
    for name in names:
        folded = name.casefold()
        if name in SAFE_RESPONSE_FIELDS and folded not in SENSITIVE_FIELD_NAMES:
            output.append(name)
    return tuple(sorted(set(output)))


def _filter_response_field_names(values: Iterable[Any]) -> Tuple[str, ...]:
    """Apply the same public allowlist to injected/fake response metadata."""

    return tuple(
        sorted(
            {
                str(value)
                for value in values
                if str(value) in SAFE_RESPONSE_FIELDS and str(value).casefold() not in SENSITIVE_FIELD_NAMES
            }
        )
    )


def _extract_usage(value: Any) -> Tuple[int, int, bool, Tuple[str, ...]]:
    usage = value.get("usage") if isinstance(value, Mapping) else getattr(value, "usage", None)
    if usage is None:
        return 0, 0, False, ()
    if isinstance(usage, Mapping):
        keys = tuple(sorted(str(key) for key in usage if str(key) in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}))
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    else:
        keys = tuple(
            sorted(
                key
                for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens")
                if hasattr(usage, key)
            )
        )
        input_tokens = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0))
        output_tokens = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0))
    try:
        input_value = max(0, int(input_tokens or 0))
    except (TypeError, ValueError):
        input_value = 0
    try:
        output_value = max(0, int(output_tokens or 0))
    except (TypeError, ValueError):
        output_value = 0
    return input_value, output_value, True, keys


def _choice_values(value: Any) -> Tuple[List[Any], List[str]]:
    choices = value.get("choices") if isinstance(value, Mapping) else getattr(value, "choices", None)
    if not isinstance(choices, (list, tuple)):
        return [], []
    finish: List[str] = []
    for choice in choices:
        reason = choice.get("finish_reason") if isinstance(choice, Mapping) else getattr(choice, "finish_reason", None)
        if isinstance(reason, str) and reason and len(reason) <= 80 and "\n" not in reason:
            finish.append(reason)
    return list(choices), finish


def _message_content(choice: Any) -> Tuple[str, str]:
    message = choice.get("message") if isinstance(choice, Mapping) else getattr(choice, "message", None)
    if isinstance(message, Mapping):
        content = message.get("content")
        reasoning = message.get("reasoning_content", message.get("reasoning", ""))
    else:
        content = getattr(message, "content", None)
        reasoning = getattr(message, "reasoning_content", getattr(message, "reasoning", ""))
    # OpenAI-compatible gateways sometimes expose content parts.  Joining only
    # textual parts is an in-memory extraction; the joined body is never saved.
    if isinstance(content, str):
        content_text = content
    elif isinstance(content, (list, tuple)):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content_text = "".join(parts)
    else:
        content_text = ""
    reasoning_text = reasoning if isinstance(reasoning, str) else ""
    return content_text, reasoning_text


@dataclass(frozen=True)
class DiagnosticProviderResponse:
    """Ephemeral provider result; ``content`` never crosses the write boundary."""

    content: str
    reasoning_content: str = ""
    finish_reasons: Tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    usage_present: bool = False
    usage_keys: Tuple[str, ...] = ()
    latency_ms: float = 0.0
    model: str = ""
    source: str = ""
    response_fields: Tuple[str, ...] = ()
    request_id_present: bool = False


class DiagnosticStageProvider(Protocol):
    source: str
    model_id: str
    configured: bool

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> DiagnosticProviderResponse:
        ...


@dataclass(frozen=True)
class DiagnosticProviderConfig:
    """Local config; secrets are excluded from repr/equality/public output."""

    model: str
    base_url: Optional[str]
    api_key: Optional[str] = field(default=None, repr=False, compare=False)
    timeout_seconds: Optional[float] = field(default=None, repr=False, compare=False)
    response_format_mode: str = RESPONSE_FORMAT_OMITTED
    response_format_rationale: str = ""
    thinking_disabled: bool = False

    @classmethod
    def from_workbench_settings(
        cls,
        settings_path: Union[str, Path],
        *,
        model_override: str = FALLBACK_MODEL,
        response_format_mode: str = RESPONSE_FORMAT_OMITTED,
        response_format_rationale: str = "",
        thinking_disabled: bool = False,
    ) -> "DiagnosticProviderConfig":
        settings = WorkbenchSettings(str(settings_path))
        snapshot = settings.snapshot(include_secrets=True)
        if not isinstance(snapshot, Mapping):
            raise ValueError("settings_snapshot_invalid")
        ai = snapshot.get("ai") if isinstance(snapshot.get("ai"), Mapping) else {}
        env_key = os.environ.get("OPENAI_API_KEY") or ""
        env_url = os.environ.get("OPENAI_BASE_URL") or ""
        configured_url = str(ai.get("base_url") or env_url or "").strip().rstrip("/") or None
        configured_key = str(ai.get("api_key") or env_key or "").strip() or None
        model = str(model_override or FALLBACK_MODEL).strip()[:120] or FALLBACK_MODEL
        mode = str(response_format_mode or RESPONSE_FORMAT_OMITTED)
        if mode not in {RESPONSE_FORMAT_OMITTED, RESPONSE_FORMAT_JSON_OBJECT}:
            raise ValueError("response_format_mode_invalid")
        return cls(
            model=model,
            base_url=configured_url,
            api_key=configured_key,
            response_format_mode=mode,
            response_format_rationale=str(response_format_rationale or "")[:600],
            thinking_disabled=bool(thinking_disabled),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "provider": "deepseek",
            "source": "deepseek-openai-compatible",
            "model": self.model,
            "base_url_configured": bool(self.base_url),
            "api_key_configured": bool(self.api_key),
            "response_format_mode": self.response_format_mode,
            "response_format_sent": self.response_format_mode == RESPONSE_FORMAT_JSON_OBJECT,
            "thinking_disabled": bool(self.thinking_disabled),
        }


class DeepSeekProtocolDiagnosticProvider:
    """One-call OpenAI-compatible adapter that preserves response text in RAM."""

    source = "deepseek-openai-compatible"

    def __init__(self, config: DiagnosticProviderConfig, *, client: Any = None, clock: Optional[Callable[[], float]] = None) -> None:
        self.config = config
        self.model_id = config.model
        self._client = client
        self._clock = clock or time.perf_counter

    @property
    def configured(self) -> bool:
        return self._client is not None or bool(self.config.api_key)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.config.api_key:
            raise DiagnosticProviderError("provider_unconfigured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise DiagnosticProviderError("provider_sdk_unavailable") from exc
        kwargs: Dict[str, Any] = {"api_key": self.config.api_key}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.timeout_seconds is not None:
            kwargs["timeout"] = self.config.timeout_seconds
        self._client = OpenAI(**kwargs)
        return self._client

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> DiagnosticProviderResponse:
        del stage
        client = self._get_client()
        request: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_json(user_packet)},
            ],
            "max_tokens": int(max_output_tokens),
            "temperature": 0,
        }
        if self.config.response_format_mode == RESPONSE_FORMAT_JSON_OBJECT:
            request["response_format"] = {"type": "json_object"}
        if self.config.thinking_disabled:
            # Explicit per-call provider extension.  It is intentionally not
            # loaded from or written back to global WorkbenchSettings.
            request["extra_body"] = {"thinking": {"type": "disabled"}}
        started = self._clock()
        try:
            response = client.chat.completions.create(**request)
        except DiagnosticProviderError:
            raise
        except Exception as exc:
            raise DiagnosticProviderError("provider_request_failed") from exc
        elapsed = max(0.0, (self._clock() - started) * 1000.0)
        choices, finish_reasons = _choice_values(response)
        if not choices:
            raise DiagnosticProviderError("provider_response_shape")
        content, reasoning = _message_content(choices[0])
        input_tokens, output_tokens, usage_present, usage_keys = _extract_usage(response)
        response_id = response.get("id") if isinstance(response, Mapping) else getattr(response, "id", None)
        model = response.get("model") if isinstance(response, Mapping) else getattr(response, "model", None)
        return DiagnosticProviderResponse(
            content=content,
            reasoning_content=reasoning,
            finish_reasons=tuple(finish_reasons),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_present=usage_present,
            usage_keys=usage_keys,
            latency_ms=elapsed,
            model=str(model or self.model_id),
            source=self.source,
            response_fields=_safe_field_names(response),
            request_id_present=bool(response_id),
        )


def _coerce_provider_response(value: Any, provider: Any) -> DiagnosticProviderResponse:
    if isinstance(value, DiagnosticProviderResponse):
        return value
    if isinstance(value, Mapping):
        content = value.get("content", "")
        if not isinstance(content, str):
            content = ""
        finish = value.get("finish_reasons", value.get("finish_reason", ()))
        if isinstance(finish, str):
            finish = (finish,)
        elif not isinstance(finish, (list, tuple)):
            finish = ()
        try:
            input_tokens = max(0, int(value.get("input_tokens", 0) or 0))
        except (TypeError, ValueError):
            input_tokens = 0
        try:
            output_tokens = max(0, int(value.get("output_tokens", 0) or 0))
        except (TypeError, ValueError):
            output_tokens = 0
        return DiagnosticProviderResponse(
            content=content,
            reasoning_content=str(value.get("reasoning_content", "") or "") if isinstance(value.get("reasoning_content", ""), str) else "",
            finish_reasons=tuple(str(item)[:80] for item in finish if isinstance(item, str) and item),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_present=bool(value.get("usage_present", "input_tokens" in value or "output_tokens" in value)),
            usage_keys=tuple(str(item) for item in value.get("usage_keys", ()) if isinstance(item, str)),
            latency_ms=max(0.0, float(value.get("latency_ms", 0.0) or 0.0)),
            model=str(value.get("model") or getattr(provider, "model_id", FALLBACK_MODEL)),
            source=str(value.get("source") or getattr(provider, "source", "unknown")),
            response_fields=tuple(str(item) for item in value.get("response_fields", ()) if isinstance(item, str)),
            request_id_present=bool(value.get("request_id_present", False)),
        )
    # A small compatibility bridge for synthetic fakes returning an object
    # with the same public attributes as DiagnosticProviderResponse.
    content = getattr(value, "content", "")
    if not isinstance(content, str):
        content = ""
    finish = getattr(value, "finish_reasons", ())
    if isinstance(finish, str):
        finish = (finish,)
    return DiagnosticProviderResponse(
        content=content,
        reasoning_content=str(getattr(value, "reasoning_content", "") or ""),
        finish_reasons=tuple(str(item)[:80] for item in finish if isinstance(item, str) and item),
        input_tokens=max(0, int(getattr(value, "input_tokens", 0) or 0)),
        output_tokens=max(0, int(getattr(value, "output_tokens", 0) or 0)),
        usage_present=bool(getattr(value, "usage_present", False)),
        usage_keys=tuple(str(item) for item in getattr(value, "usage_keys", ()) if isinstance(item, str)),
        latency_ms=max(0.0, float(getattr(value, "latency_ms", 0.0) or 0.0)),
        model=str(getattr(value, "model", "") or getattr(provider, "model_id", FALLBACK_MODEL)),
        source=str(getattr(value, "source", "") or getattr(provider, "source", "unknown")),
        response_fields=tuple(str(item) for item in getattr(value, "response_fields", ()) if isinstance(item, str)),
        request_id_present=bool(getattr(value, "request_id_present", False)),
    )


def _strict_loads(text: Any) -> Any:
    if type(text) is not str:
        raise DiagnosticProtocolError("provider_response_not_text")

    def reject_constant(_value: str) -> Any:
        raise DiagnosticProtocolError("provider_nonstandard_json")

    def reject_duplicate(pairs: List[Tuple[Any, Any]]) -> Dict[Any, Any]:
        result: Dict[Any, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DiagnosticProtocolError("provider_duplicate_json_key")
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicate)
    except DiagnosticProtocolError:
        raise
    except (TypeError, ValueError) as exc:
        raise DiagnosticProtocolError("provider_invalid_json") from exc


def _leading_shape(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        return "empty"
    if stripped.startswith("```"):
        return "fence"
    if stripped.startswith("{"):
        return "brace"
    return "prose"


def _json_object_spans(text: str) -> List[Tuple[int, int, Any, str]]:
    """Find non-overlapping outer JSON objects without retaining raw text."""

    decoder = json.JSONDecoder(parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    found: List[Tuple[int, int, Any, str]] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except (TypeError, ValueError):
            continue
        if not isinstance(value, Mapping):
            continue
        end = start + consumed
        found.append((start, end, value, stable_hash(value)))
    # The first/largest outer object subsumes its nested objects.  Keep only
    # non-overlapping spans so a normal one-object Stage-A payload is count 1.
    outer: List[Tuple[int, int, Any, str]] = []
    for item in sorted(found, key=lambda row: (row[0], -(row[1] - row[0]))):
        if any(item[0] >= previous[0] and item[1] <= previous[1] for previous in outer):
            continue
        outer.append(item)
    return outer


_FENCE_PATTERN = re.compile(r"```(?:[A-Za-z0-9_+.-]+)?[ \t]*\r?\n?(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class ParsedDiagnostics:
    leading_shape: str
    fence_detected: bool
    unique_json_object_count: int
    output_sha256: str
    content_length: int
    reasoning_length: int
    strict_parse_code: str
    strict_validation_code: str
    strict_result_status: str
    strict_complete: bool
    candidate_present: bool
    candidate_extraction: str
    candidate_parse_code: str
    candidate_validation_code: str
    candidate_status: str
    candidate_accepted_for_complete: bool
    candidate_field_names: Tuple[str, ...]


def _candidate_parse(text: str, spans: Sequence[Tuple[int, int, Any, str]], fence_detected: bool) -> Tuple[Optional[Any], str, str]:
    """Return one diagnostic-only candidate and its extraction label."""

    if fence_detected:
        matches = list(_FENCE_PATTERN.finditer(text))
        if len(matches) != 1:
            return None, "ambiguous_fence", "none"
        fenced = matches[0].group(1).strip()
        try:
            return _strict_loads(fenced), "ok", "unique_markdown_fence"
        except DiagnosticProtocolError as exc:
            return None, exc.code, "unique_markdown_fence"
    if len(spans) != 1:
        return None, "unique_json_object_count_not_one", "none"
    try:
        return _strict_loads(text[spans[0][0] : spans[0][1]]), "ok", "single_json_object_with_prose"
    except DiagnosticProtocolError as exc:
        return None, exc.code, "single_json_object_with_prose"


def _parse_diagnostics(response: DiagnosticProviderResponse, request: Mapping[str, Any]) -> ParsedDiagnostics:
    text = response.content
    shape = _leading_shape(text)
    fence_detected = bool(_FENCE_PATTERN.search(text))
    spans = _json_object_spans(text)
    strict_parse_code = "ok"
    strict_validation_code = "ok"
    strict_complete = False
    try:
        strict_value = _strict_loads(text)
    except DiagnosticProtocolError as exc:
        strict_parse_code = exc.code
        strict_validation_code = "not_run"
        strict_result_status = "blocked"
    else:
        try:
            validate_stage_a_output(strict_value, request)
        except DiagnosticProtocolError as exc:
            strict_validation_code = exc.code
            strict_result_status = "blocked"
        else:
            strict_complete = True
            strict_result_status = "available"

    candidate_value, candidate_parse_code, extraction = _candidate_parse(text, spans, fence_detected)
    candidate_present = candidate_value is not None
    candidate_validation_code = "not_run"
    candidate_status = "not_available"
    candidate_fields: Tuple[str, ...] = ()
    if candidate_value is not None:
        candidate_fields = tuple(sorted(str(key) for key in candidate_value if str(key) not in SENSITIVE_FIELD_NAMES)) if isinstance(candidate_value, Mapping) else ()
        try:
            validate_stage_a_output(candidate_value, request)
        except DiagnosticProtocolError as exc:
            candidate_validation_code = exc.code
            candidate_status = "candidate_invalid_schema"
        else:
            candidate_validation_code = "ok"
            candidate_status = "candidate_valid_schema_not_accepted"
    return ParsedDiagnostics(
        leading_shape=shape,
        fence_detected=fence_detected,
        unique_json_object_count=len({item[3] for item in spans}),
        output_sha256=_sha256_text(text),
        content_length=len(text),
        reasoning_length=len(response.reasoning_content),
        strict_parse_code=strict_parse_code,
        strict_validation_code=strict_validation_code,
        strict_result_status=strict_result_status,
        strict_complete=strict_complete,
        candidate_present=candidate_present,
        candidate_extraction=extraction,
        candidate_parse_code=candidate_parse_code,
        candidate_validation_code=candidate_validation_code,
        candidate_status=candidate_status,
        candidate_accepted_for_complete=False,
        candidate_field_names=candidate_fields,
    )


def _empty_diagnostics(error_code: str = "not_run") -> ParsedDiagnostics:
    return ParsedDiagnostics(
        leading_shape="empty",
        fence_detected=False,
        unique_json_object_count=0,
        output_sha256=_sha256_text(""),
        content_length=0,
        reasoning_length=0,
        strict_parse_code=error_code,
        strict_validation_code="not_run",
        strict_result_status="not_run",
        strict_complete=False,
        candidate_present=False,
        candidate_extraction="none",
        candidate_parse_code="not_run",
        candidate_validation_code="not_run",
        candidate_status="not_available",
        candidate_accepted_for_complete=False,
        candidate_field_names=(),
    )


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, (DiagnosticProtocolError, DiagnosticProviderError)):
        return str(exc.code)
    name = type(exc).__name__.casefold()
    if "timeout" in name:
        return "provider_timeout"
    if "ratelimit" in name or "rate_limit" in name:
        return "provider_rate_limited"
    if "auth" in name or "permission" in name:
        return "provider_authentication_error"
    if "connection" in name or "network" in name:
        return "provider_network_error"
    return "provider_call_failed"


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid_json:%s" % path.name) from exc
    if not isinstance(value, Mapping):
        raise ValueError("json_not_object:%s" % path.name)
    return value


def _safe_path(path: Union[str, Path]) -> Path:
    resolved = Path(path).expanduser().resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in resolved.parts):
        raise ValueError("diagnostic_refuses_frozen_path")
    return resolved


@dataclass(frozen=True)
class PriorState:
    k11_artifact_ok: bool
    k11_audit_ok: bool
    k11_model: str
    k11_response_format_mode: str
    k11_error_code: str
    fallback_model: Optional[str]
    fallback_available: bool
    fallback_source: str
    fallback_capability_artifact: str
    fallback_response_format_mode: str
    errors: Tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return bool(self.k11_artifact_ok and self.k11_audit_ok and self.fallback_available and not self.errors)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "k11_artifact_ok": self.k11_artifact_ok,
            "k11_audit_ok": self.k11_audit_ok,
            "k11_artifact_version": "linear_stage_a_pilot_v1",
            "k11_model": self.k11_model,
            "k11_response_format_mode": self.k11_response_format_mode,
            "k11_error_code": self.k11_error_code,
            "fallback_model": self.fallback_model,
            "fallback_available": self.fallback_available,
            "fallback_source": self.fallback_source,
            "fallback_capability_artifact": self.fallback_capability_artifact,
            "fallback_response_format_mode": self.fallback_response_format_mode,
            "errors": list(self.errors),
        }


def _capability_candidate_from_matrix(path: Path) -> Tuple[bool, str, str]:
    """Read the old matrix for evidence, but require a successful health elsewhere."""

    try:
        matrix = _read_json(path)
    except ValueError:
        return False, "", ""
    rows = matrix.get("candidates")
    if not isinstance(rows, (list, tuple)):
        return False, "", ""
    for row in rows:
        if not isinstance(row, Mapping) or row.get("model_id") != FALLBACK_MODEL:
            continue
        hints = row.get("capability_hints") if isinstance(row.get("capability_hints"), Mapping) else {}
        if row.get("eligible") is True and hints.get("vision_like") is False and hints.get("vision_exp") is False:
            return False, "", "heuristic_only"
    return False, "", ""


def _capability_health_from_directory(path: Path) -> Tuple[bool, str, str, str]:
    health_path = path / "provider_health.private.json"
    if not health_path.is_file():
        return False, "", "", ""
    try:
        health = _read_json(health_path)
    except ValueError:
        return False, "", "", ""
    model = str(health.get("model") or "")
    if model != FALLBACK_MODEL or health.get("ok") is not True or health.get("status") != "available":
        return False, "", "", ""
    diagnostics = health.get("diagnostics") if isinstance(health.get("diagnostics"), Mapping) else {}
    if health.get("raw_content_saved") is True or health.get("raw_reasoning_saved") is True:
        return False, "", "", ""
    if diagnostics.get("raw_content_saved") is True or diagnostics.get("raw_reasoning_saved") is True:
        return False, "", "", ""
    # v2.10's successful text-model health was explicitly sent without a
    # response_format.  Treat a different strategy as weaker evidence.
    sent = health.get("response_format_sent", diagnostics.get("response_format_sent"))
    if sent is not False:
        return False, "", "", ""
    source = str(health.get("source") or "unknown")
    return True, model, source, RESPONSE_FORMAT_OMITTED


def inspect_prior_state(
    k11_artifact_directory: Union[str, Path] = DEFAULT_K11_ARTIFACT_DIRECTORY,
    capability_directories: Sequence[Union[str, Path]] = DEFAULT_CAPABILITY_DIRECTORIES,
) -> PriorState:
    """Read only body-free gate metadata needed before the one call."""

    errors: List[str] = []
    k11_dir = _safe_path(k11_artifact_directory)
    artifact_ok = False
    audit_ok = False
    k11_model = ""
    k11_format = ""
    k11_error = ""
    try:
        manifest = _read_json(k11_dir / "manifest.private.json")
        aggregate = _read_json(k11_dir / "aggregate.private.json")
        audit = _read_json(k11_dir / "audit" / "audit_summary.private.json")
        provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
        health = aggregate.get("health") if isinstance(aggregate.get("health"), Mapping) else {}
        next_step = audit.get("next_step") if isinstance(audit.get("next_step"), Mapping) else {}
        artifact_ok = bool(
            k11_dir.name == "linear_stage_a_pilot_v1"
            and manifest.get("artifact_version") == "linear_stage_a_pilot_v1"
            and manifest.get("status") == "blocked"
            and manifest.get("success") is False
            and manifest.get("development_input_read") is False
            and manifest.get("frozen_read") is False
            and manifest.get("gold_loaded") is False
            and manifest.get("production_state_written") is False
            and manifest.get("provider_calls") == 1
            and manifest.get("selected_page_count") == 0
        )
        k11_model = str(provider.get("model") or "")
        k11_format = str(provider.get("response_format_mode") or "")
        k11_error = str(health.get("error_code") or "")
        audit_ok = bool(
            audit.get("audit_status") == "pass"
            and audit.get("health_gate") == "blocked"
            and audit.get("strict_error") == "provider_invalid_json"
            and audit.get("allow_stage_a_development") is False
            and audit.get("allow_stage_b_pilot") is False
            and audit.get("allow_one_protocol_diagnostic") is True
            and next_step.get("diagnostic_scope") == "synthetic_health_only"
            and next_step.get("diagnostic_must_not_read_development_input") is True
        )
        if not artifact_ok:
            errors.append("k11_artifact_gate_failed")
        if not audit_ok:
            errors.append("k11_audit_gate_failed")
        if k11_model != K11_MODEL:
            errors.append("k11_model_mismatch")
        if k11_format != RESPONSE_FORMAT_JSON_OBJECT:
            errors.append("k11_response_format_mismatch")
        if k11_error != "provider_invalid_json":
            errors.append("k11_error_mismatch")
    except (OSError, ValueError) as exc:
        errors.append(str(exc))

    fallback_available = False
    fallback_model: Optional[str] = None
    fallback_source = ""
    fallback_artifact = ""
    fallback_format = ""
    for raw_directory in capability_directories:
        try:
            directory = _safe_path(raw_directory)
        except ValueError:
            errors.append("capability_frozen_path_refused")
            continue
        # This matrix is evidence that the model is non-vision, but its own
        # health failed; it must not by itself authorize the call.
        if directory.name == "contextual_bundle_provider_capabilities_v1":
            _capability_candidate_from_matrix(directory / "provider_capabilities.private.json")
        available, model, source, response_mode = _capability_health_from_directory(directory)
        if available and not fallback_available:
            fallback_available = True
            fallback_model = model
            fallback_source = source
            fallback_artifact = directory.name
            fallback_format = response_mode
    if not fallback_available:
        errors.append("fallback_model_unavailable")
    return PriorState(
        k11_artifact_ok=artifact_ok,
        k11_audit_ok=audit_ok,
        k11_model=k11_model,
        k11_response_format_mode=k11_format,
        k11_error_code=k11_error,
        fallback_model=fallback_model,
        fallback_available=fallback_available,
        fallback_source=fallback_source,
        fallback_capability_artifact=fallback_artifact,
        fallback_response_format_mode=fallback_format,
        errors=tuple(dict.fromkeys(errors)),
    )


def _selected_opaque_refs(request: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "root_id": str(request.get("root_id", "")),
        "page_id": str(request.get("page_id", "")),
        "scope": deepcopy(request.get("scope", {})),
        "message_handles": list(request.get("message_handles", ())),
        "candidate_handles": list(request.get("candidate_handles", ())),
        "evidence_handles": list(request.get("evidence_handles", ())),
    }


def _response_diagnostic_dict(response: Optional[DiagnosticProviderResponse], parsed: ParsedDiagnostics) -> Dict[str, Any]:
    finish_reasons = list(response.finish_reasons) if response else []
    safe_fields = _filter_response_field_names(response.response_fields) if response else ()
    return {
        "content_length": parsed.content_length,
        "reasoning_length": parsed.reasoning_length,
        "finish_reasons": finish_reasons,
        "status": "received" if response is not None else "not_run",
        "usage": {
            "present": bool(response.usage_present) if response else False,
            "input_tokens": int(response.input_tokens) if response else 0,
            "output_tokens": int(response.output_tokens) if response else 0,
            "keys": list(response.usage_keys) if response else [],
        },
        "output_sha256": parsed.output_sha256,
        "leading_shape": parsed.leading_shape,
        "fence_detected": parsed.fence_detected,
        "unique_json_object_count": parsed.unique_json_object_count,
        "strict_parse_code": parsed.strict_parse_code,
        "strict_validation_code": parsed.strict_validation_code,
        "safe_public_field_names": list(safe_fields),
        "request_id_present": bool(response.request_id_present) if response else False,
        "raw_response_saved": False,
        "raw_reasoning_saved": False,
    }


def _strict_result_dict(parsed: ParsedDiagnostics) -> Dict[str, Any]:
    return {
        "status": parsed.strict_result_status,
        "strict_complete": parsed.strict_complete,
        "accepted_for_stage_a_development": False,
        "parse_code": parsed.strict_parse_code,
        "validation_code": parsed.strict_validation_code,
    }


def _candidate_result_dict(parsed: ParsedDiagnostics) -> Dict[str, Any]:
    return {
        "present": parsed.candidate_present,
        "status": parsed.candidate_status,
        "extraction": parsed.candidate_extraction,
        "parse_code": parsed.candidate_parse_code,
        "validation_code": parsed.candidate_validation_code,
        "safe_field_names": list(parsed.candidate_field_names),
        "accepted_for_complete": False,
    }


@dataclass(frozen=True)
class DiagnosticRunResult:
    output_directory: str
    status: str
    success: bool
    provider_calls: int
    retry_count: int
    health_ok: bool
    aggregate: Mapping[str, Any]
    artifact_paths: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_directory": self.output_directory,
            "status": self.status,
            "success": self.success,
            "provider_calls": self.provider_calls,
            "retry_count": self.retry_count,
            "health_ok": self.health_ok,
            "aggregate": _body_free(self.aggregate),
            "artifact_paths": dict(self.artifact_paths),
        }


def _make_artifact(
    *,
    output_directory: Path,
    prior: PriorState,
    config_public: Mapping[str, Any],
    response_format_rationale: str,
    request: Mapping[str, Any],
    response: Optional[DiagnosticProviderResponse],
    parsed: ParsedDiagnostics,
    provider_calls: int,
    error_code: Optional[str],
    prior_input_label: str,
) -> DiagnosticRunResult:
    health_ok = bool(parsed.strict_complete and error_code is None)
    status = "diagnostic_available" if health_ok else "blocked"
    success = health_ok
    source = str((response.source if response else config_public.get("source")) or "unknown")
    model = str((response.model if response else config_public.get("model")) or FALLBACK_MODEL)
    latency = max(0.0, float(response.latency_ms if response else 0.0))
    input_tokens = max(0, int(response.input_tokens if response else 0))
    output_tokens = max(0, int(response.output_tokens if response else 0))
    request_sha = stable_hash(
        {
            "runner_schema_version": RUNNER_SCHEMA_VERSION,
            "stage_schema_version": STAGE_A_SCHEMA_VERSION,
            "stage": "A",
            "model": model,
            "source": source,
            "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
            "user_packet_sha256": stable_hash(request),
        }
    )
    ledger = {
        "phase": "synthetic_health_only",
        "page_id": str(request["page_id"]),
        "root_id": str(request["root_id"]),
        "status": "available" if health_ok else "blocked",
        "error_code": error_code,
        "provider_call": bool(provider_calls),
        "retry_count": MAX_RETRIES,
        "cache_hit": False,
        "request_sha256": request_sha,
        "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
        "user_packet_sha256": stable_hash(request),
        "source": source,
        "model": model,
        "latency_ms": latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "selected_opaque_refs": _selected_opaque_refs(request),
        "content_length": parsed.content_length,
        "reasoning_length": parsed.reasoning_length,
        "finish_reasons": list(response.finish_reasons) if response else [],
        "leading_shape": parsed.leading_shape,
        "fence_detected": parsed.fence_detected,
        "unique_json_object_count": parsed.unique_json_object_count,
        "strict_parse_code": parsed.strict_parse_code,
        "strict_validation_code": parsed.strict_validation_code,
        "output_sha256": parsed.output_sha256,
        "safe_public_field_names": list(_filter_response_field_names(response.response_fields)) if response else [],
    }
    errors = []
    if error_code:
        errors.append(
            {
                "phase": "synthetic_health_only",
                "error_code": error_code,
                "model": model,
                "source": source,
            }
        )
    diagnostic = _response_diagnostic_dict(response, parsed)
    aggregate: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "status": status,
        "success": success,
        "protocol_health_ok": health_ok,
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": MAX_RETRIES,
        "development_input_read": False,
        "stage_a_development": False,
        "stage_b_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "prior_state": prior.to_dict(),
        "provider": {
            **dict(config_public),
            "model": model,
            "source": source,
            "calls": provider_calls,
            "within_call_limit": provider_calls <= MAX_PROVIDER_CALLS,
        },
        "response_format": {
            "mode": str(config_public.get("response_format_mode") or RESPONSE_FORMAT_OMITTED),
            "sent": bool(config_public.get("response_format_sent")),
            "predeclared": True,
            "rationale": response_format_rationale,
        },
        "health_request": {
            "page_id": str(request["page_id"]),
            "root_id": str(request["root_id"]),
            "input_token_proxy": _token_proxy(request, STAGE_A_SYSTEM_PROMPT),
            "input_token_proxy_limit": HEALTH_MAX_INPUT_PROXY,
            "output_limit": HEALTH_MAX_OUTPUT_TOKENS,
            "request_sha256": request_sha,
            "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
            "user_packet_sha256": stable_hash(request),
        },
        "strict_result": _strict_result_dict(parsed),
        "diagnostic_candidate": _candidate_result_dict(parsed),
        "response_diagnostics": diagnostic,
        "cost": {
            "provider_calls": provider_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency,
            "retry_count": MAX_RETRIES,
        },
        "errors": {"count": len(errors), "codes": sorted({str(row["error_code"]) for row in errors})},
        "input_artifact_read": prior_input_label,
    }
    cost = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "provider_calls": provider_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency,
        "retry_count": MAX_RETRIES,
        "limits": {
            "max_provider_calls": MAX_PROVIDER_CALLS,
            "max_retries": MAX_RETRIES,
            "health_input_proxy": HEALTH_MAX_INPUT_PROXY,
            "max_output_tokens": HEALTH_MAX_OUTPUT_TOKENS,
        },
        "response_format_sent": bool(config_public.get("response_format_sent")),
    }
    manifest: Dict[str, Any] = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "local_day": LOCAL_DAY,
        "output_directory_name": output_directory.name,
        "status": status,
        "success": success,
        "protocol_health_ok": health_ok,
        "provider_called": bool(provider_calls),
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "retry_count": MAX_RETRIES,
        "development_input_read": False,
        "stage_a_development": False,
        "stage_b_pilot": False,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": {**dict(config_public), "model": model, "source": source},
        "response_format": {
            "mode": str(config_public.get("response_format_mode") or RESPONSE_FORMAT_OMITTED),
            "sent": bool(config_public.get("response_format_sent")),
            "predeclared": True,
            "rationale": response_format_rationale,
        },
        "prior_k11_artifact_directory_name": Path(prior_input_label).name,
        "fallback_capability_artifact": prior.fallback_capability_artifact,
        "output_files": dict(OUTPUT_FILENAMES),
    }
    outputs: Dict[str, Any] = {
        "aggregate": aggregate,
        "cost": cost,
        "diagnostic": {
            "schema_version": REPORT_SCHEMA_VERSION,
            "artifact_version": ARTIFACT_VERSION,
            "strict_result": _strict_result_dict(parsed),
            "diagnostic_candidate": _candidate_result_dict(parsed),
            "response_diagnostics": diagnostic,
        },
        "ledger": [ledger],
        "errors": errors,
    }
    for label, value in outputs.items():
        _assert_body_free(value, label=label)
    _assert_body_free(manifest, label="manifest")
    output_directory.mkdir(parents=True, exist_ok=False)
    _write_json(output_directory / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_directory / OUTPUT_FILENAMES["cost"], cost)
    _write_json(output_directory / OUTPUT_FILENAMES["diagnostic"], outputs["diagnostic"])
    _write_jsonl(output_directory / OUTPUT_FILENAMES["ledger"], outputs["ledger"])
    _write_jsonl(output_directory / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _sha256_file(output_directory / filename)
        for key, filename in OUTPUT_FILENAMES.items()
        if key != "manifest"
    }
    _write_json(output_directory / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_directory / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return DiagnosticRunResult(
        output_directory=str(output_directory),
        status=status,
        success=success,
        provider_calls=provider_calls,
        retry_count=MAX_RETRIES,
        health_ok=health_ok,
        aggregate=aggregate,
        artifact_paths=paths,
    )


def run_linear_stage_a_protocol_diagnostic(
    output_directory: Union[str, Path] = DEFAULT_ARTIFACT_DIRECTORY,
    *,
    k11_artifact_directory: Union[str, Path] = DEFAULT_K11_ARTIFACT_DIRECTORY,
    capability_directories: Sequence[Union[str, Path]] = DEFAULT_CAPABILITY_DIRECTORIES,
    settings_path: Union[str, Path] = DEFAULT_SETTINGS_PATH,
    provider: Optional[DiagnosticStageProvider] = None,
    config: Optional[DiagnosticProviderConfig] = None,
    clock: Optional[Callable[[], float]] = None,
) -> DiagnosticRunResult:
    """Run one bounded synthetic protocol diagnostic and write an immutable artifact."""

    output_root = _safe_path(output_directory)
    if output_root.exists():
        raise FileExistsError("diagnostic_output_is_immutable")
    request = _health_request()
    prior = inspect_prior_state(k11_artifact_directory, capability_directories)
    rationale = (
        "Predeclared omitted response_format: the existing non-vision "
        "deepseek-v4-flash capability artifact records successful health with "
        "response_format_sent=false; K11 vision-exp json_object health failed "
        "as provider_invalid_json."
    )
    selected_model = prior.fallback_model or FALLBACK_MODEL
    selected_config = config or DiagnosticProviderConfig.from_workbench_settings(
        settings_path,
        model_override=selected_model,
        response_format_mode=RESPONSE_FORMAT_OMITTED,
        response_format_rationale=rationale,
    )
    if selected_config.model != FALLBACK_MODEL:
        # A caller may inject credentials, but may not redirect this diagnostic
        # to the failed vision model or another unverified model.
        prior = PriorState(
            **{**prior.__dict__, "errors": tuple(dict.fromkeys(prior.errors + ("diagnostic_model_override_refused",)))}
        )
    config_public = selected_config.public_dict()
    provider_object: Optional[DiagnosticStageProvider] = provider
    if provider_object is None and prior.allowed and selected_config.configured:
        provider_object = DeepSeekProtocolDiagnosticProvider(selected_config, clock=clock)
    provider_calls = 0
    response: Optional[DiagnosticProviderResponse] = None
    parsed = _empty_diagnostics("not_run")
    error_code: Optional[str] = None
    input_proxy = _token_proxy(request, STAGE_A_SYSTEM_PROMPT)
    if input_proxy > HEALTH_MAX_INPUT_PROXY:
        error_code = "health_input_proxy_exceeded"
    elif not prior.allowed:
        error_code = prior.errors[0] if prior.errors else "prior_gate_blocked"
    elif provider_object is None:
        error_code = "provider_unconfigured"
    elif not bool(getattr(provider_object, "configured", True)):
        error_code = "provider_unconfigured"
    elif str(getattr(provider_object, "model_id", FALLBACK_MODEL)) != FALLBACK_MODEL:
        error_code = "diagnostic_model_override_refused"
    else:
        provider_calls = 1
        try:
            raw_response = provider_object.complete(
                "A",
                STAGE_A_SYSTEM_PROMPT,
                request,
                max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
            )
            response = _coerce_provider_response(raw_response, provider_object)
            parsed = _parse_diagnostics(response, request)
            if response.input_tokens > HEALTH_MAX_INPUT_PROXY:
                error_code = "health_input_tokens_exceeded"
            elif response.output_tokens > HEALTH_MAX_OUTPUT_TOKENS:
                error_code = "health_output_tokens_exceeded"
            elif not parsed.strict_complete:
                error_code = parsed.strict_parse_code if parsed.strict_parse_code != "ok" else parsed.strict_validation_code
        except Exception as exc:
            error_code = _safe_error_code(exc)
            parsed = _empty_diagnostics(error_code)
    # Strictly no retry branch exists.  The provider is called at most once,
    # and this runner never receives or opens a development input directory.
    return _make_artifact(
        output_directory=output_root,
        prior=prior,
        config_public=config_public,
        response_format_rationale=rationale,
        request=request,
        response=response,
        parsed=parsed,
        provider_calls=provider_calls,
        error_code=error_code,
        prior_input_label=str(k11_artifact_directory),
    )


run_stage_a_protocol_diagnostic = run_linear_stage_a_protocol_diagnostic


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--k11-artifact-directory", type=Path, default=DEFAULT_K11_ARTIFACT_DIRECTORY)
    parser.add_argument("--settings-path", type=Path, default=DEFAULT_SETTINGS_PATH)
    args = parser.parse_args(argv)
    result = run_linear_stage_a_protocol_diagnostic(
        args.output_directory,
        k11_artifact_directory=args.k11_artifact_directory,
        settings_path=args.settings_path,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "success": result.success,
                "health_ok": result.health_ok,
                "provider_calls": result.provider_calls,
                "retry_count": result.retry_count,
                "output_directory": result.output_directory,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


__all__ = [
    "ARTIFACT_VERSION",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "STAGE_A_SCHEMA_VERSION",
    "STAGE_A_SYSTEM_PROMPT",
    "FALLBACK_MODEL",
    "K11_MODEL",
    "MAX_PROVIDER_CALLS",
    "MAX_RETRIES",
    "HEALTH_MAX_INPUT_PROXY",
    "HEALTH_MAX_OUTPUT_TOKENS",
    "BODY_KEYS",
    "DiagnosticProtocolError",
    "DiagnosticProviderError",
    "DiagnosticProviderConfig",
    "DiagnosticProviderResponse",
    "DeepSeekProtocolDiagnosticProvider",
    "DiagnosticRunResult",
    "PriorState",
    "_health_request",
    "_leading_shape",
    "_json_object_spans",
    "_parse_diagnostics",
    "inspect_prior_state",
    "validate_stage_a_request",
    "validate_stage_a_output",
    "run_linear_stage_a_protocol_diagnostic",
    "run_stage_a_protocol_diagnostic",
]


if __name__ == "__main__":
    raise SystemExit(main())
