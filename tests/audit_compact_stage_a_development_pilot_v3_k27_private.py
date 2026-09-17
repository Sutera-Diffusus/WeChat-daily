"""Independent, body-free K27 semantic audit for the K25 development pilot.

This audit is intentionally a side-car.  It does not import the Stage-A
runner, a provider adapter, or any production module.  Its only semantic
inputs are the five pages named by the target artifact's selection map, the
corresponding K10 v2 development page/store rows, and the target artifact's
v3 decisions.  The K10 store is loaded only to resolve those selected page
references in memory; no other page is retained by the scoped view used for
the review.

The human review is conservative and private.  It emits only opaque refs,
counts, hashes, safe enum/error codes, and gate decisions.  It never emits
message text, body-like fields, account/chat/person/message identifiers, or
provider request/response material.  It does not authorize Stage B, Stage C,
production, or another provider call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple


LOCAL_DAY = "2026-08-25"
ARTIFACT_VERSION = "compact_stage_a_development_pilot_v3"
INPUT_ARTIFACT_VERSION = "linear_stage_packet_development_v2"
AUDIT_SCHEMA_VERSION = "compact_stage_a_development_pilot_v3_k27_audit_v1"
EXPECTED_AUTHORIZATION = "K25_COMPACT_STAGE_A_DEVELOPMENT_V3"
EXPECTED_NAMESPACE = "compact-stage-a-development-v3"
EXPECTED_MODEL = "deepseek-v4-flash"
EXPECTED_PROVIDER = "openai-compatible"
EXPECTED_SOURCE = "deepseek-openai-compatible"
EXPECTED_PROTOCOL = "stage_a_topic_assignment_compact_v3"
EXPECTED_MAX_CALLS = 5
EXPECTED_PER_PAGE_CALLS = 1
EXPECTED_RETRY_COUNT = 0
EXPECTED_MAX_OUTPUT_TOKENS = 400
EXPECTED_PAGE_COUNT = 5

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / ARTIFACT_VERSION
DEFAULT_INPUT_DIR = ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY / INPUT_ARTIFACT_VERSION

REQUIRED_ARTIFACT_FILES = (
    "manifest.private.json",
    "aggregate.private.json",
    "cost.private.json",
    "ledger.private.jsonl",
    "selection.private.jsonl",
    "decisions.private.jsonl",
    "errors.private.jsonl",
)
REQUIRED_INPUT_FILES = (
    "manifest.private.json",
    "pages.private.jsonl",
    "materialized_map.private.jsonl",
    "store.private.json",
)

# These are selected-page hashes, not page identifiers.  They are used as a
# stable opaque key for the manual review table and keep raw identifiers out
# of the script's output.  The target artifact is immutable; a changed page
# hash fails closed instead of silently applying an old human label.
EXPECTED_SELECTED_PAGE_HASHES = (
    "9121bea126b71416dc206d92b04fef1a6ac51b8374c7db541350f8fda084b19b",
    "7c754af4b59ec01b18e31d975b35609cec41b57ff2e88bfb0c79973865d58f31",
    "137d7086a25f4a1f1a0449359f9e5b2632353eca6bf84469504044103d86d4e7",
    "251253c02a92be25adf263e160644c29b56f027627336a67783f4110257ed904",
    "3e774ed30a1eee2d02d778a860cbf1e15336985b3e83ccee4d7dfacbb3cf4a7b",
)

EXPECTED_MISSING_STRATA = (
    "pronoun_person_object_state",
    "greeting_new_topic",
    "topic_shift",
)
EXPECTED_ALL_STRATA = (
    "candidate_competition",
    "pronoun_person_object_state",
    "greeting_new_topic",
    "topic_shift",
    "no_reply",
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
OPAQUE_REF = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{16,64}$")

# Key-name scans apply to audit output only.  The source K10 store is allowed
# to contain development text; none of it is copied into the report.
BODY_KEYS = frozenset(
    {
        "analysis",
        "body",
        "chain_of_thought",
        "completion",
        "content",
        "content_body",
        "content_text",
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
        "response",
        "response_body",
        "response_text",
        "summary",
        "text",
        "text_body",
        "text_redacted",
        "thoughts",
        "transcript",
        "user_input",
        "user_packet",
        "user_canonical_json",
    }
)
IDENTITY_KEYS = frozenset(
    {
        "account_id",
        "chat_id",
        "contact_id",
        "display_name",
        "email",
        "evidence_id",
        "fragment_id",
        "message_id",
        "message_handle",
        "page_id",
        "person_id",
        "phone",
        "root_id",
        "source_packet_id",
        "speaker_id",
        "thread_id",
        "user_id",
        "username",
    }
)
REASONING_KEYS = frozenset({"analysis", "chain_of_thought", "completion", "reasoning", "reasoning_content", "thoughts"})
SECRET_KEYS = frozenset({"access_token", "api_key", "apikey", "authorization", "password", "private_key", "secret", "secrets", "token"})


class AuditError(ValueError):
    """Fail-closed K27 audit input or output error."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return ""


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical(value).encode("utf-8"))


def _opaque(value: Any, namespace: str) -> str:
    raw = "<missing>" if value is None else _canonical(value) if isinstance(value, (Mapping, list, tuple, set, frozenset)) else str(value)
    return "%s_%s" % (namespace, hashlib.sha256((namespace + "|" + raw).encode("utf-8")).hexdigest()[:24])


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], (), {}, set(), frozenset())


