"""Query-conditioned, evidence-bound daily-brief selection.

The existing reconstruction and content-line stages deliberately produce
candidate material.  This module is the small, fail-closed selection step
that turns *annotated* candidates into body-eligible claims.  It does not
cluster by words, rank with a score, impose a length cap, or provide a local
fallback.  ``must`` and ``may`` are the only codebook labels that can enter
the body; ``exclude`` is retained in the audit output only.

The selector accepts either canonical candidates (already carrying the ACU
fields) or the content-line records emitted by
``semantic_content_lines.enrich_review_content_lines``.  The latter are
intentionally incomplete until an explicit codebook/annotation decision is
joined to them.  A join, when needed, is by exact direct-evidence message-ID
intersection only.  No text similarity or broad-topic overlap is used.

There is intentionally no automatic inclusion-labeler here.  Supplying a
manual ACU/codebook marks the result as a research-only Oracle artifact; it
does not establish a general production selector.

``unknown`` is a value, not a failure to be repaired.  In particular, the
selector never changes an unknown/pending status to ``confirmed`` or
``resolved``.  A caller that needs an actual briefing must handle
``SelectionFailure`` explicitly; this module never falls back to the old
local candidate path.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple, Union


SCHEMA_VERSION = "daily_brief_query_selection.v1"
DEFAULT_QUERY = (
    "根据这一天的微信记录，找出会改变用户行动、决定、风险判断、对重要外部变化的认识，"
    "或值得长期保留的方法与资源的信息。保留人物、动作、对象和状态；将个人体验、观点、"
    "问题和事实分开。不要因为私聊、@用户、出现日期或存在链接就自动纳入。报告长度由通过"
    "纳入判断的信息自然决定。"
)

SLOT_FIELDS = ("actor", "action", "object_or_recipient", "status")
CLAIM_TYPES = frozenset(
    {"observed_action", "reported_experience", "opinion", "question", "hypothesis", "resource", "mixed", "unknown"}
)
STATUSES = frozenset({"confirmed", "reported", "pending", "rejected", "resolved", "unknown"})
RELEVANCE = frozenset({"direct", "decision_support", "situational_awareness", "background", "none", "unknown"})
LABELS = frozenset({"must", "may", "exclude"})


class SelectionInputError(ValueError):
    """Raised when a candidate set cannot be audited safely."""


class SelectionFailure(SelectionInputError):
    """Raised when no candidate passes the explicit codebook/evidence gate."""

    def __init__(self, result: Mapping[str, Any], message: str = "no_qualified_candidates") -> None:
        super().__init__(message)
        self.result = dict(result)
        self.code = message


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _unique_strings(value: Any, *, field: str) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for item in _as_list(value):
        text = _text(item)
        if not text:
            continue
        if text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _candidate_id(row: Mapping[str, Any], index: int) -> str:
    for key in ("candidate_id", "content_line_ref", "acu_id", "claim_id", "id"):
        value = _text(row.get(key))
        if value:
            return value
    return f"candidate-{index:04d}"


def _direct_evidence_ids(row: Mapping[str, Any]) -> List[str]:
    """Read canonical direct IDs and explicit legacy aliases.

    ``support_message_ids`` is the field emitted by the semantic content-line
    stage.  ``evidence_refs`` is accepted only as an explicitly documented
    compatibility alias; it is never treated as text for matching.
    """

    for key in ("evidence_message_ids", "support_message_ids", "primary_message_ids", "evidence_refs"):
        if key in row:
            return _unique_strings(row.get(key), field=key)
    return []


def _context_evidence_ids(row: Mapping[str, Any]) -> List[str]:
    return _unique_strings(
        row.get("context_message_ids", row.get("context_message_refs", [])),
        field="context_message_ids",
    )


def _string_list(value: Any, *, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SelectionInputError(f"{field}_must_be_array")
    result: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SelectionInputError(f"{field}_must_contain_non_empty_strings")
        result.append(item.strip())
    if len(result) != len(set(result)):
        raise SelectionInputError(f"{field}_contains_duplicates")
    return result


def _normalise_label(value: Any) -> Optional[str]:
    label = _text(value).casefold()
    aliases = {
        "must_include": "must",
        "required": "must",
        "include_must": "must",
        "can_include": "may",
        "optional": "may",
        "maybe": "may",
        "include": "may",
        "should_not_include": "exclude",
        "not_include": "exclude",
        "excluded": "exclude",
    }
    label = aliases.get(label, label)
    return label if label in LABELS else None


def _normalise_slot(value: Any) -> str:
    value = _text(value)
    return value or "unknown"


def _normalise_status(value: Any) -> str:
    status = _text(value, default="unknown").casefold()
    return status if status in STATUSES else "unknown"


def _normalise_relevance(value: Any) -> str:
    relevance = _text(value, default="none").casefold()
    return relevance if relevance in RELEVANCE else "unknown"


def _normalise_claim_type(value: Any) -> str:
    claim_type = _text(value, default="unknown").casefold()
    # The semantic content-line stage uses a deliberately broader label for
    # a chat-sourced report.  Keep the canonical ACU vocabulary at the
    # selection boundary while accepting that explicit, non-fuzzy alias.
    claim_type = {
        "chat_report": "reported_experience",
        "reported_claim": "reported_experience",
    }.get(claim_type, claim_type)
    return claim_type if claim_type in CLAIM_TYPES else "unknown"


def _evidence_bindings(row: Mapping[str, Any], evidence_ids: Sequence[str], summary: str) -> List[Dict[str, Any]]:
    raw = row.get("evidence_bindings", row.get("direct_evidence"))
    if raw is None:
        # Canonical ACU exports carry one atomic_content plus direct message
        # IDs, but do not require a second binding array.  Preserve that
        # direct claim as one auditable binding.  Content-line records use
        # summary_candidate/support_message_ids and must provide explicit
        # per-statement bindings instead of getting this synthesis.
        if (
            "atomic_content" in row
            and evidence_ids
            and _normalise_label(row.get("inclusion_label", row.get("decision"))) in LABELS
        ):
            return [{"statement": summary, "message_ids": list(evidence_ids)}]
        return []
    if not isinstance(raw, list):
        raise SelectionInputError("evidence_bindings_must_be_array")
    bindings: List[Dict[str, Any]] = []
    direct = set(evidence_ids)
    bound: Set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise SelectionInputError(f"evidence_binding_{index}_must_be_object")
        statement = _text(item.get("statement"))
        ids = _unique_strings(item.get("message_ids", item.get("evidence_message_ids", [])), field="binding_message_ids")
        if not statement or not ids:
            raise SelectionInputError(f"evidence_binding_{index}_requires_statement_and_message_ids")
        if not set(ids) <= direct:
            raise SelectionInputError(f"evidence_binding_{index}_outside_direct_evidence")
        bound.update(ids)
        bindings.append({"statement": statement, "message_ids": ids})
    if bound != direct:
        raise SelectionInputError("evidence_bindings_do_not_cover_direct_evidence")
    return bindings


def _normalise_candidate(
    row: Mapping[str, Any],
    index: int,
    *,
    decision: Optional[Mapping[str, Any]] = None,
    enrich_slots: bool = False,
) -> Dict[str, Any]:
    if not isinstance(row, Mapping):
        raise SelectionInputError(f"candidate_{index}_must_be_object")
    candidate_id = _candidate_id(row, index)
    decision = decision if isinstance(decision, Mapping) else {}
    # The content-line stage calls these fields title/summary_candidate and
    # support_message_ids; canonical output always exposes stable names.
    summary = _text(row.get("atomic_content", row.get("summary", row.get("summary_candidate"))))
    title = _text(row.get("title", row.get("headline", candidate_id)), default=candidate_id)
    if not summary:
        summary = title
    slots: Dict[str, str] = {}
    for field in SLOT_FIELDS:
        value = row.get(field)
        if value is None and field == "object_or_recipient":
            value = row.get("object")
        if enrich_slots and (value is None or _normalise_slot(value) == "unknown"):
            value = decision.get(field)
            if field == "object_or_recipient" and value is None:
                value = decision.get("object")
        slots[field] = _normalise_status(value) if field == "status" else _normalise_slot(value)
    evidence_ids = _direct_evidence_ids(row)
    context_ids = _context_evidence_ids(row)
    bindings = _evidence_bindings(row, evidence_ids, summary)
    label = _normalise_label(row.get("inclusion_label", row.get("decision")))
    decision_label = _normalise_label(decision.get("inclusion_label", decision.get("decision")))
    if label and decision_label and label != decision_label:
        raise SelectionInputError(f"candidate_{candidate_id}_conflicting_codebook_labels")
    label = label or decision_label
    uncertainty = _string_list(
        row.get("uncertainty", row.get("uncertainties", [])),
        field=f"candidate_{candidate_id}.uncertainty",
    )
    if slots["status"] == "unknown" and "status_unknown" not in uncertainty:
        uncertainty.append("status_unknown")
    claim_type_value = row.get("claim_type")
    if enrich_slots and decision.get("claim_type") not in (None, ""):
        claim_type_value = decision.get("claim_type")
    relevance_value = row.get("user_relevance")
    if enrich_slots and decision.get("user_relevance") not in (None, ""):
        relevance_value = decision.get("user_relevance")
    canonical: Dict[str, Any] = {
        "candidate_id": candidate_id,
        "title": title,
        "atomic_content": summary,
        "actor": slots["actor"],
        "action": slots["action"],
        "object_or_recipient": slots["object_or_recipient"],
        "status": slots["status"],
        "claim_type": _normalise_claim_type(claim_type_value),
        "user_relevance": _normalise_relevance(relevance_value),
        "evidence_message_ids": list(evidence_ids),
        "context_message_ids": list(context_ids),
        "evidence_bindings": bindings,
        "inclusion_label": label,
        "exclusion_reason": _text(
            row.get("exclusion_reason", decision.get("exclusion_reason")),
            default="",
        ) or None,
        "uncertainty": uncertainty,
        "source_unit_ref": _text(row.get("source_unit_ref", row.get("unit_ref")), default="") or None,
        "source_content_line_ref": _text(row.get("content_line_ref"), default="") or None,
    }
    # Keep optional source IDs/refs for auditing without allowing them to
    # stand in for direct message evidence.
    for key in ("acu_id", "claim_id", "support_message_refs", "context_message_refs"):
        if key in row and row.get(key) not in (None, ""):
            canonical[key] = copy.deepcopy(row[key])
    if decision:
        decision_id = _decision_id(decision)
        if decision_id and "acu_id" not in canonical:
            canonical["codebook_decision_id"] = decision_id
    return canonical


def _decision_rows(codebook: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]]) -> List[Mapping[str, Any]]:
    if codebook is None:
        return []
    if isinstance(codebook, Mapping):
        for key in ("decisions", "labels", "candidates", "claims", "acus"):
            if isinstance(codebook.get(key), list):
                return [item for item in codebook[key] if isinstance(item, Mapping)]
        # A mapping keyed by candidate ID is also accepted.
        if all(isinstance(value, Mapping) for value in codebook.values()):
            return [dict(value, candidate_id=str(key)) for key, value in codebook.items()]
        if all(not isinstance(value, (Mapping, list, tuple, set, frozenset)) for value in codebook.values()):
            return [
                {"candidate_id": str(key), "inclusion_label": value}
                for key, value in codebook.items()
            ]
        return [codebook]
    if isinstance(codebook, Sequence) and not isinstance(codebook, (str, bytes)):
        return [item for item in codebook if isinstance(item, Mapping)]
    raise SelectionInputError("codebook_must_be_object_or_array")


def _decision_id(row: Mapping[str, Any]) -> str:
    return _text(
        row.get(
            "candidate_id",
            row.get("content_line_ref", row.get("acu_id", row.get("claim_id"))),
        )
    )


def _decision_evidence(row: Mapping[str, Any]) -> Set[str]:
    return set(_direct_evidence_ids(row))


def join_codebook_by_evidence(
    candidates: Sequence[Mapping[str, Any]],
    codebook: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
) -> Dict[str, Dict[str, Any]]:
    """Join annotation decisions to candidates using direct IDs only.

    An explicit candidate-ID key is accepted first.  Otherwise exact
    evidence-set matches are preferred; if none exists, a single non-empty
    intersection is accepted.  A many-to-many or zero intersection remains
    unresolved and is represented with an explicit ``join_error``; text/title
    overlap is never consulted.
    """

    decisions = _decision_rows(codebook)
    prepared: List[Tuple[Mapping[str, Any], Set[str]]] = [
        (decision, _decision_evidence(decision)) for decision in decisions
    ]
    prepared_by_id = {
        _decision_id(decision): decision
        for decision, _evidence in prepared
        if _decision_id(decision)
    }
    result: Dict[str, Dict[str, Any]] = {}
    for index, raw in enumerate(candidates, 1):
        if not isinstance(raw, Mapping):
            continue
        cid = _candidate_id(raw, index)
        evidence = set(_direct_evidence_ids(raw))
        if cid in prepared_by_id:
            result[cid] = dict(prepared_by_id[cid])
            continue
        exact = [decision for decision, ids in prepared if evidence and ids and ids == evidence]
        hits = exact if exact else [decision for decision, ids in prepared if evidence and ids and ids.intersection(evidence)]
        if len(hits) == 1:
            result[cid] = dict(hits[0])
        elif len(hits) > 1:
            result[cid] = {
                "candidate_id": cid,
                "join_error": "ambiguous_direct_evidence_intersection",
                "matched_decision_ids": [_decision_id(item) for item in hits],
            }
        else:
            result[cid] = {"candidate_id": cid, "join_error": "no_direct_evidence_intersection"}
    return result


def _allowed_evidence_from_reconstruction(reconstruction: Mapping[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    for row in reconstruction.get("messages") or ():
        if not isinstance(row, Mapping):
            continue
        for key in ("source_message_id", "message_id"):
            value = _text(row.get(key))
            if value:
                allowed.add(value)
    return allowed


def candidates_from_reconstruction(reconstruction: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Flatten semantic content lines from a reconstruction artifact.

    This adapter does not invent actor/action/object/status, relevance, or
    inclusion labels.  Missing fields are explicit ``unknown``/``none`` and
    therefore cannot silently become a formal report.
    """

    if not isinstance(reconstruction, Mapping):
        raise SelectionInputError("reconstruction_must_be_object")
    review = reconstruction.get("review")
    if not isinstance(review, Mapping):
        raise SelectionInputError("reconstruction_missing_review")
    units = review.get("sample_units")
    if not isinstance(units, list):
        raise SelectionInputError("reconstruction_missing_review_sample_units")
    candidates: List[Dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        unit_ref = _text(unit.get("unit_ref", unit.get("unit_id")))
        lines = unit.get("content_line_candidates")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, Mapping):
                continue
            candidate = copy.deepcopy(dict(line))
            candidate["candidate_id"] = _candidate_id(candidate, len(candidates) + 1)
            candidate["source_unit_ref"] = unit_ref or None
            # Keep the canonical direct-ID field alongside the semantic-stage
            # alias so downstream audit code never has to guess its meaning.
            candidate["evidence_message_ids"] = _direct_evidence_ids(candidate)
            candidate["context_message_ids"] = _context_evidence_ids(candidate)
            candidates.append(candidate)
    return candidates


