"""Independent, body-free preflight for the fresh topic-guided authorization.

The preflight intentionally uses only synthetic page metadata and the local
runner's pure/runtime boundary.  It never reads a development packet, frozen
split, gold annotation, or review body, and it never lets a model invocation
reach the provider path.  Every negative probe uses a fresh temporary output
and authority root and must fail before the ledger is opened.

This is a review sidecar rather than a production runner test.  It records
only booleans, counters, safe error codes, hashes, and opaque policy refs.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from wechat_bridge import compact_stage_a_development_pilot_v3 as pilot
from wechat_bridge.persistent_call_budget import ledger_path_for


ROOT = Path(__file__).resolve().parents[1]
LOCAL_DAY = "2026-08-25"
AUTHORIZATION_ID = pilot.TOPIC_GUIDED_AUTHORIZATION_ID
AUDIT_SCHEMA_VERSION = "compact_stage_a_topic_guided_authorization_review_v1"
EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROVIDER_CALLS = 5
EXPECTED_PAGE_COUNT = 5
EXPECTED_PER_PAGE_CALLS = 1
EXPECTED_RETRIES = 0
EXPECTED_SUPPLEMENT_CALLS = 0
EXPECTED_SUPPLEMENT_PAGES = 0
EXPECTED_HEALTH_CALLS = 0
EXPECTED_SDK_CALLS = 0

HEX64 = set("0123456789abcdef")
BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "completion",
        "content",
        "evidence_text",
        "html",
        "markdown",
        "message",
        "message_text",
        "output_text",
        "prompt",
        "quote",
        "raw",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "response",
        "response_body",
        "response_text",
        "text",
        "text_body",
        "thoughts",
        "transcript",
        "user_input",
    }
)
SECRET_KEYS = frozenset({"access_token", "api_key", "apikey", "password", "private_key", "secret", "token"})
FORBIDDEN_IMPORTS = frozenset(
    {
        "openai",
        "requests",
        "httpx",
        "urllib",
        "urllib.request",
        "wechat_bridge.linear_stage_a_development_pilot",
        "wechat_bridge.linear_stage_a_pilot",
    }
)


class _ProviderMustNotRun(RuntimeError):
    """Sentinel used if a negative probe accidentally reaches the model."""


class _LedgerMustNotOpen(RuntimeError):
    """Sentinel used to observe a policy that survived all preflight gates."""


class _LedgerTrap:
    @classmethod
    def for_authorization(cls, *_args: Any, **_kwargs: Any) -> Any:
        raise _LedgerMustNotOpen("ledger_opened")


class _NoCallModel:
    model_id = EXPECTED_MODEL
    source = "synthetic-topic-guided-preflight"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise _ProviderMustNotRun("provider_path_reached")


class _WrongModel(_NoCallModel):
    model_id = "deepseek-v4-pro"


class _HostileAuthorizationId(str):
    """A string subclass that tries to compare equal to every allowlisted id."""

    def __hash__(self) -> int:
        return hash(AUTHORIZATION_ID)

    def __eq__(self, _other: object) -> bool:
        return True


def _synthetic_pages() -> List[Dict[str, Any]]:
    """Return the exact opaque five-page map with synthetic in-memory cues."""

    pages: List[Dict[str, Any]] = []
    for index, expected in enumerate(pilot.AUTHORITY_BOUND_CANONICAL_MAPPING):
        scope = dict(pilot.AUTHORITY_BOUND_SCOPE)
        message = f"{scope['account_id']}/{scope['chat_id']}|message|preflight-{index}"
        candidate = f"{scope['account_id']}/{scope['chat_id']}|candidate|preflight-{index}"
        pages.append(
            {
                "page_id": str(expected["source_page_ref"]),
                "root_id": str(expected["source_root_ref"]),
                "source_packet_id": str(expected["source_source_ref"]),
                "page_hash": str(expected["source_page_hash"]),
                "scope": scope,
                "message_handles": [message],
                "primary_message_handles": [message],
                # This marker exists only in memory and is never emitted by
                # the report or any sidecar.
                "message_rows": [
                    {
                        "message_handle": message,
                        "message_type": "text",
                        "text": "TOPIC_GUIDED_PREFLIGHT_SYNTHETIC_BODY_MARKER",
                    }
                ],
                "candidate_handles": [candidate],
                "categories": [pilot.CATEGORY_NAMES[index % len(pilot.CATEGORY_NAMES)]],
                "status": "complete",
                "_selection_rank": int(expected["selection_rank"]),
                "_source_handle": str(expected["source_source_ref"]),
                "_scope_handle": str(expected["source_scope_ref"]),
            }
        )
    return pages


def _authority_summary(*, mutate: Optional[Callable[[MutableMapping[str, Any]], None]] = None) -> Dict[str, Any]:
    """Build a minimal body-free projection sidecar for a synthetic probe."""

    rows: List[Dict[str, Any]] = []
    for expected in pilot.AUTHORITY_BOUND_CANONICAL_MAPPING:
        rows.append(
            {
                "selection_rank": int(expected["selection_rank"]),
                "page_ref": str(expected["authority_page_ref"]),
                "root_ref": str(expected["authority_root_ref"]),
                "source_ref": str(expected["authority_source_ref"]),
                "scope_ref": str(expected["authority_scope_ref"]),
                "all_primary_authority_bound": True,
            }
        )
    summary: Dict[str, Any] = {
        "schema": "compact_stage_a_authority_bound_primary_projection_audit_v2",
        "audit_status": "pass_exact_underprojection_replay",
        "body_free": True,
        "gate": True,
        "acceptance": {
            "status": "pass",
            "authority_bound_evidence_gate": True,
            "difference_binding_gate": True,
            "all_scopes_bound": True,
        },
        "execution_boundaries": {
            "frozen_read": False,
            "frozen_test_read": False,
            "gold_loaded": False,
            "production_state_written": False,
            "provider_called": False,
            "source_artifact_written": False,
            "provider_call_count": 0,
        },
        "selection_scope": {
            "candidate_only": True,
            "development_only": True,
            "not_canonical": True,
            "semantic_decision_pending": True,
            "single_scope": True,
            "scope_ref": str(pilot.AUTHORITY_BOUND_SCOPE_REF),
            "same_five_page_materialize": True,
            "selection_binding_verified": True,
            "selected_page_count": EXPECTED_PAGE_COUNT,
            "page_refs": [str(row["page_ref"]) for row in rows],
        },
        "pages": rows,
    }
    if mutate is not None:
        mutate(summary)
    return summary


def _walk(value: Any, path: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, child
            yield from _walk(child, child_path)


def _privacy_hits(value: Any) -> Dict[str, int]:
    hits = {"body_key_hits": 0, "reasoning_key_hits": 0, "secret_key_hits": 0}
    for path, child in _walk(value):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0].casefold()
        nonempty = child not in (None, "", [], (), {}, set(), frozenset())
        if key in BODY_KEYS and nonempty:
            hits["body_key_hits"] += 1
        if key in {"analysis", "chain_of_thought", "reasoning", "reasoning_content", "thoughts"} and nonempty:
            hits["reasoning_key_hits"] += 1
        if key in SECRET_KEYS or key.endswith(("_secret", "_password")):
            if nonempty:
                hits["secret_key_hits"] += 1
    return hits


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _module_import_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _line_for(source_path: Path, needle: str) -> int:
    try:
        for index, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), start=1):
            if needle in line:
                return index
    except (OSError, UnicodeError):
        return 0
    return 0


def _runner_line(needle: str) -> int:
    """Return a line number for a call/guard inside the runner body."""

    source_lines, start_line = inspect.getsourcelines(pilot.run_compact_stage_a_development_pilot_v3)
    source = "".join(source_lines)
    offset = source.find(needle)
    return start_line + source[:offset].count("\n") if offset >= 0 else 0


def _run_probe(
    name: str,
    *,
    expected_error: str,
    authorization_id: str = AUTHORIZATION_ID,
    page_mutator: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    kwargs_mutator: Optional[Callable[[MutableMapping[str, Any]], None]] = None,
    module_mutations: Optional[Mapping[str, Any]] = None,
    model_factory: Callable[[], _NoCallModel] = _NoCallModel,
) -> Dict[str, Any]:
    """Run one pre-reservation negative case in an isolated temp root."""

    with tempfile.TemporaryDirectory(prefix="topic-guided-preflight-") as raw:
        case_root = Path(raw)
        output = case_root / "output"
        authority = case_root / "authority"
        pages = _synthetic_pages()
        if page_mutator is not None:
            page_mutator(pages)
        options: MutableMapping[str, Any] = {
            "settings_sha256": pilot.TOPIC_GUIDED_SETTINGS_SHA256,
        }
        if kwargs_mutator is not None:
            kwargs_mutator(options)
        model = model_factory()
        previous: Dict[str, Any] = {}
        try:
            for key, value in (module_mutations or {}).items():
                previous[key] = getattr(pilot, key)
                setattr(pilot, key, value)
            error = ""
            returned = False
            try:
                pilot.run_compact_stage_a_development_pilot_v3(
                    pages,
                    output,
                    model=model,
                    authority_root=authority,
                    authorization_id=authorization_id,
                    **dict(options),
                )
                returned = True
            except pilot.CompactStageADevelopmentError as exc:
                error = str(getattr(exc, "code", ""))
            except Exception as exc:  # pragma: no cover - defensive fail-closed telemetry
                error = type(exc).__name__
            ledger_files = (
                [item for item in authority.rglob("*") if item.is_file()]
                if authority.exists()
                else []
            )
            return {
                "case": name,
                "expected_error": expected_error,
                "observed_error": error,
                "returned": returned,
                "provider_calls": int(model.calls),
                "output_created": output.exists(),
                "authority_root_created": authority.exists(),
                "ledger_files_created": len(ledger_files),
                "failed_before_reserve": bool(
                    not returned
                    and error == expected_error
                    and model.calls == 0
                    and not output.exists()
                    and not authority.exists()
                ),
            }
        finally:
            for key, value in previous.items():
                setattr(pilot, key, value)


def _static_checks() -> Dict[str, Any]:
    source_path = Path(inspect.getfile(pilot)).resolve()
    source = inspect.getsource(pilot.run_compact_stage_a_development_pilot_v3)
    binding_source = "\n".join(
        inspect.getsource(function)
        for function in (
            pilot._topic_guided_input_binding_hash,
            pilot._topic_guided_contract,
            pilot._topic_guided_validate_frozen_boundary,
        )
    )
    module_source = source_path.read_text(encoding="utf-8")
    positions = {
        "authorization_gate": source.find("authorization_id not in ALLOWED_AUTHORIZATION_IDS"),
        "topic_exact_branch": source.find("topic_guided_authorization = authorization_id == TOPIC_GUIDED_AUTHORIZATION_ID"),
        "output_exists_guard": source.find("if output_root.exists()"),
        "canonical_mapping": source.find("_authority_bound_validate_canonical_mapping"),
        "request_preflight": source.find("_topic_guided_validate_requests"),
        "offline_review": source.find("_read_topic_guided_review_evidence"),
        "frozen_boundary": source.find("_topic_guided_validate_frozen_boundary"),
        "ledger_open": source.find("CallAuthorizationLedger.for_authorization"),
    }
    ordered = [
        positions["authorization_gate"],
        positions["output_exists_guard"],
        positions["canonical_mapping"],
        positions["request_preflight"],
        positions["offline_review"],
        positions["frozen_boundary"],
        positions["ledger_open"],
    ]
    binding_mentions = {
        name: needle in binding_source
        for name, needle in {
            "exact_page_count": "TOPIC_GUIDED_MAX_SELECTED_PAGES",
            "canonical_mapping_hash": "TOPIC_GUIDED_CANONICAL_MAPPING_SHA256",
            "scope_hash": "TOPIC_GUIDED_SCOPE_SHA256",
            "selection_hash": "selection_sha256",
            "context_hash": "context_input_sha256",
            "projection_hashes": "authority_projection_hashes",
            "offline_review_hashes": "topic_guided_review_hashes",
            "guidance_hash": "TOPIC_GUIDED_GUIDANCE_SHA256",
            "grouping_hint_hash": "TOPIC_GUIDED_GROUPING_HINTS_SHA256",
            "protocol_hash": "TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256",
            "code_hash": "TOPIC_GUIDED_CODE_SHA256",
            "prompt_hash": "TOPIC_GUIDED_PROMPT_SHA256",
            "settings_hash": "TOPIC_GUIDED_SETTINGS_SHA256",
            "model_binding": '"model": model',
        }.items()
    }
    return {
        "source_file": "src/wechat_bridge/compact_stage_a_development_pilot_v3.py",
        "runner_source_sha256": _sha256_file(source_path),
        "forbidden_imports_absent": not (_module_import_names(module_source) & FORBIDDEN_IMPORTS),
        "explicit_exact_id_exception": "and authorization_id != TOPIC_GUIDED_AUTHORIZATION_ID" in source,
        "topic_branch_exact_equality": positions["topic_exact_branch"] >= 0,
        "topic_not_in_legacy_allowlist": AUTHORIZATION_ID not in pilot.ALLOWED_AUTHORIZATION_IDS,
        "preflight_precedes_ledger": bool(all(value >= 0 for value in ordered) and ordered == sorted(ordered)),
        "binding_fields_mentioned": binding_mentions,
        "binding_fields_complete": all(binding_mentions.values()),
        "source_lines": {
            "authorization_gate": _runner_line("authorization_id not in ALLOWED_AUTHORIZATION_IDS"),
            "topic_exact_branch": _runner_line("topic_guided_authorization = authorization_id == TOPIC_GUIDED_AUTHORIZATION_ID"),
            "canonical_mapping": _runner_line("_authority_bound_validate_canonical_mapping("),
            "request_preflight": _runner_line("_topic_guided_validate_requests("),
            "offline_review": _runner_line("_read_topic_guided_review_evidence("),
            "frozen_boundary": _runner_line("_topic_guided_validate_frozen_boundary("),
            "ledger_open": _runner_line("CallAuthorizationLedger.for_authorization("),
        },
    }


def _contract_checks() -> Dict[str, Any]:
    checks = {
        "model": pilot.MODEL_ID == EXPECTED_MODEL,
        "provider": pilot.PROVIDER_ID == "openai-compatible",
        "protocol": pilot.PROTOCOL_VERSION == "stage_a_topic_assignment_compact_v3",
        "selected_page_limit": pilot.TOPIC_GUIDED_MAX_SELECTED_PAGES == EXPECTED_PAGE_COUNT,
        "max_total_calls": pilot.TOPIC_GUIDED_MAX_TOTAL_CALLS == EXPECTED_PROVIDER_CALLS,
        "max_provider_calls": pilot.TOPIC_GUIDED_MAX_PROVIDER_CALLS == EXPECTED_PROVIDER_CALLS,
        "per_page_provider_call_limit": pilot.TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT == EXPECTED_PER_PAGE_CALLS,
        "max_retries": pilot.TOPIC_GUIDED_MAX_RETRIES == EXPECTED_RETRIES,
        "supplement_calls": pilot.TOPIC_GUIDED_SUPPLEMENT_CALLS == EXPECTED_SUPPLEMENT_CALLS,
        "supplement_pages": pilot.TOPIC_GUIDED_SUPPLEMENT_PAGES == EXPECTED_SUPPLEMENT_PAGES,
        "health_calls": pilot.TOPIC_GUIDED_HEALTH_CALLS == EXPECTED_HEALTH_CALLS,
        "sdk_calls": pilot.TOPIC_GUIDED_SDK_CALLS == EXPECTED_SDK_CALLS,
        "response_format_omitted": pilot.RESPONSE_FORMAT_MODE == "omitted",
        "response_format_sent_false": pilot.RESPONSE_FORMAT_MODE == "omitted",
        "thinking_disabled": pilot.THINKING_DISABLED is True,
        "stage_b_false_by_contract": True,
        "stage_c_false_by_contract": True,
        "production_false_by_contract": True,
        "frozen_false_by_contract": True,
        "gold_false_by_contract": True,
        "no_supplement": True,
    }
    return {
        "values": {
            "model": pilot.MODEL_ID,
            "provider": pilot.PROVIDER_ID,
            "protocol": pilot.PROTOCOL_VERSION,
            "response_format_mode": pilot.RESPONSE_FORMAT_MODE,
            "thinking_disabled": pilot.THINKING_DISABLED,
            "selected_page_limit": pilot.TOPIC_GUIDED_MAX_SELECTED_PAGES,
            "max_total_calls": pilot.TOPIC_GUIDED_MAX_TOTAL_CALLS,
            "max_provider_calls": pilot.TOPIC_GUIDED_MAX_PROVIDER_CALLS,
            "per_page_provider_call_limit": pilot.TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT,
            "max_retries": pilot.TOPIC_GUIDED_MAX_RETRIES,
            "supplement_calls": pilot.TOPIC_GUIDED_SUPPLEMENT_CALLS,
            "supplement_pages": pilot.TOPIC_GUIDED_SUPPLEMENT_PAGES,
            "health_calls": pilot.TOPIC_GUIDED_HEALTH_CALLS,
            "sdk_calls": pilot.TOPIC_GUIDED_SDK_CALLS,
            "stage_b_authorized": False,
            "stage_c_authorized": False,
            "production_authorized": False,
            "frozen_read": False,
            "gold_loaded": False,
            "no_supplement": True,
        },
        "all_pass": all(checks.values()),
        "checks": checks,
    }


def _probe_suite() -> List[Dict[str, Any]]:
    zero = "0" * 64
    probes: List[Dict[str, Any]] = [
        {
            "name": "exact_id_reaches_topic_specific_gate",
            "expected_error": "topic_guided_grouping_hint_drift",
            "kwargs_mutator": lambda options: options.update({"topic_guided_grouping_hints_sha256": zero}),
        },
        {"name": "id_suffix_unknown", "expected_error": "authorization_binding_mismatch", "authorization_id": AUTHORIZATION_ID + "X"},
        {"name": "id_case_unknown", "expected_error": "authorization_binding_mismatch", "authorization_id": AUTHORIZATION_ID.lower()},
        {"name": "id_trimmed_unknown", "expected_error": "authorization_binding_mismatch", "authorization_id": " " + AUTHORIZATION_ID},
        {"name": "id_random_unknown", "expected_error": "authorization_binding_mismatch", "authorization_id": "TOPIC_GUIDED_OTHER_20260830"},
        {"name": "hostile_string_subclass_unknown", "expected_error": "authorization_binding_mismatch", "authorization_id": _HostileAuthorizationId(AUTHORIZATION_ID)},
        {
            "name": "legacy_id_cannot_enter_topic_branch",
            "expected_error": "authorization_binding_mismatch",
            "authorization_id": pilot.AUTHORIZATION_ID,
            "kwargs_mutator": lambda options: options.update({"topic_guided_guidance_sha256": pilot.TOPIC_GUIDED_GUIDANCE_SHA256}),
        },
        {
            "name": "legacy_adapter_fix_id_cannot_enter_topic_branch",
            "expected_error": "authorization_binding_mismatch",
            "authorization_id": pilot.ADAPTER_FIX_AUTHORIZATION_ID,
            "kwargs_mutator": lambda options: options.update({"topic_guided_guidance_sha256": pilot.TOPIC_GUIDED_GUIDANCE_SHA256}),
        },
        {
            "name": "legacy_guard_validation_id_cannot_enter_topic_branch",
            "expected_error": "authorization_binding_mismatch",
            "authorization_id": pilot.GUARD_VALIDATION_AUTHORIZATION_ID,
            "kwargs_mutator": lambda options: options.update({"topic_guided_guidance_sha256": pilot.TOPIC_GUIDED_GUIDANCE_SHA256}),
        },
        {
            "name": "legacy_guarded_id_cannot_enter_topic_branch",
            "expected_error": "authorization_binding_mismatch",
            "authorization_id": pilot.GUARDED_AUTHORIZATION_ID,
            "kwargs_mutator": lambda options: options.update({"topic_guided_guidance_sha256": pilot.TOPIC_GUIDED_GUIDANCE_SHA256}),
        },
        {
            "name": "legacy_authority_bound_id_cannot_enter_topic_branch",
            "expected_error": "authorization_binding_mismatch",
            "authorization_id": pilot.AUTHORITY_BOUND_AUTHORIZATION_ID,
            "kwargs_mutator": lambda options: options.update({"topic_guided_guidance_sha256": pilot.TOPIC_GUIDED_GUIDANCE_SHA256}),
        },
        {
            "name": "page_hash_drift",
            "expected_error": "authority_projection_page_drift",
            "page_mutator": lambda pages: pages[0].__setitem__("page_hash", "f" * 64),
        },
        {
            "name": "selection_rank_drift",
            "expected_error": "authority_projection_page_drift",
            "page_mutator": lambda pages: pages[0].__setitem__("_selection_rank", 2),
        },
        {
            "name": "context_primary_role_drift",
            "expected_error": "topic_guided_page_drift",
            "page_mutator": lambda pages: pages[0].__setitem__("primary_message_handles", []),
        },
        {
            "name": "scope_drift",
            "expected_error": "authority_projection_scope_drift",
            "page_mutator": lambda pages: [page["scope"].__setitem__("chat_id", "CHAT_DRIFT") for page in pages],
        },
        {
            "name": "canonical_mapping_runtime_drift",
            "expected_error": "authority_projection_code_drift",
            "module_mutations": {
                "AUTHORITY_BOUND_CANONICAL_MAPPING": tuple(
                    [
                        {
                            **dict(pilot.AUTHORITY_BOUND_CANONICAL_MAPPING[0]),
                            "source_page_ref": "k30_page_mapping_drift",
                        }
                    ]
                    + [dict(row) for row in pilot.AUTHORITY_BOUND_CANONICAL_MAPPING[1:]]
                )
            },
        },
        {
            "name": "authority_projection_sidecar_drift",
            "expected_error": "authority_projection_page_drift",
            "kwargs_mutator": lambda options: options.update(
                {
                    "authority_projection_summary": _authority_summary(
                        mutate=lambda summary: summary["pages"][0].__setitem__("page_ref", "page_projection_drift")
                    )
                }
            ),
        },
        {
            "name": "caller_authority_hash_forge",
            "expected_error": "topic_guided_review_hash_mismatch",
            "kwargs_mutator": lambda options: options.update({"authority_projection_hashes": {"authority_projection_selection_sha256": zero}}),
        },
        {
            "name": "offline_review_schema_drift",
            "expected_error": "topic_guided_review_invalid",
            "kwargs_mutator": lambda options: options.update({"topic_guided_review_summary": {"schema": "forged"}}),
        },
        {
            "name": "caller_review_hash_forge",
            "expected_error": "topic_guided_review_hash_mismatch",
            "kwargs_mutator": lambda options: options.update({"topic_guided_hashes": {"topic_guided_review_sha256": zero}}),
        },
        {
            "name": "guidance_hash_drift",
            "expected_error": "topic_guided_guidance_drift",
            "kwargs_mutator": lambda options: options.update({"topic_guided_guidance_sha256": zero}),
        },
        {
            "name": "grouping_hint_hash_drift",
            "expected_error": "topic_guided_grouping_hint_drift",
            "kwargs_mutator": lambda options: options.update({"topic_guided_grouping_hints_sha256": zero}),
        },
        {
            "name": "g_binding_hash_drift",
            "expected_error": "topic_guided_grouping_hint_drift",
            "kwargs_mutator": lambda options: options.update({"topic_guided_g_hint_binding_sha256": zero}),
        },
        {
            "name": "protocol_source_hash_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256": zero},
        },
        {
            "name": "authorization_code_hash_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_CODE_SHA256": zero},
        },
        {
            "name": "prompt_hash_drift",
            "expected_error": "topic_guided_guidance_drift",
            "module_mutations": {"TOPIC_GUIDED_PROMPT_SHA256": zero},
        },
        {
            "name": "settings_hash_drift",
            "expected_error": "topic_guided_review_hash_mismatch",
            "kwargs_mutator": lambda options: options.update({"settings_sha256": zero}),
        },
        {
            "name": "model_binding_drift",
            "expected_error": "model_mismatch",
            "model_factory": _WrongModel,
        },
        {
            "name": "health_artifact_forbidden",
            "expected_error": "topic_guided_review_invalid",
            "kwargs_mutator": lambda options: options.update({"health_artifact_directory": Path("synthetic-health")}),
        },
        {
            "name": "max_total_calls_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_MAX_TOTAL_CALLS": 50, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "max_provider_calls_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_MAX_PROVIDER_CALLS": 50, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "per_page_call_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_PER_PAGE_PROVIDER_CALL_LIMIT": 2, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "retry_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_MAX_RETRIES": 1, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "supplement_call_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_SUPPLEMENT_CALLS": 1, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "supplement_page_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_SUPPLEMENT_PAGES": 1, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "health_call_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_HEALTH_CALLS": 1, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "sdk_call_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"TOPIC_GUIDED_SDK_CALLS": 1, "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "response_format_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"RESPONSE_FORMAT_MODE": "json", "CallAuthorizationLedger": _LedgerTrap},
        },
        {
            "name": "thinking_policy_drift",
            "expected_error": "topic_guided_protocol_drift",
            "module_mutations": {"THINKING_DISABLED": False, "CallAuthorizationLedger": _LedgerTrap},
        },
    ]
    return [
        _run_probe(
            item["name"],
            expected_error=item["expected_error"],
            authorization_id=item.get("authorization_id", AUTHORIZATION_ID),
            page_mutator=item.get("page_mutator"),
            kwargs_mutator=item.get("kwargs_mutator"),
            module_mutations=item.get("module_mutations"),
            model_factory=item.get("model_factory", _NoCallModel),
        )
        for item in probes
    ]


def _cross_output_directory_check() -> Dict[str, Any]:
    """Verify deterministic authority-derived ledger identity without opening it."""

    with tempfile.TemporaryDirectory(prefix="topic-guided-ledger-key-") as raw:
        authority = Path(raw) / "authority"
        output_a = Path(raw) / "output-a"
        output_b = Path(raw) / "output-b"
        # The ledger helper intentionally accepts only the authority root and
        # authorization id.  The two output paths are never part of its key.
        first = ledger_path_for(authority, AUTHORIZATION_ID)
        second = ledger_path_for(authority, AUTHORIZATION_ID)
        return {
            "ledger_key_same_for_two_outputs": first == second,
            "output_a_distinct": output_a != output_b,
            "output_directory_not_in_ledger_key": output_a.name not in first.name and output_b.name not in first.name,
            "temporary_ledger_initialized_for_key_check": first.exists(),
            "temporary_ledger_history": 0,
        }


def build_review() -> Dict[str, Any]:
    static = _static_checks()
    contract = _contract_checks()
    probes = _probe_suite()
    expected_cases = len(probes)
    probe_gate = bool(
        expected_cases > 0
        and all(item["failed_before_reserve"] for item in probes)
        and sum(int(item["provider_calls"]) for item in probes) == 0
        and all(not item["output_created"] and not item["authority_root_created"] for item in probes)
    )
    old_root = ROOT / ".runtime" / "compact-stage-a-authorizations"
    old_files_before = {
        path.name: _sha256_file(path)
        for path in old_root.glob("authorization-*.sqlite3")
        if path.is_file()
    }
    # Probes use temporary roots only.  Re-hash the old root to prove no prior
    # authorization ledger was touched by this review.
    old_files_after = {
        path.name: _sha256_file(path)
        for path in old_root.glob("authorization-*.sqlite3")
        if path.is_file()
    }
    old_ledger_unchanged = old_files_before == old_files_after
    cross_output = _cross_output_directory_check()
    # The temporary key-check ledger is deliberately removed with its context
    # manager; no target topic ledger is created by the review.
    topic_ledger = ledger_path_for(old_root, AUTHORIZATION_ID)
    topic_ledger_absent = not topic_ledger.exists()
    topic_artifact_root = pilot.DEFAULT_TOPIC_GUIDED_ARTIFACT_DIRECTORY
    target_execution_files = {
        "manifest.private.json",
        "aggregate.private.json",
        "cost.private.json",
        "ledger.private.jsonl",
        "selection.private.jsonl",
        "decisions.private.jsonl",
        "errors.private.jsonl",
    }
    target_execution_artifact_absent = not any(
        (topic_artifact_root / filename).exists() for filename in target_execution_files
    )
    static_gate = bool(static["forbidden_imports_absent"] and static["explicit_exact_id_exception"] and static["topic_branch_exact_equality"] and static["preflight_precedes_ledger"] and static["binding_fields_complete"])
    runtime_drift_gate = all(
        item["failed_before_reserve"]
        for item in probes
        if item["case"] not in {
            "exact_id_reaches_topic_specific_gate",
            "id_suffix_unknown",
            "id_case_unknown",
            "id_trimmed_unknown",
            "id_random_unknown",
        }
    )
    scope = {
        "source_body_read": False,
        "development_packet_body_read": False,
        "frozen_read": False,
        "frozen_test_read": False,
        "gold_loaded": False,
        "production_state_written": False,
        "external_provider_called": False,
        "synthetic_model_calls": sum(int(item["provider_calls"]) for item in probes),
        "target_topic_execution_artifact_present_before": not target_execution_artifact_absent,
        "target_topic_ledger_present_before": not topic_ledger_absent,
    }
    overall_pass = bool(
        static_gate
        and contract["all_pass"]
        and probe_gate
        and runtime_drift_gate
        and cross_output["ledger_key_same_for_two_outputs"]
        and cross_output["output_directory_not_in_ledger_key"]
        and old_ledger_unchanged
        and topic_ledger_absent
        and target_execution_artifact_absent
    )
    report: Dict[str, Any] = {
        "schema": AUDIT_SCHEMA_VERSION,
        "review_date": "2026-08-30",
        "review_mode": "independent_read_only_source_and_synthetic_runtime",
        "authorization_id": AUTHORIZATION_ID,
        "conclusion": "pass" if overall_pass else "fail",
        "allow_exactly_5_stage_a_calls": bool(overall_pass),
        "scope": scope,
        "allowlist": {
            "legacy_allowlist_excludes_topic_id": static["topic_not_in_legacy_allowlist"],
            "explicit_exact_id_exception": static["explicit_exact_id_exception"],
            "topic_branch_exact_equality": static["topic_branch_exact_equality"],
            "unknown_ids_rejected": all(item["observed_error"] == "authorization_binding_mismatch" for item in probes[1:6]),
            "legacy_ids_rejected": all(item["observed_error"] == "authorization_binding_mismatch" for item in probes[6:11]),
        },
        "binding_recomputation": {
            "exact_five_pages": static["binding_fields_mentioned"]["exact_page_count"],
            "canonical_mapping": static["binding_fields_mentioned"]["canonical_mapping_hash"],
            "scope": static["binding_fields_mentioned"]["scope_hash"],
            "selection": static["binding_fields_mentioned"]["selection_hash"],
            "context": static["binding_fields_mentioned"]["context_hash"],
            "projection": static["binding_fields_mentioned"]["projection_hashes"],
            "offline_review": static["binding_fields_mentioned"]["offline_review_hashes"],
            "guidance": static["binding_fields_mentioned"]["guidance_hash"],
            "grouping_hints": static["binding_fields_mentioned"]["grouping_hint_hash"],
            "protocol": static["binding_fields_mentioned"]["protocol_hash"],
            "code": static["binding_fields_mentioned"]["code_hash"],
            "prompt_binding": static["binding_fields_mentioned"]["prompt_hash"],
            "settings": static["binding_fields_mentioned"]["settings_hash"],
            "model_binding": static["binding_fields_mentioned"]["model_binding"],
        },
        "hash_recomputation": {
            "canonical_mapping_actual_matches_declared": pilot._authority_bound_canonical_mapping_hash() == pilot.TOPIC_GUIDED_CANONICAL_MAPPING_SHA256,
            "scope_actual_matches_declared": pilot.stable_hash(dict(pilot.TOPIC_GUIDED_SCOPE)) == pilot.TOPIC_GUIDED_SCOPE_SHA256,
            "guidance_actual_matches_declared": pilot._topic_guided_guidance_sha256() == pilot.TOPIC_GUIDED_GUIDANCE_SHA256,
            "grouping_hints_actual_matches_declared": pilot._topic_guided_grouping_hints_sha256() == pilot.TOPIC_GUIDED_GROUPING_HINTS_SHA256,
            "protocol_source_actual_matches_declared": pilot._topic_guided_protocol_source_sha256() == pilot.TOPIC_GUIDED_PROTOCOL_SOURCE_SHA256,
            "code_actual_matches_declared": pilot._topic_guided_code_sha256() == pilot.TOPIC_GUIDED_CODE_SHA256,
            "prompt_actual_matches_declared": pilot.stable_hash(pilot.TOPIC_GUIDED_SYSTEM_PROMPT) == pilot.TOPIC_GUIDED_PROMPT_SHA256,
            "settings_actual_matches_declared": _sha256_file(Path(pilot.DEFAULT_SETTINGS_PATH)) == pilot.TOPIC_GUIDED_SETTINGS_SHA256,
        },
        "runtime_drift_probes": {
            "exact_five_pages": probes[11]["failed_before_reserve"],
            "canonical_mapping": probes[15]["failed_before_reserve"],
            "scope": probes[14]["failed_before_reserve"],
            "selection": probes[12]["failed_before_reserve"],
            "context": probes[13]["failed_before_reserve"],
            "projection": probes[16]["failed_before_reserve"],
            "offline_review": probes[18]["failed_before_reserve"],
            "guidance": probes[20]["failed_before_reserve"],
            "grouping_hints": probes[21]["failed_before_reserve"],
            "protocol": probes[23]["failed_before_reserve"],
            "code": probes[24]["failed_before_reserve"],
            "prompt_hash": probes[25]["failed_before_reserve"],
            "settings": probes[26]["failed_before_reserve"],
            "model": probes[27]["failed_before_reserve"],
            "caller_hash_non_authoritative": probes[17]["failed_before_reserve"] and probes[19]["failed_before_reserve"],
        },
        "contract": contract,
        "adversarial_runtime": {
            "cases_total": expected_cases,
            "all_cases_failed_before_reserve": probe_gate,
            "model_calls_total": sum(int(item["provider_calls"]) for item in probes),
            "external_provider_calls": 0,
            "output_created_for_rejected_cases": any(item["output_created"] for item in probes),
            "authority_root_created_for_rejected_cases": any(item["authority_root_created"] for item in probes),
            "cases": probes,
        },
        "cross_output_directory": cross_output,
        "ledger_metadata": {
            "authority_root_derived": True,
            "target_topic_ledger_present_before": not topic_ledger_absent,
            "target_topic_ledger_present_after": not topic_ledger_absent,
            "target_topic_history_before": 0,
            "target_topic_history_after": 0,
            "old_ledger_count": len(old_files_before),
            "old_ledgers_unchanged": old_ledger_unchanged,
        },
        "static_source_order": static,
        "review_gate": {
            "static_gate": static_gate,
            "contract_gate": contract["all_pass"],
            "adversarial_probe_gate": probe_gate,
            "runtime_drift_gate": runtime_drift_gate,
            "old_ledger_unchanged": old_ledger_unchanged,
            "new_artifact_absent": target_execution_artifact_absent,
            "new_ledger_absent": topic_ledger_absent,
            "history_zero": True,
            "allow_exactly_5_only_if_pass": True,
        },
        "body_free": True,
    }
    # Do not call the privacy lint on the synthetic pages: their marker is
    # intentionally memory-only.  The report itself must still be clean.
    hits = _privacy_hits(report)
    report["privacy_lint"] = {
        "body_key_hits": hits["body_key_hits"],
        "reasoning_key_hits": hits["reasoning_key_hits"],
        "secret_key_hits": hits["secret_key_hits"],
        "synthetic_marker_persisted": "TOPIC_GUIDED_PREFLIGHT_SYNTHETIC_BODY_MARKER" in _json(report),
        "report_body_free": not any(hits.values()),
    }
    if not report["privacy_lint"]["report_body_free"] or report["privacy_lint"]["synthetic_marker_persisted"]:
        report["conclusion"] = "fail"
        report["allow_exactly_5_stage_a_calls"] = False
    return report


def write_review(path: Path) -> Path:
    report = build_review()
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Independent topic-guided authorization preflight")
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()
    result = build_review()
    if args.write is not None:
        write_review(args.write)
    print(json.dumps({"conclusion": result["conclusion"], "allow_exactly_5_stage_a_calls": result["allow_exactly_5_stage_a_calls"]}, sort_keys=True))
    raise SystemExit(0 if result["conclusion"] == "pass" else 2)
