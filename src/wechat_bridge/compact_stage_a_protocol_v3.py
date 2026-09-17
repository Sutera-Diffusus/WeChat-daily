"""Offline compact Stage-A topic assignment wire, version 3.

The v2 wire used one-letter output keys (``t/i/p/c/u``).  That saved a few
bytes but made the provider-facing contract unnecessarily opaque and was a
direct contributor to the K14 output failures: a response such as ``t`` was
easy to confuse with an unrelated short-key protocol.  K22 keeps the
request-side optimisation -- long authoritative ids occur once in a
request-local table and the model only sees short aliases -- while making the
*output* self describing.

This module is intentionally offline.  It has no provider client, no
filesystem access, no private/frozen-data access, and no production entry
point.  It only builds/validates/measures an in-memory protocol exchange and
projects a body-free ledger.  Stage A answers one question: which primary
messages belong to which topics, and which context-only messages help those
topics.  People, speakers, objects, claims, states, evidence, summaries and
all other Stage-B fields are out of scope and rejected by exact-key checks.

The canonical response shape is::

    {"topics":[
      {"topic_id":"t1",
       "primary_message_ids":["m1"],
       "context_message_ids":["m2"],
       "uncertainty":"unknown"}
    ]}

``m1``/``m2`` are request-local aliases, not model-authoritative ids.  The
local resolver can map them to the immutable scoped handles after validation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .dialogue_segments import has_context_prefix, is_context_only_text

PROTOCOL_VERSION = "stage_a_topic_assignment_compact_v3"
STAGE_A_PROTOCOL_VERSION = PROTOCOL_VERSION
STAGE_A_SCHEMA_VERSION = PROTOCOL_VERSION
COMPACT_STAGE_A_SCHEMA_VERSION = PROTOCOL_VERSION
PROMPT_VERSION = "stage_a_topic_assignment_compact_prompt_v3_grouping1"
CACHE_VERSION = "stage_a_topic_assignment_compact_cache_v3_grouping1"
CACHE_NAMESPACE = "compact-stage-a-v3"
# Explicit aliases make the version boundary obvious to adapters that use a
# ``wire_*`` naming convention.  They are values, not a second protocol.
WIRE_PROTOCOL_VERSION = PROTOCOL_VERSION
WIRE_PROMPT_VERSION = PROMPT_VERSION
CACHE_SCHEMA_VERSION = CACHE_VERSION

# A topic cannot be created by context alone.  The same rule is embedded in
# the system prompt, request/response schema descriptions, and the validator.
TOPIC_LIMIT_RULE = (
    "primary_count=count(message rows with role primary)>=1; "
    "topic_count<=primary_count; every topic has >=1 primary; "
    "each primary is assigned exactly once; each context at most once"
)

# Context ownership is a global partition, not a per-topic hint.  Keep this
# text as a single source of truth for the request schema, response schema,
# system prompt, and the validator's body-free constraint field.  The rule is
# deliberately expressed in terms of aliases because authoritative handles
# never occur in provider output.
CONTEXT_UNIQUENESS_FIELD = "context_unique"
CONTEXT_UNIQUENESS_MODE = "global"
CONTEXT_UNIQUENESS_RULE = (
    "context_unique=global; each context_message_id/alias may appear in at most "
    "one topic globally; the allocated context set is disjoint across topics "
    "and an allocated alias must not be reused; context aliases come only from "
    "request rows with role context"
)

# Grouping guidance is intentionally a short, provider-facing rule rather
# than another semantic schema.  The model still makes the topic decision;
# these cues only counter the known one-message-per-topic failure mode.
TOPIC_GROUPING_GUIDANCE = (
    "group primary rows sharing subject/object/action, question-answer continuity, "
    "or state update; multiple primary IDs may share; do not create one topic "
    "per message; under uncertainty prefer merge; split only on explicit "
    "subject/object/action/topic shift; reaction/system/event/media "
    "placeholders stay context; only non-placeholder direct human caption + "
    "positive evidence may be primary; candidate cues are context hints only, "
    "never final decisions; connected candidate groups are merge priors, not "
    "local topics"
)
GROUPING_HINTS_FIELD = "g"
TOPIC_HINT_KEYS = frozenset({"a", "b", "r"})
TOPIC_HINT_RELATIONS = frozenset(
    {
        "same_subject",
        "same_object",
        "same_action",
        "state_update",
        "qa_continuity",
        "reply_continuity",
        "candidate_context",
        "candidate_qa",
        "topic_shift",
    }
)
TOPIC_HINT_RELATION_ORDER = (
    "same_subject",
    "same_object",
    "same_action",
    "state_update",
    "qa_continuity",
    "reply_continuity",
    "candidate_context",
    "candidate_qa",
    "topic_shift",
)
MAX_TOPIC_GROUPING_HINTS = 24
# The wire validator continues to accept the historical 24-entry envelope so
# an explicitly supplied packet remains a strict, backwards-compatible
# surface.  Auto-generated hints are deliberately sparser: dense pages can
# otherwise spend the remaining request budget on repeated pair cues before a
# provider sees the primary rows themselves.  The preflight is only a hint
# producer; it never changes message roles or topic assignments.
MAX_GENERATED_TOPIC_GROUPING_HINTS = 16
MAX_TOPIC_HINTS_PER_ALIAS = 2
MAX_TOPIC_HINTS_PER_RELATION = 4
TOPIC_HINT_MAX_RELATIONS = 3

# These examples contain aliases and scalar enum values only.  They are safe to
# expose in a provider prompt/schema and make the cross-topic ownership rule
# concrete without copying any message body.
MINIMAL_MULTI_TOPIC_VALID_EXAMPLE = (
    '{"topics":[{"topic_id":"t1","primary_message_ids":["m1"],'
    '"context_message_ids":["m3"],"uncertainty":"uncertain"},'
    '{"topic_id":"t2","primary_message_ids":["m2"],'
    '"context_message_ids":[],"uncertainty":"unknown"}]}'
)
MINIMAL_MULTI_TOPIC_RESPONSE_EXAMPLE = MINIMAL_MULTI_TOPIC_VALID_EXAMPLE
DUPLICATE_CONTEXT_COUNTEREXAMPLE = (
    '{"topics":[{"topic_id":"t1","primary_message_ids":["m1"],'
    '"context_message_ids":["m3"],"uncertainty":"unknown"},'
    '{"topic_id":"t2","primary_message_ids":["m2"],'
    '"context_message_ids":["m3"],"uncertainty":"unknown"}]}'
)
MULTI_PRIMARY_ONE_TOPIC_EXAMPLE = (
    '{"topics":[{"topic_id":"t1","primary_message_ids":["m1","m2"],'
    '"context_message_ids":[],"uncertainty":"unknown"}]}'
)

# Keep this finite and body-free so adapters can preserve a useful category
# without persisting model text.  ``output_context_item`` covers a context
# alias that is not in the legal context channel; the duplicate codes cover
# both per-topic and cross-topic reuse.
CONTEXT_UNIQUENESS_ERROR_CODES = frozenset(
    {
        "request_context_unique",
        "output_context_item",
        "output_context_duplicate",
        "duplicate_context_handle",
    }
)

# Keep the prompt short enough that the largest request remains below the
# 1600-token proxy while being explicit enough to prevent the v2 short-key
# response from reappearing.  The examples are also exposed in
# RESPONSE_SCHEMA so tests and future adapters use one source of truth.
MINIMAL_RESPONSE_EXAMPLE = (
    '{"topics":[{"topic_id":"t1","primary_message_ids":["m1"],'
    '"context_message_ids":[],"uncertainty":"unknown"}]}'
)
# This wording is intentionally compact.  The request table may contain the
# largest supported 14 messages and 20 candidates; keeping the fixed system
# instructions short leaves room for that table under the 1600 proxy.
SYSTEM_PROMPT = (
    "JSON only; only these exact keys: topics/topic_id/primary_message_ids/"
    "context_message_ids/uncertainty; use m aliases. "
    + TOPIC_GROUPING_GUIDANCE
    + ". "
    "topic_count<=primary_count; each topic has >=1 primary; primary exactly "
    "once; context at most once; uncertainty=certain|uncertain|unknown. "
    + CONTEXT_UNIQUENESS_RULE
    + ". If context ownership is ambiguous, omit the alias; alternatively put "
    "it only on the most relevant topic and mark that topic uncertain. "
    "If request u=uncertain, never emit certain; unresolved candidate cues are "
    "not facts; merged candidate cues are auxiliary only and cannot create a "
    "primary message or a topic; g hints are structural preflight hints only "
    "and never a model verdict. "
    "No prose, markdown, duplicate keys, or Stage-B fields. Minimal valid example: see schema."
    + ". Minimal two-topic valid example: "
    + MINIMAL_MULTI_TOPIC_VALID_EXAMPLE
    + ". Invalid duplicate-context example (m3 is reused across topics): "
    + DUPLICATE_CONTEXT_COUNTEREXAMPLE
    + ". Merge: m1,m2=>one topic."
)
STAGE_A_SYSTEM_PROMPT = SYSTEM_PROMPT

TOP_LEVEL_KEYS = frozenset({"v", "s", "h"})
# ``u`` is an optional request-only ceiling and ``context_unique`` is a
# body-free contract marker.  ``g`` is an optional, compact structural-hint
# list; it contains aliases and enum relation labels only.
REQUEST_OPTIONAL_KEYS = frozenset({"u", CONTEXT_UNIQUENESS_FIELD, GROUPING_HINTS_FIELD})
MESSAGE_ROW_KEYS = frozenset({"i", "k", "h", "r", "x"})
CANDIDATE_ROW_KEYS = frozenset({"i", "k", "h"})
OUTPUT_TOP_KEYS = frozenset({"topics"})
OUTPUT_TOPIC_KEYS = frozenset(
    {"topic_id", "primary_message_ids", "context_message_ids", "uncertainty"}
)
OUTPUT_FIELDS = OUTPUT_TOPIC_KEYS
TOPIC_FIELDS = OUTPUT_TOPIC_KEYS
REQUEST_SCHEMA = (
    "{v:string,s:{a:string,c:string},"
    "h:[{i:message_alias,k:m,r:p|c,h:scoped_handle,x:bounded_cue}|"
    "{i:candidate_alias,k:c,h:scoped_handle}],"
    "u?:uncertain|unknown,context_unique?:global,"
    "g?:[{a:message_alias,b:message_or_candidate_alias,r:topic_hint}],"
    + TOPIC_LIMIT_RULE
    + ";"
    + TOPIC_GROUPING_GUIDANCE
    + ";"
    + CONTEXT_UNIQUENESS_RULE
    + "}"
)
RESPONSE_SCHEMA = (
    "{topics:[{topic_id:string,primary_message_ids:[message_alias],"
    "context_message_ids:[message_alias],uncertainty:certain|uncertain|unknown}],"
    + TOPIC_LIMIT_RULE
    + ";"
    + TOPIC_GROUPING_GUIDANCE
    + ";"
    + CONTEXT_UNIQUENESS_RULE
    + "; only these exact keys; example="
    + MINIMAL_RESPONSE_EXAMPLE
    + ";valid_two_topic_example="
    + MINIMAL_MULTI_TOPIC_VALID_EXAMPLE
    + ";invalid_duplicate_context_example="
    + DUPLICATE_CONTEXT_COUNTEREXAMPLE
    + ";multiple_primary_one_topic_example="
    + MULTI_PRIMARY_ONE_TOPIC_EXAMPLE
)

HANDLE_KINDS = frozenset({"m", "c"})
UNCERTAINTY_VALUES = frozenset({"certain", "uncertain", "unknown"})

MAX_MESSAGES = 14
MAX_CANDIDATES = 20
MAX_HANDLE_CHARS = 128
MAX_ALIAS_CHARS = 8
MAX_TOPIC_ID_CHARS = 32
# Stage A only needs a tiny registration cue.  Keeping this bounded to 32
# characters avoids repeating message material in the provider request while
# preserving enough signal for coarse topic assignment.
MAX_MESSAGE_CUE_CHARS = 32
MAX_INPUT_TOKEN_PROXY = 1600
MAX_OUTPUT_TOKENS = 400
TOKEN_PROXY_CHARS = 4

# The historical proxy is intentionally retained for backwards-compatible
# diagnostics.  Provider preflight uses this conservative, versioned upper
# bound because the compact character proxy under-counted the observed
# request envelope.  Keep the arithmetic integer/monotonic so a retry cannot
# accidentally pass a stricter gate than the initial preflight.
TOKEN_CALIBRATION_VERSION = "stage_a_topic_guided_token_calibration_v1"
TOKEN_CALIBRATION_MULTIPLIER = 1.31
TOKEN_CALIBRATION_ADDEND = 432
CALIBRATED_INPUT_TOKEN_PROXY_LIMIT = MAX_INPUT_TOKEN_PROXY

# Body-free context validation telemetry.  These are semantic labels, not
# aliases or provider diagnostics.  Counts are calculated while parsing and
# only the fixed names/counts/booleans may leave the protocol boundary.
CONTEXT_VALIDATION_TAXONOMY_VERSION = "stage_a_context_validation_taxonomy_v1"
CONTEXT_ERROR_TAXONOMY_VERSION = CONTEXT_VALIDATION_TAXONOMY_VERSION
CONTEXT_ERROR_TELEMETRY_KEYS = (
    "duplicate_within_topic",
    "duplicate_across_topics",
    "primary_as_context",
    "candidate_or_unknown_alias",
    "primary_context_overlap",
    "invalid_type",
)

TOPIC_SPLIT_RELATIONS = frozenset({"topic_shift"})

_ALIAS_RE = re.compile(r"^[mc](?:0|[1-9])[0-9]{0,2}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# The compact runner persists only opaque handles and scalar diagnostics.  A
# validation failure therefore needs a finite, stable taxonomy of its own;
# otherwise the runner's safe-code gate can collapse a useful protocol error
# into ``unknown_error``.  Keep these labels deliberately independent of
# provider text and expose them on the protocol exception as well as through
# the runner's diagnostic projection.
ERROR_TAXONOMY_VERSION = "compact_stage_a_response_error_taxonomy_v1"
VALIDATION_CATEGORIES = frozenset(
    {
        "json",
        "schema_keys",
        "alias",
        "primary_exactly_once",
        "context_unique",
        "topic_limit",
        "cross_scope",
        "evidence",
        "enum",
        "exception",
        "input",
        "token_limit",
        "provider",
        "authorization",
        "selection",
        "body_free",
        "other_validation",
    }
)

# All literals emitted by this v3 validator (plus the v2 compatibility
# spelling and the staged analyzer's evidence/scope spellings) are listed
# explicitly.  This is intentionally an allow-list: arbitrary provider
# exception text must never become an artifact error code.
KNOWN_VALIDATION_ERROR_CODES = frozenset(
    {
        "canonical_json_invalid",
        "nonstandard_json_number",
        "duplicate_json_key",
        "json_not_text",
        "invalid_json",
        "request_shape",
        "request_keys",
        "request_version",
        "request_scope_shape",
        "request_scope_keys",
        "request_scope_account",
        "request_scope_chat",
        "request_handle_table",
        "request_handle_table_limit",
        "request_handle_row_shape",
        "request_handle_kind",
        "request_handle_row_keys",
        "request_alias_invalid",
        "alias_invalid",
        "duplicate_request_alias",
        "request_alias_kind",
        "request_authoritative_handle_invalid",
        "handle_invalid",
        "cross_scope_handle",
        "handle_kind_mismatch",
        "duplicate_authoritative_handle",
        "request_message_limit",
        "request_message_role",
        "request_message_cue",
        "request_candidate_limit",
        "request_messages_empty",
        "request_primary_messages_empty",
        "request_uncertainty_ceiling",
        "request_context_unique",
        "request_grouping_hints",
        "request_grouping_hint_shape",
        "request_grouping_hint_keys",
        "request_grouping_hint_alias",
        "request_grouping_hint_relation",
        "request_grouping_hint_duplicate",
        "request_grouping_hint_pair",
        "request_grouping_hint_candidate",
        "scope_missing",
        "scope_invalid",
        "scope_invalid_account",
        "scope_invalid_chat",
        "packet_shape",
        "messages_shape",
        "candidates_shape",
        "messages_empty",
        "messages_limit",
        "candidates_limit",
        "message_handle_missing",
        "message_handle_missing_shape",
        "candidate_handle_missing",
        "candidate_handle_missing_shape",
        "message_cue_invalid",
        "message_cue_too_long",
        "output_shape",
        "output_keys",
        "output_topics",
        "output_topic_limit",
        "output_topic_shape",
        "output_topic_keys",
        "duplicate_topic_id",
        "topic_id_invalid",
        "output_primary",
        "output_primary_item",
        "primary_alias_not_semantic_eligible",
        "primary_alias_not_source_primary",
        "output_primary_duplicate",
        "output_context",
        "output_context_item",
        "output_context_duplicate",
        "output_uncertainty_enum",
        "uncertainty_overstated_unresolved_reference",
        "output_primary_context_overlap",
        "duplicate_primary_handle",
        "duplicate_context_handle",
        "primary_coverage",
        "output_token_proxy_exceeded",
        "system_prompt_invalid",
        "request_input_token_proxy_exceeded",
        "max_size_response_exceeds_output_limit",
        "ledger_report_shape",
        "model_id_invalid",
        "ruleset_version_invalid",
        # v2/current adapter aliases retained as safe, body-free codes.
        "output_primary_handle_scope",
        "output_context_handle_scope",
        # Staged analyzer validation codes can cross the same adapter boundary
        # in synthetic contract tests.  They are not accepted by this v3
        # validator; listing them only prevents an already-known code from
        # being erased if a caller wraps it here.
        "stage_a_shape",
        "stage_a_topics_type",
        "stage_a_topic_shape",
        "stage_a_topic_id",
        "stage_a_duplicate_topic",
        "stage_a_primary_message_ids",
        "stage_a_primary_message_ids_duplicate",
        "stage_a_context_message_ids",
        "stage_a_context_message_ids_duplicate",
        "stage_a_primary_out_of_scope",
        "stage_a_context_out_of_scope",
        "stage_a_primary_context_overlap",
        "stage_a_relation",
        "stage_a_uncertainties",
        "stage_a_uncertainties_duplicate",
        "stage_a_evidence_ids",
        "stage_a_evidence_ids_duplicate",
        "stage_a_evidence_out_of_scope",
        "stage_b_shape",
        "stage_b_topic_id",
        "stage_b_topic_mismatch",
        "stage_b_claims_type",
        "stage_b_claim_shape",
        "stage_b_speaker_invalid",
        "stage_b_subject_invalid",
        "stage_b_mentioned_invalid",
        "stage_b_target_invalid",
        "stage_b_object_invalid",
        "stage_b_speaker_out_of_scope",
        "stage_b_subject_out_of_scope",
        "stage_b_mentioned_out_of_scope",
        "stage_b_target_out_of_scope",
        "stage_b_object_out_of_scope",
        "stage_b_action",
        "stage_b_claim_type",
        "stage_b_state",
        "stage_b_modality",
        "stage_b_evidence_ids",
        "stage_b_evidence_ids_duplicate",
        "stage_b_evidence_out_of_scope",
        "stage_b_known_claim_missing_evidence",
        "stage_b_uncertainties",
        "stage_b_uncertainties_duplicate",
        "stage_c_shape",
        "stage_c_accepted_claim_ids",
        "stage_c_accepted_claim_ids_duplicate",
        "stage_c_accepted_claim_out_of_scope",
        "stage_c_conflicts_type",
        "stage_c_conflict_shape",
        "stage_c_conflict_claim_ids",
        "stage_c_conflict_claim_ids_duplicate",
        "stage_c_conflict_claim_scope",
        "stage_c_conflict_reason",
        "stage_c_conflict_evidence_ids",
        "stage_c_conflict_evidence_ids_duplicate",
        "stage_c_conflict_evidence_scope",
        "stage_c_conflict_missing_evidence",
        "stage_c_missing_context",
        "stage_c_missing_context_duplicate",
        "stage_c_needs_more_context",
        "stage_c_needs_more_context_duplicate",
        "stage_c_message_scope",
        "stage_c_overmerge_type",
        "stage_c_overmerge_shape",
        "stage_c_overmerge_topics",
        "stage_c_overmerge_topics_duplicate",
        "stage_c_overmerge_claims",
        "stage_c_overmerge_claims_duplicate",
        "stage_c_overmerge_scope",
        "stage_c_undermerge_type",
        "stage_c_undermerge_shape",
        "stage_c_undermerge_topics",
        "stage_c_undermerge_topics_duplicate",
        "stage_c_undermerge_messages",
        "stage_c_undermerge_messages_duplicate",
        "stage_c_undermerge_scope",
        "context_packet_not_object",
        "context_packet_type",
        "context_packet_extra_field",
        "context_packet_schema_version",
        "context_packet_packet_id",
        "context_packet_scope",
        "context_packet_message_ids_type",
        "context_packet_context_message_ids_type",
        "context_packet_evidence_ids_type",
        "context_packet_entity_ids_type",
        "context_packet_messages_type",
        "context_packet_evidence_type",
        "context_packet_metadata_type",
        "packet_id",
        "scope",
        "message_ids",
        "context_message_ids",
        "evidence_ids",
        "entity_ids",
        "messages_shape",
        "evidence_shape",
        "metadata_shape",
        "message_context_overlap",
        "frozen_scope_forbidden",
        "id_invalid",
        "previous_packet_mismatch",
        # Stable adapter/runner boundary codes.  These are included here so
        # the durable body-free ledger can preserve a known provider failure
        # instead of degrading it to ``provider_error`` when the compact
        # runner records the exception.
        "provider_response_not_text",
        "provider_nonstandard_json",
        "provider_duplicate_json_key",
        "provider_invalid_json",
        "provider_unconfigured",
        "provider_sdk_unavailable",
        "provider_request_failed",
        "provider_response_shape",
        "provider_response_object",
        "provider_response_metadata",
        "fake_response_missing",
        "fake_response_exhausted",
        "provider_error",
        "model_call_failed",
        "stage_call_failed",
        "stage_dependency_pending",
        "unsupported_model_interface",
        "context_only_page",
        "body_free_violation",
    }
)


def validation_categories_for_code(code: Any) -> Tuple[str, ...]:
    """Return fixed body-free categories for a known protocol error code."""

    text = str(code or "").strip()
    folded = text.casefold()
    if folded in {
        "invalid_json",
        "json_not_text",
        "duplicate_json_key",
        "nonstandard_json_number",
        "canonical_json_invalid",
        "provider_nonstandard_json",
        "provider_duplicate_json_key",
        "provider_invalid_json",
    }:
        return ("json",)
    if "evidence" in folded:
        return ("evidence",)
    if folded == "primary_alias_not_semantic_eligible":
        return ("selection",)
    if folded == "primary_alias_not_source_primary":
        return ("selection",)
    if folded == "uncertainty_overstated_unresolved_reference":
        return ("enum",)
    if "cross_scope" in folded or "scope_violation" in folded or "out_of_scope" in folded or "chat_mismatch" in folded:
        return ("cross_scope",)
    if folded in CONTEXT_UNIQUENESS_ERROR_CODES or folded in {
        "output_context",
        "stage_a_context_message_ids",
        "stage_a_context_message_ids_duplicate",
    }:
        return ("context_unique",)
    if folded.startswith("request_grouping_hint") or folded == "request_grouping_hints":
        return ("input",)
    if "alias" in folded or "handle" in folded and ("primary" not in folded and "context" not in folded):
        return ("alias",)
    if "primary" in folded or "coverage" in folded:
        return ("primary_exactly_once",)
    if folded == "output_topic_limit" or "topic_limit" in folded:
        return ("topic_limit",)
    if "context" in folded and ("duplicate" in folded or "overlap" in folded or "item" in folded):
        return ("context_unique",)
    if "topics" in folded or "topic" in folded and "limit" in folded:
        return ("schema_keys",)
    if "uncertainty" in folded or folded.endswith(("_enum", "_role", "_kind", "_version")):
        return ("enum",)
    if "token" in folded or "proxy" in folded or "output_limit" in folded:
        return ("token_limit",)
    if folded in {"provider_response_shape", "provider_response_object", "provider_response_not_text", "duplicate_topic_id"}:
        return ("schema_keys",)
    if folded in {"provider_response_metadata", "provider_request_failed", "model_call_failed", "stage_call_failed", "provider_error"}:
        return ("exception",)
    if folded in {"stage_dependency_pending", "context_only_page"}:
        return ("input",)
    if folded == "unsupported_model_interface":
        return ("exception",)
    if folded in {"packet_shape", "scope_invalid", "scope_invalid_account", "scope_invalid_chat", "model_id_invalid", "ruleset_version_invalid", "id_invalid", "previous_packet_mismatch", "context_packet_type"}:
        return ("input",)
    if folded.startswith(("request_", "messages_", "candidates_", "message_", "candidate_", "scope_", "packet_", "context_packet_")):
        return ("input",)
    if folded.startswith(("output_", "stage_")) or folded in {"topic_id_invalid", "topic_id"}:
        return ("schema_keys",)
    if folded in {"body_free_violation"}:
        return ("body_free",)
    if folded.startswith(("authorization_", "selection_", "candidate_scope_")):
        return ("authorization",)
    if folded.startswith(("provider_", "model_", "unsupported_", "fake_")):
        return ("provider",)
    if folded in {"unknown_error", "stage_call_failed", "model_call_failed", "provider_error"}:
        return ("exception",)
    return ("other_validation",)


class CompactStageAProtocolV3Error(ValueError):
    """Body-free, fail-closed K22 protocol error."""

    def __init__(self, code: str, *, context_telemetry: Optional[Mapping[str, Any]] = None) -> None:
        self.code = str(code)
        self.validation_categories = validation_categories_for_code(self.code)
        self.error_category = self.validation_categories[0] if self.validation_categories else "other_validation"
        self.context_telemetry = _body_free_context_telemetry(context_telemetry)
        self.context_error_counts = dict(self.context_telemetry.get("error_counts", {}))
        self.context_error_flags = dict(self.context_telemetry.get("error_flags", {}))
        super().__init__(self.code)


# Consistent names make it easy for a runner or independent audit to avoid
# accidentally treating a class as a callable validator.
ProtocolError = CompactStageAProtocolV3Error
CompactStageAProtocolError = CompactStageAProtocolV3Error


def _fail(code: str) -> None:
    raise CompactStageAProtocolV3Error(code)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=str)
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON and reject non-standard numbers."""

    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompactStageAProtocolV3Error("canonical_json_invalid") from exc


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _reject_constant(value: str) -> None:
    _fail("nonstandard_json_number")