def _failure_result(
    *,
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    rejected: Sequence[Mapping[str, Any]],
    reason: str,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "formal_briefing_allowed": False,
        "failure_reason": reason,
        "query": query,
        "source": source,
        "policy": {
            "selection_basis": "codebook_must_may_exclude_only",
            "query_conditioned": True,
            "fixed_output_count": False,
            "top_k": False,
            "word_overlap_clustering": False,
            "local_fallback_used": False,
            "unknown_status_completed": False,
        },
        "selected": [],
        "rejected": [dict(item) for item in rejected],
        "audit": {
            "candidate_count": len(candidates),
            "selected_count": 0,
            "rejected_count": len(rejected),
            "rejection_reasons": dict(Counter(str(item.get("reason")) for item in rejected)),
        },
    }


def _mark_manual_codebook_oracle(result: Dict[str, Any], codebook: Any) -> Dict[str, Any]:
    """Mark explicit ACU/codebook input as research-only oracle material."""

    if codebook is None:
        return result
    result["oracle"] = {
        "manual_codebook": True,
        "automatic_inclusion_labeler": False,
        "production": False,
        "reason": "explicit_manual_codebook_or_acu_input",
    }
    result["formal_briefing_allowed"] = False
    result.setdefault("policy", {})["automatic_inclusion_labeler"] = False
    result["policy"]["research_only"] = True
    return result


