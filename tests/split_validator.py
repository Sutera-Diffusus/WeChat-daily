"""Public, dependency-free validation for private evaluation splits.

The semantic gold files are intentionally kept outside the repository.  This
module only knows the structure needed to validate a split: a conversation
block is assigned to exactly one split, graph records cannot cross that
assignment, and a frozen-test manifest is versioned and hash checked.  It does
not import the production analysis path and it never needs to inspect message
text.

The main entry points are :func:`validate_split_records` for in-memory
synthetic fixtures and :func:`validate_split_directories` for the two private
split directories produced by the offline builder.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


SPLITS = ("development", "frozen_test")
COLLECTIONS = (
    "messages",
    "mentions",
    "claims",
    "relations",
    "clusters",
    "presentations",
    "adjudications",
)
COLLECTION_FILES = {name: f"{name}.private.jsonl" for name in COLLECTIONS}
ID_FIELDS = {
    "messages": "message_id",
    "mentions": "mention_id",
    "claims": "claim_id",
    "relations": "relation_id",
    "clusters": "cluster_id",
    "presentations": "presentation_id",
    "adjudications": "adjudication_id",
}
RELATION_LABELS = (
    "same_event",
    "related_event",
    "same_topic_only",
    "unrelated",
    "insufficient_context",
)
TIME_BUCKETS = ("early", "morning", "afternoon", "evening", "late")
CONTEXT_ROLES = frozenset(
    {
        "context",
        "context_only",
        "conversation_opener",
        "event_context_only",
    }
)


@dataclass(frozen=True)
class SplitValidationResult:
    """Structured result returned by the public validators."""

    ok: bool
    errors: Tuple[str, ...]
    warnings: Tuple[str, ...]
    summary: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "summary": dict(self.summary),
        }


def canonical_json(value: Any) -> bytes:
    """Return the stable UTF-8 representation used by all artifact hashes."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_text(value: Any) -> str:
    return str(value) if value is not None else ""


def _record_id(collection: str, record: Mapping[str, Any]) -> str:
    field = ID_FIELDS[collection]
    return _as_text(record.get(field) or record.get("record_id"))


def _record_block_id(record: Mapping[str, Any]) -> str:
    """Read both contract rows and pilot rows without reading their text."""

    for field in ("block_id", "conversation_block_id", "isolation_group_id"):
        value = record.get(field)
        if value:
            return _as_text(value)
    pilot = record.get("pilot")
    if isinstance(pilot, Mapping) and pilot.get("block_id"):
        return _as_text(pilot.get("block_id"))
    return ""


def _record_split(record: Mapping[str, Any]) -> str:
    for field in ("split", "split_name"):
        value = record.get(field)
        if value:
            return _as_text(value)
    return ""


def _record_type(collection: str, record: Mapping[str, Any]) -> str:
    value = record.get("record_type")
    return _as_text(value or collection[:-1])


def _json_ids(record: Mapping[str, Any], field: str) -> Tuple[str, ...]:
    value = record.get(field)
    if value is None:
        return ()

    def item_id(item: Any) -> str:
        # pre_release_v4 adjudication rows use explicit typed references such
        # as {"type": "claim", "id": "CLAIM_..."}.  Split validation only
        # needs the stable ID for isolation/graph checks; contract validation
        # remains responsible for validating the type and catalog membership.
        if isinstance(item, Mapping):
            return _as_text(
                item.get("id")
                or item.get("typed_id")
                or item.get("value")
            )
        return _as_text(item)

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        identifier = item_id(value)
        return (identifier,) if identifier else ()
    return tuple(
        identifier
        for item in value
        if (identifier := item_id(item))
    )


def _event_keys(record: Mapping[str, Any]) -> Tuple[str, ...]:
    """Return explicit event identities, falling back to cluster identity."""

    values: List[str] = []
    for field in ("event_id", "event_instance_id", "event_seed_id", "cluster_id"):
        value = record.get(field)
        if value:
            values.append(_as_text(value))
    return tuple(dict.fromkeys(values))


def _context_only(record: Optional[Mapping[str, Any]]) -> bool:
    if not record:
        return False
    for field in ("message_role", "dialogue_role", "dialogue_event_role", "event_role", "dialogue_event_role"):
        value = _as_text(record.get(field)).casefold()
        if value in CONTEXT_ROLES:
            return True
    return False


