"""Synthetic-only regression tests for the offline release graph gate.

These fixtures are assembled by ``test_semantic_gold`` and never open the
private gold-standard directory or a source database.  The tests intentionally
mutate structure-only IDs/labels so a regression cannot be hidden by message
正文 content.
"""

from copy import deepcopy

from tests.test_semantic_gold import _synthetic_contract_and_predictions
from wechat_bridge.annotation_tools import validate_annotation_graph


def _dataset():
    dataset, _ = _synthetic_contract_and_predictions()
    return deepcopy(dataset)


def _has_error(result, fragment):
    return any(fragment in error for error in result.errors)


def test_claims_must_have_exactly_one_cluster_membership():
    duplicate = _dataset()
    duplicate["clusters"][1]["claim_ids"].append(duplicate["clusters"][0]["claim_ids"][0])
    result = validate_annotation_graph(duplicate)
    assert not result.ok
    assert _has_error(result, "appears in multiple clusters")

    missing = _dataset()
    missing_id = missing["clusters"][0]["claim_ids"].pop()
    result = validate_annotation_graph(missing)
    assert not result.ok
    assert _has_error(result, "claim %s is not assigned to a cluster" % missing_id)


def test_same_event_edges_must_match_event_cluster_boundaries():
    dataset = _dataset()
    relation = next(item for item in dataset["relations"] if item["label"] == "same_event")
    right_claim = relation["right_anchor_id"]
    source_cluster = next(item for item in dataset["clusters"] if right_claim in item["claim_ids"])
    destination = next(item for item in dataset["clusters"] if item is not source_cluster)
    source_cluster["claim_ids"].remove(right_claim)
    destination["claim_ids"].append(right_claim)
    result = validate_annotation_graph(dataset)
    assert not result.ok
    assert _has_error(result, "same_event relation %s crosses cluster boundary" % relation["relation_id"])


def test_cluster_mentions_and_core_entities_need_member_support():
    missing_mention = _dataset()
    cluster = next(item for item in missing_mention["clusters"] if item["mention_ids"])
    cluster["mention_ids"].pop()
    result = validate_annotation_graph(missing_mention)
    assert not result.ok
    assert _has_error(result, "mention coverage mismatch")

    unsupported_entity = _dataset()
    cluster = unsupported_entity["clusters"][0]
    cluster["core_entity_ids"].append("entity:unsupported")
    result = validate_annotation_graph(unsupported_entity)
    assert not result.ok
    assert _has_error(result, "lacks member claim/mention support")


def test_context_only_evidence_cannot_enter_event_or_visible_presentation():
    event_context = _dataset()
    event_cluster = event_context["clusters"][0]
    message_id = event_cluster["member_message_ids"][0]
    message = next(item for item in event_context["messages"] if item["message_id"] == message_id)
    message["dialogue_role"] = "context_only"
    result = validate_annotation_graph(event_context)
    assert not result.ok
    assert _has_error(result, "event cluster contains context_only evidence")

    visible_context = _dataset()
    cluster = visible_context["clusters"][0]
    cluster["cluster_type"] = "non_event_context"
    presentation = next(
        item for item in visible_context["presentations"]
        if cluster["cluster_id"] in item["source_cluster_ids"]
    )
    result = validate_annotation_graph(visible_context)
    assert not result.ok
    assert _has_error(result, "visible presentation contains context_only/non_event_context evidence")


def test_presentations_validate_typed_foreign_keys():
    dataset = _dataset()
    presentation = dataset["presentations"][0]
    presentation["source_cluster_ids"] = [dataset["claims"][0]["claim_id"]]
    result = validate_annotation_graph(dataset)
    assert not result.ok
    assert _has_error(result, "source_cluster_ids references unknown cluster")


def test_final_relations_require_both_source_labels_or_missing_side_adjudication():
    missing_label = _dataset()
    relation = missing_label["relations"][0]
    relation.pop("annotator_b_label")
    result = validate_annotation_graph(missing_label)
    assert not result.ok
    assert _has_error(result, "missing annotator_b_label without an explicit missing-side marker")

    adjudicated_missing = _dataset()
    relation = adjudicated_missing["relations"][0]
    relation.pop("annotator_b_label")
    relation["missing_side"] = "b"
    relation["adjudication_id"] = "ADJ_SYNTHETIC_RELATION"
    result = validate_annotation_graph(adjudicated_missing)
    assert result.ok


def test_same_event_requires_typed_span_bound_observable_support():
    missing_refs = _dataset()
    relation = next(item for item in missing_refs["relations"] if item["label"] == "same_event")
    relation.pop("observable_support_refs")
    result = validate_annotation_graph(missing_refs)
    assert not result.ok
    assert _has_error(result, "same_event requires observable_support_refs")

    untyped_ref = _dataset()
    relation = next(item for item in untyped_ref["relations"] if item["label"] == "same_event")
    relation["observable_support_refs"][0] = "MENTION_000002"
    result = validate_annotation_graph(untyped_ref)
    assert not result.ok
    assert _has_error(result, "must have an explicit type")

    action_only = _dataset()
    relation = next(item for item in action_only["relations"] if item["label"] == "same_event")
    relation["observable_support_refs"] = [
        item for item in relation["observable_support_refs"]
        if item.get("support_code") != "same_message"
    ]
    # Remove the exact same-message fallback as well; a same_event row cannot
    # become observable merely because its two claims happen to share a
    # message record.
    right_claim = next(
        item for item in action_only["claims"]
        if item["claim_id"] == relation["right_anchor_id"]
    )
    right_claim["message_id"] = action_only["messages"][0]["message_id"]
    result = validate_annotation_graph(action_only)
    assert not result.ok
    assert _has_error(result, "lacks algorithm-readable observable instance signal")
