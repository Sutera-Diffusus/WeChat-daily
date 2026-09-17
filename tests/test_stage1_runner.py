import json

import pytest

from wechat_bridge.stage1_runner import (
    AGGREGATE_FILENAME,
    MANIFEST_FILENAME,
    PREDICTIONS_FILENAME,
    REVIEW_QUEUE_FILENAME,
    run_stage1_development_pilot,
)


def _write_messages(directory):
    directory.mkdir()
    rows = [
        {
            "message_id": "MESSAGE_SYNTH_001",
            "account_id": "ACCOUNT_SYNTH",
            "chat_id": "CHAT_SYNTH",
            "speaker_id": "PERSON_SYNTH_A",
            "message_type": "text",
            "time_offset_seconds": 0,
            "sequence_in_chat": 0,
            "split": "development",
            "redacted_text": "synthetic service failed",
        },
        {
            "message_id": "MESSAGE_SYNTH_002",
            "account_id": "ACCOUNT_SYNTH",
            "chat_id": "CHAT_SYNTH",
            "speaker_id": "PERSON_SYNTH_B",
            "message_type": "text",
            "time_offset_seconds": 1,
            "sequence_in_chat": 1,
            "split": "development",
            "redacted_text": "synthetic service ongoing",
        },
    ]
    directory.joinpath("messages.private.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def test_runner_writes_private_artifacts_and_body_free_aggregate(tmp_path):
    input_directory = tmp_path / "development"
    _write_messages(input_directory)
    output_directory = tmp_path / "stage1_context_development_v1"
    result = run_stage1_development_pilot(input_directory, output_directory)

    assert result.message_count == 2
    assert result.fragment_count >= 2
    assert output_directory.joinpath(PREDICTIONS_FILENAME).is_file()
    assert output_directory.joinpath(AGGREGATE_FILENAME).is_file()
    assert output_directory.joinpath(REVIEW_QUEUE_FILENAME).is_file()
    assert output_directory.joinpath(MANIFEST_FILENAME).is_file()

    aggregate = json.loads(output_directory.joinpath(AGGREGATE_FILENAME).read_text(encoding="utf-8"))
    manifest = json.loads(output_directory.joinpath(MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert aggregate["scope"] == "development"
    assert aggregate["split"] == "development"
    assert aggregate["prediction_is_not_gold"] is True
    assert aggregate["gold_comparison"] == "N/A"
    assert aggregate["output"]["body_free"] is True
    assert aggregate["input"]["split_counts"] == {"development": 2}
    assert aggregate["scoring"]["fields"]["state"] == "N/A"
    assert aggregate["context_relations"]["time_only_strong_link_rate"] in {0.0, "N/A"}
    assert manifest["dataset_split"] == "development"
    assert manifest["split"] == "development"
    assert manifest["frozen_read"] is False
    assert manifest["gold_loaded"] is False
    assert len(manifest["input_sha256"]) == 64
    assert len(manifest["code_sha256"]) == 64

    aggregate_text = output_directory.joinpath(AGGREGATE_FILENAME).read_text(encoding="utf-8")
    assert "synthetic service failed" not in aggregate_text
    assert "redacted_text" not in aggregate_text
    prediction_rows = [
        json.loads(line)
        for line in output_directory.joinpath(PREDICTIONS_FILENAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert prediction_rows
    assert {row["record_type"] for row in prediction_rows} >= {"fragment", "claim"}


def test_runner_refuses_frozen_and_non_development_inputs(tmp_path):
    frozen = tmp_path / "frozen_test" / "development"
    frozen.mkdir(parents=True)
    with pytest.raises(ValueError, match="frozen"):
        run_stage1_development_pilot(frozen, tmp_path / "stage1_context_development_v1")

    non_dev = tmp_path / "working"
    _write_messages(non_dev)
    with pytest.raises(ValueError, match="development"):
        run_stage1_development_pilot(non_dev, tmp_path / "stage1_context_development_v1")


def test_runner_requires_development_rows_and_does_not_overwrite(tmp_path):
    input_directory = tmp_path / "development"
    _write_messages(input_directory)
    rows = input_directory.joinpath("messages.private.jsonl").read_text(encoding="utf-8").replace(
        '"split": "development"', '"split": "frozen_test"', 1
    )
    input_directory.joinpath("messages.private.jsonl").write_text(rows, encoding="utf-8")
    with pytest.raises(ValueError, match="non-development"):
        run_stage1_development_pilot(input_directory, tmp_path / "stage1_context_development_v1")

    second_input = tmp_path / "other" / "development"
    second_input.parent.mkdir()
    _write_messages(second_input)
    output_directory = tmp_path / "stage1_context_development_v1"
    run_stage1_development_pilot(second_input, output_directory)
    with pytest.raises(FileExistsError):
        run_stage1_development_pilot(second_input, output_directory)


def test_runner_rejects_messages_outside_the_pilot_day(tmp_path):
    input_directory = tmp_path / "development"
    _write_messages(input_directory)
    rows = input_directory.joinpath("messages.private.jsonl").read_text(encoding="utf-8").replace(
        '"split": "development"', '"split": "development", "local_day": "2026-08-24"', 1
    )
    input_directory.joinpath("messages.private.jsonl").write_text(rows, encoding="utf-8")
    with pytest.raises(ValueError, match="2026-08-25"):
        run_stage1_development_pilot(input_directory, tmp_path / "stage1_context_development_v1")
