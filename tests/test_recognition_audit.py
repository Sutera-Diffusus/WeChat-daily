from recognition_audit import run_audit


def test_mixed_recognition_audit_has_no_hard_noise_leakage():
    audit = run_audit()

    assert audit["noise"]["metrics"]["precision"] == 1.0
    assert audit["noise"]["metrics"]["recall"] == 1.0
    assert audit["noise"]["misclassified"] == []
    assert audit["value_surface"]["surface_noise_ids"] == []


def test_mixed_recognition_audit_separates_focus_from_full_census():
    audit = run_audit()

    assert audit["actions"]["metrics"]["precision"] == 1.0
    assert audit["actions"]["metrics"]["recall"] == 1.0
    assert audit["review_surface"]["coverage"] == 1.0
    assert audit["value_surface"]["focus_metrics"]["precision"] == 1.0
    assert audit["value_surface"]["metrics"]["recall"] == 1.0


def test_mixed_recognition_audit_finds_phrase_family_but_not_operational_repetition():
    audit = run_audit()

    assert audit["memes"]["actual_metrics"]["precision"] == 1.0
    assert audit["memes"]["actual_metrics"]["recall"] == 1.0
    assert len(audit["memes"]["candidates"]) == 1
    candidate = audit["memes"]["candidates"][0]
    assert candidate["phrase"] == "未来古法"
    assert candidate["kind"] == "inside_joke"
    assert set(candidate["message_ids"]) == {"meme-1", "meme-2"}
    assert set(candidate["reaction_message_ids"]) == {"meme-r1", "meme-r2"}


def test_mixed_recognition_audit_blocks_parallel_topic_leakage():
    audit = run_audit()

    generic = audit["context"]["parallel_request"]
    followup = audit["context"]["followup_discovery"]
    assert generic["candidate_state"] == "context_needed"
    assert generic["context_message_ids"] == ["parallel-followup"]
    assert "model-1" in followup["context_message_ids"]
    assert "course-1" not in followup["context_message_ids"]
    assert audit["context"]["dynamic_subject_leaks"] == []


def test_mixed_recognition_audit_deduplicates_cross_chat_risk_event():
    audit = run_audit()

    risk_events = [
        event for event in audit["events"]
        if "账号风控" in str(event.get("title") or "")
    ]
    assert len(risk_events) == 1
    assert set(risk_events[0]["message_ids"]) >= {
        "risk-send", "risk-readonly", "cross-private-1", "cross-risk-1"
    }


def test_mixed_recognition_audit_topic_briefs_do_not_publish_generic_buckets():
    audit = run_audit()

    topic_summary = audit["topics"]["summary"]
    topic_names = {item["topic"] for item in audit["topics"]["briefs"]}
    assert topic_summary["generic_topic_count"] == 0
    assert topic_summary["topic_specificity_rate"] == 1.0
    assert topic_summary["unclassified_topic_messages"] > 0
    assert "其他讨论" not in topic_names
    assert "社群 / 活动" not in topic_names
    assert all(item["subtopics"] for item in audit["topics"]["briefs"])


def test_mixed_recognition_audit_funnel_retains_all_rows_before_reduction():
    audit = run_audit()

    invariant = audit["funnel"]["invariant"]
    assert audit["funnel"]["principle"] == "many_to_few_reversible"
    assert invariant["input_count"] == audit["input"]["message_count"]
    assert invariant["ledger_count"] == audit["input"]["message_count"]
    assert invariant["all_rows_retained"] is True
    assert invariant["missing_row_ids"] == []
    assert invariant["extra_row_ids"] == []
    assert invariant["duplicate_row_ids"] == []
    assert {item["layer"] for item in audit["funnel"]["exclusive_layers"]} >= {
        "first_screen",
        "suppressed_noise",
    }
