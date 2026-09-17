"""Independent K20 offline audit for the compact Stage-A protocol v2.

This audit is intentionally side-car only.  It reads the public K16 audit
document and the body-free K19/K18 audit summary, then exercises a synthetic
minimum/maximum/adversarial packet against the current pure protocol module.
It never imports a runner/provider, opens development or frozen input, or
writes an artifact.  A successful result authorizes one *new*
``synthetic_health_only`` check and nothing beyond that check.

Run from the repository root::

    .venv\\Scripts\\python.exe tests\\audit_compact_stage_a_protocol_v2_k20.py

The printed JSON is body-free: synthetic cues are used in memory to verify
the ledger projection, but only counts, hashes, enums, and pass/fail flags are
returned.
"""

from __future__ import annotations

from copy import deepcopy
import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from wechat_bridge import compact_stage_a_protocol as protocol


ROOT = Path(__file__).resolve().parents[1]
LOCAL_DAY = "2026-08-25"
K16_DOC_PATH = ROOT / "docs" / "compact-stage-a-protocol-k16-audit.md"
K19_SUMMARY_PATH = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / LOCAL_DAY
    / "compact_stage_a_protocol_health_v1"
    / "audit"
    / "audit_summary.private.json"
)
K19_AUDIT_SCRIPT_PATH = ROOT / "tests" / "audit_compact_stage_a_protocol_health_k19_private.py"
K18_PROTOCOL = "stage_a_topic_assignment_compact_v1"
K18_PROMPT = "stage_a_topic_assignment_compact_prompt_v1"
K20_AUTHORIZATION_ID = "K20_COMPACT_STAGE_A_HEALTH_V2"

ACCOUNT = "account-k20-audit"
CHAT = "chat-k20-audit"
OTHER_ACCOUNT = "account-k20-other"
OTHER_CHAT = "chat-k20-other"
SCOPE = {"account_id": ACCOUNT, "chat_id": CHAT}
BODY_MARKER = "K20_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"

HEX64 = re.compile(r"^[0-9a-f]{64}$")
BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "content",
        "content_body",
        "content_text",
        "detail",
        "evidence_text",
        "html",
        "markdown",
        "message",
        "message_text",
        "output_text",
        "prompt",
        "quote",
        "raw",
        "raw_content",
        "raw_output",
        "raw_reasoning",
        "raw_response",
        "raw_text",
        "reasoning",
        "reasoning_content",
        "redacted_text",
        "response",
        "response_body",
        "response_text",
        "summary",
        "system_prompt",
        "text",
        "text_body",
        "thoughts",
        "transcript",
        "user_input",
        "user_packet",
    }
)