def _reject_duplicate_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def strict_json_loads(value: Any) -> Any:
    """Parse exactly one JSON value; prose/fences and duplicate keys fail."""

    if type(value) is not str:
        _fail("json_not_text")
    try:
        return json.loads(
            value,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except CompactStageAProtocolV3Error:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CompactStageAProtocolV3Error("invalid_json") from exc


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], code: str) -> None:
    if set(value) != set(expected):
        _fail(code)


def _safe_string(
    value: Any,
    code: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        _fail(code)
    if not allow_empty and not value:
        _fail(code)
    if len(value) > max_length or _CONTROL_RE.search(value):
        _fail(code)
    if value != value.strip():
        _fail(code)
    return value


def _safe_handle(value: Any, code: str = "handle_invalid") -> str:
    return _safe_string(value, code, max_length=MAX_HANDLE_CHARS)


def _safe_alias(value: Any, code: str = "alias_invalid") -> str:
    alias = _safe_string(value, code, max_length=MAX_ALIAS_CHARS)
    if not _ALIAS_RE.fullmatch(alias):
        _fail(code)
    return alias


def _safe_topic_id(value: Any, code: str = "topic_id_invalid") -> str:
    return _safe_string(value, code, max_length=MAX_TOPIC_ID_CHARS)


def _scope_pair(value: Any, code: str = "scope_invalid") -> Tuple[str, str]:
    scope = _mapping(value, code)
    account = scope.get("a", scope.get("account_id", scope.get("account")))
    chat = scope.get("c", scope.get("chat_id", scope.get("chat")))
    return (
        _safe_string(account, code + "_account", max_length=MAX_HANDLE_CHARS),
        _safe_string(chat, code + "_chat", max_length=MAX_HANDLE_CHARS),
    )


def _parse_handle_scope(value: str) -> Optional[Tuple[str, str]]:
    """Parse ``account/chat|kind|id`` if a scoped handle uses that form."""

    if "|" not in value:
        return None
    prefix = value.split("|", 1)[0]
    if "/" not in prefix:
        return None
    account, chat = prefix.split("/", 1)
    if not account or not chat:
        return None
    return account, chat


def _assert_scope(handle: str, scope: Tuple[str, str], code: str = "cross_scope_handle") -> None:
    parsed = _parse_handle_scope(handle)
    if parsed is not None and parsed != scope:
        _fail(code)


def _assert_handle_kind(handle: str, kind: str) -> None:
    parts = handle.split("|")
    if len(parts) >= 3 and parts[1] in {"message", "candidate"}:
        expected = "message" if kind == "m" else "candidate"
        if parts[1] != expected:
            _fail("handle_kind_mismatch")


_PROVIDER_MEDIA_MESSAGE_TYPES = frozenset(
    {
        "image",
        "photo",
        "picture",
        "video",
        "audio",
        "voice",
        "file",
        "document",
        "card",
        "sticker",
        "emoji",
        "reaction",
        "system",
        "location",
        "media",
        # Event rows are transport/system records even when the upstream
        # adapter did not add an explicit ``is_placeholder`` marker.  They
        # may cross the provider boundary only through the direct-caption
        # exception handled by ``_role_from_record``.
        "event",
        "event_message",
        "event_placeholder",
        "event_place_holder",
    }
)
_PROVIDER_PLACEHOLDER_ROLES = frozenset(
    {
        "empty_authority",
        "media_placeholder",
        "reaction",
        "placeholder",
        "event_placeholder",
        "event_place_holder",
    }
)
_PROVIDER_AUTHORITY_ROLES = frozenset(
    {
        "authority",
        "authority_only",
        "empty_authority",
    }
)
_PROVIDER_DIRECT_HUMAN_CUE_FIELDS = (
    "caption",
    "text",
    "text_redacted",
    "message_text",
)
_MERGED_CANDIDATE_CUE_KEYS = frozenset(
    {
        "merged_candidate_cue",
        "merged_candidate_cues",
        "merged_candidate_cue_present",
        "_merged_candidate_cue",
        "merged_cue",
        "merged_cues",
        "candidate_cue",
        "candidate_cues",
        "candidate_context",
        "activation_cue",
        "activation_cues",
        "cue_text",
    }
)


def _normalise_provider_label(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _record_role_labels(value: Mapping[str, Any]) -> frozenset[str]:
    labels: set[str] = set()
    sources: List[Mapping[str, Any]] = [value]
    for key in ("identity_row", "authoritative", "source_row", "mapping_row"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key in ("semantic_role", "provider_role", "primary_context_role", "role", "layer", "message_role"):
            label = _normalise_provider_label(source.get(key))
            if label:
                labels.add(label)
        roles = source.get("roles")
        if isinstance(roles, (list, tuple, set, frozenset)):
            labels.update(_normalise_provider_label(item) for item in roles if item not in (None, ""))
    return frozenset(labels)


def _record_message_type(value: Mapping[str, Any]) -> str:
    """Read only the authoritative type marker used by the provider gate."""

    sources: List[Mapping[str, Any]] = [value]
    for nested_key in ("identity_row", "authoritative", "source_row", "mapping_row"):
        nested = value.get(nested_key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        for key in ("message_type", "type", "media_type", "content_type"):
            marker = _normalise_provider_label(source.get(key))
            if marker:
                return marker
    return ""


def _record_has_merged_candidate_cue(value: Any) -> bool:
    """Return whether a row carries an explicit merged/candidate marker.

    Candidate cues are auxiliary evidence only.  The marker is intentionally
    read from the row and its narrow identity/authority wrappers, never from
    arbitrary nested content, so a body field cannot accidentally alter the
    provider role.
    """

    if not isinstance(value, Mapping):
        return False
    for key, child in value.items():
        if str(key).casefold() not in _MERGED_CANDIDATE_CUE_KEYS:
            continue
        # ``*_present=False`` is an explicit absence marker and must not make
        # an otherwise ordinary text row look like a candidate overlay.
        if child is True or child == 1:
            return True
        if child not in (None, "", [], (), {}, False, 0):
            return True
    for key in ("identity_row", "source_row", "mapping_row", "authoritative", "authority"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and _record_has_merged_candidate_cue(nested):
            return True
    return False


def _record_is_hard_placeholder(value: Any) -> bool:
    """Return whether an explicit placeholder marker must stay context-only.

    Typed media rows may carry a real caption, so the media gate cannot treat
    every ``image``/``event`` type as a hard placeholder.  Conversely, an
    explicit placeholder marker is authoritative provenance: even a stale
    positive source-primary role and a direct-looking cue must not turn that
    row into an independent topic.  Follow only the same narrow identity /
    authority wrappers used by the role gate; arbitrary body mappings are not
    searched.
    """

    if not isinstance(value, Mapping):
        return False
    marker_keys = {
        "placeholder",
        "is_placeholder",
        "event_placeholder",
        "media_placeholder",
        "is_event_placeholder",
        "is_media_placeholder",
        "placeholder_present",
        "event_placeholder_present",
        "media_placeholder_present",
    }
    label_keys = (
        "message_type",
        "type",
        "media_type",
        "content_type",
        "fragment_type",
        "kind",
        "event_type",
        "semantic_role",
        "provider_role",
        "primary_context_role",
        "role",
        "layer",
        "message_role",
    )
    placeholder_labels = _PROVIDER_PLACEHOLDER_ROLES | {
        "placeholder",
        "event_placeholder",
        "event_place_holder",
        "media_placeholder",
        "reaction",
    }
    sources: List[Mapping[str, Any]] = []
    queue: List[Mapping[str, Any]] = [value]
    seen: set[int] = set()
    while queue:
        source = queue.pop(0)
        if id(source) in seen:
            continue
        seen.add(id(source))
        sources.append(source)
        for key in ("identity_row", "authoritative", "source_row", "mapping_row", "authority"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                queue.append(nested)
    for source in sources:
        for key in marker_keys:
            marker = source.get(key)
            if marker is True or marker == 1:
                return True
            if isinstance(marker, str) and _normalise_provider_label(marker) in {
                "true",
                "yes",
                "on",
                "placeholder",
                "event_placeholder",
                "event_place_holder",
                "media_placeholder",
            }:
                return True
        for key in label_keys:
            if _normalise_provider_label(source.get(key)) in placeholder_labels:
                return True
        roles = source.get("roles")
        if isinstance(roles, (list, tuple, set, frozenset)) and any(
            _normalise_provider_label(item) in placeholder_labels for item in roles
        ):
            return True
    return False


def _record_has_positive_authority_role(value: Any) -> bool:
    """Return whether a row carries both authority and semantic role labels."""

    if not isinstance(value, Mapping):
        return False
    labels = _record_role_labels(value)
    return bool(labels.intersection(_PROVIDER_AUTHORITY_ROLES)) and bool(
        labels.intersection({"primary", "substantive", "mixed", "ellipsis"})
    )


def _record_is_provider_media(value: Any) -> bool:
    """Return whether a row is a typed media/system/placeholder row.

    A typed media row is not allowed to become provider primary merely because
    an upstream serializer populated ``content``/``description``.  Those
    fields are often XML, JSON, or transport metadata rather than human
    caption text.
    """

    if not isinstance(value, Mapping):
        return False
    message_type = _record_message_type(value)
    if message_type in _PROVIDER_MEDIA_MESSAGE_TYPES:
        return True
    if message_type in {"event", "event_message"} and any(
        value.get(key) is True or value.get(key) == 1
        for key in ("placeholder", "is_placeholder", "event_placeholder", "media_placeholder")
    ):
        return True
    for key in ("fragment_type", "kind", "event_type"):
        if _normalise_provider_label(value.get(key)) in {
            "placeholder",
            "event_placeholder",
            "event_place_holder",
            "media_placeholder",
        }:
            return True
    labels = _record_role_labels(value)
    # Authority rows are also gated by direct human text.  In particular,
    # ``description`` on an empty authority is metadata and must not become a
    # provider cue simply because it is non-empty.
    return bool(labels.intersection(_PROVIDER_PLACEHOLDER_ROLES | _PROVIDER_AUTHORITY_ROLES))


def _looks_serialized_or_markup_cue(value: str) -> bool:
    """Reject transport/XML/JSON wrappers as human semantic cues."""

    cue = " ".join(str(value or "").split())
    if not cue:
        return False
    if re.match(r"^<\s*(?:\?xml\b|!--|/?[A-Za-z][^>]*>)", cue, re.IGNORECASE):
        return True
    if cue[:1] in "[{" and cue[-1:] in "]}":
        try:
            parsed = json.loads(cue)
        except (TypeError, ValueError, json.JSONDecodeError):
            # A bracketed placeholder is handled by ``_MEDIA_CUE_RE`` below;
            # malformed serialized payloads remain non-semantic as well.
            return True
        return isinstance(parsed, (Mapping, list))
    return False


def _explicit_human_cue(value: Any) -> str:
    """Return a direct, non-placeholder human caption/text cue only.

    Nested ``content``/``raw``/``description`` values are intentionally not
    considered.  A caption is independent only when it is carried by one of
    the direct text fields below; this prevents metadata/XML/serialized
    content from laundering an empty authority or media placeholder into a
    primary unit.
    """

    if not isinstance(value, Mapping):
        return ""
    message_type = _record_message_type(value)
    # Reactions are transport/social events even when an upstream row carries
    # a stale positive role; they can never create an independent topic.
    if message_type == "reaction" or _record_role_labels(value).intersection({"reaction"}):
        return ""
    # ``text`` on a system/event row is commonly a serialized notification or
    # an adapter overlay.  A direct human caption marker is required for the
    # narrow event-caption exception; ordinary media/file text remains
    # eligible when paired with a positive source role for compatibility with
    # real substantive captions.
    direct_fields = _PROVIDER_DIRECT_HUMAN_CUE_FIELDS
    if message_type in {
        "system",
        "event",
        "event_message",
        "event_placeholder",
        "event_place_holder",
    }:
        direct_fields = ("caption", "text_redacted", "message_text")
        # The development context merge sets this body-free marker only after
        # matching a source-primary event fragment with an authoritative
        # substantive role.  It is the explicit exception for an overlaid
        # direct caption; an unmarked system/event ``text`` remains context.
        if value.get("_context_direct_caption") is True:
            direct_fields = _PROVIDER_DIRECT_HUMAN_CUE_FIELDS
    for key in direct_fields:
        candidate = value.get(key)
        if candidate is None:
            continue
        if not isinstance(candidate, str):
            _fail("message_cue_invalid")
        cue = " ".join(candidate.split())
        if not cue or _looks_serialized_or_markup_cue(cue):
            continue
        if _MEDIA_CUE_RE.fullmatch(cue):
            continue
        if not _semantic_cue_is_eligible(cue):
            continue
        return cue[:MAX_MESSAGE_CUE_CHARS]
    return ""


def _media_caption_source_role_is_valid(
    value: Mapping[str, Any],
    *,
    source_roles: Iterable[str] = (),
) -> bool:
    """Require a semantic source role before a media caption can be primary."""

    labels = _record_role_labels(value)
    positive = {"primary", "substantive", "mixed"}
    bound_roles = {str(item) for item in source_roles if item}
    if bound_roles:
        # A caption attached to an authority/context/candidate row is still
        # metadata unless that same row is explicitly bound to the source
        # primary set.  Overlap is handled conservatively by requiring the
        # positive source role; an explicit placeholder label may be replaced
        # by a real caption only when the source itself is primary.
        if "primary" not in bound_roles:
            return False
    # Authority/candidate/context labels are not semantic source roles.  A
    # direct caption can override a media type only when the row explicitly
    # carries a positive semantic role (or a caller has marked it primary).
    if labels.intersection(_PROVIDER_AUTHORITY_ROLES):
        return bool(labels.intersection(positive))
    if bound_roles and "primary" in bound_roles:
        return True
    return bool(labels.intersection(positive))


def _text_from_record(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    if _record_has_merged_candidate_cue(value):
        # Candidate overlays are often stored in ``text``/``content``.  Do
        # not expose that material as a provider cue.  An explicitly named
        # caption/text field that is independent of the overlay may still be
        # retained as a bounded hint; the role remains context above because
        # no authority binding is available at this protocol boundary.
        for key in ("caption", "text_redacted", "message_text"):
            candidate = value.get(key)
            if not isinstance(candidate, str):
                continue
            cue = " ".join(candidate.split())
            if cue and not _looks_serialized_or_markup_cue(cue) and not _MEDIA_CUE_RE.fullmatch(cue):
                return cue[:MAX_MESSAGE_CUE_CHARS]
        return ""
    # Typed media/placeholders may expose a real human caption only through a
    # direct caption/text field.  Never fall through to description/content,
    # which can contain XML or raw serialized transport data.
    if _record_is_provider_media(value):
        return _explicit_human_cue(value)
    # ``caption``/``alt_text`` are the only media-derived material that may
    # make a provider-facing row substantive.  The value is compacted once at
    # this boundary and is never retained by the artifact writer.
    for key in (
        "text",
        "text_redacted",
        "material",
        "message_text",
        "caption",
        "alt_text",
        "description",
        "body",
        "content",
    ):
        candidate = value.get(key)
        if candidate is not None:
            if not isinstance(candidate, str):
                _fail("message_cue_invalid")
            cue = " ".join(candidate.split())
            if not cue:
                continue
            # Never truncate a serialized/XML payload into a seemingly
            # ordinary lexical prefix before semantic eligibility is checked;
            # doing so would turn a non-semantic ``{"..."}``/``<...>`` row
            # into a provider primary.
            if _looks_serialized_or_markup_cue(cue):
                continue
            # The builder is the compaction boundary: truncate a caller's
            # local body-derived cue once, rather than rejecting the whole
            # packet.  Direct wire validation still rejects an over-limit x
            # field, so a manually forged oversized request cannot pass.
            return cue[:MAX_MESSAGE_CUE_CHARS]
    return ""


def _message_identity_for_dedup(value: Any) -> str:
    """Return the canonical message id/alias used for local cue de-duplication.

    Linear/K2 adapters may present the same message through a fragment handle
    and an immutable ``message_id`` (or through an ``identity_row`` wrapper).
    Prefer message ids/aliases across those wrappers before falling back to a
    message handle, so one authoritative message contributes one wire row.
    """

    if not isinstance(value, Mapping):
        return ""
    sources: List[Mapping[str, Any]] = [value]
    seen: set[int] = {id(value)}
    index = 0
    while index < len(sources):
        source = sources[index]
        index += 1
        for key in (
            "identity_row",
            "source_row",
            "mapping_row",
            "authoritative",
            "authority",
            "message_metadata",
        ):
            nested = source.get(key)
            if isinstance(nested, Mapping) and id(nested) not in seen:
                sources.append(nested)
                seen.add(id(nested))
    # Keep this order stable and avoid treating a fragment handle as a second
    # message when its wrapper exposes the immutable id/alias.
    for key in (
        "message_id",
        "source_message_id",
        "message_alias",
        "source_message_alias",
        "id",
    ):
        for source in sources:
            marker = source.get(key)
            if marker not in (None, "") and not isinstance(marker, (Mapping, list, tuple, set, frozenset)):
                result = str(marker)
                if result and result.casefold() != "unknown":
                    return result
    for key in ("message_ids", "source_message_ids", "message_aliases", "aliases"):
        for source in sources:
            markers = source.get(key)
            if isinstance(markers, str):
                markers = (markers,)
            if isinstance(markers, (list, tuple, set, frozenset)):
                for marker in markers:
                    if marker not in (None, ""):
                        result = str(marker)
                        if result and result.casefold() != "unknown":
                            return result
    for key in ("message_handle", "source_message_handle", "handle"):
        for source in sources:
            marker = source.get(key)
            if marker not in (None, "") and not isinstance(marker, (Mapping, list, tuple, set, frozenset)):
                return str(marker)
    return ""


_SUBSTANTIVE_FRAGMENT_TYPES = frozenset(
    {
        "question",
        "request",
        "answer",
        "statement",
        "event",
        "event_caption",
        "caption",
        "claim",
        "action",
        "state",
        "topic",
        "topic_shift",
        "topic_change",
        "continuation",
        "elaboration",
    }
)
_SUBSTANTIVE_ROLE_LABELS = frozenset({"substantive", "mixed"})
_SUBSTANTIVE_CUE_WORD_RE = re.compile(
    r"(?:[?？!！]|这个|那个|它|他|她|此|该|上述|前面|刚才|怎么|如何|是否|为什么|能否|请|帮|处理|上线|状态|问题|失败|恢复|注册|登录|登陆|接口|项目|内容|消息|action|state|object|person)",
    re.IGNORECASE,
)


def _record_has_substantive_signal(value: Mapping[str, Any], *, text: str = "") -> bool:
    """Return whether metadata/cue identifies a real semantic unit.

    Provider role projection is intentionally stricter than a non-empty
    check.  Explicit substantive/mixed labels, event/question fragment
    types, and a small set of typed cue markers can override a stale adjacent
    or context label.  A short question/ellipsis only qualifies when it has a
    lexical cue; punctuation-only turns remain context.
    """

    semantic = str(
        value.get("semantic_role", value.get("provider_role", value.get("primary_context_role", "")))
        or ""
    ).strip().casefold().replace("-", "_").replace(" ", "_")
    raw = str(value.get("role", value.get("layer", value.get("message_role", ""))) or "").strip().casefold().replace("-", "_").replace(" ", "_")
    role_values = {
        str(item or "").strip().casefold().replace("-", "_").replace(" ", "_")
        for item in value.get("roles", ())
    } if isinstance(value.get("roles"), (list, tuple, set, frozenset)) else set()
    if semantic in _SUBSTANTIVE_ROLE_LABELS or raw in _SUBSTANTIVE_ROLE_LABELS or role_values.intersection(_SUBSTANTIVE_ROLE_LABELS):
        return True
    if semantic in _SUBSTANTIVE_FRAGMENT_TYPES or raw in _SUBSTANTIVE_FRAGMENT_TYPES or role_values.intersection(_SUBSTANTIVE_FRAGMENT_TYPES):
        return True
    fragment_type = str(value.get("fragment_type", value.get("kind", "")) or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if fragment_type in _SUBSTANTIVE_FRAGMENT_TYPES:
        return True
    for key in ("intent", "unit_kind", "semantic_kind", "cue_type", "fragment_role", "event_role", "event_type"):
        label = str(value.get(key, "") or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if label in _SUBSTANTIVE_FRAGMENT_TYPES or label in {"question", "pronoun", "object", "action", "state", "substantive", "mixed", "event"}:
            return True
    for key in ("question", "is_question", "has_question", "pronoun", "object", "action", "state", "event", "is_event", "semantic_cue"):
        marker = value.get(key)
        if marker is True or marker == 1:
            return True
        label = str(marker or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if label in {"question", "pronoun", "object", "action", "state", "substantive", "mixed", "event", "event_caption", "caption"}:
            return True
    cue = " ".join(str(text or "").split())
    if not cue or not _semantic_cue_is_eligible(cue):
        return False
    return bool(has_context_prefix(cue) or _SUBSTANTIVE_CUE_WORD_RE.search(cue))


def _role_from_record(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "p"
    # A merged candidate/activation cue is never an independent semantic
    # message.  The development pilot may keep such a cue as auxiliary
    # evidence for an already-bound message, but this protocol-only builder
    # has no binding table and therefore fails closed to context.
    if _record_has_merged_candidate_cue(value):
        return "c"
    # Explicit placeholder provenance is a hard context barrier regardless of
    # whether the transport row also carries a direct-looking caption.
    if _record_is_hard_placeholder(value):
        return "c"
    text = _text_from_record(value)
    message_type = value.get("message_type", value.get("type", "text"))
    raw_semantic = value.get(
        "semantic_role",
        value.get("provider_role", value.get("primary_context_role")),
    )
    semantic = str(raw_semantic or "").strip().casefold()
    raw = str(value.get("role", value.get("layer", value.get("message_role", ""))) or "").strip().casefold()
    fragment_type = str(value.get("fragment_type") or value.get("kind") or "").strip().casefold()
    role_values = {
        str(item or "").strip().casefold().replace("-", "_").replace(" ", "_")
        for item in value.get("roles", ())
    } if isinstance(value.get("roles"), (list, tuple, set, frozenset)) else set()
    eligible = _semantic_cue_is_eligible(text)
    promoting_signal = _record_has_substantive_signal(value, text=text)

    # Media/system/placeholder rows are context by default.  Only an
    # independent direct human caption/text cue, paired with a positive
    # semantic source role, can cross this gate.  In particular, a non-empty
    # XML/JSON payload, placeholder label, reaction text, or metadata
    # description must not manufacture a provider primary.
    if _record_is_provider_media(value):
        # An explicit placeholder is stronger than the direct-caption
        # compatibility exception.  Source-primary membership is provenance,
        # not permission to promote a transport placeholder into a topic.
        if _record_is_hard_placeholder(value):
            return "c"
        direct_cue = _explicit_human_cue(value)
        caption_role_valid = _media_caption_source_role_is_valid(value)
        # Event/system captions are an intentionally narrow exception: a
        # direct human caption needs explicit positive authority provenance.
        # A bare source-primary bit is not enough to promote a transport
        # notification; the development overlay adds the same provenance
        # marker before it reaches this boundary.
        if message_type in {"system", "event", "event_message"}:
            caption_role_valid = caption_role_valid and _record_has_positive_authority_role(value)
        if not direct_cue or not caption_role_valid:
            return "c"
        text = direct_cue
        eligible = _semantic_cue_is_eligible(text)
        promoting_signal = True

    # A pure social/media row is never a provider primary, even when an
    # upstream layer supplied a stale positive role.  Conversely, a mixed
    # turn must survive a stale ``context_only``/``acknowledgement`` label
    # when its bounded cue or semantic fragment metadata carries a real
    # question/object/action/state signal.
    if not eligible:
        return "c"
    if semantic in {"substantive", "mixed"} or raw in {"substantive", "mixed"} or role_values.intersection(_SUBSTANTIVE_ROLE_LABELS):
        return "p"
    if fragment_type in _SUBSTANTIVE_FRAGMENT_TYPES or promoting_signal:
        return "p"
    if semantic in {
        "authority",
        "authority_only",
        "context",
        "context_only",
        "adjacent",
        "secondary",
        "media",
        "greeting",
        "ack",
        "acknowledgement",
        "reaction",
    }:
        return "c"
    if any(
        value.get(key) is True
        for key in ("authority", "authority_only", "is_authority", "context_only", "is_context_only")
    ):
        return "c"
    if raw in {
        "authority",
        "authority_only",
        "context",
        "context_only",
        "adjacent",
        "secondary",
        "unknown_context",
        "unknown",
        "media",
        "greeting",
        "ack",
        "acknowledgement",
        "reaction",
    }:
        return "c"
    if role_values & {
        "authority",
        "authority_only",
        "context",
        "context_only",
        "adjacent",
        "secondary",
        "unknown_context",
        "unknown",
        "media",
        "greeting",
        "ack",
        "acknowledgement",
        "reaction",
    }:
        return "c"
    if semantic == "primary" or raw == "primary":
        return "p"
    if is_context_only_text(text, message_type=message_type):
        return "c"
    if text and has_context_prefix(text):
        # A social prefix followed by substantive material is explicitly a
        # mixed turn and remains provider-facing primary.
        return "p"
    return "p"


_MEDIA_CUE_RE = re.compile(
    r"^(?:\[[^\]]{1,120}\]|<[^>]{1,120}>|"
    r"(?:image|photo|picture|video|audio|voice|file|sticker|emoji|media)"
    r"(?:[\s_-]+(?:only|placeholder))?|"
    r"(?:card|system|event)(?:[\s_-]+(?:only|placeholder))?|"
    r"图片|照片|视频|音频|文件|卡片|系统|事件|贴图|表情|媒体)$",
    re.IGNORECASE,
)


def _semantic_cue_is_eligible(value: Any) -> bool:
    """Return whether a compact cue can support a provider primary alias.

    The protocol intentionally has no message-type field.  A non-empty cue is
    therefore the provider-visible evidence of substantive/mixed content;
    exact social turns and stand-alone media placeholders are rejected here.
    Captions are already normalised into the same bounded ``x`` cue by the
    request builder.
    """

    if type(value) is not str:
        return False
    cue = " ".join(value.split())
    if not cue or is_context_only_text(cue):
        return False
    if _MEDIA_CUE_RE.fullmatch(cue):
        return False
    if _looks_serialized_or_markup_cue(cue):
        return False
    # Punctuation-only turns (including a bare ellipsis or question mark)
    # have no semantic unit for Stage A to group.  A short question such as
    # ``它现在？`` still passes because it contains a letter/number/CJK cue.
    if not re.search(r"[A-Za-z0-9_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", cue):
        return False
    return True


# The preflight hint extractor deliberately reads only narrow metadata fields.
# It never searches message bodies, descriptions, arbitrary nested mappings or
# candidate payloads.  Its output is a bounded alias/relation list consumed as
# a structural hint; the model and the response validator remain responsible
# for the topic decision.
_HINT_UNKNOWN_VALUES = frozenset(
    {"", "unknown", "uncertain", "unresolved", "missing", "none", "null", "n/a", "na"}
)
_HINT_WRAPPER_KEYS = (
    "identity_row",
    "authoritative",
    "authority",
    "source_row",
    "mapping_row",
    "message_metadata",
    "metadata",
)
_HINT_ID_KEYS = (
    "handle",
    "message_handle",
    "candidate_handle",
    "message_id",
    "source_message_id",
    "message_alias",
    "source_message_alias",
    "candidate_id",
    "candidate_alias",
    "id",
    "alias",
    "h",
    "i",
)
_HINT_SUBJECT_KEYS = (
    "subject_id",
    "subject_ref_id",
    "subject_ref",
    "subject",
    "subject_entity_id",
)
_HINT_OBJECT_KEYS = (
    "object_id",
    "object_ref_id",
    "object_ref",
    "object",
    "target_id",
    "target_ref_id",
)
_HINT_ACTION_KEYS = (
    "action",
    "action_id",
    "action_ref",
    "actions",
    "actions_candidate",
    "action_candidate",
    "intent",
)
_HINT_STATE_KEYS = (
    "state",
    "state_id",
    "state_candidate",
    "state_ref",
    "status",
)
_HINT_REPLY_KEYS = (
    "reply_to_message_id",
    "reply_to",
    "in_reply_to",
    "reply_target",
    "reply_of",
    "answer_to",
    "question_id",
)
_HINT_LINK_KEYS = (
    "left_message_id",
    "right_message_id",
    "source_message_ids",
    "message_ids",
    "member_message_ids",
    "primary_message_ids",
    "context_message_ids",
    "related_message_ids",
    "continuation_refs",
    "continuation_ref",
)
_HINT_RELATION_KEYS = (
    "relation",
    "relation_label",
    "relation_subtype",
    "relation_type",
    "semantic_relation",
    "continuation",
    "qa_relation",
    "question_answer",
    "same_topic",
    "same_subject",
    "same_object",
    "same_action",
    "state_change",
    "object_inheritance",
)
_HINT_BOUNDARY_KEYS = (
    "topic_shift",
    "topic_change",
    "new_topic",
    "is_opener",
    "is_new_topic",
)
_HINT_RELATION_LABELS = frozenset(
    {
        "qa",
        "question_answer",
        "question_follow_up",
        "question_followup",
        "answer",
        "answers",
        "reply",
        "replied",
        "continuation",
        "continues",
        "elaborates",
        "state_change",
        "state_transition",
        "same_topic",
        "same_subject",
        "same_object",
        "same_action",
        "shared_subject",
        "shared_object",
        "shared_action",
        "object_inheritance",
    }
)
_HINT_BOUNDARY_LABELS = frozenset(
    {
        "topic_shift",
        "topic_change",
        "new_topic",
        "is_opener",
        "opener",
        "conversation_opener",
        "greeting_new_topic",
    }
)
_HINT_QUESTION_LABELS = frozenset({"question", "request", "question_follow_up", "question_followup"})
_HINT_ANSWER_LABELS = frozenset({"answer", "answers", "statement", "response", "reply"})


def _hint_sources(value: Any) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    sources: List[Mapping[str, Any]] = [value]
    seen: set[int] = {id(value)}
    index = 0
    while index < len(sources):
        source = sources[index]
        index += 1
        for key in _HINT_WRAPPER_KEYS:
            child = source.get(key)
            if isinstance(child, Mapping) and id(child) not in seen:
                sources.append(child)
                seen.add(id(child))
    return tuple(sources)


def _hint_scalar_values(value: Any) -> Tuple[str, ...]:
    """Read bounded scalar metadata without copying the value to a wire."""

    values: List[str] = []

    def add(item: Any) -> None:
        if isinstance(item, bool) or item is None:
            return
        if isinstance(item, (int, float)):
            text = str(item)
        elif isinstance(item, str):
            text = " ".join(item.split())
        else:
            return
        if not text or text.casefold() in _HINT_UNKNOWN_VALUES or len(text) > 256:
            return
        if text not in values:
            values.append(text)

    if isinstance(value, Mapping):
        # Semantic ids/labels are intentionally narrow; arbitrary mapping
        # values may contain message bodies and are never traversed.
        for key in ("id", "ref", "ref_id", "value", "code", "label", "name"):
            if key in value:
                child = value.get(key)
                if isinstance(child, (list, tuple, set, frozenset)):
                    for item in child:
                        add(item)
                else:
                    add(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, Mapping):
                values.extend(item_value for item_value in _hint_scalar_values(item) if item_value not in values)
            else:
                add(item)
    else:
        add(value)
    return tuple(values)


def _hint_field_values(value: Any, keys: Iterable[str]) -> frozenset[str]:
    values: set[str] = set()
    wanted = tuple(str(key) for key in keys)
    for source in _hint_sources(value):
        for key in wanted:
            if key not in source:
                continue
            values.update(_hint_scalar_values(source.get(key)))
    return frozenset(values)


def _hint_raw_labels(value: Any, keys: Iterable[str]) -> frozenset[str]:
    labels: set[str] = set()
    for item in _hint_field_values(value, keys):
        label = item.strip().casefold().replace("-", "_").replace(" ", "_")
        if label:
            labels.add(label)
    return frozenset(labels)


def _hint_truthy(value: Any, keys: Iterable[str]) -> bool:
    for source in _hint_sources(value):
        for key in keys:
            marker = source.get(key)
            if marker is True or marker == 1:
                return True
            if isinstance(marker, str) and marker.strip().casefold() in {"true", "yes", "on"}:
                return True
    return False


def _hint_record_handle(value: Any, kind: str, index: int) -> str:
    if isinstance(value, Mapping):
        for source in _hint_sources(value):
            for key in _HINT_ID_KEYS:
                marker = source.get(key)
                if marker in (None, "") or isinstance(marker, (Mapping, list, tuple, set, frozenset)):
                    continue
                text = str(marker).strip()
                if text and text.casefold() != "unknown":
                    return text
    elif isinstance(value, str) and value.strip():
        return value.strip()
    return "%s%d" % ("c" if kind == "c" else "m", index)


def _hint_role(value: Any, kind: str) -> str:
    if kind == "c":
        return "candidate"
    if isinstance(value, Mapping):
        direct = value.get("r")
        if direct in {"p", "primary", "substantive", "mixed"}:
            return "p"
        if direct in {"c", "context", "context_only", "adjacent", "authority", "reaction", "greeting"}:
            return "c"
    labels = _hint_raw_labels(
        value,
        ("semantic_role", "provider_role", "primary_context_role", "role", "layer", "message_role", "fragment_type", "kind", "intent"),
    )
    if labels.intersection({"primary", "substantive", "mixed", "p", "question", "request", "answer", "statement", "action", "state"}):
        return "p"
    return "c"


def _hint_refs(value: Any, keys: Iterable[str]) -> frozenset[str]:
    refs: set[str] = set()
    for source in _hint_sources(value):
        for key in keys:
            if key not in source:
                continue
            child = source.get(key)
            if isinstance(child, Mapping):
                refs.update(_hint_scalar_values(child))
            elif isinstance(child, (list, tuple, set, frozenset)):
                for item in child:
                    if isinstance(item, Mapping):
                        refs.update(_hint_scalar_values(item))
                    else:
                        refs.update(_hint_scalar_values(item))
            else:
                refs.update(_hint_scalar_values(child))
    return frozenset(refs)


def _hint_descriptor(value: Any, alias: str, kind: str) -> Dict[str, Any]:
    refs = set(_hint_refs(value, _HINT_ID_KEYS))
    refs.add(alias)
    if isinstance(value, Mapping):
        for key in ("handle", "message_handle", "candidate_handle", "h", "i"):
            refs.update(_hint_scalar_values(value.get(key)))
    fragment_types = _hint_raw_labels(value, ("fragment_type", "kind", "intent", "unit_kind", "semantic_kind"))
    boundary_labels = _hint_raw_labels(value, _HINT_BOUNDARY_KEYS)
    return {
        "alias": alias,
        "kind": kind,
        "role": _hint_role(value, kind),
        "refs": frozenset(refs),
        "subject": _hint_field_values(value, _HINT_SUBJECT_KEYS),
        "object": _hint_field_values(value, _HINT_OBJECT_KEYS),
        "action": _hint_field_values(value, _HINT_ACTION_KEYS),
        "state": _hint_field_values(value, _HINT_STATE_KEYS),
        "reply_refs": _hint_refs(value, _HINT_REPLY_KEYS),
        "link_refs": _hint_refs(value, _HINT_LINK_KEYS),
        "relations": _hint_raw_labels(value, _HINT_RELATION_KEYS),
        "fragment_types": fragment_types,
        "boundary": _hint_truthy(value, _HINT_BOUNDARY_KEYS)
        or bool(boundary_labels.intersection(_HINT_BOUNDARY_LABELS))
        or bool(fragment_types.intersection(_HINT_BOUNDARY_LABELS)),
    }


def _hint_input_rows(value: Any, kind: str) -> Tuple[Any, ...]:
    if isinstance(value, Mapping):
        if kind == "m" and isinstance(value.get("h"), list):
            return tuple(row for row in value["h"] if isinstance(row, Mapping) and row.get("k") == "m")
        key = "messages" if kind == "m" else "candidates"
        child = value.get(key)
        if isinstance(child, Sequence) and not isinstance(child, (str, bytes)):
            return tuple(child)
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    return ()


def _hint_alias_descriptors(value: Any, kind: str) -> Tuple[Dict[str, Any], ...]:
    descriptors: List[Dict[str, Any]] = []
    used: set[str] = set()
    for index, item in enumerate(_hint_input_rows(value, kind), 1):
        if isinstance(item, Mapping) and isinstance(item.get("i"), str) and _ALIAS_RE.fullmatch(item["i"]):
            alias = str(item["i"])
        else:
            alias = _hint_record_handle(item, kind, index)
            prefix = "c" if kind == "c" else "m"
            if not alias.startswith(prefix) or not _ALIAS_RE.fullmatch(alias):
                alias = "%s%d" % (prefix, index)
        if alias in used:
            # Duplicate source identities are not a semantic hint.  The
            # request builder performs authoritative de-duplication; here we
            # simply skip a duplicate alias rather than inventing a merge.
            continue
        used.add(alias)
        descriptors.append(_hint_descriptor(item, alias, kind))
    return tuple(descriptors)


def _hint_alias_index(descriptors: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    index: Dict[str, str] = {}
    for descriptor in descriptors:
        alias = str(descriptor.get("alias", ""))
        if not alias:
            continue
        index[alias] = alias
        for ref in descriptor.get("refs", ()):
            text = str(ref)
            if text and text not in index:
                index[text] = alias
    return index


def _hint_resolve_ref(value: Any, aliases: Mapping[str, str]) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text or text.casefold() in _HINT_UNKNOWN_VALUES:
        return ""
    return str(aliases.get(text, ""))


def _hint_pair_has_reply(left: Mapping[str, Any], right: Mapping[str, Any], aliases: Mapping[str, str]) -> bool:
    left_alias = str(left.get("alias", ""))
    right_alias = str(right.get("alias", ""))
    left_replies = {_hint_resolve_ref(item, aliases) for item in left.get("reply_refs", ())}
    right_replies = {_hint_resolve_ref(item, aliases) for item in right.get("reply_refs", ())}
    left_links = {_hint_resolve_ref(item, aliases) for item in left.get("link_refs", ())}
    right_links = {_hint_resolve_ref(item, aliases) for item in right.get("link_refs", ())}
    return bool(
        right_alias in left_replies
        or left_alias in right_replies
        or ({left_alias, right_alias} <= left_links)
        or ({left_alias, right_alias} <= right_links)
    )


def _hint_pair_relations(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    aliases: Mapping[str, str],
    *,
    max_relations: Optional[int] = TOPIC_HINT_MAX_RELATIONS,
) -> Tuple[str, ...]:
    if left.get("boundary") or right.get("boundary"):
        return ()
    reasons: List[str] = []
    for field, relation in (
        ("subject", "same_subject"),
        ("object", "same_object"),
        ("action", "same_action"),
    ):
        left_values = set(left.get(field, ()))
        right_values = set(right.get(field, ()))
        if left_values and right_values and left_values.intersection(right_values):
            reasons.append(relation)
    left_state = set(left.get("state", ()))
    right_state = set(right.get("state", ()))
    relation_labels = set(left.get("relations", ())) | set(right.get("relations", ()))
    if (
        left_state
        and right_state
        and (left_state != {"unknown"} or right_state != {"unknown"})
        and (
            set(left.get("subject", ())).intersection(right.get("subject", ()))
            or set(left.get("object", ())).intersection(right.get("object", ()))
            or set(left.get("action", ())).intersection(right.get("action", ()))
            or "state_change" in relation_labels
            or "state_transition" in relation_labels
        )
    ):
        reasons.append("state_update")
    if _hint_pair_has_reply(left, right, aliases):
        reasons.append("reply_continuity")
    elif relation_labels.intersection({"qa", "question_answer", "question_follow_up", "question_followup", "answer", "answers", "reply", "continuation", "continues", "elaborates"}):
        left_types = set(left.get("fragment_types", ()))
        right_types = set(right.get("fragment_types", ()))
        if (left_types & (_HINT_QUESTION_LABELS | _HINT_ANSWER_LABELS)) or (right_types & (_HINT_QUESTION_LABELS | _HINT_ANSWER_LABELS)):
            reasons.append("qa_continuity")
    # Keep the canonical order stable.  The preflight can request the complete
    # body-free relation set and split it into compact entries before applying
    # its global scarcity/alias caps; callers that use this helper directly
    # retain the historical three-relation default.
    order = {name: index for index, name in enumerate(TOPIC_HINT_RELATION_ORDER)}
    canonical = tuple(sorted(dict.fromkeys(reasons), key=lambda name: order.get(name, 999)))
    if max_relations is None:
        return canonical
    if type(max_relations) is not int or max_relations < 0:
        return canonical[:TOPIC_HINT_MAX_RELATIONS]
    return canonical[:max_relations]


def _hint_relation_groups(relations: Sequence[str]) -> Tuple[Tuple[str, ...], ...]:
    """Split one pair's relation families into compact, valid hint entries.

    A single pair can share subject/object/action *and* continue a state or
    QA exchange.  Truncating that list to the first three families silently
    erased the latter signals.  Keep the three compact slot families together
    and emit at most one additional continuity group; the outer selector then
    applies its per-alias and per-family limits.  This preserves a
    representative for each available family without copying any body text.
    """

    order = {name: index for index, name in enumerate(TOPIC_HINT_RELATION_ORDER)}
    canonical = tuple(
        sorted(dict.fromkeys(str(item) for item in relations if item), key=lambda name: order.get(name, 999))
    )
    if not canonical:
        return ()
    if len(canonical) <= TOPIC_HINT_MAX_RELATIONS:
        return (canonical,)
    slots = tuple(item for item in canonical if item in {"same_subject", "same_object", "same_action"})
    continuity = tuple(item for item in canonical if item not in {"same_subject", "same_object", "same_action"})
    groups: List[Tuple[str, ...]] = []
    if slots:
        groups.append(slots[:TOPIC_HINT_MAX_RELATIONS])
    for index in range(0, len(continuity), TOPIC_HINT_MAX_RELATIONS):
        group = continuity[index : index + TOPIC_HINT_MAX_RELATIONS]
        if group:
            groups.append(tuple(group))
    if not groups:
        for index in range(0, len(canonical), TOPIC_HINT_MAX_RELATIONS):
            groups.append(tuple(canonical[index : index + TOPIC_HINT_MAX_RELATIONS]))
    return tuple(groups)


def build_topic_candidate_hints(
    messages: Any,
    candidates: Any = (),
    *,
    max_hints: int = MAX_TOPIC_GROUPING_HINTS,
) -> List[Dict[str, str]]:
    """Build deterministic, body-free topic grouping hints for Stage A.

    The result is only a list of alias pairs and fixed relation labels.  It is
    intentionally not a topic assignment and does not inspect candidate or
    message bodies.  Empty/unknown metadata produces no hint; a model must
    still decide whether related primary rows share a topic.
    """

    if type(max_hints) is not int or max_hints < 0 or max_hints > MAX_TOPIC_GROUPING_HINTS:
        _fail("request_grouping_hints")
    message_rows = _hint_alias_descriptors(messages, "m")
    candidate_rows = _hint_alias_descriptors(candidates, "c")
    all_rows: Tuple[Mapping[str, Any], ...] = tuple(message_rows) + tuple(candidate_rows)
    aliases = _hint_alias_index(all_rows)
    # Keep the explicit 24-entry validator envelope for callers that already
    # have a packet, but make this auto-generated preflight intentionally
    # smaller.  The request table and original message/content cues remain
    # untouched; only redundant structural hints are budgeted here.
    hint_limit = min(max_hints, MAX_GENERATED_TOPIC_GROUPING_HINTS)
    if hint_limit == 0:
        return []
    raw_hints: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, str]] = set()

    def add(left: Any, right: Any, relation: str) -> None:
        a = str(left or "")
        b = str(right or "")
        if not a or not b or a == b:
            return
        components = [item for item in str(relation).split("+") if item]
        order = {name: index for index, name in enumerate(TOPIC_HINT_RELATION_ORDER)}
        if not components or any(item not in TOPIC_HINT_RELATIONS for item in components):
            return
        components = list(dict.fromkeys(components))[:TOPIC_HINT_MAX_RELATIONS]
        components.sort(key=lambda item: order[item])
        canonical_relation = "+".join(components)
        key = (a, b, canonical_relation)
        if key in seen:
            return
        seen.add(key)
        raw_hints.append({"a": a, "b": b, "r": canonical_relation})

    # Only primary message pairs can suggest sharing one topic.  A context
    # row remains context even when it has the same metadata as a primary.
    for index, left in enumerate(message_rows):
        if left.get("role") != "p":
            continue
        for right in message_rows[index + 1 :]:
            if right.get("role") != "p":
                continue
            relations = _hint_pair_relations(left, right, aliases, max_relations=None)
            for relation_group in _hint_relation_groups(relations):
                add(left.get("alias"), right.get("alias"), "+".join(relation_group))

    # Candidate references are auxiliary context hints only.  They never
    # become primary/context response ids and never create a topic.
    for candidate in candidate_rows:
        relation_labels = set(candidate.get("relations", ()))
        relation = "candidate_qa" if relation_labels.intersection({"qa", "question_answer", "answer", "answers", "reply", "continuation", "continues"}) else "candidate_context"
        refs = set(candidate.get("link_refs", ())) | set(candidate.get("reply_refs", ()))
        for ref in refs:
            message_alias = _hint_resolve_ref(ref, aliases)
            if message_alias and message_alias.startswith("m"):
                add(message_alias, candidate.get("alias"), relation)

    if not raw_hints:
        return []

    relation_order = {name: index for index, name in enumerate(TOPIC_HINT_RELATION_ORDER)}

    def components(item: Mapping[str, str]) -> Tuple[str, ...]:
        return tuple(str(item.get("r", "")).split("+"))

    # Count before selecting so rare families (typically one QA/state edge)
    # are considered before a dense same-subject clique.  Exact triplets were
    # already de-duplicated above; this pass only chooses a sparse witness.
    family_counts: Dict[str, int] = {name: 0 for name in TOPIC_HINT_RELATION_ORDER}
    for item in raw_hints:
        for relation in components(item):
            if relation in family_counts:
                family_counts[relation] += 1

    def alias_key(alias: Any) -> Tuple[int, int, str]:
        text = str(alias or "")
        prefix = 0 if text.startswith("m") else 1
        try:
            number = int(text[1:])
        except (TypeError, ValueError):
            number = 9999
        return prefix, number, text

    def item_key(item: Mapping[str, str]) -> Tuple[Any, ...]:
        families = components(item)
        scarcity = min((family_counts.get(name, 9999) for name in families), default=9999)
        first_family = min((relation_order.get(name, 9999) for name in families), default=9999)
        return (
            scarcity,
            first_family,
            len(families),
            alias_key(item.get("a")),
            alias_key(item.get("b")),
            str(item.get("r", "")),
        )

    ordered_hints = sorted(raw_hints, key=item_key)
    ordered_families = sorted(
        (name for name in TOPIC_HINT_RELATION_ORDER if family_counts.get(name, 0)),
        key=lambda name: (family_counts[name], relation_order[name]),
    )
    selected: List[Dict[str, str]] = []
    selected_keys: set[Tuple[str, str, str]] = set()
    selected_family_counts: Dict[str, int] = {name: 0 for name in TOPIC_HINT_RELATION_ORDER}
    selected_alias_counts: Dict[str, int] = {}

    def select(item: Mapping[str, str], *, ignore_alias_limit: bool = False) -> bool:
        if len(selected) >= hint_limit:
            return False
        key = (str(item.get("a", "")), str(item.get("b", "")), str(item.get("r", "")))
        if key in selected_keys:
            return False
        families = components(item)
        if any(
            selected_family_counts.get(name, 0) >= MAX_TOPIC_HINTS_PER_RELATION
            for name in families
        ):
            return False
        if not ignore_alias_limit and any(
            selected_alias_counts.get(alias, 0) >= MAX_TOPIC_HINTS_PER_ALIAS
            for alias in (str(item.get("a", "")), str(item.get("b", "")))
        ):
            return False
        chosen = {"a": key[0], "b": key[1], "r": key[2]}
        selected.append(chosen)
        selected_keys.add(key)
        for name in families:
            selected_family_counts[name] = selected_family_counts.get(name, 0) + 1
        for alias in (key[0], key[1]):
            selected_alias_counts[alias] = selected_alias_counts.get(alias, 0) + 1
        return True

    # First guarantee one deterministic witness for every available relation
    # family.  Keep the canonical family order here so a later HTTP-budget
    # trim retains the primary subject/object/action representatives before
    # optional candidate edges.  Within each family prefer a relation-rich
    # combined entry, then use the rarity/alias order above as a tie-breaker.
    family_priority = {
        name: index for index, name in enumerate(TOPIC_HINT_RELATION_ORDER)
    }
    ordered_families = sorted(ordered_families, key=lambda name: family_priority[name])
    for family in ordered_families:
        if len(selected) >= hint_limit:
            break
        family_items = sorted(
            (item for item in ordered_hints if family in components(item)),
            key=lambda item: (-len(components(item)), item_key(item)),
        )
        if any(select(item) for item in family_items):
            continue
        # Alias sparsity is a guard against a dense clique, not a reason to
        # erase a relation family altogether.  If every family witness shares
        # a capped alias, retain the rare family once while still respecting
        # the per-family limit.
        for item in family_items:
            if select(item, ignore_alias_limit=True):
                break

    for item in ordered_hints:
        if len(selected) >= hint_limit:
            break
        select(item)
    return selected


deterministic_topic_candidate_hints = build_topic_candidate_hints
topic_candidate_hints = build_topic_candidate_hints


def build_candidate_connected_components(
    request: Mapping[str, Any],
    hints: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Derive structural primary groups and a conservative merge prior.

    This helper is intentionally candidate-only: it exposes connected
    components as evidence for the provider, but never emits topic ids or a
    local final assignment.  Edges are merged by default.  A ``topic_shift``
    relation is the sole explicit split witness accepted by this compact
    surface; the provider remains responsible for the final decision.
    """

    packet = validate_compact_stage_a_request(request)
    primary_aliases = [
        str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "p"
    ]
    primary_set = set(primary_aliases)
    raw_hints = list(packet.get(GROUPING_HINTS_FIELD, ())) if hints is None else list(hints)
    edges: List[Tuple[str, str, Tuple[str, ...]]] = []
    split_families: set[str] = set()
    for item in raw_hints:
        if not isinstance(item, Mapping):
            continue
        left = item.get("a")
        right = item.get("b")
        if type(left) is not str or type(right) is not str:
            continue
        relations = tuple(part for part in str(item.get("r", "")).split("+") if part)
        if left not in primary_set or right not in primary_set:
            continue
        edges.append((left, right, relations))
        split_families.update(part for part in relations if part in TOPIC_SPLIT_RELATIONS)

    parent: Dict[str, str] = {alias: alias for alias in primary_aliases}

    def find(alias: str) -> str:
        root = alias
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(alias, alias) != alias:
            previous = parent[alias]
            parent[alias] = root
            alias = previous
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            # Preserve input order for stable, body-free diagnostics.
            if primary_aliases.index(left_root) <= primary_aliases.index(right_root):
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

    split_count = 0
    for left, right, relations in edges:
        if any(item in TOPIC_SPLIT_RELATIONS for item in relations):
            split_count += 1
            continue
        union(left, right)

    groups: Dict[str, List[str]] = {}
    for alias in primary_aliases:
        groups.setdefault(find(alias), []).append(alias)
    components = [
        {
            "primary_message_ids": list(values),
            "primary_count": len(values),
            "merge_prior": "merge",
            "candidate_only": True,
        }
        for values in groups.values()
    ]
    return {
        "version": "stage_a_candidate_connected_components_v1",
        "components": components,
        "merge_prior": "connected_components_merge_by_default",
        "split_evidence_count": split_count,
        "split_evidence_families": sorted(split_families),
        "candidate_only": True,
        "model_decision_required": True,
        "final_topic_assignment": False,
        "under_uncertainty": "prefer_merge",
        "no_one_message_per_topic": True,
        "body_free": True,
    }


candidate_connected_components = build_candidate_connected_components
derive_candidate_connected_components = build_candidate_connected_components
build_topic_merge_prior = build_candidate_connected_components


def _validate_grouping_hints(
    value: Any,
    aliases: set[str],
    message_aliases: set[str],
    candidate_aliases: set[str],
) -> List[Dict[str, str]]:
    if type(value) is not list or len(value) > MAX_TOPIC_GROUPING_HINTS:
        _fail("request_grouping_hints")
    result: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, str]] = set()
    relation_order = {name: index for index, name in enumerate(TOPIC_HINT_RELATION_ORDER)}
    for raw in value:
        hint = dict(_mapping(raw, "request_grouping_hint_shape"))
        _exact_keys(hint, TOPIC_HINT_KEYS, "request_grouping_hint_keys")
        a = _safe_alias(hint.get("a"), "request_grouping_hint_alias")
        b = _safe_alias(hint.get("b"), "request_grouping_hint_alias")
        if a not in aliases or b not in aliases:
            _fail("request_grouping_hint_alias")
        if a == b:
            _fail("request_grouping_hint_pair")
        relation = _safe_string(hint.get("r"), "request_grouping_hint_relation", max_length=96)
        components = relation.split("+")
        if (
            not components
            or len(components) > TOPIC_HINT_MAX_RELATIONS
            or any(item not in TOPIC_HINT_RELATIONS for item in components)
            or len(set(components)) != len(components)
            or components != sorted(components, key=lambda item: relation_order[item])
        ):
            _fail("request_grouping_hint_relation")
        candidate_relation = any(item.startswith("candidate_") for item in components)
        if candidate_relation:
            if relation not in {"candidate_context", "candidate_qa"} or not ((a in message_aliases and b in candidate_aliases) or (b in message_aliases and a in candidate_aliases)):
                _fail("request_grouping_hint_candidate")
        elif a not in message_aliases or b not in message_aliases:
            _fail("request_grouping_hint_pair")
        key = (a, b, relation)
        if key in seen:
            _fail("request_grouping_hint_duplicate")
        seen.add(key)
        result.append({"a": a, "b": b, "r": relation})
    return result


def _handle_from_record(value: Any, code: str) -> str:
    if isinstance(value, str):
        return _safe_handle(value, code)
    row = _mapping(value, code + "_shape")
    for key in (
        "handle",
        "message_handle",
        "candidate_handle",
        "message_id",
        "candidate_id",
        "id",
    ):
        if row.get(key) not in (None, ""):
            return _safe_handle(row[key], code)
    _fail(code)


def _normalise_scope_wire(scope: Any) -> Dict[str, str]:
    account, chat = _scope_pair(scope)
    return {"a": account, "c": chat}


def _unpack_source_packet(
    scope: Optional[Mapping[str, Any]],
    messages: Optional[Sequence[Any]],
    candidates: Sequence[Any],
    packet: Optional[Mapping[str, Any]],
    context_packet: Optional[Mapping[str, Any]],
) -> Tuple[Any, Any, Any]:
    source_packet = packet if packet is not None else context_packet
    if source_packet is None and isinstance(scope, Mapping) and messages is None and "messages" in scope:
        source_packet = scope
    if source_packet is None:
        return scope, messages, candidates
    source = _mapping(source_packet, "packet_shape")
    if scope is None or (isinstance(scope, Mapping) and "messages" in scope):
        scope = source.get("scope")
    if messages is None:
        messages = source.get("messages")
    if candidates == ():
        candidates = source.get("candidates", ())
    return scope, messages, candidates


def build_compact_stage_a_request(
    scope: Optional[Mapping[str, Any]] = None,
    messages: Optional[Sequence[Any]] = None,
    candidates: Sequence[Any] = (),
    *,
    packet: Optional[Mapping[str, Any]] = None,
    context_packet: Optional[Mapping[str, Any]] = None,
    max_input_token_proxy: int = MAX_INPUT_TOKEN_PROXY,
    uncertainty_ceiling: Optional[str] = None,
    context_unique: str = CONTEXT_UNIQUENESS_MODE,
    topic_hints: Optional[Any] = None,
    grouping_hints: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build one validated request-local handle table.

    Message cues are bounded provider input hints only.  Candidate bodies,
    speakers, people, objects and all other upstream details are not copied
    into this Stage-A wire.
    """

    source_packet = packet if packet is not None else context_packet
    source_has_topic_hints = isinstance(source_packet, Mapping) and GROUPING_HINTS_FIELD in source_packet
    source_topic_hints = source_packet.get(GROUPING_HINTS_FIELD) if source_has_topic_hints else None
    scope, messages, candidates = _unpack_source_packet(
        scope, messages, candidates, packet, context_packet
    )
    if scope is None:
        _fail("scope_missing")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        _fail("messages_shape")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        _fail("candidates_shape")
    if not messages:
        _fail("messages_empty")
    if len(messages) > MAX_MESSAGES:
        _fail("messages_limit")
    if len(candidates) > MAX_CANDIDATES:
        _fail("candidates_limit")
    if context_unique != CONTEXT_UNIQUENESS_MODE:
        _fail("request_context_unique")
    if topic_hints is not None and grouping_hints is not None:
        _fail("request_grouping_hints")
    if topic_hints is not None:
        explicit_topic_hints = topic_hints
    elif grouping_hints is not None:
        explicit_topic_hints = grouping_hints
    elif source_has_topic_hints:
        explicit_topic_hints = source_topic_hints
    else:
        explicit_topic_hints = None

    scope_wire = _normalise_scope_wire(scope)
    scope_pair = (scope_wire["a"], scope_wire["c"])
    rows: List[Dict[str, Any]] = []
    hint_messages: List[Dict[str, Any]] = []
    hint_candidates: List[Dict[str, Any]] = []
    seen_handles: set[str] = set()
    seen_message_identities: set[str] = set()

    for value in messages:
        handle = _handle_from_record(value, "message_handle_missing")
        _assert_scope(handle, scope_pair)
        _assert_handle_kind(handle, "m")
        if handle in seen_handles:
            _fail("duplicate_authoritative_handle")
        seen_handles.add(handle)
        # A single authoritative message can arrive through several K2
        # fragments/aliases.  Keep one row and one bounded cue for it; this
        # is local de-duplication, not semantic merging of distinct ids.
        identity = _message_identity_for_dedup(value)
        if identity and identity != handle and identity in seen_message_identities:
            continue
        if identity:
            seen_message_identities.add(identity)
        message_alias = "m%d" % (sum(1 for row in rows if row.get("k") == "m") + 1)
        role = _role_from_record(value)
        cue = _text_from_record(value)
        rows.append(
            {
                "i": message_alias,
                "k": "m",
                "h": handle,
                "r": role,
                "x": cue,
            }
        )
        # Keep the richer row only in memory for deterministic preflight.  It
        # is never copied into the provider wire; the hint extractor reads a
        # narrow set of identity/subject/object/action/state/reply fields.
        hint_record = dict(value) if isinstance(value, Mapping) else {"handle": handle}
        hint_record.update({"i": message_alias, "k": "m", "h": handle, "r": role})
        hint_messages.append(hint_record)

    for index, value in enumerate(candidates, 1):
        handle = _handle_from_record(value, "candidate_handle_missing")
        _assert_scope(handle, scope_pair)
        _assert_handle_kind(handle, "c")
        if handle in seen_handles:
            _fail("duplicate_authoritative_handle")
        seen_handles.add(handle)
        candidate_alias = "c%d" % index
        rows.append({"i": candidate_alias, "k": "c", "h": handle})
        hint_record = dict(value) if isinstance(value, Mapping) else {"handle": handle}
        hint_record.update({"i": candidate_alias, "k": "c", "h": handle})
        hint_candidates.append(hint_record)

    # Keep the uniqueness mode explicit in the request so an adapter cannot
    # silently reinterpret context ownership as a per-topic hint.  It is a
    # scalar contract marker only; no message body or identity material is
    # added by this field.
    wire_packet: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "s": scope_wire,
        "h": rows,
        CONTEXT_UNIQUENESS_FIELD: context_unique,
    }
    if uncertainty_ceiling is not None:
        if uncertainty_ceiling not in {"uncertain", "unknown"}:
            _fail("request_uncertainty_ceiling")
        wire_packet["u"] = str(uncertainty_ceiling)
    if explicit_topic_hints is None:
        generated_hints = build_topic_candidate_hints(hint_messages, hint_candidates)
    else:
        # Validation below is deliberately strict: an explicit hint list is
        # not repaired, deduplicated, or coerced before it reaches the wire.
        generated_hints = explicit_topic_hints
    if generated_hints or explicit_topic_hints is not None:
        wire_packet[GROUPING_HINTS_FIELD] = generated_hints
    if explicit_topic_hints is None and generated_hints:
        # The preflight selector already applies deterministic scarcity,
        # relation-family, and alias caps.  A page can still have unusually
        # long scoped handles/cues, so fit only the *generated* hint tail to
        # the measured HTTP envelope.  Primary/context rows and their cues
        # are never removed, and an explicit caller-supplied ``g`` remains
        # strict (no repair/coercion).
        fitted_hints = list(generated_hints)
        while fitted_hints:
            wire_packet[GROUPING_HINTS_FIELD] = fitted_hints
            if measure_full_http_messages(SYSTEM_PROMPT, wire_packet)["http_token_proxy"] <= int(max_input_token_proxy):
                break
            fitted_hints.pop()
        if fitted_hints:
            wire_packet[GROUPING_HINTS_FIELD] = fitted_hints
        else:
            wire_packet.pop(GROUPING_HINTS_FIELD, None)
    validate_compact_stage_a_request(wire_packet)
    stats = measure_wire_size(wire_packet)
    if stats.http_token_proxy > int(max_input_token_proxy):
        _fail("request_input_token_proxy_exceeded")
    return wire_packet


def validate_compact_stage_a_request(value: Any) -> Dict[str, Any]:
    """Strictly validate and normalise one v3 request."""

    packet = dict(_mapping(value, "request_shape"))
    if not TOP_LEVEL_KEYS <= set(packet) or not set(packet) <= (TOP_LEVEL_KEYS | REQUEST_OPTIONAL_KEYS):
        _fail("request_keys")
    if packet.get("v") != PROTOCOL_VERSION:
        _fail("request_version")
    ceiling = packet.get("u")
    if ceiling is not None and ceiling not in {"uncertain", "unknown"}:
        _fail("request_uncertainty_ceiling")
    scope = _mapping(packet.get("s"), "request_scope_shape")
    _exact_keys(scope, {"a", "c"}, "request_scope_keys")
    account = _safe_string(scope.get("a"), "request_scope_account", max_length=MAX_HANDLE_CHARS)
    chat = _safe_string(scope.get("c"), "request_scope_chat", max_length=MAX_HANDLE_CHARS)
    scope_pair = (account, chat)
    context_unique = packet.get(CONTEXT_UNIQUENESS_FIELD, CONTEXT_UNIQUENESS_MODE)
    if context_unique != CONTEXT_UNIQUENESS_MODE:
        _fail("request_context_unique")

    rows = packet.get("h")
    if type(rows) is not list or not rows:
        _fail("request_handle_table")
    if len(rows) > MAX_MESSAGES + MAX_CANDIDATES:
        _fail("request_handle_table_limit")

    aliases: set[str] = set()
    handles: set[str] = set()
    message_count = 0
    candidate_count = 0
    normalized_rows: List[Dict[str, Any]] = []
    for raw_row in rows:
        row = dict(_mapping(raw_row, "request_handle_row_shape"))
        kind = row.get("k")
        expected = MESSAGE_ROW_KEYS if kind == "m" else CANDIDATE_ROW_KEYS if kind == "c" else ()
        if not expected:
            _fail("request_handle_kind")
        _exact_keys(row, expected, "request_handle_row_keys")
        alias = _safe_alias(row.get("i"), "request_alias_invalid")
        if alias in aliases:
            _fail("duplicate_request_alias")
        if (kind == "m" and not alias.startswith("m")) or (kind == "c" and not alias.startswith("c")):
            _fail("request_alias_kind")
        aliases.add(alias)
        handle = _safe_handle(row.get("h"), "request_authoritative_handle_invalid")
        _assert_scope(handle, scope_pair)
        _assert_handle_kind(handle, str(kind))
        if handle in handles:
            _fail("duplicate_authoritative_handle")
        handles.add(handle)
        if kind == "m":
            message_count += 1
            if message_count > MAX_MESSAGES:
                _fail("request_message_limit")
            role = row.get("r")
            if role not in {"p", "c"}:
                _fail("request_message_role")
            cue = row.get("x")
            if type(cue) is not str or len(cue) > MAX_MESSAGE_CUE_CHARS or _CONTROL_RE.search(cue):
                _fail("request_message_cue")
            if cue != cue.strip():
                _fail("request_message_cue")
            normalized_rows.append({"i": alias, "k": "m", "h": handle, "r": role, "x": cue})
        else:
            candidate_count += 1
            if candidate_count > MAX_CANDIDATES:
                _fail("request_candidate_limit")
            normalized_rows.append({"i": alias, "k": "c", "h": handle})

    if message_count == 0:
        _fail("request_messages_empty")
    if not any(row["k"] == "m" and row["r"] == "p" for row in normalized_rows):
        _fail("request_primary_messages_empty")
    message_aliases = {
        str(row["i"]) for row in normalized_rows if row["k"] == "m"
    }
    candidate_aliases = {
        str(row["i"]) for row in normalized_rows if row["k"] == "c"
    }
    grouping_hints: Optional[List[Dict[str, str]]] = None
    if GROUPING_HINTS_FIELD in packet:
        grouping_hints = _validate_grouping_hints(
            packet[GROUPING_HINTS_FIELD],
            aliases,
            message_aliases,
            candidate_aliases,
        )
    result: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "s": {"a": account, "c": chat},
        "h": normalized_rows,
    }
    if ceiling is not None:
        result["u"] = str(ceiling)
    if CONTEXT_UNIQUENESS_FIELD in packet:
        result[CONTEXT_UNIQUENESS_FIELD] = CONTEXT_UNIQUENESS_MODE
    if grouping_hints is not None:
        result[GROUPING_HINTS_FIELD] = grouping_hints
    return result


def preflight_compact_stage_a_request(value: Any) -> Dict[str, Any]:
    """Return a body-free structural preflight for one v3 request.

    Preflight reports only alias counts and fixed relation labels.  It never
    assigns topics, promotes a primary, or repairs a malformed hint list;
    callers still need a model response and the strict output validator.
    """

    packet = validate_compact_stage_a_request(value)
    hints = list(packet.get(GROUPING_HINTS_FIELD, ()))
    wire = measure_wire_size(packet)
    components = build_candidate_connected_components(packet, hints=hints)
    relations = sorted(
        {str(item["r"]) for item in hints if isinstance(item, Mapping)},
        key=lambda item: tuple(
            TOPIC_HINT_RELATION_ORDER.index(part)
            for part in item.split("+")
            if part in TOPIC_HINT_RELATION_ORDER
        ),
    )
    return {
        "valid": True,
        "protocol_version": PROTOCOL_VERSION,
        "message_count": sum(row["k"] == "m" for row in packet["h"]),
        "candidate_count": sum(row["k"] == "c" for row in packet["h"]),
        "primary_aliases": _semantic_primary_aliases_from_packet(packet),
        "context_aliases": [
            str(row["i"])
            for row in packet["h"]
            if row["k"] == "m" and row["r"] == "c"
        ],
        "grouping_hint_count": len(hints),
        "grouping_hint_relations": relations,
        "candidate_connected_components": components["components"],
        "candidate_merge_prior": components["merge_prior"],
        "candidate_split_evidence_count": components["split_evidence_count"],
        "candidate_only": True,
        "topic_assignment_is_local": False,
        "token_calibration_version": TOKEN_CALIBRATION_VERSION,
        "http_token_proxy": wire.http_token_proxy,
        "calibrated_input_token_proxy": wire.calibrated_input_proxy,
        "within_calibrated_input_limit": wire.calibrated_input_proxy <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT,
        "hints_are_structural": True,
        "model_decision_required": True,
        "body_free": True,
    }


def _primary_aliases_from_packet(packet: Mapping[str, Any]) -> List[str]:
    return [str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "p"]


def _semantic_primary_aliases_from_packet(packet: Mapping[str, Any]) -> List[str]:
    return [
        str(row["i"])
        for row in packet["h"]
        if row["k"] == "m" and row["r"] == "p" and _semantic_cue_is_eligible(row.get("x", ""))
    ]


def _nonsemantic_primary_aliases_from_packet(packet: Mapping[str, Any]) -> List[str]:
    return [
        str(row["i"])
        for row in packet["h"]
        if row["k"] == "m" and row["r"] == "p" and not _semantic_cue_is_eligible(row.get("x", ""))
    ]


def topic_limit_for_request(request: Mapping[str, Any]) -> int:
    packet = validate_compact_stage_a_request(request)
    return len(_semantic_primary_aliases_from_packet(packet))


def _string_list(value: Any, code: str, *, allow_empty: bool = True) -> List[str]:
    if type(value) is not list or (not allow_empty and not value):
        _fail(code)
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        item_text = _safe_alias(item, code + "_item")
        if item_text in seen:
            _fail(code + "_duplicate")
        seen.add(item_text)
        result.append(item_text)
    return result


def _empty_context_telemetry() -> Dict[str, Any]:
    counts = {name: 0 for name in CONTEXT_ERROR_TELEMETRY_KEYS}
    flags = {name: False for name in CONTEXT_ERROR_TELEMETRY_KEYS}
    return {
        "telemetry_version": CONTEXT_VALIDATION_TAXONOMY_VERSION,
        "error_counts": counts,
        "error_flags": flags,
        "has_error": False,
        "body_free": True,
    }


def _body_free_context_telemetry(value: Any) -> Dict[str, Any]:
    """Sanitize a telemetry mapping without retaining aliases or payloads."""

    result = _empty_context_telemetry()
    if not isinstance(value, Mapping):
        return result
    source = value.get("error_counts", value.get("counts", value))
    if isinstance(source, Mapping):
        for name in CONTEXT_ERROR_TELEMETRY_KEYS:
            raw = source.get(name, 0)
            try:
                number = int(raw)
            except (TypeError, ValueError):
                number = 0
            result["error_counts"][name] = max(0, number)
    result["error_flags"] = {
        name: bool(result["error_counts"].get(name, 0))
        for name in CONTEXT_ERROR_TELEMETRY_KEYS
    }
    result["has_error"] = any(result["error_flags"].values())
    # A compact alias is useful to callers, but both views contain only the
    # fixed taxonomy and scalar booleans/counts.
    result["counts"] = dict(result["error_counts"])
    result["flags"] = dict(result["error_flags"])
    return result


def context_validation_telemetry(value: Any, request: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Classify context failures at parse time using body-free counters.

    The function intentionally does not return offending aliases, message
    bodies, raw JSON, or exception text.  It is safe to attach to an exception
    and to persist in an offline diagnostic ledger.
    """

    result = _empty_context_telemetry()
    counts = result["error_counts"]
    if isinstance(value, str):
        try:
            value = strict_json_loads(value)
        except Exception:
            counts["invalid_type"] += 1
            result["error_flags"]["invalid_type"] = True
            result["has_error"] = True
            result["counts"] = dict(counts)
            result["flags"] = dict(result["error_flags"])
            return result
    if not isinstance(value, Mapping):
        counts["invalid_type"] += 1
        result["error_flags"]["invalid_type"] = True
        result["has_error"] = True
        result["counts"] = dict(counts)
        result["flags"] = dict(result["error_flags"])
        return result

    allowed_context: set[str] = set()
    known_primary: set[str] = set()
    candidate_aliases: set[str] = set()
    if isinstance(request, Mapping):
        rows = request.get("h", ())
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                alias = row.get("i")
                if type(alias) is not str:
                    continue
                kind = row.get("k")
                if kind == "m" and row.get("r") == "c":
                    allowed_context.add(alias)
                elif kind == "m" and row.get("r") == "p":
                    known_primary.add(alias)
                elif kind == "c":
                    candidate_aliases.add(alias)

    raw_topics = value.get("topics")
    if type(raw_topics) is not list:
        counts["invalid_type"] += 1
        raw_topics = ()
    owners: Dict[str, int] = {}
    for topic_index, raw_topic in enumerate(raw_topics):
        if not isinstance(raw_topic, Mapping):
            counts["invalid_type"] += 1
            continue
        primary_value = raw_topic.get("primary_message_ids")
        context_value = raw_topic.get("context_message_ids")
        primary_items: Sequence[Any]
        context_items: Sequence[Any]
        if type(primary_value) is list:
            primary_items = primary_value
        else:
            counts["invalid_type"] += 1
            primary_items = ()
        if type(context_value) is list:
            context_items = context_value
        else:
            counts["invalid_type"] += 1
            context_items = ()
        primary_strings = {item for item in primary_items if type(item) is str}
        context_strings = {item for item in context_items if type(item) is str}
        counts["primary_context_overlap"] += len(primary_strings.intersection(context_strings))
        local_seen: set[str] = set()
        for item in context_items:
            if type(item) is not str:
                counts["invalid_type"] += 1
                continue
            if item in local_seen:
                counts["duplicate_within_topic"] += 1
            local_seen.add(item)
            if item in owners and owners[item] != topic_index:
                counts["duplicate_across_topics"] += 1
            else:
                owners.setdefault(item, topic_index)
            if item in known_primary:
                counts["primary_as_context"] += 1
            elif item not in allowed_context:
                # This single category deliberately covers both a candidate
                # alias and an unknown alias; the subtype never leaves the
                # protocol boundary.
                counts["candidate_or_unknown_alias"] += 1

    result["error_flags"] = {
        name: bool(counts[name]) for name in CONTEXT_ERROR_TELEMETRY_KEYS
    }
    result["has_error"] = any(result["error_flags"].values())
    result["counts"] = dict(counts)
    result["flags"] = dict(result["error_flags"])
    return result


def _attach_context_telemetry(
    error: CompactStageAProtocolV3Error,
    value: Any,
    request: Optional[Mapping[str, Any]],
) -> CompactStageAProtocolV3Error:
    telemetry = context_validation_telemetry(value, request)
    error.context_telemetry = telemetry
    error.context_error_counts = dict(telemetry.get("error_counts", {}))
    error.context_error_flags = dict(telemetry.get("error_flags", {}))
    return error


def _validate_compact_stage_a_output_impl(value: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly validate the descriptive v3 topic assignment output."""

    packet = validate_compact_stage_a_request(request)
    # ``validate_compact_stage_a_request`` already rejects a non-global marker;
    # keep the explicit guard here as a contract invariant next to the global
    # ``seen_context`` allocation set below.
    if packet.get(CONTEXT_UNIQUENESS_FIELD, CONTEXT_UNIQUENESS_MODE) != CONTEXT_UNIQUENESS_MODE:
        _fail("request_context_unique")
    root = dict(_mapping(value, "output_shape"))
    _exact_keys(root, OUTPUT_TOP_KEYS, "output_keys")
    raw_topics = root.get("topics")
    if type(raw_topics) is not list or not raw_topics:
        _fail("output_topics")

    primary_aliases = _semantic_primary_aliases_from_packet(packet)
    all_primary_aliases = set(_primary_aliases_from_packet(packet))
    nonsemantic_primary_aliases = set(_nonsemantic_primary_aliases_from_packet(packet))
    context_aliases = [
        str(row["i"])
        for row in packet["h"]
        if row["k"] == "m" and row["r"] == "c"
    ]

    allowed_primary = set(primary_aliases)
    allowed_context = set(context_aliases)
    # Use the declared primary-role capacity for this early structural guard.
    # A forged ineligible primary is diagnosed below with the semantic error
    # code rather than being hidden behind a topic-count error.
    if len(raw_topics) > len(all_primary_aliases):
        _fail("output_topic_limit")

    seen_topics: set[str] = set()
    seen_primary: set[str] = set()
    # This set is intentionally shared across the complete response, not
    # reset for each topic: a context alias has one global owner at most.
    seen_context: set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for raw_topic in raw_topics:
        topic = dict(_mapping(raw_topic, "output_topic_shape"))
        _exact_keys(topic, OUTPUT_TOPIC_KEYS, "output_topic_keys")
        topic_id = _safe_topic_id(topic.get("topic_id"))
        if topic_id in seen_topics:
            _fail("duplicate_topic_id")
        seen_topics.add(topic_id)
        primary = _string_list(topic.get("primary_message_ids"), "output_primary", allow_empty=False)
        context = _string_list(topic.get("context_message_ids"), "output_context", allow_empty=True)
        uncertainty = topic.get("uncertainty")
        if uncertainty not in UNCERTAINTY_VALUES:
            _fail("output_uncertainty_enum")
        if packet.get("u") == "uncertain" and uncertainty == "certain":
            _fail("uncertainty_overstated_unresolved_reference")
        # Check semantic eligibility before the ordinary out-of-scope check so
        # a model promoting a known greeting/media/authority alias gets the
        # stable diagnostic promised by the provider-facing contract.
        if any(item in nonsemantic_primary_aliases for item in primary):
            _fail("primary_alias_not_semantic_eligible")
        if not set(primary) <= allowed_primary:
            _fail("output_primary_item")
        if not set(context) <= allowed_context:
            _fail("output_context_item")
        if set(primary).intersection(context):
            _fail("output_primary_context_overlap")
        if seen_primary.intersection(primary):
            _fail("duplicate_primary_handle")
        if seen_context.intersection(context):
            _fail("duplicate_context_handle")
        seen_primary.update(primary)
        seen_context.update(context)
        normalized.append(
            {
                "topic_id": topic_id,
                "primary_message_ids": primary,
                "context_message_ids": context,
                "uncertainty": str(uncertainty),
            }
        )

    if seen_primary != allowed_primary:
        _fail("primary_coverage")
    result = {"topics": normalized}
    output_stats = measure_output_size(result)
    if output_stats["token_proxy"] > MAX_OUTPUT_TOKENS:
        _fail("output_token_proxy_exceeded")
    return result


def validate_compact_stage_a_output(value: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Strict output validation with body-free context failure telemetry."""

    try:
        return _validate_compact_stage_a_output_impl(value, request)
    except CompactStageAProtocolV3Error as error:
        _attach_context_telemetry(error, value, request)
        raise


def parse_compact_stage_a_output(text: str, request: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        parsed = strict_json_loads(text)
        return validate_compact_stage_a_output(parsed, request)
    except CompactStageAProtocolV3Error as error:
        _attach_context_telemetry(error, text, request)
        raise


def resolve_compact_output(value: Any, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve aliases to authoritative handles after local validation."""

    compact = validate_compact_stage_a_output(value, request)
    packet = validate_compact_stage_a_request(request)
    table = {str(row["i"]): str(row["h"]) for row in packet["h"] if row["k"] == "m"}
    return {
        "topics": [
            {
                "topic_id": topic["topic_id"],
                "primary_message_ids": [table[item] for item in topic["primary_message_ids"]],
                "context_message_ids": [table[item] for item in topic["context_message_ids"]],
                "uncertainty": topic["uncertainty"],
            }
            for topic in compact["topics"]
        ]
    }


def canonical_http_messages(system_prompt: str, request: Mapping[str, Any]) -> str:
    if type(system_prompt) is not str or _CONTROL_RE.search(system_prompt):
        _fail("system_prompt_invalid")
    canonical_user = canonical_json(request)
    return canonical_json(
        {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_user},
            ]
        }
    )


def _proxy(chars: int) -> int:
    return int(math.ceil(max(0, chars) / float(TOKEN_PROXY_CHARS)))


def calibrate_token_proxy(proxy: Any) -> int:
    """Return the conservative calibrated input-token proxy.

    ``proxy`` is deliberately accepted as a scalar only.  Callers retain the
    raw proxy for comparison, while the calibrated value is the hard-gate
    value used by the offline development runner.  Invalid values are treated
    as zero here so this helper remains a pure measurement function; request
    validation still fails closed before it is called by the protocol.
    """

    try:
        numeric = float(proxy)
    except (TypeError, ValueError):
        numeric = 0.0
    if not math.isfinite(numeric):
        numeric = 0.0
    numeric = max(0.0, numeric)
    return int(math.ceil(max(numeric * TOKEN_CALIBRATION_MULTIPLIER, numeric + TOKEN_CALIBRATION_ADDEND)))


calibrated_token_proxy = calibrate_token_proxy


def calibrated_input_token_proxy(proxy: Any) -> int:
    return calibrate_token_proxy(proxy)


def within_calibrated_input_limit(
    proxy: Any,
    limit: int = CALIBRATED_INPUT_TOKEN_PROXY_LIMIT,
) -> bool:
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = CALIBRATED_INPUT_TOKEN_PROXY_LIMIT
    return calibrate_token_proxy(proxy) <= cap


def measure_full_http_messages(system_prompt: str, request: Mapping[str, Any]) -> Dict[str, Any]:
    """Measure an envelope without retaining request or response bodies."""

    user = canonical_json(request)
    messages = canonical_http_messages(system_prompt, request)
    schema = canonical_json(
        {
            "topics": [
                {
                    "topic_id": "t1",
                    "primary_message_ids": ["m1"],
                    "context_message_ids": [],
                    "uncertainty": "unknown",
                }
            ]
        }
    )
    http_token_proxy = _proxy(len(messages))
    calibrated_proxy = calibrate_token_proxy(http_token_proxy)
    return {
        "system_chars": len(system_prompt),
        "user_chars": len(user),
        "messages_chars": len(messages),
        "schema_chars": len(schema),
        "system_bytes": len(system_prompt.encode("utf-8")),
        "user_bytes": len(user.encode("utf-8")),
        "messages_bytes": len(messages.encode("utf-8")),
        "schema_bytes": len(schema.encode("utf-8")),
        "system_token_proxy": _proxy(len(system_prompt)),
        "user_token_proxy": _proxy(len(user)),
        "input_token_proxy": _proxy(len(system_prompt) + len(user)),
        "http_token_proxy": http_token_proxy,
        "calibrated_input_token_proxy": calibrated_proxy,
        "token_calibration_version": TOKEN_CALIBRATION_VERSION,
        "within_calibrated_input_limit": calibrated_proxy <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT,
        "schema_token_proxy": _proxy(len(schema)),
        "messages_sha256": hashlib.sha256(messages.encode("utf-8")).hexdigest(),
    }


@dataclass(frozen=True)
class WireSizeStats:
    system_chars: int
    user_chars: int
    messages_chars: int
    schema_chars: int
    system_bytes: int
    user_bytes: int
    messages_bytes: int
    schema_bytes: int
    system_token_proxy: int
    user_token_proxy: int
    input_token_proxy: int
    http_token_proxy: int
    schema_token_proxy: int
    messages_sha256: str
    calibrated_input_token_proxy: int = 0
    token_calibration_version: str = TOKEN_CALIBRATION_VERSION
    within_calibrated_input_limit: bool = False

    @property
    def within_input_limit(self) -> bool:
        return self.http_token_proxy <= MAX_INPUT_TOKEN_PROXY

    @property
    def calibrated_input_proxy(self) -> int:
        return self.calibrated_input_token_proxy or calibrate_token_proxy(self.http_token_proxy)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "system_chars": self.system_chars,
            "user_chars": self.user_chars,
            "messages_chars": self.messages_chars,
            "schema_chars": self.schema_chars,
            "system_bytes": self.system_bytes,
            "user_bytes": self.user_bytes,
            "messages_bytes": self.messages_bytes,
            "schema_bytes": self.schema_bytes,
            "system_token_proxy": self.system_token_proxy,
            "user_token_proxy": self.user_token_proxy,
            "input_token_proxy": self.input_token_proxy,
            "http_token_proxy": self.http_token_proxy,
            "schema_token_proxy": self.schema_token_proxy,
            "within_input_limit": self.within_input_limit,
            "calibrated_input_token_proxy": self.calibrated_input_proxy,
            "token_calibration_version": self.token_calibration_version,
            "within_calibrated_input_limit": self.within_calibrated_input_limit
            if self.calibrated_input_token_proxy
            else self.calibrated_input_proxy <= CALIBRATED_INPUT_TOKEN_PROXY_LIMIT,
            "messages_sha256": self.messages_sha256,
        }


def measure_wire_size(
    request: Mapping[str, Any], system_prompt: str = SYSTEM_PROMPT
) -> WireSizeStats:
    validate_compact_stage_a_request(request)
    return WireSizeStats(**measure_full_http_messages(system_prompt, request))


def measure_output_size(value: Mapping[str, Any]) -> Dict[str, int]:
    text = canonical_json(value)
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "token_proxy": _proxy(len(text)),
    }


def compare_wire_sizes(
    old_system_prompt: str,
    old_request: Mapping[str, Any],
    new_request: Mapping[str, Any],
    *,
    new_system_prompt: str = SYSTEM_PROMPT,
) -> Dict[str, Any]:
    """Return body-free old/new request size deltas for offline evaluation."""

    old = measure_full_http_messages(old_system_prompt, old_request)
    new = measure_full_http_messages(new_system_prompt, new_request)
    return {
        "old": old,
        "new": new,
        "reduction": {
            "messages_chars": old["messages_chars"] - new["messages_chars"],
            "messages_bytes": old["messages_bytes"] - new["messages_bytes"],
            "http_token_proxy": old["http_token_proxy"] - new["http_token_proxy"],
        },
        "new_within_input_limit": new["http_token_proxy"] <= MAX_INPUT_TOKEN_PROXY,
    }


def build_max_size_response(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a largest-shape valid v3 response for a request."""

    packet = validate_compact_stage_a_request(request)
    primary = _semantic_primary_aliases_from_packet(packet)
    context_rows = [str(row["i"]) for row in packet["h"] if row["k"] == "m" and row["r"] == "c"]
    topics: List[Dict[str, Any]] = []
    for index, alias in enumerate(primary):
        context = [context_rows[index]] if index < len(context_rows) else []
        topics.append(
            {
                "topic_id": "topic-%d" % (index + 1),
                "primary_message_ids": [alias],
                "context_message_ids": context,
                "uncertainty": "unknown",
            }
        )
    result = validate_compact_stage_a_output({"topics": topics}, packet)
    if measure_output_size(result)["token_proxy"] > MAX_OUTPUT_TOKENS:
        _fail("max_size_response_exceeds_output_limit")
    return result


def max_size_proof(request: Mapping[str, Any]) -> Dict[str, Any]:
    packet = validate_compact_stage_a_request(request)
    response = build_max_size_response(packet)
    request_stats = measure_wire_size(packet)
    output_stats = measure_output_size(response)
    primary_count = len(_semantic_primary_aliases_from_packet(packet))
    return {
        "valid": True,
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "message_count": sum(row["k"] == "m" for row in packet["h"]),
        "candidate_count": sum(row["k"] == "c" for row in packet["h"]),
        "primary_count": primary_count,
        "topic_limit": primary_count,
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "context_unique": CONTEXT_UNIQUENESS_MODE,
        "context_uniqueness_rule": CONTEXT_UNIQUENESS_RULE,
        "topic_count": len(response["topics"]),
        "request": request_stats.to_dict(),
        "response": output_stats,
        "request_within_1600": request_stats.within_input_limit,
        "response_within_400": output_stats["token_proxy"] <= MAX_OUTPUT_TOKENS,
    }


def size_report(
    request: Mapping[str, Any], response: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Return body-free request/response wire statistics."""

    stats = measure_wire_size(request)
    result = stats.to_dict()
    if response is not None:
        validated = validate_compact_stage_a_output(response, request)
        output = measure_output_size(validated)
        result.update(
            {
                "output_chars": output["chars"],
                "output_bytes": output["bytes"],
                "output_token_proxy": output["token_proxy"],
            }
        )
    return result


def cache_context(
    request: Mapping[str, Any],
    *,
    model_id: str = "",
    ruleset_version: str = "",
) -> Dict[str, str]:
    """Return body-free, versioned cache inputs for one request."""

    validate_compact_stage_a_request(request)
    model = _safe_string(model_id, "model_id_invalid", max_length=128, allow_empty=True)
    ruleset = _safe_string(ruleset_version, "ruleset_version_invalid", max_length=128, allow_empty=True)
    return {
        "cache_version": CACHE_VERSION,
        "cache_namespace": CACHE_NAMESPACE,
        "protocol_version": PROTOCOL_VERSION,
        "wire_protocol_version": WIRE_PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "wire_prompt_version": WIRE_PROMPT_VERSION,
        "request_sha256": stable_hash(request),
        "model_id": model,
        "ruleset_version": ruleset,
    }


def cache_key(
    request: Mapping[str, Any],
    *,
    model_id: str = "",
    ruleset_version: str = "",
) -> str:
    return stable_hash(cache_context(request, model_id=model_id, ruleset_version=ruleset_version))


def project_body_free_ledger(
    request: Mapping[str, Any],
    response: Optional[Mapping[str, Any]] = None,
    report: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Project handles/counts/hashes without copying cues or provider text."""

    packet = validate_compact_stage_a_request(request)
    assignments: Optional[Dict[str, Any]] = None
    if response is not None:
        assignments = validate_compact_stage_a_output(response, packet)
    message_handles = [str(row["h"]) for row in packet["h"] if row["k"] == "m"]
    candidate_handles = [str(row["h"]) for row in packet["h"] if row["k"] == "c"]
    primary_count = len(_semantic_primary_aliases_from_packet(packet))
    projected_context_count = sum(
        row["k"] == "m" and row["r"] == "c" for row in packet["h"]
    )
    result: Dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "cache_version": CACHE_VERSION,
        "scope": dict(packet["s"]),
        "message_handles": message_handles,
        "candidate_handles": candidate_handles,
        "message_count": len(message_handles),
        "candidate_count": len(candidate_handles),
        "primary_count": primary_count,
        "projected_primary_count": primary_count,
        "projected_context_count": projected_context_count,
        "topic_limit": primary_count,
        "topic_limit_cause": "projected_primary_count" if primary_count else "no_projected_primary",
        "topic_limit_rule": TOPIC_LIMIT_RULE,
        "context_unique": packet.get(CONTEXT_UNIQUENESS_FIELD, CONTEXT_UNIQUENESS_MODE),
        "context_uniqueness_rule": CONTEXT_UNIQUENESS_RULE,
        "request_sha256": stable_hash(packet),
    }
    if packet.get("u") is not None:
        # This is a scalar protocol guard, not provider text.  Preserve it in
        # the body-free projection so an audit can see why ``certain`` was
        # disallowed without retaining the request body.
        result["uncertainty_ceiling"] = str(packet["u"])
    if assignments is not None:
        result["assignments"] = assignments
        result["topic_count"] = len(assignments["topics"])
    if report is None:
        result["size"] = size_report(packet, response)
    elif isinstance(report, WireSizeStats):
        result["size"] = report.to_dict()
    elif isinstance(report, Mapping):
        result["size"] = dict(report)
    else:
        _fail("ledger_report_shape")
    return result


# Friendly aliases for future runners and independent tests.  They all point
# to the v3 implementation, so none accidentally accepts the v2 short-key
# response.
build_request = build_compact_stage_a_request
build_stage_a_request = build_compact_stage_a_request
build_canonical_request = build_compact_stage_a_request
build_topic_grouping_hints = build_topic_candidate_hints
deterministic_topic_grouping_hints = build_topic_candidate_hints
preflight_topic_hints = build_topic_candidate_hints
validate_request = validate_compact_stage_a_request
preflight_request = preflight_compact_stage_a_request
preflight_stage_a_request = preflight_compact_stage_a_request
validate_output = validate_compact_stage_a_output
validate_response = validate_compact_stage_a_output
validate_stage_a_response = validate_compact_stage_a_output
parse_output = parse_compact_stage_a_output
request_size = measure_wire_size
output_size = measure_output_size
request_size_report = size_report
stage_a_size_report = size_report
project_ledger = project_body_free_ledger
ledger_projection = project_body_free_ledger

PUBLIC_API = {
    "builder": "build_compact_stage_a_request",
    "validator": "validate_compact_stage_a_output",
    "parser": "parse_compact_stage_a_output",
    "sizer": "size_report",
    "cache": "cache_key",
    "ledger": "project_body_free_ledger",
}

__all__ = [
    "PROTOCOL_VERSION",
    "STAGE_A_PROTOCOL_VERSION",
    "STAGE_A_SCHEMA_VERSION",
    "COMPACT_STAGE_A_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "CACHE_VERSION",
    "CACHE_NAMESPACE",
    "CACHE_SCHEMA_VERSION",
    "WIRE_PROTOCOL_VERSION",
    "WIRE_PROMPT_VERSION",
    "TOPIC_LIMIT_RULE",
    "TOPIC_GROUPING_GUIDANCE",
    "CONTEXT_UNIQUENESS_FIELD",
    "CONTEXT_UNIQUENESS_MODE",
    "CONTEXT_UNIQUENESS_RULE",
    "MINIMAL_MULTI_TOPIC_VALID_EXAMPLE",
    "MINIMAL_MULTI_TOPIC_RESPONSE_EXAMPLE",
    "DUPLICATE_CONTEXT_COUNTEREXAMPLE",
    "MULTI_PRIMARY_ONE_TOPIC_EXAMPLE",
    "MINIMAL_RESPONSE_EXAMPLE",
    "SYSTEM_PROMPT",
    "STAGE_A_SYSTEM_PROMPT",
    "TOP_LEVEL_KEYS",
    "REQUEST_OPTIONAL_KEYS",
    "MESSAGE_ROW_KEYS",
    "CANDIDATE_ROW_KEYS",
    "OUTPUT_TOP_KEYS",
    "OUTPUT_TOPIC_KEYS",
    "OUTPUT_FIELDS",
    "TOPIC_FIELDS",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "UNCERTAINTY_VALUES",
    "MAX_MESSAGES",
    "MAX_CANDIDATES",
    "MAX_HANDLE_CHARS",
    "MAX_ALIAS_CHARS",
    "MAX_MESSAGE_CUE_CHARS",
    "MAX_INPUT_TOKEN_PROXY",
    "TOKEN_CALIBRATION_VERSION",
    "TOKEN_CALIBRATION_MULTIPLIER",
    "TOKEN_CALIBRATION_ADDEND",
    "CALIBRATED_INPUT_TOKEN_PROXY_LIMIT",
    "CONTEXT_VALIDATION_TAXONOMY_VERSION",
    "CONTEXT_ERROR_TAXONOMY_VERSION",
    "CONTEXT_ERROR_TELEMETRY_KEYS",
    "TOPIC_SPLIT_RELATIONS",
    "MAX_OUTPUT_TOKENS",
    "GROUPING_HINTS_FIELD",
    "TOPIC_HINT_KEYS",
    "TOPIC_HINT_RELATIONS",
    "TOPIC_HINT_RELATION_ORDER",
    "MAX_TOPIC_GROUPING_HINTS",
    "MAX_GENERATED_TOPIC_GROUPING_HINTS",
    "MAX_TOPIC_HINTS_PER_ALIAS",
    "MAX_TOPIC_HINTS_PER_RELATION",
    "ERROR_TAXONOMY_VERSION",
    "VALIDATION_CATEGORIES",
    "CONTEXT_UNIQUENESS_ERROR_CODES",
    "KNOWN_VALIDATION_ERROR_CODES",
    "validation_categories_for_code",
    "CompactStageAProtocolV3Error",
    "CompactStageAProtocolError",
    "ProtocolError",
    "WireSizeStats",
    "canonical_json",
    "stable_hash",
    "strict_json_loads",
    "build_compact_stage_a_request",
    "build_request",
    "build_stage_a_request",
    "build_canonical_request",
    "build_topic_candidate_hints",
    "build_candidate_connected_components",
    "candidate_connected_components",
    "derive_candidate_connected_components",
    "build_topic_merge_prior",
    "deterministic_topic_candidate_hints",
    "topic_candidate_hints",
    "build_topic_grouping_hints",
    "deterministic_topic_grouping_hints",
    "preflight_topic_hints",
    "validate_compact_stage_a_request",
    "validate_request",
    "preflight_compact_stage_a_request",
    "preflight_request",
    "preflight_stage_a_request",
    "topic_limit_for_request",
    "_semantic_primary_aliases_from_packet",
    "validate_compact_stage_a_output",
    "context_validation_telemetry",
    "validate_output",
    "validate_response",
    "validate_stage_a_response",
    "parse_compact_stage_a_output",
    "parse_output",
    "resolve_compact_output",
    "canonical_http_messages",
    "measure_full_http_messages",
    "measure_wire_size",
    "calibrate_token_proxy",
    "calibrated_token_proxy",
    "calibrated_input_token_proxy",
    "within_calibrated_input_limit",
    "request_size",
    "measure_output_size",
    "output_size",
    "size_report",
    "request_size_report",
    "stage_a_size_report",
    "compare_wire_sizes",
    "build_max_size_response",
    "max_size_proof",
    "cache_context",
    "cache_key",
    "project_body_free_ledger",
    "project_ledger",
    "ledger_projection",
    "PUBLIC_API",
]
