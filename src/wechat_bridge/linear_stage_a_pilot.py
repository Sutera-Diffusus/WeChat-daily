"""K11 real DeepSeek Stage-A pilot for the K8 linear development artifact.

This module is an intentionally isolated, read-only provider harness.  It
performs one synthetic Stage-A health request first and, only after that
request has returned a locally validated payload, reads the complete pages of
``linear_stage_packet_development_v2``.  At most five development pages are
then sent, one request per page and without retries.  Stage B/C, frozen data,
production state, event code, and frontend surfaces are outside this module.

The provider surface is deliberately smaller than the canonical semantic
contract.  DeepSeek receives opaque handles plus bounded ``material`` text;
it never receives the K8 store, its 17-field claim schema, or the private
artifact ledgers.  Responses are parsed and validated in memory.  Only
successful, handle-only topic decisions are cached and written to the pilot
artifact.  Failed responses remain pending and are never cached.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Protocol, Sequence, Set, Tuple, Union

from .linear_stage_packets import LINEAR_STAGE_PACKET_VERSION, canonical_json, stable_hash
from .settings import WorkbenchSettings
from .staged_deepseek_analyzer import (
    OpenAICompatibleStageModel,
    StageModelResponse,
    StageProviderError,
    _strict_json,
)


PILOT_ARTIFACT_VERSION = "linear_stage_a_pilot_v1"
REPORT_SCHEMA_VERSION = "linear_stage_a_pilot_report_v1"
RUNNER_SCHEMA_VERSION = "linear_stage_a_pilot_runner_v1"
INPUT_ARTIFACT_VERSION = "linear_stage_packet_development_v2"
LOCAL_DAY = "2026-08-25"
MAX_PROVIDER_CALLS = 6
MAX_DEVELOPMENT_CALLS = 5
HEALTH_MAX_INPUT_PROXY = 900
HEALTH_MAX_OUTPUT_TOKENS = 300
DEVELOPMENT_MAX_OUTPUT_TOKENS = 300
CACHE_PREFIX_LENGTH = 16

STAGE_A_SCHEMA_VERSION = "stage_a_topic_map_small_v1"
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

OUTPUT_FILENAMES: Dict[str, str] = {
    "manifest": "manifest.private.json",
    "aggregate": "aggregate.private.json",
    "cost": "cost.private.json",
    "ledger": "ledger.private.jsonl",
    "selection": "selection.private.jsonl",
    "decisions": "decisions.private.jsonl",
    "errors": "errors.private.jsonl",
}

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
REQUEST_TOP_KEYS = frozenset(
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
OUTPUT_TOP_KEYS = frozenset({"topics"})
OUTPUT_TOPIC_KEYS = frozenset(
    {"topic_id", "message_handles", "candidate_handles", "evidence_handles", "relation"}
)
CATEGORY_NAMES = (
    "greeting_new_topic",
    "no_reply",
    "pronoun_person_object_state",
    "topic_shift",
    "candidate_competition",
)


class PilotProtocolError(ValueError):
    """Stable body-free local protocol error."""

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
    return value


def _canonical(value: Any) -> str:
    return canonical_json(value)


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
        output: Dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.casefold() in BODY_KEYS and child not in (None, "", [], (), {}):
                continue
            output[key] = _body_free(child)
        return output
    if isinstance(value, (list, tuple)):
        return [_body_free(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return [_body_free(child) for child in sorted(value, key=str)]
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


def _safe_text(value: Any, code: str, *, max_length: int = 160) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > max_length:
        raise PilotProtocolError(code)
    if any(ord(character) < 32 for character in value):
        raise PilotProtocolError(code)
    return value


def _list_of_strings(value: Any, code: str, *, allow_empty: bool = True) -> List[str]:
    if type(value) is not list or (not allow_empty and not value):
        raise PilotProtocolError(code)
    result: List[str] = []
    for item in value:
        result.append(_safe_text(item, code, max_length=400))
    if len(result) != len(set(result)):
        raise PilotProtocolError(code + "_duplicate")
    return result


def _scope_pair(scope: Any) -> Tuple[str, str]:
    if not isinstance(scope, Mapping):
        raise PilotProtocolError("scope_shape")
    account = scope.get("account_id", scope.get("account"))
    chat = scope.get("chat_id", scope.get("chat"))
    account = _safe_text(account, "scope_account", max_length=120)
    chat = _safe_text(chat, "scope_chat", max_length=120)
    return account, chat


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
        raise PilotProtocolError(code)


def _token_proxy(value: Any, system_prompt: str = "") -> int:
    return (len(_canonical(value)) + len(str(system_prompt)) + 3) // 4


def _user_token_proxy(value: Any) -> int:
    return (len(_canonical(value)) + 3) // 4


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_mapping(value: Any, code: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PilotProtocolError(code)
    return {str(key): value[key] for key in value}


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ValueError("input_file_missing") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_input_jsonl_line_%d" % line_number) from exc
            if not isinstance(value, Mapping):
                raise ValueError("input_jsonl_row_not_object")
            yield {str(key): value[key] for key in value}


@dataclass(frozen=True)
class PilotProviderConfig:
    """Private provider config; ``api_key`` is excluded from repr/equality."""

    model: str
    base_url: Optional[str]
    api_key: Optional[str] = field(default=None, repr=False, compare=False)
    timeout_seconds: Optional[float] = field(default=None, repr=False, compare=False)

    @classmethod
    def from_workbench_settings(cls, settings_path: Union[str, Path]) -> "PilotProviderConfig":
        settings = WorkbenchSettings(str(settings_path))
        # This is the one deliberate secret boundary.  The snapshot is kept
        # in local scope and only the key is passed to the lazy provider.
        snapshot = settings.snapshot(include_secrets=True)
        if not isinstance(snapshot, Mapping):
            raise ValueError("settings_snapshot_invalid")
        ai = snapshot.get("ai") if isinstance(snapshot.get("ai"), Mapping) else {}
        environment_key = os.environ.get("OPENAI_API_KEY") or ""
        environment_url = os.environ.get("OPENAI_BASE_URL") or ""
        model = str(ai.get("model") or "deepseek-v4-flash").strip()[:120] or "deepseek-v4-flash"
        base_url = str(ai.get("base_url") or environment_url or "").strip().rstrip("/") or None
        api_key = str(ai.get("api_key") or environment_key or "").strip() or None
        return cls(model=model, base_url=base_url, api_key=api_key)

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
            "response_format_mode": "json_object" if self.base_url else "compatibility_default",
        }


class PilotStageProvider(Protocol):
    source: str
    model_id: str

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> Union[StageModelResponse, Mapping[str, Any]]:
        ...


class DeepSeekStageAProvider:
    """Thin response-format-aware adapter around the existing lazy adapter."""

    source = "deepseek-openai-compatible"

    def __init__(self, config: PilotProviderConfig, *, client: Any = None) -> None:
        self.config = config
        self._adapter = _PilotOpenAICompatibleStageModel(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            client=client,
            timeout_seconds=config.timeout_seconds,
        )
        self.model_id = config.model

    @property
    def configured(self) -> bool:
        return self._adapter.configured

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> StageModelResponse:
        return self._adapter.complete(
            stage,
            system_prompt,
            user_packet,
            max_output_tokens=max_output_tokens,
        )


class _PilotOpenAICompatibleStageModel(OpenAICompatibleStageModel):
    """Use JSON mode for the configured DeepSeek OpenAI-compatible gateway."""

    source = "deepseek-openai-compatible"

    def complete(
        self,
        stage: str,
        system_prompt: str,
        user_packet: Mapping[str, Any],
        *,
        max_output_tokens: int,
    ) -> StageModelResponse:
        client = self._get_client()
        started = self._clock()
        request: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_json(user_packet)},
            ],
            "max_tokens": int(max_output_tokens),
            "temperature": 0,
        }
        # WorkbenchSettings.base_url identifies the OpenAI-compatible path.
        # JSON mode is the narrowest available compatibility contract; the
        # fixed schema remains in the prompt and is enforced locally below.
        if self.base_url:
            request["response_format"] = {"type": "json_object"}
        try:
            response = client.chat.completions.create(**request)
        except Exception as exc:
            raise StageProviderError("provider_request_failed") from exc
        elapsed = max(0.0, (self._clock() - started) * 1000.0)
        try:
            choice = response.choices[0]
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None)
            if content is None and isinstance(message, Mapping):
                content = message.get("content")
            payload = _strict_json(content)
        except StageProviderError:
            raise
        except Exception as exc:
            raise StageProviderError("provider_response_shape") from exc
        usage = getattr(response, "usage", None)
        if isinstance(usage, Mapping):
            input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
        else:
            input_tokens = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0))
            output_tokens = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0))
        try:
            input_tokens = max(0, int(input_tokens or 0))
            output_tokens = max(0, int(output_tokens or 0))
        except (TypeError, ValueError):
            input_tokens = 0
            output_tokens = 0
        return StageModelResponse(
            payload=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed,
            model=self.model_id,
            request_id=str(getattr(response, "id", "") or ""),
        )


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status: str
    source: str
    model: str
    request_sha256: str
    input_tokens: int
    output_tokens: int
    input_token_proxy: int
    output_limit: int
    latency_ms: float
    response_format_mode: str
    provider_call: bool
    error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "status": self.status,
            "source": self.source,
            "model": self.model,
            "request_sha256": self.request_sha256,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "input_token_proxy": int(self.input_token_proxy),
            "output_limit": int(self.output_limit),
            "latency_ms": float(self.latency_ms),
            "response_format_mode": self.response_format_mode,
            "provider_call": bool(self.provider_call),
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class PilotLedgerRecord:
    phase: str
    page_id: str
    root_id: str
    status: str
    error_code: Optional[str]
    input_tokens: int
    output_tokens: int
    latency_ms: float
    request_sha256: str
    system_prompt_sha256: str
    user_packet_sha256: str
    source: str
    model: str
    cache_hit: bool
    provider_call: bool
    selected_opaque_refs: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "page_id": self.page_id,
            "root_id": self.root_id,
            "status": self.status,
            "error_code": self.error_code,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "latency_ms": float(self.latency_ms),
            "request_sha256": self.request_sha256,
            "system_prompt_sha256": self.system_prompt_sha256,
            "user_packet_sha256": self.user_packet_sha256,
            "source": self.source,
            "model": self.model,
            "cache_hit": bool(self.cache_hit),
            "provider_call": bool(self.provider_call),
            "selected_opaque_refs": _body_free(dict(self.selected_opaque_refs)),
        }


@dataclass(frozen=True)
class PageRun:
    page: Mapping[str, Any]
    materialized: Mapping[str, Any]
    request: Mapping[str, Any]
    request_sha256: str
    status: str
    payload: Optional[Mapping[str, Any]]
    errors: Tuple[str, ...]
    cache_hit: bool
    provider_call: bool
    input_tokens: int
    output_tokens: int
    latency_ms: float
    categories: Tuple[str, ...]
    source: str
    model: str


@dataclass(frozen=True)
class PilotRunResult:
    input_directory: str
    output_directory: str
    status: str
    success: bool
    health: HealthResult
    selected_page_count: int
    provider_calls: int
    artifact_paths: Mapping[str, str]
    aggregate: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "status": self.status,
            "success": bool(self.success),
            "health": self.health.to_dict(),
            "selected_page_count": int(self.selected_page_count),
            "provider_calls": int(self.provider_calls),
            "artifact_paths": dict(self.artifact_paths),
            "aggregate": _body_free(self.aggregate),
        }


def validate_stage_a_request(user_packet: Mapping[str, Any]) -> None:
    """Validate the narrow provider input before every request."""

    packet = _as_mapping(user_packet, "stage_a_request_shape")
    if set(packet) != set(REQUEST_TOP_KEYS):
        raise PilotProtocolError("stage_a_request_keys")
    if packet.get("schema_version") != STAGE_A_SCHEMA_VERSION or packet.get("stage") != "A":
        raise PilotProtocolError("stage_a_request_version")
    scope = _scope_pair(packet.get("scope"))
    root_id = _safe_text(packet.get("root_id"), "stage_a_request_root_id")
    page_id = _safe_text(packet.get("page_id"), "stage_a_request_page_id", max_length=400)
    if not root_id or not page_id:
        raise PilotProtocolError("stage_a_request_identity")
    message_handles = _list_of_strings(packet.get("message_handles"), "stage_a_request_messages", allow_empty=False)
    candidate_handles = _list_of_strings(packet.get("candidate_handles"), "stage_a_request_candidates")
    evidence_handles = _list_of_strings(packet.get("evidence_handles"), "stage_a_request_evidence")
    for handle in message_handles + candidate_handles + evidence_handles:
        _assert_handle_scope(handle, scope, "stage_a_request_cross_scope")
    materials = packet.get("materials")
    if type(materials) is not list or len(materials) != len(message_handles):
        raise PilotProtocolError("stage_a_request_materials")
    material_handles: List[str] = []
    for row in materials:
        item = _as_mapping(row, "stage_a_request_message_material_shape")
        if set(item) != set(MESSAGE_MATERIAL_KEYS):
            raise PilotProtocolError("stage_a_request_message_material_keys")
        handle = _safe_text(item.get("message_handle"), "stage_a_request_message_handle", max_length=400)
        _safe_text(item.get("message_id"), "stage_a_request_message_id", max_length=200)
        roles = _list_of_strings(item.get("roles"), "stage_a_request_roles")
        material = item.get("material")
        if type(material) is not str or len(material) > 512:
            raise PilotProtocolError("stage_a_request_material_text")
        if any(key in REQUEST_FORBIDDEN_KEYS for key in item):
            raise PilotProtocolError("stage_a_request_17_field")
        material_handles.append(handle)
        _ = roles
    if material_handles != message_handles:
        raise PilotProtocolError("stage_a_request_material_alignment")
    candidate_materials = packet.get("candidate_materials")
    if type(candidate_materials) is not list or len(candidate_materials) != len(candidate_handles):
        raise PilotProtocolError("stage_a_request_candidate_materials")
    candidate_material_handles: List[str] = []
    for row in candidate_materials:
        item = _as_mapping(row, "stage_a_request_candidate_material_shape")
        if set(item) != set(CANDIDATE_MATERIAL_KEYS):
            raise PilotProtocolError("stage_a_request_candidate_material_keys")
        handle = _safe_text(item.get("candidate_handle"), "stage_a_request_candidate_handle", max_length=400)
        candidate_material_handles.append(handle)
        related_messages = _list_of_strings(item.get("message_handles"), "stage_a_request_candidate_messages")
        related_evidence = _list_of_strings(item.get("evidence_handles"), "stage_a_request_candidate_evidence")
        if not set(related_messages) <= set(message_handles) or not set(related_evidence) <= set(evidence_handles):
            raise PilotProtocolError("stage_a_request_candidate_scope")
        relation_type = item.get("relation_type")
        if relation_type is not None and type(relation_type) is not str:
            raise PilotProtocolError("stage_a_request_candidate_relation")
        material = item.get("material")
        if not isinstance(material, Mapping):
            raise PilotProtocolError("stage_a_request_candidate_material")
        if set(material) - {"views", "reason_codes", "uncertainties"}:
            raise PilotProtocolError("stage_a_request_candidate_material_keys")
        for key in ("views", "reason_codes", "uncertainties"):
            _list_of_strings(material.get(key, []), "stage_a_request_candidate_%s" % key)
        if any(key in REQUEST_FORBIDDEN_KEYS for key in item) or any(
            key in REQUEST_FORBIDDEN_KEYS for key in material
        ):
            raise PilotProtocolError("stage_a_request_17_field")
    if candidate_material_handles != candidate_handles:
        raise PilotProtocolError("stage_a_request_candidate_alignment")


def validate_stage_a_output(value: Any, user_packet: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly validate one small Stage-A response and return a copy."""

    validate_stage_a_request(user_packet)
    root = _as_mapping(value, "stage_a_output_shape")
    if set(root) != set(OUTPUT_TOP_KEYS):
        raise PilotProtocolError("stage_a_output_keys")
    topics = root.get("topics")
    if type(topics) is not list or not topics or len(topics) > len(user_packet["message_handles"]):
        raise PilotProtocolError("stage_a_output_topics")
    allowed_messages = set(str(item) for item in user_packet["message_handles"])
    allowed_candidates = set(str(item) for item in user_packet["candidate_handles"])
    allowed_evidence = set(str(item) for item in user_packet["evidence_handles"])
    seen_topics: Set[str] = set()
    seen_messages: Set[str] = set()
    seen_candidates: Set[str] = set()
    seen_evidence: Set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for row in topics:
        item = _as_mapping(row, "stage_a_output_topic_shape")
        if set(item) != set(OUTPUT_TOPIC_KEYS):
            raise PilotProtocolError("stage_a_output_topic_keys")
        topic_id = _safe_text(item.get("topic_id"), "stage_a_output_topic_id", max_length=120)
        if topic_id in seen_topics:
            raise PilotProtocolError("stage_a_output_duplicate_topic")
        seen_topics.add(topic_id)
        messages = _list_of_strings(item.get("message_handles"), "stage_a_output_messages", allow_empty=False)
        candidates = _list_of_strings(item.get("candidate_handles"), "stage_a_output_candidates")
        evidence = _list_of_strings(item.get("evidence_handles"), "stage_a_output_evidence")
        relation = item.get("relation")
        if relation not in RELATIONS:
            raise PilotProtocolError("stage_a_output_relation")
        if not set(messages) <= allowed_messages:
            raise PilotProtocolError("stage_a_output_message_scope")
        if not set(candidates) <= allowed_candidates:
            raise PilotProtocolError("stage_a_output_candidate_scope")
        if not set(evidence) <= allowed_evidence:
            raise PilotProtocolError("stage_a_output_evidence_scope")
        if seen_messages.intersection(messages) or seen_candidates.intersection(candidates) or seen_evidence.intersection(evidence):
            raise PilotProtocolError("stage_a_output_duplicate_handle")
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
        raise PilotProtocolError("stage_a_output_message_coverage")
    if seen_candidates != allowed_candidates:
        raise PilotProtocolError("stage_a_output_candidate_coverage")
    return {"topics": normalized}


