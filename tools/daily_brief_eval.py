"""A small, reproducible evaluator for daily-brief ACU selection.

The evaluator deliberately scores *separate* dimensions.  It does not set a
target number of claims and it does not turn the dimensions into one overall
score.

Input contract
--------------
``gold`` is JSON Lines.  Every non-empty line is one ACU object::

    {"acu_id": "a1", "atomic_content": "...", "inclusion_label": "must",
     "actor": "宁江月", "action": "交接",
     "object_or_recipient": "团组织关系转接工作", "status": "in_progress",
     "evidence_message_ids": ["m17"]}

``inclusion_label`` is one of ``must``, ``may`` or ``exclude``.
``evidence_message_ids``
and the four slot fields are required by the annotation contract, but an empty
list or ``null`` slot is valid.  ``may`` is the codebook's "可以纳入" label;
``exclude`` is "不应纳入" (including trivial ACUs).

``candidate`` is one JSON object with a ``claims`` array.  A claim is one
atomic claim expressed in the candidate report::

    {"claim_id": "c1", "text": "...", "acu_ids": ["a1"],
     "actor": "宁江月", "action": "交接",
     "object_or_recipient": "团组织关系转接工作", "status": "in_progress",
     "evidence_message_ids": ["m17"]}

``acu_ids`` binds a report claim to the ACUs it purports to express.  A claim
without a binding is still counted as an output unit (and as unsupported) so
that missing provenance cannot inflate precision.  A claim may bind more than
one ACU, but slot scoring then compares the candidate slot to the union of
the bound ACUs' slot values.  No fuzzy text matching is performed: unbound
claims are intentionally visible as unaligned output.

Metrics
-------
* ``acu_precision`` is eligible (``must`` or ``may``) ACU references divided
  by all referenced ACU IDs, including unknown IDs and one unmatched unit for
  each claim with no ``acu_ids``.
* ``acu_recall`` covers eligible ACUs (``must`` + ``may``), while
  ``must_recall`` covers only ``must`` ACUs.  Coverage is set based, so a
  duplicated claim cannot improve recall.
* ``trivial_inclusion_rate`` is the fraction of claims linked to at least one
  ``exclude`` ACU.
* ``redundancy_rate`` is repeated ACU references after the first occurrence,
  divided by all ACU references.  It is ``null`` when there are no references.
* ``slot_accuracy`` reports exact, normalized
  actor/action/object_or_recipient/status
  accuracy for aligned claims.  Missing values and the literal ``unknown``
  are treated as the same explicit unknown value.
* ``unsupported_claim_rate`` is the fraction of claims for which candidate
  evidence does not intersect the gold evidence of any bound ACU.

Undefined ratios are ``null`` rather than zero.  Counts and per-claim
diagnostics are included to make every ratio auditable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union


SCHEMA_VERSION = "daily_brief_eval.v1"
DECISION_MUST = "must"
DECISION_MAY = "may"
DECISION_EXCLUDE = "exclude"
ELIGIBLE_DECISIONS = frozenset({DECISION_MUST, DECISION_MAY})
SLOTS = ("actor", "action", "object_or_recipient", "status")
UNKNOWN = "__unknown__"


class EvalInputError(ValueError):
    """Raised when an input file violates the evaluator's small schema."""


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _string_id(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _ids(value: Any, *, field: str) -> List[str]:
    result: List[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            # This accepts the common evidence/reference shape while keeping
            # the canonical contract a list of strings.
            item = item.get("id", item.get("ref", item.get("message_id")))
        item_id = _string_id(item)
        if item_id is None:
            raise EvalInputError(f"{field} must contain non-empty strings")
        result.append(item_id)
    return result


def _slot_values(value: Any) -> Set[str]:
    """Normalize a slot to a set, retaining explicit unknown semantics."""

    values: Set[str] = set()
    for item in _as_list(value):
        if isinstance(item, Mapping):
            # Slots are normally strings.  A structured value is represented
            # canonically instead of relying on Python's dict string order.
            item = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if item is None:
            values.add(UNKNOWN)
            continue
        text = str(item).strip()
        folded = text.casefold()
        if not text or folded in {
            "unknown", "unk", "n/a", "na", "null", "none", "not stated",
            "未提及", "未知", "不明", "待确认", "未确认", "无法判断",
        }:
            values.add(UNKNOWN)
        else:
            values.add(folded)
    return values or {UNKNOWN}


def _decision(row: Mapping[str, Any], *, context: str) -> str:
    value = row.get("inclusion_label", row.get("decision"))
    if value is None:
        # These aliases make hand-authored codebook exports usable without
        # making a missing decision silently default to inclusion.
        if row.get("must_include") is True:
            value = DECISION_MUST
        elif row.get("can_include") is True or row.get("optional") is True:
            value = DECISION_MAY
        elif row.get("exclude") is True or row.get("should_not_include") is True:
            value = DECISION_EXCLUDE
    value_id = _string_id(value)
    if value_id is None:
        raise EvalInputError(f"{context} is missing decision (must/may/exclude)")
    normalized = value_id.casefold()
    aliases = {
        "must_include": DECISION_MUST,
        "required": DECISION_MUST,
        "include_must": DECISION_MUST,
        "can_include": DECISION_MAY,
        "optional": DECISION_MAY,
        "maybe": DECISION_MAY,
        "include": DECISION_MAY,
        "should_not_include": DECISION_EXCLUDE,
        "not_include": DECISION_EXCLUDE,
        "excluded": DECISION_EXCLUDE,
        "trivial": DECISION_EXCLUDE,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {DECISION_MUST, DECISION_MAY, DECISION_EXCLUDE}:
        raise EvalInputError(f"{context} has unsupported decision {value!r}")
    return normalized


def _copy_gold(row: Mapping[str, Any], *, line_number: int) -> Dict[str, Any]:
    if not isinstance(row, Mapping):
        raise EvalInputError(f"gold line {line_number} must be a JSON object")
    acu_id = _string_id(row.get("acu_id", row.get("id")))
    if acu_id is None:
        raise EvalInputError(f"gold line {line_number} is missing acu_id")
    decision = _decision(row, context=f"gold line {line_number}")
    evidence_value = row.get("evidence_message_ids", row.get("evidence_refs", []))
    evidence = _ids(evidence_value, field=f"gold line {line_number}.evidence_message_ids")
    result: Dict[str, Any] = {"acu_id": acu_id, "decision": decision, "evidence_refs": evidence}
    if "text" in row:
        result["text"] = row.get("text")
    for slot in SLOTS:
        # Keep the raw value for diagnostics; normalized values are used for
        # comparisons so callers can inspect the input they supplied.
        result[slot] = row.get(slot, row.get("object") if slot == "object_or_recipient" else None)
    return result


def load_gold_jsonl(path: Union[Path, str]) -> List[Dict[str, Any]]:
    """Load and validate one ACU per non-empty JSONL line."""

    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalInputError(f"cannot read gold file {path}: {exc}") from exc
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalInputError(f"gold line {line_number} is not valid JSON: {exc.msg}") from exc
        row = _copy_gold(parsed, line_number=line_number)
        if row["acu_id"] in seen:
            raise EvalInputError(f"duplicate gold acu_id {row['acu_id']!r} on line {line_number}")
        seen.add(row["acu_id"])
        rows.append(row)
    return rows


def _copy_claim(row: Mapping[str, Any], *, index: int) -> Dict[str, Any]:
    if not isinstance(row, Mapping):
        raise EvalInputError(f"candidate claims[{index}] must be a JSON object")
    claim_id = _string_id(row.get("claim_id", row.get("id"))) or f"claim-{index + 1}"
    acu_ids = _ids(row.get("acu_ids", row.get("acu_id", [])), field=f"candidate claims[{index}].acu_ids")
    evidence_value = row.get("evidence_message_ids", row.get("evidence_refs", []))
    evidence = _ids(evidence_value, field=f"candidate claims[{index}].evidence_message_ids")
    result: Dict[str, Any] = {
        "claim_id": claim_id,
        "acu_ids": acu_ids,
        "evidence_refs": evidence,
    }
    if "text" in row:
        result["text"] = row.get("text")
    for slot in SLOTS:
        result[slot] = row.get(slot, row.get("object") if slot == "object_or_recipient" else None)
    return result


def load_candidate_json(path: Union[Path, str]) -> Dict[str, Any]:
    """Load and validate the candidate summary JSON object."""

    path = Path(path)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalInputError(f"cannot read candidate file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalInputError(f"candidate is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, Mapping):
        raise EvalInputError("candidate summary must be a JSON object with a claims array")
    claims = parsed.get("claims")
    if claims is None:
        # A missing array is almost always a producer/schema bug.  Empty
        # reports must use an explicit empty array so the distinction is
        # visible in an audit.
        raise EvalInputError("candidate summary is missing claims array")
    if not isinstance(claims, list):
        raise EvalInputError("candidate claims must be an array")
    return {"claims": [_copy_claim(row, index=i) for i, row in enumerate(claims)]}


def _ratio(numerator: Union[int, float], denominator: Union[int, float]) -> Optional[float]:
    if denominator == 0:
        return None
    value = float(numerator) / float(denominator)
    # Avoid a surprising 1.0000000000000002 in JSON output for integer counts.
    return round(value, 12)


def evaluate(gold_acus: Iterable[Mapping[str, Any]], candidate_summary: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate a gold ACU collection against a candidate summary.

    The function accepts already-loaded mappings for notebook/tests, while the
    CLI uses :func:`load_gold_jsonl` and :func:`load_candidate_json` to enforce
    the file contract.  ``gold_acus`` is validated again here so direct API
    callers receive the same errors as CLI callers.
    """

    gold_rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for index, raw in enumerate(gold_acus, 1):
        row = _copy_gold(raw, line_number=index)
        if row["acu_id"] in seen:
            raise EvalInputError(f"duplicate gold acu_id {row['acu_id']!r}")
        seen.add(row["acu_id"])
        gold_rows.append(row)
    if not isinstance(candidate_summary, Mapping):
        raise EvalInputError("candidate summary must be a JSON object")
    raw_claims = candidate_summary.get("claims")
    if not isinstance(raw_claims, list):
        raise EvalInputError("candidate claims must be an array")
    claims = [_copy_claim(row, index=i) for i, row in enumerate(raw_claims)]
    gold_by_id = {row["acu_id"]: row for row in gold_rows}

    must_ids = {row["acu_id"] for row in gold_rows if row["decision"] == DECISION_MUST}
    eligible_ids = {row["acu_id"] for row in gold_rows if row["decision"] in ELIGIBLE_DECISIONS}
    exclude_ids = {row["acu_id"] for row in gold_rows if row["decision"] == DECISION_EXCLUDE}

    all_references: List[str] = []
    covered_ids: Set[str] = set()
    covered_must: Set[str] = set()
    eligible_reference_count = 0
    trivial_claim_count = 0
    duplicate_reference_count = 0
    seen_references: Set[str] = set()
    unsupported_claim_count = 0
    aligned_claim_count = 0
    slot_correct: Dict[str, int] = {slot: 0 for slot in SLOTS}
    slot_total: Dict[str, int] = {slot: 0 for slot in SLOTS}
    per_claim: List[Dict[str, Any]] = []

    for claim in claims:
        refs = list(claim["acu_ids"])
        all_references.extend(refs)
        for acu_id in refs:
            if acu_id in seen_references:
                duplicate_reference_count += 1
            seen_references.add(acu_id)
            if acu_id in eligible_ids:
                eligible_reference_count += 1
                covered_ids.add(acu_id)
                if acu_id in must_ids:
                    covered_must.add(acu_id)

        known_ids = [acu_id for acu_id in refs if acu_id in gold_by_id]
        claim_is_trivial = bool(set(known_ids) & exclude_ids)
        if claim_is_trivial:
            trivial_claim_count += 1

        gold_evidence: Set[str] = set()
        for acu_id in known_ids:
            gold_evidence.update(gold_by_id[acu_id].get("evidence_refs", []))
        candidate_evidence = set(claim["evidence_refs"])
        supported = bool(candidate_evidence & gold_evidence)
        if not supported:
            unsupported_claim_count += 1

        claim_slot_result: Dict[str, Optional[bool]] = {slot: None for slot in SLOTS}
        if known_ids:
            aligned_claim_count += 1
            for slot in SLOTS:
                expected: Set[str] = set()
                for acu_id in known_ids:
                    expected.update(_slot_values(gold_by_id[acu_id].get(slot)))
                actual = _slot_values(claim.get(slot))
                correct = actual == expected
                claim_slot_result[slot] = correct
                slot_total[slot] += 1
                if correct:
                    slot_correct[slot] += 1

        per_claim.append(
            {
                "claim_id": claim["claim_id"],
                "acu_ids": refs,
                "known_acu_ids": known_ids,
                "unknown_acu_ids": [acu_id for acu_id in refs if acu_id not in gold_by_id],
                "supported": supported,
                "trivial": claim_is_trivial,
                "slot_accuracy": claim_slot_result,
            }
        )

    # A claim with no ACU binding is an output unit for precision purposes.
    # This prevents a producer from hiding unaligned claims by omitting IDs.
    unmatched_claim_count = sum(1 for claim in claims if not claim["acu_ids"])
    precision_denominator = len(all_references) + unmatched_claim_count
    acu_precision = _ratio(eligible_reference_count, precision_denominator)
    acu_recall = _ratio(len(covered_ids), len(eligible_ids))
    must_recall = _ratio(len(covered_must), len(must_ids))
    trivial_rate = _ratio(trivial_claim_count, len(claims))
    redundancy_rate = _ratio(duplicate_reference_count, len(all_references))
    unsupported_rate = _ratio(unsupported_claim_count, len(claims))

    slot_accuracy: Dict[str, Optional[float]] = {
        slot: _ratio(slot_correct[slot], slot_total[slot]) for slot in SLOTS
    }
    metrics: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "acu_precision": acu_precision,
        "acu_recall": acu_recall,
        "must_recall": must_recall,
        "trivial_inclusion_rate": trivial_rate,
        "redundancy_rate": redundancy_rate,
        "redundancy": redundancy_rate,
        "slot_accuracy": slot_accuracy,
        "actor_slot_accuracy": slot_accuracy["actor"],
        "action_slot_accuracy": slot_accuracy["action"],
        "object_or_recipient_slot_accuracy": slot_accuracy["object_or_recipient"],
        # Compatibility alias for consumers of the first evaluator draft.
        "object_slot_accuracy": slot_accuracy["object_or_recipient"],
        "status_slot_accuracy": slot_accuracy["status"],
        "unsupported_claim_rate": unsupported_rate,
        "counts": {
            "gold_acus": len(gold_rows),
            "gold_eligible_acus": len(eligible_ids),
            "gold_must_acus": len(must_ids),
            "gold_exclude_acus": len(exclude_ids),
            "candidate_claims": len(claims),
            "candidate_acu_references": len(all_references),
            "candidate_known_acu_references": sum(acu_id in gold_by_id for acu_id in all_references),
            "candidate_unknown_acu_references": sum(acu_id not in gold_by_id for acu_id in all_references),
            "unmatched_claims": unmatched_claim_count,
            "eligible_references": eligible_reference_count,
            "covered_eligible_acus": len(covered_ids),
            "covered_must_acus": len(covered_must),
            "trivial_claims": trivial_claim_count,
            "duplicate_acu_references": duplicate_reference_count,
            "unsupported_claims": unsupported_claim_count,
            "aligned_claims": aligned_claim_count,
            "slot_comparisons": slot_total,
            "slot_correct": slot_correct,
        },
        "per_claim": per_claim,
    }
    return metrics


def evaluate_files(gold_path: Union[Path, str], candidate_path: Union[Path, str]) -> Dict[str, Any]:
    """Load two files and return :func:`evaluate` output."""

    return evaluate(load_gold_jsonl(gold_path), load_candidate_json(candidate_path))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score daily-brief ACU selection without a length cap or aggregate score.",
        epilog=(
            "Gold JSONL: one object per line with acu_id, inclusion_label "
            "(must|may|exclude), four slots, and evidence_message_ids. Candidate JSON: "
            "{\"claims\": [{...}]} with acu_ids and evidence_message_ids per claim. "
            "Legacy decision/object/evidence_refs aliases remain accepted. Ratios with "
            "no denominator are null."
        ),
    )
    parser.add_argument("--gold", required=True, type=Path, help="gold ACU JSONL path")
    parser.add_argument("--candidate", required=True, type=Path, help="candidate summary JSON path")
    parser.add_argument("--compact", action="store_true", help="emit one-line JSON instead of indented JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = evaluate_files(args.gold, args.candidate)
    except EvalInputError as exc:
        print(f"daily_brief_eval: {exc}", file=sys.stderr)
        return 2
    indent = None if args.compact else 2
    print(json.dumps(result, ensure_ascii=False, indent=indent, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
