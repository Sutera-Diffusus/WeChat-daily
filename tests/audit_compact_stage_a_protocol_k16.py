"""Independent offline K16 audit for the compact Stage-A wire.

The audit is intentionally narrower than a semantic-quality evaluation.  It
checks the local wire contract and the exact canonical size calculation on a
synthetic worst-case page.  It does not import a runner/provider, read a
development message, read frozen data, or persist request/response bodies.

Run from the repository root::

    .venv\\Scripts\\python.exe tests/audit_compact_stage_a_protocol_k16.py

The process prints a body-free JSON result.  It is safe to redirect that
result to an audit log; the synthetic cue marker is never included in the
returned mapping.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

from wechat_bridge import compact_stage_a_protocol as protocol


ROOT = Path(__file__).resolve().parents[1]
K14_SUMMARY_PATH = (
    ROOT
    / "data"
    / "private"
    / "gold_standard"
    / "2026-08-25"
    / "linear_stage_a_development_pilot_v1"
    / "audit"
    / "audit_summary.private.json"
)

SCOPE = {
    "account_id": "account-k16-synthetic",
    "chat_id": "chat-k16-synthetic",
}
OTHER_SCOPE = {
    "account_id": "account-k16-other",
    "chat_id": "chat-k16-other",
}
BODY_MARKER = "K16_SYNTHETIC_BODY_MUST_NOT_BE_PERSISTED"
BODY_KEYS = frozenset(
    {
        "body",
        "content",
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


def _message(index: int, role: str) -> dict[str, Any]:
    alias = f"m{index}"
    return {
        # The scoped handle and cue use the protocol's long synthetic
        # boundary, so this is a genuine worst-case page rather than a short
        # happy-path sample.
        "message_handle": f"{SCOPE['account_id']}/{SCOPE['chat_id']}|message|M{index:03d}",
        # This is synthetic provider-input material.  The only persisted
        # projection below is checked to ensure this value cannot escape.
        "text": f"{BODY_MARKER}_{index:02d}".ljust(protocol.MAX_MESSAGE_CUE_CHARS, "x"),
        "role": role,
        "speaker": f"synthetic-speaker-{index}",
    }


def _candidate(index: int) -> dict[str, Any]:
    alias = f"c{index}"
    return {
        "candidate_handle": (
            f"{SCOPE['account_id']}/{SCOPE['chat_id']}|candidate|C{index:03d}"
            + "x" * 70
        ),
        "left_message": f"m{((index - 1) % 12) + 1}",
        "right_message": f"m{(index % 12) + 1}",
        "relation": "no_reply" if index == 20 else "continuity",
    }


def _request() -> dict[str, Any]:
    messages = [_message(index, "primary") for index in range(1, 13)]
    messages.extend([_message(13, "context"), _message(14, "context")])
    return protocol.build_compact_stage_a_request(
        SCOPE,
        messages,
        [_candidate(index) for index in range(1, 21)],
    )


def _topic_response() -> dict[str, Any]:
    """A valid two-topic witness: greeting context plus a topic shift."""

    return {
        "t": [
            {
                "i": "topic-alpha",
                "p": [f"m{index}" for index in range(1, 7)],
                "c": ["m13"],
                "u": "uncertain",
            },
            {
                "i": "topic-beta",
                "p": [f"m{index}" for index in range(7, 13)],
                "c": ["m14"],
                "u": "unknown",
            },
        ]
    }


def _assert_rejected(
    label: str,
    operation: Callable[[], Any],
    expected_code: str | None = None,
) -> str:
    try:
        operation()
    except protocol.CompactStageAProtocolError as exc:
        if expected_code is not None and exc.code != expected_code:
            raise AssertionError(
                f"{label}: expected {expected_code}, got {exc.code}"
            ) from exc
        return exc.code
    except Exception as exc:  # pragma: no cover - fail with a useful audit error
        raise AssertionError(f"{label}: unexpected exception {type(exc).__name__}") from exc
    raise AssertionError(f"{label}: malformed input was accepted")


def _assignment_lists(response: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    primary: list[str] = []
    context: list[str] = []
    for topic in response["t"]:
        primary.extend(topic["p"])
        context.extend(topic["c"])
    return primary, context


def _assert_body_free(value: Any) -> None:
    encoded = protocol.canonical_json(value)
    if BODY_MARKER in encoded:
        raise AssertionError("body marker escaped into the ledger projection")

    bad_keys: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).casefold()
                is_ref = key.endswith(("_ref", "_handle", "_handles"))
                if key in BODY_KEYS and child not in (None, "", [], {}, ()) and not is_ref:
                    bad_keys.append(str(raw_key))
                visit(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    if bad_keys:
        raise AssertionError(f"body-bearing ledger keys: {bad_keys[:8]}")


def _load_k14_summary() -> dict[str, Any]:
    """Read only the already body-free K14 audit summary."""

    if not K14_SUMMARY_PATH.is_file():
        raise AssertionError(f"K14 body-free summary is missing: {K14_SUMMARY_PATH}")
    data = json.loads(K14_SUMMARY_PATH.read_text(encoding="utf-8"))
    calls = data.get("calls_and_budget", {})
    cost = data.get("cost", {})
    root_cause = data.get("root_cause", {})
    strict = data.get("strict_result", {})
    privacy = data.get("privacy", {})
    # Keep only scalar/enum evidence in the result.  In particular, never
    # copy an arbitrary value from the private artifact into this output.
    return {
        "artifact_version": str(data.get("artifact_version", "")),
        "audit_status": str(data.get("audit_status", "")),
        "artifact_status": str(data.get("artifact_status", "")),
        "authorized_calls": int(calls.get("authorized_total", 0)),
        "combined_provider_calls": int(calls.get("combined_provider_calls", 0)),
        "input_tokens_min": int(root_cause.get("input_tokens_min", 0)),
        "input_tokens_max": int(root_cause.get("input_tokens_max", 0)),
        "output_limit": int(cost.get("output_token_limit", 0)),
        "output_at_limit_rows": int(root_cause.get("output_at_400_rows", 0)),
        "finish_length_rows": int(root_cause.get("finish_length_rows", 0)),
        "strict_incomplete_rows": int(root_cause.get("strict_incomplete_rows", 0)),
        "inference_codes": [str(item) for item in root_cause.get("inference_codes", ())],
        "not_a_retry_relaxation_problem": bool(
            root_cause.get("not_a_relaxation_or_retry_problem", False)
        ),
        "audit_body_key_hits": {
            str(key): int(value)
            for key, value in (privacy.get("audit_output_key_hits") or {}).items()
            if str(key)
            in {"body_key_hits", "identity_key_hits", "reasoning_key_hits", "secret_key_hits"}
        },
        "source_path_kind": "K14_body_free_audit_summary_only",
    }


def run_audit() -> dict[str, Any]:
    request = _request()
    request_json = protocol.canonical_json(request)
    stats = protocol.measure_wire_size(request)
    envelope = json.loads(protocol.canonical_http_messages(protocol.SYSTEM_PROMPT, request))
    if envelope["messages"][0]["content"] != protocol.SYSTEM_PROMPT:
        raise AssertionError("system prompt was not preserved in canonical HTTP messages")
    if json.loads(envelope["messages"][1]["content"]) != request:
        raise AssertionError("user content is not the canonical request JSON")
    system_user_proxy = math.ceil(
        (len(protocol.SYSTEM_PROMPT) + len(request_json)) / protocol.TOKEN_PROXY_CHARS
    )
    if system_user_proxy > protocol.MAX_INPUT_TOKEN_PROXY:
        raise AssertionError(f"system+user proxy exceeded input limit: {system_user_proxy}")
    if stats.http_token_proxy > protocol.MAX_INPUT_TOKEN_PROXY:
        raise AssertionError(f"full HTTP envelope exceeded input limit: {stats.http_token_proxy}")

    message_rows = [row for row in request["h"] if row["k"] == "m"]
    candidate_rows = [row for row in request["h"] if row["k"] == "c"]
    if len(message_rows) != 14 or len(candidate_rows) != 20:
        raise AssertionError("worst-case request did not retain 14 messages and 20 candidates")
    if len({row["i"] for row in message_rows}) != 14:
        raise AssertionError("message aliases are not unique")
    if len({row["i"] for row in candidate_rows}) != 20:
        raise AssertionError("candidate aliases are not unique")
    if "c20" not in {row["i"] for row in candidate_rows}:
        raise AssertionError("no-reply candidate was dropped")

    worst_response = protocol.build_max_size_response(request)
    protocol.validate_compact_stage_a_output(worst_response, request)
    worst_output_stats = protocol.measure_output_size(worst_response)
    if worst_output_stats["token_proxy"] > protocol.MAX_OUTPUT_TOKENS:
        raise AssertionError("worst-case response exceeded the 400-token proxy")
    worst_primary, worst_context = _assignment_lists(worst_response)
    primary_aliases = [row["i"] for row in message_rows if row["r"] == "p"]
    if worst_primary != list(dict.fromkeys(worst_primary)):
        raise AssertionError("worst-case response repeated a primary alias")
    if set(worst_primary) != set(primary_aliases) or len(worst_primary) != len(primary_aliases):
        raise AssertionError("worst-case response did not cover each primary exactly once")
    if worst_context != list(dict.fromkeys(worst_context)):
        raise AssertionError("worst-case response repeated a context alias")

    valid = _topic_response()
    normalized = protocol.validate_compact_stage_a_output(valid, request)
    primary, context = _assignment_lists(normalized)
    if primary != [f"m{index}" for index in range(1, 13)]:
        raise AssertionError("two-topic witness did not preserve primary exactly-once order")
    if context != ["m13", "m14"]:
        raise AssertionError("greeting/topic-shift context was not retained uniquely")
    if normalized["t"][1]["u"] != "unknown":
        raise AssertionError("unknown/no-reply uncertainty was not retained")

    rejection_codes: dict[str, str] = {}
    malformed = deepcopy(valid)
    malformed["t"][0]["p"].append("m-forged")
    rejection_codes["forged_alias"] = _assert_rejected(
        "forged alias", lambda: protocol.validate_compact_stage_a_output(malformed, request), "output_primary_item"
    )

    malformed = deepcopy(valid)
    malformed["t"][0]["p"].append("m1")
    rejection_codes["duplicate_primary"] = _assert_rejected(
        "duplicate primary", lambda: protocol.validate_compact_stage_a_output(malformed, request), "output_primary_duplicate"
    )

    malformed = deepcopy(valid)
    malformed["t"][0]["p"].pop()
    rejection_codes["missing_primary"] = _assert_rejected(
        "missing primary", lambda: protocol.validate_compact_stage_a_output(malformed, request), "primary_coverage"
    )

    malformed = deepcopy(valid)
    malformed["t"][1]["c"].append("m13")
    rejection_codes["duplicate_context"] = _assert_rejected(
        "duplicate context", lambda: protocol.validate_compact_stage_a_output(malformed, request), "duplicate_context_handle"
    )

    malformed = deepcopy(valid)
    malformed["t"][0]["c"].append(
        f"{OTHER_SCOPE['account_id']}/{OTHER_SCOPE['chat_id']}|message|m99"
    )
    rejection_codes["cross_scope_output"] = _assert_rejected(
        "cross-scope output", lambda: protocol.validate_compact_stage_a_output(malformed, request)
    )

    malformed = deepcopy(valid)
    malformed["t"][0]["state"] = "unknown"
    rejection_codes["stage_b_field"] = _assert_rejected(
        "Stage-B field", lambda: protocol.validate_compact_stage_a_output(malformed, request), "output_topic_keys"
    )

    cross_scope_message = [_message(1, "primary")]
    cross_scope_message[0]["message_handle"] = (
        f"{OTHER_SCOPE['account_id']}/{OTHER_SCOPE['chat_id']}|message|m1"
    )
    rejection_codes["cross_scope_request"] = _assert_rejected(
        "cross-scope request",
        lambda: protocol.build_compact_stage_a_request(SCOPE, cross_scope_message, []),
        "cross_scope_handle",
    )

    ledger = protocol.project_body_free_ledger(
        request,
        valid,
        protocol.size_report(request, valid),
    )
    _assert_body_free(ledger)

    old_request = {
        "schema_version": "k14_synthetic_verbose_witness",
        "messages": [
            {
                "message_handle": row["h"],
                "material": BODY_MARKER * 10,
                "speaker": "synthetic-only",
            }
            for row in message_rows
        ],
        "candidate_materials": [
            {"candidate_handle": row["h"], "material": BODY_MARKER * 5}
            for row in candidate_rows
        ],
        "evidence_handles": ["synthetic/evidence/ref"],
    }
    comparison = protocol.compare_wire_sizes(
        "verbose K14 schema " * 100,
        old_request,
        request,
    )
    if not comparison["new_within_input_limit"]:
        raise AssertionError("compact request comparison is outside the local input limit")
    if comparison["reduction"]["http_token_proxy"] <= 0:
        raise AssertionError("controlled verbose witness did not shrink")

    k14 = _load_k14_summary()
    if k14["audit_body_key_hits"] and any(k14["audit_body_key_hits"].values()):
        raise AssertionError("K14 summary is not body-free according to its own audit")

    return {
        "audit_schema_version": "compact_stage_a_protocol_k16_audit_v1",
        "audit_status": "pass_offline_protocol_only",
        "production_ready": False,
        "semantic_quality_evaluable": False,
        "scope": {
            "provider_calls": 0,
            "development_input_read": False,
            "frozen_read": False,
            "private_request_body_read": False,
            "persisted_body": False,
            "synthetic_only": True,
        },
        "protocol": {
            "version": protocol.PROTOCOL_VERSION,
            "prompt_version": protocol.PROMPT_VERSION,
            "system_chars": len(protocol.SYSTEM_PROMPT),
            "message_count": len(message_rows),
            "candidate_count": len(candidate_rows),
            "system_user_token_proxy": system_user_proxy,
            "full_http_token_proxy": stats.http_token_proxy,
            "input_limit": protocol.MAX_INPUT_TOKEN_PROXY,
            "max_response_token_proxy": worst_output_stats["token_proxy"],
            "output_limit": protocol.MAX_OUTPUT_TOKENS,
            "request_within_input_limit": True,
            "response_within_output_limit": True,
            "request_sha256": protocol.stable_hash(request),
            "response_sha256": protocol.stable_hash(worst_response),
        },
        "semantic_wire_guards": {
            "primary_exactly_once": True,
            "context_unique": True,
            "unknown_allowed": True,
            "greeting_context_retained": True,
            "topic_shift_representable": True,
            "no_reply_candidate_retained": True,
            "forged_duplicate_missing_cross_scope_stage_b_rejected": True,
            "rejection_codes": rejection_codes,
            "scope_and_kind_authoritative": True,
        },
        "ledger": {
            "body_free": True,
            "synthetic_body_marker_persisted": False,
            "opaque_handles_and_counts_only": True,
            "ledger_sha256": protocol.stable_hash(ledger),
        },
        "controlled_comparison": {
            "witness": "synthetic_verbose_duplicate_material_and_schema",
            "old_http_token_proxy": comparison["old"]["http_token_proxy"],
            "new_http_token_proxy": comparison["new"]["http_token_proxy"],
            "reduction_http_token_proxy": comparison["reduction"]["http_token_proxy"],
            "old_messages_sha256": comparison["old"]["messages_sha256"],
            "new_messages_sha256": comparison["new"]["messages_sha256"],
            "not_a_provider_causal_measurement": True,
        },
        "k14_evidence": k14,
        "next_step": {
            "allow_one_synthetic_health_only": True,
            "max_provider_calls": 1,
            "allow_development_input": False,
            "allow_stage_b": False,
            "allow_stage_c": False,
            "reason": "offline compact wire gates pass; K14 semantic output remained strict-incomplete",
            "conditions": {
                "model": "deepseek-v4-flash",
                "response_format": "omitted",
                "thinking_disabled": True,
                "max_output_tokens": 400,
                "synthetic_health_only": True,
                "no_retry": True,
            },
        },
    }


def main() -> int:
    result = run_audit()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the audit command
    raise SystemExit(main())