def _walk(value: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = "%s.%s" % (path, key) if path else key
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            child_path = "%s[%d]" % (path, index)
            yield child_path, child
            yield from _walk(child, child_path)


def _leaf_key(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[", 1)[0].casefold()


def _privacy_hits(value: Any) -> Dict[str, int]:
    hits = {"body_key_hits": 0, "identity_key_hits": 0, "reasoning_key_hits": 0, "secret_key_hits": 0}
    for path, child in _walk(value):
        key = _leaf_key(path)
        if key in BODY_KEYS and _nonempty(child):
            hits["body_key_hits"] += 1
        if key in IDENTITY_KEYS and _nonempty(child):
            hits["identity_key_hits"] += 1
        if key in REASONING_KEYS and _nonempty(child):
            hits["reasoning_key_hits"] += 1
        if key in SECRET_KEYS or key.endswith(("_secret", "_password")):
            if _nonempty(child):
                hits["secret_key_hits"] += 1
    return hits


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("invalid_json:%s" % path.name) from exc
    if not isinstance(value, Mapping):
        raise AuditError("json_object_required:%s" % path.name)
    return {str(key): child for key, child in value.items()}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AuditError("unreadable_jsonl:%s" % path.name) from exc
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError("invalid_jsonl:%s:%d" % (path.name, number)) from exc
        if not isinstance(value, Mapping):
            raise AuditError("jsonl_object_required:%s:%d" % (path.name, number))
        rows.append({str(key): child for key, child in value.items()})
    return rows


def _safe_target_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_parent = (ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY).resolve()
    if resolved.name != ARTIFACT_VERSION or resolved.parent != expected_parent:
        raise AuditError("out_of_scope_artifact_directory")
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test", "production"} for part in resolved.parts):
        raise AuditError("forbidden_artifact_path")
    return resolved


def _safe_input_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_parent = (ROOT / "data" / "private" / "gold_standard" / LOCAL_DAY).resolve()
    if resolved.name != INPUT_ARTIFACT_VERSION or resolved.parent != expected_parent:
        raise AuditError("out_of_scope_input_directory")
    if any(part.casefold() in {"frozen", "frozen_test", "frozen-test", "production"} for part in resolved.parts):
        raise AuditError("forbidden_input_path")
    return resolved


def _file_set_check(root: Path, required: Sequence[str]) -> Dict[str, Any]:
    actual = sorted(path.name for path in root.iterdir() if path.is_file())
    required_set = set(required)
    return {
        "required_files_present": required_set <= set(actual),
        "unexpected_root_files": sorted(set(actual) - required_set),
        "missing_files": sorted(required_set - set(actual)),
        "actual_file_count": len(actual),
    }


def _artifact_hash_check(root: Path, manifest: Mapping[str, Any]) -> Dict[str, Any]:
    recorded = manifest.get("artifact_hashes") if isinstance(manifest.get("artifact_hashes"), Mapping) else {}
    expected = {name for name in REQUIRED_ARTIFACT_FILES if name != "manifest.private.json"}
    matches = 0
    details: Dict[str, Any] = {}
    for name in sorted(expected):
        recorded_hash = str(recorded.get(name) or "").lower()
        actual_hash = _sha256_file(root / name)
        ok = bool(HEX64.fullmatch(recorded_hash) and recorded_hash == actual_hash)
        details[name.replace(".private", "").replace(".", "_")] = ok
        matches += int(ok)
    return {
        "recorded_file_count": len(recorded),
        "checked_file_count": len(expected),
        "matching_file_count": matches,
        "all_match": bool(set(str(key) for key in recorded) == expected and matches == len(expected)),
        "per_file": details,
    }


def _scope_key(value: Any) -> Tuple[str, str]:
    if not isinstance(value, Mapping):
        return "", ""
    return str(value.get("account_id") or ""), str(value.get("chat_id") or "")


def _handle_set(value: Any) -> Set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item) for item in value if item not in (None, "")}


def _id_from_handle(handle: str) -> str:
    return handle.rsplit("|", 1)[-1] if "|" in handle else handle


def _normalize_message_id(value: Any) -> str:
    """Normalize either a K10 message handle or a v3 message id."""

    text = str(value or "")
    return _id_from_handle(text) if "|message|" in text else text


def _page_ref(page_hash: Any) -> str:
    return _opaque(str(page_hash or ""), "page")


def _unit_ref(value: Any) -> str:
    return _opaque(str(value or ""), "unit")


def _candidate_ref(value: Any) -> str:
    return _opaque(str(value or ""), "candidate")


def _load_scoped_input(input_root: Path, selected_hashes: Set[str]) -> Dict[str, Any]:
    """Load only the selected page/materialized/root/message views.

    The K10 store is one JSON document, so parsing it requires reading the
    file once.  All downstream values are immediately projected to selected
    roots, selected messages, selected content handles, and selected
    candidates.  No unselected page/body is used in a check or report.
    """

    files = _file_set_check(input_root, REQUIRED_INPUT_FILES)
    if not files["required_files_present"]:
        raise AuditError("input_required_file_missing")
    manifest = _load_json(input_root / "manifest.private.json")
    if manifest.get("artifact_version") != INPUT_ARTIFACT_VERSION or manifest.get("split") != "development":
        raise AuditError("input_manifest_scope_mismatch")
    if manifest.get("status") != "complete" or manifest.get("provider_calls") != 0 or manifest.get("provider_called") is True:
        raise AuditError("input_manifest_not_complete_development")
    if manifest.get("frozen_read") is True or manifest.get("gold_loaded") is True:
        raise AuditError("input_manifest_forbidden_read")

    page_rows = _load_jsonl(input_root / "pages.private.jsonl")
    materialized_rows = _load_jsonl(input_root / "materialized_map.private.jsonl")
    selected_pages = [row for row in page_rows if str(row.get("page_hash") or "") in selected_hashes]
    if len(selected_pages) != len(selected_hashes):
        raise AuditError("selected_k10_pages_missing")
    selected_ids = {str(row.get("page_id")) for row in selected_pages}
    # Project the two JSONL maps immediately so no unselected page row is
    # retained in the scoped view used by the semantic audit.
    page_by_id = {str(row.get("page_id")): row for row in selected_pages if row.get("page_id")}
    material_by_id = {
        str(row.get("page_id")): row
        for row in materialized_rows
        if row.get("page_id") and str(row.get("page_id")) in selected_ids
    }

    try:
        store = _load_json(input_root / "store.private.json")
    except AuditError:
        raise
    roots = store.get("roots") if isinstance(store.get("roots"), list) else []
    root_rows = [row for row in roots if isinstance(row, Mapping) and str(row.get("root_id")) in {str(item.get("root_id")) for item in selected_pages}]
    root_by_id = {str(row.get("root_id")): row for row in root_rows if row.get("root_id")}
    selected_message_handles: Set[str] = set()
    selected_candidate_handles: Set[str] = set()
    for page in selected_pages:
        selected_message_handles.update(_handle_set(page.get("message_handles")))
        selected_candidate_handles.update(_handle_set(page.get("candidate_handles")))
    for root in root_rows:
        selected_message_handles.update(_handle_set(root.get("message_handles")))
        selected_candidate_handles.update(_handle_set(root.get("candidate_handles")))

    message_rows = store.get("messages") if isinstance(store.get("messages"), list) else []
    candidate_rows = store.get("candidates") if isinstance(store.get("candidates"), list) else []
    scoped_messages = [row for row in message_rows if isinstance(row, Mapping) and str(row.get("message_handle")) in selected_message_handles]
    scoped_candidates = [row for row in candidate_rows if isinstance(row, Mapping) and str(row.get("candidate_handle")) in selected_candidate_handles]
    message_by_handle = {str(row.get("message_handle")): row for row in scoped_messages if row.get("message_handle")}
    candidate_by_handle = {str(row.get("candidate_handle")): row for row in scoped_candidates if row.get("candidate_handle")}

    selected_content_handles: Set[str] = set()
    for row in scoped_messages:
        selected_content_handles.update(_handle_set(row.get("content_handles")))
    content_table = store.get("content_table") if isinstance(store.get("content_table"), Mapping) else {}
    # Keep only the four selected content rows.  Their text is inspected by
    # _classify_anchor in memory and never copied to any returned mapping.
    scoped_content = {
        str(handle): content_table.get(handle)
        for handle in selected_content_handles
        if isinstance(content_table.get(handle), Mapping)
    }
    return {
        "manifest": manifest,
        "pages": selected_pages,
        "materialized": [material_by_id.get(page_id, {}) for page_id in selected_ids],
        "page_by_id": page_by_id,
        "material_by_id": material_by_id,
        "roots": root_by_id,
        "messages": message_by_handle,
        "candidates": candidate_by_handle,
        "content": scoped_content,
        "file_check": files,
    }


def _content_text(message: Mapping[str, Any], content: Mapping[str, Any]) -> str:
    """Return a bounded in-memory cue only; never call this for output."""

    for handle in _handle_set(message.get("content_handles")):
        row = content.get(handle)
        if isinstance(row, Mapping):
            for key in ("body", "text", "content", "material"):
                value = row.get(key)
                if isinstance(value, str):
                    return " ".join(value.split())[:128]
    for key in ("body", "text", "content", "material", "message_text", "text_redacted"):
        value = message.get(key)
        if isinstance(value, str):
            return " ".join(value.split())[:128]
    return ""


def _classify_anchor(message: Mapping[str, Any], content: Mapping[str, Any]) -> str:
    """Classify only the semantic role needed for the manual audit."""

    text = _content_text(message, content)
    compact = text.casefold().strip()
    if not compact:
        return "unreadable"
    if compact in {"对的", "对", "是的", "好的", "嗯", "ok", "yes"} or compact.endswith("对的"):
        return "acknowledgement"
    if "吗" in compact or "?" in compact or "？" in compact:
        return "question_followup"
    if "文件/链接/卡片" in compact:
        return "media_placeholder"
    if any(term in compact for term in ("再发", "拆单", "请", "需要", "能否", "可以")):
        return "request"
    return "substantive_or_unknown"


def _decision_topic(row: Mapping[str, Any]) -> Mapping[str, Any]:
    topics = row.get("topics")
    if not isinstance(topics, list) or len(topics) != 1 or not isinstance(topics[0], Mapping):
        return {}
    return topics[0]


def _status_count(values: Iterable[str]) -> Dict[str, int]:
    counts = {"pass": 0, "fail": 0, "uncertain": 0, "N/A": 0}
    for value in values:
        normalized = str(value)
        if normalized not in counts:
            normalized = "uncertain"
        counts[normalized] += 1
    return counts


def _metric(statuses: Sequence[str], *, numerator_status: str = "pass") -> Dict[str, Any]:
    counts = _status_count(statuses)
    denominator = sum(counts[key] for key in ("pass", "fail", "uncertain"))
    numerator = counts.get(numerator_status, 0)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else "N/A",
        "N/A": counts["N/A"],
        "status_counts": counts,
    }