def _provider_error_code(exc: BaseException) -> str:
    if isinstance(exc, StageProviderError):
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


def _response_parts(value: Any, provider: Any) -> Tuple[Any, int, int, float, str]:
    if isinstance(value, StageModelResponse):
        return (
            value.payload,
            max(0, int(value.input_tokens or 0)),
            max(0, int(value.output_tokens or 0)),
            max(0.0, float(value.latency_ms or 0.0)),
            str(value.model or getattr(provider, "model_id", "unknown")),
        )
    if isinstance(value, Mapping):
        return (
            value,
            max(0, int(value.get("input_tokens", 0) or 0)),
            max(0, int(value.get("output_tokens", 0) or 0)),
            max(0.0, float(value.get("latency_ms", 0.0) or 0.0)),
            str(value.get("model") or getattr(provider, "model_id", "unknown")),
        )
    raise StageProviderError("provider_response_object")


def _request_sha256(provider: Any, user_packet: Mapping[str, Any]) -> str:
    return stable_hash(
        {
            "runner_schema_version": RUNNER_SCHEMA_VERSION,
            "stage_schema_version": STAGE_A_SCHEMA_VERSION,
            "stage": "A",
            "model": str(getattr(provider, "model_id", "unknown")),
            "source": str(getattr(provider, "source", "unknown")),
            "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
            "user_packet_sha256": stable_hash(user_packet),
        }
    )