def _normalise_collections(value: Mapping[str, Any]) -> Dict[str, List[Mapping[str, Any]]]:
    """Accept a collection mapping while ignoring optional metadata keys."""

    output: Dict[str, List[Mapping[str, Any]]] = {}
    for collection in COLLECTIONS:
        rows = value.get(collection, [])
        if rows is None:
            rows = []
        output[collection] = list(rows) if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else []
    return output


def _references(
    collection: str,
    record: Mapping[str, Any],
) -> Dict[str, Tuple[str, ...]]:
    """Return typed graph references used for split-consistency checks."""

    if collection == "mentions":
        return {"messages": _json_ids(record, "message_id")}
    if collection == "claims":
        return {
            "messages": _json_ids(record, "message_id") + _json_ids(record, "context_message_ids"),
            "mentions": _json_ids(record, "event_mention_ids"),
        }
    if collection == "relations":
        return {
            "anchors": _json_ids(record, "left_anchor_id") + _json_ids(record, "right_anchor_id"),
            "messages": _json_ids(record, "evidence_message_ids"),
        }
    if collection == "clusters":
        return {
            "claims": _json_ids(record, "claim_ids"),
            "mentions": _json_ids(record, "mention_ids"),
            "messages": _json_ids(record, "member_message_ids")
            + _json_ids(record, "start_message_id")
            + _json_ids(record, "end_message_id"),
            "relations": _json_ids(record, "relation_ids"),
        }
    if collection == "presentations":
        return {
            "clusters": _json_ids(record, "source_cluster_ids"),
            "claims": _json_ids(record, "source_claim_ids")
            + _json_ids(record, "fact_claim_ids")
            + _json_ids(record, "opinion_claim_ids")
            + _json_ids(record, "question_claim_ids"),
            "presentations": _json_ids(record, "must_remain_separate_from"),
        }
    if collection == "adjudications":
        values = _json_ids(record, "evidence_ids")
        if not values:
            values = _json_ids(record, "evidence_refs")
        values += _json_ids(record, "anchor_id")
        values += _json_ids(record, "record_id")
        return {"any": tuple(dict.fromkeys(values))}
    return {}


def _counter_dict(values: Iterable[Any]) -> Dict[str, int]:
    return dict(sorted(Counter(_as_text(value) for value in values).items()))


def _empty_split_summary() -> Dict[str, Any]:
    return {
        "message_count": 0,
        "mention_count": 0,
        "claim_count": 0,
        "relation_count": 0,
        "cluster_count": 0,
        "presentation_count": 0,
        "adjudication_count": 0,
        "block_count": 0,
        "chat_count": 0,
        "chat_type": {},
        "time_bucket": {},
        "relation_labels": {},
        "must_not_link_count": 0,
        "context_only_message_count": 0,
        "event_count": 0,
    }


