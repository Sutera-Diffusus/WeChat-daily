"""Contract tests for the metadata-first blind reconstruction runner."""

from __future__ import annotations

import json
from pathlib import Path

from wechat_bridge.context_reconstruction import assert_body_free
from wechat_bridge.context_reconstruction_blind_runner import (
    InMemoryBlindSource,
    MetadataMessage,
    build_metadata_windows,
    build_selection_manifest,
    run_blind_round1,
    select_representative_windows,
    verify_selection_manifest,
)


def _fixture_source() -> InMemoryBlindSource:
    metadata = []
    bodies = {}
    sequence = 0
    for day_index, day in enumerate(("2026-08-20", "2026-08-21", "2026-08-22")):
        for scope in ("direct", "group"):
            chat_ref = f"chat-{day_index}-{scope}"
            for offset in range(4):
                sequence += 1
                ref = f"message-{sequence:03d}"
                timestamp = f"{day}T0{day_index + 1}:{offset:02d}:00+08:00"
                metadata.append(MetadataMessage(
                    message_ref=ref,
                    chat_ref=chat_ref,
                    chat_type=scope,
                    local_day=day,
                    timestamp=timestamp,
                    timestamp_epoch=float(sequence * 60),
                    sequence=sequence,
                    participant_ref=f"participant-{scope}-{offset % 2}",
                    message_type="image" if offset == 3 else "text",
                    media=offset == 3,
                ))
                body = f"fixture {day} {scope} turn {offset}"
                bodies[ref] = {
                    "message_id": ref,
                    "account_id": "account-fixture",
                    "chat_id": chat_ref,
                    "chat_type": scope,
                    "speaker_id": f"participant-{scope}-{offset % 2}",
                    "speaker_name": f"participant-{scope}-{offset % 2}",
                    "timestamp": timestamp,
                    "sequence_in_chat": sequence,
                    "message_type": "image" if offset == 3 else "text",
                    "content": body if offset != 3 else "[图片]",
                    "media_state": "unavailable" if offset == 3 else None,
                    "media_available": False if offset == 3 else None,
                    "media_path": None,
                    "split": "development",
                }
    return InMemoryBlindSource(metadata, bodies)


def test_metadata_windows_and_selection_cover_three_dates_and_two_scopes() -> None:
    source = _fixture_source()
    windows = build_metadata_windows(source.scan_metadata())
    selected = select_representative_windows(windows)

    assert len(selected) == 6
    assert {window.local_day for window in selected} == {"2026-08-20", "2026-08-21", "2026-08-22"}
    assert {window.chat_type for window in selected} == {"direct", "group"}
    refs = [row.message_ref for window in selected for row in window.rows]
    assert len(refs) == len(set(refs))
    assert all(window.body_free().get("message_refs") for window in selected)


def test_selection_manifest_is_hashed_and_body_free() -> None:
    source = _fixture_source()
    selected = select_representative_windows(build_metadata_windows(source.scan_metadata()))
    manifest = build_selection_manifest(
        selected,
        source_ref=source.source_ref,
        preferred_dates=("2026-08-20", "2026-08-21", "2026-08-22"),
        excluded_dates=("2026-08-25",),
    )

    assert manifest["blind_locked_before_body_read"] is True
    assert manifest["body_fields_read_during_selection"] is False
    assert "2026-08-25" not in manifest["selected_dates"]
    assert verify_selection_manifest(manifest) is True
    assert_body_free(manifest)


def test_runner_writes_lock_before_materializing_bodies_and_emits_structural_audit(tmp_path: Path) -> None:
    source = _fixture_source()
    paths = run_blind_round1(source, output_dir=tmp_path)

    assert source.body_read is True
    selection_path = paths["selection_manifest"]
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    reconstruction_manifest = json.loads(paths["reconstruction_manifest"].read_text(encoding="utf-8"))
    audit = json.loads(paths["blind_audit"].read_text(encoding="utf-8"))
    reconstruction = json.loads(paths["reconstruction"].read_text(encoding="utf-8"))

    assert verify_selection_manifest(selection) is True
    assert reconstruction_manifest["blind_locked_before_body_read"] is True
    assert reconstruction_manifest["provider_calls"] == 0
    assert reconstruction_manifest["production_blocked"] is True
    assert reconstruction_manifest["selection_manifest_sha256"] == selection["selection_manifest_sha256"]
    assert audit["semantic_accuracy_measured"] is False
    assert audit["passed"] is True
    assert audit["gates"]["review_source_sets_mutually_exclusive"] is True
    assert audit["gates"]["direct_and_group_covered"] is True
    assert_body_free(reconstruction_manifest)
    assert_body_free(reconstruction)
    assert paths["review_html"].exists()


def test_runner_never_selects_excluded_feedback_day(tmp_path: Path) -> None:
    source = _fixture_source()
    # Add an excluded-day row.  It is metadata only and has a body, but the
    # runner must never materialize it.
    excluded = MetadataMessage(
        message_ref="message-excluded",
        chat_ref="chat-excluded",
        chat_type="direct",
        local_day="2026-08-25",
        timestamp="2026-08-25T10:00:00+08:00",
        timestamp_epoch=10.0,
        sequence=999,
        participant_ref="participant-excluded",
        message_type="text",
        media=False,
    )
    source._metadata = tuple(source._metadata) + (excluded,)
    source._bodies[excluded.message_ref] = {
        "message_id": excluded.message_ref,
        "account_id": "account-fixture",
        "chat_id": excluded.chat_ref,
        "chat_type": excluded.chat_type,
        "speaker_id": excluded.participant_ref,
        "timestamp": excluded.timestamp,
        "content": "must not be materialized",
        "message_type": "text",
        "split": "development",
    }

    paths = run_blind_round1(source, output_dir=tmp_path)
    selection = json.loads(paths["selection_manifest"].read_text(encoding="utf-8"))
    assert "2026-08-25" not in selection["selected_dates"]
    assert excluded.message_ref not in selection["selected_message_refs"]