def _selected_opaque_refs(packet: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "root_id": str(packet.get("root_id", "")),
        "page_id": str(packet.get("page_id", "")),
        "scope": deepcopy(packet.get("scope", {})),
        "message_handles": list(packet.get("message_handles", ())),
        "candidate_handles": list(packet.get("candidate_handles", ())),
        "evidence_handles": list(packet.get("evidence_handles", ())),
    }


def _material_body(content_table: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    bodies: List[str] = []
    for handle in record.get("content_handles", ()) if isinstance(record.get("content_handles"), (list, tuple)) else ():
        row = content_table.get(str(handle))
        if isinstance(row, Mapping) and isinstance(row.get("body"), str):
            bodies.append(row["body"])
    # A single bounded material field is intentional.  This is transient
    # provider input and never appears in any pilot artifact.
    return " ".join(bodies)[:512]


def _message_material(store: Mapping[str, Any], page: Mapping[str, Any]) -> List[Dict[str, Any]]:
    messages = {
        str(row.get("message_handle")): row
        for row in store.get("messages", ())
        if isinstance(row, Mapping) and row.get("message_handle") not in (None, "")
    }
    content = store.get("content_table") if isinstance(store.get("content_table"), Mapping) else {}
    output: List[Dict[str, Any]] = []
    for handle in page.get("message_handles", ()):
        value = messages.get(str(handle), {})
        output.append(
            {
                "message_handle": str(handle),
                "message_id": str(value.get("message_id", "")),
                "roles": [str(item) for item in value.get("roles", ())],
                "material": _material_body(content, value),
            }
        )
    return output


def _candidate_material(store: Mapping[str, Any], page: Mapping[str, Any]) -> List[Dict[str, Any]]:
    candidates = {
        str(row.get("candidate_handle")): row
        for row in store.get("candidates", ())
        if isinstance(row, Mapping) and row.get("candidate_handle") not in (None, "")
    }
    message_by_id = {
        str(row.get("message_id")): str(row.get("message_handle"))
        for row in store.get("messages", ())
        if isinstance(row, Mapping) and row.get("message_id") not in (None, "")
    }
    page_messages = set(str(item) for item in page.get("message_handles", ()))
    page_evidence = set(str(item) for item in page.get("evidence_handles", ()))
    output: List[Dict[str, Any]] = []
    for handle in page.get("candidate_handles", ()):
        value = candidates.get(str(handle), {})
        related = [message_by_id[str(item)] for item in value.get("message_ids", ()) if str(item) in message_by_id]
        related = [item for item in related if item in page_messages]
        evidence = [str(item) for item in value.get("evidence_handle_refs", ()) if str(item) in page_evidence]
        views = [str(item) for item in value.get("view_names", ())]
        reasons = [str(item) for item in value.get("candidate_reason", ())]
        uncertainties = [str(item) for item in value.get("uncertainties", ())]
        output.append(
            {
                "candidate_handle": str(handle),
                "message_handles": related,
                "evidence_handles": evidence,
                "relation_type": str(value.get("relation_subtype") or value.get("relation_label") or "candidate_only"),
                "material": {"views": views, "reason_codes": reasons, "uncertainties": uncertainties},
            }
        )
    return output


def _build_stage_a_request(store: Mapping[str, Any], page: Mapping[str, Any]) -> Dict[str, Any]:
    scope = page.get("scope") if isinstance(page.get("scope"), Mapping) else {}
    packet = {
        "schema_version": STAGE_A_SCHEMA_VERSION,
        "stage": "A",
        "root_id": str(page.get("root_id", "")),
        "page_id": str(page.get("page_id", "")),
        "scope": {"account_id": str(scope.get("account_id", "")), "chat_id": str(scope.get("chat_id", ""))},
        "message_handles": [str(item) for item in page.get("message_handles", ())],
        "materials": _message_material(store, page),
        "candidate_handles": [str(item) for item in page.get("candidate_handles", ())],
        "candidate_materials": _candidate_material(store, page),
        "evidence_handles": [str(item) for item in page.get("evidence_handles", ())],
    }
    validate_stage_a_request(packet)
    return packet


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


def _page_flags(page: Mapping[str, Any], store: Mapping[str, Any]) -> Tuple[str, ...]:
    message_rows = {
        str(row.get("message_handle")): row
        for row in store.get("messages", ())
        if isinstance(row, Mapping) and row.get("message_handle") not in (None, "")
    }
    candidate_rows = {
        str(row.get("candidate_handle")): row
        for row in store.get("candidates", ())
        if isinstance(row, Mapping) and row.get("candidate_handle") not in (None, "")
    }
    selected_messages = [message_rows.get(str(handle), {}) for handle in page.get("message_handles", ())]
    selected_candidates = [candidate_rows.get(str(handle), {}) for handle in page.get("candidate_handles", ())]
    views = {
        str(view)
        for row in selected_candidates
        for view in row.get("view_names", ()) if isinstance(row.get("view_names"), (list, tuple))
    }
    reasons = {
        str(reason).casefold()
        for row in selected_candidates
        for reason in row.get("candidate_reason", ()) if isinstance(row.get("candidate_reason"), (list, tuple))
    }
    identities = [row.get("identity_row", {}) for row in selected_messages if isinstance(row.get("identity_row"), Mapping)]
    flags: Set[str] = set()
    if len(page.get("candidate_handles", ())) >= 10 or len(views) >= 5:
        flags.add("candidate_competition")
    if {"candidate_person_history", "candidate_object_history", "candidate_state_history"} <= views:
        flags.add("pronoun_person_object_state")
    if any(
        bool(row.get(key))
        for row in identities
        for key in ("is_opener", "topic_shift", "new_topic")
    ) or any(
        str(row.get("fragment_type", "")).casefold() in {"conversation_opener", "greeting"}
        for row in identities
    ) or "new_topic" in reasons:
        flags.add("greeting_new_topic")
    has_reply = any(
        bool(row.get("identity_row", {}).get("reply_to_message_id"))
        for row in selected_messages
        if isinstance(row.get("identity_row"), Mapping)
    ) or any("explicit_reply" in reasons for _ in (0,))
    if not has_reply and ({"time_proximity_weak", "same_segment_weak"} & reasons):
        flags.add("no_reply")
    segments = {
        str(row.get("identity_row", {}).get("segment_id"))
        for row in selected_messages
        if isinstance(row.get("identity_row"), Mapping) and row.get("identity_row", {}).get("segment_id") not in (None, "")
    }
    if any(bool(row.get("identity_row", {}).get("topic_shift")) for row in selected_messages if isinstance(row.get("identity_row"), Mapping)) or len(segments) > 1:
        flags.add("topic_shift")
    return tuple(category for category in CATEGORY_NAMES if category in flags)


def _select_pages(pages: Sequence[Mapping[str, Any]], store: Mapping[str, Any]) -> List[Tuple[Mapping[str, Any], Tuple[str, ...]]]:
    decorated = [(page, _page_flags(page, store)) for page in pages]
    selected: List[Tuple[Mapping[str, Any], Tuple[str, ...]]] = []
    used: Set[str] = set()
    # Priority keeps the strongest available strata first.  Missing strata
    # remain explicit in the report rather than being fabricated.
    for category in ("candidate_competition", "pronoun_person_object_state", "topic_shift", "greeting_new_topic", "no_reply"):
        candidates = [item for item in decorated if category in item[1] and str(item[0].get("page_id")) not in used]
        candidates.sort(
            key=lambda item: (
                -len(item[0].get("candidate_handles", ())),
                -len(item[0].get("message_handles", ())),
                str(item[0].get("page_id", "")),
            )
        )
        if candidates:
            selected.append(candidates[0])
            used.add(str(candidates[0][0].get("page_id", "")))
    remaining = [item for item in decorated if str(item[0].get("page_id", "")) not in used]
    remaining.sort(
        key=lambda item: (
            -len(item[0].get("candidate_handles", ())),
            -len(item[0].get("message_handles", ())),
            str(item[0].get("page_id", "")),
        )
    )
    for item in remaining:
        if len(selected) >= MAX_DEVELOPMENT_CALLS:
            break
        selected.append(item)
    return selected[:MAX_DEVELOPMENT_CALLS]


def _execute_once(
    provider: PilotStageProvider,
    *,
    phase: str,
    page: Mapping[str, Any],
    request: Mapping[str, Any],
    cache: MutableMapping[str, Mapping[str, Any]],
    allow_cache: bool,
) -> Tuple[Optional[Mapping[str, Any]], PilotLedgerRecord, Tuple[str, ...]]:
    request_hash = _request_sha256(provider, request)
    page_id = str(page.get("page_id", request.get("page_id", "")))
    root_id = str(page.get("root_id", request.get("root_id", "")))
    refs = _selected_opaque_refs(request)
    if allow_cache and request_hash in cache:
        cached = deepcopy(dict(cache[request_hash]))
        try:
            payload = validate_stage_a_output(cached, request)
        except PilotProtocolError:
            # An invalid cache entry is not trusted and is not allowed to
            # turn into a provider call in this one-shot runner.
            cache.pop(request_hash, None)
        else:
            ledger = PilotLedgerRecord(
                phase=phase,
                page_id=page_id,
                root_id=root_id,
                status="complete",
                error_code=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0.0,
                request_sha256=request_hash,
                system_prompt_sha256=stable_hash(STAGE_A_SYSTEM_PROMPT),
                user_packet_sha256=stable_hash(request),
                source="stage_a_cache",
                model=str(getattr(provider, "model_id", "unknown")),
                cache_hit=True,
                provider_call=False,
                selected_opaque_refs=refs,
            )
            return payload, ledger, ()
    started = time.perf_counter()
    try:
        response = provider.complete(
            "A",
            STAGE_A_SYSTEM_PROMPT,
            request,
            max_output_tokens=DEVELOPMENT_MAX_OUTPUT_TOKENS,
        )
        payload_raw, input_tokens, output_tokens, response_latency, response_model = _response_parts(response, provider)
        payload = validate_stage_a_output(payload_raw, request)
        latency = response_latency or max(0.0, (time.perf_counter() - started) * 1000.0)
        cache[request_hash] = deepcopy(payload)
        ledger = PilotLedgerRecord(
            phase=phase,
            page_id=page_id,
            root_id=root_id,
            status="complete",
            error_code=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
            request_sha256=request_hash,
            system_prompt_sha256=stable_hash(STAGE_A_SYSTEM_PROMPT),
            user_packet_sha256=stable_hash(request),
            source=str(getattr(provider, "source", "unknown")),
            model=response_model,
            cache_hit=False,
            provider_call=True,
            selected_opaque_refs=refs,
        )
        return payload, ledger, ()
    except PilotProtocolError as exc:
        code = exc.code
    except Exception as exc:
        code = _provider_error_code(exc)
    latency = max(0.0, (time.perf_counter() - started) * 1000.0)
    ledger = PilotLedgerRecord(
        phase=phase,
        page_id=page_id,
        root_id=root_id,
        status="pending",
        error_code=code,
        input_tokens=0,
        output_tokens=0,
        latency_ms=latency,
        request_sha256=request_hash,
        system_prompt_sha256=stable_hash(STAGE_A_SYSTEM_PROMPT),
        user_packet_sha256=stable_hash(request),
        source=str(getattr(provider, "source", "unknown")),
        model=str(getattr(provider, "model_id", "unknown")),
        cache_hit=False,
        provider_call=True,
        selected_opaque_refs=refs,
    )
    return None, ledger, (code,)


def _run_health(provider: PilotStageProvider) -> Tuple[HealthResult, PilotLedgerRecord, Tuple[str, ...]]:
    request = _health_request()
    request_hash = _request_sha256(provider, request)
    input_proxy = _token_proxy(request, STAGE_A_SYSTEM_PROMPT)
    refs = _selected_opaque_refs(request)
    source = str(getattr(provider, "source", "unknown"))
    model = str(getattr(provider, "model_id", "unknown"))
    response_format = "json_object" if bool(getattr(getattr(provider, "config", None), "base_url", None)) else "compatibility_default"
    if input_proxy > HEALTH_MAX_INPUT_PROXY:
        health = HealthResult(
            False,
            "blocked",
            source,
            model,
            request_hash,
            0,
            0,
            input_proxy,
            HEALTH_MAX_OUTPUT_TOKENS,
            0.0,
            response_format,
            False,
            "health_input_proxy_exceeded",
        )
        ledger = PilotLedgerRecord(
            "health", str(request["page_id"]), str(request["root_id"]), "pending", health.error_code, 0, 0, 0.0,
            request_hash, stable_hash(STAGE_A_SYSTEM_PROMPT), stable_hash(request), source, model, False, False, refs,
        )
        return health, ledger, (health.error_code or "health_failed",)
    if not bool(getattr(provider, "configured", True)):
        health = HealthResult(
            False,
            "blocked",
            source,
            model,
            request_hash,
            0,
            0,
            input_proxy,
            HEALTH_MAX_OUTPUT_TOKENS,
            0.0,
            response_format,
            False,
            "provider_unconfigured",
        )
        ledger = PilotLedgerRecord(
            "health", str(request["page_id"]), str(request["root_id"]), "pending", health.error_code, 0, 0, 0.0,
            request_hash, stable_hash(STAGE_A_SYSTEM_PROMPT), stable_hash(request), source, model, False, False, refs,
        )
        return health, ledger, (health.error_code or "health_failed",)
    started = time.perf_counter()
    try:
        response = provider.complete(
            "A",
            STAGE_A_SYSTEM_PROMPT,
            request,
            max_output_tokens=HEALTH_MAX_OUTPUT_TOKENS,
        )
        payload_raw, input_tokens, output_tokens, response_latency, response_model = _response_parts(response, provider)
        payload = validate_stage_a_output(payload_raw, request)
        latency = response_latency or max(0.0, (time.perf_counter() - started) * 1000.0)
        if input_tokens and input_tokens > HEALTH_MAX_INPUT_PROXY:
            raise PilotProtocolError("health_input_tokens_exceeded")
        if output_tokens and output_tokens > HEALTH_MAX_OUTPUT_TOKENS:
            raise PilotProtocolError("health_output_tokens_exceeded")
        health = HealthResult(
            True,
            "available",
            source,
            response_model,
            request_hash,
            input_tokens or input_proxy,
            output_tokens,
            input_proxy,
            HEALTH_MAX_OUTPUT_TOKENS,
            latency,
            response_format,
            True,
            None,
        )
        ledger = PilotLedgerRecord(
            "health", str(request["page_id"]), str(request["root_id"]), "complete", None,
            health.input_tokens, health.output_tokens, latency, request_hash, stable_hash(STAGE_A_SYSTEM_PROMPT),
            stable_hash(request), source, response_model, False, True, refs,
        )
        # Health is deliberately not read from cache: a real availability
        # probe must precede any development read in every invocation.
        return health, ledger, ()
    except PilotProtocolError as exc:
        code = exc.code
    except Exception as exc:
        code = _provider_error_code(exc)
    latency = max(0.0, (time.perf_counter() - started) * 1000.0)
    health = HealthResult(
        False,
        "blocked",
        source,
        model,
        request_hash,
        0,
        0,
        input_proxy,
        HEALTH_MAX_OUTPUT_TOKENS,
        latency,
        response_format,
        True,
        code,
    )
    ledger = PilotLedgerRecord(
        "health", str(request["page_id"]), str(request["root_id"]), "pending", code, 0, 0, latency,
        request_hash, stable_hash(STAGE_A_SYSTEM_PROMPT), stable_hash(request), source, model, False, True, refs,
    )
    return health, ledger, (code,)


def _guard_input(input_directory: Union[str, Path]) -> Path:
    root = Path(input_directory).expanduser().resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in root.parts):
        raise ValueError("pilot_refuses_frozen_input")
    if root.name.casefold() != "linear_stage_packet_development_v2":
        raise ValueError("pilot_requires_linear_stage_packet_development_v2")
    if not root.is_dir():
        raise ValueError("pilot_input_directory_missing")
    return root