def _coverage_errors(summary: Mapping[str, Any], requirements: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    for split, required in requirements.items():
        if not isinstance(required, Mapping):
            continue
        values = summary.get(split) or {}
        for field, error_name in (
            ("chat_types", "chat type"),
            ("time_buckets", "time bucket"),
            ("relation_labels", "relation label"),
        ):
            expected = required.get(field) or ()
            observed = set((values.get("chat_type") if field == "chat_types" else values.get("time_bucket") if field == "time_buckets" else values.get("relation_labels")) or {})
            for item in expected:
                if _as_text(item) not in observed:
                    errors.append(f"{split} missing required {error_name} {_as_text(item)}")
        if required.get("must_not_link") and int(values.get("must_not_link_count", 0)) <= 0:
            errors.append(f"{split} missing required must_not_link coverage")
        if required.get("context_only") and int(values.get("context_only_message_count", 0)) <= 0:
            errors.append(f"{split} missing required context_only coverage")
        if required.get("min_messages") is not None and int(values.get("message_count", 0)) < int(required["min_messages"]):
            errors.append(f"{split} has fewer than required messages")
    return errors


_PUBLIC_LABEL_ACCESS_KEYS = frozenset(
    {
        "labels_frozen",
        "labels_readable_by_implementation",
        "frozen_test_labels_readable_by_implementation",
        "public_manifest_contains_labels",
        "frozen_test_label_access",
    }
)
_PUBLIC_LABEL_DISTRIBUTION_KEYS = frozenset(
    {
        "label_counts",
        "relation_labels",
        "label_distribution",
        "label_histogram",
        "must_not_link_count",
        "must_not_link_counts",
    }
)


def _public_manifest_label_errors(value: Any, path: str = "$") -> List[str]:
    """Reject label values/distributions on the public aggregate surface.

    Access-policy booleans are safe metadata and are intentionally allowed;
    actual labels, histograms, and label-derived counts are not.  This check
    only walks the supplied manifest object and never opens a split file.
    """

    errors: List[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = _as_text(key)
            key_folded = key_text.casefold()
            child_path = f"{path}.{key_text}"
            if key_folded in _PUBLIC_LABEL_DISTRIBUTION_KEYS:
                errors.append(f"aggregate manifest exposes label distribution at {child_path}")
                continue
            if "label" in key_folded and key_folded not in _PUBLIC_LABEL_ACCESS_KEYS:
                errors.append(f"aggregate manifest exposes label metadata at {child_path}")
                continue
            errors.extend(_public_manifest_label_errors(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_public_manifest_label_errors(child, f"{path}[{index}]"))
    return errors


def _manifest_freeze_errors(
    manifests: Mapping[str, Mapping[str, Any]],
    aggregate_manifest: Optional[Mapping[str, Any]],
) -> List[str]:
    errors: List[str] = []
    versions = {
        _as_text(item.get("split_version"))
        for item in manifests.values()
        if item.get("split_version")
    }
    dataset_versions = {
        _as_text(item.get("dataset_version"))
        for item in manifests.values()
        if item.get("dataset_version")
    }
    if len(versions) > 1:
        errors.append("split manifests disagree on split_version")
    if len(dataset_versions) > 1:
        errors.append("split manifests disagree on dataset_version")
    if aggregate_manifest:
        errors.extend(_public_manifest_label_errors(aggregate_manifest))
        aggregate_version = _as_text(aggregate_manifest.get("split_version"))
        if aggregate_version and versions and versions != {aggregate_version}:
            errors.append("aggregate manifest split_version does not match split manifests")
        aggregate_dataset_version = _as_text(aggregate_manifest.get("dataset_version"))
        if aggregate_dataset_version and dataset_versions and dataset_versions != {aggregate_dataset_version}:
            errors.append("aggregate manifest dataset_version does not match split manifests")
        if aggregate_manifest.get("frozen_test_labels_readable_by_implementation") is True:
            errors.append("aggregate manifest exposes frozen-test labels to implementation")
        declared = aggregate_manifest.get("coverage_requirements")
        if declared is not None and not isinstance(declared, Mapping):
            errors.append("coverage_requirements must be an object")
    frozen = manifests.get("frozen_test")
    if frozen is None:
        errors.append("frozen_test manifest is missing")
    else:
        if not frozen.get("labels_frozen"):
            errors.append("frozen_test labels_frozen must be true")
        if not frozen.get("frozen_at"):
            errors.append("frozen_test manifest is missing frozen_at")
        if _as_text(frozen.get("status")) not in {"frozen", "frozen_test"}:
            errors.append("frozen_test manifest status must be frozen or frozen_test")
        if frozen.get("supersedes") not in (None, ""):
            errors.append("frozen_test cannot silently supersede another version")
        file_hashes = frozen.get("file_sha256")
        if not isinstance(file_hashes, Mapping) or not file_hashes:
            errors.append("frozen_test manifest must contain file_sha256")
        freeze_digest = _as_text(frozen.get("freeze_digest"))
        if not freeze_digest:
            errors.append("frozen_test manifest is missing freeze_digest")
        else:
            payload = {
                "dataset_version": frozen.get("dataset_version"),
                "file_sha256": frozen.get("file_sha256") or {},
                "record_counts_by_file": frozen.get("record_counts_by_file") or {},
                "split": "frozen_test",
                "split_version": frozen.get("split_version"),
            }
            if freeze_digest != sha256_bytes(canonical_json(payload)):
                errors.append("frozen_test freeze_digest does not match manifest contents")
    return errors


def validate_split_records(
    split_collections: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    *,
    manifests: Optional[Mapping[str, Mapping[str, Any]]] = None,
    aggregate_manifest: Optional[Mapping[str, Any]] = None,
    policy: Optional[Mapping[str, Any]] = None,
    coverage_requirements: Optional[Mapping[str, Any]] = None,
) -> SplitValidationResult:
    """Validate in-memory records from development and frozen-test splits.

    ``split_collections`` is keyed by ``development`` and ``frozen_test``;
    each value is a mapping of contract collection names to JSON-like rows.
    Rows may omit ``split`` on derived annotation files: the validator infers
    it from their block/message/claim references.  This makes the function
    useful for small synthetic fixtures as well as the private artifacts.

    The default isolation policy is ``same_chat``.  It is intentionally
    conservative: assigning a whole chat to one split makes it impossible for
    adjacent messages, context windows, or a hidden same-event chain to leak
    across the boundary.  Callers may explicitly use ``adjacent_segment``
    when they provide a stricter precomputed isolation group in each row.
    """

    errors: List[str] = []
    warnings: List[str] = []
    policy = dict(policy or {})
    isolation_unit = _as_text(policy.get("isolation_unit") or "same_chat")
    if isolation_unit not in {"same_chat", "adjacent_segment", "conversation_block"}:
        errors.append(f"unknown isolation_unit {isolation_unit}")
        isolation_unit = "same_chat"

    split_names = set(split_collections)
    missing_splits = [name for name in SPLITS if name not in split_names]
    errors.extend(f"missing split {name}" for name in missing_splits)
    unknown_splits = sorted(split_names - set(SPLITS))
    errors.extend(f"unknown split {name}" for name in unknown_splits)

    datasets = {
        split: _normalise_collections(split_collections.get(split, {}))
        for split in SPLITS
    }
    manifests = dict(manifests or {})

    # Keep every row tied to the split in which it was supplied.  A row's
    # explicit split field is checked below; references are checked after all
    # typed IDs have been indexed.
    row_entries: Dict[str, List[Tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    indexes: Dict[str, Dict[str, Dict[str, Mapping[str, Any]]]] = {
        split: {collection: {} for collection in COLLECTIONS}
        for split in SPLITS
    }
    global_indexes: Dict[str, Dict[str, Set[str]]] = {collection: defaultdict(set) for collection in COLLECTIONS}
    block_rows: Dict[str, List[Tuple[str, str, Mapping[str, Any]]]] = defaultdict(list)
    block_metadata: Dict[str, Dict[str, Any]] = {}

    for split in SPLITS:
        for collection in COLLECTIONS:
            for index, record in enumerate(datasets[split][collection]):
                if not isinstance(record, Mapping):
                    errors.append(f"{split}.{collection}[{index}] must be an object")
                    continue
                record_id = _record_id(collection, record)
                if not record_id:
                    errors.append(f"{split}.{collection}[{index}] is missing its ID")
                    continue
                if record_id in indexes[split][collection]:
                    errors.append(f"{split}.{collection} duplicates ID {record_id}")
                indexes[split][collection][record_id] = record
                global_indexes[collection][record_id].add(split)
                row_entries[collection].append((split, record))
                declared_split = _record_split(record)
                if declared_split and declared_split != split:
                    errors.append(f"{collection} {record_id} declares split {declared_split} but is stored in {split}")
                block_id = _record_block_id(record)
                if block_id:
                    block_rows[block_id].append((split, collection, record))
                if collection == "messages":
                    chat_id = _as_text(record.get("chat_id"))
                    dialogue_segment_id = _as_text(record.get("dialogue_segment_id"))
                    if not dialogue_segment_id:
                        pilot = record.get("pilot")
                        if isinstance(pilot, Mapping):
                            dialogue_segment_id = _as_text(pilot.get("dialogue_segment_id"))
                    block_metadata.setdefault(
                        block_id,
                        {
                            "chat_ids": set(),
                            "dialogue_segment_ids": set(),
                            "block_orders": set(),
                            "sequence_values": [],
                            "chat_types": set(),
                        },
                    )
                    if block_id:
                        meta = block_metadata[block_id]
                        meta["chat_ids"].add(chat_id)
                        meta["dialogue_segment_ids"].add(dialogue_segment_id)
                        meta["chat_types"].add(_as_text(record.get("chat_type")))
                        if record.get("block_order") is not None:
                            meta["block_orders"].add(record.get("block_order"))
                        elif isinstance(record.get("pilot"), Mapping) and record["pilot"].get("block_order") is not None:
                            meta["block_orders"].add(record["pilot"].get("block_order"))
                        if record.get("sequence_in_chat") is not None:
                            try:
                                meta["sequence_values"].append(int(record["sequence_in_chat"]))
                            except (TypeError, ValueError):
                                pass

    # Build a row-level split assignment.  A row that contains a direct block
    # assignment and a reference to a different split is rejected even if the
    # caller omitted its own split field.
    row_assignment: Dict[Tuple[str, str], str] = {}
    id_to_splits: Dict[str, Dict[str, Set[str]]] = {
        collection: {record_id: set(splits) for record_id, splits in global_indexes[collection].items()}
        for collection in COLLECTIONS
    }

    def reference_splits(collection: str, ids: Iterable[str]) -> Set[str]:
        found: Set[str] = set()
        for ref_id in ids:
            if not ref_id:
                continue
            # ``any`` is used only for adjudication rows, where an anchor can
            # be a message, claim, relation, cluster, or presentation ID.
            if collection == "any":
                for target in COLLECTIONS:
                    found.update(id_to_splits[target].get(ref_id, set()))
            else:
                found.update(id_to_splits.get(collection, {}).get(ref_id, set()))
        return found

    for collection in COLLECTIONS:
        for split, record in row_entries[collection]:
            record_id = _record_id(collection, record)
            candidates: Set[str] = {split}
            declared_split = _record_split(record)
            if declared_split:
                candidates.add(declared_split)
            block_id = _record_block_id(record)
            if block_id:
                candidates.update(item[0] for item in block_rows.get(block_id, ()))
            for target_collection, references in _references(collection, record).items():
                candidates.update(reference_splits(target_collection, references))
            if len(candidates) > 1:
                errors.append(
                    f"{collection} {record_id} has cross-split references: {','.join(sorted(candidates))}"
                )
            row_assignment[(collection, record_id)] = split

    # Every block is an indivisible unit.  This catches both a duplicate block
    # copied into the two output directories and a partial block accidentally
    # assigned by a downstream filter.
    block_splits: Dict[str, Set[str]] = defaultdict(set)
    for block_id, rows in block_rows.items():
        block_splits[block_id].update(split for split, _collection, _record in rows)
        if len(block_splits[block_id]) > 1:
            errors.append(f"block overlap across splits: {block_id}")
    block_overlap = sorted(block_id for block_id, splits in block_splits.items() if len(splits) > 1)

    # Isolation at the conversation level.  ``chat_id`` is the strongest
    # available stable unit for this corpus; segment and explicit isolation
    # groups remain hard boundaries in the less conservative modes.
    chat_splits: Dict[str, Set[str]] = defaultdict(set)
    segment_splits: Dict[str, Set[str]] = defaultdict(set)
    explicit_group_splits: Dict[str, Set[str]] = defaultdict(set)
    for block_id, rows in block_rows.items():
        splits = {split for split, _collection, _record in rows}
        meta = block_metadata.get(block_id, {})
        for chat_id in meta.get("chat_ids", set()):
            if chat_id:
                chat_splits[chat_id].update(splits)
        for segment_id in meta.get("dialogue_segment_ids", set()):
            if segment_id:
                segment_splits[segment_id].update(splits)
        for split, _collection, record in rows:
            for field in ("conversation_group_id", "isolation_group_id", "conversation_id"):
                value = record.get(field)
                if value:
                    explicit_group_splits[_as_text(value)].add(split)
    if isolation_unit == "same_chat":
        for chat_id, splits in sorted(chat_splits.items()):
            if len(splits) > 1:
                errors.append(f"same chat crosses splits: {chat_id}")
    if isolation_unit in {"same_chat", "adjacent_segment"}:
        for segment_id, splits in sorted(segment_splits.items()):
            if len(splits) > 1:
                errors.append(f"dialogue segment crosses splits: {segment_id}")
    for group_id, splits in sorted(explicit_group_splits.items()):
        if len(splits) > 1:
            errors.append(f"isolation group crosses splits: {group_id}")

    # A generic adjacent-block check is useful when a caller opts out of
    # whole-chat isolation.  Block order is only compared within one chat and
    # catches consecutive selected blocks; it never uses message text.
    if isolation_unit in {"adjacent_segment", "conversation_block"}:
        ordered_by_chat: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
        for block_id, meta in block_metadata.items():
            chats = sorted(meta.get("chat_ids", set()))
            orders = [int(value) for value in meta.get("block_orders", set()) if _as_text(value).lstrip("-").isdigit()]
            if not chats or not orders or block_id not in block_splits:
                continue
            ordered_by_chat[chats[0]].append((min(orders), block_id, sorted(block_splits[block_id])[0]))
        for chat_id, ordered in ordered_by_chat.items():
            ordered.sort()
            for left, right in zip(ordered, ordered[1:]):
                if right[0] - left[0] == 1 and left[2] != right[2]:
                    errors.append(f"adjacent blocks cross splits in chat {chat_id}: {left[1]},{right[1]}")

    # Cross-split graph references are independently checked.  This produces
    # actionable error categories instead of relying solely on block overlap.
    record_split_lookup = {
        collection: {
            record_id: row_assignment.get((collection, record_id), split)
            for split in SPLITS
            for record_id in indexes[split][collection]
        }
        for collection in COLLECTIONS
    }

    def target_split(target_collection: str, ref_id: str) -> str:
        return record_split_lookup.get(target_collection, {}).get(ref_id, "")

    def relation_target_splits(record: Mapping[str, Any]) -> Set[str]:
        found: Set[str] = set()
        anchor_type = _as_text(record.get("anchor_type") or "claim")
        target_collection = "mentions" if anchor_type == "mention" else "claims"
        for ref_id in _json_ids(record, "left_anchor_id") + _json_ids(record, "right_anchor_id"):
            target = target_split(target_collection, ref_id)
            if target:
                found.add(target)
        for ref_id in _json_ids(record, "evidence_message_ids"):
            target = target_split("messages", ref_id)
            if target:
                found.add(target)
        return found

    cross_split_claims: List[str] = []
    cross_split_events: List[str] = []
    for collection in COLLECTIONS:
        for split in SPLITS:
            for record_id, record in indexes[split][collection].items():
                owner_split = row_assignment.get((collection, record_id), split)
                if collection == "relations":
                    targets = relation_target_splits(record)
                    if targets and (targets != {owner_split} or len(targets) > 1):
                        errors.append(f"relation crosses splits: {record_id}")
                elif collection in {"claims", "mentions", "clusters", "presentations"}:
                    refs = _references(collection, record)
                    targets: Set[str] = set()
                    for target_collection, ids in refs.items():
                        if target_collection == "any":
                            continue
                        targets.update(
                            target_split(target_collection, ref_id)
                            for ref_id in ids
                            if target_split(target_collection, ref_id)
                        )
                    if targets and (targets != {owner_split} or len(targets) > 1):
                        errors.append(f"{collection} crosses splits: {record_id}")
                    if collection == "claims" and targets and targets != {owner_split}:
                        cross_split_claims.append(record_id)
                    if collection == "clusters":
                        event_keys = _event_keys(record)
                        if targets and targets != {owner_split}:
                            cross_split_events.extend(event_keys or (record_id,))

    # Any repeated IDs in a typed collection are an overlap even when the rows
    # happen to be byte-identical.  Event IDs additionally cover schemas that
    # use a separate event key instead of cluster_id.
    overlaps: Dict[str, List[str]] = {}
    for collection in ("messages", "claims", "clusters", "relations", "mentions", "presentations", "adjudications"):
        overlaps[collection] = sorted(record_id for record_id, splits in global_indexes[collection].items() if len(splits) > 1)
        if overlaps[collection]:
            errors.append(f"{collection} ID overlap across splits: {','.join(overlaps[collection])}")
    event_splits: Dict[str, Set[str]] = defaultdict(set)
    for split in SPLITS:
        for record in indexes[split]["clusters"].values():
            for event_key in _event_keys(record):
                event_splits[event_key].add(split)
    event_overlap = sorted(event_key for event_key, splits in event_splits.items() if len(splits) > 1)
    for event_key in event_overlap:
        errors.append(f"event overlap across splits: {event_key}")

    # Aggregate distribution summary.  It is deliberately metadata-only and
    # contains no redacted text, participant names, or source identifiers.
    summary: Dict[str, Any] = {"development": _empty_split_summary(), "frozen_test": _empty_split_summary()}
    for split in SPLITS:
        rows = datasets[split]
        split_summary = summary[split]
        split_messages = list(rows["messages"])
        split_summary["message_count"] = len(split_messages)
        split_summary["mention_count"] = len(rows["mentions"])
        split_summary["claim_count"] = len(rows["claims"])
        split_summary["relation_count"] = len(rows["relations"])
        split_summary["cluster_count"] = len(rows["clusters"])
        split_summary["presentation_count"] = len(rows["presentations"])
        split_summary["adjudication_count"] = len(rows["adjudications"])
        split_summary["block_count"] = len({
            _record_block_id(record) for collection in COLLECTIONS for record in rows[collection] if _record_block_id(record)
        })
        split_summary["chat_count"] = len({_as_text(record.get("chat_id")) for record in split_messages if record.get("chat_id")})
        split_summary["chat_type"] = _counter_dict(record.get("chat_type") for record in split_messages if record.get("chat_type"))
        split_summary["time_bucket"] = _counter_dict(record.get("time_bucket") for record in split_messages if record.get("time_bucket"))
        split_summary["relation_labels"] = _counter_dict(record.get("label") for record in rows["relations"] if record.get("label"))
        split_summary["must_not_link_count"] = sum(1 for record in rows["relations"] if record.get("must_not_link") is True)
        split_summary["context_only_message_count"] = sum(1 for record in split_messages if _context_only(record))
        split_summary["event_count"] = sum(1 for record in rows["clusters"] if _as_text(record.get("cluster_type")) == "event")
    summary["total_message_count"] = sum(summary[split]["message_count"] for split in SPLITS)
    summary["message_test_fraction"] = (
        summary["frozen_test"]["message_count"] / summary["total_message_count"]
        if summary["total_message_count"]
        else 0.0
    )
    summary["block_overlap"] = block_overlap
    summary["claim_overlap"] = overlaps.get("claims", [])
    summary["event_overlap"] = event_overlap
    summary["typed_overlaps"] = overlaps
    summary["cross_split_claims"] = sorted(set(cross_split_claims))
    summary["cross_split_events"] = sorted(set(cross_split_events))

    requirements = coverage_requirements
    if requirements is None and aggregate_manifest:
        declared_requirements = aggregate_manifest.get("coverage_requirements")
        if isinstance(declared_requirements, Mapping):
            requirements = declared_requirements
    if requirements:
        errors.extend(_coverage_errors(summary, requirements))

    if manifests or aggregate_manifest:
        errors.extend(_manifest_freeze_errors(manifests, aggregate_manifest))
        # Records may carry a source dataset version.  A mixed version is a
        # reproducibility failure even when the graph itself is disjoint.
        manifest_dataset_versions = {
            _as_text(item.get("dataset_version"))
            for item in manifests.values()
            if item.get("dataset_version")
        }
        for split in SPLITS:
            expected_version = next(iter(manifest_dataset_versions), "")
            if not expected_version:
                continue
            observed = {
                _as_text(record.get("dataset_version"))
                for collection in COLLECTIONS
                for record in datasets[split][collection]
                if record.get("dataset_version")
            }
            if observed - {expected_version}:
                errors.append(f"{split} records disagree with manifest dataset_version")

    if not summary["total_message_count"]:
        warnings.append("split contains no messages")
    return SplitValidationResult(
        ok=not errors,
        errors=tuple(sorted(set(errors))),
        warnings=tuple(sorted(set(warnings))),
        summary=summary,
    )


def _read_jsonl(path: Path) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} is not valid JSON") from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"{path}:{line_number} must contain an object")
            rows.append(value)
    return rows


def _manifest_hash_errors(
    root: Path,
    manifests: Mapping[str, Mapping[str, Any]],
    aggregate_manifest: Optional[Mapping[str, Any]],
) -> List[str]:
    errors: List[str] = []
    aggregate_files = aggregate_manifest.get("files") if aggregate_manifest else None
    if aggregate_files is not None and not isinstance(aggregate_files, Sequence):
        errors.append("aggregate manifest files must be an array")
        aggregate_files = []
    aggregate_by_path = {
        _as_text(entry.get("path")): entry
        for entry in (aggregate_files or ())
        if isinstance(entry, Mapping) and entry.get("path")
    }
    for split in SPLITS:
        split_manifest = manifests.get(split, {})
        split_root = root / split
        expected_hashes = split_manifest.get("file_sha256") or {}
        expected_counts = split_manifest.get("record_counts_by_file") or {}
        if not isinstance(expected_hashes, Mapping):
            errors.append(f"{split} file_sha256 must be an object")
            expected_hashes = {}
        for collection, filename in COLLECTION_FILES.items():
            path = split_root / filename
            if not path.exists():
                errors.append(f"missing split file {split}/{filename}")
                continue
            actual_hash = sha256_file(path)
            if expected_hashes.get(filename) and _as_text(expected_hashes[filename]) != actual_hash:
                errors.append(f"{split}/{filename} sha256 mismatch")
            if expected_counts.get(filename) is not None:
                actual_count = len(_read_jsonl(path))
                if int(expected_counts[filename]) != actual_count:
                    errors.append(f"{split}/{filename} record count mismatch")
            aggregate_entry = aggregate_by_path.get(f"{split}/{filename}")
            if aggregate_entry is not None and _as_text(aggregate_entry.get("sha256")) != actual_hash:
                errors.append(f"aggregate hash mismatch for {split}/{filename}")
        manifest_path = split_root / "manifest.json"
        aggregate_entry = aggregate_by_path.get(f"{split}/manifest.json")
        if aggregate_entry is not None and manifest_path.exists():
            actual_hash = sha256_file(manifest_path)
            if _as_text(aggregate_entry.get("sha256")) != actual_hash:
                errors.append(f"aggregate hash mismatch for {split}/manifest.json")
    if aggregate_manifest:
        aggregate_digest = _as_text(aggregate_manifest.get("aggregate_sha256"))
        if aggregate_digest:
            payload = dict(aggregate_manifest)
            payload.pop("aggregate_sha256", None)
            if aggregate_digest != sha256_bytes(canonical_json(payload)):
                errors.append("aggregate_sha256 does not match aggregate manifest")
    return errors


def validate_split_directories(
    root: Path,
    *,
    coverage_requirements: Optional[Mapping[str, Any]] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> SplitValidationResult:
    """Load and validate ``root/development`` and ``root/frozen_test``.

    The function verifies each JSONL file's declared SHA-256 and the aggregate
    manifest.  It is intentionally read-only; in particular it never writes
    a report or modifies a frozen-test file.
    """

    root = Path(root)
    split_collections: Dict[str, Dict[str, List[Mapping[str, Any]]]] = {}
    manifests: Dict[str, Mapping[str, Any]] = {}
    errors: List[str] = []
    for split in SPLITS:
        split_root = root / split
        manifest_path = split_root / "manifest.json"
        if not manifest_path.exists():
            errors.append(f"missing {split}/manifest.json")
            manifests[split] = {}
        else:
            try:
                manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest_value = {}
                errors.append(f"{split}/manifest.json is not valid JSON")
            manifests[split] = manifest_value if isinstance(manifest_value, Mapping) else {}
        split_collections[split] = {}
        for collection, filename in COLLECTION_FILES.items():
            path = split_root / filename
            if not path.exists():
                errors.append(f"missing split file {split}/{filename}")
                split_collections[split][collection] = []
                continue
            try:
                split_collections[split][collection] = _read_jsonl(path)
            except ValueError as exc:
                errors.append(str(exc))
                split_collections[split][collection] = []
    aggregate_path = root / "aggregate_manifest.json"
    aggregate_manifest: Optional[Mapping[str, Any]] = None
    if not aggregate_path.exists():
        errors.append("missing aggregate_manifest.json")
    else:
        try:
            value = json.loads(aggregate_path.read_text(encoding="utf-8"))
            aggregate_manifest = value if isinstance(value, Mapping) else None
        except json.JSONDecodeError:
            errors.append("aggregate_manifest.json is not valid JSON")
    result = validate_split_records(
        split_collections,
        manifests=manifests,
        aggregate_manifest=aggregate_manifest,
        policy=policy,
        coverage_requirements=coverage_requirements,
    )
    errors.extend(_manifest_hash_errors(root, manifests, aggregate_manifest))
    if errors:
        return SplitValidationResult(
            ok=False,
            errors=tuple(sorted(set(result.errors).union(errors))),
            warnings=result.warnings,
            summary=result.summary,
        )
    return result


# Discoverable aliases for callers that use the term "evaluation split".
validate_evaluation_split = validate_split_records
validate_split_artifact = validate_split_directories


__all__ = [
    "COLLECTIONS",
    "COLLECTION_FILES",
    "RELATION_LABELS",
    "SplitValidationResult",
    "canonical_json",
    "sha256_bytes",
    "sha256_file",
    "validate_evaluation_split",
    "validate_split_artifact",
    "validate_split_directories",
    "validate_split_records",
]
