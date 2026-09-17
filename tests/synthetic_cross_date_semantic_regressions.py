"""Synthetic, human-labeled regression fixtures for cross-date review.

The fixture messages are category cues only.  They intentionally contain no
exported WeChat body, person name, product name, timestamp, or private ID, and
must never be treated as an accuracy sample or shipped as a gold set.
"""

from __future__ import annotations

from typing import Any


CONTAMINATION_MARKER = "contaminated_human_labeled_regression_only"
SAMPLE_COUNT = 15


def _case(case_id: str, category: str, *, cue: str, expected: dict[str, Any], note: str) -> dict[str, Any]:
    """Build one abstract review case with deterministic opaque message IDs."""
    return {
        "package_id": f"synthetic-{case_id}",
        "sample_status": CONTAMINATION_MARKER,
        "messages": [
            {
                "id": f"{case_id}-m1",
                "text": f"SYNTHETIC_{cue}_CORE",
                "role": "primary",
                "message_type": "text",
            },
            {
                "id": f"{case_id}-m2",
                "text": f"SYNTHETIC_{cue}_CONTEXT",
                "role": "context",
                "message_type": "text",
            },
        ],
        "regression_category": category,
        "expected": expected,
        "human_note": note,
    }


def build_synthetic_cross_date_semantic_regressions() -> list[dict[str, Any]]:
    """Return exactly 15 abstract cases covering the review findings."""
    return [
        _case("01", "greeting_filler_no_topic", cue="GREETING_FILLER", expected={"no_topic": True}, note="greeting/filler only"),
        _case("02", "no_topic_evidence_trigger", cue="NO_TOPIC_TRIGGER", expected={"no_topic": True, "evidence_aliases": ["m1"]}, note="no-topic decision cites its trigger"),
        _case("03", "corrected_evidence_selection", cue="EVIDENCE_SELECTION", expected={"evidence_alias": "m2"}, note="evidence must point to the claim-specific turn"),
        _case("04", "named_product_not_location", cue="NAMED_PRODUCT", expected={"exact_noun_phrase": "SYNTHETIC_COMPOUND_OBJECT"}, note="keep a compound noun phrase intact"),
        _case("05", "discussion_about_person", cue="PERSON_DISCUSSION", expected={"topic_kind": "person_discussion"}, note="topic is discussion about a person"),
        _case("06", "discussion_about_work", cue="WORK_DISCUSSION", expected={"topic_kind": "work_discussion"}, note="topic is work discussion"),
        _case("07", "interlocutor_teasing", cue="INTERLOCUTOR_TEASING", expected={"speech_mode": "teasing"}, note="teasing belongs to the interlocutor"),
        _case("08", "group_insult", cue="GROUP_INSULT", expected={"speech_mode": "insulting"}, note="group message is insulting/嘴臭"),
        _case("09", "meme_abstract", cue="MEME_ABSTRACT", expected={"speech_mode": "meme"}, note="abstract/meme play"),
        _case("10", "person_object_relation", cue="PERSON_OBJECT_RELATION", expected={"relation": "person_object"}, note="person/object relation must stay bound"),
        _case("11", "state_relation", cue="STATE_RELATION", expected={"relation": "state"}, note="semantic relation is evaluated separately"),
        _case("12", "tone_joke", cue="TONE_JOKE", expected={"speech_mode": "joking"}, note="joke tone is not a factual topic"),
        _case("13", "uncertain_interesting", cue="UNCERTAIN_INTERESTING", expected={"review": "uncertain_interesting"}, note="interesting but uncertain"),
        _case("14", "low_information", cue="LOW_INFORMATION", expected={"information_value": "low"}, note="low information is not necessarily no topic"),
        _case("15", "substantive_correct", cue="SUBSTANTIVE_CORRECT", expected={"information_value": "substantive"}, note="substantive control case"),
    ]


def synthetic_cross_date_regression_manifest() -> dict[str, Any]:
    """Return the release-blocking manifest for these 15 labeled cases."""
    return {
        "sample_count": SAMPLE_COUNT,
        "sample_status": CONTAMINATION_MARKER,
        "accuracy_release_allowed": False,
        "accuracy_release_block_reason": "human_labeled_regression_only_not_gold_or_holdout",
        "human_labeled": True,
        "source": "synthetic_cross_date_semantic_regressions",
    }


SYNTHETIC_CROSS_DATE_SEMANTIC_REGRESSIONS = build_synthetic_cross_date_semantic_regressions()
SYNTHETIC_CROSS_DATE_REGRESSION_MANIFEST = synthetic_cross_date_regression_manifest()


__all__ = [
    "CONTAMINATION_MARKER",
    "SAMPLE_COUNT",
    "SYNTHETIC_CROSS_DATE_SEMANTIC_REGRESSIONS",
    "SYNTHETIC_CROSS_DATE_REGRESSION_MANIFEST",
    "build_synthetic_cross_date_semantic_regressions",
    "synthetic_cross_date_regression_manifest",
]