def _read_v2_after_health(root: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    manifest_path = root / "manifest.private.json"
    pages_path = root / "pages.private.jsonl"
    materialized_path = root / "materialized_map.private.jsonl"
    store_path = root / "store.private.json"
    if not all(path.is_file() for path in (manifest_path, pages_path, materialized_path, store_path)):
        raise ValueError("pilot_v2_input_files_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("pilot_v2_manifest_invalid")
    if manifest.get("artifact_version") != INPUT_ARTIFACT_VERSION or manifest.get("split") not in (None, "development"):
        raise ValueError("pilot_v2_manifest_wrong_artifact")
    if manifest.get("local_day") not in (None, LOCAL_DAY) or manifest.get("frozen_read") is True:
        raise ValueError("pilot_v2_manifest_wrong_day_or_frozen")
    if manifest.get("provider_called") is True or int(manifest.get("provider_calls") or 0) != 0:
        raise ValueError("pilot_v2_manifest_provider_boundary")
    pages = list(_iter_jsonl(pages_path))
    materialized = list(_iter_jsonl(materialized_path))
    if len(pages) != len(materialized) or not pages:
        raise ValueError("pilot_v2_page_material_alignment")
    material_by_page = {str(row.get("page_id")): row for row in materialized}
    page_out: List[Dict[str, Any]] = []
    for page in pages:
        page_id = str(page.get("page_id") or "")
        material = material_by_page.get(page_id)
        if not page_id or material is None:
            raise ValueError("pilot_v2_page_missing_material")
        if material.get("status") != "complete" or material.get("stage_a", {}).get("status") != "complete":
            continue
        if material.get("stage_b_status") not in (None, "N/A") or material.get("stage_c_status") not in (None, "N/A"):
            raise ValueError("pilot_v2_stage_b_c_boundary")
        if material.get("within_limits") is False or material.get("stage_a", {}).get("material_stats", {}).get("within_limits") is False:
            continue
        if page.get("root_id") != material.get("root_id"):
            raise ValueError("pilot_v2_page_root_mismatch")
        page_out.append({"page": page, "materialized": material})
    store = json.loads(store_path.read_text(encoding="utf-8"))
    if not isinstance(store, Mapping):
        raise ValueError("pilot_v2_store_invalid")
    return page_out, dict(store), dict(manifest)


def _coverage(page_runs: Sequence[PageRun]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    category_rows: Dict[str, Dict[str, int]] = {
        category: {"selected_pages": 0, "complete_pages": 0, "pending_pages": 0}
        for category in CATEGORY_NAMES
    }
    message_expected = message_bound = candidate_expected = candidate_bound = evidence_expected = evidence_bound = 0
    bindings: List[Dict[str, Any]] = []
    for run in page_runs:
        for category in run.categories:
            category_rows[category]["selected_pages"] += 1
            category_rows[category]["complete_pages"] += run.status == "complete"
            category_rows[category]["pending_pages"] += run.status != "complete"
        message_handles = [str(item) for item in run.request.get("message_handles", ())]
        candidate_handles = [str(item) for item in run.request.get("candidate_handles", ())]
        evidence_handles = [str(item) for item in run.request.get("evidence_handles", ())]
        message_expected += len(message_handles)
        candidate_expected += len(candidate_handles)
        evidence_expected += len(evidence_handles)
        if run.payload is None:
            continue
        topics = list(run.payload.get("topics", ()))
        bound_messages = {str(handle) for topic in topics for handle in topic.get("message_handles", ())}
        bound_candidates = {str(handle) for topic in topics for handle in topic.get("candidate_handles", ())}
        bound_evidence = {str(handle) for topic in topics for handle in topic.get("evidence_handles", ())}
        message_bound += len(bound_messages)
        candidate_bound += len(bound_candidates)
        evidence_bound += len(bound_evidence)
        bindings.extend(
            {
                "page_id": str(run.page.get("page_id", "")),
                "root_id": str(run.page.get("root_id", "")),
                "topic_id": str(topic.get("topic_id", "")),
                "relation": str(topic.get("relation", "unknown")),
                "message_handles": list(topic.get("message_handles", ())),
                "candidate_handles": list(topic.get("candidate_handles", ())),
                "evidence_handles": list(topic.get("evidence_handles", ())),
            }
            for topic in topics
        )
    topic_coverage = {
        "category_flags": category_rows,
        "pages": {
            "selected": len(page_runs),
            "complete": sum(run.status == "complete" for run in page_runs),
            "pending": sum(run.status != "complete" for run in page_runs),
        },
        "message_handles": {"expected": message_expected, "bound": message_bound, "rate": message_bound / message_expected if message_expected else 1.0},
        "candidate_handles": {"expected": candidate_expected, "bound": candidate_bound, "rate": candidate_bound / candidate_expected if candidate_expected else 1.0},
    }
    evidence_bindings = {
        "expected_handles": evidence_expected,
        "bound_handles": evidence_bound,
        "rate": evidence_bound / evidence_expected if evidence_expected else 1.0,
        "topic_binding_count": len(bindings),
        "bindings": bindings,
    }
    return topic_coverage, evidence_bindings


def _cache_stability(ledger: Sequence[PilotLedgerRecord]) -> Dict[str, Any]:
    prefixes = [row.request_sha256[:CACHE_PREFIX_LENGTH] for row in ledger if row.request_sha256]
    return {
        "prefix_length": CACHE_PREFIX_LENGTH,
        "prefixes": prefixes,
        "stable": all(len(prefix) == CACHE_PREFIX_LENGTH for prefix in prefixes),
        "unique_prefix_count": len(set(prefixes)),
    }


def _make_artifacts(
    *,
    input_root_label: str,
    output_root: Path,
    health: HealthResult,
    health_ledger: PilotLedgerRecord,
    health_errors: Sequence[str],
    page_runs: Sequence[PageRun],
    provider_config_public: Mapping[str, Any],
    input_manifest: Optional[Mapping[str, Any]],
    development_input_read: bool,
) -> Tuple[Dict[str, str], Dict[str, Any], str, bool, int]:
    ledger = [health_ledger] + [
        PilotLedgerRecord(
            phase="development",
            page_id=str(run.page.get("page_id", "")),
            root_id=str(run.page.get("root_id", "")),
            status=run.status,
            error_code=run.errors[0] if run.errors else None,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            latency_ms=run.latency_ms,
            request_sha256=run.request_sha256,
            system_prompt_sha256=stable_hash(STAGE_A_SYSTEM_PROMPT),
            user_packet_sha256=stable_hash(run.request),
            source=run.source or ("stage_a_cache" if run.cache_hit else "deepseek-openai-compatible"),
            model=run.model or str(provider_config_public.get("model", "unknown")),
            cache_hit=run.cache_hit,
            provider_call=run.provider_call,
            selected_opaque_refs=_selected_opaque_refs(run.request),
        )
        for run in page_runs
    ]
    topic_coverage, evidence_bindings = _coverage(page_runs)
    provider_calls = sum(bool(row.provider_call) for row in ledger)
    input_tokens = sum(int(row.input_tokens) for row in ledger)
    output_tokens = sum(int(row.output_tokens) for row in ledger)
    pending_errors = list(health_errors) + [error for run in page_runs for error in run.errors]
    all_complete = bool(health.ok) and bool(page_runs) and all(run.status == "complete" for run in page_runs) and len(page_runs) == MAX_DEVELOPMENT_CALLS
    status = "complete" if all_complete else ("blocked" if not health.ok else "partial")
    success = all_complete
    cache_stability = _cache_stability(ledger)
    decisions = []
    for run in page_runs:
        if run.payload is None:
            continue
        decisions.append(
            {
                "page_id": str(run.page.get("page_id", "")),
                "root_id": str(run.page.get("root_id", "")),
                "status": run.status,
                "cache_hit": bool(run.cache_hit),
                "topics": deepcopy(list(run.payload.get("topics", ()))),
            }
        )
    selection = [
        {
            "selection_rank": index + 1,
            "page_id": str(run.page.get("page_id", "")),
            "root_id": str(run.page.get("root_id", "")),
            "source_packet_id": str(run.page.get("source_packet_id") or run.page.get("root_id", "")),
            "scope": deepcopy(run.page.get("scope", {})),
            "categories": list(run.categories),
            "status": run.status,
            "message_count": len(run.request.get("message_handles", ())),
            "candidate_count": len(run.request.get("candidate_handles", ())),
            "evidence_count": len(run.request.get("evidence_handles", ())),
        }
        for index, run in enumerate(page_runs)
    ]
    errors = [
        {"phase": "health", "page_id": health_ledger.page_id, "root_id": health_ledger.root_id, "error_code": error}
        for error in health_errors
    ] + [
        {"phase": "development", "page_id": str(run.page.get("page_id", "")), "root_id": str(run.page.get("root_id", "")), "error_code": error}
        for run in page_runs
        for error in run.errors
    ]
    aggregate: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": PILOT_ARTIFACT_VERSION,
        "runner_schema_version": RUNNER_SCHEMA_VERSION,
        "success": success,
        "status": status,
        "input_artifact_version": INPUT_ARTIFACT_VERSION,
        "input_directory_name": Path(input_root_label).name,
        "development_input_read": development_input_read,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider": {
            **dict(provider_config_public),
            "calls": provider_calls,
            "call_limit": MAX_PROVIDER_CALLS,
            "within_call_limit": provider_calls <= MAX_PROVIDER_CALLS,
        },
        "health": health.to_dict(),
        "topic_coverage": topic_coverage,
        "evidence_bindings": evidence_bindings,
        "cost": {
            "provider_calls": provider_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round(sum(row.latency_ms for row in ledger), 3),
            "health_input_token_proxy": health.input_token_proxy,
            "health_output_limit": HEALTH_MAX_OUTPUT_TOKENS,
            "development_calls": sum(row.phase == "development" and row.provider_call for row in ledger),
            "cached_completions": sum(row.cache_hit for row in ledger),
        },
        "cache_prefix_stability": cache_stability,
        "errors": {"count": len(errors), "codes": sorted(set(pending_errors))},
        "input_manifest_digest": str((input_manifest or {}).get("input_selected_digest", "")),
    }
    cost = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": PILOT_ARTIFACT_VERSION,
        "provider_calls": provider_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": round(sum(row.latency_ms for row in ledger), 3),
        "health": health.to_dict(),
        "limits": {
            "max_provider_calls": MAX_PROVIDER_CALLS,
            "max_development_calls": MAX_DEVELOPMENT_CALLS,
            "health_input_proxy": HEALTH_MAX_INPUT_PROXY,
            "max_output_tokens": DEVELOPMENT_MAX_OUTPUT_TOKENS,
        },
        "cache_prefix_stability": cache_stability,
    }
    manifest: Dict[str, Any] = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "artifact_version": PILOT_ARTIFACT_VERSION,
        "input_artifact_version": INPUT_ARTIFACT_VERSION,
        "input_directory_name": Path(input_root_label).name,
        "output_directory_name": output_root.name,
        "local_day": LOCAL_DAY,
        "split": "development",
        "development_input_read": development_input_read,
        "frozen_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "provider_called": bool(provider_calls),
        "provider_calls": provider_calls,
        "provider_call_limit": MAX_PROVIDER_CALLS,
        "selected_page_count": len(page_runs),
        "status": status,
        "success": success,
        "provider": dict(provider_config_public),
        "system_prompt_sha256": stable_hash(STAGE_A_SYSTEM_PROMPT),
        "system_prompt_chars": len(STAGE_A_SYSTEM_PROMPT),
        "output_files": dict(OUTPUT_FILENAMES),
    }
    all_outputs: Dict[str, Any] = {
        "aggregate": aggregate,
        "cost": cost,
        "ledger": [row.to_dict() for row in ledger],
        "selection": selection,
        "decisions": decisions,
        "errors": errors,
    }
    for label, value in all_outputs.items():
        _assert_body_free(value, label=label)
    _assert_body_free(manifest, label="manifest")
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(output_root / OUTPUT_FILENAMES["aggregate"], aggregate)
    _write_json(output_root / OUTPUT_FILENAMES["cost"], cost)
    _write_jsonl(output_root / OUTPUT_FILENAMES["ledger"], all_outputs["ledger"])
    _write_jsonl(output_root / OUTPUT_FILENAMES["selection"], selection)
    _write_jsonl(output_root / OUTPUT_FILENAMES["decisions"], decisions)
    _write_jsonl(output_root / OUTPUT_FILENAMES["errors"], errors)
    manifest["artifact_hashes"] = {
        filename: _sha256_file(output_root / filename)
        for filename in OUTPUT_FILENAMES.values()
        if filename != OUTPUT_FILENAMES["manifest"]
    }
    _write_json(output_root / OUTPUT_FILENAMES["manifest"], manifest)
    paths = {key: str(output_root / filename) for key, filename in OUTPUT_FILENAMES.items()}
    return paths, aggregate, status, success, provider_calls