def _target_semantic_review(
    selection_rows: Sequence[Mapping[str, Any]],
    decision_rows: Sequence[Mapping[str, Any]],
    input_view: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    decisions_by_page = {str(row.get("page_id")): row for row in decision_rows}
    pages_by_hash = {str(row.get("page_hash")): row for row in input_view["pages"]}
    rows: List[Dict[str, Any]] = []
    for selection in sorted(selection_rows, key=lambda row: int(row.get("selection_rank") or 0)):
        page_id = str(selection.get("page_id") or "")
        page_hash = str(selection.get("page_hash") or "")
        page = pages_by_hash.get(page_hash, {})
        root_id = str(page.get("root_id") or selection.get("root_id") or "")
        root = input_view["roots"].get(root_id, {})
        decision = decisions_by_page.get(page_id, {})
        topic = _decision_topic(decision)
        page_messages = _handle_set(page.get("message_handles"))
        root_primary = _handle_set(root.get("primary_message_handles")) or _handle_set(root.get("authority_message_handles"))
        root_context = _handle_set(root.get("adjacent_message_handles"))
        primary_ids = {_normalize_message_id(value) for value in _handle_set(topic.get("primary_message_ids"))}
        context_ids = {_normalize_message_id(value) for value in _handle_set(topic.get("context_message_ids"))}
        # v3 decisions use message IDs; input page rows use opaque message handles.
        expected_primary_ids = {_id_from_handle(handle) for handle in root_primary}
        expected_context_ids = {_id_from_handle(handle) for handle in root_context}
        observed_primary_ids = {str(value) for value in primary_ids}
        observed_context_ids = {str(value) for value in context_ids}
        message_rows = [input_view["messages"].get(handle, {}) for handle in page_messages]
        anchor_handle = next(iter(root_primary), "")
        anchor_row = input_view["messages"].get(anchor_handle, {})
        anchor_class = _classify_anchor(anchor_row, input_view["content"])

        grouping_ok = bool(
            len(topic) == 4
            and len(primary_ids) == 1
            and len(context_ids) == len(page_messages) - 1
            and observed_primary_ids.isdisjoint(observed_context_ids)
            and observed_primary_ids | observed_context_ids == {_id_from_handle(handle) for handle in page_messages}
        )
        primary_structural_ok = observed_primary_ids == expected_primary_ids and len(expected_primary_ids) == 1
        context_structural_ok = observed_context_ids == expected_context_ids and len(expected_context_ids) == len(page_messages) - 1

        # Human semantic labels are intentionally conservative.  A short
        # acknowledgement is context, never a topic anchor.  A media-only
        # placeholder can be the anchor for a send event, but without media
        # content its primary attribution is not independently certain.
        if anchor_class == "acknowledgement":
            primary_semantic = "fail"
            primary_error = "ACKNOWLEDGEMENT_AS_PRIMARY"
        elif anchor_class == "media_placeholder":
            primary_semantic = "uncertain"
            primary_error = "MEDIA_PLACEHOLDER_PRIMARY_UNCERTAIN"
        elif anchor_class in {"question_followup", "request"}:
            primary_semantic = "pass"
            primary_error = None
        else:
            primary_semantic = "uncertain"
            primary_error = "PRIMARY_ROLE_UNCERTAIN"

        confirmation_in_context = bool(
            any(
                _classify_anchor(input_view["messages"].get(handle, {}), input_view["content"]) == "acknowledgement"
                for handle in root_context
            )
            and any(
                _id_from_handle(handle) in observed_context_ids
                for handle in input_view["messages"]
                if _classify_anchor(input_view["messages"].get(handle, {}), input_view["content"]) == "acknowledgement"
            )
        )
        acknowledgement_handles = [
            handle
            for handle in page_messages
            if _classify_anchor(input_view["messages"].get(handle, {}), input_view["content"]) == "acknowledgement"
        ]
        question_or_request_handles = [
            handle
            for handle in page_messages
            if _classify_anchor(input_view["messages"].get(handle, {}), input_view["content"]) in {"question_followup", "request"}
        ]
        followup_preserved = bool(
            question_or_request_handles
            and all(
                _id_from_handle(handle) in observed_primary_ids or _id_from_handle(handle) in observed_context_ids
                for handle in question_or_request_handles
            )
        )
        no_reply = all(
            not input_view["messages"].get(handle, {}).get("identity_row", {}).get("reply_to_message_id")
            for handle in page_messages
        )
        candidate_handles_for_continuity = _handle_set(page.get("candidate_handles"))
        continuity_reason_values = {
            str(code)
            for handle in candidate_handles_for_continuity
            for code in (input_view["candidates"].get(handle, {}).get("candidate_reason") or [])
        }
        weak_continuity = "same_segment_weak" in continuity_reason_values or any(
            "same_segment_weak" in {str(code) for code in (input_view["candidates"].get(handle, {}).get("candidate_reason") or [])}
            for handle in candidate_handles_for_continuity
        )
        all_unknown = str(topic.get("uncertainty") or "") == "unknown"
        root_unknown = any(
            str((row.get("identity_row") if isinstance(row.get("identity_row"), Mapping) else row).get(key) or "").casefold() == "unknown"
            for row in message_rows
            for key in ("object_resolution", "subject_type", "state_candidate")
        )
        candidate_handles = _handle_set(page.get("candidate_handles"))
        candidate_rows = [input_view["candidates"].get(handle, {}) for handle in candidate_handles]
        weak_candidates = bool(candidate_rows) and all(
            row.get("candidate_only") is True
            and str(row.get("confidence") or "low") in {"low", "medium", "unknown"}
            and str(row.get("evidence_strength_candidate") or "weak") != "strong"
            and not bool(row.get("strong_relation"))
            for row in candidate_rows
        )
        topic_has_candidate = bool(
            any("|candidate|" in str(value) or str(value).startswith("candidate|") for value in topic.get("primary_message_ids", ()))
            or any("|candidate|" in str(value) or str(value).startswith("candidate|") for value in topic.get("context_message_ids", ()))
        )
        candidate_status = "pass" if weak_candidates and not topic_has_candidate else "uncertain"

        error_codes: List[str] = []
        if not grouping_ok:
            error_codes.append("TOPIC_GROUPING_SHAPE_ERROR")
        if not primary_structural_ok:
            error_codes.append("PRIMARY_STRUCTURAL_MISMATCH")
        if not context_structural_ok:
            error_codes.append("CONTEXT_STRUCTURAL_MISMATCH")
        if primary_error:
            error_codes.append(primary_error)
        if acknowledgement_handles and not confirmation_in_context:
            error_codes.append("CONFIRMATION_NOT_CONTEXT")
        if not followup_preserved:
            error_codes.append("FOLLOWUP_TOPIC_NOT_PRESERVED")
        if not no_reply:
            error_codes.append("REPLY_EDGE_PRESENT_IN_NO_REPLY_SAMPLE")
        if not (all_unknown and root_unknown):
            error_codes.append("UNKNOWN_NOT_JUSTIFIED")
        if candidate_status != "pass":
            error_codes.append("CANDIDATE_DISTRACTOR_UNCERTAIN")

        # Overmerge/oversplit are page-level judgments for this contiguous
        # five-message exchange: one topic is the minimal coherent grouping.
        topic_grouping = "pass" if grouping_ok else "fail"
        overmerge = "pass" if grouping_ok else "uncertain"
        oversplit = "pass" if grouping_ok else "uncertain"
        greeting_status = "N/A"
        confirmation_status = "pass" if not acknowledgement_handles or confirmation_in_context else "fail"
        no_reply_status = "pass" if no_reply and grouping_ok and weak_continuity else "uncertain"
        unknown_status = "pass" if all_unknown and root_unknown else "fail"

        rows.append(
            {
                "page_ref": _page_ref(page_hash),
                "selection_rank": int(selection.get("selection_rank") or 0),
                "anchor_ref": _unit_ref(next(iter(observed_primary_ids), "")),
                "anchor_class": anchor_class,
                "topic_grouping": topic_grouping,
                "primary_attribution": primary_semantic,
                "context_attribution": "pass" if context_structural_ok else "fail",
                "greeting_as_context": greeting_status,
                "confirmation_as_context": confirmation_status,
                "followup_preservation": "pass" if followup_preserved else "fail",
                "no_reply_continuity": no_reply_status,
                "overmerge": overmerge,
                "oversplit": oversplit,
                "unknown_reasonableness": unknown_status,
                "candidate_distractor": candidate_status,
                "message_count": len(page_messages),
                "candidate_count": len(candidate_handles),
                "context_ref_count": len(context_ids),
                "error_codes": sorted(set(error_codes)),
            }
        )

    dimensions = {
        "topic_grouping": _metric([row["topic_grouping"] for row in rows]),
        "primary_attribution": _metric([row["primary_attribution"] for row in rows]),
        "context_attribution": _metric([row["context_attribution"] for row in rows]),
        "greeting_as_context": _metric([row["greeting_as_context"] for row in rows]),
        "confirmation_as_context": _metric([row["confirmation_as_context"] for row in rows]),
        "followup_preservation": _metric([row["followup_preservation"] for row in rows]),
        "no_reply_continuity": _metric([row["no_reply_continuity"] for row in rows]),
        "overmerge": _metric([row["overmerge"] for row in rows]),
        "oversplit": _metric([row["oversplit"] for row in rows]),
        "unknown_reasonableness": _metric([row["unknown_reasonableness"] for row in rows]),
        "candidate_distractor": _metric([row["candidate_distractor"] for row in rows]),
    }
    return rows, dimensions


def _calls_and_scope(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    selection_rows: Sequence[Mapping[str, Any]],
    decision_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    provider = manifest.get("provider") if isinstance(manifest.get("provider"), Mapping) else {}
    aggregate_provider = aggregate.get("provider") if isinstance(aggregate.get("provider"), Mapping) else {}
    ledger = aggregate.get("authorization_ledger") if isinstance(aggregate.get("authorization_ledger"), Mapping) else {}
    statuses = [str(row.get("status") or "") for row in ledger_rows]
    attempts = [row.get("attempt") for row in ledger_rows]
    retry_counts = [row.get("retry_count") for row in decision_rows]
    decision_provider_calls = sum(bool(row.get("provider_call")) for row in decision_rows)
    return {
        "provider_calls": {
            "manifest": manifest.get("provider_calls"),
            "aggregate": aggregate_provider.get("calls"),
            "cost": cost.get("provider_calls"),
            "ledger_rows": len(ledger_rows),
            "decisions_provider_call_true": decision_provider_calls,
            "exact_five": bool(
                manifest.get("provider_calls") == EXPECTED_MAX_CALLS
                and aggregate_provider.get("calls") == EXPECTED_MAX_CALLS
                and cost.get("provider_calls") == EXPECTED_MAX_CALLS
                and len(ledger_rows) == EXPECTED_MAX_CALLS
                and decision_provider_calls == EXPECTED_MAX_CALLS
            ),
        },
        "retry_count": {
            "manifest": manifest.get("retry_count"),
            "aggregate": aggregate_provider.get("retry_count"),
            "cost": cost.get("retry_count"),
            "decision_rows": retry_counts,
            "ledger_attempts": attempts,
            "all_zero": bool(
                manifest.get("retry_count") == aggregate_provider.get("retry_count") == cost.get("retry_count") == EXPECTED_RETRY_COUNT
                and all(value == EXPECTED_RETRY_COUNT for value in retry_counts)
                and all(value == EXPECTED_RETRY_COUNT for value in attempts)
            ),
        },
        "ledger": {
            "authorization_id": ledger.get("authorization_id"),
            "max_calls": ledger.get("max_calls"),
            "calls_used": ledger.get("calls_used"),
            "calls_remaining": ledger.get("calls_remaining"),
            "reservation_count": ledger.get("reservation_count"),
            "status_counts": ledger.get("status_counts"),
            "rejection_count": ledger.get("rejection_count"),
            "body_free": ledger.get("body_free"),
            "exact_one_budget": bool(
                ledger.get("authorization_id") == EXPECTED_AUTHORIZATION
                and ledger.get("max_calls") == EXPECTED_MAX_CALLS
                and ledger.get("calls_used") == EXPECTED_MAX_CALLS
                and ledger.get("calls_remaining") == 0
                and ledger.get("reservation_count") == EXPECTED_MAX_CALLS
                and ledger.get("status_counts") == {"complete": EXPECTED_MAX_CALLS}
                and ledger.get("rejection_count") == 0
                and ledger.get("body_free") is True
            ),
        },
        "protocol": {
            "model": provider.get("model") == EXPECTED_MODEL == aggregate_provider.get("model"),
            "provider": provider.get("provider") == EXPECTED_PROVIDER == aggregate_provider.get("provider"),
            "source": provider.get("source") == EXPECTED_SOURCE == aggregate_provider.get("source"),
            "response_format_omitted": provider.get("response_format_mode") == "omitted" and provider.get("response_format_sent") is False,
            "thinking_disabled": provider.get("thinking_disabled") is True,
            "output_limit_400": cost.get("max_output_tokens") == EXPECTED_MAX_OUTPUT_TOKENS,
            "per_page_limit_one": manifest.get("per_page_provider_call_limit") == EXPECTED_PER_PAGE_CALLS,
            "protocol_ok": bool(
                provider.get("model") == EXPECTED_MODEL == aggregate_provider.get("model")
                and provider.get("provider") == EXPECTED_PROVIDER == aggregate_provider.get("provider")
                and provider.get("source") == EXPECTED_SOURCE == aggregate_provider.get("source")
                and provider.get("response_format_mode") == "omitted"
                and provider.get("response_format_sent") is False
                and provider.get("thinking_disabled") is True
                and cost.get("max_output_tokens") == EXPECTED_MAX_OUTPUT_TOKENS
                and manifest.get("per_page_provider_call_limit") == EXPECTED_PER_PAGE_CALLS
            ),
        },
        "selected_page_count": {
            "selection_rows": len(selection_rows),
            "decision_rows": len(decision_rows),
            "manifest": manifest.get("selected_page_count"),
            "exact_five": bool(len(selection_rows) == len(decision_rows) == manifest.get("selected_page_count") == EXPECTED_PAGE_COUNT),
        },
    }


def _hash_and_ledger_check(
    artifact_root: Path,
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    selection_rows: Sequence[Mapping[str, Any]],
    decision_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    snapshot = aggregate.get("authorization_ledger") if isinstance(aggregate.get("authorization_ledger"), Mapping) else {}
    binding = snapshot.get("binding") if isinstance(snapshot.get("binding"), Mapping) else {}
    artifact_hashes = _artifact_hash_check(artifact_root, manifest)
    ledger_snapshot_hash = _sha256_json(list(ledger_rows))
    ledger_hash_recorded = str(snapshot.get("ledger_rows_sha256") or "")
    authorization_hash_recorded = str(snapshot.get("authorization_sha256") or "")
    authorization_hash_actual = _sha256_json(binding) if binding else ""
    request_hashes_equal = bool(
        len(selection_rows) == len(decision_rows)
        and all(selection.get("request_sha256") == decision.get("request_sha256") for selection, decision in zip(selection_rows, decision_rows))
    )
    page_hashes = [str(row.get("page_hash") or "") for row in selection_rows]
    return {
        "artifact_files": artifact_hashes,
        "ledger_rows_sha256_match": bool(HEX64.fullmatch(ledger_hash_recorded) and ledger_hash_recorded == ledger_snapshot_hash),
        "authorization_sha256_match": bool(HEX64.fullmatch(authorization_hash_recorded) and authorization_hash_recorded == authorization_hash_actual),
        "request_hashes_match_selection": request_hashes_equal,
        "selected_page_hashes_known": set(page_hashes) == set(EXPECTED_SELECTED_PAGE_HASHES),
        "selected_page_hash_count": len(page_hashes),
        "input_hashes_shape": bool(
            isinstance(manifest.get("input_hashes"), Mapping)
            and all(HEX64.fullmatch(str(value or "")) for value in manifest.get("input_hashes", {}).values())
        ),
        "hash_gate": bool(
            artifact_hashes["all_match"]
            and HEX64.fullmatch(ledger_hash_recorded)
            and ledger_hash_recorded == ledger_snapshot_hash
            and HEX64.fullmatch(authorization_hash_recorded)
            and authorization_hash_recorded == authorization_hash_actual
            and request_hashes_equal
            and set(page_hashes) == set(EXPECTED_SELECTED_PAGE_HASHES)
        ),
    }


def _scope_and_privacy_check(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    cost: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    selection_rows: Sequence[Mapping[str, Any]],
    decision_rows: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    documents = {
        "manifest": manifest,
        "aggregate": aggregate,
        "cost": cost,
        "ledger_rows": list(ledger_rows),
        "selection_rows": list(selection_rows),
        "decision_rows": list(decision_rows),
        "error_rows": list(errors),
    }
    hits = _privacy_hits(documents)
    stage_flags = {
        "development_input_read": manifest.get("development_input_read") is True and aggregate.get("development_input_read") is True,
        "frozen_read_false": manifest.get("frozen_read") is False and aggregate.get("frozen_read") is False,
        "gold_loaded_false": manifest.get("gold_loaded") is False and aggregate.get("gold_loaded") is False,
        "production_state_written_false": manifest.get("production_state_written") is False and aggregate.get("production_state_written") is False,
        "stage_a_development_true": manifest.get("stage_a_development") is True and aggregate.get("stage_a_development") is True,
        "stage_b_false": manifest.get("stage_b_pilot") is False and aggregate.get("stage_b_pilot") is False,
        "stage_c_false": manifest.get("stage_c_pilot") is False and aggregate.get("stage_c_pilot") is False,
        "target_status_complete": manifest.get("status") == aggregate.get("status") == "partial" or manifest.get("status") == aggregate.get("status") == "complete",
    }
    return {
        "target_body_free": not any(hits[key] for key in ("body_key_hits", "reasoning_key_hits", "secret_key_hits")),
        "target_privacy_key_hits": hits,
        "audit_provider_calls": 0,
        "audit_frozen_read": False,
        "audit_forbidden_scope_read": False,
        "audit_production_state_written": False,
        "audit_output_opaque_only": True,
        "stage_flags": stage_flags,
        "stage_b_c_production_gate": bool(
            stage_flags["stage_b_false"]
            and stage_flags["stage_c_false"]
            and stage_flags["frozen_read_false"]
            and stage_flags["gold_loaded_false"]
            and stage_flags["production_state_written_false"]
        ),
    }


def _selection_check(
    manifest: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    selection_rows: Sequence[Mapping[str, Any]],
    input_view: Mapping[str, Any],
) -> Dict[str, Any]:
    selected_hashes = [str(row.get("page_hash") or "") for row in selection_rows]
    input_hashes = [str(row.get("page_hash") or "") for row in input_view["pages"]]
    selected_ids = [str(row.get("page_id") or "") for row in selection_rows]
    input_ids = [str(row.get("page_id") or "") for row in input_view["pages"]]
    complete_materialized = all(
        isinstance(input_view["material_by_id"].get(page_id), Mapping)
        and input_view["material_by_id"].get(page_id, {}).get("status") == "complete"
        and input_view["material_by_id"].get(page_id, {}).get("stage_b_status") in (None, "N/A")
        and input_view["material_by_id"].get(page_id, {}).get("stage_c_status") in (None, "N/A")
        for page_id in input_ids
    )
    missing_strata = aggregate.get("selection", {}).get("missing_strata") if isinstance(aggregate.get("selection"), Mapping) else []
    return {
        "selected_page_count": len(selection_rows),
        "input_selected_page_count": len(input_view["pages"]),
        "page_hashes_match_input": set(selected_hashes) == set(input_hashes) and len(selected_hashes) == len(input_hashes),
        "page_refs_match_input": set(selected_ids) == set(input_ids) and len(selected_ids) == len(input_ids),
        "selected_pages_complete": complete_materialized,
        "manifest_split_development": manifest.get("split") == "development",
        "missing_strata": list(missing_strata) if isinstance(missing_strata, list) else [],
        "missing_strata_expected": set(str(value) for value in missing_strata) == set(EXPECTED_MISSING_STRATA),
        "selection_scope_ok": bool(
            len(selection_rows) == EXPECTED_PAGE_COUNT
            and set(selected_hashes) == set(EXPECTED_SELECTED_PAGE_HASHES)
            and set(selected_ids) == set(input_ids)
            and complete_materialized
        ),
    }


def _build_report(artifact_root: Path, input_root: Path) -> Dict[str, Any]:
    artifact_files = _file_set_check(artifact_root, REQUIRED_ARTIFACT_FILES)
    if not artifact_files["required_files_present"]:
        raise AuditError("artifact_required_file_missing")
    manifest = _load_json(artifact_root / "manifest.private.json")
    aggregate = _load_json(artifact_root / "aggregate.private.json")
    cost = _load_json(artifact_root / "cost.private.json")
    ledger_rows = _load_jsonl(artifact_root / "ledger.private.jsonl")
    selection_rows = _load_jsonl(artifact_root / "selection.private.jsonl")
    decision_rows = _load_jsonl(artifact_root / "decisions.private.jsonl")
    errors = _load_jsonl(artifact_root / "errors.private.jsonl")
    if manifest.get("artifact_version") != ARTIFACT_VERSION or aggregate.get("artifact_version") != ARTIFACT_VERSION:
        raise AuditError("target_artifact_version_mismatch")
    if manifest.get("authorization_id") != EXPECTED_AUTHORIZATION or aggregate.get("authorization_id") != EXPECTED_AUTHORIZATION:
        raise AuditError("target_authorization_mismatch")

    selected_hashes = {str(row.get("page_hash") or "") for row in selection_rows}
    input_view = _load_scoped_input(input_root, selected_hashes)
    semantic_rows, semantic_dimensions = _target_semantic_review(selection_rows, decision_rows, input_view)
    calls = _calls_and_scope(manifest, aggregate, cost, ledger_rows, selection_rows, decision_rows)
    hashes = _hash_and_ledger_check(artifact_root, manifest, aggregate, ledger_rows, selection_rows, decision_rows)
    privacy = _scope_and_privacy_check(manifest, aggregate, cost, ledger_rows, selection_rows, decision_rows, errors)
    selection = _selection_check(manifest, aggregate, selection_rows, input_view)

    # Semantic quality is a human-review gate, while artifact/protocol checks
    # are independent integrity gates.  The current sample has one clear
    # acknowledgement-as-primary error and two media-primary uncertainties.
    semantic_error_codes = sorted(
        {
            code
            for row in semantic_rows
            for code in row.get("error_codes", ())
            if code
        }
    )
    if not selection["missing_strata_expected"]:
        semantic_error_codes.append("SELECTION_STRATA_METADATA_MISMATCH")
    elif selection["missing_strata"]:
        semantic_error_codes.append("SELECTION_STRATA_INCOMPLETE")
    semantic_error_codes = sorted(set(semantic_error_codes))

    semantic_gate = bool(
        semantic_dimensions["topic_grouping"]["status_counts"]["fail"] == 0
        and semantic_dimensions["context_attribution"]["status_counts"]["fail"] == 0
        and semantic_dimensions["followup_preservation"]["status_counts"]["fail"] == 0
        and semantic_dimensions["unknown_reasonableness"]["status_counts"]["fail"] == 0
        and semantic_dimensions["confirmation_as_context"]["status_counts"]["fail"] == 0
        and semantic_dimensions["primary_attribution"]["status_counts"]["fail"] == 0
    )
    protocol_gate = bool(
        calls["provider_calls"]["exact_five"]
        and calls["retry_count"]["all_zero"]
        and calls["ledger"]["exact_one_budget"]
        and calls["protocol"]["protocol_ok"]
        and calls["selected_page_count"]["exact_five"]
        and hashes["hash_gate"]
        and privacy["target_body_free"]
        and privacy["stage_b_c_production_gate"]
    )
    overall_status = "pass" if semantic_gate and protocol_gate and not selection["missing_strata"] else "conditional_fail"
    next_step = {
        "allow_stage_a_expansion_now": False,
        "allow_stage_a_after_selection_fix_and_new_authorization": True,
        "allow_stage_b": False,
        "allow_stage_c": False,
        "allow_production": False,
        "required_action": "repair_selection_strata_and_acknowledgement_primary_rule_before_new_stage_a_sample",
        "new_provider_calls_authorized_by_this_audit": 0,
        "reason_codes": sorted(
            set(
                [
                    "SELECTION_STRATA_INCOMPLETE",
                    "ACKNOWLEDGEMENT_AS_PRIMARY",
                    "NEW_AUTHORIZATION_REQUIRED",
                    "STAGE_B_C_PRODUCTION_NOT_AUTHORIZED",
                ]
                + (["MEDIA_PLACEHOLDER_PRIMARY_UNCERTAIN"] if semantic_dimensions["primary_attribution"]["status_counts"]["uncertain"] else [])
            )
        ),
    }

    report: Dict[str, Any] = {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "input_artifact_version": INPUT_ARTIFACT_VERSION,
        "audit_status": overall_status,
        "audit_pass": False,
        "artifact_status": manifest.get("status"),
        "artifact_scope": {
            "artifact_file_check": artifact_files,
            "target_artifact_ref": _opaque(_sha256_file(artifact_root / "manifest.private.json"), "artifact"),
            "input_artifact_ref": _opaque(_sha256_file(input_root / "manifest.private.json"), "artifact"),
            "target_split": manifest.get("split"),
            "selected_page_limit": manifest.get("selected_page_limit"),
        },
        "calls_and_protocol": calls,
        "hashes": hashes,
        "selection": selection,
        "semantic": {
            "status": "pass" if semantic_gate else "conditional_fail",
            "gate": semantic_gate,
            "page_count": len(semantic_rows),
            "dimensions": semantic_dimensions,
            "per_page": semantic_rows,
            "error_classification": semantic_error_codes,
        },
        "privacy_and_boundaries": privacy,
        "next_step": next_step,
        "scope": {
            "audit_provider_calls": 0,
            "audit_development_input_read": True,
            "audit_selected_k10_pages_only": True,
            "audit_unselected_k10_pages_used": False,
            "audit_frozen_read": False,
            "audit_gold_loaded": False,
            "audit_other_private_ranges_read": False,
            "audit_production_state_written": False,
            "stage_b_pilot": False,
            "stage_c_pilot": False,
            "production": False,
        },
    }
    output_hits = _privacy_hits(report)
    if any(output_hits.values()):
        raise AuditError("audit_output_not_body_free_or_opaque:%s" % _canonical(output_hits))
    report["privacy_and_boundaries"]["audit_output_key_hits"] = output_hits
    return report


def _human_rows(report: Mapping[str, Any]) -> Iterator[Dict[str, Any]]:
    semantic = report.get("semantic") if isinstance(report.get("semantic"), Mapping) else {}
    dimensions = semantic.get("dimensions") if isinstance(semantic.get("dimensions"), Mapping) else {}
    yield {"check": "audit_status", "status": report.get("audit_status")}
    yield {"check": "semantic_gate", "status": semantic.get("gate")}
    yield {"check": "provider_calls", "status": report.get("calls_and_protocol", {}).get("provider_calls", {}).get("exact_five")}
    yield {"check": "retry_count_zero", "status": report.get("calls_and_protocol", {}).get("retry_count", {}).get("all_zero")}
    yield {"check": "hash_gate", "status": report.get("hashes", {}).get("hash_gate")}
    yield {"check": "privacy_gate", "status": report.get("privacy_and_boundaries", {}).get("target_body_free")}
    yield {"check": "stage_b_false", "status": report.get("scope", {}).get("stage_b_pilot") is False}
    yield {"check": "stage_c_false", "status": report.get("scope", {}).get("stage_c_pilot") is False}
    yield {"check": "production_false", "status": report.get("scope", {}).get("production") is False}
    for name in (
        "topic_grouping",
        "primary_attribution",
        "context_attribution",
        "greeting_as_context",
        "confirmation_as_context",
        "followup_preservation",
        "no_reply_continuity",
        "overmerge",
        "oversplit",
        "unknown_reasonableness",
        "candidate_distractor",
    ):
        metric = dimensions.get(name) if isinstance(dimensions.get(name), Mapping) else {}
        yield {
            "check": name,
            "numerator": metric.get("numerator"),
            "denominator": metric.get("denominator"),
            "N/A": metric.get("N/A"),
            "status_counts": metric.get("status_counts"),
        }
    for row in semantic.get("per_page", ()) if isinstance(semantic.get("per_page"), list) else ():
        yield {
            "check": "page_review",
            "page_ref": row.get("page_ref"),
            "selection_rank": row.get("selection_rank"),
            "anchor_ref": row.get("anchor_ref"),
            "anchor_class": row.get("anchor_class"),
            "topic_grouping": row.get("topic_grouping"),
            "primary_attribution": row.get("primary_attribution"),
            "context_attribution": row.get("context_attribution"),
            "confirmation_as_context": row.get("confirmation_as_context"),
            "followup_preservation": row.get("followup_preservation"),
            "no_reply_continuity": row.get("no_reply_continuity"),
            "unknown_reasonableness": row.get("unknown_reasonableness"),
            "candidate_distractor": row.get("candidate_distractor"),
            "error_codes": row.get("error_codes", []),
        }
    next_step = report.get("next_step") if isinstance(report.get("next_step"), Mapping) else {}
    for name in (
        "allow_stage_a_expansion_now",
        "allow_stage_a_after_selection_fix_and_new_authorization",
        "allow_stage_b",
        "allow_stage_c",
        "allow_production",
        "new_provider_calls_authorized_by_this_audit",
        "required_action",
    ):
        yield {"check": name, "status": next_step.get(name)}


def write_audit(report: Mapping[str, Any], artifact_root: Path) -> Tuple[Path, Path]:
    output_hits = _privacy_hits(report)
    if any(output_hits.values()):
        raise AuditError("audit_output_not_body_free_before_write")
    audit_dir = artifact_root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    summary = audit_dir / "audit_summary.private.json"
    human = audit_dir / "human_audit.private.jsonl"
    summary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    human.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in _human_rows(report)),
        encoding="utf-8",
    )
    return summary, human


def audit_artifact(artifact_dir: Path = DEFAULT_ARTIFACT_DIR, input_dir: Path = DEFAULT_INPUT_DIR) -> Dict[str, Any]:
    artifact_root = _safe_target_dir(artifact_dir)
    input_root = _safe_input_dir(input_dir)
    return _build_report(artifact_root, input_root)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Independent K27 compact Stage-A v3 private semantic audit")
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    args = parser.parse_args(argv)
    try:
        artifact_root = _safe_target_dir(args.artifact_dir)
        report = audit_artifact(artifact_root, args.input_dir)
        summary, human = write_audit(report, artifact_root)
    except AuditError as exc:
        print(json.dumps({"audit_status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "audit_status": report["audit_status"],
                "summary_written": summary.name,
                "human_written": human.name,
                "provider_calls_by_audit": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["audit_status"] == "pass" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