class K20AuditError(ValueError):
    """Body-free offline audit failure."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise K20AuditError("invalid_prior_json") from exc
    if not isinstance(value, Mapping):
        raise K20AuditError("prior_json_object_required")
    return value


def _walk(value: Any) -> Iterator[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _walk(child)


def _assert_body_free(value: Any) -> None:
    encoded = _canonical(value)
    if BODY_MARKER in encoded:
        raise K20AuditError("synthetic_body_marker_escaped")
    for key, child in _walk(value):
        normalized = key.casefold()
        is_ref = normalized.endswith(("_ref", "_handle", "_handles"))
        if normalized in BODY_KEYS and child not in (None, "", [], {}, ()) and not is_ref:
            raise K20AuditError("body_field_in_output")


def _message(index: int, role: str = "primary", *, long: bool = False) -> Dict[str, Any]:
    cue = f"{BODY_MARKER}-{index:02d}"
    if long:
        cue = cue.ljust(protocol.MAX_MESSAGE_CUE_CHARS, "x")
    return {
        "message_handle": f"{ACCOUNT}/{CHAT}|message|M{index:03d}",
        "text": cue,
        "role": role,
        "speaker": f"synthetic-speaker-{index}",
        "scope": dict(SCOPE),
    }


def _candidate(index: int, *, long: bool = False) -> Dict[str, Any]:
    handle = f"{ACCOUNT}/{CHAT}|candidate|C{index:03d}"
    if long:
        handle += "x" * 70
    return {
        "candidate_handle": handle,
        "left_message": f"m{((index - 1) % 12) + 1}",
        "right_message": f"m{(index % 12) + 1}",
        "relation": "no_reply" if index == 20 else "continuity",
        "scope": dict(SCOPE),
        "text": BODY_MARKER,
    }


def _request(
    primary: int,
    context: int = 0,
    candidates: int = 0,
    *,
    long: bool = False,
) -> Dict[str, Any]:
    messages = [_message(index, "primary", long=long) for index in range(1, primary + 1)]
    first_context = primary + 1
    messages.extend(
        _message(index, "context", long=long)
        for index in range(first_context, first_context + context)
    )
    return protocol.build_compact_stage_a_request(
        SCOPE,
        messages,
        [_candidate(index, long=long) for index in range(1, candidates + 1)],
    )


def _topic(topic_id: str, primary: Iterable[str], context: Iterable[str] = ()) -> Dict[str, Any]:
    return {"i": topic_id, "p": list(primary), "c": list(context), "u": "unknown"}


def _reject(operation: Callable[[], Any], expected: Optional[str] = None) -> str:
    try:
        operation()
    except protocol.CompactStageAProtocolError as exc:
        if expected is not None and exc.code != expected:
            raise K20AuditError("unexpected_rejection_code") from exc
        return exc.code
    except Exception as exc:
        raise K20AuditError("unexpected_protocol_exception") from exc
    raise K20AuditError("malformed_input_accepted")


def _prior_audit_evidence() -> Dict[str, Any]:
    if not K16_DOC_PATH.is_file() or not K19_AUDIT_SCRIPT_PATH.is_file():
        raise K20AuditError("prior_public_audit_missing")
    try:
        k16_text = K16_DOC_PATH.read_text(encoding="utf-8")
        k19_script_text = K19_AUDIT_SCRIPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise K20AuditError("prior_public_audit_unreadable") from exc
    k19 = _load_json(K19_SUMMARY_PATH)
    _assert_body_free(k19)
    if "pass_offline_protocol_only" not in k16_text:
        raise K20AuditError("k16_public_status_missing")
    if "requires_offline_protocol_repair" not in k19_script_text:
        raise K20AuditError("k19_audit_contract_missing")
    privacy = k19.get("privacy") if isinstance(k19.get("privacy"), Mapping) else {}
    next_step = k19.get("next_step") if isinstance(k19.get("next_step"), Mapping) else {}
    if k19.get("audit_status") != "pass" or k19.get("artifact_status") != "blocked":
        raise K20AuditError("k19_prior_status_unexpected")
    if k19.get("health_complete") is not False:
        raise K20AuditError("k19_health_complete_unexpected")
    if privacy.get("body_free") is not True:
        raise K20AuditError("k19_prior_not_body_free")
    if next_step.get("allow_one_synthetic_health_only") is not False:
        raise K20AuditError("k19_prior_authorization_not_closed")
    return {
        "k16_public_audit_present": True,
        "k16_status_marker": "pass_offline_protocol_only",
        "k19_public_audit_present": True,
        "k19_audit_status": "pass",
        "k19_artifact_status": "blocked",
        "k19_health_complete": False,
        "k19_body_free": True,
        "k19_prior_health_authorization": False,
        "k18_failed_schema_gate": True,
    }


def _single_rule_source_check() -> Dict[str, Any]:
    source_path = Path(protocol.__file__).resolve()
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        raise K20AuditError("protocol_source_unreadable") from exc
    assignments = 0
    topic_fn_uses_primary_helper = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: List[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif node.target is not None:
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "TOPIC_LIMIT_RULE":
                    assignments += 1
        if isinstance(node, ast.FunctionDef) and node.name == "topic_limit_for_request":
            topic_fn_uses_primary_helper = any(
                isinstance(child, ast.Name) and child.id == "_primary_aliases_from_packet"
                for child in ast.walk(node)
            )
    return {
        "topic_limit_symbol_assignments": assignments,
        "single_topic_limit_symbol": assignments == 1,
        "topic_limit_function_uses_primary_projection": topic_fn_uses_primary_helper,
    }


def run_audit() -> Dict[str, Any]:
    prior = _prior_audit_evidence()
    source_check = _single_rule_source_check()

    minimal = _request(primary=1, context=1, candidates=0)
    minimal_response = {"t": [_topic("t1", ["m1"], ["m2"])]}
    if protocol.validate_compact_stage_a_output(minimal_response, minimal) != minimal_response:
        raise K20AuditError("minimal_valid_response_not_preserved")

    small = _request(primary=2, context=0, candidates=0)
    large_same_primary = _request(primary=2, context=2, candidates=20)
    if protocol.topic_limit_for_request(small) != 2 or protocol.topic_limit_for_request(large_same_primary) != 2:
        raise K20AuditError("topic_limit_depends_on_non_primary_count")
    same_primary_response = {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]}
    protocol.validate_compact_stage_a_output(same_primary_response, small)
    protocol.validate_compact_stage_a_output(same_primary_response, large_same_primary)

    maximal = _request(primary=12, context=2, candidates=20, long=True)
    maximal_size = protocol.measure_wire_size(maximal)
    maximal_response = protocol.build_max_size_response(maximal)
    maximal_output_size = protocol.measure_output_size(maximal_response)
    if maximal_size.http_token_proxy > 1600:
        raise K20AuditError("maximal_http_proxy_exceeded")
    if maximal_output_size["token_proxy"] > 400:
        raise K20AuditError("maximal_response_proxy_exceeded")
    if len(maximal_response["t"]) != 12:
        raise K20AuditError("maximal_primary_topic_count_unexpected")

    rule_surface = {
        "system_rule": protocol.TOPIC_LIMIT_RULE in protocol.SYSTEM_PROMPT,
        "request_schema": protocol.TOPIC_LIMIT_RULE in protocol.REQUEST_SCHEMA,
        "response_schema": protocol.TOPIC_LIMIT_RULE in protocol.RESPONSE_SCHEMA,
        "validator_derived_limit": protocol.topic_limit_for_request(minimal) == 1
        and protocol.topic_limit_for_request(maximal) == 12,
        "single_source_assignment": bool(source_check["single_topic_limit_symbol"]),
        "primary_projection": bool(source_check["topic_limit_function_uses_primary_projection"]),
    }
    if not all(rule_surface.values()):
        raise K20AuditError("topic_limit_single_source_gate_failed")

    rejection_codes: Dict[str, str] = {}
    too_many = {"t": [_topic("t1", ["m1"]), _topic("t2", ["m2"])]}
    rejection_codes["topic_over_primary_limit"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(too_many, minimal),
        "output_topic_limit",
    )
    malformed = deepcopy(same_primary_response)
    malformed["extra"] = True
    rejection_codes["extra_field"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(malformed, small),
        "output_keys",
    )
    rejection_codes["empty_topics"] = _reject(
        lambda: protocol.validate_compact_stage_a_output({"t": []}, small),
        "output_topics",
    )
    malformed = deepcopy(same_primary_response)
    malformed["t"][0]["p"] = []
    rejection_codes["empty_primary"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(malformed, small),
        "output_primary",
    )
    malformed = deepcopy(same_primary_response)
    malformed["t"][0]["p"].append("m1")
    rejection_codes["duplicate_primary"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(malformed, small),
        "output_primary_duplicate",
    )
    malformed = deepcopy(same_primary_response)
    malformed["t"][0]["p"].append("m-forged")
    rejection_codes["forged_alias"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(malformed, small),
        "output_primary_item",
    )
    malformed = deepcopy(same_primary_response)
    malformed["t"][0]["c"].append("m-forged")
    rejection_codes["cross_scope_output_alias"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(malformed, small),
        "output_context_item",
    )
    malformed = deepcopy(same_primary_response)
    malformed["t"][0]["state"] = "unknown"
    rejection_codes["stage_b_field"] = _reject(
        lambda: protocol.validate_compact_stage_a_output(malformed, small),
        "output_topic_keys",
    )
    wrong_message = _message(1)
    wrong_message["message_handle"] = f"{OTHER_ACCOUNT}/{OTHER_CHAT}|message|M001"
    rejection_codes["cross_scope_request"] = _reject(
        lambda: protocol.build_compact_stage_a_request(SCOPE, [wrong_message], []),
        "cross_scope_handle",
    )
    context_only = _message(1, "context")
    rejection_codes["context_only_request"] = _reject(
        lambda: protocol.build_compact_stage_a_request(SCOPE, [context_only], []),
        "request_primary_messages_empty",
    )

    ledger = protocol.project_body_free_ledger(
        maximal,
        maximal_response,
        protocol.size_report(maximal, maximal_response),
    )
    _assert_body_free(ledger)
    if ledger.get("primary_count") != 12 or ledger.get("topic_limit") != 12:
        raise K20AuditError("ledger_primary_limit_mismatch")
    if ledger.get("candidate_count") != 20 or ledger.get("message_count") != 14:
        raise K20AuditError("ledger_count_mismatch")

    legacy = deepcopy(minimal)
    legacy["v"] = K18_PROTOCOL
    v2_hash = protocol.stable_hash(minimal)
    v1_hash = protocol.stable_hash(legacy)
    v2_http_hash = protocol.measure_full_http_messages(protocol.SYSTEM_PROMPT, minimal)["messages_sha256"]
    v1_http_hash = protocol.measure_full_http_messages(protocol.SYSTEM_PROMPT, legacy)["messages_sha256"]
    prompt_hash_v1 = protocol.stable_hash({"protocol": K18_PROTOCOL, "prompt": K18_PROMPT, "request": minimal})
    prompt_hash_v2 = protocol.stable_hash({"protocol": protocol.PROTOCOL_VERSION, "prompt": protocol.PROMPT_VERSION, "request": minimal})
    cache_key_v1 = protocol.stable_hash(
        {
            "protocol_version": K18_PROTOCOL,
            "prompt_version": K18_PROMPT,
            "request_sha256": v1_hash,
        }
    )
    cache_key_v2 = protocol.stable_hash(
        {
            "protocol_version": protocol.PROTOCOL_VERSION,
            "prompt_version": protocol.PROMPT_VERSION,
            "request_sha256": v2_hash,
        }
    )
    hash_namespace = {
        "protocol_is_v2": protocol.PROTOCOL_VERSION != K18_PROTOCOL,
        "prompt_is_v2": protocol.PROMPT_VERSION != K18_PROMPT,
        "request_hash_changes_with_protocol": v1_hash != v2_hash,
        "http_hash_changes_with_protocol": v1_http_hash != v2_http_hash,
        "prompt_namespace_hash_changes": prompt_hash_v1 != prompt_hash_v2,
        "cache_key_material_hash_changes": cache_key_v1 != cache_key_v2,
        "ledger_protocol_is_v2": ledger.get("protocol_version") == protocol.PROTOCOL_VERSION,
        "cache_reuse_blocked_by_hash_namespace": len({v1_hash, v2_hash, v1_http_hash, v2_http_hash, cache_key_v1, cache_key_v2}) == 6,
    }
    if not all(hash_namespace.values()):
        raise K20AuditError("v1_cache_namespace_reuse_guard_failed")

    next_step = {
        "new_authorization_required": True,
        "authorization_id": K20_AUTHORIZATION_ID,
        "prior_authorization_id_different": K20_AUTHORIZATION_ID != "K18_COMPACT_STAGE_A_HEALTH_V1",
        "allow_one_synthetic_health_only": True,
        "max_provider_calls": 1,
        "allow_development_input": False,
        "allow_stage_b": False,
        "allow_stage_c": False,
        "allow_production": False,
        "conditions": {
            "model": "deepseek-v4-flash",
            "response_format": "omitted",
            "thinking_disabled": True,
            "max_output_tokens": 400,
            "synthetic_health_only": True,
            "no_retry": True,
            "new_authorization_id": K20_AUTHORIZATION_ID,
        },
        "reason": "offline compact Stage-A v2 gates pass; K18 v1 schema failure is not reused",
    }
    if not next_step["prior_authorization_id_different"]:
        raise K20AuditError("authorization_id_not_new")

    report = {
        "audit_schema_version": "compact_stage_a_protocol_v2_k20_audit_v1",
        "audit_status": "pass_offline_authorization_only",
        "production_ready": False,
        "health_complete": False,
        "scope": {
            "provider_calls": 0,
            "development_input_read": False,
            "private_request_body_read": False,
            "frozen_read": False,
            "production_state_written": False,
            "synthetic_only": True,
        },
        "prior_evidence": prior,
        "protocol": {
            "version": protocol.PROTOCOL_VERSION,
            "prompt_version": protocol.PROMPT_VERSION,
            "v1_protocol_not_reused": protocol.PROTOCOL_VERSION != K18_PROTOCOL,
            "v1_prompt_not_reused": protocol.PROMPT_VERSION != K18_PROMPT,
            "topic_limit_rule": protocol.TOPIC_LIMIT_RULE,
            "rule_surface": rule_surface,
            "source_check": source_check,
            "minimal": {
                "message_count": 2,
                "candidate_count": 0,
                "primary_count": 1,
                "topic_limit": protocol.topic_limit_for_request(minimal),
            },
            "maximal": {
                "message_count": 14,
                "candidate_count": 20,
                "primary_count": 12,
                "topic_limit": protocol.topic_limit_for_request(maximal),
                "http_token_proxy": maximal_size.http_token_proxy,
                "http_limit": 1600,
                "response_token_proxy": maximal_output_size["token_proxy"],
                "response_limit": 400,
            },
            "same_primary_count_stable": True,
        },
        "guards": {
            "primary_exactly_once": True,
            "each_topic_has_primary": True,
            "context_cannot_create_topic": True,
            "candidate_and_message_counts_do_not_set_limit": True,
            "cross_scope_blocked": True,
            "stage_b_fields_blocked": True,
            "context_only_request_blocked": True,
            "rejection_codes": rejection_codes,
        },
        "ledger": {
            "body_free": True,
            "synthetic_body_marker_persisted": False,
            "opaque_facts_only": True,
            "message_count": ledger["message_count"],
            "candidate_count": ledger["candidate_count"],
            "primary_count": ledger["primary_count"],
            "topic_limit": ledger["topic_limit"],
            "ledger_sha256": _sha256(ledger),
        },
        "hash_namespace": {
            **hash_namespace,
            "v1_request_hash": v1_hash,
            "v2_request_hash": v2_hash,
            "v1_http_hash": v1_http_hash,
            "v2_http_hash": v2_http_hash,
        },
        "next_step": next_step,
    }
    _assert_body_free(report)
    return report


def main() -> int:
    try:
        report = run_audit()
    except K20AuditError as exc:
        print(json.dumps({"audit_status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(_canonical(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
