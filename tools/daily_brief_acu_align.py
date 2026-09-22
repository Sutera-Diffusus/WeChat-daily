"""Align two independent ACU annotation rounds by direct evidence IDs.

This is deliberately a small arbitration aid, not a selector or a scorer.
Rows are aligned only when their canonical ``evidence_message_ids`` sets have
an exact, non-empty intersection.  A connected component with more than one
row on either side is kept as an unresolved many-to-many ambiguity; the tool
never chooses a one-to-one assignment, uses text similarity, or computes a
total score.

The input is one ACU object per non-empty JSONL line.  The canonical fields
from ``docs/daily-brief-annotation-codebook-v1.md`` are supported directly.
``decision``, ``evidence_refs`` and ``object`` are accepted only as explicit
legacy aliases so that older hand-authored exports can be audited without
changing the canonical output semantics.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union


SCHEMA_VERSION = "daily_brief_acu_align.v1"
SLOTS = ("actor", "action", "object_or_recipient", "status")
LABELS = frozenset({"must", "may", "exclude"})
UNKNOWN_ALIASES = frozenset(
    {
        "",
        "unknown",
        "unk",
        "n/a",
        "na",
        "null",
        "none",
        "not stated",
        "未提及",
        "未知",
        "不明",
        "待确认",
        "未确认",
        "无法判断",
    }
)


class AlignmentInputError(ValueError):
    """Raised when an annotation round cannot be audited safely."""


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


def _ids(value: Any, *, context: str) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for item in _as_list(value):
        if isinstance(item, Mapping):
            item = item.get("id", item.get("ref", item.get("message_id")))
        item_id = _string_id(item)
        if item_id is None:
            raise AlignmentInputError(f"{context} must contain non-empty string IDs")
        if item_id not in seen:
            result.append(item_id)
            seen.add(item_id)
    return result


def _normalise_label(value: Any, *, context: str) -> str:
    label = _string_id(value)
    if label is None:
        raise AlignmentInputError(f"{context} is missing inclusion_label")
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
        "trivial": "exclude",
    }
    normalised = aliases.get(label.casefold(), label.casefold())
    if normalised not in LABELS:
        raise AlignmentInputError(
            f"{context} has unsupported inclusion_label {value!r}; expected must/may/exclude"
        )
    return normalised


def _extract_label(row: Mapping[str, Any], *, context: str) -> str:
    has_canonical = "inclusion_label" in row
    has_alias = "decision" in row
    if not has_canonical and not has_alias:
        raise AlignmentInputError(f"{context} is missing inclusion_label")
    canonical = _normalise_label(row.get("inclusion_label"), context=context) if has_canonical else None
    alias = _normalise_label(row.get("decision"), context=context) if has_alias else None
    if canonical is not None and alias is not None and canonical != alias:
        raise AlignmentInputError(f"{context} has conflicting inclusion_label and decision")
    return canonical or alias  # type: ignore[return-value]


def _extract_evidence(row: Mapping[str, Any], *, context: str) -> List[str]:
    if "evidence_message_ids" in row:
        value = row.get("evidence_message_ids")
    else:
        value = row.get("evidence_refs", [])
    return _ids(value, context=f"{context}.evidence_message_ids")


def _canonical_slot_value(row: Mapping[str, Any], field: str) -> Any:
    if field == "object_or_recipient" and field not in row and "object" in row:
        return row.get("object")
    return row.get(field)


def _comparison_value(value: Any) -> Any:
    """Return a deterministic exact-comparison representation.

    This normalization only handles case/whitespace and explicit unknown
    spellings.  It intentionally does not perform fuzzy or semantic text
    matching.
    """

    if value is None:
        return ("missing",)
    if isinstance(value, Mapping):
        return (
            "mapping",
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (list, tuple, set)):
        values = [_comparison_value(item) for item in value]
        return ("list", tuple(sorted(values, key=repr)))
    text = str(value).strip()
    folded = text.casefold()
    if folded in UNKNOWN_ALIASES:
        return ("unknown",)
    return ("text", folded)


def _disagreements(a: Mapping[str, Any], b: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for field in (*SLOTS, "inclusion_label"):
        if field == "inclusion_label":
            left = a["inclusion_label"]
            right = b["inclusion_label"]
        else:
            left = _canonical_slot_value(a["row"], field)
            right = _canonical_slot_value(b["row"], field)
        if _comparison_value(left) != _comparison_value(right):
            result.append({"field": field, "round_a": copy.deepcopy(left), "round_b": copy.deepcopy(right)})
    return result


def _pair_record(a: Mapping[str, Any], b: Mapping[str, Any], intersection: Iterable[str]) -> Dict[str, Any]:
    return {
        "round_a_acu_id": a["acu_id"],
        "round_b_acu_id": b["acu_id"],
        "evidence_intersection": sorted(set(intersection)),
        "disagreements": _disagreements(a, b),
    }


def _load_round(path: Union[Path, str], *, round_name: str) -> List[Dict[str, Any]]:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AlignmentInputError(f"cannot read {round_name} JSONL {path}: {exc}") from exc

    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AlignmentInputError(
                f"{round_name} line {line_number} is not valid JSON: {exc.msg}"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise AlignmentInputError(f"{round_name} line {line_number} must be a JSON object")
        context = f"{round_name} line {line_number}"
        acu_id = _string_id(parsed.get("acu_id", parsed.get("id")))
        if acu_id is None:
            raise AlignmentInputError(f"{context} is missing acu_id")
        if acu_id in seen:
            raise AlignmentInputError(f"duplicate {round_name} acu_id {acu_id!r} on line {line_number}")
        seen.add(acu_id)
        rows.append(
            {
                "acu_id": acu_id,
                "row": copy.deepcopy(dict(parsed)),
                "evidence_ids": _extract_evidence(parsed, context=context),
                "inclusion_label": _extract_label(parsed, context=context),
            }
        )
    return rows


def align_rows(
    round_a_rows: Sequence[Mapping[str, Any]],
    round_b_rows: Sequence[Mapping[str, Any]],
    *,
    round_a_source: Optional[str] = None,
    round_b_source: Optional[str] = None,
) -> Dict[str, Any]:
    """Align two already-loaded canonical ACU row sequences.

    The sequences may be raw codebook dictionaries or the internal records
    returned by ``_load_round``.  Using this function in tests keeps all
    matching decisions deterministic and independent of filesystem layout.
    """

    def prepare(rows: Sequence[Mapping[str, Any]], round_name: str) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for index, raw in enumerate(rows, 1):
            if "row" in raw and "evidence_ids" in raw and "inclusion_label" in raw:
                record = {
                    "acu_id": raw.get("acu_id"),
                    "row": copy.deepcopy(dict(raw["row"])),
                    "evidence_ids": list(raw.get("evidence_ids", [])),
                    "inclusion_label": raw.get("inclusion_label"),
                }
                acu_id = _string_id(record["acu_id"])
                if acu_id is None:
                    raise AlignmentInputError(f"{round_name} row {index} is missing acu_id")
                record["acu_id"] = acu_id
            else:
                if not isinstance(raw, Mapping):
                    raise AlignmentInputError(f"{round_name} row {index} must be a JSON object")
                acu_id = _string_id(raw.get("acu_id", raw.get("id")))
                if acu_id is None:
                    raise AlignmentInputError(f"{round_name} row {index} is missing acu_id")
                record = {
                    "acu_id": acu_id,
                    "row": copy.deepcopy(dict(raw)),
                    "evidence_ids": _extract_evidence(raw, context=f"{round_name} row {index}"),
                    "inclusion_label": _extract_label(raw, context=f"{round_name} row {index}"),
                }
            if record["acu_id"] in seen:
                raise AlignmentInputError(f"duplicate {round_name} acu_id {record['acu_id']!r}")
            seen.add(record["acu_id"])
            prepared.append(record)
        return prepared

    a_rows = prepare(round_a_rows, "round_a")
    b_rows = prepare(round_b_rows, "round_b")
    a_by_id = {row["acu_id"]: row for row in a_rows}
    b_by_id = {row["acu_id"]: row for row in b_rows}

    edges: Dict[str, Dict[str, Set[str]]] = {}
    reverse: Dict[str, Dict[str, Set[str]]] = {}
    for a in a_rows:
        a_evidence = set(a["evidence_ids"])
        if not a_evidence:
            continue
        for b in b_rows:
            intersection = a_evidence.intersection(b["evidence_ids"])
            if not intersection:
                continue
            edges.setdefault(a["acu_id"], {})[b["acu_id"]] = intersection
            reverse.setdefault(b["acu_id"], {})[a["acu_id"]] = intersection

    visited_a: Set[str] = set()
    visited_b: Set[str] = set()
    matched_pairs: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []

    for start_a in sorted(edges):
        if start_a in visited_a:
            continue
        component_a: Set[str] = set()
        component_b: Set[str] = set()
        pending_a = [start_a]
        while pending_a:
            current_a = pending_a.pop()
            if current_a in component_a:
                continue
            component_a.add(current_a)
            for b_id in edges.get(current_a, {}):
                if b_id in component_b:
                    continue
                component_b.add(b_id)
                for next_a in reverse.get(b_id, {}):
                    if next_a not in component_a:
                        pending_a.append(next_a)

        visited_a.update(component_a)
        visited_b.update(component_b)
        candidate_pairs = [
            _pair_record(a_by_id[a_id], b_by_id[b_id], edges[a_id][b_id])
            for a_id in sorted(component_a)
            for b_id in sorted(edges.get(a_id, {}))
            if b_id in component_b
        ]
        if len(component_a) == 1 and len(component_b) == 1:
            matched_pairs.extend(candidate_pairs)
        else:
            ambiguous.append(
                {
                    "round_a_acu_ids": sorted(component_a),
                    "round_b_acu_ids": sorted(component_b),
                    "candidate_pairs": candidate_pairs,
                    "round_a_rows": [copy.deepcopy(a_by_id[item]["row"]) for item in sorted(component_a)],
                    "round_b_rows": [copy.deepcopy(b_by_id[item]["row"]) for item in sorted(component_b)],
                    "resolution": "unresolved",
                    "reason": "direct_evidence_id_many_to_many",
                }
            )

    unmatched_a: List[Dict[str, Any]] = []
    unmatched_b: List[Dict[str, Any]] = []
    for row in a_rows:
        acu_id = row["acu_id"]
        if acu_id not in visited_a:
            unmatched_a.append(
                {
                    "acu_id": acu_id,
                    "reason": "missing_direct_evidence_ids" if not row["evidence_ids"] else "no_direct_evidence_id_intersection",
                    "row": copy.deepcopy(row["row"]),
                }
            )
    for row in b_rows:
        acu_id = row["acu_id"]
        if acu_id not in visited_b:
            unmatched_b.append(
                {
                    "acu_id": acu_id,
                    "reason": "missing_direct_evidence_ids" if not row["evidence_ids"] else "no_direct_evidence_id_intersection",
                    "row": copy.deepcopy(row["row"]),
                }
            )

    matched_pairs.sort(key=lambda pair: (pair["round_a_acu_id"], pair["round_b_acu_id"]))
    ambiguous.sort(key=lambda item: (item["round_a_acu_ids"], item["round_b_acu_ids"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "policy": {
            "alignment_basis": "exact_non_empty_intersection_of_evidence_message_ids",
            "slot_fields": list(SLOTS),
            "reported_disagreement_fields": [*SLOTS, "inclusion_label"],
            "text_similarity_used": False,
            "forced_one_to_one_assignment": False,
            "total_score": None,
            "fixed_acu_count": None,
        },
        "inputs": {
            "round_a": {"source": round_a_source, "row_count": len(a_rows)},
            "round_b": {"source": round_b_source, "row_count": len(b_rows)},
        },
        "matched_pairs": matched_pairs,
        "ambiguous_many_to_many": ambiguous,
        "unmatched": {"round_a": unmatched_a, "round_b": unmatched_b},
        "summary": {
            "round_a_rows": len(a_rows),
            "round_b_rows": len(b_rows),
            "direct_evidence_edges": sum(len(neighbors) for neighbors in edges.values()),
            "matched_pair_count": len(matched_pairs),
            "ambiguous_component_count": len(ambiguous),
            "ambiguous_round_a_row_count": sum(len(item["round_a_acu_ids"]) for item in ambiguous),
            "ambiguous_round_b_row_count": sum(len(item["round_b_acu_ids"]) for item in ambiguous),
            "unmatched_round_a_count": len(unmatched_a),
            "unmatched_round_b_count": len(unmatched_b),
        },
    }


def align_files(round_a_path: Union[Path, str], round_b_path: Union[Path, str]) -> Dict[str, Any]:
    """Load and align two JSONL annotation rounds."""

    a_path = Path(round_a_path)
    b_path = Path(round_b_path)
    a_rows = _load_round(a_path, round_name="round_a")
    b_rows = _load_round(b_path, round_name="round_b")
    return align_rows(a_rows, b_rows, round_a_source=str(a_path), round_b_source=str(b_path))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Align two ACU JSONL rounds by exact evidence-message-ID intersection; "
            "retain unmatched and unresolved many-to-many rows."
        )
    )
    parser.add_argument("--round-a", required=True, help="first ACU annotation JSONL")
    parser.add_argument("--round-b", required=True, help="second ACU annotation JSONL")
    parser.add_argument("--output", help="output JSON path (default: stdout)")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = align_files(args.round_a, args.round_b)
    except AlignmentInputError as exc:
        print(f"daily_brief_acu_align: {exc}", file=sys.stderr)
        return 2

    if args.compact:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    else:
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI test
    raise SystemExit(main())
