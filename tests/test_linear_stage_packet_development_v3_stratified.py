"""Synthetic K30 contracts for the stratified linear development path.

The fixtures in this module are deliberately invented and in-memory.  They
do not open a checked-in development/private artifact, frozen data, a message
store, or a provider.  The runner-facing tests are kept behind a small public
API adapter because the K30 runner is being introduced independently of the
K28 metadata side-car.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from wechat_bridge.compact_context_packets import (
    compact_context_packets,
    materialize_stage_packet,
)
from wechat_bridge.context_packets import build_context_packets
from wechat_bridge.dialogue_bundle import build_dialogue_bundles
from wechat_bridge.dialogue_segments import is_context_only_text
from wechat_bridge.linear_stage_packets import (
    build_linear_stage_packets,
    materialize_stage_a,
    recover_linear_packet,
)
from wechat_bridge.selection_strata import (
    CANONICAL_STRATA,
    STRATA_OUTPUT_FILENAMES,
    build_canonical_strata_metadata,
    select_pages_by_strata,
    verify_strata_replay,
    write_canonical_strata_sidecar,
)


ACCOUNT = "k30-account"
CHAT = "k30-chat"

_BODY_KEYS = {
    "analysis",
    "body",
    "chain_of_thought",
    "completion",
    "content",
    "content_text",
    "evidence_text",
    "html",
    "markdown",
    "message",
    "message_text",
    "output_text",
    "prompt",
    "quote",
    "raw",
    "raw_text",
    "reasoning",
    "response",
    "response_text",
    "summary",
    "text",
    "text_body",
    "text_redacted",
    "transcript",
    "user_input",
    "user_packet",
    "user_canonical_json",
}


def _message(message_id: str, text: str, sequence: int, *, message_type: str = "text") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "speaker_id": "k30-speaker",
        "content": text,
        "message_type": message_type,
        "sequence_in_chat": sequence,
        "time_offset_seconds": float(sequence),
        "split": "synthetic",
    }


def _assert_body_free(value: Any) -> None:
    """Assert the body-free boundary without inspecting any private file."""

    def visit(item: Any, path: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key.casefold() in _BODY_KEYS and child not in (None, "", (), [], {}):
                    raise AssertionError("body-bearing key escaped: %s%s" % (path, key))
                visit(child, path + key + ".")
        elif isinstance(item, (list, tuple, set, frozenset)):
            for index, child in enumerate(item):
                visit(child, path + str(index) + ".")

    visit(value)


def _stratum_page(
    page_id: str,
    stratum: str,
    *,
    account: str = ACCOUNT,
    chat: str = CHAT,
    body_suffix: str = "",
) -> dict[str, Any]:
    """Build one bodyful K2-shaped packet with one strong stratum marker."""

    context_id = "m-%s-context" % page_id
    semantic_id = "m-%s-semantic" % page_id
    evidence_id = "e-%s" % page_id
    message_rows = [
        {
            "message_id": context_id,
            "account_id": account,
            "chat_id": chat,
            "role": "context_only",
            "fragment_type": "acknowledgement",
            "message_type": "text",
            "content": "ack%s" % body_suffix,
            "sequence_in_chat": 1,
        },
        {
            "message_id": semantic_id,
            "account_id": account,
            "chat_id": chat,
            "role": "substantive",
            "fragment_type": "statement",
            "message_type": "text",
            "content": "semantic%s" % body_suffix,
            "sequence_in_chat": 2,
        },
    ]
    packet: dict[str, Any] = {
        "packet_id": "packet-%s" % page_id,
        "account_id": account,
        "chat_id": chat,
        "source_message_ids": [context_id, semantic_id],
        # K2 retention is intentionally complete; the first row is not
        # removed here merely because the later provider view is semantic.
        "primary_fragments": message_rows,
        "adjacent_context": [],
        "message_handles": [context_id, semantic_id],
        "candidate_handles": ["candidate-%s" % page_id],
        "evidence_handles": [evidence_id],
        "evidence_refs": [
            {
                "evidence_id": evidence_id,
                "message_id": semantic_id,
                "account_id": account,
                "chat_id": chat,
                "span": {"start": 0, "end": 1},
                "evidence_text": "body%s" % body_suffix,
            }
        ],
        "source_refs": [
            {"type": "message", "id": context_id, "account_id": account, "chat_id": chat},
            {"type": "message", "id": semantic_id, "account_id": account, "chat_id": chat},
        ],
    }

    # The linear root intentionally retains known candidate rows while it
    # projects message roles.  Carry the synthetic canonical marker on that
    # public metadata row as well, so the runner can classify the same K2
    # packet after the K2 -> linear boundary without looking at message body.
    candidate_ids = list(packet["candidate_handles"])
    packet["candidate_qa_links"] = [
        {
            "candidate_id": candidate_ids[0],
            "candidate_ids": candidate_ids,
            "left_message_id": context_id,
            "right_message_id": semantic_id,
            "account_id": account,
            "chat_id": chat,
            "categories": [stratum],
            "evidence_refs": [{"evidence_id": evidence_id}],
        }
    ]

    if stratum == "pronoun_person_object_state":
        for kind in ("person", "object", "state"):
            packet["candidate_%s_history" % kind] = [
                {
                    "candidate_id": "%s-%s" % (kind, page_id),
                    "left_message_id": context_id,
                    "right_message_id": semantic_id,
                    "account_id": account,
                    "chat_id": chat,
                    "evidence_refs": [{"evidence_id": evidence_id}],
                }
            ]
    elif stratum == "greeting_new_topic":
        packet["messages"] = [
            {
                "message_id": semantic_id,
                "account_id": account,
                "chat_id": chat,
                "fragment_type": "conversation_opener",
                "is_opener": True,
            }
        ]
    elif stratum == "topic_shift":
        packet["topic_transitions"] = [
            {
                "topic_shift": True,
                "message_ids": [context_id, semantic_id],
                "evidence_id": evidence_id,
            }
        ]
    elif stratum == "candidate_competition":
        packet["candidate_competition"] = True
        packet["candidate_handles"] = ["candidate-a-%s" % page_id, "candidate-b-%s" % page_id]
    elif stratum == "no_reply":
        packet["reply_status"] = "awaiting_reply"
    else:  # pragma: no cover - protects the fixture from silent typos
        raise AssertionError("unknown synthetic stratum: %s" % stratum)
    return packet


def _weak_page(page_id: str = "weak") -> dict[str, Any]:
    row = _stratum_page(page_id, "greeting_new_topic")
    # Remove all strong markers.  These signals must never manufacture a
    # canonical stratum by themselves.
    row.pop("messages", None)
    row.pop("candidate_qa_links", None)
    row["candidate_reasons"] = ["time_proximity_weak", "same_segment_weak"]
    row["candidate_views"] = [
        "candidate_person_history",
        "candidate_object_history",
        "candidate_state_history",
    ]
    row["reply_count"] = 0
    row["answers"] = []
    return row


def _runner_safe_packet(page_id: str = "runner") -> dict[str, Any]:
    """A compact all-substantive packet for the artifact-only runner smoke.

    K2 context retention and provider role projection are covered above.  The
    runner smoke intentionally keeps both retained rows substantive so K10's
    linear recovery gate can be tested independently of the context-only
    role relocation already covered by the K28 contract.
    """

    packet = _stratum_page(page_id, "pronoun_person_object_state")
    for row in packet["primary_fragments"]:
        row["role"] = "substantive"
        row["fragment_type"] = "statement"
    return packet


def _provider_projection(result: Any) -> tuple[set[str], set[str], set[str]]:
    """Project K2 packets through the existing public compact adapter."""

    compact = compact_context_packets(
        tuple(getattr(result, "packets")),
        max_input_token_proxy=20_000,
        max_messages=100,
        max_candidate_rows=100,
        max_evidence_refs=100,
    )
    primary: set[str] = set()
    context: set[str] = set()
    all_ids: set[str] = set()
    for packet in compact.packets:
        materialized = materialize_stage_packet(compact, packet, allow_over_capacity=True)
        primary.update(str(value) for value in materialized.get("primary_message_ids", ()) if value)
        context.update(str(value) for value in materialized.get("context_message_ids", ()) if value)
        all_ids.update(str(value) for value in materialized.get("message_ids", ()) if value)
    return primary, context, all_ids


def test_k30_k2_retention_is_lossless_but_provider_primary_is_semantic() -> None:
    messages = [
        _message("confirm", "确认", 1),
        _message("topic", "确认项目上线状态", 2),
        _message("greeting", "你好", 3),
        _message("shift", "换个话题，明天选课怎么办？", 4),
        _message("media", "", 5, message_type="image"),
    ]
    assert is_context_only_text(messages[0]["content"]) is True

    bundles = build_dialogue_bundles(messages)
    result = build_context_packets(dialogue_result=bundles)
    retained = {
        str(fragment["message_id"])
        for packet in result.packets
        for fragment in packet.primary_fragments
    }
    assert retained == {row["message_id"] for row in messages}

    provider_primary, provider_context, provider_all = _provider_projection(result)
    assert {"topic", "shift"} <= provider_primary
    assert {"confirm", "greeting", "media"} <= provider_context
    assert provider_all == retained


def test_k30_only_the_five_canonical_strata_are_classified_from_strong_evidence() -> None:
    pages = [_stratum_page("strong-%d" % index, name) for index, name in enumerate(CANONICAL_STRATA)]
    report = build_canonical_strata_metadata(pages)

    assert tuple(report["canonical_strata"]) == CANONICAL_STRATA
    assert set(report["available_strata"]) == set(CANONICAL_STRATA)
    assert report["missing_strata"] == []
    assert all(set(row["observed_strata"]) <= set(CANONICAL_STRATA) for row in report["pages"])
    evidence_types = {
        evidence_type
        for row in report["pages"]
        for value in row["strata"].values()
        if value["status"] == "observed"
        for evidence_type in value["evidence_types"]
    }
    assert evidence_types <= {
        "candidate_history_triad",
        "opener_fragment",
        "topic_transition",
        "explicit_canonical_stratum",
        "authoritative_reply_status",
    }

    weak = build_canonical_strata_metadata([_weak_page()])
    assert weak["available_strata"] == []
    assert set(weak["pages"][0]["metadata_missing_strata"]) == set(CANONICAL_STRATA)
    assert weak["pages"][0]["ambiguous_strata"] == []


def test_k30_metadata_missing_and_ambiguous_are_not_collapsed() -> None:
    missing = {
        "packet_id": "missing",
        "account_id": ACCOUNT,
        "chat_id": CHAT,
        "primary_fragments": [],
    }
    ambiguous = _stratum_page("ambiguous", "greeting_new_topic")
    ambiguous["ambiguous_strata"] = ["greeting_new_topic"]

    report = build_canonical_strata_metadata([missing, ambiguous])
    missing_row = next(row for row in report["pages"] if row["status"] == "metadata_missing")
    ambiguous_row = next(row for row in report["pages"] if row["status"] == "ambiguous")
    assert set(missing_row["metadata_missing_strata"]) == set(CANONICAL_STRATA)
    assert ambiguous_row["ambiguous_strata"] == ["greeting_new_topic"]
    assert ambiguous_row["strata"]["greeting_new_topic"]["status"] == "ambiguous"
    assert "greeting_new_topic" not in ambiguous_row["observed_strata"]
    _assert_body_free(report)


def test_k30_selection_is_stable_bounded_and_rare_strata_first(tmp_path: Path) -> None:
    pages = [_stratum_page("rare-%d" % index, name) for index, name in enumerate(CANONICAL_STRATA)]
    pages.extend(_stratum_page("common-%d" % index, "greeting_new_topic") for index in range(8))
    report = build_canonical_strata_metadata(pages)
    selection = select_pages_by_strata(report, max_pages=5)

    assert selection["selected_page_count"] <= 5
    assert set(selection["selected_stratum_counts"]) == set(CANONICAL_STRATA)
    assert all(selection["selected_stratum_counts"][name] >= 1 for name in CANONICAL_STRATA)
    assert selection["selection_rule"].startswith("rare_strata_first")
    assert selection["provider_allowed"] is True
    _assert_body_free(selection)

    # A same-input replay has exactly the same opaque handles and selection
    # hash; the contract never relies on raw body text as an identity.
    replay_report = build_canonical_strata_metadata(pages)
    replay_selection = select_pages_by_strata(replay_report, max_pages=5)
    assert verify_strata_replay(report, replay_report)
    assert selection["selection_hash"] == replay_selection["selection_hash"]

    paths = write_canonical_strata_sidecar(report, tmp_path / "k30-sidecar", selection=selection)
    allowed = {
        "manifest": paths["manifest"],
        "aggregate": paths["aggregate"],
        "strata": paths["strata"],
        "selection": paths["selection"],
    }
    assert set(allowed) == {"manifest", "aggregate", "strata", "selection"}
    # This intentionally reads only the four body-free audit files.  The
    # side-car may also emit a diagnosis file, but K30 does not need it.
    for key, raw_path in allowed.items():
        path = Path(raw_path)
        value: Any
        if path.suffix == ".jsonl":
            value = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
        _assert_body_free(value)
        if key in {"manifest", "aggregate"}:
            assert value["body_free"] is True


def test_k30_single_scope_shortfall_requires_explicit_multi_scope_authorization() -> None:
    pages = [
        _stratum_page("scope-a-%d" % index, name, chat="chat-a")
        for index, name in enumerate(CANONICAL_STRATA[:3])
    ] + [
        _stratum_page("scope-b-%d" % index, name, chat="chat-b")
        for index, name in enumerate(CANONICAL_STRATA[3:])
    ]
    report = build_canonical_strata_metadata(pages)

    selection = select_pages_by_strata(report, max_pages=5)
    assert selection["global_coverage_plan_available"] is True
    assert selection["global_coverage_plan_scope_count"] == 2
    assert selection["single_scope_coverage_plan_available"] is False
    assert selection["scope_authorization_required"] is True
    assert selection["provider_allowed"] is False
    assert "multi_scope" in str(selection["authorization_error"])
    assert selection["missing_strata"]

    authorized = select_pages_by_strata(report, max_pages=5, allow_multi_scope=True)
    assert authorized["provider_allowed"] is True
    assert authorized["selected_page_count"] == 5
    assert authorized["selected_scope_count"] == 2


def test_k30_linear_token_bound_and_recovery_cover_all_retained_roles() -> None:
    packet = _stratum_page("recovery", "topic_shift")
    store = build_linear_stage_packets(
        [packet],
        capacity={
            "max_input_token_proxy": 2_000,
            "max_user_token_proxy": 1_600,
            "max_messages": 24,
            "max_candidate_rows": 64,
            "max_evidence_refs": 64,
        },
    )
    root = store.roots[0]
    stage_a = materialize_stage_a(store, root["page_refs"][0])
    recovered = recover_linear_packet(store, root["root_id"])

    assert stage_a["status"] == "complete"
    assert stage_a["material_stats"]["input_token_proxy"] <= 2_000
    assert stage_a["material_stats"]["user_token_proxy"] <= 1_600
    retained_ids = {
        str(row["message_id"])
        for row in packet["primary_fragments"]
    }
    recovered_ids = {
        str(row["message_id"])
        for key in ("primary_fragments", "adjacent_context")
        for row in recovered.get(key, ())
    }
    assert recovered_ids == retained_ids


def _load_v3_runner() -> Any:
    """Load the K30 runner only after its public module exists."""

    module_names = (
        "wechat_bridge.linear_stage_packet_development_stratified_runner",
        "wechat_bridge.linear_stage_packet_development_v3_stratified",
        "wechat_bridge.linear_stage_packet_development_runner",
    )
    function_names = (
        "run_linear_stage_packet_development_v3_stratified_from_mappings",
        "run_linear_stage_packet_development_stratified_from_mappings",
        "run_linear_stage_packet_development_v3_stratified",
        "run_linear_stage_packet_development_stratified",
        "run_stratified_linear_stage_packet_development",
    )
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for function_name in function_names:
            function = getattr(module, function_name, None)
            if callable(function):
                return module, function
    pytest.skip("K30 v3 stratified runner public API is not present yet")


def _invoke_v3_runner(
    function: Any,
    packets: Sequence[Mapping[str, Any]],
    selection_refs: Sequence[Mapping[str, Any]],
    output: Path,
) -> Any:
    """Call the one public mappings boundary without reaching private input."""

    # The first public draft follows K9's three-positional mapping boundary.
    # Keep this adapter narrow: if the API changes, the failure points at the
    # contract rather than silently falling back to a private artifact.
    try:
        return function(
            packets,
            selection_refs,
            output,
            selected_packet_count=len(selection_refs),
            max_pages=5,
            capacity={
                "max_input_token_proxy": 2_000,
                "max_user_token_proxy": 1_600,
                "max_messages": 24,
                "max_candidate_rows": 64,
                "max_evidence_refs": 64,
            },
        )
    except TypeError as exc:
        # A public API that omits selection_refs is still safe to adapt once;
        # no path-based invocation is attempted here.
        try:
            return function(
                packets,
                output,
                selection_refs=selection_refs,
                selected_packet_count=len(selection_refs),
                max_pages=5,
                capacity={
                    "max_input_token_proxy": 2_000,
                    "max_user_token_proxy": 1_600,
                    "max_messages": 24,
                    "max_candidate_rows": 64,
                    "max_evidence_refs": 64,
                },
            )
        except TypeError:
            raise exc


def _result_mapping(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(key, default)
    return getattr(result, key, default)


def test_k30_public_runner_contract_and_body_free_artifact_audit(tmp_path: Path) -> None:
    module, runner = _load_v3_runner()
    packets = [_runner_safe_packet()]
    selections = [
        {"packet_id": packet["packet_id"], "selected": True, "selection_rank": index + 1}
        for index, packet in enumerate(packets)
    ]
    result = _invoke_v3_runner(runner, packets, selections, tmp_path / "linear_stage_packet_development_v3_stratified")
    assert _result_mapping(result, "status") in {"complete", "pending", "authorized"}
    paths = _result_mapping(result, "artifact_paths", {})
    assert isinstance(paths, Mapping)

    # Only these four files are opened.  In particular, the audit never opens
    # store/messages/recovery/materialized files, even if the runner emits them.
    aliases = {
        "manifest": ("manifest",),
        "aggregate": ("aggregate",),
        "strata": ("strata", "strata_map", "strata_metadata"),
        "selection": ("selection", "selection_map"),
    }
    for required, candidates in aliases.items():
        raw_path = next((paths.get(candidate) for candidate in candidates if paths.get(candidate)), None)
        if raw_path is None:
            output_files = _result_mapping(result, "manifest", {}).get("output_files", {})
            filename = next((output_files.get(candidate) for candidate in candidates if output_files.get(candidate)), None)
            if filename:
                raw_path = Path(_result_mapping(result, "output_directory")) / filename
        assert raw_path is not None, "runner did not expose body-free %s artifact" % required
        path = Path(raw_path)
        assert path.is_file()
        value = (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if path.suffix == ".jsonl"
            else json.loads(path.read_text(encoding="utf-8"))
        )
        _assert_body_free(value)

    manifest = _result_mapping(result, "manifest", {})
    aggregate = _result_mapping(result, "aggregate", {})
    assert manifest.get("provider_called", False) is False
    assert int(manifest.get("provider_calls", 0) or 0) == 0
    assert aggregate.get("provider_called", False) is False
    assert int(aggregate.get("provider_calls", 0) or 0) == 0


def test_k30_public_runner_fails_closed_on_cross_scope_relationship(tmp_path: Path) -> None:
    """A relation may not smuggle a second chat into one selected page."""

    _, runner = _load_v3_runner()
    packet = _runner_safe_packet("cross-scope")
    packet["candidate_qa_links"] = [
        {
            "candidate_id": "cross-scope-candidate",
            "left_message_id": "m-cross-scope-context",
            "right_message_id": "m-cross-scope-semantic",
            "left_account_id": ACCOUNT,
            "left_chat_id": "chat-a",
            "right_account_id": ACCOUNT,
            "right_chat_id": "chat-b",
            "relation_label": "candidate_competition",
            "evidence_refs": [{"evidence_id": "e-cross-scope"}],
        }
    ]
    refs = [{"packet_id": packet["packet_id"], "selected": True, "selection_rank": 1}]
    output = tmp_path / "linear_stage_packet_development_v3_stratified-cross-scope"
    try:
        result = _invoke_v3_runner(runner, [packet], refs, output)
    except (ValueError, RuntimeError) as exc:
        assert "scope" in str(exc).casefold() or "relation" in str(exc).casefold()
        return

    # Implementations that persist a fail-closed result instead of raising
    # must expose a non-zero cross-scope/zero-tolerance diagnostic and keep
    # provider use disabled.
    aggregate = _result_mapping(result, "aggregate", {})
    provider_called = bool(_result_mapping(result, "manifest", {}).get("provider_called", False))
    assert provider_called is False

    def has_violation(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = str(key).casefold()
                if any(marker in name for marker in ("cross_scope", "scope_violation", "zero_tolerance")):
                    if child not in (None, False, 0, "", [], {}, ()):
                        return True
                if has_violation(child):
                    return True
        elif isinstance(value, (list, tuple, set, frozenset)):
            return any(has_violation(child) for child in value)
        return False

    assert has_violation(aggregate)


__all__ = ["CANONICAL_STRATA", "STRATA_OUTPUT_FILENAMES"]