def run_linear_stage_a_pilot(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    settings_path: Union[str, Path] = Path("data/workbench_settings.json"),
    provider: Optional[PilotStageProvider] = None,
    cache: Optional[MutableMapping[str, Mapping[str, Any]]] = None,
    config: Optional[PilotProviderConfig] = None,
) -> PilotRunResult:
    """Run the bounded K11 pilot and write a new immutable private artifact.

    The input directory is not resolved or opened until the synthetic health
    request succeeds.  This ordering is deliberate: a failed health probe
    cannot accidentally read development messages or leak their hashes.
    """

    output_root = Path(output_directory).expanduser().resolve()
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test"} for part in output_root.parts):
        raise ValueError("pilot_refuses_frozen_output")
    if output_root.exists():
        raise FileExistsError("pilot_output_is_immutable")
    if provider is None:
        selected_config = config or PilotProviderConfig.from_workbench_settings(settings_path)
        provider = DeepSeekStageAProvider(selected_config)
        provider_public = selected_config.public_dict()
    else:
        selected_config = config
        provider_public = (
            selected_config.public_dict()
            if selected_config is not None
            else {
                "provider": "injected",
                "source": str(getattr(provider, "source", "unknown")),
                "model": str(getattr(provider, "model_id", "unknown")),
                "base_url_configured": False,
                "api_key_configured": True,
                "response_format_mode": "synthetic",
            }
        )
    cache_store: MutableMapping[str, Mapping[str, Any]] = cache if cache is not None else {}
    health, health_ledger, health_errors = _run_health(provider)
    page_runs: List[PageRun] = []
    input_root_label = str(input_directory)
    input_manifest: Optional[Mapping[str, Any]] = None
    development_input_read = False
    if health.ok:
        input_root = _guard_input(input_directory)
        page_rows, store, input_manifest = _read_v2_after_health(input_root)
        selected = _select_pages([row["page"] for row in page_rows], store)
        if len(selected) != MAX_DEVELOPMENT_CALLS:
            raise ValueError("pilot_requires_five_complete_v2_pages")
        development_input_read = True
        material_by_page = {str(row["page"].get("page_id")): row["materialized"] for row in page_rows}
        for page, categories in selected:
            request = _build_stage_a_request(store, page)
            payload, ledger, errors = _execute_once(
                provider,
                phase="development",
                page=page,
                request=request,
                cache=cache_store,
                allow_cache=True,
            )
            page_runs.append(
                PageRun(
                    page=page,
                    materialized=material_by_page[str(page.get("page_id"))],
                    request=request,
                    request_sha256=ledger.request_sha256,
                    status=ledger.status,
                    payload=payload,
                    errors=tuple(errors),
                    cache_hit=ledger.cache_hit,
                    provider_call=ledger.provider_call,
                    input_tokens=ledger.input_tokens,
                    output_tokens=ledger.output_tokens,
                    latency_ms=ledger.latency_ms,
                    categories=tuple(categories),
                    source=ledger.source,
                    model=ledger.model,
                )
            )
    paths, aggregate, status, success, provider_calls = _make_artifacts(
        input_root_label=input_root_label,
        output_root=output_root,
        health=health,
        health_ledger=health_ledger,
        health_errors=health_errors,
        page_runs=page_runs,
        provider_config_public=provider_public,
        input_manifest=input_manifest,
        development_input_read=development_input_read,
    )
    return PilotRunResult(
        input_directory=input_root_label,
        output_directory=str(output_root),
        status=status,
        success=success,
        health=health,
        selected_page_count=len(page_runs),
        provider_calls=provider_calls,
        artifact_paths=paths,
        aggregate=aggregate,
    )


