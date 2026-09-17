"""Run the private development-only Stage 1 context pilot.

The runner is an offline adapter around :mod:`contextual_fragments`.  It reads
one explicitly supplied ``development/messages.private.jsonl`` file, maps its
already-redacted text into the extractor's public ``content`` field in
memory, and writes three private artifacts:

* ``predictions.private.jsonl``: typed extractor DTOs, including private
  redacted text needed for local review;
* ``review_queue.private.jsonl``: uncertain fragments/relations with enough
  private evidence for a local reviewer; and
* ``aggregate.private.json`` plus ``manifest.private.json``: structure-only
  counts, rates, hashes, and N/A scoring status.

This module never loads gold labels, claims, clusters, presentations, frozen
files, or event/title/frontend code.  It deliberately refuses a path whose
last component is not ``development`` or whose ancestors are named
``frozen``/``frozen_test``.  The aggregate serializer has a second body-field
guard so a future DTO field cannot accidentally publish text there.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from . import contextual_fragments as _extractor


RUNNER_SCHEMA_VERSION = "stage1_context_development_v1"
SPLIT_DEVELOPMENT = "development"
LOCAL_DAY = "2026-08-25"
INPUT_FILENAME = "messages.private.jsonl"
PREDICTIONS_FILENAME = "predictions.private.jsonl"
AGGREGATE_FILENAME = "aggregate.private.json"
REVIEW_QUEUE_FILENAME = "review_queue.private.jsonl"
MANIFEST_FILENAME = "manifest.private.json"
TERMINAL_STATES = frozenset({"resolved", "failed", "cancelled"})
CONTEXT_LABELS = frozenset(
    {"continues", "elaborates", "answers", "contrasts", "topic_shift", "possibly_related", "insufficient"}
)
CONTEXT_STRENGTHS = frozenset({"strong", "medium", "weak", "none"})

# Aggregate output is intentionally free of all body-bearing/free-form fields.
_BODY_FIELD_NAMES = frozenset(
    {
        "redacted_text", "fragment_text_redacted", "surface_redacted", "claim_text_redacted",
        "text", "content", "message_text", "evidence_text", "title_redacted", "summary_of_boundary_redacted",
        "uncertainties", "annotator_notes", "notes", "body", "raw_text", "raw_message", "provenance",
    }
)


@dataclass(frozen=True)
class PilotRunResult:
    """Paths and aggregate metadata for one private pilot run."""

    input_directory: str
    output_directory: str
    message_count: int
    fragment_count: int
    relation_count: int
    manifest_path: str
    predictions_path: str
    aggregate_path: str
    review_queue_path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_directory": self.input_directory,
            "output_directory": self.output_directory,
            "message_count": self.message_count,
            "fragment_count": self.fragment_count,
            "relation_count": self.relation_count,
            "manifest_path": self.manifest_path,
            "predictions_path": self.predictions_path,
            "aggregate_path": self.aggregate_path,
            "review_queue_path": self.review_queue_path,
        }


def _guard_development_directory(directory: Union[str, Path]) -> Path:
    root = Path(directory)
    if root.name.casefold() != SPLIT_DEVELOPMENT:
        raise ValueError("Stage1 pilot input must end in development")
    if any(part.casefold() in {"frozen", "frozen_test"} for part in root.parts):
        raise ValueError("Stage1 pilot refuses frozen directories")
    if not root.is_dir():
        raise ValueError("Stage1 pilot input directory does not exist")
    return root


def _read_development_messages(directory: Union[str, Path]) -> Tuple[Tuple[Dict[str, Any], ...], bytes]:
    root = _guard_development_directory(directory)
    path = root / INPUT_FILENAME
    if not path.is_file():
        raise ValueError("development messages.private.jsonl is missing")
    raw = path.read_bytes()
    messages: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid development message JSON at line %d" % line_number) from exc
        if not isinstance(value, Mapping):
            raise ValueError("development message line %d is not an object" % line_number)
        message = dict(value)
        if message.get("split") != SPLIT_DEVELOPMENT:
            raise ValueError("development input contains a non-development message")
        if message.get("local_day") not in (None, LOCAL_DAY):
            raise ValueError("development input contains a message outside 2026-08-25")
        message_id = str(message.get("message_id") or "")
        if not message_id:
            raise ValueError("development message line %d is missing message_id" % line_number)
        if message_id in seen:
            raise ValueError("duplicate development message_id")
        seen.add(message_id)
        messages.append(message)
    if not messages:
        raise ValueError("development message file is empty")
    return tuple(messages), raw


def _extractor_messages(messages: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Any], ...]:
    """Copy only public extractor fields and map redacted_text to content."""

    allowed = (
        "message_id", "chat_id", "account_id", "speaker_id", "sender_id", "sender_name",
        "content", "text", "message_text",
        "message_type", "timestamp", "time_offset_seconds", "time_offset", "sequence_in_chat",
        "sequence", "reply_to_message_id", "reply_to_id", "quoted_message_id", "quote_message_id",
        "referenced_message_id", "reference_message_id", "parent_message_id", "in_reply_to",
        "dialogue_segment_id", "dialogue_role", "message_role", "mentioned_persons", "mentioned_people",
        "person_mentions", "mentions", "object", "object_ref", "objects", "object_refs", "target",
        "target_entity",
    )
    output: List[Dict[str, Any]] = []
    for message in messages:
        normalized = {field: message[field] for field in allowed if field in message}
        # contextual_fragments intentionally does not inspect redacted_text;
        # this one-way in-memory adaptation is the runner's boundary.
        if "content" not in normalized:
            text = message.get("redacted_text")
            if text is not None:
                normalized["content"] = text
        output.append(normalized)
    return tuple(output)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_files(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _counter(values: Iterable[Any]) -> Dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _rate(numerator: int, denominator: int) -> Union[float, str]:
    return round(numerator / denominator, 6) if denominator else "N/A"


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _unknown(value: Any) -> bool:
    return value in (None, "", "unknown", "UNKNOWN", "ACTOR_UNKNOWN", "OBJECT_UNKNOWN")


def _object_unknown(fragment: Any) -> bool:
    resolution = _value(fragment, "object_resolution", "unknown")
    object_id = str(_value(fragment, "object_id", "unknown") or "unknown")
    return resolution == "unknown" or object_id in {"unknown", "OBJECT_UNKNOWN"}


def _time_only_relation(relation: Any) -> bool:
    if _value(relation, "explicit_reply_present", False) is not False:
        return False
    if _value(relation, "time_evidence", "none") != "weak":
        return False
    signals = {
        str(value).casefold()
        for value in (_value(relation, "supporting_signals", ()) or _value(relation, "supporting_slot_codes", ()) or ())
    }
    time_signals = {"time", "time_near", "time_proximity", "time_proximity_weak", "temporal", "time_distance"}
    return bool(signals) and signals.issubset(time_signals)


def _terminal_violation(item: Any, kind: str) -> bool:
    state = str(_value(item, "state", "unknown") or "unknown")
    if state not in TERMINAL_STATES:
        return False
    evidence_state = str(_value(item, "state_evidence", "unknown") or "unknown")
    if evidence_state in {"", "unknown"}:
        return True
    if kind == "fragment":
        return not bool(_value(item, "evidence_refs", ()))
    if kind == "claim":
        return not bool(_value(item, "evidence_spans", ()))
    if kind == "state_assertion":
        return not bool(_value(item, "evidence_text", "")) or _value(item, "span_start") is None
    return False


def _body_free(value: Any) -> Any:
    """Drop body/free-form fields recursively for aggregate safety."""

    if isinstance(value, Mapping):
        return {
            str(key): _body_free(item)
            for key, item in value.items()
            if not _is_body_key(str(key))
        }
    if isinstance(value, (tuple, list)):
        return [_body_free(item) for item in value]
    if isinstance(value, set):
        return sorted(_body_free(item) for item in value)
    return value


def _is_body_key(key: str) -> bool:
    lower = key.casefold()
    return (
        key in _BODY_FIELD_NAMES
        or lower.endswith("_text")
        or lower.endswith("_content")
        or lower.endswith("_surface")
        or lower.endswith("_title")
        or lower.endswith("_summary")
    )


def _dto_row(kind: str, item: Any) -> Dict[str, Any]:
    payload = item.to_dict() if hasattr(item, "to_dict") else dict(item)
    payload = dict(payload)
    payload["record_type"] = kind
    record_id = _value(item, "id", None)
    if record_id in (None, ""):
        record_id = _value(item, "fragment_id", None) or _value(item, "claim_id", None) or _value(item, "relation_id", None)
    payload["prediction_id"] = str(record_id or "unknown")
    return payload


def _prediction_rows(result: Any) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    for kind, values in (
        ("fragment", result.fragments), ("person", result.persons), ("object", result.object_refs),
        ("state_assertion", result.state_assertions), ("argument", result.arguments),
        ("claim", result.claims), ("context_relation", result.relations),
    ):
        rows.extend(_dto_row(kind, item) for item in values)
    return tuple(rows)


def _review_rows(result: Any) -> Tuple[Dict[str, Any], ...]:
    rows: List[Dict[str, Any]] = []
    for fragment in result.fragments:
        reasons: List[str] = []
        if _unknown(_value(fragment, "speaker_id", "unknown")):
            reasons.append("SPEAKER_UNKNOWN")
        if _unknown(_value(fragment, "subject_id", "unknown")):
            reasons.append("SUBJECT_UNKNOWN")
        resolution = str(_value(fragment, "object_resolution", "unknown") or "unknown")
        if resolution == "unknown":
            reasons.append("OBJECT_UNKNOWN")
        elif resolution == "inherited":
            reasons.append("OBJECT_INHERITED")
        if str(_value(fragment, "state", "unknown") or "unknown") == "unknown":
            reasons.append("STATE_UNKNOWN")
        if _terminal_violation(fragment, "fragment"):
            reasons.append("TERMINAL_EVIDENCE_MISSING")
        if bool(_value(fragment, "topic_shift", False)):
            reasons.append("TOPIC_BOUNDARY")
        if _value(fragment, "role", "substantive") in {"conversation_opener", "context_only"}:
            reasons.append("NON_SUBSTANTIVE_FRAGMENT")
        if reasons:
            rows.append(
                {
                    "review_id": "review:%s" % str(_value(fragment, "fragment_id", "unknown")),
                    "record_type": "fragment",
                    "record_id": str(_value(fragment, "fragment_id", "unknown")),
                    "message_id": str(_value(fragment, "message_id", "unknown")),
                    "fragment_id": str(_value(fragment, "fragment_id", "unknown")),
                    "reason_codes": sorted(set(reasons)),
                    "confidence": str(_value(fragment, "source", "unknown")),
                    "text": str(_value(fragment, "text", "")),
                    "evidence_text": str(_value(fragment, "evidence_text", "")),
                    "span": {"start": _value(fragment, "span_start", 0), "end": _value(fragment, "span_end", 0)},
                    "object_resolution": resolution,
                    "state": str(_value(fragment, "state", "unknown") or "unknown"),
                }
            )
    for relation in result.relations:
        reasons = []
        label = str(_value(relation, "relation", _value(relation, "label", "insufficient")) or "insufficient")
        strength = str(_value(relation, "evidence_strength", "none") or "none")
        if bool(_value(relation, "requires_review", True)):
            reasons.append("RELATION_CANDIDATE")
        if label in {"possibly_related", "insufficient"}:
            reasons.append("LOW_SEMANTIC_CERTAINTY")
        if _value(relation, "explicit_reply_present", False) is False:
            reasons.append("NO_EXPLICIT_REPLY")
        if _time_only_relation(relation):
            reasons.append("TIME_ONLY_SIGNAL")
        if strength in {"weak", "none"}:
            reasons.append("WEAK_EVIDENCE")
        if reasons:
            rows.append(
                {
                    "review_id": "review:%s" % str(_value(relation, "relation_id", "unknown")),
                    "record_type": "context_relation",
                    "record_id": str(_value(relation, "relation_id", "unknown")),
                    "left_fragment_id": str(_value(relation, "left_fragment_id", "unknown")),
                    "right_fragment_id": str(_value(relation, "right_fragment_id", "unknown")),
                    "reason_codes": sorted(set(reasons)),
                    "label": label,
                    "evidence_strength": strength,
                    "explicit_reply_present": bool(_value(relation, "explicit_reply_present", False)),
                    "time_evidence": str(_value(relation, "time_evidence", "none") or "none"),
                    "supporting_signals": list(_value(relation, "supporting_signals", ()) or ()),
                    "conflicting_signals": list(_value(relation, "conflicting_signals", ()) or ()),
                }
            )
    return tuple(rows)


def _aggregate(result: Any, messages: Sequence[Mapping[str, Any]], *, input_sha256: str, code_sha256: str) -> Dict[str, Any]:
    fragments = tuple(result.fragments)
    relations = tuple(result.relations)
    claims = tuple(result.claims)
    persons = tuple(result.persons)
    arguments = tuple(result.arguments)
    objects = tuple(result.object_refs)
    states = tuple(result.state_assertions)
    terminal_items = [
        (_terminal_violation(item, "fragment"), "fragment") for item in fragments
    ] + [
        (_terminal_violation(item, "claim"), "claim") for item in claims
    ] + [
        (_terminal_violation(item, "state_assertion"), "state_assertion") for item in states
    ]
    terminal_total = sum(
        str(_value(item, "state", "unknown") or "unknown") in TERMINAL_STATES
        for item in list(fragments) + list(claims) + list(states)
    )
    terminal_violations = sum(value for value, _ in terminal_items)
    strong_relations = [item for item in relations if _value(item, "evidence_strength", "none") == "strong"]
    time_only_strong = sum(_time_only_relation(item) for item in strong_relations)
    object_known = sum(not _object_unknown(item) for item in fragments)
    object_inherited = sum(_value(item, "object_resolution", "unknown") == "inherited" for item in fragments)
    subject_unknown = sum(_unknown(_value(item, "subject_id", "unknown")) for item in fragments)
    state_unknown = sum(_value(item, "state", "unknown") == "unknown" for item in fragments)
    speaker_unknown = sum(_unknown(_value(item, "speaker_id", "unknown")) for item in fragments)
    person_unknown = sum(_unknown(_value(item, "person_id", "unknown")) for item in persons)
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "scope": SPLIT_DEVELOPMENT,
        "split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "prediction_is_not_gold": True,
        "gold_comparison": "N/A",
        "input": {
            "message_count": len(messages),
            "split_counts": _counter(_value(item, "split", "unknown") for item in messages),
            "input_sha256": input_sha256,
        },
        "code": {"code_sha256": code_sha256, "extractor_module": "contextual_fragments"},
        "records": {
            "messages": len(messages), "fragments": len(fragments), "persons": len(persons),
            "objects": len(objects), "state_assertions": len(states), "arguments": len(arguments),
            "claims": len(claims), "context_relations": len(relations),
        },
        "enum_counts": {
            "fragment_role": _counter(_value(item, "role", "unknown") for item in fragments),
            "fragment_type": _counter(_value(item, "fragment_type", "unknown") for item in fragments),
            "person_role": _counter(_value(item, "role", "unknown") for item in persons),
            "person_resolution": _counter(_value(item, "resolution", "unknown") for item in persons),
            "object_resolution": _counter(_value(item, "object_resolution", "unknown") for item in fragments),
            "argument_resolution": _counter(_value(item, "resolution", "unknown") for item in arguments),
            "state": _counter(_value(item, "state", "unknown") for item in fragments),
            "claim_state": _counter(_value(item, "state", "unknown") for item in claims),
            "state_assertion": _counter(_value(item, "state", "unknown") for item in states),
            "state_evidence": _counter(_value(item, "state_evidence", "unknown") for item in fragments),
            "modality": _counter(_value(item, "modality", "unknown") for item in fragments),
            "claim_type": _counter(_value(item, "claim_type", "unknown") for item in claims),
            "intent": _counter(_value(item, "intent", "unknown") for item in fragments),
            "information_value": _counter(_value(item, "information_value", "unknown") for item in fragments),
            "event_completeness": _counter(_value(item, "event_completeness", "unknown") for item in fragments),
            "context_relation_label": _counter(_value(item, "relation", _value(item, "label", "unknown")) for item in relations),
            "context_relation_strength": _counter(_value(item, "evidence_strength", "none") for item in relations),
            "context_time_evidence": _counter(_value(item, "time_evidence", "none") for item in relations),
            "context_reply_flag": _counter(bool(_value(item, "explicit_reply_present", False)) for item in relations),
        },
        "unknown_rates": {
            "fragment_speaker": _rate(speaker_unknown, len(fragments)),
            "fragment_subject": _rate(subject_unknown, len(fragments)),
            "fragment_object": _rate(sum(_object_unknown(item) for item in fragments), len(fragments)),
            "fragment_state": _rate(state_unknown, len(fragments)),
            "person_identity": _rate(person_unknown, len(persons)),
            "claim_state": _rate(sum(_value(item, "state", "unknown") == "unknown" for item in claims), len(claims)),
        },
        "inheritance": {
            "fragment_object_inherited_count": object_inherited,
            "fragment_object_resolution_denominator": len(fragments),
            "fragment_object_inherited_rate": _rate(object_inherited, len(fragments)),
            "known_object_denominator": object_known,
        },
        "context_relations": {
            "candidate_count": len(relations),
            "strong_count": len(strong_relations),
            "time_only_strong_link_count": time_only_strong,
            "time_only_strong_link_rate": _rate(time_only_strong, len(strong_relations)),
            "time_only_any_strength_count": sum(_time_only_relation(item) for item in relations),
            "label_vocabulary_valid": all(_value(item, "relation", _value(item, "label", "")) in CONTEXT_LABELS for item in relations),
            "strength_vocabulary_valid": all(_value(item, "evidence_strength", "") in CONTEXT_STRENGTHS for item in relations),
        },
        "terminal_evidence": {
            "terminal_count": terminal_total,
            "violation_count": terminal_violations,
            "violation_rate": _rate(terminal_violations, terminal_total),
        },
        "boundary_distribution": {
            "topic_boundary": _counter(_value(item, "topic_boundary", "unknown") for item in fragments),
            "topic_shift_true_count": sum(bool(_value(item, "topic_shift", False)) for item in fragments),
            "reply_to_present_count": sum(bool(_value(item, "reply_to_message_id", None)) for item in fragments),
            "segment_count": len(set(str(_value(item, "segment_id", "unknown")) for item in fragments)),
        },
        "review": {
            "queue_count": len(_review_rows(result)),
            "reason_counts": _counter(
                reason
                for row in _review_rows(result)
                for reason in row.get("reason_codes", ())
            ),
        },
        "scoring": {
            "status": "N/A",
            "reason": "development pilot contains predictions only; no gold labels were loaded",
            "fields": {
                "speaker_vs_mentioned_person": "N/A",
                "subject": "N/A",
                "object_resolution": "N/A",
                "state": "N/A",
                "modality": "N/A",
                "fragment_role": "N/A",
                "context_relation_label": "N/A",
                "context_relation_strength": "N/A",
            },
        },
    }


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False))
            handle.write("\n")
            count += 1
    return count


def run_stage1_development_pilot(
    input_directory: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    overwrite: bool = False,
) -> PilotRunResult:
    """Run one development-only private Stage 1 pilot.

    ``overwrite=False`` is the default safety guard: a new versioned output
    directory is required for every run.  The function returns only paths and
    counts; callers should inspect the private aggregate file locally.
    """

    root = _guard_development_directory(input_directory)
    output = Path(output_directory)
    if output.exists() and not overwrite:
        raise FileExistsError("Stage1 output directory already exists")
    if output == root:
        raise ValueError("Stage1 output must be separate from development input")
    messages, input_bytes = _read_development_messages(root)
    output.mkdir(parents=True, exist_ok=overwrite)
    # The extractor receives only normalized public fields; no raw/private key
    # fallback is available inside contextual_fragments.
    result = _extractor.extract_context_fragments(_extractor_messages(messages))
    code_paths = [Path(__file__), Path(_extractor.__file__)]
    code_sha256 = _sha256_files(code_paths)
    input_sha256 = _sha256_bytes(input_bytes)

    predictions_path = output / PREDICTIONS_FILENAME
    review_path = output / REVIEW_QUEUE_FILENAME
    aggregate_path = output / AGGREGATE_FILENAME
    manifest_path = output / MANIFEST_FILENAME
    prediction_count = _write_jsonl(predictions_path, _prediction_rows(result))
    review_count = _write_jsonl(review_path, _review_rows(result))
    aggregate = _aggregate(result, messages, input_sha256=input_sha256, code_sha256=code_sha256)
    aggregate["output"] = {
        "predictions_record_count": prediction_count,
        "review_queue_record_count": review_count,
        "body_free": True,
        "files": [AGGREGATE_FILENAME, MANIFEST_FILENAME, PREDICTIONS_FILENAME, REVIEW_QUEUE_FILENAME],
    }
    aggregate = _body_free(aggregate)
    _write_json(aggregate_path, aggregate)
    manifest = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "split": SPLIT_DEVELOPMENT,
        "dataset_split": SPLIT_DEVELOPMENT,
        "local_day": LOCAL_DAY,
        "source_file": INPUT_FILENAME,
        "input_sha256": input_sha256,
        "code_sha256": code_sha256,
        "runner_code_sha256": _sha256_files([Path(__file__)]),
        "extractor_code_sha256": _sha256_files([Path(_extractor.__file__)]),
        "prediction_is_not_gold": True,
        "gold_loaded": False,
        "frozen_read": False,
        "private_output": True,
        "record_counts": {
            "messages": len(messages), "predictions": prediction_count, "review_queue": review_count,
            "fragments": len(result.fragments), "context_relations": len(result.relations),
        },
        "output_files": [AGGREGATE_FILENAME, MANIFEST_FILENAME, PREDICTIONS_FILENAME, REVIEW_QUEUE_FILENAME],
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    _write_json(manifest_path, manifest)
    return PilotRunResult(
        input_directory=str(root), output_directory=str(output), message_count=len(messages),
        fragment_count=len(result.fragments), relation_count=len(result.relations),
        manifest_path=str(manifest_path), predictions_path=str(predictions_path),
        aggregate_path=str(aggregate_path), review_queue_path=str(review_path),
    )


run_stage1_pilot = run_stage1_development_pilot


__all__ = [
    "RUNNER_SCHEMA_VERSION", "SPLIT_DEVELOPMENT", "LOCAL_DAY", "INPUT_FILENAME", "PREDICTIONS_FILENAME",
    "AGGREGATE_FILENAME", "REVIEW_QUEUE_FILENAME", "MANIFEST_FILENAME", "PilotRunResult",
    "run_stage1_development_pilot", "run_stage1_pilot",
]