def evaluate_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    query: str = DEFAULT_QUERY,
    codebook: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
    allowed_evidence_ids: Optional[Iterable[str]] = None,
    enrich_codebook_slots: bool = False,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit and select candidates without raising on an empty result.

    ``status=failed`` is an intentional result and is what the CLI persists
    when no qualified candidate exists.  Use :func:`select_candidates` when a
    caller wants a fail-closed exception instead.
    """

    query = _text(query)
    if not query:
        raise SelectionInputError("query_required")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise SelectionInputError("candidates_must_be_array")
    allowed = set(_text(item) for item in allowed_evidence_ids or () if _text(item))
    joins = join_codebook_by_evidence(candidates, codebook) if codebook is not None else {}
    seen: Set[str] = set()
    selected: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for index, row in enumerate(candidates, 1):
        cid = _candidate_id(row, index) if isinstance(row, Mapping) else f"candidate-{index:04d}"
        if cid in seen:
            raise SelectionInputError(f"duplicate_candidate_id:{cid}")
        seen.add(cid)
        decision = joins.get(cid, {})
        try:
            candidate = _normalise_candidate(
                row,
                index,
                decision=decision,
                enrich_slots=enrich_codebook_slots,
            )
        except SelectionInputError as exc:
            rejected.append({"candidate_id": cid, "reason": "invalid_candidate", "error": str(exc), "candidate": copy.deepcopy(dict(row)) if isinstance(row, Mapping) else row})
            continue
        reason: Optional[str] = None
        if decision.get("join_error"):
            reason = str(decision["join_error"])
        elif candidate.get("inclusion_label") not in LABELS:
            reason = "missing_codebook_label"
        elif candidate.get("inclusion_label") == "exclude":
            reason = "exclude_reason_missing" if not candidate.get("exclusion_reason") else "codebook_exclude"
        elif not candidate.get("evidence_message_ids"):
            reason = "missing_direct_evidence"
        elif not candidate.get("evidence_bindings"):
            reason = "missing_direct_evidence_binding"
        elif allowed and not set(candidate["evidence_message_ids"]) <= allowed:
            reason = "direct_evidence_outside_source"
        elif set(candidate.get("context_message_ids") or ()) & set(candidate.get("evidence_message_ids") or ()):
            reason = "direct_context_evidence_overlap"
        if reason:
            rejected.append({"candidate_id": cid, "reason": reason, "candidate": candidate})
            continue
        # Explicitly preserve unknown/pending.  This marker is an audit
        # affordance, not a state transition and is deliberately deterministic.
        candidate["completion_inference_forbidden"] = candidate.get("status") in {"unknown", "pending"}
        selected.append(candidate)
    if not selected:
        return _mark_manual_codebook_oracle(
            _failure_result(
                query=query,
                candidates=candidates,
                rejected=rejected,
                reason="no_qualified_candidates",
                source=source,
            ),
            codebook,
        )
    labels = Counter(str(item.get("inclusion_label")) for item in selected)
    return _mark_manual_codebook_oracle({
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "formal_briefing_allowed": True,
        "failure_reason": None,
        "query": query,
        "source": source,
        "policy": {
            "selection_basis": "codebook_must_may_exclude_only",
            "query_conditioned": True,
            "fixed_output_count": False,
            "top_k": False,
            "word_overlap_clustering": False,
            "local_fallback_used": False,
            "unknown_status_completed": False,
        },
        "selected": selected,
        "rejected": rejected,
        "audit": {
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
            "selected_by_label": dict(labels),
            "rejection_reasons": dict(Counter(str(item.get("reason")) for item in rejected)),
            "unknown_status_count": sum(item.get("status") == "unknown" for item in selected),
            "pending_status_count": sum(item.get("status") == "pending" for item in selected),
        },
    }, codebook)


def select_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    query: str = DEFAULT_QUERY,
    codebook: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
    allowed_evidence_ids: Optional[Iterable[str]] = None,
    enrich_codebook_slots: bool = False,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a successful selection or raise :class:`SelectionFailure`."""

    result = evaluate_candidates(
        candidates,
        query=query,
        codebook=codebook,
        allowed_evidence_ids=allowed_evidence_ids,
        enrich_codebook_slots=enrich_codebook_slots,
        source=source,
    )
    if result.get("status") != "ok":
        raise SelectionFailure(result, str(result.get("failure_reason") or "selection_failed"))
    return result