run_stage_a_pilot = run_linear_stage_a_pilot


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-directory",
        type=Path,
        default=Path("data/private/gold_standard/2026-08-25/linear_stage_packet_development_v2"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("data/private/gold_standard/2026-08-25/linear_stage_a_pilot_v1"),
    )
    parser.add_argument("--settings-path", type=Path, default=Path("data/workbench_settings.json"))
    args = parser.parse_args(argv)
    result = run_linear_stage_a_pilot(args.input_directory, args.output_directory, settings_path=args.settings_path)
    print(
        json.dumps(
            {
                "status": result.status,
                "success": result.success,
                "health_ok": result.health.ok,
                "selected_page_count": result.selected_page_count,
                "provider_calls": result.provider_calls,
                "output_directory": result.output_directory,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


__all__ = [
    "CACHE_PREFIX_LENGTH",
    "CATEGORY_NAMES",
    "DeepSeekStageAProvider",
    "HealthResult",
    "INPUT_ARTIFACT_VERSION",
    "MAX_DEVELOPMENT_CALLS",
    "MAX_PROVIDER_CALLS",
    "OUTPUT_FILENAMES",
    "PilotLedgerRecord",
    "PilotProviderConfig",
    "PilotProtocolError",
    "PilotRunResult",
    "REPORT_SCHEMA_VERSION",
    "RUNNER_SCHEMA_VERSION",
    "STAGE_A_SCHEMA_VERSION",
    "STAGE_A_SYSTEM_PROMPT",
    "run_linear_stage_a_pilot",
    "run_stage_a_pilot",
    "validate_stage_a_output",
    "validate_stage_a_request",
]


if __name__ == "__main__":
    raise SystemExit(main())
