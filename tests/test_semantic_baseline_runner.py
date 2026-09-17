from copy import deepcopy
import json

from tests.test_semantic_gold import _synthetic_contract_and_predictions
from tests.semantic_baseline_runner import (
    is_event_evidence_eligible,
    pilot_record_to_legacy_message,
    run_p0_baseline,
)


def _pilot_record(message, *, role="substantive", eligible=True, greeting_only=False,
                  greeting_prefix=False, message_type="text", direction="inbound"):
    return {
        "message_id": message["message_id"],
        "chat_id": message["chat_id"],
        "chat_type": message["chat_type"],
        "direction": direction,
        "message_type": message_type,
        "redacted_text": message["redacted_text"],
        "speaker_id": message["speaker_id"],
        "local_day": "2026-08-25",
        "time_offset_seconds": message["time_offset_seconds"],
        "reply_to_message_id": None,
        "pilot": {
            "dialogue_role": role,
            "dialogue_event_role": role,
            "dialogue_evidence_eligible": eligible,
            "dialogue_greeting_only": greeting_only,
            "dialogue_greeting_prefix": greeting_prefix,
        },
    }


def test_pilot_adapter_preserves_pseudonymous_speaker_for_outbound_records():
    dataset, _ = _synthetic_contract_and_predictions()
    message = dataset["messages"][0]
    adapted = pilot_record_to_legacy_message(
        _pilot_record(message, direction="outbound")
    )

    assert adapted["sender_id"] == message["speaker_id"]
    assert adapted["sender_name"] == "匿名成员"
    assert adapted["is_self"] is False
    assert adapted["content"] == message["redacted_text"]
    assert "raw_text" not in adapted


def test_event_evidence_filter_is_deny_by_default_for_context_and_greeting_only():
    dataset, _ = _synthetic_contract_and_predictions()
    message = dataset["messages"][0]
    context = _pilot_record(message, role="context_only", eligible=True)
    greeting_only = _pilot_record(message, greeting_only=True, eligible=True)
    greeting_prefix = _pilot_record(message, greeting_prefix=True, eligible=True)
    media = _pilot_record(message, message_type="image", eligible=True)

    assert not is_event_evidence_eligible(context)
    assert not is_event_evidence_eligible(greeting_only)
    assert is_event_evidence_eligible(greeting_prefix)
    assert not is_event_evidence_eligible(media)


def test_runner_uses_only_eligible_records_for_event_evidence_and_is_synthetic():
    dataset, _ = _synthetic_contract_and_predictions()
    pilot = [
        _pilot_record(message)
        for message in dataset["messages"]
    ]
    # These records intentionally contain event-looking text, but the runner
    # must not let them reach claim/event/presentation construction.
    context = _pilot_record(dataset["messages"][0], role="context_only", eligible=True)
    context["message_id"] = "MESSAGE_CONTEXT"
    context["chat_id"] = "CHAT_CONTEXT"
    context["speaker_id"] = "PERSON_CONTEXT"
    greeting = _pilot_record(dataset["messages"][1], greeting_only=True, eligible=True)
    greeting["message_id"] = "MESSAGE_GREETING"
    greeting["chat_id"] = "CHAT_GREETING"
    greeting["speaker_id"] = "PERSON_GREETING"
    pilot.extend((context, greeting))

    run = run_p0_baseline(pilot, dataset)
    event_ids = {item.message_id for item in run.event_result.claims}

    assert "MESSAGE_CONTEXT" not in event_ids
    assert "MESSAGE_GREETING" not in event_ids
    assert run.aggregate_error_report["provisional"] is True
    assert run.aggregate_error_report["zero_tolerance"]["passed"] is True
    assert run.aggregate_error_report["evidence_audit"]["context_only_event_evidence_count"] == 0
    assert run.aggregate_error_report["evidence_audit"]["greeting_only_event_evidence_count"] == 0
    serialized = json.dumps(run.aggregate_error_report, ensure_ascii=False)
    for key in ("redacted_text", "claim_text", "surface_text", "speaker_name"):
        assert key not in serialized