def select_from_reconstruction(
    reconstruction: Mapping[str, Any],
    *,
    query: str = DEFAULT_QUERY,
    codebook: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
    enrich_codebook_slots: bool = False,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    candidates = candidates_from_reconstruction(reconstruction)
    allowed = _allowed_evidence_from_reconstruction(reconstruction)
    result = select_candidates(
        candidates,
        query=query,
        codebook=codebook,
        allowed_evidence_ids=allowed,
        enrich_codebook_slots=enrich_codebook_slots,
        source=source,
    )
    if bool(reconstruction.get("production_blocked")) or bool(reconstruction.get("candidate_only")):
        result["production_blocked"] = True
        result["formal_briefing_allowed"] = False
        result.setdefault("policy", {})["formal_fallback_allowed"] = False
    return result


def _load_json_or_jsonl(path: Union[str, Path]) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SelectionInputError(f"cannot_read:{path}:{exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows: List[Any] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SelectionInputError(f"invalid_json_line:{path}:{line_number}") from exc
        return rows


def _candidate_rows(value: Any) -> List[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in ("candidates", "claims", "content_lines"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, Mapping)]
        if "review" in value:
            return candidates_from_reconstruction(value)
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    raise SelectionInputError("candidate_document_must_be_object_or_array")


def write_selection_artifacts(result: Mapping[str, Any], output_dir: Union[str, Path]) -> Dict[str, Path]:
    """Write a new, auditable selection directory without touching old output."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    summary_path = output / "selection_summary.json"
    summary_path.write_text(payload, encoding="utf-8")
    if result.get("status") != "ok":
        # A rerun into a previously successful *new* selection directory
        # must not leave stale body artifacts next to an explicit failure.
        for stale_name in ("selected_claims.json", "selected_claims.md", "selected_claims.html"):
            stale_path = output / stale_name
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass
        failure_path = output / "selection_failure.json"
        failure_path.write_text(payload, encoding="utf-8")
        return {"summary": summary_path, "failure": failure_path}
    try:
        (output / "selection_failure.json").unlink()
    except FileNotFoundError:
        pass
    selected = result.get("selected") if isinstance(result.get("selected"), list) else []
    claims_path = output / "selected_claims.json"
    claims_path.write_text(json.dumps({"claims": selected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Markdown is intentionally a faithful projection of selected structured
    # claims; it is not a fallback prose report and does not hide status.
    lines = ["# Query-conditioned daily brief selection", "", f"Query: {result.get('query', '')}", ""]
    for item in selected:
        lines.append(f"## {item.get('title') or item.get('candidate_id')}")
        lines.append("")
        lines.append(str(item.get("atomic_content") or ""))
        lines.append("")
        lines.append(
            "- actor: {actor}; action: {action}; object_or_recipient: {object}; status: {status}".format(
                actor=item.get("actor"),
                action=item.get("action"),
                object=item.get("object_or_recipient"),
                status=item.get("status"),
            )
        )
        lines.append(f"- claim_type: {item.get('claim_type')}; user_relevance: {item.get('user_relevance')}")
        lines.append(f"- direct evidence: {', '.join(item.get('evidence_message_ids') or [])}")
        if item.get("status") in {"unknown", "pending"}:
            lines.append("- boundary: status is not complete; no completion inferred")
        lines.append("")
    md_path = output / "selected_claims.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    html_items: List[str] = []
    for item in selected:
        status_note = "；状态未闭环，未推断完成" if item.get("status") in {"unknown", "pending"} else ""
        html_items.append(
            "<article><h2>{title}</h2><p>{summary}</p>"
            "<dl><dt>actor</dt><dd>{actor}</dd><dt>action</dt><dd>{action}</dd>"
            "<dt>object_or_recipient</dt><dd>{object}</dd><dt>status</dt><dd>{status}{note}</dd>"
            "<dt>claim_type</dt><dd>{claim}</dd><dt>user_relevance</dt><dd>{relevance}</dd>"
            "<dt>direct evidence</dt><dd><code>{evidence}</code></dd></dl></article>".format(
                title=html.escape(str(item.get("title") or item.get("candidate_id")), quote=True),
                summary=html.escape(str(item.get("atomic_content") or ""), quote=True),
                actor=html.escape(str(item.get("actor") or "unknown"), quote=True),
                action=html.escape(str(item.get("action") or "unknown"), quote=True),
                object=html.escape(str(item.get("object_or_recipient") or "unknown"), quote=True),
                status=html.escape(str(item.get("status") or "unknown"), quote=True),
                note=html.escape(status_note, quote=True),
                claim=html.escape(str(item.get("claim_type") or "unknown"), quote=True),
                relevance=html.escape(str(item.get("user_relevance") or "none"), quote=True),
                evidence=html.escape(", ".join(item.get("evidence_message_ids") or []), quote=True),
            )
        )
    html_path = output / "selected_claims.html"
    html_path.write_text(
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>"
        "Query-conditioned daily brief selection</title><style>body{font-family:sans-serif;max-width:900px;margin:2rem auto;line-height:1.55}"
        "article{border:1px solid #ccd6df;padding:.8rem 1rem;margin:1rem 0}dt{font-weight:700;float:left;clear:left;width:170px}dd{margin-left:180px}code{overflow-wrap:anywhere}</style>"
        f"<h1>Query-conditioned daily brief selection</h1><p>{html.escape(str(result.get('query') or ''), quote=True)}</p>"
        + "".join(html_items)
        + "</html>\n",
        encoding="utf-8",
    )
    return {"summary": summary_path, "claims": claims_path, "markdown": md_path, "html": html_path}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed query-conditioned daily-brief selector")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candidates", type=Path, help="candidate JSON/JSONL or reconstruction JSON")
    source.add_argument("--reconstruction", type=Path, help="semantic reconstruction artifact")
    parser.add_argument("--codebook", type=Path, help="JSON/JSONL candidate or ACU decisions")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="user daily-brief question")
    parser.add_argument("--output-dir", required=True, type=Path, help="new output directory")
    parser.add_argument("--enrich-from-codebook", action="store_true", help="explicitly copy missing slots from joined decisions")
    parser.add_argument("--source", default=None, help="input artifact label")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        candidate_value = _load_json_or_jsonl(args.reconstruction or args.candidates)
        if args.reconstruction:
            reconstruction = candidate_value
            if not isinstance(reconstruction, Mapping):
                raise SelectionInputError("reconstruction_must_be_object")
            candidates = candidates_from_reconstruction(reconstruction)
            allowed = _allowed_evidence_from_reconstruction(reconstruction)
        else:
            candidates = _candidate_rows(candidate_value)
            allowed = None
        codebook = _load_json_or_jsonl(args.codebook) if args.codebook else None
        result = evaluate_candidates(
            candidates,
            query=args.query,
            codebook=codebook,
            allowed_evidence_ids=allowed,
            enrich_codebook_slots=args.enrich_from_codebook,
            source=args.source or str(args.reconstruction or args.candidates),
        )
        if args.reconstruction and (
            bool(reconstruction.get("production_blocked")) or bool(reconstruction.get("candidate_only"))
        ):
            result["production_blocked"] = True
            result["formal_briefing_allowed"] = False
            result.setdefault("policy", {})["formal_fallback_allowed"] = False
        paths = write_selection_artifacts(result, args.output_dir)
        print(json.dumps({"status": result.get("status"), "paths": {key: str(path) for key, path in paths.items()}}, ensure_ascii=False))
        return 0 if result.get("status") == "ok" else 2
    except SelectionInputError as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "formal_briefing_allowed": False,
            "failure_reason": "invalid_input",
            "error": str(exc),
            "policy": {"local_fallback_used": False, "fixed_output_count": False, "top_k": False},
        }
        paths = write_selection_artifacts(failure, args.output_dir)
        print(json.dumps({"status": "failed", "paths": {key: str(path) for key, path in paths.items()}, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


__all__ = [
    "CLAIM_TYPES",
    "DEFAULT_QUERY",
    "LABELS",
    "RELEVANCE",
    "SCHEMA_VERSION",
    "SelectionFailure",
    "SelectionInputError",
    "STATUSES",
    "SLOT_FIELDS",
    "candidates_from_reconstruction",
    "evaluate_candidates",
    "join_codebook_by_evidence",
    "main",
    "select_candidates",
    "select_from_reconstruction",
    "write_selection_artifacts",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
